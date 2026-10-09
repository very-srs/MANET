#!/bin/bash
# Run operator setup scripts once after radio-setup finishes. The flasher
# embeds provisioning/additional-scripts/ in firstrun.sh heredocs, which
# write SCRIPT_DIR on first boot. A separate unit isolates provisioning
# from operator failures and avoids rerunning hooks on config changes.
# Record each completed exit code so power loss retries only unfinished work.
# Failures are advisory in manet-provision-status.sh. Never change
# /var/lib/manet-provision.{state,failures}.
# Options: --force reruns all scripts, --list shows staged and completed work.

SCRIPT_DIR="${MANET_USER_SCRIPT_DIR:-/var/lib/manet-user-scripts}"
STATE_FILE="${MANET_USER_SCRIPT_STATE:-/var/lib/manet-user-scripts.state}"
DONE_FILE="${MANET_USER_SCRIPT_DONE:-/var/lib/manet-user-scripts.done}"
LOG_FILE="${MANET_USER_SCRIPT_LOG:-/var/log/manet-user-scripts.log}"
MESH_CONF="${MANET_MESH_CONF:-/etc/mesh.conf}"

# Per-script wall-clock limit prevents a hung script from consuming the
# unit's TimeoutStartSec and skipping later scripts. Override with
# user_script_timeout= in /etc/mesh.conf.
DEFAULT_TIMEOUT=300

usage() {
    printf '%s\n' \
        'usage: manet-user-scripts.sh [--force] [--list] [-h|--help]' >&2
}

FORCE=0
LIST_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --list)  LIST_ONLY=1 ;;
        -h|--help)
            usage 2>&1
            exit 0
            ;;
        *)
            printf '%s\n' "manet-user-scripts.sh: unknown option: $arg" >&2
            usage
            exit 1
            ;;
    esac
done

# Parse user_script_timeout without sourcing mesh.conf so stray lines
# in this operator-editable file cannot execute as shell.
TIMEOUT="$DEFAULT_TIMEOUT"
if [ -r "$MESH_CONF" ]; then
    conf_timeout=$(sed -n 's/^user_script_timeout=\([0-9]\{1,\}\)[[:space:]]*$/\1/p' \
        "$MESH_CONF" | tail -1)
    [ -n "$conf_timeout" ] && [ "$conf_timeout" -gt 0 ] 2>/dev/null && TIMEOUT="$conf_timeout"
fi

# Completed scripts: TAB-separated name, exit code, epoch. Write each result
# immediately so interrupted runs repeat only the script without a record.
mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true

already_ran() {
    [ -r "$STATE_FILE" ] || return 1
    cut -f1 "$STATE_FILE" 2>/dev/null | grep -qxF "$1"
}

record_result() {
    printf '%s\t%s\t%s\n' "$1" "$2" "$(date +%s)" >> "$STATE_FILE"
}

# Revalidate candidates, including files copied directly onto a live node.
# Use lexical C order for the 10-/20-/30- convention. The run loop checks
# shebangs separately so skipped files can be reported.
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
    [ "$found" -eq 0 ] &&
        printf '%s\n' "  no operator scripts staged in $SCRIPT_DIR"
    exit 0
fi

# Append output to the log, preserving failed runs across --force and
# resumed execution after power loss.
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=============================================================="
printf '%s\n' " manet-user-scripts starting: $(date -Is)"
echo "=============================================================="

if [ ! -d "$SCRIPT_DIR" ]; then
    printf '%s\n' \
        " > no $SCRIPT_DIR: nothing staged at flash time, nothing to do"
    touch "$DONE_FILE"
    exit 0
fi

if [ "$FORCE" -eq 1 ]; then
    echo " > --force: ignoring previous state, re-running everything"
    : > "$STATE_FILE"
    rm -f "$DONE_FILE"
fi

if ! command -v timeout >/dev/null 2>&1; then
    echo " !! coreutils 'timeout' not found: scripts will run unbounded"
fi

TOTAL=0; RAN=0; FAILED=0; SKIPPED=0; NOTSCRIPT=0

while IFS= read -r name; do
    [ -n "$name" ] || continue
    path="$SCRIPT_DIR/$name"
    TOTAL=$((TOTAL + 1))

    if already_ran "$name"; then
        printf '%s\n' " -- $name: already ran, skipping (use --force to repeat)"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # Require a shebang, including for files copied directly onto a live node.
    # Bash falls back to interpreting files the kernel rejects without one,
    # which could execute a config file that happens to parse as shell.
    if ! head -c 2 "$path" 2>/dev/null | grep -q '^#!'; then
        printf '%s\n' " -- $name: no #! on line 1, not a script: skipping"
        NOTSCRIPT=$((NOTSCRIPT + 1))
        continue
    fi

    # Repair the executable bit for heredoc-written or manually copied scripts.
    [ -x "$path" ] || chmod +x "$path" 2>/dev/null || true

    echo ""
    echo "--------------------------------------------------------------"
    printf '%s\n' " >> $name  (timeout ${TIMEOUT}s)"
    echo "--------------------------------------------------------------"
    started=$(date +%s)

    # Use cwd / and stdin /dev/null for the systemd oneshot without a terminal.
    # Reads then fail immediately instead of waiting for the timeout.
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
        printf '%s\n' " << $name: OK (${elapsed}s)"
        RAN=$((RAN + 1))
    elif [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
        printf '%s\n' " << $name: TIMED OUT after ${TIMEOUT}s (exit $rc)"
        FAILED=$((FAILED + 1))
    elif [ "$rc" -eq 126 ] || [ "$rc" -eq 127 ]; then
        # Exit 127 often means the shebang interpreter is missing. Report that
        # possibility explicitly. Any installed interpreter is allowed.
        interp=$(head -1 "$path" | sed 's|^#!||' | awk '{ if ($1 ~ /\/env$/) print $2; else print $1 }')
        if [ -n "$interp" ] && ! command -v "$interp" >/dev/null 2>&1 \
           && [ ! -x "$interp" ]; then
            printf '%s%s\n' \
                " << $name: FAILED (exit $rc): interpreter '$interp' is not" \
                " installed"
        else
            printf '%s\n' " << $name: FAILED (exit $rc, could not be executed)"
        fi
        FAILED=$((FAILED + 1))
    else
        printf '%s\n' " << $name: FAILED (exit $rc, ${elapsed}s)"
        FAILED=$((FAILED + 1))
    fi
done < <(list_scripts)

# Mark completion even after advisory failures. A nonzero script exit
# does not retry on every boot. --force permits another attempt.
touch "$DONE_FILE"

echo ""
echo "=============================================================="
if [ "$TOTAL" -eq 0 ]; then
    echo " no operator scripts staged: nothing to do"
else
    summary=" operator scripts: $RAN ok, $FAILED failed"
    [ "$SKIPPED"   -gt 0 ] && summary="$summary, $SKIPPED already run"
    [ "$NOTSCRIPT" -gt 0 ] && summary="$summary, $NOTSCRIPT not a script"
    printf '%s\n' "$summary (of $TOTAL)"
    [ "$FAILED" -gt 0 ] && echo " failures are advisory: the node is still provisioned"
fi
printf '%s\n' " finished: $(date -Is)"
echo "=============================================================="

# Exit 0: the banner and log report script failures without failing the unit.
exit 0
