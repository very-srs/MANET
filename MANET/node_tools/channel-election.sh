#!/bin/bash
# Channel Election Manager
# This script requests an agreed channel change, or scores a proposal for the
# coordinator (--score). If every channel is terrible it elects the
# least-bad one it actually measured and asserts limp mode.

set -eo pipefail

# Scoring never changes a radio. Normal calls make this round eligible for the
# agreement service, which owns proposal, ACK, activation and recovery.
if [ "${1:-}" != "--score" ]; then
    [ "$#" -eq 0 ] || exit 2
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
STALE_THRESHOLD=240 # (4 minutes) Ignore scan reports older than this (scans are every 3 min)

# Channel score, lower is better:
#
#   occupancy% + max(0, median_noise - NOISE_REFERENCE_DBM) * NOISE_WEIGHT
#              + mean_bss_count * BSS_WEIGHT
#
# Occupancy leads because it is the only input that means the same thing on
# every node. It is the ratio of two counters from the same driver over the
# same scan, so it needs no calibration and survives a chip revision, and it
# measures the thing we actually care about: how much of the air is already
# spoken for. The noise floor is driver-derived and uncalibrated in absolute
# terms, so it contributes a capped penalty instead of deciding the election
# on its own. The BSS count is a mean per reporting node, not a sum, so a
# channel is not scored worse for having been measured by more nodes.
NOISE_REFERENCE_DBM=-95   # noise at or below this adds nothing
NOISE_WEIGHT=0.5          # -70 dBm, the disqualification edge, therefore costs 12.5
BSS_WEIGHT=0.5            # a co-channel network costs half a point of occupancy

# One node calls a channel bad if it is over either of these.
NOISE_DISQUALIFY_THRESHOLD_DBM=-70
OCCUPANCY_DISQUALIFY_PCT=85

# ...but one node cannot take a channel away from the whole mesh once there
# are enough reporters to outvote it. A bad connector, a radio parked next to
# a microwave, or one driver reporting garbage used to disqualify a channel
# for everybody, and with only two candidates at 2.4 GHz that costs the mesh
# a whole band. This fraction of the nodes reporting on a channel, rounded up
# and never less than one, must agree: 1 of 1, 1 of 2, 2 of 3, 2 of 5, 3 of 6.
DISQUALIFY_QUORUM=0.34

# Hysteresis: a challenger must beat the channel we are already on by this
# much score before we pay for a migration.
CHANNEL_BIAS_SCORE=10

# If the best channel in a band still scores worse than this, the band is not
# usable and the mesh drops to legacy bitrates. In the units above that reads
# as "the quietest channel available is still about two thirds occupied",
# which is a figure the election can actually reach. The previous dBm-based
# threshold could not: disqualification capped surviving channels at -70 dBm,
# so crossing -60 needed more than 100 co-channel BSSes summed across the
# mesh, and the branch that logged JAMMING DETECTED never fired in the field.
LIMP_MODE_SCORE_THRESHOLD=60

# List of channels this mesh is allowed to use. The lobby frequencies (2412 /
# 5180) are deliberately absent: if the election landed on the lobby pair,
# every node would flip into lobby state (is_in_lobby checks frequencies) and
# scanning, elections and tourguide duty would silently stop. The lobby is a
# rendezvous point, not a destination this script can choose -- see the
# ALL CHANNELS DISQUALIFIED branch in find_best_channel.
#
# The coordinator supplies candidate capabilities and a shared incumbent.
# Receivers ACK this frozen result; they never re-score their own registry.
CHANNELS_2_4="${ACS_CHANNELS_2_4-2437 2462}"
CHANNELS_5_0="${ACS_CHANNELS_5_0-5200 5220 5240 5745 5765 5785 5805 5825}"

# --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - CHAN-ELECTION: $1" | systemd-cat -t channel-election
}

