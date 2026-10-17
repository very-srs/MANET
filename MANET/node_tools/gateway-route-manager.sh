#!/bin/bash
# Select a reachable mesh gateway and maintain its IPv4 default route.
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-common.sh" || exit 1
set -euo pipefail

# Pending setup sleeps 1 s during the first 90 s, then 10 s between polls.
POLL_INTERVAL=10
STARTUP_POLL_INTERVAL=1
# Bound the extra polling when no gateway exists. /proc/uptime is unaffected
# by the large clock corrections common on nodes without an RTC.
read -r started _ < /proc/uptime
FAST_POLL_UNTIL=$(( ${started%.*} + 90 ))
LOCK_FILE=/var/run/gateway-route-manager.lock
# Switching gateways changes the NAT address and breaks open connections.
# Require SWITCH_RATIO times the score and SWITCH_MIN_GAIN Mbit/s more for
# SWITCH_SUSTAIN seconds, outside SWITCH_HOLD since the last switch.
# Replace a missing gateway or one failing UNREACHABLE_POLLS polls at once.
SWITCH_RATIO=1.5
SWITCH_MIN_GAIN=2
SWITCH_SUSTAIN=60
SWITCH_HOLD=300
UNREACHABLE_POLLS=2
# Prefer promoted direct (100), then mesh (500), then unqualified DHCP (1024).
MESH_METRIC=500

# Keep one route manager active for the lifetime of this process.
exec 200>"$LOCK_FILE"
flock -n 200 || exit 0

# The unit sends stderr to the journal with this program's SyslogIdentifier.
# Avoid duplicate systemd-cat logging. Manual runs send stderr to the terminal.
log() {
    printf '%s\n' "GW-ROUTE-MGR: $*" >&2
}

# Sleep using FAST_POLL_UNTIL and the poll intervals.
# A ready argument skips the faster startup polling.
poll_wait() {
    local uptime unused
    read -r uptime unused < /proc/uptime
    if [ "${1:-pending}" = pending ] && [ "${uptime%.*}" -lt "$FAST_POLL_UNTIL" ]; then
        sleep "$STARTUP_POLL_INTERVAL"
    else
        sleep "$POLL_INTERVAL"
    fi
}

# Emit "MAC SCORE" for each gateway, best first, ties by lowest MAC.
# SCORE is the smaller of path throughput and advertised download Mbit/s
# (measured by manet-uplink-speed.sh for Ethernet gateways).
# Ignore batman's "*" choice: it neither sets IPv4 routes nor applies a timer.
gateway_scores() {
    # Read the first MAC, the parenthesized rate and optional ]: down/up.
    # Keep numeric MACs and omit headers for stable parsing.
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

# Require both relative and absolute Mbit/s improvement over CURRENT,
# so the same comparison applies at HaLow and Wi-Fi rates.
noticeably_better() {
    awk -v c="$1" -v cur="$2" -v r="$SWITCH_RATIO" -v g="$SWITCH_MIN_GAIN" \
        'BEGIN { exit !(c >= cur * r && c - cur >= g) }'
}

# Require one ping reply from the supplied gateway IP within 1 s.
reachable() {
    ping -c 1 -W 1 "$1" >/dev/null 2>&1
}


log "Starting Gateway Route Manager (pending startup ${STARTUP_POLL_INTERVAL}s, steady ${POLL_INTERVAL}s)"

# Print the registry IPv4 address associated with a gateway MAC.
# Match its MAC list, then read the following address for that node.
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
    # Inspect mesh and direct defaults independently of their display order.
    defaults="$(ip route show default || true)"
    cur=$(awk '/ dev br0( |$)/ {print; exit}' <<< "$defaults")
    if [ -f /var/run/manet-uplink/mesh-gateway.state ]; then
        # Only log when actually removing a route: this branch runs every
        # poll cycle while in gateway mode.
        if [[ " $cur " == *" dev br0 "* ]]; then
            ip route del default dev br0 2>/dev/null || true
            log "Local gateway mode active; removed mesh-managed default route"
        fi
        poll_wait ready
        continue
    fi

    # The marker can lag the promoted route. DHCP alone never qualifies.
    upstream=$(cat /var/run/upstream_iface 2>/dev/null || true)
    if [ -n "$upstream" ] && [ "$upstream" != br0 ] &&
            awk -v iface="$upstream" '
                { dev=""; metric=0
                  for (i=1; i<NF; i++) {
                      if ($i == "dev") dev=$(i+1)
                      if ($i == "metric") metric=$(i+1)
                  }
                  if (dev == iface && metric == 100) found=1
                }
                END {exit !found}' <<< "$defaults"; then
        poll_wait ready
        continue
    fi

    scores="$(gateway_scores || true)"
    if [ -z "$scores" ]; then
        # Withdraw the stale default route when no gateway advertises NAT.
        # Remove only br0 routes, preserving local Ethernet uplink routes.
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
        if [[ " $cur " == *" via $gw_ip dev br0 "* &&
              " $cur " == *" src $local_ip "* &&
              " $cur " == *" metric $MESH_METRIC "* ]]; then
            route_ready=true
        elif ip route replace default via "$gw_ip" dev br0 \
                src "$local_ip" metric "$MESH_METRIC"; then
            route_ready=true
            # A metric change adds a route. Remove the former metric-0
            # mesh route only after the replacement is installed.
            if [ -n "$cur" ] && [[ " $cur " != *" metric "* ||
                                      " $cur " == *" metric 0 "* ]]; then
                ip route del default dev br0 metric 0 2>/dev/null || true
            fi
            if [ -n "$cur_mac" ] && [ "$gw_mac" != "$cur_mac" ]; then
                log "Switching gateway from $cur_mac (${cur_score:-gone} Mbit/s) to $gw_mac: ${urgent:-noticeably better, $best_score Mbit/s}"
            fi
            log "Gateway detected: $gw_mac at $gw_ip"
            log "Default route updated: via $gw_ip src $local_ip"
            # Queue once on gaining a route, not on refreshes or gateway swaps.
            if [ -z "$cur" ] && grep -qiE '^auto_update=(y|yes|1|true)[[:space:]]*$' /etc/mesh.conf 2>/dev/null; then
                systemctl start --no-block manet-auto-update.service || log "Could not queue automatic update check"
            fi
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
