#!/usr/bin/env python3
"""Write the regulatory domain from mesh.conf into every radio's own config.

radio-setup.sh writes the region into these files once, at provisioning. A
later change from the web UI only reached mesh.conf, so the reboot that was
supposed to apply it changed nothing on the radios. This writes the same
files radio-setup.sh does. The radios pick them up when their modules and
supplicants next start, which in practice means the next boot.

  /etc/modprobe.d/cfg80211.conf   Wi-Fi regulatory domain (real ISO code)
  /etc/modprobe.d/morse.conf      HaLow country (EU for every EU country)
  /etc/default/crda               regdomain for userspace tools
  /etc/hostapd/hostapd.conf       EUD access point country_code
  wpa_supplicant-wlan*.conf       Wi-Fi mesh supplicant country
  wpa_supplicant-*-s1g.conf       HaLow supplicant country, and the region's
                                  default channel when US <-> non-US changes,
                                  because the two plans share no channels

Usage: manet-region.py apply
"""

import os
from pathlib import Path
import re
import sys
import tempfile

# Must match uses_eu_halow_region() in radio-setup.sh.
EU_HALOW_COUNTRIES = frozenset(
    'AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI '
    'ES SE GB CH NO'.split())

# The default HaLow channel of each radio-setup.sh supplicant template:
# channel, op_class, s1g_prim_chwidth, s1g_prim_1mhz_chan_index.
HALOW_DEFAULT_CHANNEL = {
    'US': ('10', '69', '1', '1'),       # 907 MHz, 2 MHz
    'other': ('1', '66', '0', '0'),     # 863.5 MHz, 1 MHz (EU 2 MHz is refused)
}
EU_MORSE_OPTIONS = 'options morse enable_auto_duty_cycle=0 enable_auto_mpsw=0'
# Global S1G operating classes of the US plan; anything else is the EU plan.
US_OP_CLASSES = {'68', '69', '70', '71'}
COUNTRY_LINE = re.compile(r'^(\s*)country=("?)[A-Za-z0-9]{2}\2[ \t]*$', re.M)


def paths():
    root = Path(os.environ.get('MANET_REGION_ROOT', '/'))
    return {
        'mesh': root / 'etc/mesh.conf',
        'cfg80211': root / 'etc/modprobe.d/cfg80211.conf',
        'morse': root / 'etc/modprobe.d/morse.conf',
        'crda': root / 'etc/default/crda',
        'hostapd': root / 'etc/hostapd/hostapd.conf',
        'wpa': root / 'etc/wpa_supplicant',
    }


def write(path, content):
    """Atomic replace, keeping the existing file's mode."""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def conf_value(text, key):
    match = re.search(rf'^{re.escape(key)}=(.*)$', text, re.M)
    return match[1].strip().strip('"') if match else ''


def set_conf(text, key, value):
    if re.search(rf'^{re.escape(key)}=', text, re.M):
        return re.sub(rf'^{re.escape(key)}=.*$', f'{key}={value}', text, flags=re.M)
    return text + ('' if text.endswith('\n') or not text else '\n') + f'{key}={value}\n'


def halow_region(country):
    return 'EU' if country in EU_HALOW_COUNTRIES else country


def morse_options(text, halow):
    lines = [line for line in text.splitlines()
             if not re.match(r'options morse country=', line)
             and line.strip() != EU_MORSE_OPTIONS]
    lines.append(f'options morse country={halow}')
    if halow == 'EU':
        lines.append(EU_MORSE_OPTIONS)
    return '\n'.join(lines) + '\n'


def s1g_on_us_plan(text):
    match = re.search(r'^\s*op_class=(\d+)', text, re.M)
    return bool(match) and match[1] in US_OP_CLASSES


def s1g_config(text, halow):
    """Country lines only (never SSID/key text), and the region's default
    channel when this file is on the other plan. The plan is read from the
    file itself, so a retry after a partial failure still moves it."""
    text = COUNTRY_LINE.sub(lambda m: f'{m[1]}country="{halow}"', text)
    family_changed = s1g_on_us_plan(text) != (halow == 'US')
    if family_changed:
        channel, op_class, chwidth, index = HALOW_DEFAULT_CHANNEL[
            'US' if halow == 'US' else 'other']
        for key, value in (('channel', channel), ('op_class', op_class),
                           ('s1g_prim_chwidth', chwidth),
                           ('s1g_prim_1mhz_chan_index', index)):
            text = re.sub(rf'^(\s*{key}=)\S+', rf'\g<1>{value}', text, flags=re.M)
    return text, family_changed


def apply():
    p = paths()
    mesh = p['mesh'].read_text()
    country = conf_value(mesh, 'regulatory_domain').upper() or 'US'
    if not re.fullmatch(r'[A-Z]{2}', country):
        raise ValueError(f'Invalid regulatory domain {country!r}')
    halow = halow_region(country)
    family_changed = False
    changed = []

    def update(path, content):
        if not path.exists() or path.read_text() != content:
            write(path, content)
            changed.append(str(path))

    update(p['cfg80211'], f'options cfg80211 ieee80211_regdom={country}\n')
    morse = p['morse'].read_text() if p['morse'].exists() else ''
    update(p['morse'], morse_options(morse, halow))
    update(p['crda'], f'REGDOMAIN={country}\n')
    if p['hostapd'].exists():
        update(p['hostapd'], re.sub(r'^country_code=.*$', f'country_code={country}',
                                    p['hostapd'].read_text(), flags=re.M))
    for conf in sorted(p['wpa'].glob('wpa_supplicant-*.conf')):
        text = conf.read_text()
        if conf.name.endswith('-s1g.conf'):
            text, moved = s1g_config(text, halow)
            family_changed = family_changed or moved
            update(conf, text)
        else:
            update(conf, COUNTRY_LINE.sub(lambda m: f'{m[1]}country={country}', text))
    # Last: mesh.conf records the region only once every radio file has it.
    update(p['mesh'], set_conf(p['mesh'].read_text(), 'halow_regulatory_domain', halow))
    return country, halow, family_changed, changed


def main(argv):
    if argv != ['apply']:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    try:
        country, halow, family_changed, changed = apply()
    except (OSError, ValueError) as error:
        print(f'ERROR: region: {error}', file=sys.stderr)
        return 1
    print(f'Region {country} (HaLow {halow}) written to {len(changed)} file(s); '
          'radios use it from the next boot'
          + ('; HaLow moved to the region default channel' if family_changed else ''))
    for path in changed:
        print(f'  {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
