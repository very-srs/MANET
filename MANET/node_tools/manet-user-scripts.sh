#!/usr/bin/env bash
# ==============================================================================
# Operator setup scripts — one-time post-provisioning hook
# ==============================================================================
# Runs whatever the operator put in provisioning/additional-scripts/ at flash
# time. Those files are baked into firstrun.sh as heredocs and written out to
# SCRIPT_DIR on the very first boot; this runner is what executes them, once,
# after radio-setup has finished and the node is a working mesh node.
#
# Why a separate runner rather than a few lines at the end of radio-setup:
#
#   * radio-setup must not be at the mercy of operator code. It is the script
#     that decides whether a node is provisioned at all, and it re-runs on a
#     config change. A user script that hangs, reboots, or exits non-zero
#     inside it would take provisioning with it.
#   * These run ONCE per node, not on every radio-setup. radio-setup is
#     explicitly re-runnable ("This script can be re-run to set new network
#     settings"); a site hook that adds a route or installs a package is not.
#   * A one-shot unit survives a power pull. If the board loses power part-way
#     through, the completion marker was never written and the next boot picks
#     up where it stopped.
#
# Failures are ADVISORY. A broken operator script must never make a working
# mesh node report itself unprovisioned, so nothing here touches
# /var/lib/manet-provision.{state,failures}. What it does instead is record
# every exit code and let manet-provision-status.sh say so on the login banner.
#
# Usage:
#   manet-user-scripts.sh            # normal one-time run (what the unit does)
#   manet-user-scripts.sh --force    # re-run everything, ignoring past state
#   manet-user-scripts.sh --list     # show what is staged and what has run
# ==============================================================================

SCRIPT_DIR="${MANET_USER_SCRIPT_DIR:-/var/lib/manet-user-scripts}"
STATE_FILE="${MANET_USER_SCRIPT_STATE:-/var/lib/manet-user-scripts.state}"
DONE_FILE="${MANET_USER_SCRIPT_DONE:-/var/lib/manet-user-scripts.done}"
LOG_FILE="${MANET_USER_SCRIPT_LOG:-/var/log/manet-user-scripts.log}"
MESH_CONF="${MANET_MESH_CONF:-/etc/mesh.conf}"

# Per-script wall-clock limit. An operator script that waits forever on a
# prompt, a dead mirror, or a link that never comes up would otherwise hold the
# unit open until systemd's TimeoutStartSec kills the whole run and the scripts
# after it never get their turn. Overridable per node with
# `user_script_timeout=` in /etc/mesh.conf.
DEFAULT_TIMEOUT=300

FORCE=0
LIST_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --list)  LIST_ONLY=1 ;;
        -h|--help)
            sed -n '2,32p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

# ── Per-script timeout from mesh.conf ────────────────────────────────────────
# Read by hand rather than sourced: mesh.conf is operator-editable and a stray
# line must not execute as shell here.
TIMEOUT="$DEFAULT_TIMEOUT"
if [ -r "$MESH_CONF" ]; then
    conf_timeout=$(sed -n 's/^user_script_timeout=\([0-9]\{1,\}\)[[:space:]]*$/\1/p' \
        "$MESH_CONF" | tail -1)
    [ -n "$conf_timeout" ] && [ "$conf_timeout" -gt 0 ] 2>/dev/null && TIMEOUT="$conf_timeout"
fi

# ── State ────────────────────────────────────────────────────────────────────
# One TAB-separated line per completed script: name, exit code, epoch. Written
# as each script finishes rather than in one batch at the end, so an
# interrupted run resumes instead of restarting — the script that was cut off
# mid-way has no line, and runs again; the ones before it do not.
mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true

already_ran() {
    [ -r "$STATE_FILE" ] || return 1
    cut -f1 "$STATE_FILE" 2>/dev/null | grep -qxF "$1"
}

record_result() {
    printf '%s\t%s\t%s\n' "$1" "$2" "$(date +%s)" >> "$STATE_FILE"
}

# ── Which files are candidates ───────────────────────────────────────────────
# The flasher already validated everything it embedded, so this is a second
# line of defence rather than the real gate — it also covers files an operator
# dropped in by hand on a live node, which is the only path that reaches here
# unvalidated. Lexical order, C collation, so the 10-/20-/30- convention means
# what it looks like it means. The shebang test that pairs with this lives in
# the run loop, where it can report the skip.
list_scripts() {
    [ -d "$SCRIPT_DIR" ] || return 0
    find "$SCRIPT_DIR" -maxdepth 1 -type f -printf '%f\n' 2>/dev/null \
        | grep -v -e '^\.' -e '\.disabled$' -e '\.bak$' -e '\.orig$' -e '~$' \
        | LC_ALL=C sort
}

