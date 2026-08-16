#!/usr/bin/env python3
"""Alfred-coordinated mesh configuration changes — the receiving half.

sync:
  - reads the newest config package from Alfred type 70
  - validates it, stages it to /var/run/mesh_pending_config.json, and publishes
    an ACK by writing /var/run/mesh_config_ack_version (the node managers carry
    that into telemetry, which is what fills the ACK table in the UI)
  - runs mesh-config-apply.sh once activate_at has passed

The operator drives the other half from the management UI: Stage broadcasts a
package with activate_at=0, the ACK table fills as nodes acknowledge, then Apply
sets activate_at and every node applies at the same moment.

Everything in a package arrives from the network and ends up in /etc/mesh.conf
and in wpa_supplicant configs, so it is validated here rather than trusted.
"""

import json
import os
import re
import subprocess
import sys
import time

ALFRED_CONFIG_TYPE = 70
PENDING_FILE = "/var/run/mesh_pending_config.json"
ACK_VERSION_FILE = "/var/run/mesh_config_ack_version"
APPLIED_VERSION_FILE = "/var/run/mesh_applied_config_version"
APPLY_SCRIPT = "/usr/local/bin/mesh-config-apply.sh"
ROLLBACK_SCRIPT = "/usr/local/bin/mesh-config-rollback.sh"
LOG_FILE = "/var/log/mesh-config-sync.log"

# Only these may be carried in a package. Anything else is ignored rather than
# written through to mesh.conf.
SAFE_KEYS = ("admin_password", "eud", "lan_ap_ssid", "lan_ap_key",
             "max_euds_per_node", "mtx", "mumble", "auto_update")
DANGEROUS_KEYS = ("mesh_ssid", "mesh_key", "ipv4_network")
ALLOWED_KEYS = SAFE_KEYS + DANGEROUS_KEYS + ("regulatory_domain", "acs")


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] CONFIG-SYNC: {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, file=sys.stderr)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def write_file(path, text):
    try:
        with open(path, "w") as f:
            f.write(text)
        return True
    except Exception as e:
        log(f"cannot write {path}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────
# A value ends up on a `key=value` line in mesh.conf, and some are substituted
# into wpa_supplicant configs inside double quotes. Newlines and quotes would
# let a peer write arbitrary configuration, so they are refused outright.
_FORBIDDEN = re.compile(r'["\'\n\r\x00]')


def valid_value(key, value):
    if not isinstance(value, str):
        return False, "not a string"
    if _FORBIDDEN.search(value):
        return False, "contains a quote, newline or NUL"
    if len(value) > 128:
        return False, "longer than 128 characters"

    if key == "ipv4_network":
        m = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})", value)
        if not m:
            return False, "not a CIDR block"
        if any(int(o) > 255 for o in m.group(1).split(".")):
            return False, "octet out of range"
        if not 8 <= int(m.group(2)) <= 30:
            return False, "prefix length out of range"
    elif key == "mesh_ssid":
        if not 1 <= len(value) <= 32:
            return False, "SSID must be 1-32 characters"
    elif key == "mesh_key":
        if not 8 <= len(value) <= 63:
            return False, "SAE key must be 8-63 characters"
    elif key == "eud":
        if value not in ("wired", "wireless", "auto"):
            return False, "must be wired, wireless or auto"
    elif key in ("mtx", "mumble", "auto_update", "acs"):
        if value not in ("y", "n"):
            return False, "must be y or n"
    elif key == "regulatory_domain":
        if not re.fullmatch(r"[A-Z]{2}", value):
            return False, "must be a 2-letter country code"
    elif key == "max_euds_per_node":
        if not value.isdigit() or int(value) > 253:
            return False, "must be a number 0-253"
    return True, ""


def validate_package(pkg):
    if not isinstance(pkg, dict):
        return False, "package is not an object"
    if pkg.get("kind") not in (None, "mesh_config"):
        return False, "not a mesh_config package"
    version = pkg.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{6,64}", version):
        return False, "missing or malformed version"
    config = pkg.get("config")
    if not isinstance(config, dict) or not config:
        return False, "missing config block"

    for key, value in config.items():
        if key not in ALLOWED_KEYS:
            return False, f"unknown setting {key!r}"
        ok, why = valid_value(key, value)
        if not ok:
            return False, f"{key}: {why}"

    activate_at = pkg.get("activate_at", 0)
    if not isinstance(activate_at, int) or activate_at < 0:
        return False, "malformed activate_at"
    return True, ""


