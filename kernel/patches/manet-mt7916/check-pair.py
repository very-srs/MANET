#!/usr/bin/env python3
"""Check the built pair's private exports, imports, vermagic and symbol CRCs."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

PRIVATE = {'cfg80211_hold_token_v2', 'cfg80211_hold_sample_v2',
           'cfg80211_hold_transfer_v3', 'cfg80211_hold_main_stop_v3'}
DRIVER = 'drivers/net/wireless/mediatek/mt76/mt7915'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def output(*args):
    return subprocess.check_output(list(map(str, args)), text=True)


def check(tree, build, cfg80211, driver, extra_symvers=(), mac80211=None):
    release = (build / 'include/config/kernel.release').read_text().strip()
    headers = [tree / 'net/wireless/hold-api.h', tree / DRIVER / 'hold-api.h']
    if headers[0].read_bytes() != headers[1].read_bytes():
        raise ValueError('Paired private headers differ')
    exports = {}
    for symvers in (build / 'Module.symvers', *extra_symvers):
        for line in symvers.read_text().splitlines():
            crc, symbol, provider, kind, *rest = line.split()
            if symbol in exports and exports[symbol]['crc'] != int(crc, 16):
                raise ValueError('Conflicting symbol CRC: ' + symbol)
            exports[symbol] = dict(crc=int(crc, 16), provider=provider, kind=kind)
    defined = {line.split()[-1] for line in output('aarch64-linux-gnu-nm', '--defined-only', cfg80211).splitlines()}
    if not all('__ksymtab_' + symbol in defined for symbol in PRIVATE):
        raise ValueError('cfg80211 does not export every private hold symbol')
    result = dict(release=release, private_header_sha256=sha(headers[0]),
                  private_exports=sorted(PRIVATE), config_sha256=sha(build / '.config'),
                  symvers_sha256=sha(build / 'Module.symvers'), modules={})
    modules = [('cfg80211', cfg80211), ('mt7915e', driver)]
    if mac80211 is not None:
        modules.append(('mac80211', mac80211))
    for name, path in modules:
        imports = {line.split()[-1] for line in output('aarch64-linux-gnu-nm', '-u', path).splitlines()}
        missing = imports - exports.keys()
        if missing:
            raise ValueError(f'{name}: unresolved imports {sorted(missing)}')
        if name == 'mt7915e' and imports & PRIVATE != PRIVATE:
            raise ValueError('Driver does not import the complete private API')
        vermagic = output('modinfo', '-F', 'vermagic', path).strip()
        if not vermagic.startswith(release + ' '):
            raise ValueError('Mismatched module release: ' + vermagic)
        if name != 'mac80211' and output('modinfo', '-F', 'manet_cac_hold', path).strip() != 'experimental-v2':
            raise ValueError('Missing hold capability tag')
        versions = {}
        versioned = 'CONFIG_MODVERSIONS=y' in (build / '.config').read_text()
        if versioned:
            for line in output('modprobe', '--dump-modversions', path).splitlines():
                crc, symbol = line.split()
                versions[symbol] = int(crc, 16)
        for symbol, crc in versions.items():
            if symbol not in exports or exports[symbol]['crc'] != crc:
                raise ValueError(f'{name}: modversions mismatch for {symbol}')
        if versioned and not versions:
            raise ValueError('Missing module version CRCs')
        result['modules'][name] = dict(sha256=sha(path), bytes=path.stat().st_size,
                                       vermagic=vermagic, imports=len(imports),
                                       private_imports=sorted(imports & PRIVATE),
                                       verified_version_crcs=len(versions))
    if output('modinfo', '-F', 'manet_rx_transition', driver).strip() != 'diagnostic-v2':
        raise ValueError('Missing runtime transition capability')
    if output('modinfo', '-F', 'manet_timing', driver).strip() != '1':
        raise ValueError('Missing timing capability')
    if output('modinfo', '-F', 'manet_dfs_mesh', driver).strip() != '1':
        raise ValueError('Missing main detector transfer capability')
    driver_symbols = {line.split()[-1] for line in
                      output('aarch64-linux-gnu-nm', '--defined-only', driver).splitlines()}
    if 'mt7915_eeprom_can_reserve_background_radar' not in driver_symbols:
        raise ValueError('Missing runtime background-CAC capability predicate')
    result['runtime_background_cac_predicate'] = True
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('tree', 'build', 'cfg80211', 'driver', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--mac80211', type=Path)
    parser.add_argument('--extra-symvers', action='append', default=[], type=Path)
    args = parser.parse_args()
    result = check(args.tree, args.build, args.cfg80211, args.driver, args.extra_symvers, args.mac80211)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
