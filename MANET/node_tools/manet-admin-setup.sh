#!/bin/bash
# Install the admin transport dependency on nodes receiving a tools update.
# Failure leaves admin control disabled; the public status page still works.
set -e

if python3 -c 'from cryptography.hazmat.primitives.ciphers.aead import AESGCM; from cryptography.hazmat.primitives.kdf.scrypt import Scrypt' 2>/dev/null; then
    exit 0
fi

export DEBIAN_FRONTEND=noninteractive
if ! apt-get install -y --no-install-recommends python3-cryptography; then
    apt-get update
    apt-get install -y --no-install-recommends python3-cryptography
fi
python3 -c 'from cryptography.hazmat.primitives.ciphers.aead import AESGCM; from cryptography.hazmat.primitives.kdf.scrypt import Scrypt'
