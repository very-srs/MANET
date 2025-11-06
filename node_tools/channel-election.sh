#!/bin/bash
# ==============================================================================
# Channel Election Manager
# ==============================================================================
# This script performs a decentralized, deterministic election for the best
# 2.4GHz and 5GHz channels. If all channels are terrible, falls back to lobby.
# ==============================================================================

set -eo pipefail

# --- Dry Run Mode (set to true for testing) ---
DRY_RUN=false

# --- Configuration ---
REGISTRY_FILE="/var/run/mesh_node_registry"
OUTPUT_FILE="/var/run/mesh_channel_election"
LOCK_FILE="/var/run/channel-election.lock"
WPA_CONF_2_4="/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"
WPA_CONF_5_0="/etc/wpa_supplicant/wpa_supplicant-wlan1.conf"

# --- Tunable Parameters ---
STALE_THRESHOLD=240 # (4 minutes) Ignore scan reports older than this (scans are every 3 min)
NOISE_DISQUALIFY_THRESHOLD_DBM=-70 # Disqualify channel if ANY node reports noise worse than this
CHANNEL_BIAS_DB=10 # A new channel must be at least this much quieter (in dB) to trigger a move
LIMP_MODE_SCORE_THRESHOLD=110 # If the *best* channel's score is worse than this, trigger limp mode

# Lobby channels
LOBBY_FREQ_2_4=2412
LOBBY_FREQ_5_0=5180

# List of channels this mesh is allowed to use
CHANNELS_2_4="2412 2437 2462"
CHANNELS_5_0="5180 5200 5220 5240 5745 5765 5785 5805 5825"

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

# --- Main Logic ---

# Use flock to ensure this script only runs once
(
    flock -n 9 || { log "Channel election already in progress. Exiting."; exit 1; }
    log "--- Starting Channel Election ---"

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

    # 1. Aggregate all *active* scan reports from the registry
    ALL_REPORTS_JSON=$(awk -F"['=]" \
        -v now="$NOW" -v stale="$STALE_THRESHOLD" \
        'BEGIN{print "["}
         /LAST_SEEN_TIMESTAMP/ { mac=$2; timestamps[mac]=$3 }
         /CHANNEL_REPORT_JSON/ {
             mac=$2;
             if ( (mac in timestamps) && (now - timestamps[mac]) < stale) {
                 if (count > 0) print ",";
                 print $3;
                 count++
             }
         }
         END{print "]"}' "$REGISTRY_FILE")

    if [ "$(echo "$ALL_REPORTS_JSON" | jq 'length')" -eq 0 ]; then
        log "No active scan reports in registry. Exiting."
        exit 0
    fi

    # Initialize Limp Mode variable
    LIMP_MODE_NEEDED="false"

    # Function to score a band and find the winner
    find_best_channel() {
        local band_channels="$1"
        local current_channel="$2"
        local band_name="$3"
        local winner_channel=""
        local winner_score=1000 # Lower is better

        local candidates=()

        for chan in $band_channels; do
            local stats=$(echo "$ALL_REPORTS_JSON" | jq -r --argjson c "$chan" '
                [
                    flatten | .[] | .results | .[] |
                    select(.channel == $c) |
                    {noise: .noise_floor, bss: .bss_count}
                ] |
                if length > 0 then
                    {
                        "max_noise": (map(.noise) | max),
                        "avg_noise": (map(.noise) | add / length),
                        "total_bss": (map(.bss) | add)
                    }
                else
                    null
                end
            ')

            if [ "$stats" == "null" ]; then
                log "[$band_name] No scan data for channel $chan"
                continue
            fi

            local max_noise=$(echo "$stats" | jq '.max_noise')
            local avg_noise=$(echo "$stats" | jq '.avg_noise')
            local total_bss=$(echo "$stats" | jq '.total_bss')

            # --- Filter Disqualified Channels ---
            if (( $(echo "$max_noise > $NOISE_DISQUALIFY_THRESHOLD_DBM" | bc -l) )); then
                log "[$band_name] Channel $chan disqualified (max_noise ${max_noise}dBm)"
                continue
            fi

            # --- Calculate Final Score ---
            local score=$(echo "($avg_noise * -1) + ($total_bss * 0.1)" | bc -l)

            # --- Apply Bias ---
            if [ "$chan" == "$current_channel" ]; then
                score=$(echo "$score - $CHANNEL_BIAS_DB" | bc -l)
                log "[$band_name] Applying bias to current channel $chan. Score: $score"
            fi

            candidates+=("$(printf "%.2f %s\n" "$score" "$chan")")
        done

        # --- Sort and Pick Winner ---
        if [ ${#candidates[@]} -eq 0 ]; then
            log "[$band_name] ALL CHANNELS DISQUALIFIED. Falling back to lobby."
            LIMP_MODE_NEEDED="true"

            if [ "$band_name" == "2.4GHz" ]; then
                echo "$LOBBY_FREQ_2_4"
            else
                echo "$LOBBY_FREQ_5_0"
            fi
        else
            local winner_line=$(printf '%s\n' "${candidates[@]}" | sort -n -k1,1 -k2,2 | head -1)
            winner_channel=$(echo "$winner_line" | awk '{print $2}')
            winner_score=$(echo "$winner_line" | awk '{print $1}')

            # Check if the *best* channel is still terrible
            if (( $(echo "$winner_score > $LIMP_MODE_SCORE_THRESHOLD" | bc -l) )); then
                log "[$band_name] JAMMING DETECTED. Best channel $winner_channel has poor score ($winner_score). Falling back to lobby."
                LIMP_MODE_NEEDED="true"

                if [ "$band_name" == "2.4GHz" ]; then
                    echo "$LOBBY_FREQ_2_4"
                else
                    echo "$LOBBY_FREQ_5_0"
                fi
            else
                log "[$band_name] Winner is $winner_channel (Score: $winner_score)"
                echo "$winner_channel"
            fi
        fi
    }

    # --- Get Current State ---
    CURRENT_2_4=$(get_current_freq "$WPA_CONF_2_4")
    CURRENT_5_0=$(get_current_freq "$WPA_CONF_5_0")
    log "Current channels: 2.4G=${CURRENT_2_4:-none}, 5.0G=${CURRENT_5_0:-none}"

    # --- Run Elections ---
    WINNER_2_4=$(find_best_channel "$CHANNELS_2_4" "$CURRENT_2_4" "2.4GHz")
    WINNER_5_0=$(find_best_channel "$CHANNELS_5_0" "$CURRENT_5_0" "5.0GHz")

    # --- Write Output File (for node-manager) ---
    cat > "$OUTPUT_FILE" <<- EOF
		WINNER_2_4=$WINNER_2_4
		WINNER_5_0=$WINNER_5_0
		LIMP_MODE=$LIMP_MODE_NEEDED
	EOF

    # --- Act on Changes ---
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

    # Restart services *after* all configs are written
	if [ "$DRY_RUN" = false ]; then
    	if [ "$MIGRATION_2_4_NEEDED" = true ]; then
    	    log "Restarting wpa_supplicant@wlan0.service..."
    	    systemctl restart wpa_supplicant@wlan0.service
    	fi

    	if [ "$MIGRATION_5_0_NEEDED" = true ]; then
    	    log "Restarting wpa_supplicant@wlan1.service..."
    	    systemctl restart wpa_supplicant@wlan1.service
    	fi
	else
    	log "DRY RUN: Skipping service restarts"
	fi
   	log "--- Election Complete ---"

) 9>/var/run/channel-election.lock
