#!/usr/bin/env python3
"""Fetch and stage the pinned, unmodified MT7916 firmware set."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
stage = Path(sys.argv[1])
manifest = json.loads((root / 'MANET/share/manet/mt7916-firmware.json').read_text())
pin = manifest['linux_firmware']
cache = Path(os.environ.get('MANET_FIRMWARE_CACHE', root / 'kernel-work/cache/linux-firmware' / pin['commit']))
cache.mkdir(parents=True, exist_ok=True)
destination = stage / 'usr/local/share/manet/firmware/mt7916'
destination.mkdir(parents=True, exist_ok=True)
for name, entry in pin['files'].items():
    source = cache / name
    if not source.exists():
        with tempfile.TemporaryDirectory(prefix='download-', dir=cache) as scratch:
            download = Path(scratch) / name
            subprocess.run(['curl', '--fail', '--location', '--retry', '2', '--connect-timeout', '15',
                            '--max-time', '180', '--output', str(download), pin['base_url'] + entry['path']], check=True)
            if hashlib.sha256(download.read_bytes()).hexdigest() != entry['sha256']:
                sys.exit(f'MT7916 download hash mismatch: {name}')
            os.replace(download, source)
    if hashlib.sha256(source.read_bytes()).hexdigest() != entry['sha256']:
        sys.exit(f'MT7916 cache hash mismatch: {source}')
    shutil.copyfile(source, destination / name)
    (destination / name).chmod(0o644)
# A stale SBC overlay must never distribute the patched WM elsewhere.
for path in stage.rglob('mt7916_*.bin'):
    if path.is_symlink() or path.name not in pin['files']:
        sys.exit(f'Unexpected MT7916 firmware in stage: {path}')
    if hashlib.sha256(path.read_bytes()).hexdigest() != pin['files'][path.name]['sha256']:
        sys.exit(f'Non-stock MT7916 firmware in stage: {path}')
print(f'MT7916: pinned stock set staged from {cache}')
