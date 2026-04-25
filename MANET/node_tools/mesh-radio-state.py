#!/usr/bin/env python3
"""
Alfred-coordinated MANET radio state changes.

sync:
  - reads the latest radio-state package from Alfred type 71
  - stages it locally and publishes an ACK on Alfred type 72
  - applies it at activate_at after the coordinator has collected ACKs

apply:
  - applies a staged package from disk
"""

import json
import os
import re
import socket
import subprocess
import sys
import time


ALFRED_RADIO_TYPE = 71
ALFRED_RADIO_ACK_TYPE = 72
PENDING_FILE = "/var/run/mesh_pending_radio_state.json"
ACK_VERSION_FILE = "/var/run/mesh_radio_ack_version"
APPLIED_VERSION_FILE = "/var/run/mesh_applied_radio_version"
CURRENT_STATE_FILE = "/var/lib/mesh_radio_state.json"
LOG_FILE = "/var/log/mesh-radio-state.log"
VALID_IFACES = ("wlan0", "wlan1", "wlan2")
VALID_STATES = ("up", "down")


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] RADIO-STATE: {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        subprocess.run(["systemd-cat", "-t", "mesh-radio-state"],
                       input=line + "\n", text=True, timeout=2)
    except Exception:
        pass


def run(cmd, timeout=10, check=False):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, check=check)


def hostname():
    return socket.gethostname()


