#!/bin/bash
# Mesh Node Manager - Main Orchestrator
# Coordinates timing and delegates complex tasks to specialized scripts

. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/mesh-acs-common.sh" || exit 1

# --- Configuration ---
CONTROL_IFACE="br0"
ALFRED_IDENTITY_TYPE=67
ALFRED_DATA_TYPE=68
ALFRED_HELPER_TYPE=69
MONITOR_INTERVAL=15
STARTUP_MONITOR_INTERVAL=5

# Lobby channels
LOBBY_FREQ_2_4=2412
LOBBY_FREQ_5_0=5180

# Cold-start bootstrap: a fresh mesh is all-lobby, so no data-state node exists
# to send helper beacons and nothing would ever pick initial data channels.
# After this many seconds in the lobby without being rescued (several missed
# tourguide windows: an established mesh gets to rescue us first), run the
# scan/publish/election pipeline from the lobby to elect data channels.
LOBBY_BOOTSTRAP_DWELL=300

# Radio Config. radio-setup writes runtime interface role files because wlanX
# ordering varies between otherwise identical Raspberry Pi 5 nodes.
WPA_IFACE_2_4=""
WPA_IFACE_5_0=""
WPA_CONF_2_4=""
WPA_CONF_5_0=""

# Scan frequencies (must match the candidate lists in channel-election.sh;
# lobby frequencies are excluded: they are reserved as the rendezvous point)
SCAN_FREQS_2_4="2437 2462"
SCAN_FREQS_5_0="5200 5220 5240 5745 5765 5785 5805 5825"

# A channel whose survey shows less than this much dwell during the scan gets
# no occupancy figure. Busy time is counted in whole milliseconds, so on a
# 10 ms visit the ratio moves in 10% steps and means nothing.
SURVEY_MIN_DWELL_MS=25

# Helper scripts
IP_MANAGER="/usr/local/bin/mesh-ip-manager.sh"
CHANNEL_ELECTION="/usr/local/bin/channel-election.sh"
TOURGUIDE_MANAGER="/usr/local/bin/tourguide-manager.sh"
QUORUM_CHECKER="/usr/local/bin/quorum-checker.sh"
LIMP_MODE_MANAGER="/usr/local/bin/limp-mode-manager.sh"
ELECTION_OUTPUT_FILE="/var/run/mesh_channel_election"
REGISTRY_STATE_FILE="/var/run/mesh_node_registry"
ENCODER_PATH="/usr/local/bin/encoder.py"
BATCTL_PATH="/usr/sbin/batctl"
PEER_COUNTER="$(dirname "${BASH_SOURCE[0]}")/mesh-peer-count.py"
THROUGHPUT_MEAN="/usr/local/bin/mesh-throughput-mean.sh"
RADIO_STATE_SYNC="/usr/local/bin/mesh-radio-state.py"
CONFIG_SYNC="/usr/local/bin/mesh-config-sync.py"
CONFIG_ROLLBACK="/usr/local/bin/mesh-config-rollback.sh"
HALOW_MCS_SUMMARY="/usr/local/bin/halow-mcs-summary.py"

# --- State Variables ---
LAST_PUBLISHED_PAYLOAD=""
LAST_PUBLISH_TIME=0

# Identity has a slow keepalive; startup and allocation changes publish sooner.
# Alfred purges any record it has not seen for ALFRED_DATA_TIMEOUT = 600 s, so
# at 270 s a publish can fail once and the record still survives. This runs on
# its own timer rather than inside a pipeline stage, so it keeps ticking in
# both lobby and data state.
LAST_IDENTITY_PUBLISH=0
LAST_IDENTITY_ALLOCATION=""
LAST_ACK_PUBLISHED=""
IDENTITY_PUBLISH_INTERVAL=270
CACHED_SCAN_REPORT_JSON="{}"
LAST_SCAN_COMPLETE_TIME=0
LOBBY_ENTERED_TIME=0
BOOTSTRAP_START_WINDOW=-1
LOBBY_SOLO_LOGGED=false
CLOCK_READY_SEEN=false
LIMP_STATE_FILE="/var/run/mesh_limp_mode.state"

# Window tracking
declare -A LAST_ACTION_WINDOW

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - NODE-MGR: $1" >&2
}


# === GATEWAY DETECTION ===
# Gateway state is owned by manet-uplink-dispatch.sh. Node manager only
# asks it to reconcile periodically before publishing status to Alfred.
GATEWAY_STATE_FILE="/var/run/mesh-gateway.state"
LAST_GW_CHECK=0
GW_CHECK_INTERVAL=60

detect_and_update_gateway_state() {
    local NOW
    NOW=$(date +%s)

    local time_since_check=$(( NOW - LAST_GW_CHECK ))
    if [ "$time_since_check" -lt "$GW_CHECK_INTERVAL" ] && [ -f "$GATEWAY_STATE_FILE" ]; then
        return
    fi

    LAST_GW_CHECK=$NOW
    [ -x /usr/local/bin/manet-uplink-dispatch.sh ] && /usr/local/bin/manet-uplink-dispatch.sh reconcile >/dev/null 2>&1 || true
}

# Time source advertisement (the time service owns chrony and these markers)
GPS_STATUS_FILE="/run/gps_status.json"
GPS_FIX_MAX_AGE=60

is_ntp_time_source() {
    local run_dir="${MANET_TIME_RUN_DIR:-/run}"
    [ -f "$run_dir/mesh-ntp.state" ] || [ -f "$run_dir/mesh-ntp-gps.state" ]
}

# --- Clock-synchronized action checker ---
# Fires once per interval, at the first loop wakeup at-or-after the offset.
# (The old ±2 s match missed windows routinely: with a 15 s sleep a wakeup
# only landed inside a given 5 s window about a third of the time, so stages
# silently skipped intervals and peers aged out of each other's elections.)
should_perform_action() {
    local action_name=$1
    local interval_seconds=$2
    local offset_seconds=$3

    case "$action_name" in SCAN|ELECTION) acs_clock_ready || return 1 ;; esac

    local NOW=$(date +%s)
    local SECONDS_INTO_INTERVAL=$((NOW % interval_seconds))

    if [ "$SECONDS_INTO_INTERVAL" -ge "$offset_seconds" ]; then
        local CURRENT_WINDOW=$((NOW / interval_seconds))

        if [ "${LAST_ACTION_WINDOW[$action_name]}" != "$CURRENT_WINDOW" ]; then
            LAST_ACTION_WINDOW[$action_name]=$CURRENT_WINDOW
            return 0
        fi
    fi

    return 1
}

