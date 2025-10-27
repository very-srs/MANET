#!/usr/bin/env bash
# mtx-ip.sh
# Deterministically derive a MediaMTX IPv6 VIP within the first /64
# subnet of a given ULA prefix (e.g., /48 or /64).

set -euo pipefail

# Input: Find the first IPv6 prefix line in radvd config
PREFIX_LINE=$(grep -m 1 '^[[:space:]]*prefix[[:space:]]\+fd[0-9a-fA-F:]\+/[0-9]\+' /etc/radvd-mesh.conf || echo "")
PREFIX_CIDR=$(echo "$PREFIX_LINE" | awk '{print $2}') # e.g., fd01:ed20:ecb4::/48

if [ -z "$PREFIX_CIDR" ]; then
    echo "Error: No valid IPv6 ULA prefix found in /etc/radvd-mesh.conf." >&2
    exit 1
fi

# Normalize prefix: strip CIDR length, ensure we have the first 4 hextets.
PREFIX=${PREFIX_CIDR%/*} # Remove /XX suffix
PREFIX=$(echo "$PREFIX" | sed 's/::$//; s/:$//') # Remove trailing :: or :

# Basic validation: must contain colons and start with 'fd' (ULA)
if ! [[ "$PREFIX" =~ ^fd && "$PREFIX" =~ : ]]; then
    echo "Error: '$PREFIX_CIDR' does not contain a valid IPv6 ULA prefix starting with 'fd'." >&2
    exit 1
fi

# --- Expand compressed prefix to get exactly 4 hextets ---
IFS=':' read -r -a HEXTETS <<< "$PREFIX"
EXPANDED_PREFIX_ARRAY=()
SEEN_EMPTY=false

for hextet in "${HEXTETS[@]}"; do
    if [[ -z "$hextet" && "$SEEN_EMPTY" = false ]]; then
        # Found the '::' compression
        SEEN_EMPTY=true
        # Calculate how many zero hextets to insert to reach 4 total
        MISSING=$((4 - ${#HEXTETS[@]} + 1)) # +1 because the empty string counts as one hextet
        for ((j=0; j<MISSING; j++)); do
            EXPANDED_PREFIX_ARRAY+=("0000")
        done
    elif [[ -n "$hextet" ]]; then
        # Add non-empty hextet, padding with leading zeros
        EXPANDED_PREFIX_ARRAY+=("$(printf "%04x" "0x${hextet}")")
    fi
    # Stop processing if we already have 4 hextets (handles cases like fdXX::/48)
    if [[ ${#EXPANDED_PREFIX_ARRAY[@]} -ge 4 ]]; then
        break
    fi
done

# Ensure we have exactly 4 hextets for the /64 base
while [[ ${#EXPANDED_PREFIX_ARRAY[@]} -lt 4 ]]; do
    EXPANDED_PREFIX_ARRAY+=("0000")
done

# Join the first 4 hextets back into a string
EXPANDED_PREFIX=$(IFS=: ; echo "${EXPANDED_PREFIX_ARRAY[*]}")

# Final Validation: Check the resulting /64 prefix structure
if ! [[ "$EXPANDED_PREFIX" =~ ^fd[0-9a-fA-F]{2}:[0-9a-fA-F]{4}:[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$ ]]; then
    echo "Error: Failed to normalize '$PREFIX_CIDR' into a valid /64 ULA prefix. Result: '$EXPANDED_PREFIX'." >&2
    exit 1
fi

# --- Suffix Generation (remains the same) ---
# Hash the normalized /64 prefix to derive a deterministic suffix
if command -v md5sum >/dev/null; then
    HASH_BYTE=$(echo -n "${EXPANDED_PREFIX}-mediamtx" | md5sum | cut -c1-2)
elif command -v sha256sum >/dev/null; then
    HASH_BYTE=$(echo -n "${EXPANDED_PREFIX}-mediamtx" | sha256sum | cut -c1-2)
else
    echo "Error: Neither md5sum nor sha256sum found. Please install one." >&2
    exit 1
fi

# Convert hash byte to decimal offset (100-511)
OFFSET=$(( 0x$HASH_BYTE % 412 + 100 ))

# Form the VIP: normalized prefix + :: + offset (in hex)
VIP="${EXPANDED_PREFIX}::$(printf "%x" "$OFFSET")"

# Output the final address with /128 mask for assignment
echo "$VIP/128"
