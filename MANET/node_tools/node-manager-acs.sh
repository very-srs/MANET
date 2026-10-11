#!/bin/bash
# Mesh Node Manager - Main Orchestrator
# Coordinates timing and delegates complex tasks to specialized scripts

. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/mesh-acs-common.sh" ||
    exit 1
. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/manet-manager-common.sh" \
    || exit 1

# --- Configuration ---
CONTROL_IFACE="br0"
# Public Alfred records split stable identity (67) from current telemetry (68).
ALFRED_IDENTITY_TYPE=67
ALFRED_DATA_TYPE=68
# Loop sleep durations in seconds after and before the first allocation.
MONITOR_INTERVAL=15
STARTUP_MONITOR_INTERVAL=1

# Lobby channels
LOBBY_FREQ_2_4=2412
LOBBY_FREQ_5_0=5180

# Lobby dwell seconds before joint bootstrap scans and elects data channels.
# Allow several missed tourguide windows first so an established mesh can
# rescue new nodes before they choose initial data channels.
LOBBY_BOOTSTRAP_DWELL=300

# Radio Config. radio-setup writes runtime interface role files because wlanX
# ordering varies between otherwise identical Raspberry Pi 5 nodes.
WPA_IFACE_2_4=""
WPA_IFACE_5_0=""
WPA_CONF_2_4=""
WPA_CONF_5_0=""

# Scan frequencies match channel-election.sh candidates. Exclude the lobby
# frequencies reserved for rendezvous.
SCAN_FREQS_2_4="2437 2462"
SCAN_FREQS_5_0="5200 5220 5240 5745 5765 5785 5805 5825"

# Minimum scan dwell for occupancy data. Busy time has millisecond
# resolution, so a 10 ms visit yields only 10% steps.
SURVEY_MIN_DWELL_MS=25

# Publish the median of three valid visits within ten minutes. A single
# short off-channel visit is too variable to justify a migration.
SURVEY_BUSY_SAMPLES=3
SURVEY_BUSY_MAX_AGE=600

# Helper scripts
IP_MANAGER="/usr/local/bin/mesh-ip-manager.sh"
CHANNEL_ELECTION="/usr/local/bin/channel-election.sh"
TOURGUIDE_MANAGER="/usr/local/bin/tourguide-manager.sh"
QUORUM_CHECKER="/usr/local/bin/quorum-checker.sh"
LIMP_MODE_MANAGER="/usr/local/bin/limp-mode-manager.sh"
ELECTION_OUTPUT_FILE="/var/run/mesh_channel_election"
ENCODER_PATH="/usr/local/bin/encoder.py"
BATCTL_PATH="/usr/sbin/batctl"
PEER_COUNTER="$(dirname "${BASH_SOURCE[0]}")/mesh-peer-count.sh"
THROUGHPUT_MEAN="/usr/local/bin/mesh-throughput-mean.sh"
RADIO_STATE_SYNC="/usr/local/bin/mesh-radio-state.py"
CONFIG_SYNC="/usr/local/bin/mesh-config-sync.py"
CONFIG_ROLLBACK="/usr/local/bin/mesh-config-rollback.sh"
HALOW_MCS_SUMMARY="/usr/local/bin/halow-mcs-summary.py"

# --- State Variables ---
# Use boot-clock MONO from uptime_now for local timers, unaffected by time
# sync steps. NEVER places an unset timer far enough back to be due at once.
NEVER=-1000000
LAST_PUBLISH_TIME=$NEVER

# Publish identity at startup and allocation changes, then every 270 s.
# Alfred expires records at ALFRED_DATA_TIMEOUT = 600 s, allowing one missed
# keepalive. The separate timer runs in both lobby and data state.
LAST_IDENTITY_PUBLISH=$NEVER
LAST_IDENTITY_ALLOCATION=""
LAST_ACK_PUBLISHED=""
SYNCTHING_ID=""
IDENTITY_PUBLISH_INTERVAL=270
CACHED_SCAN_REPORT_JSON="{}"
LOBBY_ENTERED_TIME=0
BOOTSTRAP_START_WINDOW=-1
LOBBY_SOLO_LOGGED=false
CLOCK_READY_SEEN=false
LIMP_STATE_FILE="/var/run/mesh_limp_mode.state"