collect_radio_mcs() {
    WLAN0_TX_MCS=""; WLAN0_RX_MCS=""
    WLAN1_TX_MCS=""; WLAN1_RX_MCS=""
    WLAN2_TX_MCS=""; WLAN2_RX_MCS=""
    [ -x "$HALOW_MCS_SUMMARY" ] || return 0
    for iface in wlan0 wlan1 wlan2; do
        [ -d "/sys/class/net/$iface" ] || continue
        eval "$("$HALOW_MCS_SUMMARY" --iface "$iface" --shell 2>/dev/null || true)"
    done
}

collect_interfaces_json() {
    python3 /usr/local/bin/mesh-status.py --dump-interfaces 2>/dev/null || echo '[]'
}

collect_ap_ssid() {
    grep "^lan_ap_ssid=" /etc/mesh.conf 2>/dev/null | head -1 | cut -d'=' -f2-
}

restart_mesh_supplicants() {
    [ -n "$WPA_IFACE_2_4" ] && radio_iface_enabled "$WPA_IFACE_2_4" && systemctl restart "wpa_supplicant@${WPA_IFACE_2_4}.service"
    [ -n "$WPA_IFACE_5_0" ] && radio_iface_enabled "$WPA_IFACE_5_0" && systemctl restart "wpa_supplicant@${WPA_IFACE_5_0}.service"
}

# Leaving the lobby for data channels: clear any legacy bitrate masks (set by
# tourguide lobby hops or limp-mode entry: they persist on the netdev across
# supplicant restarts) and drop the limp-mode state file. limp-mode-manager
# only runs in data state, so without this a lobby fallback never resets them.
leave_lobby_cleanup() {
    [ -n "$WPA_IFACE_2_4" ] && iw dev "$WPA_IFACE_2_4" set bitrates 2>/dev/null
    [ -n "$WPA_IFACE_5_0" ] && iw dev "$WPA_IFACE_5_0" set bitrates 2>/dev/null
    rm -f "$LIMP_STATE_FILE"
}

adopt_helper_channels() {
    local target24="$1" target5="$2" band iface target actual
    local -a changed=()
    for band in 2_4 5_0; do
        local iface_var="WPA_IFACE_$band"
        iface=${!iface_var}
        target="$target24"; [ "$band" != 5_0 ] || target="$target5"
        [ -n "$target" ] && radio_iface_enabled "$iface" || continue
        actual=$(timeout 2 iw dev "$iface" info 2>/dev/null | grep -oP 'channel.*\((\K[0-9]+)' || true)
        [ "$actual" = "$target" ] || changed+=("$iface")
    done
    acs_write_channels "$target24" "$target5" || return 1
    # A rotating lobby can be the live data channel. Accept the authenticated
    # helper and finish discovery there without disturbing an established link.
    for iface in "${changed[@]}"; do
        systemctl restart "wpa_supplicant@${iface}.service" || return 1
    done
    leave_lobby_cleanup
    [ "${#changed[@]}" -eq 0 ] || sleep 5
    return 0
}

is_in_lobby() {
    load_mesh_roles

    if ! acs_configs_ready; then
        log "Mesh WPA configs not ready: $WPA_CONF_2_4 / $WPA_CONF_5_0"
        echo "true"
        return
    fi

    # Search channels overlap real data channels. Frequency is only a startup
    # hint; authenticated adoption and explicit return-to-lobby set the mode.
    acs_discovery_state || echo "true"
}

# How many other nodes are meshed with us right now, by batman-adv's data-plane
# view. Counts a peer found over ANY batman interface, HaLow
# included -- what the lobby bootstrap needs is somebody to run a joint
# deterministic election with, and it does not matter which radio found them.
mesh_peer_count() {
    python3 "$PEER_COUNTER" --batctl "$BATCTL_PATH"
}

update_lobby_bootstrap() {
    local peers
    if ! acs_clock_ready; then
        BOOTSTRAPPING=false
        BOOTSTRAP_START_WINDOW=-1
        return
    fi
    if ! peers=$(mesh_peer_count); then
        # Do not elect from an unknown topology. A later successful query
        # must start a fresh scan/publish round before elections resume.
        BOOTSTRAPPING=false
        BOOTSTRAP_START_WINDOW=-1
        LOBBY_SOLO_LOGGED=false
        log "Cannot read BATMAN peers. Deferring lobby bootstrap."
    elif [ "$peers" -gt 0 ]; then
        BOOTSTRAPPING=true
        [ "$BOOTSTRAP_START_WINDOW" -lt 0 ] && BOOTSTRAP_START_WINDOW=$((NOW / 180))
        LOBBY_SOLO_LOGGED=false
    else
        BOOTSTRAPPING=false
        BOOTSTRAP_START_WINDOW=-1
        if [ "$LOBBY_SOLO_LOGGED" = false ]; then
            log "Solo in discovery (no batman peers). Searching for a tourguide or another radio; not electing."
            LOBBY_SOLO_LOGGED=true
        fi
    fi
}

check_mesh_quorum() {
    local status
    "$QUORUM_CHECKER"
    status=$?
    case "$status" in
        0) return 0 ;;
        1) return 1 ;;  # Confirmed quorum loss: return to the lobby.
        *) log "Quorum check unavailable (exit $status). Holding current channels."; return 0 ;;
    esac
}

return_to_lobby() {
    log "Returning to lobby channels..."
    acs_write_channels "$LOBBY_FREQ_2_4" "$LOBBY_FREQ_5_0" search || return 1
    restart_mesh_supplicants
    sleep 5
}

# Return the phy backing an interface (same derivation as radio-setup.sh's
# iface_phy).
iface_phy() {
    iw dev "$1" info 2>/dev/null | awk '/wiphy/ {print "phy"$2; exit}'
}

