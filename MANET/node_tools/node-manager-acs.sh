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

# Minimum scan dwell for fresh survey data. Busy time has millisecond
# resolution, so a 10 ms visit yields only 10% steps.
SURVEY_MIN_DWELL_MS=25
SCAN_DWELL_MS=250
HOME_MIN_WINDOW_S=60
# Mesh throughput holds to about 60% foreign airtime, then falls steeply.
# The noise limit is uncalibrated: Wi-Fi interference does not move it.
HOME_FOREIGN_LIMIT=60
HOME_NOISE_LIMIT_DBM=-80

# Retain the median of three short visits on 5 GHz and during discovery.
# Home windows and long 2.4 GHz candidate visits publish directly.
SURVEY_BUSY_SAMPLES=3
SURVEY_BUSY_MAX_AGE=600
# Do not republish a missed scan window as a new acquisition.
SCAN_REPORT_MAX_AGE=180

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
declare -A SCAN_FAILED
declare -A HOME_BASELINE HOME_IDENTITY HOME_TROUBLE HOME_BAD HOME_GOOD
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
# rejects the entire iw scan request.
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
        # perform_scan logs any rejected request once per failure transition.
        printf '%s\n' "$wanted"
    else
        printf '%s\n' "${usable% }"
    fi
}

# Parse "iw dev X survey dump" into one line per frequency:
#   <freq> <noise|-> <active_ms|-> <busy_ms|-> <tx_ms|-> <in_use> <rx_ms|->
# Most counters accumulate. MT7915 resets off-channel counters on each visit.
parse_survey() {
    awk '
        $1 == "frequency:" {
            f = $2 + 0; seen[f] = 1; in_use[f] = /\[in use\]/; next
        }
        f == "" { next }
        $1 == "noise:" { noise[f] = $2; next }
        /channel active time:/ { act[f] = $4; next }
        /channel busy time:/ { busy[f] = $4; next }
        /channel transmit time:/ { tx[f] = $4; next }
        /channel receive time:/ { rx[f] = $4; next }
        END {
            for (k in seen)
                printf "%s %s %s %s %s %s %s\n", k,
                    (k in noise ? noise[k] : "-"),
                    (k in act ? act[k] : "-"),
                    (k in busy ? busy[k] : "-"),
                    (k in tx ? tx[k] : "-"), in_use[k],
                    (k in rx ? rx[k] : "-")
        }'
}

# Reset a band's measurement and consecutive-window history.
reset_home_monitor() {
    local band=$1
    unset 'HOME_BASELINE[$band]'
    HOME_BAD[$band]=0; HOME_GOOD[$band]=0
    # Only three good windows clear trouble. An unavailable window cannot.
    HOME_TROUBLE[$band]=${HOME_TROUBLE[$band]:-false}
}

# Update sender-owned hysteresis only after a valid home window.
update_home_trouble() {
    local band=$1 foreign=$2 noise=$3
    if (( foreign >= HOME_FOREIGN_LIMIT ||
          noise >= HOME_NOISE_LIMIT_DBM )); then
        HOME_BAD[$band]=$(( ${HOME_BAD[$band]:-0} + 1 ))
        HOME_GOOD[$band]=0
        if (( HOME_BAD[$band] >= 2 )); then HOME_TROUBLE[$band]=true; fi
    elif (( foreign < HOME_FOREIGN_LIMIT - 10 &&
            noise < HOME_NOISE_LIMIT_DBM - 5 )); then
        HOME_GOOD[$band]=$(( ${HOME_GOOD[$band]:-0} + 1 ))
        HOME_BAD[$band]=0
        if (( HOME_GOOD[$band] >= 3 )); then HOME_TROUBLE[$band]=false; fi
    else
        HOME_BAD[$band]=0; HOME_GOOD[$band]=0
    fi
}

