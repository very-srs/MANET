#!/usr/bin/env bash
# ==============================================================================
# Provisioning status
# ==============================================================================
# Answers one question, on login and on demand: is this node finished setting
# itself up, or not?
#
# A node provisions across several reboots and can take ten minutes. Pulling
# the Ethernet cable or the power part-way through leaves it half-configured,
# and until now nothing said so — the node looked identical to a finished one.
# That is how a node ended up in the field with none of its packages installed.
#
# Installed as /etc/update-motd.d/50-manet-provision, so it prints on every SSH
# login, and runnable directly as `manet-provision-status.sh`.
#
# Never fails: a motd hook that errors breaks the login banner.
# ==============================================================================

STATE_FILE="${MANET_PROVISION_STATE:-/var/lib/manet-provision.state}"
FAIL_FILE="${MANET_PROVISION_FAILURES:-/var/lib/manet-provision.failures}"
DONE_FILE="${MANET_PROVISION_DONE:-/var/lib/radio-setup.done}"
LOG_FILE="/var/log/radio-setup.log"
VERSION_FILE="/etc/manet_version.txt"

STATE=""; PHASE=""; STARTED=""; FINISHED=""; UPDATED=""
if [ -r "$STATE_FILE" ]; then
    # Our own file, written as plain key=value by radio-setup.
    while IFS='=' read -r k v; do
        case "$k" in
            STATE) STATE="$v" ;; PHASE) PHASE="$v" ;;
            STARTED) STARTED="$v" ;; FINISHED) FINISHED="$v" ;;
            UPDATED) UPDATED="$v" ;;
        esac
    done < "$STATE_FILE"
fi

# A node provisioned before this reporting existed has no state file, but it
# does have the marker radio-setup touches on success. Treat that as the record
# of completion rather than saying nothing.
if [ -z "$STATE" ] && [ -f "$DONE_FILE" ]; then
    STATE=complete
    PHASE=radio-setup
fi

# Still nothing: provisioning has not started, or this is not a MANET node.
# Say nothing rather than guess.
[ -z "$STATE" ] && exit 0

# The completion time comes from the marker's mtime, not from whatever the
# state file claims. The filesystem records when radio-setup actually finished;
# the state file is just a copy of that, and a copy can be wrong.
if [ "$STATE" = complete ] && [ -f "$DONE_FILE" ]; then
    marker_time=$(stat -c %Y "$DONE_FILE" 2>/dev/null)
    [ -n "$marker_time" ] && FINISHED="$marker_time"
fi

human_delta() {
    local from="$1" now secs
    [ -z "$from" ] && { echo "unknown"; return; }
    now=$(date +%s)
    secs=$(( now - from ))
    [ "$secs" -lt 0 ] && secs=0
    if   [ "$secs" -lt 60 ]   ; then echo "${secs}s"
    elif [ "$secs" -lt 3600 ] ; then echo "$((secs / 60))m $((secs % 60))s"
    else echo "$((secs / 3600))h $(((secs % 3600) / 60))m"
    fi
}

rule() { printf '  %s\n' '────────────────────────────────────────────────────────────'; }

case "$STATE" in
running)
    echo
    rule
    printf '  MANET PROVISIONING IN PROGRESS — do not disconnect\n'
    rule
    printf '  Stage    : %s\n' "${PHASE:-unknown}"
    printf '  Running  : %s\n' "$(human_delta "$STARTED")"
    printf '  Keep Ethernet and power connected. The node reboots itself\n'
    printf '  several times; this can take about ten minutes.\n'
    printf '  Watch    : sudo tail -f %s\n' "$LOG_FILE"
    rule
    echo
    ;;
incomplete)
    echo
    rule
    printf '  ** MANET PROVISIONING DID NOT COMPLETE **\n'
    rule
    printf '  Stopped in : %s\n' "${PHASE:-unknown}"
    [ -n "$FINISHED" ] && printf '  When       : %s ago\n' "$(human_delta "$FINISHED")"
    if [ -s "$FAIL_FILE" ]; then
        printf '  Failures   :\n'
        # Cap it: the interesting failures are the first few, and a login
        # banner should not scroll a terminal off the screen.
        head -8 "$FAIL_FILE" | sed 's/^/    - /'
        total=$(wc -l < "$FAIL_FILE")
        [ "$total" -gt 8 ] && printf '    ... and %s more\n' "$((total - 8))"
    fi
    printf '\n  This node is NOT ready for use. Most failures are a missing\n'
    printf '  network during setup — the usual cause is the Ethernet cable\n'
    printf '  being unplugged before provisioning finished.\n\n'
    printf '  To retry: reconnect Ethernet, confirm internet, then run\n'
    printf '      sudo radio-setup.sh\n'
    printf '  or simply reboot — it retries automatically.\n'
    printf '  Details : %s\n' "$FAIL_FILE"
    printf '  Full log: %s\n' "$LOG_FILE"
    rule
    echo
    ;;
complete)
    ver=""
    [ -r "$VERSION_FILE" ] && ver=$(head -1 "$VERSION_FILE" 2>/dev/null)
    printf '  MANET node provisioned%s%s\n' \
        "${ver:+ (v$ver)}" \
        "${FINISHED:+, $(human_delta "$FINISHED") ago}"
    ;;
esac

exit 0