def package_is_dangerous(pkg, mesh_conf="/etc/mesh.conf"):
    """Would this package actually change a mesh-breaking setting *here*?

    Presence of the key is not enough: re-broadcasting the current SSID is a
    no-op and should not put the node into a five-minute trial window. The
    comparison is per node, because two nodes can hold different current
    values — the one that really is changing arms, the one already on the new
    value does not.
    """
    config = pkg.get("config", {})
    current = {}
    try:
        with open(mesh_conf) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    current[k.strip()] = v.strip().strip("\"'")
    except Exception:
        # Cannot tell what would change, so assume the worst and arm.
        return any(k in config for k in DANGEROUS_KEYS)
    return any(k in config and config[k] != current.get(k, "") for k in DANGEROUS_KEYS)


# ─────────────────────────────────────────────────────────────────────────────
# Alfred
# ─────────────────────────────────────────────────────────────────────────────
def latest_config_package():
    """Newest valid package on type 70, or None.

    Alfred hands back one record per publishing node; the newest issue wins so
    a stale copy from a node that has not refreshed cannot override a newer
    change.
    """
    try:
        r = run(["alfred", "-r", str(ALFRED_CONFIG_TYPE)], timeout=5)
    except Exception as e:
        log(f"alfred read failed: {e}")
        return None
    if r.returncode != 0:
        return None

    packages = []
    # Records look like:  { "aa:bb:cc:dd:ee:ff", "<json>" },
    for match in re.finditer(r'"((?:\\.|[^"\\])*)"\s*(?:[,}])', r.stdout):
        text = match.group(1)
        if "{" not in text:
            continue
        try:
            candidate = json.loads(text.encode().decode("unicode_escape"))
        except Exception:
            continue
        if isinstance(candidate, dict) and (
                candidate.get("config") or candidate.get("kind") == "mesh_config_cancel"):
            packages.append(candidate)

    if not packages:
        return None
    packages.sort(key=lambda p: (int(p.get("issued_at", 0) or 0), str(p.get("version", ""))))
    return packages[-1]


# ─────────────────────────────────────────────────────────────────────────────
# Sync
# ─────────────────────────────────────────────────────────────────────────────
def clear_staging(reason):
    removed = False
    for path in (PENDING_FILE, ACK_VERSION_FILE):
        try:
            os.remove(path)
            removed = True
        except FileNotFoundError:
            pass
    if removed:
        log(f"Cleared staged config: {reason}")


def sync_once():
    pkg = latest_config_package()
    if not pkg:
        return 0

    # A cancel has no config block, so it is handled before validation. Without
    # this the operator's cancel would be undone on the next cycle: the
    # original package is still resident in Alfred, and we would re-stage it.
    if pkg.get("kind") == "mesh_config_cancel":
        clear_staging(f"cancelled by {pkg.get('issued_by', 'operator')}")
        return 0

    ok, why = validate_package(pkg)
    if not ok:
        log(f"Ignoring config package: {why}")
        return 0

    version = pkg["version"]
    if read_file(APPLIED_VERSION_FILE) == version:
        # Already applied. Drop any leftover staging state so a re-broadcast of
        # the same version does not make us apply it twice.
        clear_staging(f"version {version} already applied")
        return 0

    staged = ""
    try:
        with open(PENDING_FILE) as f:
            staged = json.load(f).get("version", "")
    except Exception:
        pass

    if staged != version or read_file(ACK_VERSION_FILE) != version:
        if not write_file(PENDING_FILE, json.dumps(pkg)):
            return 1
        write_file(ACK_VERSION_FILE, version)
        log(f"Staged config version {version}"
            f"{' (dangerous)' if package_is_dangerous(pkg) else ''}; ACK published")

    activate_at = int(pkg.get("activate_at", 0) or 0)
    if activate_at <= 0:
        return 0
    if time.time() < activate_at:
        return 0

    log(f"Activating config version {version}")

    # Arm the safety net before touching anything, so a change that takes the
    # mesh down can still be undone by this node on its own.
    if package_is_dangerous(pkg) and not pkg.get("no_rollback"):
        if os.path.exists(ROLLBACK_SCRIPT):
            run([ROLLBACK_SCRIPT, "arm", version], timeout=30)
        else:
            log("WARNING: rollback script missing; applying without a safety net")

    r = run([APPLY_SCRIPT], timeout=180)
    if r.returncode != 0:
        log(f"apply failed: {(r.stderr or r.stdout).strip()}")
        return 1
    return 0


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if action != "sync":
        print(f"usage: {os.path.basename(sys.argv[0])} sync", file=sys.stderr)
        return 2
    try:
        return sync_once()
    except Exception as e:
        log(f"sync error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
