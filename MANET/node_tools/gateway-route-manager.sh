#!/usr/bin/env bash
set -euo pipefail

POLL_INTERVAL=10
STARTUP_POLL_INTERVAL=1
# Bound the extra polling when no gateway exists. /proc/uptime is unaffected
# by the large clock corrections common on nodes without an RTC.
read -r started _ < /proc/uptime
FAST_POLL_UNTIL=$(( ${started%.*} + 90 ))
LOCK_FILE=/var/run/gateway-route-manager.lock
# Switching gateways breaks every open internet connection (the new gateway
# NATs with a different address), so a node moves only for a noticeable gain:
# SWITCH_RATIO times the current score and SWITCH_MIN_GAIN Mbit/s more,
# holding for SWITCH_SUSTAIN seconds, and not within SWITCH_HOLD seconds of
# the last switch. A gateway that disappears, or stops answering for
# UNREACHABLE_POLLS polls, is replaced at once.
SWITCH_RATIO=1.5
SWITCH_MIN_GAIN=2
SWITCH_SUSTAIN=60
SWITCH_HOLD=300
UNREACHABLE_POLLS=2

exec 200>"$LOCK_FILE"
flock -n 200 || exit 0

# The unit runs this with StandardError=journal and
# SyslogIdentifier=gateway-route-manager, so stderr already lands in the
# journal under the right tag. Piping to systemd-cat as well logged every line
# twice and forked an extra process per line. Run by hand, stderr goes to the
# terminal, which is what you want there.
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] - GW-ROUTE-MGR: $*" >&2
}

poll_wait() {
    local uptime unused
    read -r uptime unused < /proc/uptime
    if [ "${1:-pending}" = pending ] && [ "${uptime%.*}" -lt "$FAST_POLL_UNTIL" ]; then
        sleep "$STARTUP_POLL_INTERVAL"
    else
        sleep "$POLL_INTERVAL"
    fi
}

# Every gateway batman knows, one "MAC SCORE" line each, best first (ties to
# the lowest MAC). SCORE is the bottleneck in Mbit/s: the lower of the path
# throughput to the gateway and the download bandwidth it announces, which an
# Ethernet gateway measures (manet-uplink-speed.sh). batman also picks a
# gateway itself ("*"), but that choice never reaches IPv4 routing and has no
# time gate, so it is ignored here.
gateway_scores() {
    batctl gwl -H -n 2>/dev/null | awk '
        {
            mac = ""
            for (i = 1; i <= NF; i++)
                if (tolower($i) ~ /^([0-9a-f]{2}:){5}[0-9a-f]{2}$/) { mac = tolower($i); break }
            if (mac == "" || !match($0, /\( *[0-9.]+\)/)) next
            tput = substr($0, RSTART + 1, RLENGTH - 2) + 0
            score = tput
            if (match($0, /\]: *[0-9.]+\//)) {
                bw = substr($0, RSTART, RLENGTH); gsub(/[^0-9.]/, "", bw)
                if (bw + 0 < score) score = bw + 0
            }
            printf "%s %.1f\n", mac, score
        }' | sort -k2,2nr -k1,1
}

# Is CANDIDATE a noticeable improvement on CURRENT? Both are scores in
# Mbit/s. Relative and absolute, so it means the same at HaLow and Wi-Fi rates.
noticeably_better() {
    awk -v c="$1" -v cur="$2" -v r="$SWITCH_RATIO" -v g="$SWITCH_MIN_GAIN" \
        'BEGIN { exit !(c >= cur * r && c - cur >= g) }'
}

reachable() {
    ping -c 1 -W 1 "$1" >/dev/null 2>&1
}

uptime_now() {
    local up unused
    read -r up unused < /proc/uptime
    echo "${up%.*}"
}

log "Starting Gateway Route Manager (pending startup ${STARTUP_POLL_INTERVAL}s, steady ${POLL_INTERVAL}s)"

lookup_gateway_ip_by_mac() {
    local gw_mac="$1"
    local reg="/var/run/mesh_node_registry"

    [ -f "$reg" ] || return 1

    awk -F"'" -v mac="${gw_mac,,}" '
        BEGIN {
            found=0
            ip=""
            prefix=""
        }
        /_MAC_ADDRESSES=/ {
            this_mac_list=tolower($2)
            if (index(this_mac_list, mac) > 0) {
                found=1
                sub(/_MAC_ADDRESSES=.*/, "", $1)
                prefix=$1
            }
        }
        found && $1 == prefix "_IPV4_ADDRESS=" {
            ip=$2
            print ip
            exit
        }
    ' "$reg"
}

