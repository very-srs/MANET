#!/bin/bash
# mtx-ip.sh
# Deterministically derive a MediaMTX IPv6 VIP within the first /64
# subnet of a given ULA prefix (e.g., /48 or /64).

set -euo pipefail

# Input: Find the first IPv6 prefix line in radvd config
PREFIX_LINE=$(grep -m 1 '^[[:space:]]*prefix[[:space:]]\+fd[0-9a-fA-F:]\+/[0-9]\+' /etc/radvd-mesh.conf || echo "")
PREFIX_CIDR=$(printf '%s\n' "$PREFIX_LINE" |
    awk '{print $2}') # e.g., fd01:ed20:ecb4::/48

if [ -z "$PREFIX_CIDR" ]; then
    echo "Error: No valid IPv6 ULA prefix found in /etc/radvd-mesh.conf." >&2
    exit 1
fi

# Normalize the prefix to its first four hextets without the CIDR length.
PREFIX=${PREFIX_CIDR%/*} # Remove /XX suffix
PREFIX=$(printf '%s\n' "$PREFIX" |
    sed 's/::$//; s/:$//') # Remove trailing :: or :

# Basic validation: must contain colons and start with 'fd' (ULA)
if ! [[ "$PREFIX" =~ ^fd && "$PREFIX" =~ : ]]; then
    printf '%s%s\n' \
        "Error: '$PREFIX_CIDR' does not contain a valid IPv6 ULA prefix " \
        "starting with 'fd'." >&2
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
        MISSING=$((4 - ${#HEXTETS[@]} + 1)) # The empty hextet counts as one.
        for ((j=0; j<MISSING; j++)); do
            EXPANDED_PREFIX_ARRAY+=("0000")
        done
    elif [[ -n "$hextet" ]]; then
        # Add non-empty hextet, padding with leading zeros
        EXPANDED_PREFIX_ARRAY+=("$(printf "%04x" "0x${hextet}")")
    fi
    # Stop at four hextets, including fdXX::/48 prefixes.
    if [[ ${#EXPANDED_PREFIX_ARRAY[@]} -ge 4 ]]; then
        break
    fi
done

# Ensure we have exactly 4 hextets for the /64 base
while [[ ${#EXPANDED_PREFIX_ARRAY[@]} -lt 4 ]]; do
    EXPANDED_PREFIX_ARRAY+=("0000")
done

# Join the first 4 hextets back into a string
EXPANDED_PREFIX=$(IFS=: ; printf '%s\n' "${EXPANDED_PREFIX_ARRAY[*]}")


# Final Validation: Check the resulting /64 prefix structure
if ! [[ "$EXPANDED_PREFIX" =~ ^fd[0-9a-fA-F]{2}:[0-9a-fA-F]{4}:[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$ ]]; then
    printf '%s%s\n' \
        "Error: Failed to normalize '$PREFIX_CIDR' into a valid /64 ULA " \
        "prefix. Result: '$EXPANDED_PREFIX'." >&2
    exit 1
fi

# Hash the normalized /64 prefix into a deterministic suffix.
if command -v md5sum >/dev/null; then
    HASH_BYTE=$(printf '%s' "${EXPANDED_PREFIX}-mediamtx" | md5sum | cut -c1-2)
elif command -v sha256sum >/dev/null; then
    HASH_BYTE=$(printf '%s' "${EXPANDED_PREFIX}-mediamtx" |
        sha256sum | cut -c1-2)
else
    echo "Error: Neither md5sum nor sha256sum found. Please install one." >&2
    exit 1
fi

# One hash byte gives offsets 100-355. mumble-ip.sh uses 400-655.
OFFSET=$(( 0x$HASH_BYTE + 100 ))

# Form the VIP: normalized prefix + :: + offset (in hex)
VIP="${EXPANDED_PREFIX}::$(printf "%x" "$OFFSET")"


# Output the final address with /128 mask for assignment
printf '%s\n' "$VIP/128"
