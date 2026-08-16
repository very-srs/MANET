#!/usr/bin/env bash
# ==============================================================================
# Mean mesh throughput to peers
# ==============================================================================
# Prints the mean of BATMAN_V's metric across this node's originators, in
# Mbit/s, to two decimals. Service elections use it to pick the best-connected
# node, and it is published as MEAN_THROUGHPUT_MBPS.
#
# Parsing note: `batctl o` puts the metric in parentheses, and the column it
# lands in shifts, because the selected route is prefixed with `*`:
#
#      Originator        last-seen ( throughput)  Nexthop           [outgoingIF]
#    * 0c:bf:74:00:2b:f1    0.372s (       43.2)  0c:bf:74:00:2b:f1 [     wlan2]
#      0c:bf:74:00:2b:f1    0.372s (        4.2)  00:0a:52:09:60:fe [     wlan0]
#
# So $3 is "0.372s" on the starred row and "(" on the others. Reading $3 as the
# metric — as this did until 2026-08-16 — averaged the last-seen timestamp and
# produced numbers like 0.14 on a mesh running at 43 Mbit/s. Match the
# parenthesised value instead, and never a positional field.
#
# One peer reachable over two radios is still one peer: keep the best path per
# originator, then average those, so a second radio cannot drag the mean down.
# ==============================================================================

BATCTL="${BATCTL:-/usr/sbin/batctl}"

"$BATCTL" o 2>/dev/null | awk '
    # Data rows carry a MAC and a parenthesised metric; headers do not.
    match($0, /[0-9a-f]{2}(:[0-9a-f]{2}){5}/) {
        mac = substr($0, RSTART, RLENGTH)
        if (match($0, /\([[:space:]]*[0-9.]+\)/)) {
            v = substr($0, RSTART + 1, RLENGTH - 2)
            gsub(/[[:space:]]/, "", v)
            if (v + 0 > best[mac]) best[mac] = v + 0
        }
    }
    END {
        for (m in best) { sum += best[m]; n++ }
        if (n) printf "%.2f\n", sum / n; else print "0.00"
    }
'
