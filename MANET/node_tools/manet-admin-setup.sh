#!/bin/bash
# Install the admin transport dependency on nodes receiving a tools update.
# Failure disables admin control while the public status page remains usable.
set -e

# Also runs from a verified staging directory on the first tools update.
# Apply before the dependency check, even when cryptography is already present.
python3 "$(dirname "$(readlink -f "$0")")/manet-kernel-protect.py"

if python3 -c 'from cryptography.hazmat.primitives.ciphers.aead import AESGCM; from cryptography.hazmat.primitives.kdf.scrypt import Scrypt' 2>/dev/null; then
    exit 0
fi

export DEBIAN_FRONTEND=noninteractive
# Try cached package indexes first, then refresh them for one install retry.
if ! apt-get install -y --no-install-recommends \
    -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold python3-cryptography; then
    apt-get update
    apt-get install -y --no-install-recommends \
        -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold python3-cryptography
fi
python3 -c 'from cryptography.hazmat.primitives.ciphers.aead import AESGCM; from cryptography.hazmat.primitives.kdf.scrypt import Scrypt'