# Window tracking
declare -A LAST_ACTION_WINDOW

# Write a prefixed message through the shared logging helper.
log() {
    manet_log NODE-MGR "$*"
}


# === GATEWAY DETECTION ===
# Gateway state is owned by manet-uplink-dispatch.sh. Node manager only
# asks it to reconcile periodically before publishing status to Alfred.
GATEWAY_STATE_FILE="/var/run/mesh-gateway.state"
LAST_GW_CHECK=$NEVER
GW_CHECK_INTERVAL=60


# Time source advertisement (the time service owns chrony and these markers)
GPS_STATUS_FILE="/run/gps_status.json"
GPS_FIX_MAX_AGE=60


# Run each clock-aligned action once per interval at the first wakeup at or
# after its offset. A narrow +/-2 s window can be skipped by the 15 s sleep.
should_perform_action() {
    local action_name=$1
    local interval_seconds=$2
    local offset_seconds=$3

    # SCAN and ELECTION require synchronization. Update LAST_ACTION_WINDOW
    # only once the supplied interval's offset in seconds is reached.
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

# Shared telemetry collectors: manet-common.sh

# Run service elections after address allocation, once per steady interval.
# Before discovery completes or times out, a self-only registry could take
# a peer's VIP. Local locks and this gate reduce churn but cannot prevent
# duplicate holders across partitions until registries converge.
ELECTION_INTERVAL=15
LAST_ELECTION_RUN=""


# Clear legacy bitrate masks and limp state when leaving the lobby. Masks
# from tourguide hops or limp entry survive supplicant restarts, while
# limp-mode-manager runs only in data state.
leave_lobby_cleanup() (
    exec 8>"${MANET_ACS_LOCK_FILE:-/run/channel-election.lock}"
    flock -n 8 || return 1
    load_mesh_roles
    [ -n "$WPA_IFACE_2_4" ] && iw dev "$WPA_IFACE_2_4" set bitrates 2>/dev/null
    [ -n "$WPA_IFACE_5_0" ] && iw dev "$WPA_IFACE_5_0" set bitrates 2>/dev/null
    rm -f "$LIMP_STATE_FILE"
)

# Apply supplied 2.4/5 GHz MHz values and clear lobby bitrate/limp state.
adopt_helper_channels() {
    # A live lobby channel may already be the authenticated data destination.
    # Check actual channels and restart only changed radios, under one lock.
    acs_write_channels "$1" "$2" data changed || return 1
    leave_lobby_cleanup
    return 0
}

# Reload WPA roles and print discovery state, defaulting to true if unavailable.
is_in_lobby() {
    load_mesh_roles

    if ! acs_configs_ready; then
        log "Mesh WPA configs not ready: $WPA_CONF_2_4 / $WPA_CONF_5_0"
        echo "true"
        return
    fi

    # Search and data frequencies overlap. Frequency is only a startup hint.
    # Authenticated adoption and explicit return-to-lobby set the mode.
    acs_discovery_state || echo "true"
}

# Count distinct batman-adv peers across all interfaces, including HaLow.
# Any radio contact supplies the peer needed for joint lobby bootstrap.
mesh_peer_count() {
    "$PEER_COUNTER" --batctl "$BATCTL_PATH"
}

# Set BOOTSTRAPPING from clock readiness and live peers.
# Use NOW for BOOTSTRAP_START_WINDOW and update LOBBY_SOLO_LOGGED.
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

# Return failure only for confirmed quorum loss from QUORUM_CHECKER.
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

# Apply LOBBY_FREQ_2_4/5_0 in search mode, then wait five seconds.
return_to_lobby() {
    log "Returning to lobby channels..."
    acs_write_channels "$LOBBY_FREQ_2_4" "$LOBBY_FREQ_5_0" search all || return 1
    sleep 5
}

# Return the phy backing an interface (same derivation as radio-setup.sh's
# iface_phy).


# Filter frequencies to those the PHY supports. One unsupported frequency
# rejects the entire iw scan request, whose exit status perform_scan ignores.
# Before /root/regulatory.db is installed, UNII-3 candidates 5745-5825 MHz
# can be absent or tagged no IR/DFS. This filter does not set regulatory policy.
# If the PHY cannot be read or parsed, return the full input list to avoid
# disabling a whole band.
phy_usable_freqs() {
    local iface="$1"
    local wanted="$2"
    local phyname info usable="" freq

    phyname="$(iface_phy "$iface")"
    [ -z "$phyname" ] && { printf '%s\n' "$wanted"; return; }

    info="$(iw phy "$phyname" info 2>/dev/null)"
    [ -z "$info" ] && { printf '%s\n' "$wanted"; return; }

    for freq in $wanted; do
        # iw prints "* 5220.0 MHz [44] (30.0 dBm)". Exclude disabled, no IR and
        # radar detection entries because mesh points cannot start on them.
        local pattern="[[:space:]]${freq}\.0 MHz.*"
        pattern+='\((disabled|no IR|radar detection|passive scan)'
        if printf '%s\n' "$info" | grep -qE "$pattern"; then
            continue
        fi
        printf '%s\n' "$info" |
            grep -q "[[:space:]]${freq}\.0 MHz" && usable+="$freq "
    done

    if [ -z "$usable" ]; then
        log "WARNING: no candidate frequency usable on $iface ($phyname). Scanning unfiltered."
        printf '%s\n' "$wanted"
    else
        printf '%s\n' "${usable% }"
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

# Keep bounded local history in the existing scan cache. The encoder sends
# only the public fields, so peers score the same published median.
smooth_scan_report() {
    local report now smoothed
    report=$(cat)
    if now=$(uptime_now) && smoothed=$(jq -ce \
        --arg previous "$CACHED_SCAN_REPORT_JSON" \
        --argjson now "$now" --argjson count "$SURVEY_BUSY_SAMPLES" \
        --argjson max_age "$SURVEY_BUSY_MAX_AGE" '
        ($previous | try fromjson catch {}
         | if type == "object" then . else {} end) as $previous
        |
        .results |= map(
            . as $sample
            | ([ $previous.results[]?
                 | select(.channel == $sample.channel
                          and ._survey_iface == $sample._survey_iface)
                 | ._busy_samples[]?
                 | select(.[0] <= $now and $now - .[0] <= $max_age) ]
               + (if (.busy_pct | type) == "number"
                  then [[$now, .busy_pct]] else [] end))[-$count:]
              as $history
            | ._busy_samples = $history
            | if (.busy_pct | type) == "number"
                 and ($history | length) == $count
              then .busy_pct = ($history | map(.[1]) | sort
                               | .[($count / 2 | floor)])
              else del(.busy_pct) end)
        ' <<< "$report"); then
        printf '%s\n' "$smoothed"
    else
        # Keep the scan usable without trusting one unsmoothed busy sample.
        # This fallback must also work when jq itself is unavailable.
        printf '%s\n' "$report" | python3 -c '
import json, sys
report = json.load(sys.stdin)
for sample in report["results"]:
    sample.pop("busy_pct", None)
print(json.dumps(report, separators=(",", ":")))'
    fi
}

# Scan enabled WPA role bands and print channel, noise, BSS and busy JSON.
# Use SCAN_FREQS_2_4/5_0 and SURVEY_MIN_DWELL_MS for candidate measurements.
perform_scan() {
    local json_out='{"results": ['
    local first_entry=true
    # Clear per-frequency survey snapshots for each interface to prevent
    # stale counters from entering the next radio's deltas.
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

        # Bracket each scan with snapshots. Lifetime counters otherwise compare
        # the home channel's average since boot with brief visits to candidates.
        while read -r f n a b t; do
            [ -z "$f" ] && continue
            pre_act[$f]=$a; pre_busy[$f]=$b; pre_tx[$f]=$t
        done < <(iw dev "$iface" survey dump 2>/dev/null | parse_survey)

        (iw dev "$iface" scan freq $freqs_to_scan > /dev/null 2>&1) &
        SCAN_PID=$!

        # Poll scan completion for up to five seconds, then send termination.
        # Read survey and cached BSS results even if the scan command failed.
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
            # Omit unvisited channels after scan failure or unavailability.
            # A synthetic -100 dBm floor would outrank real measurements.
            # find_best_channel holds the current channel if a band has no data.
            [ "$noise" = "-" ] && continue
            # Count cached scan entries with this MHz followed by a literal
            # decimal dot.
            local bss_count=$(printf '%s\n' "$scan_data" |
                grep -c "freq: ${freq}\." )

            # Occupancy excludes local transmit time. Same-driver counter ratios
            # compare across nodes and chip revisions without calibration.
            # Local receive time still counts on the home channel. Hysteresis
            # covers the few percent of airtime used by a healthy mesh.
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
                # Negative deltas mean counters reset during the scan after a
                # supplicant restart or netdev reset. Use post-reset counts.
                if [ "$d_act" -lt 0 ] || [ "$d_busy" -lt 0 ] || [ "$d_tx" -lt 0 ]; then
                    d_act=$ca; d_busy=$cb; d_tx=$ct
                fi
                # Require minimum dwell before clamping occupancy to 0..100%.
                if [ "$d_act" -ge "$SURVEY_MIN_DWELL_MS" ]; then
                    local occupancy=$(( (d_busy - d_tx) * 100 / d_act ))
                    [ "$occupancy" -lt 0 ] && occupancy=0
                    [ "$occupancy" -gt 100 ] && occupancy=100
                    occupancy_field=", \"busy_pct\": ${occupancy}"
                fi
            fi

            [ "$first_entry" = true ] && first_entry=false || json_out+=","
            json_out+="{\"channel\": ${freq}, \"noise_floor\": ${noise},"
            json_out+="\"_survey_iface\": \"${iface}\","
            json_out+="\"bss_count\": ${bss_count}${occupancy_field}}"
        done
    done

    json_out+=']}'
    printf '%s\n' "$json_out" | smooth_scan_report
}


# Claim one LAST_ACTION_WINDOW entry between seconds 30 and 49 of each
# even minute, only after initial time synchronization.
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
    NOW=$(date +%s)  # shared clock-aligned windows only
    MONO=$(uptime_now) || { log "WARN: boot clock unreadable; skipping this pass"; sleep 15; continue; }

    # First verified time sync resets timers and scan data before timed ACS.
    if [ "$CLOCK_READY_SEEN" = false ] && acs_clock_ready; then
        CLOCK_READY_SEEN=true
        LAST_ACTION_WINDOW=()
        LAST_PUBLISH_TIME=$NEVER; LAST_IDENTITY_PUBLISH=$NEVER; LAST_GW_CHECK=$NEVER
        CACHED_SCAN_REPORT_JSON="{}"
        LOBBY_ENTERED_TIME=0; BOOTSTRAP_START_WINDOW=-1
        log "Initial time sync complete; restarting discovery and ACS scheduling."
    fi

    # Tourguide temporarily rewrites a config to the lobby. Do not interpret
    # that as operating state or run scans, quorum checks or elections
    # during temporary peer loss. The child holds the channel lock.
    ACS_LOCK_FILE="${MANET_ACS_LOCK_FILE:-/var/run/channel-election.lock}"
    if [ -e "$ACS_LOCK_FILE" ] && ! flock -n "$ACS_LOCK_FILE" true; then
        sleep "$STARTUP_MONITOR_INTERVAL"
        continue
    fi

    # === ALFRED RADIO STATE SYNC ===
    # Global radio up/down changes are staged through Alfred and only applied
    # after all nodes have ACKed the same version.
    sync_alfred_command "$RADIO_STATE_SYNC" 71 || true

    # === ALFRED CONFIG SYNC ===
    # Same shape for mesh configuration: stage what the operator broadcast,
    # publish an ACK, and apply once activate_at passes.
    sync_alfred_command "$CONFIG_SYNC" 70 || true

    # Check locally whether a disruptive config change needs rollback.
    [ -x "$CONFIG_ROLLBACK" ] && "$CONFIG_ROLLBACK" check || true

    # An ACK is only useful if peers see it promptly, so a change here brings
    # the next telemetry publish forward instead of waiting out the interval.
    CURRENT_ACK=$(cat /var/run/mesh_config_ack_version 2>/dev/null || echo "")
    if [ "$CURRENT_ACK" != "$LAST_ACK_PUBLISHED" ]; then
        LAST_PUBLISH_TIME=$NEVER
        LAST_ACK_PUBLISHED="$CURRENT_ACK"
    fi

    # === PUBLISH IDENTITY (Alfred type 67) ===
    # Advertise while discovering, and on the first pass after a claim/release.
    # The 270 s interval is only a keepalive for an unchanged allocation.
    MY_CHUNK=$(cat /var/run/my_ipv4_chunk 2>/dev/null || true)
    IDENTITY_READY=true
    IDENT_IPV4=""
    if [ -n "$MY_CHUNK" ] && ! IDENT_IPV4=$(node_primary_ipv4 2>/dev/null); then
        IDENTITY_READY=false
        log "WARN: primary IPv4 unavailable; identity publish deferred until next pass"
    fi
    IDENTITY_ALLOCATION="${MY_CHUNK}:${IDENT_IPV4}"
    if [ "$IDENTITY_READY" = true ] && { [ -z "$MY_CHUNK" ] || [ "$IDENTITY_ALLOCATION" != "$LAST_IDENTITY_ALLOCATION" ] ||
            [ $((MONO - LAST_IDENTITY_PUBLISH)) -ge $IDENTITY_PUBLISH_INTERVAL ]; }; then
        # Put br0's MAC first. encoder.py drops it because Alfred stamps records
        # with that MAC.
        IDENT_MACS=("$MY_MAC")
        for iface in /sys/class/net/wlan* /sys/class/net/bat0 /sys/class/net/end0; do
            iface=$(basename "$iface")
            if [ -d "/sys/class/net/$iface" ]; then
                MAC=$(cat "/sys/class/net/$iface/address" 2>/dev/null)
                [ -n "$MAC" ] && IDENT_MACS+=("$MAC")
            fi
        done

        # Cache by certificate metadata. Missing IDs retry next publish.
        if SYNCTHING_ID=$(node_syncthing_id 2>/dev/null); then
            IDENTITY_ARGS=(
                "--hostname" "$(hostname)"
                "--mac-addresses" "${IDENT_MACS[@]}"
                "--syncthing-id" "$SYNCTHING_ID"
                "--ipv4-chunk" "${MY_CHUNK:-0}"
                "--ipv4-chunk-size" "$(cat /var/run/my_ipv4_chunk_size 2>/dev/null || echo 0)"
            )
            [ -n "$MY_CHUNK" ] && [ -n "$IDENT_IPV4" ] && IDENTITY_ARGS+=("--ipv4-address" "$IDENT_IPV4")

            IDENTITY_PAYLOAD=$("$ENCODER_PATH" identity "${IDENTITY_ARGS[@]}" 2>/dev/null)
            if [ -n "$IDENTITY_PAYLOAD" ]; then
                if printf '%s' "$IDENTITY_PAYLOAD" |
                    alfred -s $ALFRED_IDENTITY_TYPE; then
                    LAST_IDENTITY_PUBLISH=$MONO
                    LAST_IDENTITY_ALLOCATION="$IDENTITY_ALLOCATION"
                fi
            else
                log "WARN: identity encoder produced no payload"
            fi
        else
            log "WARN: Syncthing identity lookup failed; identity publish deferred until next pass"
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
            [ "$LOBBY_ENTERED_TIME" -eq 0 ] && LOBBY_ENTERED_TIME=$MONO
            if [ $((MONO - LOBBY_ENTERED_TIME)) -ge "$LOBBY_BOOTSTRAP_DWELL" ]; then
                # A solo searcher never elects itself onto data channels.
                # The agreement daemon follows the synchronized rotation, or
                # parks at fixed anchors before clock sync. Any BATMAN contact
                # holds discovery still for recovery or joint bootstrap.
                update_lobby_bootstrap
            fi
        fi

        # === BOOTSTRAP STAGE 1: RF SCAN (every 3 min at :10) ===
        if [ "$BOOTSTRAPPING" = true ] && ! acs_agreement_busy && should_perform_action "SCAN" 180 10; then
            log "=== LOBBY BOOTSTRAP SCAN ==="
            CACHED_SCAN_REPORT_JSON=$(perform_scan)
        fi

        # === PUBLISH STATUS (so other nodes can see us) ===
        # Bootstrap status uses the data-state :15 window so peer scan reports
        # arrive before the :25 election. Otherwise use the 3-minute timer.
        DO_LOBBY_PUBLISH=false
        if [ "$BOOTSTRAPPING" = true ]; then
            should_perform_action "PUBLISH" 180 15 && DO_LOBBY_PUBLISH=true
        else
            time_since_publish=$((MONO - LAST_PUBLISH_TIME))
            [ $time_since_publish -ge 180 ] && DO_LOBBY_PUBLISH=true
        fi
        # Advertise cold nodes before allocation without waiting for ACS windows
        # so other joiners can discover them.
        [ ! -s /var/run/my_ipv4_chunk ] && DO_LOBBY_PUBLISH=true
        if [ "$DO_LOBBY_PUBLISH" = true ]; then
            log "=== LOBBY PUBLISH ==="
            
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
            UPTIME_SECS=$(awk '{print int($1)}' /proc/uptime 2>/dev/null)
            [ -n "$UPTIME_SECS" ] && ENCODER_ARGS+=("--uptime-seconds" "$UPTIME_SECS")
            CPU_LOAD=$(awk '{print $1}' /proc/loadavg 2>/dev/null)
            [ -n "$CPU_LOAD" ] && ENCODER_ARGS+=("--cpu-load-average" "$CPU_LOAD")

            # --- GPS Location ---
            # Reject stale files and missing fixes. If gps-reader stops,
            # its last position is unsafe to publish.
            # Prefer boot-clock age (-5..60 s), falling back to wall-clock age
            # when written_boot is absent. Require numeric location fields.
            GPS_LAT=""; GPS_LON=""; GPS_ALT=""
            if [ -f "$GPS_STATUS_FILE" ]; then
                read -r GPS_LAT GPS_LON GPS_ALT < <(jq -r --argjson max "$GPS_FIX_MAX_AGE" --argjson up "$(uptime_now)" '
                    select(.has_fix and ((.timestamp // 0) | type) == "number"
                           and (if (.written_boot | type) == "number"
                                then ($up - .written_boot) as $age | $age >= -5 and $age <= $max
                                else now - (.timestamp // 0) <= $max end)
                           and ([.latitude, .longitude, .altitude] | all(type == "number")))
                    | "\(.latitude) \(.longitude) \(.altitude)"' "$GPS_STATUS_FILE" 2>/dev/null) || true
            fi
            [ -n "$GPS_LAT" ] && ENCODER_ARGS+=("--latitude" "$GPS_LAT" "--longitude" "$GPS_LON" "--altitude" "$GPS_ALT")

            CURRENT_PAYLOAD=$("$ENCODER_PATH" telemetry "${ENCODER_ARGS[@]}" 2>/dev/null)
            if [ -z "$CURRENT_PAYLOAD" ]; then
                # re-run only on failure, to surface why nothing was published
                log "WARN: encoder produced no payload, not publishing: $("$ENCODER_PATH" telemetry "${ENCODER_ARGS[@]}" 2>&1 >/dev/null | tr '\n' ' ')"
            fi

            if [ -n "$CURRENT_PAYLOAD" ]; then
                if printf '%s' "$CURRENT_PAYLOAD" |
                    alfred -s $ALFRED_DATA_TYPE; then
                    LAST_PUBLISH_TIME=$MONO
                fi
            fi
        fi

        # Check discovery after publishing both records, including on the first
        # pass. The helper retains its full 10/20-second observation window.
        [ -x "$IP_MANAGER" ] && "$IP_MANAGER"
        if [ -z "$MY_CHUNK" ] && [ -s /var/run/my_ipv4_chunk ]; then
            LAST_PUBLISH_TIME=$NEVER
            continue  # advertise the new claim before the steady-state sleep
        fi

        # === RUN SERVICE ELECTIONS (needed for services to start) ===
        run_service_elections
        
        # === CHECK FOR HELPER BEACON (non-blocking) ===
        HELPER_MIGRATED=false
        if ! acs_agreement_busy; then
            HELPER_CHANNELS=$(python3 "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/mesh-channel-agreement.py" helper-select 2>/dev/null) || HELPER_CHANNELS=""
            if [ -n "$HELPER_CHANNELS" ]; then
                # Authenticated helper output is 2.4 GHz MHz|5 GHz MHz, with
                # absent bands empty.
                IFS='|' read -r DATA_CHANNEL_2_4 DATA_CHANNEL_5_0 <<< "$HELPER_CHANNELS"
                if adopt_helper_channels "$DATA_CHANNEL_2_4" "$DATA_CHANNEL_5_0"; then
                    log "Fresh authenticated helper. Migrating to 2.4=${DATA_CHANNEL_2_4}, 5=${DATA_CHANNEL_5_0}"
                    HELPER_MIGRATED=true
                fi
            fi
        fi

        # === BOOTSTRAP STAGE 2: CHANNEL ELECTION (every 3 min at :25) ===
        # Wait for a full scan/publish/replicate round before requesting a plan.
        # The agreement service gathers a majority and activates it later.
        # A helper rescue takes priority over electing new channels. Skip the
        # partial window where bootstrap begins to avoid electing before peer
        # reports arrive.
        if [ "$BOOTSTRAPPING" = true ] && [ "$HELPER_MIGRATED" = false ] && \
           [ $((NOW / 180)) -gt "$BOOTSTRAP_START_WINDOW" ] && \
           should_perform_action "ELECTION" 180 25; then
            log "=== LOBBY BOOTSTRAP ELECTION ==="
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
            log "=== SCAN ==="
            SCAN_REPORT_JSON=$(perform_scan)
            CACHED_SCAN_REPORT_JSON="$SCAN_REPORT_JSON"
        else
            SCAN_REPORT_JSON="$CACHED_SCAN_REPORT_JSON"
        fi

        # === STAGE 2: PUBLISH (every 3 min at :15) ===
        if [ ! -s /var/run/my_ipv4_chunk ] || should_perform_action "PUBLISH" 180 15; then
            log "=== PUBLISH ==="

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
            # Reject stale files and missing fixes. If gps-reader stops,
            # its last position is unsafe to publish.
            # Prefer boot-clock age (-5..60 s), falling back to wall-clock age
            # when written_boot is absent. Require numeric location fields.
            GPS_LAT=""; GPS_LON=""; GPS_ALT=""
            if [ -f "$GPS_STATUS_FILE" ]; then
                read -r GPS_LAT GPS_LON GPS_ALT < <(jq -r --argjson max "$GPS_FIX_MAX_AGE" --argjson up "$(uptime_now)" '
                    select(.has_fix and ((.timestamp // 0) | type) == "number"
                           and (if (.written_boot | type) == "number"
                                then ($up - .written_boot) as $age | $age >= -5 and $age <= $max
                                else now - (.timestamp // 0) <= $max end)
                           and ([.latitude, .longitude, .altitude] | all(type == "number")))
                    | "\(.latitude) \(.longitude) \(.altitude)"' "$GPS_STATUS_FILE" 2>/dev/null) || true
            fi
            [ -n "$GPS_LAT" ] && ENCODER_ARGS+=("--latitude" "$GPS_LAT" "--longitude" "$GPS_LON" "--altitude" "$GPS_ALT")

            CURRENT_PAYLOAD=$("$ENCODER_PATH" telemetry "${ENCODER_ARGS[@]}" 2>/dev/null)
            if [ -z "$CURRENT_PAYLOAD" ]; then
                # re-run only on failure, to surface why nothing was published
                log "WARN: encoder produced no payload, not publishing: $("$ENCODER_PATH" telemetry "${ENCODER_ARGS[@]}" 2>&1 >/dev/null | tr '\n' ' ')"
            fi

            if [ -n "$CURRENT_PAYLOAD" ]; then
                if printf '%s' "$CURRENT_PAYLOAD" |
                    alfred -s $ALFRED_DATA_TYPE; then
                    LAST_PUBLISH_TIME=$MONO
                fi
            fi
        fi

        # === STAGE 3: REGISTRY REFRESH AND IP MANAGEMENT (every pass) ===
        [ -x "$IP_MANAGER" ] && "$IP_MANAGER"
        if [ -z "$MY_CHUNK" ] && [ -s /var/run/my_ipv4_chunk ]; then
            LAST_PUBLISH_TIME=$NEVER
            continue  # publish the allocation without waiting another 15 seconds
        fi

        # === STAGE 4: CHANNEL ELECTION (every 3 min at :25) ===
        if should_perform_action "ELECTION" 180 25; then
            log "=== CHANNEL ELECTION ==="
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
        # Apply PARTITION_MERGE channels when tourguide-manager finds a larger
        # lobby partition, then remove the state file before the next election.
        # One node migrates per tourguide turn. Remaining nodes follow or lose
        # quorum and receive lobby rescue.
        if ! acs_agreement_busy && [ -f "$ELECTION_OUTPUT_FILE" ] && grep -q "^PARTITION_MERGE=true" "$ELECTION_OUTPUT_FILE"; then
            MERGE_2_4=$(grep "^WINNER_2_4=" "$ELECTION_OUTPUT_FILE" | cut -d'=' -f2)
            MERGE_5_0=$(grep "^WINNER_5_0=" "$ELECTION_OUTPUT_FILE" | cut -d'=' -f2)
            rm -f "$ELECTION_OUTPUT_FILE"

            if acs_write_channels "$MERGE_2_4" "$MERGE_5_0" data all; then
                log ">>> PARTITION MERGE: migrating to 2.4=${MERGE_2_4}, 5=${MERGE_5_0}"
                sleep 5
                continue
            else
                log "PARTITION MERGE: invalid winner channels ('$MERGE_2_4'/'$MERGE_5_0'), discarded"
            fi
        fi

        # === STAGE 8: LIMP MODE MANAGEMENT ===
        [ -x "$LIMP_MODE_MANAGER" ] && "$LIMP_MODE_MANAGER"

        # === STAGE 9: OTHER ELECTIONS ===
        run_service_elections

        # === STAGE 10: TOURGUIDE (every 2 min at :30) ===
        # Check live HaLow readiness before election and departure. An available
        # S1G recovery radio keeps both Wi-Fi radios on data.
        if ! acs_agreement_busy && should_perform_tourguide; then
            log "=== TOURGUIDE WINDOW ==="
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