# Measure home from the pre-scan arrays, with shell-only station parsing.
# Baselines live in this manager process and never survive a restart.
measure_home() {
    local band=$1 iface=$2 now=$3 freq=$4
    local stations line peers=0 nrx=0 ntx=0 rx=0 tx=0
    local old_iface old_freq old_time oa ob ot orx osrx ostx value
    local duration_re='^[[:space:]]*(rx|tx)[[:space:]]duration:'
    duration_re+='[[:space:]]+([0-9]+)'
    HOME_ENTRY=""
    if [ "${HOME_IDENTITY[$band]-}" != "$iface $freq" ]; then
        reset_home_monitor "$band"
    fi
    HOME_IDENTITY[$band]="$iface $freq"
    if [ -n "$freq" ]; then
        HOME_ENTRY="{\"channel\":$freq,"
        HOME_ENTRY+="\"rf_trouble\":${HOME_TROUBLE[$band]:-false},"
        HOME_ENTRY+="\"_survey_iface\":\"$iface\",\"_survey_boot\":$now}"
    fi
    if [ -z "$freq" ] ||
       ! stations=$(iw dev "$iface" station dump 2>/dev/null); then
        reset_home_monitor "$band"
        return
    fi
    while IFS= read -r line; do
        [[ "$line" == Station\ * ]] && peers=$((peers + 1))
        if [[ "$line" =~ $duration_re ]]; then
            value=${BASH_REMATCH[2]}
            if [ "${BASH_REMATCH[1]}" = rx ]; then
                rx=$((rx + value)); nrx=$((nrx + 1))
            else
                tx=$((tx + value)); ntx=$((ntx + 1))
            fi
        fi
    done <<< "$stations"
    for value in "${pre_act[$freq]}" "${pre_busy[$freq]}" \
                 "${pre_tx[$freq]}" "${pre_rx[$freq]}"; do
        if [[ ! "$value" =~ ^[0-9]+$ ]]; then
            reset_home_monitor "$band"
            return
        fi
    done
    if (( nrx != peers || ntx != peers )) ||
       [[ ! "${pre_noise[$freq]}" =~ ^-[0-9]+$ ]]; then
        reset_home_monitor "$band"
        return
    fi
    read -r old_iface old_freq old_time oa ob ot orx osrx ostx \
        <<< "${HOME_BASELINE[$band]-}"
    if [ -n "$old_time" ] &&
       (( now <= old_time || pre_act[$freq] < ${oa:-0} ||
          pre_busy[$freq] < ${ob:-0} || pre_tx[$freq] < ${ot:-0} ||
          pre_rx[$freq] < ${orx:-0} ||
          rx < ${osrx:-0} || tx < ${ostx:-0} )); then
        reset_home_monitor "$band"
        old_time=""
    fi
    HOME_BASELINE[$band]="$iface $freq $now ${pre_act[$freq]}"
    HOME_BASELINE[$band]+=" ${pre_busy[$freq]} ${pre_tx[$freq]}"
    HOME_BASELINE[$band]+=" ${pre_rx[$freq]} $rx $tx"
    [ -n "$old_time" ] || return 0
    local window=$((now - old_time)) active=$((pre_act[$freq] - oa))
    if (( window < HOME_MIN_WINDOW_S || active <= 0 )); then
        HOME_BAD[$band]=0; HOME_GOOD[$band]=0
        return
    fi
    local busy=$(( (pre_busy[$freq] - ob) * 100 / active ))
    # Station duration is microseconds, survey counters are milliseconds.
    local foreign=$(( ((pre_busy[$freq] - ob - pre_tx[$freq] + ot) * 1000
                       - rx + osrx) * 100 / (active * 1000) ))
    (( busy < 0 )) && busy=0
    (( busy > 100 )) && busy=100
    (( foreign < 0 )) && foreign=0
    (( foreign > 100 )) && foreign=100
    update_home_trouble "$band" "$foreign" "${pre_noise[$freq]}"
    HOME_ENTRY="{\"channel\":$freq,\"noise_floor\":${pre_noise[$freq]},"
    HOME_ENTRY+="\"busy_pct\":$busy,\"foreign_pct\":$foreign,"
    HOME_ENTRY+="\"window_s\":$window,"
    HOME_ENTRY+="\"rf_trouble\":${HOME_TROUBLE[$band]:-false},"
    HOME_ENTRY+="\"_survey_iface\":\"$iface\",\"_survey_boot\":$now}"
}