# Filter a candidate frequency list down to what this interface's phy actually
# offers. This is not a regulatory mechanism and on a fully provisioned node it
# is a no-op. It exists for two failure cases:
#   1. "iw scan freq" rejects the WHOLE request if any single frequency is not
#      permitted on the phy, so one bad entry silently kills the scan for every
#      channel on that radio -- and perform_scan discards the exit status, so the
#      only symptom is an empty survey.
#   2. A node that boots before /root/regulatory.db has been copied into place
#      runs the stock database, where the UNII-3 candidates (5745-5825) can be
#      absent, gated behind (no IR), or DFS.
# Fails OPEN: if the phy cannot be read, or nothing parses, hand back the input
# list untouched. Filtering to an empty set is far worse than not filtering --
# it would take a whole band off the air on every node at once.
phy_usable_freqs() {
    local iface="$1"
    local wanted="$2"
    local phyname info usable="" freq

    phyname="$(iface_phy "$iface")"
    [ -z "$phyname" ] && { echo "$wanted"; return; }

    info="$(iw phy "$phyname" info 2>/dev/null)"
    [ -z "$info" ] && { echo "$wanted"; return; }

    for freq in $wanted; do
        # iw prints "* 5220.0 MHz [44] (30.0 dBm)"; an unusable channel carries a
        # trailing (disabled) / (no IR) / (radar detection) tag. All three mean
        # we cannot bring a mesh point up there, so contribute no data for it.
        if echo "$info" | grep -qE "[[:space:]]${freq}\.0 MHz.*\((disabled|no IR|radar detection|passive scan)"; then
            continue
        fi
        echo "$info" | grep -q "[[:space:]]${freq}\.0 MHz" && usable+="$freq "
    done

    if [ -z "$usable" ]; then
        log "WARNING: no candidate frequency usable on $iface ($phyname). Scanning unfiltered."
        echo "$wanted"
    else
        echo "${usable% }"
    fi
}

# Parse "iw dev X survey dump" into one line per frequency:
#   <freq> <noise|-> <active_ms|-> <busy_ms|-> <tx_ms|->
# These are the driver's counters since the interface came up, so they only
# mean anything as the difference between two snapshots.
parse_survey() {
    awk '
        $1 == "frequency:" { f = $2; seen[f] = 1; next }
        f == "" { next }
        $1 == "noise:" { noise[f] = $2; next }
        /channel active time:/ { act[f] = $4; next }
        /channel busy time:/ { busy[f] = $4; next }
        /channel transmit time:/ { tx[f] = $4; next }
        END {
            for (k in seen)
                printf "%s %s %s %s %s\n", k, (k in noise ? noise[k] : "-"), (k in act ? act[k] : "-"), (k in busy ? busy[k] : "-"), (k in tx ? tx[k] : "-")
        }'
}

perform_scan() {
    local json_out='{"results": ['
    local first_entry=true
    # Survey counters bracketing the scan. Keyed by frequency; frequencies do
    # not repeat between the two bands, but they are cleared per interface
    # anyway so a stale entry can never leak into the next radio's deltas.
    local -A pre_act pre_busy pre_tx post_act post_busy post_tx post_noise
    local f n a b t

    load_mesh_roles

    for iface in "$WPA_IFACE_2_4" "$WPA_IFACE_5_0"; do
        radio_iface_enabled "$iface" || continue
        local freqs_to_scan=""
        [ "$iface" == "$WPA_IFACE_2_4" ] && freqs_to_scan=$SCAN_FREQS_2_4
        [ "$iface" == "$WPA_IFACE_5_0" ] && freqs_to_scan=$SCAN_FREQS_5_0
        freqs_to_scan="$(phy_usable_freqs "$iface" "$freqs_to_scan")"

        pre_act=(); pre_busy=(); pre_tx=()
        post_act=(); post_busy=(); post_tx=(); post_noise=()

        # Snapshot before and after so every candidate is measured over the
        # same event -- this scan -- rather than over its lifetime. Without
        # the bracket the home channel reports a running average since boot
        # while the other candidates report only the milliseconds the radio
        # spent visiting them, and the two are not comparable.
        while read -r f n a b t; do
            [ -z "$f" ] && continue
            pre_act[$f]=$a; pre_busy[$f]=$b; pre_tx[$f]=$t
        done < <(iw dev "$iface" survey dump 2>/dev/null | parse_survey)

        (iw dev "$iface" scan freq $freqs_to_scan > /dev/null 2>&1) &
        SCAN_PID=$!

        for i in {1..10}; do
            kill -0 $SCAN_PID 2>/dev/null || break
            sleep 0.5
        done
        kill $SCAN_PID 2>/dev/null || true

        while read -r f n a b t; do
            [ -z "$f" ] && continue
            post_noise[$f]=$n; post_act[$f]=$a; post_busy[$f]=$b; post_tx[$f]=$t
        done < <(iw dev "$iface" survey dump 2>/dev/null | parse_survey)

        local scan_data=$(iw dev "$iface" scan dump 2>/dev/null)

        for freq in $freqs_to_scan; do
            local noise="${post_noise[$freq]:--}"
            # No survey entry means the radio never actually visited this
            # frequency (scan request rejected, channel unavailable, driver
            # hiccup). Report nothing for it rather than a synthetic floor:
            # -100 dBm beats every real measurement, so a defaulted value wins
            # channel-election.sh outright and parks the mesh on a channel
            # nobody can hear. Missing data is handled correctly downstream --
            # find_best_channel holds the current channel when a band has no
            # measurements at all.
            [ "$noise" = "-" ] && continue
            local bss_count=$(echo "$scan_data" | grep -c "freq: ${freq}\." )

            # Occupancy: the share of the visit the channel was busy with
            # something that was not our own transmission. Unlike the noise
            # floor this is a ratio of two counters from the same driver, so
            # it is comparable between nodes and between chip revisions
            # without any calibration. Our own receive time is still counted
            # against the home channel; the election's hysteresis covers the
            # few percent of duty cycle a healthy mesh adds.
            local occupancy_field=""
            local ca="${post_act[$freq]:--}" cb="${post_busy[$freq]:--}"
            local ct="${post_tx[$freq]:-0}"
            if [ "$ca" != "-" ] && [ "$cb" != "-" ]; then
                [ "$ct" = "-" ] && ct=0
                local pa="${pre_act[$freq]:-0}" pb="${pre_busy[$freq]:-0}"
                local pt="${pre_tx[$freq]:-0}"
                [ "$pa" = "-" ] && pa=0
                [ "$pb" = "-" ] && pb=0
                [ "$pt" = "-" ] && pt=0
                local d_act=$((ca - pa)) d_busy=$((cb - pb)) d_tx=$((ct - pt))
                # Negative means the counters were reset under us (supplicant
                # restart, netdev bounce) and the pre-snapshot belongs to a
                # dead epoch. Fall back to the post values, which are then
                # already the counts since that reset.
                if [ "$d_act" -lt 0 ] || [ "$d_busy" -lt 0 ] || [ "$d_tx" -lt 0 ]; then
                    d_act=$ca; d_busy=$cb; d_tx=$ct
                fi
                if [ "$d_act" -ge "$SURVEY_MIN_DWELL_MS" ]; then
                    local occupancy=$(( (d_busy - d_tx) * 100 / d_act ))
                    [ "$occupancy" -lt 0 ] && occupancy=0
                    [ "$occupancy" -gt 100 ] && occupancy=100
                    occupancy_field=", \"busy_pct\": ${occupancy}"
                fi
            fi

            [ "$first_entry" = true ] && first_entry=false || json_out+=","
            json_out+="{\"channel\": ${freq}, \"noise_floor\": ${noise}, \"bss_count\": ${bss_count}${occupancy_field}}"
        done
    done

    json_out+=']}'
    echo "$json_out"
}

