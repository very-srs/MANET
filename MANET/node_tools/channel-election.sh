#!/bin/bash
# Request an agreed channel change or score a coordinator proposal (--score).
# If all channels are disqualified, select the best measured channel and
# assert limp mode.

set -eo pipefail

# Print the optional scoring argument to stderr.
usage() {
    printf '%s\n' \
        'usage: channel-election.sh [--score]' >&2
}

if [ $# -gt 1 ] || { [ $# -eq 1 ] && [ "$1" != --score ]; }; then
    usage
    exit 1
fi

# Scoring never changes a radio. Normal calls make this round eligible for the
# agreement service, which owns proposal, ACK, activation and recovery.
if [ "${1:-}" != "--score" ]; then
    [ "$#" -eq 0 ] || { usage; exit 1; }
    exec python3 "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/mesh-channel-agreement.py" request
fi

. "${MANET_TOOLS_DIR:-$(dirname "${BASH_SOURCE[0]}")}/mesh-acs-common.sh" || exit 1

# --- Configuration ---
REGISTRY_FILE="${REGISTRY_FILE:-/var/run/mesh_node_registry}"
OUTPUT_FILE="${OUTPUT_FILE:-/var/run/mesh_channel_election}"
LOCK_FILE="${MANET_ACS_LOCK_FILE:-${LOCK_FILE:-/var/run/channel-election.lock}}"
WPA_IFACE_2_4=""
WPA_IFACE_5_0=""
WPA_CONF_2_4=""
WPA_CONF_5_0=""

# --- Tunable Parameters ---
STALE_THRESHOLD=240 # Seconds: ignore older reports (scans run every 3 min).

# Channel score, lower is better:
#
#   occupancy% + max(0, median_noise - NOISE_REFERENCE_DBM) * NOISE_WEIGHT
#              + mean_bss_count * BSS_WEIGHT
#
# Occupancy is a ratio of counters from the same driver and scan, so it
# needs no cross-node calibration. Uncalibrated noise adds a weighted penalty.
# Mean BSS count avoids penalizing channels measured by more nodes.
NOISE_REFERENCE_DBM=-95   # noise at or below this adds nothing
NOISE_WEIGHT=0.5          # -70 dBm disqualification edge: 12.5 points
BSS_WEIGHT=0.5            # a co-channel network costs half a point of occupancy

# One node calls a channel bad if it is over either of these.
NOISE_DISQUALIFY_THRESHOLD_DBM=-70
OCCUPANCY_DISQUALIFY_PCT=85

# Required fraction of reporters that must disqualify a channel, rounded up
# to at least one: 1 of 1, 1 of 2, 2 of 3, 2 of 5, 3 of 6. This limits the
# influence of a single bad radio or local noise source.
DISQUALIFY_QUORUM=0.34

# Score improvement required to switch away from the current channel.
CHANNEL_BIAS_SCORE=10

# Enter limp mode when the best channel still exceeds this score.
# Without noise or BSS penalties, the threshold is 60% occupancy.
LIMP_MODE_SCORE_THRESHOLD=60

# Default candidates exclude boot anchors 2412/5180 MHz for rendezvous.
# Discovery mode is explicit once initialized. See manet_rendezvous.py.
# The coordinator supplies candidate capabilities and a shared incumbent.
# Receivers ACK this frozen result without re-scoring their own registry.
CHANNELS_2_4="${ACS_CHANNELS_2_4-2437 2462}"
CHANNELS_5_0="${ACS_CHANNELS_5_0-5200 5220 5240 5745 5765 5785 5805 5825}"

# --- Helper Functions ---
# Write a prefixed message to the channel-election journal tag.
log() {
    manet_journal channel-election "CHAN-ELECTION: $*"
}

# --- Main Logic ---

# The agreement service holds the channel lock while taking its snapshot.
(
    log "--- Starting Channel Election ---"
    load_mesh_roles
    if ! acs_configs_ready; then
        log "No enabled ACS radio with ready configs; cannot run channel election."
        exit 1
    fi

    # Check for jq
    if ! command -v jq &>/dev/null; then
        log "ERROR: 'jq' command not found. Please install it (apt install jq). Exiting."
        exit 1
    fi

    # Check for registry
    if [ ! -f "$REGISTRY_FILE" ]; then
        log "Registry file not found, cannot run election. Exiting."
        exit 1
    fi

    NOW=$(date +%s)
    read -r UPTIME_NOW _ < "${MESH_UPTIME_FILE:-/proc/uptime}"
    UPTIME_NOW=${UPTIME_NOW%.*}

    # Aggregate active scan reports, one per line. Key by NODE_<mac> in $1
    # and pair report/timestamp in END: CHANNEL_REPORT_JSON precedes
    # OBSERVED_AT_UPTIME, and $2 is empty with this field separator.
    # Parse each payload separately so malformed JSON discards only that report.
    FRESH_REPORTS=$(awk -F"['=]" \
        -v now="$NOW" -v uptime="$UPTIME_NOW" -v stale="$STALE_THRESHOLD" -v members="${ACS_MEMBERS-}" \
        'BEGIN { n=split(members, list, " "); for (i=1; i<=n; i++) allowed[list[i]]=1 }
         /_OBSERVED_AT_UPTIME=/ { k=$1; sub(/_OBSERVED_AT_UPTIME$/, "", k); if ($3 ~ /^[0-9]+$/) seen[k]=uptime - $3 }
         /_CHANNEL_REPORT_JSON=/ { k=$1; sub(/_CHANNEL_REPORT_JSON$/, "", k); rpt[k]=$3 }
         END{
             for (k in rpt)
                 if ((members == "" || (k in allowed)) && rpt[k] != "" && (k in seen) &&
                     seen[k] >= 0 && seen[k] < stale)
                     print rpt[k]
         }' "$REGISTRY_FILE")

    ALL_REPORTS_JSON=$(printf '%s\n' "$FRESH_REPORTS" | jq -Rn '[inputs | fromjson? // empty]')
    REPORT_COUNT=$(printf '%s' "$ALL_REPORTS_JSON" | jq 'length')

    if [ "${REPORT_COUNT:-0}" -eq 0 ]; then
        log "No usable scan reports in registry. Holding current channels."
    else
        log "Scoring $REPORT_COUNT scan report(s)."
    fi

    # Initialize Limp Mode variable
    LIMP_MODE_NEEDED="false"

    # Aggregate each band in one jq pass, emitting one line per channel.
    # Output: <chan> nodata, or fields in this order:
    # <chan> <ok|dq> <score> <reporters> <median_noise> <median_busy>
    # <mean_bss> <bad>/<needed>
    score_band() {
        local chans_json dfs_json
        chans_json=$(printf '%s\n' "$1" | jq -Rc 'split(" ") | map(select(length > 0) | tonumber)')
        dfs_json=$(printf '%s\n' "${ACS_DFS_CHANNELS-}" | jq -Rc 'split(" ") | map(select(length > 0) | tonumber)')

        # Use median noise/busy values and mean BSS count per candidate MHz.
        # Unknown busy uses the incumbent's median, or the highest measured
        # candidate median when it is unmeasured. Round scores to two decimals.
        printf '%s' "$ALL_REPORTS_JSON" | jq -r \
            --argjson chans "$chans_json" \
            --argjson dfs "$dfs_json" \
            --arg current "$2" \
            --argjson noise_dq "$NOISE_DISQUALIFY_THRESHOLD_DBM" \
            --argjson busy_dq "$OCCUPANCY_DISQUALIFY_PCT" \
            --argjson quorum "$DISQUALIFY_QUORUM" \
            --argjson noise_ref "$NOISE_REFERENCE_DBM" \
            --argjson noise_w "$NOISE_WEIGHT" \
            --argjson bss_w "$BSS_WEIGHT" '
            def median:
                if length == 0 then null
                else sort as $a | ($a | length) as $n
                | if $n % 2 == 1 then $a[($n - 1) / 2]
                  else ($a[$n / 2 - 1] + $a[$n / 2]) / 2 end
                end;
            def round2: (. * 100 | round) / 100;

            [ .[]? | .results? // [] | .[]? | select(type == "object") ] as $all
            # Total home busy includes our traffic. Only foreign airtime is
            # an incumbent score. Other home entries are not candidate visits.
            | [ $all[]
                | if .channel == ($current | tonumber? // null)
                  then select((has("rf_trouble") | not)
                              or (.foreign_pct | type) == "number")
                       | .busy_pct = .foreign_pct
                  else select((has("foreign_pct") or has("rf_trouble"))
                              | not) end
                # A DFS candidate needs an actual passive occupancy sample.
                # Noise/BSS-only entries must not invent a usable channel.
                | .channel as $c
                | select(($dfs | index($c)) == null
                         or ((.busy_pct | type) == "number"
                             and .busy_pct >= 0 and .busy_pct <= 100)) ] as $all
            | ([ $all[] | select(.channel == ($current | tonumber?))
                 | .busy_pct | numbers ] | median) as $home_busy
            | ($home_busy // ([ $chans[] as $candidate
                 | [ $all[] | select(.channel == $candidate)
                     | .busy_pct | numbers ] | median
                 | numbers ] | max) // 0) as $unknown_busy
            | $chans[] as $c
            | [ $all[] | select(.channel == $c) ] as $s
            | if ($s | length) == 0 then "\($c) nodata"
              else
                ($s | length) as $n
                # Median, not max: the worst single radio no longer speaks for
                # the mesh. Disqualification is a separate quorum vote below.
                | ([ $s[] | .noise_floor | numbers ] | median) as $noise
                # busy_pct is absent on reports from a node that could not
                # measure it (too short a dwell, or an older build), so it is
                # filtered out rather than counted as zero.
                | ([ $s[] | .busy_pct | numbers ] | median) as $busy
                | (([ $s[] | .bss_count | numbers ] | add // 0) / $n) as $bss
                | ([ $s[]
                     | select(((.noise_floor // -100) > $noise_dq)
                              or ((.busy_pct // 0) > $busy_dq)) ] | length) as $bad
                | ([ 1, (($n * $quorum) | ceil) ] | max) as $need
                | ((if ($noise // -100) > $noise_ref
                    then (($noise // -100) - $noise_ref) else 0 end) * $noise_w
                   + ($bss * $bss_w)
                   + ($busy // $unknown_busy)) as $score
                | "\($c) \(if $bad >= $need then "dq" else "ok" end) \($score | round2) \($n) \($noise // "-") \($busy // "-") \($bss | round2) \($bad)/\($need)"
              end
            '
    }

    # Print ranked "<chan> <effective_score> <raw_score>" lines. want=ok uses
    # qualified channels, want=any uses all measured channels. Both apply
    # hysteresis. Break ties by lower frequency so nodes agree.
    pick_best() {
        awk -v want="$1" -v cur="$2" -v bias="$3" '
            ($2 == want) || (want == "any" && $2 != "nodata") {
                s = $3 + 0
                if ($1 + 0 == cur + 0) s -= bias
                if (best == "" || s < best_s || (s == best_s && $1 + 0 < best + 0)) {
                    best = $1; best_s = s; best_raw = $3 + 0
                }
            }
            END { if (best != "") printf "%s %.2f %.2f\n", best, best_s, best_raw }
        '
    }

    # Function to score a band and find the winner
    find_best_channel() {
        local band_channels="$1"
        local current_channel="$2"
        local band_name="$3"
        # The last two arguments name the winner and urgency outputs.
        # Poor scores also set LIMP_MODE_NEEDED for the final proposal.
        local -n _winner=$4
        local -n _urgent=$5
        local lines chan status rest best_ok best_any
        local pick_chan pick_eff pick_raw

        lines=$(score_band "$band_channels" "$current_channel")

        # Only a measured, qualified incumbent at or below the limp threshold
        # can wait through the coordinator's preference hold-down.
        _urgent=true
        if awk -v cur="$current_channel" -v t="$LIMP_MODE_SCORE_THRESHOLD" '
            $1 == cur && $2 == "ok" && $3 <= t && $6 != "-" { keep = 1 }
            END { exit !keep }
        ' <<< "$lines"; then
            _urgent=false
        fi

        while read -r chan status rest; do
            [ -z "$chan" ] && continue
            if [ "$status" = "nodata" ]; then
                log "[$band_name] $chan: no scan data"
            else
                log "[$band_name] $chan: $status score/nodes/noise/busy/bss/veto = $rest"
            fi
        done <<< "$lines"

        best_ok=$(printf '%s\n' "$lines" | pick_best ok "$current_channel" "$CHANNEL_BIAS_SCORE")
        best_any=$(printf '%s\n' "$lines" | pick_best any "$current_channel" "$CHANNEL_BIAS_SCORE")

        # An empty qualified list can mean disqualification or missing data.
        # Only measured RF conditions justify a channel or limp-mode change.
        if ! awk -v cur="$current_channel" '
            $1 != cur && $2 != "nodata" { measured = 1 }
            END { exit !measured }
        ' <<< "$lines"; then
            # Hold the channel without asserting limp mode when data is missing.
            # An absent radio, rejected scan or Alfred outage can leave no data.
            # Retry the election next cycle.
            log "[$band_name] No scan data for any candidate channel. Holding ${current_channel:-current channel}, not asserting limp mode."
            _winner="$current_channel"
            _urgent=false
            return 0
        fi

        if [ -n "$best_ok" ]; then
            read -r pick_chan pick_eff pick_raw <<< "$best_ok"
            # Use the raw score for limp mode. Hysteresis prevents migrations
            # and must not hide poor RF conditions.
            if awk -v s="$pick_raw" -v t="$LIMP_MODE_SCORE_THRESHOLD" 'BEGIN { exit !(s > t) }'; then
                log "[$band_name] Best channel $pick_chan still scores $pick_raw (worse than $LIMP_MODE_SCORE_THRESHOLD). Taking it and asserting limp mode."
                LIMP_MODE_NEEDED="true"
            else
                log "[$band_name] Winner is $pick_chan (score $pick_raw, $pick_eff after bias)"
            fi
            _winner="$pick_chan"
            return 0
        fi

        # All measured candidates are disqualified. Choose the best measured
        # channel and assert limp mode, keeping scans and elections active.
        read -r pick_chan pick_eff pick_raw <<< "$best_any"
        log "[$band_name] ALL CHANNELS DISQUALIFIED. Electing least-bad measured channel $pick_chan (score $pick_raw) and asserting limp mode."
        LIMP_MODE_NEEDED="true"
        _winner="$pick_chan"
    }

    # --- Get Current State ---
    CURRENT_2_4=${ACS_CURRENT_2_4-$(get_current_freq "$WPA_CONF_2_4")}
    CURRENT_5_0=${ACS_CURRENT_5_0-$(get_current_freq "$WPA_CONF_5_0")}
    log "Current channels: 2.4G=${CURRENT_2_4:-none}, 5.0G=${CURRENT_5_0:-none}"

    # --- Run Elections ---
    find_best_channel "$CHANNELS_2_4" "$CURRENT_2_4" "2.4GHz" \
        WINNER_2_4 URGENT_2_4
    find_best_channel "$CHANNELS_5_0" "$CURRENT_5_0" "5.0GHz" \
        WINNER_5_0 URGENT_5_0

    # --- Write Output File (for node-manager) ---
    cat > "$OUTPUT_FILE" <<- EOF
		WINNER_2_4=$WINNER_2_4
		WINNER_5_0=$WINNER_5_0
		URGENT_2_4=$URGENT_2_4
		URGENT_5_0=$URGENT_5_0
		LIMP_MODE=$LIMP_MODE_NEEDED
	EOF

    log "--- Proposal Scoring Complete ---"
)