# Read only fresh reports from the agreement service's connected member view.
# One jq replaces the normal smoothing pass when no candidates are scanned.
peer_trouble_bands() {
    local now boot rest snapshot
    local status_file="${MANET_ACS_RUN_DIR:-/run}/manet-acs-status.json"
    # Capture the atomic snapshot before reading its age clock.
    snapshot=$(< "$status_file") 2>/dev/null || return 0
    read -r now rest < "${MESH_UPTIME_FILE:-/proc/uptime}" || return 0
    read -r boot < "${MANET_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}"
    boot=${boot//-/}
    jq -rn --arg boot "$boot" --argjson now "$now" \
        --arg own "NODE_${MY_MAC//:/}" \
        --argjson status "$snapshot" \
        --rawfile registry "${REGISTRY_FILE:-/var/run/mesh_node_registry}" '
        $status as $s
        | select($s.boot == $boot and ($s.observed_boot | type) == "number")
        | select($now >= $s.observed_boot and $now - $s.observed_boot < 30)
        | ($registry | split("\n") | map(
            capture("^(?<node>NODE_[0-9a-f]{12})_" +
                    "(?<key>CHANNEL_REPORT_JSON|OBSERVED_AT_UPTIME)=" +
                    "\u0027(?<value>.*)\u0027$")?
          ) | group_by(.node))[]
        | select(.[0].node != $own)
        | select(.[0].node as $node | ($s.members // []) | index($node))
        | (map({key: .key, value: .value}) | from_entries) as $r
        | ($r.OBSERVED_AT_UPTIME | tonumber?) as $seen
        | select($now >= $seen and $now - $seen < 240)
        | ($r.CHANNEL_REPORT_JSON | fromjson?).results[]?
        | select(.rf_trouble == true)
        | if .channel >= 2400 and .channel < 2500 then "2_4"
          elif .channel >= 5000 and .channel < 6000 then "5_0"
          else empty end
        ' 2>/dev/null || true
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
            if has("rf_trouble") or ._roc == true then . else
            . as $sample
            | ($sample._survey_boot // $now) as $sample_time
            | ([ $previous.results[]?
                 | select(.channel == $sample.channel
                          and ._survey_iface == $sample._survey_iface)
                 | ._busy_samples[]?
                 | select(.[0] <= $now and $now - .[0] <= $max_age) ]
               + (if (.busy_pct | type) == "number"
                  then [[$sample_time, .busy_pct]] else [] end))[-$count:]
              as $history
            | ._busy_samples = $history
            | if (.busy_pct | type) == "number"
                 and ($history | length) == $count
              then .busy_pct = ($history | map(.[1]) | sort
                               | .[($count / 2 | floor)])
              else del(.busy_pct) end end)
        ' <<< "$report"); then
        printf '%s\n' "$smoothed"
    else
        # Keep the scan usable without trusting one unsmoothed busy sample.
        # This fallback must also work when jq itself is unavailable.
        printf '%s\n' "$report" | python3 -c '
import json, sys
report = json.load(sys.stdin)
for sample in report["results"]:
    if "foreign_pct" not in sample and not sample.get("_roc"):
        sample.pop("busy_pct", None)
print(json.dumps(report, separators=(",", ":")))'
    fi
}

# Filter at each publication, including bootstrap reuse while scans are paused.
# This process-local cache cannot survive a boot. Publication never changes
# acquisition times or renews the per-channel occupancy history.
fresh_scan_report() {
    local now report
    if now=$(uptime_now) && report=$(jq -ce \
        --argjson now "$now" --argjson age "$SCAN_REPORT_MAX_AGE" '
        .results = [.results[]?
            | select((._survey_boot | type) == "number")
            | select(._survey_boot <= $now and $now - ._survey_boot <= $age)]
        ' <<< "$CACHED_SCAN_REPORT_JSON" 2>/dev/null); then
        printf '%s\n' "$report"
    else
        printf '%s\n' '{"results":[]}'
    fi
}

# Record only transitions, keeping state in the manager, not a subshell.
scan_outcome() {
    local iface=$1 failure=$2
    if [ -n "$failure" ]; then
        [ "${SCAN_FAILED[$iface]:-false}" = true ] ||
            log "Scan failed on $iface ($failure). Discarding candidates."
        SCAN_FAILED[$iface]=true
    else
        [ "${SCAN_FAILED[$iface]:-false}" = false ] || log "Scan recovered on $iface."
        SCAN_FAILED[$iface]=false
    fi
}

# Count only BSS entries from this completed flush scan. cfg80211 can retain
# held/associated entries despite flush, so require their boottime to be fresh.
# Integer and decimal MHz are both emitted by supported iw versions.
scan_bss_count() {
    awk -v freq="$1" -v started="$2" '
        function count_bss() {
            if (f == freq && (seen != "" ? seen >= started : !held)) count++
        }
        /^BSS / { count_bss(); f = ""; seen = ""; held = / -- /; next }
        $1 == "freq:" { f = $2 + 0 }
        $1 == "last" && $2 == "seen:" && $4 == "[boottime]" {
            seen = $3 + 0
        }
        END { count_bss(); print count + 0 }
    '
}

# Capture a completed short scan and its post-survey for perform_scan.
# The caller owns scan_data and post_survey so no subshell loses the result.
capture_flush_scan() {
    local iface=$1 freqs=$2 scan_file scan_pid scan_status=0
    if ! scan_file=$(mktemp); then
        scan_outcome "$iface" "scan capture unavailable"
        return 1
    fi
    # iw's combined scan itself dumps the BSS cache, so flush old entries.
    # Bound termination too, then wait to reap and capture the actual status.
    timeout --foreground --kill-after=1s 5s iw dev "$iface" scan freq $freqs flush \
        > "$scan_file" 2>/dev/null &
    scan_pid=$!
    wait "$scan_pid" 2>/dev/null || scan_status=$?
    scan_data=$(cat "$scan_file")
    rm -f "$scan_file"
    case "$scan_status" in
        0) ;;
        124|137) scan_outcome "$iface" "timeout"; return 1 ;;
        *) scan_outcome "$iface" "exit $scan_status"; return 1 ;;
    esac
    # iw also returns zero after NL80211_CMD_SCAN_ABORTED.
    if grep -qx 'scan aborted!' <<< "$scan_data"; then
        scan_outcome "$iface" "aborted"
        return 1
    fi
    if ! post_survey=$(iw dev "$iface" survey dump 2>/dev/null); then
        scan_outcome "$iface" "survey unavailable"
        return 1
    fi
    scan_outcome "$iface" ""
}

# Measure enabled Wi-Fi home windows and scan candidates only on RF trouble.
# Discovery keeps its scan path. Call directly so baselines survive cycles.
perform_scan() {
    local mode=${1:-data} peer_bands band home roc candidate_busy=false
    local needs_smoothing=false scan_ready home_bss
    peer_bands=""
    if [ "$mode" = data ]; then
        peer_bands=$(peer_trouble_bands)
        acs_agreement_busy && candidate_busy=true
    fi
    local json_out='{"results": ['
    local first_entry=true
    # Clear per-frequency survey snapshots for each interface to prevent
    # stale counters from entering the next radio's deltas.
    local -A pre_act pre_busy pre_tx pre_use pre_noise pre_rx
    local -A post_act post_busy post_tx post_noise post_use
    local f n a b t used r iface freq scan_data
    local scan_started scan_boot driver pre_survey post_survey

    load_mesh_roles

    for band in 2_4 5_0; do
        local role="WPA_IFACE_$band"
        iface=${!role}
        if ! radio_iface_enabled "$iface"; then
            reset_home_monitor "$band"
            continue
        fi
        [ "$mode" != discovery ] || reset_home_monitor "$band"
        local freqs_to_scan=""
        [ "$iface" == "$WPA_IFACE_2_4" ] && freqs_to_scan=$SCAN_FREQS_2_4
        [ "$iface" == "$WPA_IFACE_5_0" ] && freqs_to_scan=$SCAN_FREQS_5_0
        pre_act=(); pre_busy=(); pre_tx=(); pre_use=()
        pre_noise=(); pre_rx=(); home=""
        post_act=(); post_busy=(); post_tx=(); post_noise=(); post_use=()

        # Reuse this snapshot for the home window and short-scan baseline.
        if ! pre_survey=$(iw dev "$iface" survey dump 2>/dev/null); then
            reset_home_monitor "$band"
            scan_outcome "$iface" "survey unavailable"
            continue
        fi
        while read -r f n a b t used r; do
            [ -z "$f" ] && continue
            pre_act[$f]=$a; pre_busy[$f]=$b; pre_tx[$f]=$t
            pre_use[$f]=$used
            pre_noise[$f]=$n; pre_rx[$f]=$r
            [ "$used" = 1 ] && home=$f
        done < <(printf '%s\n' "$pre_survey" | parse_survey)

        if ! read -r scan_started _ < "${MESH_UPTIME_FILE:-/proc/uptime}"; then
            scan_outcome "$iface" "scan capture unavailable"
            continue
        fi
        scan_boot=${scan_started%.*}
        HOME_ENTRY=""
        scan_ready=true
        roc=false
        if [ "$mode" = data ]; then
            measure_home "$band" "$iface" "$scan_boot" "$home"
            if [ "$candidate_busy" = true ] || [ -z "$home" ] ||
               { [ "${HOME_TROUBLE[$band]:-false}" != true ] &&
                 [[ " $peer_bands " != *"$band"* ]]; }; then
                scan_ready=false
            fi
            [ "$band" != 2_4 ] || roc=true
        fi
        if [ "$scan_ready" = true ]; then
            freqs_to_scan="$(phy_usable_freqs "$iface" "$freqs_to_scan")"
            [ -n "$freqs_to_scan" ] || scan_ready=false
        fi
        home_bss=0
        if [ "$scan_ready" = true ] && [ "$roc" = false ]; then
            if capture_flush_scan "$iface" "$freqs_to_scan"; then
                while read -r f n a b t used r; do
                    [ -z "$f" ] && continue
                    post_noise[$f]=$n; post_act[$f]=$a
                    post_busy[$f]=$b; post_tx[$f]=$t; post_use[$f]=$used
                done < <(printf '%s\n' "$post_survey" | parse_survey)
                if [ -n "$HOME_ENTRY" ]; then
                    home_bss=$(scan_bss_count "$home" "$scan_started" <<< "$scan_data")
                fi
            else
                scan_ready=false
            fi
        fi
        if [ -n "$HOME_ENTRY" ]; then
            # Both sides carry zero BSS evidence on 2.4 GHz. On 5 GHz the
            # successful scan supplies home and candidate counts together.
            HOME_ENTRY="${HOME_ENTRY%\}},\"bss_count\":$home_bss}"
            [ "$first_entry" = true ] && first_entry=false || json_out+=","
            json_out+="$HOME_ENTRY"
        fi
        [ "$scan_ready" = true ] || continue
        driver=$(iface_driver "$iface")

        for freq in $freqs_to_scan; do
            # Home measurements always come from the full inter-cycle window.
            if [ "$mode" = data ] && [ "$freq" = "$home" ]; then continue; fi
            if [ "$roc" = true ]; then
                # Occupancy and noise capture real co-channel airtime.
                # No flush scan or BSS count is needed on this data path.
                # Bracket each completed ROC separately.
                pre_act=(); pre_busy=(); pre_tx=(); pre_use=()
                if ! pre_survey=$(iw dev "$iface" survey dump 2>/dev/null); then
                    continue
                fi
                while read -r f n a b t used r; do
                    [ -n "$f" ] || continue
                    pre_act[$f]=$a; pre_busy[$f]=$b; pre_tx[$f]=$t
                    pre_use[$f]=$used
                done < <(printf '%s\n' "$pre_survey" | parse_survey)
                # Do not visit a channel that became home during collection.
                [ "${pre_use[$freq]:-0}" = 0 ] || continue
                if ! timeout --foreground --kill-after=1s 5s \
                     iw dev "$iface" offchannel "$freq" "$SCAN_DWELL_MS" \
                     >/dev/null 2>&1; then
                    continue
                fi
                # iw returns once the visit is requested, not when it ends.
                # Wait out the dwell plus switching time before reading.
                sleep "$(( (SCAN_DWELL_MS + 150) / 1000 )).$(printf '%03d' \
                    $(( (SCAN_DWELL_MS + 150) % 1000 )))"
                post_act=(); post_busy=(); post_tx=()
                post_noise=(); post_use=()
                if ! post_survey=$(iw dev "$iface" survey dump 2>/dev/null)
                then
                    continue
                fi
                scan_outcome "$iface" ""
                while read -r f n a b t used r; do
                    [ -n "$f" ] || continue
                    post_act[$f]=$a; post_busy[$f]=$b; post_tx[$f]=$t
                    post_noise[$f]=$n; post_use[$f]=$used
                done < <(printf '%s\n' "$post_survey" | parse_survey)
                [ "${post_use[$freq]:-0}" = 0 ] || continue
            fi
            local noise="${post_noise[$freq]:--}"
            # Require a measured visit before accepting noise or BSS evidence.
            # A synthetic -100 dBm floor would outrank real measurements.
            # find_best_channel holds the current channel if a band has no data.
            [ "$noise" = "-" ] && continue
            local ca="${post_act[$freq]:--}" pa="${pre_act[$freq]:--}"
            local per_visit=false d_act
            [[ "$ca" =~ ^[0-9]+$ && "$pa" =~ ^[0-9]+$ ]] || continue
            if [ "$driver" = mt7915e ] &&
               [ "${pre_use[$freq]:-1}" = 0 ] && [ "${post_use[$freq]:-1}" = 0 ]; then
                # __mt76_set_channel clears non-home state on every visit.
                # Equal pre/post counts can be two equally long healthy scans.
                # Completion (including abort detection) is essential here.
                per_visit=true
                d_act=$ca
            else
                d_act=$((ca - pa))
                # Unknown counter resets cannot establish an acquisition.
            fi
            [ "$d_act" -ge "$SURVEY_MIN_DWELL_MS" ] || continue
            if [ "$roc" = true ]; then
                [ "$d_act" -ge "$SCAN_DWELL_MS" ] || continue
            fi
            local bss_count=0
            if [ "$roc" = false ]; then
                bss_count=$(scan_bss_count "$freq" "$scan_started" <<< "$scan_data")
            fi

            # Occupancy excludes local transmit time. Same-driver counter ratios
            # compare across nodes and chip revisions without calibration.
            local occupancy_field=""
            local cb="${post_busy[$freq]:--}" pb="${pre_busy[$freq]:--}"
            local ct="${post_tx[$freq]:-0}"
            if [ "$roc" = true ]; then
                [[ "$ct" =~ ^[0-9]+$ &&
                   "${pre_tx[$freq]:--}" =~ ^[0-9]+$ ]] || continue
            fi
            if [[ "$cb" =~ ^[0-9]+$ && "$pb" =~ ^[0-9]+$ ]]; then
                [ "$ct" = "-" ] && ct=0
                local pt="${pre_tx[$freq]:-0}"
                [ "$pt" = "-" ] && pt=0
                local d_busy=$((cb - pb)) d_tx=$((ct - pt))
                if [ "$per_visit" = true ]; then
                    d_busy=$cb; d_tx=$ct
                fi
                if [ "$d_busy" -ge 0 ] && [ "$d_tx" -ge 0 ]; then
                    local occupancy=$(( (d_busy - d_tx) * 100 / d_act ))
                    [ "$occupancy" -lt 0 ] && occupancy=0
                    [ "$occupancy" -gt 100 ] && occupancy=100
                    occupancy_field=", \"busy_pct\": ${occupancy}"
                fi
            fi
            # Long dwell needs valid busy and transmit deltas, not noise alone.
            if [ "$roc" = true ]; then
                [ -n "$occupancy_field" ] || continue
                occupancy_field+=', "_roc": true'
            else
                needs_smoothing=true
            fi

            [ "$first_entry" = true ] && first_entry=false || json_out+=","
            json_out+="{\"channel\": ${freq}, \"noise_floor\": ${noise},"
            json_out+="\"_survey_iface\": \"${iface}\","
            json_out+="\"_survey_boot\": ${scan_boot},"
            json_out+="\"bss_count\": ${bss_count}${occupancy_field}}"
        done
    done

    json_out+=']}'
    # Only the retained short-scan path needs the median-of-three warmup.
    if [ "$needs_smoothing" = true ]; then
        CACHED_SCAN_REPORT_JSON=$(
            printf '%s\n' "$json_out" | smooth_scan_report) ||
            CACHED_SCAN_REPORT_JSON='{"results":[]}'
    else
        CACHED_SCAN_REPORT_JSON=$json_out
    fi
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
            perform_scan discovery
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
            [ "$BOOTSTRAPPING" = true ] && \
                ENCODER_ARGS+=("--channel-report-json" "$(fresh_scan_report)")
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
        if should_perform_action "SCAN" 180 10; then
            log "=== SCAN ==="
            perform_scan
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
                "--channel-report-json" "$(fresh_scan_report)"
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
