#!/usr/bin/env python3
"""Apply the complete series, or skip it when already applied."""
import argparse
import difflib
import fcntl
from pathlib import Path
import re
import subprocess
import tempfile

BASE = '95b85bebbedcaedfa7ca79116ed38b7376fba412'


def apply(tree, series):
    patches = [series / line for line in (series / 'series').read_text().splitlines()
               if line and not line.startswith('#')]
    paths = sorted({match for patch in patches for match in
                    re.findall(r'^diff --git a/(\S+) b/\S+$', patch.read_text(), re.M)})
    if not paths or any(not p.startswith('drivers/net/wireless/mediatek/mt76/mt7915/')
                        or '..' in Path(p).parts for p in paths):
        raise ValueError('unexpected path in mt76-ftm series')
    head = subprocess.check_output(['git', '-C', str(tree), 'rev-parse', 'HEAD'], text=True).strip()
    if head != BASE:
        raise ValueError(f'mt76-ftm requires kernel HEAD {BASE}, found {head}')
    original = {p: (tree / p).read_bytes() if (tree / p).exists() else None for p in paths}

    def trial(reverse=False):
        with tempfile.TemporaryDirectory(prefix='manet-ftm-') as temp:
            work = Path(temp)
            for p, data in original.items():
                if data is not None:
                    (work / p).parent.mkdir(parents=True, exist_ok=True)
                    (work / p).write_bytes(data)
            for patch in reversed(patches) if reverse else patches:
                result = subprocess.run(['git', 'apply', *(['-R'] if reverse else []),
                                         str(patch.resolve())], cwd=work,
                                        capture_output=True, text=True)
                if result.returncode:
                    return None, result.stderr
            return {p: (work / p).read_bytes() if (work / p).exists() else None for p in paths}, ''

    if trial(True)[0] is not None:
        print('mt76-ftm: complete series already applied')
        return
    final, error = trial()
    if final is None:
        raise ValueError('mt76-ftm: partial or conflicting series; source unchanged\n' + error)
    # Combine the tested sequence into one transaction against the actual tree.
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
    print('mt76-ftm: applied complete series')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tree', type=Path)
    args = parser.parse_args()
    # Serialize this helper with itself. git apply validates all files before writes.
    with (args.tree / '.manet-ftm-apply.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            apply(args.tree.resolve(), Path(__file__).resolve().parent)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            parser.exit(1, str(error) + '\n')