cur_mac="" pending_mac="" pending_since=0 unreachable=0
last_switch=$(( $(uptime_now) - SWITCH_HOLD ))
while true; do
    cur="$(ip route show default | head -n1 || true)"
    if [ -f /var/run/mesh-gateway.state ]; then
        # Only log when actually removing a route: this branch runs every
        # poll cycle while in gateway mode, and log() forks date.
        if [[ " $cur " == *" dev br0 "* ]]; then
            ip route del default dev br0 2>/dev/null || true
            log "Local gateway mode active; removed mesh-managed default route"
        fi
        poll_wait ready
        continue
    fi

    # A dispatcher may not have written its gateway marker yet. Preserve an
    # existing local uplink route during that transition too.
    if [ -n "$cur" ] && [[ " $cur " != *" dev br0 "* ]]; then
        poll_wait ready
        continue
    fi

    scores="$(gateway_scores || true)"
    if [ -z "$scores" ]; then
        # Nobody is announcing a gateway any more. A default route installed on
        # an earlier pass now points at a node that has stopped NATing, so
        # traffic black-holes instead of visibly failing: withdraw it. Only
        # br0 routes are ours; a local uplink route lives on the ethernet iface
        # and must be left alone.
        if [[ " $cur " == *" dev br0 "* ]]; then
            ip route del default dev br0 2>/dev/null || true
            log "No mesh gateway announced; removed stale default route ($cur)"
        fi
        cur_mac=""
        poll_wait
        continue
    fi

    # The first address on br0 may be a service VIP or the EUD gateway alias.
    local_ip="$(python3 /usr/local/bin/manet_node_ipv4.py br0 2>/dev/null || true)"
    if [ -z "$local_ip" ]; then
        log "br0 has no IPv4 address yet; skipping route install"
        poll_wait
        continue
    fi

    now=$(uptime_now)
    # After a restart, the gateway the existing route points at is current.
    if [ -z "$cur_mac" ] && [[ " $cur " == *" dev br0 "* ]]; then
        while read -r mac score; do
            if [[ " $cur " == *" via $(lookup_gateway_ip_by_mac "$mac" || true) "* ]]; then
                cur_mac=$mac
                break
            fi
        done <<< "$scores"
    fi
    cur_score=$(awk -v m="$cur_mac" '$1 == m {print $2}' <<< "$scores")
    read -r best_mac best_score <<< "$scores"

    # What to use this pass, and whether that is an immediate move.
    target="" urgent=""
    if [ -z "$cur_mac" ]; then
        urgent="no gateway selected"
    elif [ -z "$cur_score" ]; then
        urgent="gateway $cur_mac no longer announced"
    else
        cur_ip=$(lookup_gateway_ip_by_mac "$cur_mac" || true)
        if [ -n "$cur_ip" ] && reachable "$cur_ip"; then
            unreachable=0
        else
            unreachable=$((unreachable + 1))
        fi
        if [ "$unreachable" -ge "$UNREACHABLE_POLLS" ]; then
            urgent="gateway $cur_mac not answering"
        else
            target=$cur_mac
            if [ "$best_mac" != "$cur_mac" ] && [ $((now - last_switch)) -ge "$SWITCH_HOLD" ] &&
                    noticeably_better "$best_score" "$cur_score"; then
                if [ "$pending_mac" != "$best_mac" ]; then
                    pending_mac=$best_mac
                    pending_since=$now
                    log "Gateway $best_mac ($best_score Mbit/s) beats $cur_mac ($cur_score Mbit/s); switching if it holds for ${SWITCH_SUSTAIN}s"
                elif [ $((now - pending_since)) -ge "$SWITCH_SUSTAIN" ]; then
                    target=$best_mac
                fi
            else
                pending_mac=""
            fi
        fi
    fi

    # Candidates in order: the target, or for an immediate move every
    # gateway best first, skipping the one that failed.
    if [ -n "$urgent" ]; then
        candidates=$(awk -v skip="${cur_score:+$cur_mac}" '$1 != skip {print $1}' <<< "$scores")
    else
        candidates=$target
    fi

    route_ready=false
    for gw_mac in $candidates; do
        gw_ip="$(lookup_gateway_ip_by_mac "$gw_mac" || true)"
        if [ -z "${gw_ip:-}" ]; then
            log "Warning: No registry entry found for MAC $gw_mac"
            continue
        fi
        if [ "$gw_mac" = "$cur_mac" ] && [ -z "$urgent" ]; then
            : # current gateway, checked above
        elif ! reachable "$gw_ip"; then
            log "Gateway IP $gw_ip not reachable; skipping route install"
            continue
        fi
        if [[ " $cur " == *" via $gw_ip dev br0 "* && " $cur " == *" src $local_ip "* ]]; then
            route_ready=true
        elif ip route replace default via "$gw_ip" dev br0 src "$local_ip"; then
            route_ready=true
            if [ -n "$cur_mac" ] && [ "$gw_mac" != "$cur_mac" ]; then
                log "Switching gateway from $cur_mac (${cur_score:-gone} Mbit/s) to $gw_mac: ${urgent:-noticeably better, $best_score Mbit/s}"
            fi
            log "Gateway detected: $gw_mac at $gw_ip"
            log "Default route updated: via $gw_ip src $local_ip"
        else
            log "Route installation failed; retrying"
            continue
        fi
        if [ "$gw_mac" != "$cur_mac" ]; then
            cur_mac=$gw_mac
            last_switch=$now
            unreachable=0
            pending_mac=""
        fi
        break
    done

    if [ "$route_ready" = true ]; then
        poll_wait ready
    else
        poll_wait
    fi
done