# Get the currently configured frequency for an interface
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

    # 1. Aggregate all *active* scan reports from the registry, one per line.
    #
    # Key by the NODE_<mac> prefix in $1 and pair report/timestamp in END:
    # the registry writes CHANNEL_REPORT_JSON before LAST_SEEN_TIMESTAMP, so
    # a single pass keyed on $2 (always empty with this FS) dropped the first
    # node's report and matched each report against the previous node's age.
    #
    # The lines are then parsed one at a time. Assembling the JSON array in
    # awk meant one truncated or malformed payload made the whole document
    # invalid and took the election out with it. Now a bad line is dropped and
    # the rest of the mesh still votes.
    FRESH_REPORTS=$(awk -F"['=]" \
        -v now="$NOW" -v stale="$STALE_THRESHOLD" -v members="${ACS_MEMBERS-}" \
        'BEGIN { n=split(members, list, " "); for (i=1; i<=n; i++) allowed[list[i]]=1 }
         /_LAST_SEEN_TIMESTAMP=/ { k=$1; sub(/_LAST_SEEN_TIMESTAMP$/, "", k); ts[k]=$3 }
         /_CHANNEL_REPORT_JSON=/ { k=$1; sub(/_CHANNEL_REPORT_JSON$/, "", k); rpt[k]=$3 }
         END{
             for (k in rpt)
                 if ((members == "" || (k in allowed)) && rpt[k] != "" && (k in ts) &&
                     (now - ts[k]) >= -5 && (now - ts[k]) < stale)
                     print rpt[k]
         }' "$REGISTRY_FILE")

    ALL_REPORTS_JSON=$(printf '%s\n' "$FRESH_REPORTS" | jq -Rn '[inputs | fromjson? // empty]')
    REPORT_COUNT=$(printf '%s' "$ALL_REPORTS_JSON" | jq 'length')

    if [ "${REPORT_COUNT:-0}" -eq 0 ]; then
        log "No usable scan reports in registry. Exiting."
        exit 0
    fi
    log "Scoring $REPORT_COUNT scan report(s)."

    # Initialize Limp Mode variable
    LIMP_MODE_NEEDED="false"

    # Aggregate every node's report for one band and emit one line per
    # candidate channel:
    #
    #   <chan> nodata
    #   <chan> <ok|dq> <score> <reporters> <median_noise> <median_busy> <mean_bss> <bad>/<needed>
    #
    # All of it in a single jq pass. The previous version shelled out to bc
    # once per channel per comparison, which made the arithmetic dependent on
    # every operand being non-empty: one malformed report and the election
    # died inside the flock subshell with nothing but an exit status to show.
    score_band() {
        local chans_json
        chans_json=$(printf '%s\n' "$1" | jq -Rc 'split(" ") | map(select(length > 0) | tonumber)')

        printf '%s' "$ALL_REPORTS_JSON" | jq -r \
            --argjson chans "$chans_json" \
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
                   + ($busy // 0)) as $score
                | "\($c) \(if $bad >= $need then "dq" else "ok" end) \($score | round2) \($n) \($noise // "-") \($busy // "-") \($bss | round2) \($bad)/\($need)"
              end
            '
    }

    # Rank scored lines and print "<chan> <effective_score> <raw_score>".
    # want=ok considers only qualified channels, want=any every channel that
    # was measured at all. Hysteresis applies in both: it matters more, not
    # less, when the band is in trouble and every candidate looks similar.
    # Ties break on the lower frequency so all nodes reach the same answer.
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
        local -n _winner=$4
        local lines chan status rest best_ok best_any
        local pick_chan pick_eff pick_raw

        lines=$(score_band "$band_channels")

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

        # Distinguishes "every candidate was measured and rejected" (a real RF
        # verdict) from "nothing reported any measurement at all" (an outage).
        # Both leave the qualified list empty; they must not be handled the
        # same way.
        if [ -z "$best_any" ]; then
            # Not an RF verdict -- no node reported a usable measurement for any
            # candidate on this band. Causes seen in practice: the radio for this
            # band is absent (so its scan report carries no entries for it), the
            # scan request was rejected wholesale, or alfred was silently down so
            # no reports replicated. Dropping to lobby and asserting limp mode
            # here throttles the whole mesh to legacy bitrates on the strength of
            # missing data. Hold the current channel and change nothing; the next
            # cycle re-elects once measurements come back.
            log "[$band_name] No scan data for any candidate channel. Holding ${current_channel:-current channel}, not asserting limp mode."
            _winner="$current_channel"
            return 0
        fi

        if [ -n "$best_ok" ]; then
            read -r pick_chan pick_eff pick_raw <<< "$best_ok"
            # The raw score, not the biased one: the hysteresis discount exists
            # to stop migrations, and letting it also suppress limp mode would
            # hide a band going bad underneath us for as long as we sat on it.
            if awk -v s="$pick_raw" -v t="$LIMP_MODE_SCORE_THRESHOLD" 'BEGIN { exit !(s > t) }'; then
                log "[$band_name] Best channel $pick_chan still scores $pick_raw (worse than $LIMP_MODE_SCORE_THRESHOLD). Taking it and asserting limp mode."
                LIMP_MODE_NEEDED="true"
            else
                log "[$band_name] Winner is $pick_chan (score $pick_raw, $pick_eff after bias)"
            fi
            _winner="$pick_chan"
            return 0
        fi

        # Every measured candidate is over a disqualification threshold.
        #
        # This used to move the whole mesh to the lobby pair, which is the one
        # move that cannot help: those two frequencies are hardcoded, published
        # in a public repo, never scanned, and every node converges on them by
        # design, so the response to being jammed was to go somewhere with no
        # measurements at all and complete predictability. It was also a
        # one-way door -- landing there makes is_in_lobby true, which stops
        # scanning and elections, and the lobby bootstrap only runs with a
        # batman peer in sight, so a jammed and isolated node parked there for
        # good.
        #
        # Elect the least-bad channel we actually measured instead. It is not a
        # good channel and limp mode says so, but it is a measured one, and the
        # node keeps scanning and re-electing from it every cycle.
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
    find_best_channel "$CHANNELS_2_4" "$CURRENT_2_4" "2.4GHz" WINNER_2_4
    find_best_channel "$CHANNELS_5_0" "$CURRENT_5_0" "5.0GHz" WINNER_5_0

    # --- Write Output File (for node-manager) ---
    cat > "$OUTPUT_FILE" <<- EOF
		WINNER_2_4=$WINNER_2_4
		WINNER_5_0=$WINNER_5_0
		LIMP_MODE=$LIMP_MODE_NEEDED
	EOF

    log "--- Proposal Scoring Complete ---"
)
