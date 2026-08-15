#!/usr/bin/env python3
"""Wire/display conversions for identifiers carried in Alfred payloads.

MACs and syncthing device IDs are published as raw bytes rather than in their
printed spellings — a MAC is 6 bytes but 17 characters, and a syncthing ID is
32 bytes but 63. Alfred replicates these to every node on a timer, so the
printed forms cost real airtime. Everything here converts between the two.

Shared by encoder.py and decoder.py, which both live in the same directory as
this file on the node (/usr/local/bin).
"""

# Syncthing device IDs are base32 over this alphabet with Luhn check
# characters interleaved. See syncthing's lib/protocol/deviceid.go.
_B32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def mac_to_bytes(mac):
    """'0c:bf:74:00:2b:e6' -> b'\\x0c\\xbft\\x00+\\xe6'. Returns b'' if unparseable."""
    try:
        raw = bytes.fromhex(str(mac).replace(':', '').replace('-', '').strip())
    except ValueError:
        return b''
    return raw if len(raw) == 6 else b''


def bytes_to_mac(raw):
    """b'\\x0c\\xbft\\x00+\\xe6' -> '0c:bf:74:00:2b:e6'. Returns '' if not 6 bytes."""
    if not raw or len(raw) != 6:
        return ''
    return ':'.join(f'{b:02x}' for b in raw)


def _luhn32(chars):
    """Luhn mod-32 check character over the base32 alphabet."""
    n = len(_B32_ALPHABET)
    factor = 1
    total = 0
    for ch in chars:
        codepoint = _B32_ALPHABET.index(ch)
        addend = factor * codepoint
        factor = 1 if factor == 2 else 2
        addend = (addend // n) + (addend % n)
        total += addend
    return _B32_ALPHABET[(n - (total % n)) % n]


def _unluhnify(s):
    """56 chars with check characters -> the 52 payload characters."""
    if len(s) != 56:
        raise ValueError(f'expected 56 characters, got {len(s)}')
    out = []
    for i in range(4):
        chunk = s[i * 14:(i + 1) * 14]
        if _luhn32(chunk[:13]) != chunk[13]:
            raise ValueError('check character mismatch')
        out.append(chunk[:13])
    return ''.join(out)


def _luhnify(s):
    """52 payload characters -> 56 with check characters."""
    if len(s) != 52:
        raise ValueError(f'expected 52 characters, got {len(s)}')
    return ''.join(s[i * 13:(i + 1) * 13] + _luhn32(s[i * 13:(i + 1) * 13])
                   for i in range(4))


def syncthing_id_to_bytes(device_id):
    """'PDUK43G-6IOSN4B-...' -> the underlying 32 bytes. b'' if unparseable."""
    import base64
    s = str(device_id or '').replace('-', '').replace(' ', '').strip().upper()
    if not s:
        return b''
    try:
        if len(s) == 56:
            s = _unluhnify(s)
        if len(s) != 52:
            return b''
        return base64.b32decode(s + '====')
    except Exception:
        return b''


def bytes_to_syncthing_id(raw):
    """32 bytes -> the canonical dashed device ID. '' if not 32 bytes."""
    import base64
    if not raw or len(raw) != 32:
        return ''
    try:
        s = base64.b32encode(raw).decode('ascii').rstrip('=')
        s = _luhnify(s)
        return '-'.join(s[i:i + 7] for i in range(0, len(s), 7))
    except Exception:
        return ''


def ipv4_to_int(addr):
    """'10.30.2.243' -> 169738483. 0 if unparseable."""
    parts = str(addr or '').strip().split('.')
    if len(parts) != 4:
        return 0
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return 0
    if any(o < 0 or o > 255 for o in octets):
        return 0
    return int.from_bytes(bytes(octets), 'big')


def int_to_ipv4(value):
    """169738483 -> '10.30.2.243'. '' if zero/unset."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return ''
    if value <= 0:
        return ''
    return '.'.join(str(b) for b in value.to_bytes(4, 'big'))