if [ "$LIST_ONLY" -eq 1 ]; then
    found=0
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        found=1
        if already_ran "$name"; then
            code=$(awk -F'\t' -v n="$name" '$1 == n { c = $2 } END { print c }' \
                "$STATE_FILE" 2>/dev/null)
            printf '  %-40s ran, exit %s\n' "$name" "${code:-?}"
        else
            printf '  %-40s pending\n' "$name"
        fi
    done < <(list_scripts)
    [ "$found" -eq 0 ] && echo "  no operator scripts staged in $SCRIPT_DIR"
    exit 0
fi

# ── Run ──────────────────────────────────────────────────────────────────────
# Everything from here is teed to the log. Append, never truncate, for the same
# reason radio-setup.sh appends: this can run more than once (a --force, a
# resumed run after a power pull) and the run that went wrong is the one you
# want to still be able to read.
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=============================================================="
echo " manet-user-scripts starting: $(date -Is)"
echo "=============================================================="

if [ ! -d "$SCRIPT_DIR" ]; then
    echo " > no $SCRIPT_DIR — nothing staged at flash time, nothing to do"
    touch "$DONE_FILE"
    exit 0
fi

if [ "$FORCE" -eq 1 ]; then
    echo " > --force: ignoring previous state, re-running everything"
    : > "$STATE_FILE"
    rm -f "$DONE_FILE"
fi

if ! command -v timeout >/dev/null 2>&1; then
    echo " !! coreutils 'timeout' not found — scripts will run unbounded"
fi

TOTAL=0; RAN=0; FAILED=0; SKIPPED=0; NOTSCRIPT=0

while IFS= read -r name; do
    [ -n "$name" ] || continue
    path="$SCRIPT_DIR/$name"
    TOTAL=$((TOTAL + 1))

    if already_ran "$name"; then
        echo " -- $name: already ran, skipping (use --force to repeat)"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # Same rule the flasher applies: no shebang, not a script. The flasher
    # never embeds such a file, so this only bites on the other path into this
    # directory — an operator copying something onto a live node. It matters
    # there, because exec'ing a file with no shebang does not fail: the kernel
    # refuses it and bash falls back to interpreting it as a shell script. A
    # dropped-in config file whose lines happen to parse as shell would run,
    # and report success.
    if ! head -c 2 "$path" 2>/dev/null | grep -q '^#!'; then
        echo " -- $name: no #! on line 1, not a script — skipping"
        NOTSCRIPT=$((NOTSCRIPT + 1))
        continue
    fi

    # Written by a heredoc in firstrun.sh, so the executable bit depends on
    # that block having chmod'd it. Repair rather than refuse: an operator who
    # copied a script onto a live node by hand should not have to know.
    [ -x "$path" ] || chmod +x "$path" 2>/dev/null || true

    echo ""
    echo "--------------------------------------------------------------"
    echo " >> $name  (timeout ${TIMEOUT}s)"
    echo "--------------------------------------------------------------"
    started=$(date +%s)

    # cwd / and stdin from /dev/null: these run from a systemd oneshot with no
    # terminal, and a script that blocks on read would otherwise sit there
    # until the timeout instead of failing immediately.
    if command -v timeout >/dev/null 2>&1; then
        ( cd / && timeout --kill-after=30 "$TIMEOUT" "$path" ) < /dev/null
        rc=$?
    else
        ( cd / && "$path" ) < /dev/null
        rc=$?
    fi

    elapsed=$(( $(date +%s) - started ))
    record_result "$name" "$rc"

    if [ "$rc" -eq 0 ]; then
        echo " << $name: OK (${elapsed}s)"
        RAN=$((RAN + 1))
    elif [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
        echo " << $name: TIMED OUT after ${TIMEOUT}s (exit $rc)"
        FAILED=$((FAILED + 1))
    else
        echo " << $name: FAILED (exit $rc, ${elapsed}s)"
        FAILED=$((FAILED + 1))
    fi
done < <(list_scripts)

# The marker goes down even when scripts failed. The policy is advisory: a
# script that exits non-zero has had its turn and is reported, it does not earn
# a retry on every subsequent boot. `--force` is the way back.
touch "$DONE_FILE"

echo ""
echo "=============================================================="
if [ "$TOTAL" -eq 0 ]; then
    echo " no operator scripts staged — nothing to do"
else
    summary=" operator scripts: $RAN ok, $FAILED failed"
    [ "$SKIPPED"   -gt 0 ] && summary="$summary, $SKIPPED already run"
    [ "$NOTSCRIPT" -gt 0 ] && summary="$summary, $NOTSCRIPT not a script"
    echo "$summary (of $TOTAL)"
    [ "$FAILED" -gt 0 ] && echo " failures are advisory — the node is still provisioned"
fi
echo " finished: $(date -Is)"
echo "=============================================================="

# Always 0. A non-zero exit would mark the unit failed, which is a louder
# signal than an operator script deserves — the banner and the log carry it.
exit 0
