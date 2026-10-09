#!/bin/bash
# Report board power and throttling status on demand and at SSH login.
# Supply sag under simultaneous HaLow and PCIe Wi-Fi load can cause failed
# associations, unresponsive USB radios or resets without shutdown logs.
# Installed as /etc/update-motd.d/55-manet-power. mesh-status.py reads --json
# for the web UI, and direct calls print the human summary.
# Always succeed so MOTD and status work without a throttling interface.

usage() {
    printf '%s\n' 'usage: manet-power-status.sh [--json]' >&2
}

if [ $# -gt 1 ] || { [ $# -eq 1 ] && [ "$1" != --json ]; }; then
    usage
    exit 1
fi

# vcgencmd low bits report live state. Sticky bits 16+ record events since
# boot, including faults that end before the operator checks status.
BIT_UNDERVOLT_NOW=0x1
BIT_CAPPED_NOW=0x2
BIT_THROTTLED_NOW=0x4
BIT_SOFTTEMP_NOW=0x8
BIT_UNDERVOLT_EVER=0x10000
BIT_CAPPED_EVER=0x20000
BIT_THROTTLED_EVER=0x40000
BIT_SOFTTEMP_EVER=0x80000

read_throttled() {
    local out
    command -v vcgencmd >/dev/null 2>&1 || return 1
    out="$(vcgencmd get_throttled 2>/dev/null)" || return 1
    case "$out" in
        throttled=0x*) printf '%s\n' "${out#throttled=}" ;;
        *) return 1 ;;
    esac
}

RAW="$(read_throttled)" || RAW=""

# Report no measurement on Rock 3A or other boards without a Broadcom mailbox.
if [ -z "$RAW" ]; then
    if [ "$1" = "--json" ]; then
        echo '{"available": false}'
    fi
    exit 0
fi

VAL=$(( RAW ))
has() { [ $(( VAL & $1 )) -ne 0 ] && echo true || echo false; }

UNDERVOLT_NOW=$(has $BIT_UNDERVOLT_NOW)
CAPPED_NOW=$(has $BIT_CAPPED_NOW)
THROTTLED_NOW=$(has $BIT_THROTTLED_NOW)
SOFTTEMP_NOW=$(has $BIT_SOFTTEMP_NOW)
UNDERVOLT_EVER=$(has $BIT_UNDERVOLT_EVER)
CAPPED_EVER=$(has $BIT_CAPPED_EVER)
THROTTLED_EVER=$(has $BIT_THROTTLED_EVER)
SOFTTEMP_EVER=$(has $BIT_SOFTTEMP_EVER)

# Prioritize under-voltage, which often causes capping and throttling.
# The soft temperature limit is a separate condition.
if   [ "$UNDERVOLT_NOW" = true ];  then STATE=critical
elif [ "$UNDERVOLT_EVER" = true ]; then STATE=warning
elif [ "$THROTTLED_NOW" = true ] || [ "$SOFTTEMP_NOW" = true ]; then STATE=warning
elif [ "$THROTTLED_EVER" = true ] || [ "$CAPPED_EVER" = true ] || [ "$SOFTTEMP_EVER" = true ]; then STATE=notice
else STATE=ok
fi

if [ "$1" = "--json" ]; then
    printf '{"available": true, "raw": "%s", "state": "%s", ' "$RAW" "$STATE"
    printf '"undervoltage_now": %s, "undervoltage_ever": %s, ' "$UNDERVOLT_NOW" "$UNDERVOLT_EVER"
    printf '"throttled_now": %s, "throttled_ever": %s, ' "$THROTTLED_NOW" "$THROTTLED_EVER"
    printf '"capped_now": %s, "capped_ever": %s, ' "$CAPPED_NOW" "$CAPPED_EVER"
    printf '"soft_temp_now": %s, "soft_temp_ever": %s}\n' "$SOFTTEMP_NOW" "$SOFTTEMP_EVER"
    exit 0
fi

rule() { printf '  %s\n' '────────────────────────────────────────────────────────────'; }

case "$STATE" in
ok)
    printf '  Power: OK (no under-voltage or throttling since boot)\n'
    ;;
notice)
    printf '  Power: throttling has occurred since boot (%s). See: vcgencmd get_throttled\n' "$RAW"
    ;;
warning|critical)
    echo
    rule
    if [ "$STATE" = critical ]; then
        printf '  ** UNDER-VOLTAGE RIGHT NOW: THIS BOARD IS NOT GETTING ENOUGH POWER **\n'
    else
        printf '  ** UNDER-VOLTAGE / THROTTLING HAS OCCURRED ON THIS BOARD **\n'
    fi
    rule
    [ "$UNDERVOLT_NOW"  = true ] && printf '  Under-voltage : NOW\n'
    [ "$UNDERVOLT_NOW"  = false ] && [ "$UNDERVOLT_EVER" = true ] && printf '  Under-voltage : has occurred since boot\n'
    [ "$THROTTLED_NOW"  = true ] && printf '  Throttled     : NOW\n'
    [ "$CAPPED_NOW"     = true ] && printf '  ARM frequency : capped now\n'
    [ "$SOFTTEMP_NOW"   = true ] && printf '  Temperature   : soft limit active now\n'
    printf '  Raw           : %s\n' "$RAW"
    printf '\n  Expect radios to misbehave in ways that look like driver faults:\n'
    printf '  a HaLow card that stops answering, a Wi-Fi interface that will\n'
    printf '  not associate, or a board that resets with nothing in the log.\n'
    printf '  Check the PSU and the cable before suspecting the software.\n'
    rule
    echo
    ;;
esac

exit 0