is_hosting_service() {
	if systemctl is-active --quiet mediamtx.service; then
        local IPV4_NETWORK=""
	# Source the network configuration
	if [ -f /etc/mesh.conf ]; then
	    # Parse the config file
	    while IFS='=' read -r key value; do
	        [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
	        case "$key" in
	            ipv4_network)
	                IPV4_NETWORK="$value"
	                ;;
	        esac
	    done < /etc/mesh.conf
	fi
        [ -z "$IPV4_NETWORK" ] && return 1

        local CALC_OUTPUT=$(manet-ipcalc.sh "$IPV4_NETWORK" 2>/dev/null)
        local FIRST_IP=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
        local MEDIAMTX_IPV4_VIP="${FIRST_IP%.*}.$((${FIRST_IP##*.} + 1))"
        ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MEDIAMTX_IPV4_VIP/" && return 0
    fi
    return 1
}

is_hosting_mumble_service() {
    if systemctl is-active --quiet mumble-server.service; then
        if [ -f /etc/mesh.conf ]; then
            while IFS='=' read -r key value; do
                [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
                case "$key" in
                    ipv4_network)
                        IPV4_NETWORK="$value"
                        ;;
                esac
            done < /etc/mesh.conf
        fi

        local CALC_OUTPUT
        CALC_OUTPUT=$(manet-ipcalc.sh "$IPV4_NETWORK" 2>/dev/null)
        local FIRST_IP
        FIRST_IP=$(echo "$CALC_OUTPUT" | awk '/HostMin/ {print $2}')
        local MUMBLE_IPV4_VIP="${FIRST_IP%.*}.$((${FIRST_IP##*.} + 2))"
        ip addr show dev "$CONTROL_IFACE" | grep -q "inet $MUMBLE_IPV4_VIP/" && return 0
    fi
    return 1
}

should_perform_tourguide() {
    acs_clock_ready || return 1
    local NOW=$(date +%s)
    local MINUTE_OF_HOUR=$(( (NOW % 3600) / 60 ))

    [ $((MINUTE_OF_HOUR % 2)) -ne 0 ] && return 1

    local SECOND_OF_MINUTE=$((NOW % 60))

    if [ $SECOND_OF_MINUTE -ge 30 ] && [ $SECOND_OF_MINUTE -lt 50 ]; then
        local CURRENT_WINDOW=$((NOW / 120))
        if [ "${LAST_ACTION_WINDOW[TOURGUIDE]}" != "$CURRENT_WINDOW" ]; then
            LAST_ACTION_WINDOW[TOURGUIDE]=$CURRENT_WINDOW
            return 0
        fi
    fi

    return 1
}

# === MAIN SETUP ===
log "Starting Mesh Node Manager."
MY_MAC=$(cat "/sys/class/net/${CONTROL_IFACE}/address")
log "Node MAC: ${MY_MAC}"
load_mesh_roles
log "Mesh roles: 2.4G=${WPA_IFACE_2_4:-unset}, 5G=${WPA_IFACE_5_0:-unset}"

