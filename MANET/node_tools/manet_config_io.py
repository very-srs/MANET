"""Literal configuration rendering and durable, permission-preserving writes."""

import os
import json
from pathlib import Path
import re
import stat
import tempfile


def supplicant_string(value):
    """Render a literal UTF-8 value for wpa_supplicant."""
    # Ordinary wpa_supplicant quotes are literal, not C/Python string quotes.
    # Hex handles embedded quotes without relying on version-specific escapes.
    return value.encode('utf-8').hex() if '"' in value else '"' + value + '"'


def rewrite_keys(text, values, quoted_keys=()):
    """Replace matching assignments and append missing validated keys."""
    replacements = {}
    for key, value in values.items():
        # Accept lowercase configuration names only, never arbitrary syntax.
        if not re.fullmatch('[a-z][a-z0-9_]*', key):
            raise ValueError('Invalid configuration key')
        if not isinstance(value, str) or any(c in value for c in '\n\r\0'):
            raise ValueError('Configuration values must occupy one line')
        replacements[key] = supplicant_string(value) if key in quoted_keys else value
    lines, found = [], set()
    for line in text.splitlines(keepends=True):
        key = line.partition('=')[0].strip()
        if key in replacements:
            indent = line[:len(line) - len(line.lstrip())]
            lines.append(f'{indent}{key}={replacements[key]}\n')
            found.add(key)
        else:
            lines.append(line)
    for key, value in replacements.items():
        if key not in found:
            if lines and not lines[-1].endswith('\n'):
                lines[-1] += '\n'
            lines.append(f'{key}={value}\n')
    return ''.join(lines)


def atomic_write(path, contents, metadata=None):
    """Replace a file durably with its mode and, as root, ownership."""
    path = Path(path)
    if metadata is None:
        try:
            metadata = path.stat()
        except FileNotFoundError:
            pass
    # A same-directory temporary keeps replacement on one filesystem.
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as target:
            os.fchmod(target.fileno(), stat.S_IMODE(metadata.st_mode) if metadata else 0o600)
            if metadata and os.geteuid() == 0:
                os.fchown(target.fileno(), metadata.st_uid, metadata.st_gid)
            target.write(contents.encode('utf-8') if isinstance(contents, str) else contents)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        # Persist the directory entry after persisting file contents.
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    """Read a JSON object, or return empty state on read/parse failure."""
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def load_kv_file(path):
    """Parse a key=value or key='value' file into a dict."""
    conf = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    # Read assignments as data; never evaluate shell text.
                    k, v = line.split('=', 1)
                    conf[k.strip()] = v.strip().strip('"\'')
    except Exception:
        pass
    return conf
