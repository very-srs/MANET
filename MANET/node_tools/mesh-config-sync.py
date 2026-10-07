#!/usr/bin/env python3
"""Alfred-coordinated mesh configuration changes: the receiving half.

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
EUD/AP settings (eud, lan_ap_ssid, lan_ap_key, max_euds_per_node) are stripped
and never applied from Alfred: those stay on the node that staged them.
"""

import json
import os
import re
import subprocess
import sys
import time

from mesh_config import (strip_local_keys, valid_value, validate_config,
                         SAFE_KEYS, DANGEROUS_KEYS, MESH_KEYS)
from manet_admin import AdminTransport, CONFIG_ACK_TYPE, private_json_write

ALFRED_CONFIG_TYPE = 70
PENDING_FILE = "/var/run/mesh_pending_config.json"
ACK_VERSION_FILE = "/var/run/mesh_config_ack_version"
APPLIED_VERSION_FILE = "/var/run/mesh_applied_config_version"
APPLY_SCRIPT = "/usr/local/bin/mesh-config-apply.sh"
# The apply runs as its own transient unit: this process lives in
# node-manager.service, which an acs change restarts. In its own cgroup the
# apply survives that restart, finishes the whole package and verifies the
# restart before recording success. The fixed unit name refuses an overlap.
# TimeoutStartSec bounds the job itself: once the manager restart kills this
# process, nothing else would, and a hung apply would hold the unit name.
APPLY_COMMAND = ["systemd-run", "--unit=mesh-config-apply", "--wait", "--collect",
                 "--quiet", "--service-type=oneshot", "--property=TimeoutStartSec=180",
                 APPLY_SCRIPT]
ROLLBACK_SCRIPT = "/usr/local/bin/mesh-config-rollback.sh"
LOG_FILE = "/var/log/mesh-config-sync.log"
ADMIN = AdminTransport()

# Only these may be carried in a package. EUD/AP settings are per-node and are
# stripped even if an older publisher still includes them.
ALLOWED_KEYS = tuple(MESH_KEYS)


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


def publish_ack(version):
    import socket
    envelope = ADMIN.seal(CONFIG_ACK_TYPE, {
        'kind': 'config_ack', 'version': version, 'hostname': socket.gethostname(),
    })
    result = run(['alfred', '-s', str(CONFIG_ACK_TYPE)],
                 input=json.dumps(envelope, separators=(',', ':')), timeout=5)
    return result.returncode == 0



# Shared validation is also used before local changes or broadcasting.

def validate_package(pkg):
    if not isinstance(pkg, dict):
        return False, "package is not an object"
    if pkg.get("kind") not in (None, "mesh_config"):
        return False, "not a mesh_config package"
    version = pkg.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{6,64}", version):
        return False, "missing or malformed version"
    config = strip_local_keys(pkg.get("config"))
    if not isinstance(config, dict) or not config:
        return False, "missing config block"

    current = {}
    try:
        with open('/etc/mesh.conf') as source:
            for line in source:
                key, sep, value = line.strip().partition('=')
                if sep:
                    current[key] = value
    except FileNotFoundError:
        pass
    ok, why = validate_config(config, current=current)
    if not ok:
        return False, why

    activate_at = pkg.get("activate_at", 0)
    if not isinstance(activate_at, int) or activate_at < 0:
        return False, "malformed activate_at"
    if not isinstance(pkg.get("no_rollback", False), bool):
        return False, "no_rollback must be a boolean"
    return True, ""


def package_is_dangerous(pkg, mesh_conf="/etc/mesh.conf"):
    """Would this package actually change a mesh-breaking setting *here*?

    Presence of the key is not enough: re-broadcasting the current SSID is a
    no-op and should not put the node into a five-minute trial window. The
    comparison is per node, because two nodes can hold different current
    values: the one that really is changing arms, the one already on the new
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



# Alfred

def latest_config_package(raw=None):
    """Newest authenticated message on type 70, or None.

    Alfred hands back one record per publishing node; the newest issue wins so
    a stale copy from a node that has not refreshed cannot override a newer
    change.
    """
    if raw is None:
        try:
            r = run(["alfred", "-r", str(ALFRED_CONFIG_TYPE)], timeout=5)
        except Exception as e:
            log(f"alfred read failed: {e}")
            return None
        if r.returncode != 0:
            return None

        raw = r.stdout
    messages = ADMIN.messages(ALFRED_CONFIG_TYPE, raw)
    return messages[-1] if messages else None



# Sync

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


def sync_once(raw=None):
    message = latest_config_package() if raw is None else latest_config_package(raw)
    if not message:
        return 0
    pkg = message.payload

    # A cancel has no config block, so it is handled before validation. Without
    # this the operator's cancel would be undone on the next cycle: the
    # original package is still resident in Alfred, and we would re-stage it.
    if pkg.get("kind") == "mesh_config_cancel":
        if ADMIN.accept(ALFRED_CONFIG_TYPE, message):
            clear_staging('cancelled by authenticated administrator')
            ADMIN.complete(ALFRED_CONFIG_TYPE, message)
        return 0

    ok, why = validate_package(pkg)
    if not ok:
        log(f"Ignoring config package: {why}")
        return 0

    pkg = dict(pkg)
    pkg["config"] = strip_local_keys(pkg.get("config") or {})
    if not pkg["config"]:
        log("Ignoring config package: only per-node settings")
        return 0

    if not ADMIN.accept(ALFRED_CONFIG_TYPE, message):
        return 0

    version = pkg["version"]
    if read_file(APPLIED_VERSION_FILE) == version:
        # Already applied. Drop any leftover staging state so a re-broadcast of
        # the same version does not make us apply it twice.
        clear_staging(f"version {version} already applied")
        ADMIN.complete(ALFRED_CONFIG_TYPE, message)
        return 0

    staged = ""
    try:
        with open(PENDING_FILE) as f:
            staged = json.load(f)
    except Exception:
        pass

    if staged != pkg or read_file(ACK_VERSION_FILE) != version:
        private_json_write(PENDING_FILE, pkg)
        write_file(ACK_VERSION_FILE, version)
        log(f"Staged config version {version}"
            f"{' (dangerous)' if package_is_dangerous(pkg) else ''}; ACK published")

    # The telemetry field remains informational. Only this authenticated ACK
    # can satisfy the management UI's activation gate.
    if not publish_ack(version):
        log('Failed to publish authenticated config ACK')
        return 1

    activate_at = int(pkg.get("activate_at", 0) or 0)
    if activate_at <= 0:
        return 0
    if time.time() < activate_at:
        return 0

    log(f"Activating config version {version}")

    # Arm the safety net before touching anything, so a change that takes the
    # mesh down can still be undone by this node on its own.
    if package_is_dangerous(pkg) and not pkg.get("no_rollback"):
        try:
            armed = run([ROLLBACK_SCRIPT, "arm", version], timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"Cannot prepare rollback; config not applied: {exc}")
            return 1
        if armed.returncode != 0:
            log(f"Cannot prepare rollback; config not applied: "
                f"{(armed.stderr or armed.stdout).strip()}")
            return 1

    # Applying acs can restart this very service. Consume the command before
    # invoking anything disruptive so a kill/reboot cannot make a recorded
    # activation execute again. A failed attempt needs a freshly staged edit.
    ADMIN.complete(ALFRED_CONFIG_TYPE, message)
    r = run(APPLY_COMMAND, timeout=180)
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
        return sync_once(sys.stdin.read() if '--stdin' in sys.argv[2:] else None)
    except Exception as e:
        log(f"sync error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