# === MAIN LOOP ===
while true; do
    NOW=$(date +%s)

    if [ "$CLOCK_READY_SEEN" = false ] && acs_clock_ready; then
        CLOCK_READY_SEEN=true
        LAST_ACTION_WINDOW=()
        LAST_PUBLISH_TIME=0; LAST_IDENTITY_PUBLISH=0; LAST_GW_CHECK=0
        LAST_SCAN_COMPLETE_TIME=0; CACHED_SCAN_REPORT_JSON="{}"
        LOBBY_ENTERED_TIME=0; BOOTSTRAP_START_WINDOW=-1
        log "Initial time sync complete; restarting discovery and ACS scheduling."
    fi

    # Tourguide temporarily rewrites a config to the lobby. Do not interpret
    # that as our operating state, scan it, or run quorum/elections over the
    # hop's temporary loss of data peers. The child holds the channel lock.
    ACS_LOCK_FILE="${MANET_ACS_LOCK_FILE:-/var/run/channel-election.lock}"
    if [ -e "$ACS_LOCK_FILE" ] && ! flock -n "$ACS_LOCK_FILE" true; then
        sleep "$STARTUP_MONITOR_INTERVAL"
        continue
    fi

    # === ALFRED RADIO STATE SYNC ===
    # Global radio up/down changes are staged through Alfred and only applied
    # after all nodes have ACKed the same version.
    [ -x "$RADIO_STATE_SYNC" ] && "$RADIO_STATE_SYNC" sync || true

    # === ALFRED CONFIG SYNC ===
    # Same shape for mesh configuration: stage what the operator broadcast,
    # publish an ACK, and apply once activate_at passes.
    [ -x "$CONFIG_SYNC" ] && "$CONFIG_SYNC" sync || true

    # A dangerous config change can take the mesh down. This is the node
    # deciding, on its own, whether the change worked or has to be undone.
    [ -x "$CONFIG_ROLLBACK" ] && "$CONFIG_ROLLBACK" check || true

    # An ACK is only useful if peers see it promptly, so a change here brings
    # the next telemetry publish forward instead of waiting out the interval.
    CURRENT_ACK=$(cat /var/run/mesh_config_ack_version 2>/dev/null || echo "")
    if [ "$CURRENT_ACK" != "$LAST_ACK_PUBLISHED" ]; then
        LAST_PUBLISH_TIME=0
        LAST_ACK_PUBLISHED="$CURRENT_ACK"
    fi

    # === PUBLISH IDENTITY (Alfred type 67) ===
    # Advertise while discovering, and on the first pass after a claim/release.
    # The 270 s interval is only a keepalive for an unchanged allocation.
    MY_CHUNK=$(cat /var/run/my_ipv4_chunk 2>/dev/null || true)
    IDENT_IPV4=$(ip addr show dev "$CONTROL_IFACE" 2>/dev/null | grep -oP 'inet \K[\d.]+' | head -1)
    [ -z "$MY_CHUNK" ] && IDENT_IPV4=""
    IDENTITY_ALLOCATION="${MY_CHUNK}:${IDENT_IPV4}"
    if [ -z "$MY_CHUNK" ] || [ "$IDENTITY_ALLOCATION" != "$LAST_IDENTITY_ALLOCATION" ] ||
            [ $((NOW - LAST_IDENTITY_PUBLISH)) -ge $IDENTITY_PUBLISH_INTERVAL ]; then
        # br0's MAC must come first: encoder.py drops it, because Alfred
        # already stamps every record we publish with it.
        IDENT_MACS=("$MY_MAC")
        for iface in /sys/class/net/wlan* /sys/class/net/bat0 /sys/class/net/end0; do
            iface=$(basename "$iface")
            if [ -d "/sys/class/net/$iface" ]; then
                MAC=$(cat "/sys/class/net/$iface/address" 2>/dev/null)
                [ -n "$MAC" ] && IDENT_MACS+=("$MAC")
            fi
        done

        IDENTITY_ARGS=(
            "--hostname" "$(hostname)"
            "--mac-addresses" "${IDENT_MACS[@]}"
            "--syncthing-id" "$(runuser -u radio -- syncthing --device-id 2>/dev/null || echo "")"
            "--ipv4-chunk" "${MY_CHUNK:-0}"
        )
        [ -n "$MY_CHUNK" ] && [ -n "$IDENT_IPV4" ] && IDENTITY_ARGS+=("--ipv4-address" "$IDENT_IPV4")

        IDENTITY_PAYLOAD=$("$ENCODER_PATH" identity "${IDENTITY_ARGS[@]}" 2>/dev/null)
        if [ -n "$IDENTITY_PAYLOAD" ]; then
            if echo -n "$IDENTITY_PAYLOAD" | alfred -s $ALFRED_IDENTITY_TYPE; then
                LAST_IDENTITY_PUBLISH=$NOW
                LAST_IDENTITY_ALLOCATION="$IDENTITY_ALLOCATION"
            fi
        else
            log "WARN: identity encoder produced no payload"
        fi
    fi

    # === CHECK STATE: LOBBY OR DATA ===
    IS_IN_LOBBY=$(is_in_lobby)

    if [ "$IS_IN_LOBBY" = "true" ]; then
        # === LOBBY STATE ===

        # === COLD-START BOOTSTRAP DWELL TRACKING ===
        # Dwell only counts once the WPA configs exist (is_in_lobby also
        # returns true while radio-setup hasn't produced them yet).
        BOOTSTRAPPING=false
        if acs_configs_ready; then
            [ "$LOBBY_ENTERED_TIME" -eq 0 ] && LOBBY_ENTERED_TIME=$NOW
            if [ $((NOW - LOBBY_ENTERED_TIME)) -ge "$LOBBY_BOOTSTRAP_DWELL" ]; then
                # A solo searcher never elects itself onto data channels.
                # The agreement daemon follows the synchronized rotation, or
                # parks at fixed anchors before clock sync. Any BATMAN contact
                # holds discovery still for recovery or joint bootstrap.
                update_lobby_bootstrap
            fi
        fi

        # === REGISTRY REFRESH AND IP MANAGEMENT (always needed) ===
        [ -x "$IP_MANAGER" ] && "$IP_MANAGER"

        # === BOOTSTRAP STAGE 1: RF SCAN (every 3 min at :10) ===
        if [ "$BOOTSTRAPPING" = true ] && ! acs_agreement_busy && should_perform_action "SCAN" 180 10; then
            log "=== LOBBY BOOTSTRAP SCAN ($(date +'%H:%M:%S')) ==="
            CACHED_SCAN_REPORT_JSON=$(perform_scan)
            LAST_SCAN_COMPLETE_TIME=$NOW
        fi

        # === PUBLISH STATUS (so other nodes can see us) ===
        # Bootstrapping nodes publish on the same clock-synchronized :15
        # window as data state so every lobby node's scan report lands in
        # peers' registries before the shared :25 election; otherwise the
        # free-running 3-minute timer is fine.
        DO_LOBBY_PUBLISH=false
        if [ "$BOOTSTRAPPING" = true ]; then
            should_perform_action "PUBLISH" 180 15 && DO_LOBBY_PUBLISH=true
        else
            time_since_publish=$((NOW - LAST_PUBLISH_TIME))
            [ $time_since_publish -ge 180 ] && DO_LOBBY_PUBLISH=true
        fi
        # Cold nodes must advertise before allocation; do not wait for an ACS
        # scan/publish window to make their presence visible to other joiners.
        [ ! -s /var/run/my_ipv4_chunk ] && DO_LOBBY_PUBLISH=true
        if [ "$DO_LOBBY_PUBLISH" = true ]; then
            log "=== LOBBY PUBLISH ($(date +'%H:%M:%S')) ==="
            
        # Not a positional field: `batctl o` shifts columns on the starred
        # (selected) route, so $3 is the last-seen timestamp there. See
        # mesh-throughput-mean.sh.
        MEAN_THROUGHPUT=$("$THROUGHPUT_MEAN" 2>/dev/null || echo 0)
            
            # Service flags
            detect_and_update_gateway_state
            IS_GATEWAY_FLAG=$([ -f /var/run/mesh-gateway.state ] && echo "--is-internet-gateway" || echo "")
            GATEWAY_IFACE=$(cat /var/run/upstream_iface 2>/dev/null || echo "")
            IS_NTP_FLAG=$(is_ntp_time_source && echo "--is-ntp-server" || echo "")
            IS_MEDIAMTX_FLAG=$(is_hosting_service && echo "--is-mediamtx-server" || echo "")
            IS_MUMBLE_FLAG=$(is_hosting_mumble_service && echo "--is-mumble-server" || echo "")

            
            collect_radio_mcs
            INTERFACES_JSON=$(collect_interfaces_json)
            
            # Encode (scan data only while bootstrapping)
            ENCODER_ARGS=(
                "--mean-throughput-mbps" "$MEAN_THROUGHPUT"
                "--timestamp" "$NOW"
                "--config-ack-version" "$(cat /var/run/mesh_config_ack_version 2>/dev/null || echo "")"
                "--wifi-24-tx-mcs" "${WLAN0_TX_MCS:-}"
                "--wifi-24-rx-mcs" "${WLAN0_RX_MCS:-}"
                "--wifi-5-tx-mcs" "${WLAN1_TX_MCS:-}"
                "--wifi-5-rx-mcs" "${WLAN1_RX_MCS:-}"
                "--halow-tx-mcs" "${WLAN2_TX_MCS:-}"
                "--halow-rx-mcs" "${WLAN2_RX_MCS:-}"
                "--halow-mcs-peer" "${WLAN2_MCS_PEER:-}"
                "--interfaces-json" "$INTERFACES_JSON"
            )
            AP_SSID=$(collect_ap_ssid)
            [ -n "$AP_SSID" ] && ENCODER_ARGS+=("--ap-ssid" "$AP_SSID")
            [ -n "$IS_GATEWAY_FLAG" ] && ENCODER_ARGS+=("$IS_GATEWAY_FLAG")
            [ -n "$GATEWAY_IFACE" ] && ENCODER_ARGS+=("--gateway-iface" "$GATEWAY_IFACE")
            [ -n "$IS_NTP_FLAG" ] && ENCODER_ARGS+=("$IS_NTP_FLAG")
            [ -n "$IS_MEDIAMTX_FLAG" ] && ENCODER_ARGS+=("$IS_MEDIAMTX_FLAG")
            [ -n "$IS_MUMBLE_FLAG" ] && ENCODER_ARGS+=("$IS_MUMBLE_FLAG")
            # Bootstrap election needs every lobby node's scan report replicated
            [ "$BOOTSTRAPPING" = true ] && [ "$CACHED_SCAN_REPORT_JSON" != "{}" ] && \
                ENCODER_ARGS+=("--channel-report-json" "$CACHED_SCAN_REPORT_JSON")
            BATT_PCT=$(python3 -c "import json;d=json.load(open('/run/battery_status.json'));p=d.get('percentage');print('' if p is None else p)" 2>/dev/null)
            [ -n "$BATT_PCT" ] && ENCODER_ARGS+=("--battery-percentage" "$BATT_PCT")
            UPTIME_SECS=$(awk '{print int($1)}' /proc/uptime 2>/dev/null)
            [ -n "$UPTIME_SECS" ] && ENCODER_ARGS+=("--uptime-seconds" "$UPTIME_SECS")
            CPU_LOAD=$(awk '{print $1}' /proc/loadavg 2>/dev/null)
            [ -n "$CPU_LOAD" ] && ENCODER_ARGS+=("--cpu-load-average" "$CPU_LOAD")

            # --- GPS Location ---
            # Reject stale files as well as missing fixes. If gps-reader stops updating
            # its timestamp, the last recorded position is no longer safe to publish.
            GPS_LAT=""; GPS_LON=""; GPS_ALT=""
            if [ -f "$GPS_STATUS_FILE" ]; then
                eval "$(python3 -c "