def read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def write_text(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def write_json(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(value, f, separators=(",", ":"))
    os.replace(tmp, path)


def load_halow_ifaces():
    names = set()
    for path in ("/var/lib/halow_if",):
        txt = read_text(path)
        for name in txt.split():
            if name:
                names.add(name)
    return names


def is_halow_iface(iface):
    if iface in load_halow_ifaces():
        return True
    try:
        r = run(["ethtool", "-i", iface], timeout=3)
        for line in r.stdout.splitlines():
            if line.startswith("driver:") and "morse" in line.lower():
                return True
    except Exception:
        pass
    return False


def service_for_iface(iface):
    if is_halow_iface(iface):
        return f"wpa_supplicant-s1g-{iface}.service"
    return f"wpa_supplicant@{iface}.service"


def active_bat_ifaces():
    try:
        r = run(["batctl", "if"], timeout=5)
    except Exception:
        return set()
    active = set()
    for line in r.stdout.splitlines():
        m = re.match(r"^\s*([^:\s]+):\s+active\b", line)
        if m:
            active.add(m.group(1))
    return active


def bat_has_iface(iface):
    try:
        r = run(["batctl", "if"], timeout=5)
    except Exception:
        return False
    return any(re.match(rf"^\s*{re.escape(iface)}:\s+", line)
               for line in r.stdout.splitlines())


def apply_iface(iface, state):
    svc = service_for_iface(iface)
    log(f"Applying {iface}={state} using {svc}")

    if state == "down":
        run(["batctl", "if", "del", iface], timeout=10)
        r = run(["systemctl", "stop", svc], timeout=20)
        if r.returncode != 0:
            raise RuntimeError(f"systemctl stop {svc}: {r.stderr.strip() or r.stdout.strip()}")
        r = run(["ip", "link", "set", iface, "down"], timeout=10)
        if r.returncode != 0:
            raise RuntimeError(f"ip link set {iface} down: {r.stderr.strip() or r.stdout.strip()}")
        return

    r = run(["ip", "link", "set", iface, "up"], timeout=10)
    if r.returncode != 0:
        raise RuntimeError(f"ip link set {iface} up: {r.stderr.strip() or r.stdout.strip()}")
    r = run(["systemctl", "start", svc], timeout=25)
    if r.returncode != 0:
        raise RuntimeError(f"systemctl start {svc}: {r.stderr.strip() or r.stdout.strip()}")
    time.sleep(2)
    if not bat_has_iface(iface):
        r = run(["batctl", "if", "add", iface], timeout=10)
        if r.returncode != 0:
            raise RuntimeError(f"batctl if add {iface}: {r.stderr.strip() or r.stdout.strip()}")


def target_matches(pkg):
    targets = pkg.get("targets", "all")
    if targets == "all":
        return True
    if isinstance(targets, str):
        targets = [targets]
    my_name = hostname()
    return my_name in set(str(t) for t in targets)


def validate_pkg(pkg):
    if not isinstance(pkg, dict):
        return False, "package is not an object"
    if pkg.get("kind") != "radio_state":
        return False, "not a radio_state package"
    if not pkg.get("version"):
        return False, "missing version"
    desired = pkg.get("desired")
    if not isinstance(desired, dict) or not desired:
        return False, "missing desired state"
    for iface, state in desired.items():
        if iface not in VALID_IFACES or state not in VALID_STATES:
            return False, f"invalid desired state {iface}={state}"

    if target_matches(pkg):
        post = active_bat_ifaces()
        for iface, state in desired.items():
            if state == "down":
                post.discard(iface)
            else:
                post.add(iface)
        if not post:
            return False, "refusing to leave node without an active batman-adv radio"
    return True, ""


def send_alfred(type_id, payload):
    body = json.dumps(payload, separators=(",", ":"))
    try:
        r = subprocess.run(["alfred", "-s", str(type_id)], input=body,
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def publish_ack(version, ok=True, error="", target=False):
    payload = {
        "kind": "radio_ack",
        "version": version,
        "hostname": hostname(),
        "ok": bool(ok),
        "error": error,
        "target": bool(target),
        "ts": int(time.time()),
    }
    send_alfred(ALFRED_RADIO_ACK_TYPE, payload)


def add_candidate(candidates, value):
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return
        try:
            value = json.loads(value)
        except Exception:
            return
    if isinstance(value, dict) and value.get("kind") in ("radio_state", "radio_cancel"):
        candidates.append(value)


def extract_alfred_payloads(raw):
    candidates = []
    add_candidate(candidates, raw)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for value in data.values():
                add_candidate(candidates, value)
        elif isinstance(data, list):
            for value in data:
                add_candidate(candidates, value)
    except Exception:
        pass

    for line in raw.splitlines():
        add_candidate(candidates, line)

    for match in re.finditer(r'"((?:\\.|[^"\\])*)"\s*(?:[,}])', raw):
        try:
            text = bytes(match.group(1), "utf-8").decode("unicode_escape")
        except Exception:
            continue
        add_candidate(candidates, text)

    return candidates


def latest_radio_package():
    try:
        r = run(["alfred", "-r", str(ALFRED_RADIO_TYPE)], timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    pkgs = extract_alfred_payloads(r.stdout)
    if not pkgs:
        return None
    pkgs.sort(key=lambda p: (int(p.get("issued_at", 0) or 0), str(p.get("version", ""))))
    return pkgs[-1]


def clear_pending(version=None):
    pending = read_json(PENDING_FILE)
    if version and pending and pending.get("version") != version:
        return
    for path in (PENDING_FILE, ACK_VERSION_FILE):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception:
            pass


def apply_package(pkg):
    desired = pkg.get("desired", {})
    if not target_matches(pkg):
        log(f"Version {pkg.get('version')} is not targeted at this node; no-op apply")
        return
    ok, error = validate_pkg(pkg)
    if not ok:
        raise RuntimeError(error)
    for iface, state in desired.items():
        apply_iface(iface, state)
    record_current_state(pkg)


def record_current_state(pkg):
    desired = pkg.get("desired", {})
    data = read_json(CURRENT_STATE_FILE) or {"desired": {}}
    if not isinstance(data.get("desired"), dict):
        data["desired"] = {}
    data["desired"].update(desired)
    data["version"] = pkg.get("version", "")
    data["updated_at"] = int(time.time())
    os.makedirs(os.path.dirname(CURRENT_STATE_FILE), exist_ok=True)
    write_json(CURRENT_STATE_FILE, data)


def sync_once():
    pkg = latest_radio_package()
    pending = read_json(PENDING_FILE)

    if not pkg:
        if pending and pending.get("version"):
            publish_ack(pending["version"], True, "", target_matches(pending))
        return 0

    if pkg.get("kind") == "radio_cancel":
        version = pkg.get("version", "")
        clear_pending(version)
        publish_ack(version, True, "cancelled", False)
        log(f"Cancelled pending radio state {version}")
        return 0

    version = pkg.get("version", "")
    ok, error = validate_pkg(pkg)
    target = target_matches(pkg)
    if not ok:
        publish_ack(version, False, error, target)
        log(f"Rejected radio state {version}: {error}")
        return 1

    activate_at = int(pkg.get("activate_at", 0) or 0)
    already_applied = read_text(APPLIED_VERSION_FILE) == version
    if activate_at > 0 and already_applied:
        clear_pending(version)
        publish_ack(version, True, "applied", target)
        return 0

    if not pending or pending.get("version") != version or pending.get("activate_at") != pkg.get("activate_at"):
        write_json(PENDING_FILE, pkg)
        log(f"Staged radio state {version}: {pkg.get('desired')} targets={pkg.get('targets', 'all')} activate_at={pkg.get('activate_at', 0)}")

    write_text(ACK_VERSION_FILE, version)
    publish_ack(version, True, "", target)

    if activate_at > 0 and int(time.time()) >= activate_at and not already_applied:
        try:
            apply_package(pkg)
            write_text(APPLIED_VERSION_FILE, version)
            clear_pending(version)
            publish_ack(version, True, "applied", target)
            log(f"Applied radio state {version}")
        except Exception as exc:
            publish_ack(version, False, str(exc), target)
            log(f"Apply failed for radio state {version}: {exc}")
            return 1
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd == "sync":
        return sync_once()
    if cmd == "apply":
        path = sys.argv[2] if len(sys.argv) > 2 else PENDING_FILE
        pkg = read_json(path)
        if not pkg:
            print(f"No radio package at {path}", file=sys.stderr)
            return 1
        apply_package(pkg)
        return 0
    print("usage: mesh-radio-state.py [sync|apply [path]]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
