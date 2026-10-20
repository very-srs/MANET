#!/usr/bin/env python3
"""Apply the paired cfg80211/MT7916 stack to one of the pinned kernels."""
import argparse
import difflib
import fcntl
from pathlib import Path
import re
import subprocess
import tempfile

BASES = {
    '5d64d1ea73716526ed9c0ed184a9cb5f6042e32c': 'cm4',
    '2ff600d29ee478d0a8d46ef8a58afb5e038fe692': 'r3a',
}
HERE = Path(__file__).resolve().parent
PREFIXES = ('net/mac80211/', 'net/wireless/', 'drivers/net/wireless/mediatek/mt76/mt7915/')


def inventory(series=HERE):
    patches = []
    for entry in (series / 'series').read_text().splitlines():
        if not entry or entry.startswith('#'):
            continue
        if entry not in ('cfg80211/series', 'mt76/series', 'mac80211/series'):
            raise ValueError('unexpected series path')
        child = series / entry
        for name in child.read_text().splitlines():
            if not name or name.startswith('#'):
                continue
            if Path(name).name != name or not name.endswith('.patch'):
                raise ValueError('unexpected patch path')
            patches.append(child.parent / name)
    paths = sorted({p for patch in patches for p in
                    re.findall(r'^diff --git a/(\S+) b/\S+$', patch.read_text(), re.M)})
    if not paths or any(not p.startswith(PREFIXES) or '..' in Path(p).parts for p in paths):
        raise ValueError('unexpected source path in combined series')
    return patches, paths


def apply(tree, series=HERE):
    head = subprocess.check_output(['git', '-C', str(tree), 'rev-parse', 'HEAD'], text=True).strip()
    if head not in BASES:
        raise ValueError('combined stack requires kernel HEAD in ' + ', '.join(BASES))
    patches, paths = inventory(series)
    original, base = {}, {}
    for p in paths:
        target = tree / p
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents if parent != tree):
            raise ValueError('symlink in patch destination: ' + p)
        original[p] = target.read_bytes() if target.exists() else None
        result = subprocess.run(['git', '-C', str(tree), 'show', head + ':' + p], capture_output=True)
        base[p] = result.stdout if result.returncode == 0 else None
    with tempfile.TemporaryDirectory(prefix='manet-stack-') as temp:
        work = Path(temp)
        for p, data in base.items():
            if data is not None:
                (work / p).parent.mkdir(parents=True, exist_ok=True)
                (work / p).write_bytes(data)
        for patch in patches:
            subprocess.run(['git', 'apply', str(patch.resolve())], cwd=work, check=True)
        final = {p: (work / p).read_bytes() if (work / p).exists() else None for p in paths}
    if original == final:
        print('manet-mt7916: complete paired series already applied')
        return
    if original != base:
        raise ValueError('manet-mt7916: partial or conflicting series; source unchanged; use a clean pinned tree')
    patch = ''
    for p in paths:
        old, new = original[p], final[p]
        if old == new:
            continue
        patch += f'diff --git a/{p} b/{p}\n'
        if old is None:
            patch += 'new file mode 100644\n'
        patch += ''.join(difflib.unified_diff((old or b'').decode().splitlines(True),
                                             (new or b'').decode().splitlines(True),
                                             fromfile='a/' + p if old is not None else '/dev/null',
                                             tofile='b/' + p if new is not None else '/dev/null'))
    subprocess.run(['git', 'apply', '--check', '-'], cwd=tree, input=patch, text=True, check=True)
    subprocess.run(['git', 'apply', '-'], cwd=tree, input=patch, text=True, check=True)
    print('manet-mt7916: applied complete paired series')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tree', type=Path)
    args = parser.parse_args()
    with (args.tree / '.manet-stack-apply.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            apply(args.tree.resolve())
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            parser.exit(1, str(error) + '\n')