import json, sys, time
try:
    d = json.load(open(sys.argv[1]))
    if d.get('has_fix') and time.time() - d.get('timestamp', 0) <= float(sys.argv[2]):
        print('GPS_LAT=' + str(d['latitude']))
        print('GPS_LON=' + str(d['longitude']))
        print('GPS_ALT=' + str(d['altitude']))
except Exception:
    pass
" "$GPS_STATUS_FILE" "$GPS_FIX_MAX_AGE" 2>/dev/null)"
            fi
            [ -n "$GPS_LAT" ] && ENCODER_ARGS+=("--latitude" "$GPS_LAT" "--longitude" "$GPS_LON" "--altitude" "$GPS_ALT")

            CURRENT_PAYLOAD=$("$ENCODER_PATH" telemetry "${ENCODER_ARGS[@]}" 2>/dev/null)
            if [ -z "$CURRENT_PAYLOAD" ]; then
                # re-run only on failure, to surface why nothing was published
                log "WARN: encoder produced no payload, not publishing: $("$ENCODER_PATH" telemetry "${ENCODER_ARGS[@]}" 2>&1 >/dev/null | tr '\n' ' ')"
            fi

            if [ -n "$CURRENT_PAYLOAD" ]; then
                echo -n "$CURRENT_PAYLOAD" | alfred -s $ALFRED_DATA_TYPE
                LAST_PUBLISHED_PAYLOAD="$CURRENT_PAYLOAD"
                LAST_PUBLISH_TIME=$NOW
            fi
        fi

        # === RUN SERVICE ELECTIONS (needed for services to start) ===
        for election_script in /usr/local/bin/*-election.sh; do
            if [[ -f "$election_script" && -x "$election_script" ]]; then
                # Skip channel-election here; the bootstrap stage below runs it
                # on the clock-synchronized :25 window once dwell expires
                [[ "$election_script" =~ channel-election ]] && continue
                # Skip mediamtx-election.sh if MTX not enabled
                if [[ "$election_script" =~ mediamtx-election ]]; then
                    MTX_ENABLED=$(grep "^mtx=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
                    [[ "$MTX_ENABLED" != "y" ]] && continue
                fi
                if [[ "$election_script" =~ mumble-election ]]; then
                    MUMBLE_ENABLED=$(grep "^mumble=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
                    [[ "$MUMBLE_ENABLED" != "y" ]] && continue
                fi
                "$election_script" &
            fi
        done
        
        # === CHECK FOR HELPER BEACON (non-blocking) ===
        HELPER_MIGRATED=false
        if ! acs_agreement_busy; then
            HELPER_CHANNELS=$(python3 "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/mesh-channel-agreement.py" helper-select 2>/dev/null) || HELPER_CHANNELS=""
            if [ -n "$HELPER_CHANNELS" ]; then
                IFS='|' read -r DATA_CHANNEL_2_4 DATA_CHANNEL_5_0 <<< "$HELPER_CHANNELS"
                if adopt_helper_channels "$DATA_CHANNEL_2_4" "$DATA_CHANNEL_5_0"; then
                    log "Fresh authenticated helper. Migrating to 2.4=${DATA_CHANNEL_2_4}, 5=${DATA_CHANNEL_5_0}"
                    HELPER_MIGRATED=true
                fi
            fi
        fi

        # === BOOTSTRAP STAGE 2: CHANNEL ELECTION (every 3 min at :25) ===
        # Request this round after a full scan/publish window. The agreement
        # service proposes one plan, gathers a majority and activates it later.
        # A helper rescue this cycle wins over self-election: joining the
        # established mesh's channels beats electing our own. The election
        # also sits out the (partial) window in which bootstrap began so one
        # full scan->publish->replicate round completes first: otherwise a
        # dwell expiring mid-window fires all three stages back-to-back and
        # elects before any peer report can be in the registry.
        if [ "$BOOTSTRAPPING" = true ] && [ "$HELPER_MIGRATED" = false ] && \
           [ $((NOW / 180)) -gt "$BOOTSTRAP_START_WINDOW" ] && \
           should_perform_action "ELECTION" 180 25; then
            log "=== LOBBY BOOTSTRAP ELECTION ($(date +'%H:%M:%S')) ==="
            [ -x "$CHANNEL_ELECTION" ] && "$CHANNEL_ELECTION"
            if [ "$(is_in_lobby)" = "false" ]; then
                log "Bootstrap election picked data channels; leaving lobby."
                leave_lobby_cleanup
            fi
        fi

    else
        # === DATA CHANNEL STATE ===

        LOBBY_ENTERED_TIME=0
        BOOTSTRAP_START_WINDOW=-1
        LOBBY_SOLO_LOGGED=false

        # === STAGE 1: RF SCAN (every 3 min at :10) ===
        if ! acs_agreement_busy && should_perform_action "SCAN" 180 10; then
            log "=== SCAN ($(date +'%H:%M:%S')) ==="
            SCAN_REPORT_JSON=$(perform_scan)
            LAST_SCAN_COMPLETE_TIME=$NOW
            SCAN_DATA_AVAILABLE=true
            CACHED_SCAN_REPORT_JSON="$SCAN_REPORT_JSON"
        else
            SCAN_DATA_AVAILABLE=false
            SCAN_REPORT_JSON="$CACHED_SCAN_REPORT_JSON"
        fi

        # === STAGE 2: PUBLISH (every 3 min at :15) ===
        if [ ! -s /var/run/my_ipv4_chunk ] || should_perform_action "PUBLISH" 180 15; then
            log "=== PUBLISH ($(date +'%H:%M:%S')) ==="

        # Not a positional field: `batctl o` shifts columns on the starred
        # (selected) route, so $3 is the last-seen timestamp there. See
        # mesh-throughput-mean.sh.
        MEAN_THROUGHPUT=$("$THROUGHPUT_MEAN" 2>/dev/null || echo 0)

            # Service flags
            detect_and_update_gateway_state
            IS_GATEWAY_FLAG=$([ -f /var/run/mesh-gateway.state ] && echo "--is-internet-gateway" || echo "")
            GATEWAY_IFACE=$(cat /var/run/upstream_iface 2>/dev/null || echo "")
            IS_NTP_FLAG=$(is_ntp_time_source && echo "--is-ntp-server" || echo "")
            IS_MEDIAMTX_FLAG=$(is_hosting_service && echo "--is-mediamtx-server" || echo "")
            IS_MUMBLE_FLAG=$(is_hosting_mumble_service && echo "--is-mumble-server" || echo "")



            # Limp mode flag
            LIMP_MODE_FLAG=""
            if [ -f "$ELECTION_OUTPUT_FILE" ]; then
                LIMP_MODE_DECISION=$(grep "LIMP_MODE" "$ELECTION_OUTPUT_FILE" 2>/dev/null | cut -d'=' -f2)
                [ "$LIMP_MODE_DECISION" == "true" ] && LIMP_MODE_FLAG="--is-in-limp-mode"
            fi

            # Load tourguide state
            LAST_TOURGUIDE_TIME=0
            LAST_TOURGUIDE_RADIO=""
            [ -f /var/run/tourguide_state ] && source /var/run/tourguide_state

            # Encode
            collect_radio_mcs
            INTERFACES_JSON=$(collect_interfaces_json)
            ENCODER_ARGS=(
                "--mean-throughput-mbps" "$MEAN_THROUGHPUT"
                "--channel-report-json" "$SCAN_REPORT_JSON"
                "--timestamp" "$NOW"
                "--config-ack-version" "$(cat /var/run/mesh_config_ack_version 2>/dev/null || echo "")"
                "--last-tourguide-timestamp" "$LAST_TOURGUIDE_TIME"
                "--last-tourguide-radio" "$LAST_TOURGUIDE_RADIO"
                "--wifi-24-tx-mcs" "${WLAN0_TX_MCS:-}"
                "--wifi-24-rx-mcs" "${WLAN0_RX_MCS:-}"
                "--wifi-5-tx-mcs" "${WLAN1_TX_MCS:-}"
                "--wifi-5-rx-mcs" "${WLAN1_RX_MCS:-}"
                "--halow-tx-mcs" "${WLAN2_TX_MCS:-}"
                "--halow-rx-mcs" "${WLAN2_RX_MCS:-}"
                "--halow-mcs-peer" "${WLAN2_MCS_PEER:-}"
                "--interfaces-json" "$INTERFACES_JSON"
            )
            AP_SSID=$(collect_ap_ssid)
            [ -n "$AP_SSID" ] && ENCODER_ARGS+=("--ap-ssid" "$AP_SSID")
            [ -n "$IS_GATEWAY_FLAG" ] && ENCODER_ARGS+=("$IS_GATEWAY_FLAG")
            [ -n "$GATEWAY_IFACE" ] && ENCODER_ARGS+=("--gateway-iface" "$GATEWAY_IFACE")
            [ -n "$IS_NTP_FLAG" ] && ENCODER_ARGS+=("$IS_NTP_FLAG")
            [ -n "$IS_MEDIAMTX_FLAG" ] && ENCODER_ARGS+=("$IS_MEDIAMTX_FLAG")
            [ -n "$IS_MUMBLE_FLAG" ] && ENCODER_ARGS+=("$IS_MUMBLE_FLAG")
            [ -n "$LIMP_MODE_FLAG" ] && ENCODER_ARGS+=("$LIMP_MODE_FLAG")

            # --- GPS Location ---
            # Reject stale files as well as missing fixes. If gps-reader stops updating
            # its timestamp, the last recorded position is no longer safe to publish.
            GPS_LAT=""; GPS_LON=""; GPS_ALT=""
            if [ -f "$GPS_STATUS_FILE" ]; then
                eval "$(python3 -c "
import json, sys, time
try:
    d = json.load(open(sys.argv[1]))
    if d.get('has_fix') and time.time() - d.get('timestamp', 0) <= float(sys.argv[2]):
        print('GPS_LAT=' + str(d['latitude']))
        print('GPS_LON=' + str(d['longitude']))
        print('GPS_ALT=' + str(d['altitude']))
except Exception:
    pass
" "$GPS_STATUS_FILE" "$GPS_FIX_MAX_AGE" 2>/dev/null)"
            fi
            [ -n "$GPS_LAT" ] && ENCODER_ARGS+=("--latitude" "$GPS_LAT" "--longitude" "$GPS_LON" "--altitude" "$GPS_ALT")

            CURRENT_PAYLOAD=$("$ENCODER_PATH" telemetry "${ENCODER_ARGS[@]}" 2>/dev/null)
            if [ -z "$CURRENT_PAYLOAD" ]; then
                # re-run only on failure, to surface why nothing was published
                log "WARN: encoder produced no payload, not publishing: $("$ENCODER_PATH" telemetry "${ENCODER_ARGS[@]}" 2>&1 >/dev/null | tr '\n' ' ')"
            fi

            if [ -n "$CURRENT_PAYLOAD" ]; then
                echo -n "$CURRENT_PAYLOAD" | alfred -s $ALFRED_DATA_TYPE
                LAST_PUBLISHED_PAYLOAD="$CURRENT_PAYLOAD"
                LAST_PUBLISH_TIME=$NOW
            fi
        fi

        # === STAGE 3: REGISTRY REFRESH AND IP MANAGEMENT (every pass) ===
        [ -x "$IP_MANAGER" ] && "$IP_MANAGER"

        # === STAGE 4: CHANNEL ELECTION (every 3 min at :25) ===
        if should_perform_action "ELECTION" 180 25; then
            log "=== CHANNEL ELECTION ($(date +'%H:%M:%S')) ==="
            [ -x "$CHANNEL_ELECTION" ] && "$CHANNEL_ELECTION"
        fi

        # === STAGE 6: QUORUM CHECK ===
        if ! acs_agreement_busy && [ -x "$QUORUM_CHECKER" ]; then
            if ! check_mesh_quorum; then
                log "Quorum check failed. Returning to lobby."
                return_to_lobby
                continue
            fi
        fi

        # === STAGE 7.5: PARTITION MERGE ===
        # tourguide-manager writes PARTITION_MERGE=true when it met a larger
        # partition in the lobby. Apply its channels here, then drop the file
        # so the next channel election starts clean. This migrates one node
        # per tourguide turn; the shrinking remainder either follows the same
        # way or fails quorum and gets rescued via the lobby.
        if ! acs_agreement_busy && [ -f "$ELECTION_OUTPUT_FILE" ] && grep -q "^PARTITION_MERGE=true" "$ELECTION_OUTPUT_FILE"; then
            MERGE_2_4=$(grep "^WINNER_2_4=" "$ELECTION_OUTPUT_FILE" | cut -d'=' -f2)
            MERGE_5_0=$(grep "^WINNER_5_0=" "$ELECTION_OUTPUT_FILE" | cut -d'=' -f2)
            rm -f "$ELECTION_OUTPUT_FILE"

            if acs_write_channels "$MERGE_2_4" "$MERGE_5_0"; then
                log ">>> PARTITION MERGE: migrating to 2.4=${MERGE_2_4}, 5=${MERGE_5_0}"
                restart_mesh_supplicants
                sleep 5
                continue
            else
                log "PARTITION MERGE: invalid winner channels ('$MERGE_2_4'/'$MERGE_5_0'), discarded"
            fi
        fi

        # === STAGE 8: LIMP MODE MANAGEMENT ===
        [ -x "$LIMP_MODE_MANAGER" ] && "$LIMP_MODE_MANAGER"

        # === STAGE 9: OTHER ELECTIONS ===
        for election_script in /usr/local/bin/*-election.sh; do
            if [[ -f "$election_script" && -x "$election_script" && "$election_script" != "$CHANNEL_ELECTION" ]]; then
                # Skip mediamtx-election.sh if MTX not enabled
                if [[ "$election_script" =~ mediamtx-election ]]; then
                    MTX_ENABLED=$(grep "^mtx=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
                    [[ "$MTX_ENABLED" != "y" ]] && continue
                fi
                if [[ "$election_script" =~ mumble-election ]]; then
                    MUMBLE_ENABLED=$(grep "^mumble=" /etc/mesh.conf 2>/dev/null | cut -d'=' -f2)
                    [[ "$MUMBLE_ENABLED" != "y" ]] && continue
                fi
                "$election_script" &
            fi
        done

        # === STAGE 10: TOURGUIDE (every 2 min at :30) ===
        # The runner checks live HaLow readiness before election and departure;
        # an available S1G recovery radio keeps both Wi-Fi radios on data.
        if ! acs_agreement_busy && should_perform_tourguide; then
            log "=== TOURGUIDE WINDOW ($(date +'%H:%M:%S')) ==="
            [ -x "$TOURGUIDE_MANAGER" ] && "$TOURGUIDE_MANAGER" &
        fi

    fi  # End of data channel state

    # Poll discovery promptly while waiting for the first allocation.
    if [ -s /var/run/my_ipv4_chunk ]; then
        sleep "$MONITOR_INTERVAL"
    else
        sleep "$STARTUP_MONITOR_INTERVAL"
    fi
done

log "Main loop exited unexpectedly. Restarting..."
exit 1
