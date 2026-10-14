#!/usr/bin/env python3
"""Receive authenticated mesh configuration changes over Alfred type 70.

Validate packages before staging /var/run/mesh_pending_config.json. Publish
an authenticated ACK for the UI gate. /var/run/mesh_config_ack_version feeds
informational telemetry only. Stage uses activate_at=0, then Apply sets the
shared activation time for mesh-config-apply.sh.
Validate values destined for /etc/mesh.conf and wpa_supplicant configs.
Strip per-node eud, lan_ap_ssid, lan_ap_key and max_euds_per_node settings.
"""

import json
import os
import re
import subprocess
import sys
import time

from mesh_config import strip_local_keys, valid_value, validate_config
from manet_admin import AdminTransport, CONFIG_ACK_TYPE, clock_ready
from manet_config_journal import ConfigJournal

ALFRED_CONFIG_TYPE = 70
# None lets ConfigJournal resolve environment defaults at each invocation.
PENDING_FILE = None
ACK_VERSION_FILE = None
APPLIED_VERSION_FILE = None
APPLY_SCRIPT = "/usr/local/bin/mesh-config-apply.sh"
# A separate transient unit survives an acs restart of node-manager.service,
# applies the full package and verifies the restart before recording success.
# The fixed name prevents overlap. TimeoutStartSec releases it after a hung
# apply even if the manager restart has killed this caller.
APPLY_COMMAND = ["systemd-run", "--unit=mesh-config-apply", "--wait",
                 "--collect", "--quiet", "--service-type=oneshot",
                 "--property=TimeoutStartSec=180", APPLY_SCRIPT]
LOG_FILE = os.environ.get('MANET_CONFIG_LOG', '/var/log/mesh-config-sync.log')
ADMIN = AdminTransport(
    os.environ.get('MANET_MESH_CONF', '/etc/mesh.conf'),
    os.environ.get('MANET_ADMIN_STATE_DIR', '/var/lib/manet-admin'))
JOURNAL_DIR = None

def log(msg):
    """Append a timestamped config message and also emit it on stderr."""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] CONFIG-SYNC: {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, file=sys.stderr)


def run(cmd, **kw):
    """Run a command with captured text output and caller-supplied options."""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def publish_ack(version, status="staged"):
    """Seal and publish this node's configuration acknowledgement."""
    import socket
    envelope = ADMIN.seal(CONFIG_ACK_TYPE, {
        'kind': 'config_ack', 'version': version,
        'hostname': socket.gethostname(), 'application_status': status,
        'applied_version': version if status in
        {'verified', 'committed'} else None,
    })
    # Bound ACK publication to five seconds. A failed send blocks activation.
    result = run(['alfred', '-s', str(CONFIG_ACK_TYPE)],
                 input=json.dumps(envelope, separators=(',', ':')), timeout=5)
    return result.returncode == 0



# Shared validation is also used before local changes or broadcasting.

def validate_package(pkg):
    """Validate version, mesh settings, activation time and rollback flag."""
    if not isinstance(pkg, dict):
        return False, "package is not an object"
    if pkg.get("kind") not in (None, "mesh_config"):
        return False, "not a mesh_config package"
    version = pkg.get("version")
    # Version IDs are lowercase hexadecimal strings of 6-64 characters.
    if (not isinstance(version, str)
            or not re.fullmatch(r"[0-9a-f]{6,64}", version)):
        return False, "missing or malformed version"
    config = strip_local_keys(pkg.get("config"))
    if not isinstance(config, dict) or not config:
        return False, "missing config block"

    current = {}
    try:
        with open(os.environ.get('MANET_MESH_CONF', '/etc/mesh.conf')) as source:
            for line in source:
                key, sep, value = line.strip().partition('=')
                if sep:
                    current[key] = value
    except FileNotFoundError:
        pass
    ok, why = validate_config(config, current=current)
    if not ok:
        return False, why

    # Zero stages only. Positive activation times use Unix seconds.
    activate_at = pkg.get("activate_at", 0)
    if not isinstance(activate_at, int) or activate_at < 0:
        return False, "malformed activate_at"
    if not isinstance(pkg.get("no_rollback", False), bool):
        return False, "no_rollback must be a boolean"
    return True, ""


# Alfred

def latest_config_package(raw=None):
    """Return the newest authenticated type 70 message, or None.

    Alfred returns one record per publisher. Choose the newest issue so stale
    copies cannot override a later change.
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

def local_config_journal():
    """Use the same durable state for manager and transient worker passes."""
    return ConfigJournal(JOURNAL_DIR, PENDING_FILE, ACK_VERSION_FILE,
                         APPLIED_VERSION_FILE)


def advance_config_job(journal):
    """ACK staging, then authorize local work without reauthenticating it."""
    job = journal.job
    if not job:
        return False
    if (job['phase'] != 'staged'
            and job.get('reported_phase') != job['phase'] and clock_ready()):
        try:
            reported = publish_ack(job['package']['version'], job['phase'])
        except Exception:
            reported = False
        if reported:
            job['reported_phase'] = job['phase']
            journal.save()
    if job['phase'] == 'staged' and clock_ready():
        try:
            acknowledged = publish_ack(job['package']['version'])
        except Exception:
            acknowledged = False
        if acknowledged:
            journal.acknowledge()
    ready = journal.authorize(ADMIN)
    journal.project()
    return ready


def launch_config_job():
    """Let the existing independent unit own restarts and trial recovery."""
    r = run(APPLY_COMMAND, timeout=180)
    if r.returncode:
        log('Local config job remains pending after an apply failure')
        return 1
    return 0


def sync_once(raw=None):
    """Resume local work first, then consider a fresh authenticated order."""
    journal = local_config_journal()
    try:
        with journal.locked():
            journal.import_pending(validate_package, ADMIN)
            journal.retire_legacy()
            resume = (advance_config_job(journal) if journal.job
                      and journal.job['phase'] != 'staged' else False)
        if resume:
            # Ciphertext expiry, password rotation and missing time sync do
            # not revoke an activation that already entered the journal.
            return launch_config_job()
        message = (latest_config_package() if raw is None
                   else latest_config_package(raw))
        with journal.locked():
            if message:
                package = message.payload
                if package.get('kind') == 'mesh_config_cancel':
                    journal.cancel(message, ADMIN)
                    return 0
                ok, why = validate_package(package)
                if ok:
                    package = dict(package, config=strip_local_keys(
                        package['config']))
                    journal.stage(package, message, ADMIN)
                else:
                    log(f'Ignoring config package: {why}')
            # Consider a newer cancel/replacement before activating staging.
            ready = advance_config_job(journal)
        return launch_config_job() if ready else 0
    except BlockingIOError:
        # The transient worker owns the job. A later manager cycle resumes.
        return 0


def main():
    """Run a sync pass or import legacy state before an update reboot."""
    action = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if action not in {"sync", "migrate"}:
        print(f"usage: {os.path.basename(sys.argv[0])} {{sync|migrate}}",
              file=sys.stderr)
        return 2
    try:
        if action == "migrate":
            journal = local_config_journal()
            with journal.locked():
                journal.import_pending(validate_package, ADMIN, strict=True)
                journal.retire_legacy()
            return 0
        raw = sys.stdin.read() if '--stdin' in sys.argv[2:] else None
        return sync_once(raw)
    except Exception as e:
        log(f"sync error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
