#!/usr/bin/env bash
# mtx-ip.sh
# Deterministically derive a MediaMTX IPv6 VIP from a /64 ULA prefix

set -euo pipefail

# Input: IPv6 /64 prefix (e.g., fd5a:1753:4340:1::/64)
PREFIX=`grep prefix /etc/radvd-mesh.conf | awk '{print $2}'`

# Normalize prefix: strip /64 and trailing '::' or ':'
PREFIX=$(echo "$PREFIX" | sed 's#/64$##; s/::$//; s/:$//')

# Basic validation: must contain colons and resemble IPv6
if ! [[ "$PREFIX" =~ : ]]; then
  echo "Error: '$PREFIX' is not a valid IPv6 prefix." >&2
  exit 1
fi

# Expand compressed prefix (handle ::) and validate it's a /64 ULA
# Convert to array of hextets
IFS=':' read -r -a HEXTETS <<< "$PREFIX"
HEXTET_COUNT=${#HEXTETS[@]}

# Check for compression (::) and expand to 4 hextets
if [[ "$HEXTET_COUNT" -le 4 ]]; then
  if [[ "$PREFIX" =~ :: ]]; then
    # Calculate missing hextets to reach 4 (since /64 needs 4 hextets)
    MISSING=$((4 - HEXTET_COUNT + 1)) # +1 for the :: itself
    EXPANDED_PREFIX=""
    for ((i=0; i<HEXTET_COUNT; i++)); do
      if [[ "${HEXTETS[i]}" == "" && "$PREFIX" =~ :: ]]; then
        # Insert zeros for compression
        for ((j=0; j<MISSING; j++)); do
          EXPANDED_PREFIX="${EXPANDED_PREFIX}0:"
        done
      else
        # Pad hextet to 4 digits
        HEXTET=$(printf "%04x" "0x${HEXTETS[i]:-0}")
        EXPANDED_PREFIX="${EXPANDED_PREFIX}${HEXTET}:"
      fi
    done
    # Remove trailing colon
    EXPANDED_PREFIX=${EXPANDED_PREFIX%:}
  else
    EXPANDED_PREFIX="$PREFIX"
  fi
else
  echo "Error: '$PREFIX' does not form a valid /64 prefix (too many hextets)." >&2
  exit 1
fi

# Validate: must be exactly 4 hextets and start with 'fd' (ULA)
if ! [[ "$EXPANDED_PREFIX" =~ ^fd[0-9a-fA-F]{2}:[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}$ ]]; then
  echo "Error: Invalid IPv6 /64 ULA prefix '$PREFIX'. Expected format like 'fdXX:XXXX:XXXX:XXXX'." >&2
  exit 1
fi

# Hash the expanded prefix to derive a deterministic suffix
if command -v md5sum >/dev/null; then
  HASH_BYTE=$(echo -n "${EXPANDED_PREFIX}-mediamtx" | md5sum | cut -c1-2)
elif command -v sha256sum >/dev/null; then
  HASH_BYTE=$(echo -n "${EXPANDED_PREFIX}-mediamtx" | sha256sum | cut -c1-2)
else
  echo "Error: Neither md5sum nor sha256sum found. Please install one." >&2
  exit 1
fi

# Convert hash byte to decimal (100-511, hex 64-1ff, to avoid ::1 and reduce SLAAC collisions)
OFFSET=$(( 0x$HASH_BYTE % 412 + 100 )) # Maps to 0x64-0x1ff (100-511)

# Form the VIP: prefix + :: + offset (in hex)
VIP="${EXPANDED_PREFIX}::$(printf "%x" "$OFFSET")"


echo "$VIP/128"
