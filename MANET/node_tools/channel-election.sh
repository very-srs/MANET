#!/bin/bash
# ==============================================================================
# Channel Election Manager
# ==============================================================================
# This script performs a decentralized, deterministic election for the best
# 2.4GHz and 5GHz channels. If every channel is terrible it elects the
# least-bad one it actually measured and asserts limp mode.
# ==============================================================================

set -eo pipefail

# --- Dry Run Mode (set to true for testing) ---
DRY_RUN=false

# --- Configuration ---
REGISTRY_FILE="/var/run/mesh_node_registry"
OUTPUT_FILE="/var/run/mesh_channel_election"
LOCK_FILE="/var/run/channel-election.lock"
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
# These are deliberately NOT filtered against the local phy's capabilities here.
# The election is an implicit-consensus algorithm: every node must run the same
# computation over the same replicated inputs, so injecting a per-node hardware
# filter at this stage would let two nodes reach different answers from
# identical data. Unusable frequencies are excluded at scan time instead
# (phy_usable_freqs in node-manager-acs.sh), which keeps the exclusion visible
# in the replicated reports and therefore symmetric across the mesh.
CHANNELS_2_4="2437 2462"
CHANNELS_5_0="5200 5220 5240 5745 5765 5785 5805 5825"

# --- Helper Functions ---
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] - CHAN-ELECTION: $1" | systemd-cat -t channel-election
}

# Get the currently configured frequency for an interface
get_current_freq() {
    local conf_file=$1
    if [ -f "$conf_file" ]; then
        grep -oP 'frequency=\K[0-9]+' "$conf_file" | head -1
    else
        echo ""
    fi
}

radio_iface_enabled() {
    python3 - "$1" <<'PY'
import json, sys
iface = sys.argv[1]
try:
    with open('/var/lib/mesh_radio_state.json') as f:
        state = json.load(f).get('desired', {}).get(iface, 'up')
except Exception:
    state = 'up'
sys.exit(1 if state == 'down' else 0)
PY
}

load_mesh_roles() {
    local mesh_ifaces=()

    [ -f /var/lib/mesh_if ] && mapfile -t mesh_ifaces < /var/lib/mesh_if

    WPA_IFACE_2_4="$(cat /var/lib/mesh_24_if 2>/dev/null || true)"
    WPA_IFACE_5_0="$(cat /var/lib/mesh_5_if 2>/dev/null || true)"

    [ -z "$WPA_IFACE_2_4" ] && WPA_IFACE_2_4="${mesh_ifaces[0]:-}"
    [ -z "$WPA_IFACE_5_0" ] && WPA_IFACE_5_0="${mesh_ifaces[1]:-}"

    WPA_CONF_2_4="/etc/wpa_supplicant/wpa_supplicant-${WPA_IFACE_2_4}.conf"
    WPA_CONF_5_0="/etc/wpa_supplicant/wpa_supplicant-${WPA_IFACE_5_0}.conf"
}

# --- Main Logic ---

# Use flock to ensure this script only runs once
(
    flock -n 9 || { log "Channel election already in progress. Exiting."; exit 1; }
    log "--- Starting Channel Election ---"
    load_mesh_roles
    if [ -z "$WPA_IFACE_2_4" ] || [ -z "$WPA_IFACE_5_0" ]; then
        log "Mesh role files not ready; cannot run channel election."
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
        -v now="$NOW" -v stale="$STALE_THRESHOLD" \
        '/_LAST_SEEN_TIMESTAMP=/ { k=$1; sub(/_LAST_SEEN_TIMESTAMP$/, "", k); ts[k]=$3 }
         /_CHANNEL_REPORT_JSON=/ { k=$1; sub(/_CHANNEL_REPORT_JSON$/, "", k); rpt[k]=$3 }
         END{
             for (k in rpt)
                 if (rpt[k] != "" && (k in ts) && (now - ts[k]) < stale)
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
        chans_json=$(printf '%s' "$1" | jq -Rc 'split(" ") | map(select(length > 0) | tonumber)')

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
    CURRENT_2_4=$(get_current_freq "$WPA_CONF_2_4")
    CURRENT_5_0=$(get_current_freq "$WPA_CONF_5_0")
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

    # --- Act on Changes ---
    #
    # Apply a new frequency without tearing the supplicant down, and confirm
    # the radio landed there. This is the path tourguide-manager.sh already
    # uses for its lobby hops: "wpa_cli reconfigure" re-reads the conf in
    # place, so the mesh point is not destroyed and rebuilt and SAE does not
    # start over from scratch. Restarting the unit stays as the fallback for
    # when the supplicant is not answering -- and because we poll, we find
    # that out instead of assuming the move worked and sleeping on it.
    apply_frequency() {
        local iface=$1
        local freq=$2
        local landed=""
        local i

        if wpa_cli -i "$iface" reconfigure >/dev/null 2>&1; then
            for i in $(seq 1 20); do
                landed=$(iw dev "$iface" info 2>/dev/null | grep -oP 'channel.*\((\K[0-9]+)' || true)
                if [ "$landed" = "$freq" ]; then
                    log "$iface is on $freq"
                    return 0
                fi
                sleep 0.5
            done
            log "$iface did not reach $freq within 10s (currently ${landed:-unknown}); restarting supplicant"
        else
            log "wpa_cli reconfigure failed on $iface; restarting supplicant"
        fi

        systemctl restart "wpa_supplicant@${iface}.service"
    }

    MIGRATION_2_4_NEEDED=false
    MIGRATION_5_0_NEEDED=false

    if [[ -n "$WINNER_2_4" && "$WINNER_2_4" != "$CURRENT_2_4" ]]; then
        log ">>> MIGRATION: 2.4GHz channel changing: $CURRENT_2_4 -> $WINNER_2_4"

        if [ "$DRY_RUN" = false ]; then
            sed -i "s/frequency=.*/frequency=${WINNER_2_4}/" "$WPA_CONF_2_4"
            MIGRATION_2_4_NEEDED=true
        else
            log "DRY RUN: Would migrate 2.4GHz but not actually doing it"
        fi
    fi

    if [[ -n "$WINNER_5_0" && "$WINNER_5_0" != "$CURRENT_5_0" ]]; then
        log ">>> MIGRATION: 5.0GHz channel changing: $CURRENT_5_0 -> $WINNER_5_0"

        if [ "$DRY_RUN" = false ]; then
            sed -i "s/frequency=.*/frequency=${WINNER_5_0}/" "$WPA_CONF_5_0"
            MIGRATION_5_0_NEEDED=true
        else
            log "DRY RUN: Would migrate 5.0GHz but not actually doing it"
        fi
    fi

    # Reconfigure *after* all configs are written
    if [ "$DRY_RUN" = false ]; then
        if [ "$MIGRATION_2_4_NEEDED" = true ]; then
            if radio_iface_enabled "$WPA_IFACE_2_4"; then
                apply_frequency "$WPA_IFACE_2_4" "$WINNER_2_4"
            else
                log "Skipping reconfigure for ${WPA_IFACE_2_4}; radio-state says down"
            fi
        fi

        if [ "$MIGRATION_5_0_NEEDED" = true ]; then
            if radio_iface_enabled "$WPA_IFACE_5_0"; then
                apply_frequency "$WPA_IFACE_5_0" "$WINNER_5_0"
            else
                log "Skipping reconfigure for ${WPA_IFACE_5_0}; radio-state says down"
            fi
        fi
    else
        log "DRY RUN: Skipping supplicant reconfigure"
    fi
   	log "--- Election Complete ---"

) 9>/var/run/channel-election.lock
