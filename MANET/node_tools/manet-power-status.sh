#!/usr/bin/env bash
# ==============================================================================
# Power / throttling status
# ==============================================================================
# Answers one question, on login and on demand: is this board getting enough
# power?
#
# It usually is not, and nothing said so. The carrier boards these run on sit
# at the edge of their envelope with a HaLow card and a PCIe Wi-Fi card drawing
# at once, and a sagging supply does not announce itself - it presents as a
# radio that will not associate, a USB card that stops answering, or a board
# that resets with no shutdown in the journal. Hours have gone into chasing
# those as driver bugs. The SoC has known the answer the whole time.
#
# Installed as /etc/update-motd.d/55-manet-power so it prints on every SSH
# login, read by mesh-status.py for the web UI, and runnable directly.
#
#   manet-power-status.sh          human summary (the motd form)
#   manet-power-status.sh --json   machine readable, for the web UI
#
# Never fails: a motd hook that errors breaks the login banner, and the status
# page must render on hardware that has no throttling interface at all.
# ==============================================================================

# vcgencmd reports a bitmask. Low bits are live state, bits 16+ are sticky
# "has happened since boot" flags - which are the useful ones, because the
# event that killed a radio is over by the time anyone logs in to look.
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
        throttled=0x*) echo "${out#throttled=}" ;;
        *) return 1 ;;
    esac
}

RAW="$(read_throttled)" || RAW=""

# Rock 3A and anything else without the Broadcom mailbox: say nothing rather
# than imply the board is fine. There is no measurement to report.
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

# Under-voltage is the fault worth shouting about: capping and throttling are
# usually its consequence, and the soft temperature limit is a separate,
# far less mysterious problem.
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
    printf '  Power: throttling has occurred since boot (%s) — see: vcgencmd get_throttled\n' "$RAW"
    ;;
warning|critical)
    echo
    rule
    if [ "$STATE" = critical ]; then
        printf '  ** UNDER-VOLTAGE RIGHT NOW — THIS BOARD IS NOT GETTING ENOUGH POWER **\n'
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
