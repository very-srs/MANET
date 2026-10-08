#!/usr/bin/env python3
"""
Read GPS fixes from gpsd and write /run/gps_status.json.

Holds one gpsd watch open and passes every message to manet_gnss.GnssReader.
Writes the status file once a second and the primary receiver's recent
history every HISTORY_INTERVAL seconds, and both again when the gpsd
session ends. Writes has_fix=false while gpsd is unavailable or the
primary receiver has no live fix.

The original status keys (has_fix, latitude, longitude, altitude, hdop,
timestamp) are kept for existing readers. altitude is altMSL, else gpsd's
older alt, else 0. timestamp is the wall time of the fix's arrival,
derived from its raw age so a wall clock correction cannot make a fresh
fix look old.

The first fix of this OS boot is recorded in /run/gps_first_fix.json.
/run is emptied at boot, so a reader restart still knows a fix was had.
"""

import json
import os
import socket
import sys
import time

from manet_gnss import GnssReader

RUNTIME_DIR = os.environ.get("MANET_GPS_RUNTIME_DIR", "/run")
GPS_STATUS_PATH = os.path.join(RUNTIME_DIR, "gps_status.json")
GPS_HISTORY_PATH = os.path.join(RUNTIME_DIR, "gps_history.json")
GPS_FIRST_FIX_PATH = os.path.join(RUNTIME_DIR, "gps_first_fix.json")
GPSD_HOST = "127.0.0.1"
GPSD_PORT = 2947
WRITE_INTERVAL = 1     # seconds between status writes
HISTORY_INTERVAL = 5   # seconds between history writes
RECONNECT_S = 5
MAX_PENDING = 65536    # bytes without a newline before the stream is resynchronised
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def raw_mono() -> float:
    return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)


def read_boot_id() -> str:
    try:
        with open(BOOT_ID_PATH) as f:
            return f.read().strip()
    except OSError:
        return ""


def write_json(path: str, data: dict) -> bool:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(json.dumps(data, separators=(",", ":")))
        os.rename(tmp, path)
        return True
    except OSError as e:
        print(f"[gps-reader] write error {path}: {e}", file=sys.stderr, flush=True)
        return False


def load_first_fix(boot_id: str) -> float | None:
    """This boot's first-fix raw time, if an earlier reader recorded one."""
    try:
        with open(GPS_FIRST_FIX_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("boot_id") != boot_id:
        return None
    mono = data.get("mono")
    return float(mono) if isinstance(mono, (int, float)) else None


def build_status(reader: GnssReader, now_mono: float, boot_id: str,
                 connected: bool = True, now_wall: float | None = None,
                 now_boot: float | None = None) -> dict:
    """The status document. Pure apart from the clock, so tests can drive it."""
    now_wall = time.time() if now_wall is None else now_wall
    now_boot = time.clock_gettime(time.CLOCK_BOOTTIME) if now_boot is None else now_boot
    rx = reader.tick(now_mono)
    fix = rx.last_fix if rx is not None else None
    fresh = connected and rx is not None and rx.fix_live and fix is not None
    sample = fix if fresh else (rx.last if rx is not None else None)
    alt = None
    if fresh:
        alt = fix.get("alt_msl") if fix.get("alt_msl") is not None else fix.get("alt_legacy")
    hdop = sample.get("hdop") if sample else None
    first = reader.boot_first_fix
    return {
        "has_fix": fresh,
        "latitude": round(fix["lat"], 7) if fresh else 0.0,
        "longitude": round(fix["lon"], 7) if fresh else 0.0,
        "altitude": round(alt, 2) if alt is not None else 0.0,
        "hdop": round(hdop, 2) if hdop is not None else 99.9,
        "timestamp": int(now_wall - (now_mono - fix["mono"])) if fresh else int(now_wall),
        "schema": 2,
        # When this file was written, on the boot clock (/proc/uptime). Other
        # processes judge its age with this, not with timestamp, so a time
        # sync stepping the wall clock cannot make a live fix look stale.
        "written_boot": round(now_boot, 3),
        "clock": "monotonic_raw",
        "boot_id": boot_id,
        "now_mono": round(now_mono, 3),
        "gpsd": connected,
        "devices": sorted(reader.devices) if reader.devices_known else None,
        "device": rx.device if rx is not None else None,
        "first_fix_mono": round(first, 3) if first is not None else None,
        "settled": bool(rx is not None and rx.settled(now_mono)),
        "time_ok": bool(fresh and rx.time_ok(now_mono)),
        "sample": sample,
        "events": list(reader.events),
    }


def build_history(reader: GnssReader, now_mono: float, boot_id: str) -> dict:
    rx = reader.receivers.get(reader.primary)
    return {"boot_id": boot_id, "clock": "monotonic_raw", "now_mono": round(now_mono, 3),
            "device": reader.primary, "samples": list(rx.history) if rx else []}


class Publisher:
    def __init__(self, reader: GnssReader, boot_id: str, first_fix_saved: bool = False):
        self.reader = reader
        self.boot_id = boot_id
        self.last_write = self.last_history = -1e18
        self.first_fix_saved = first_fix_saved

    def maybe_write(self, now: float, connected: bool = True, force: bool = False) -> None:
        if not self.first_fix_saved and self.reader.boot_first_fix is not None:
            self.first_fix_saved = write_json(GPS_FIRST_FIX_PATH, {
                "boot_id": self.boot_id, "mono": self.reader.boot_first_fix})
        if force or now - self.last_write >= WRITE_INTERVAL:
            write_json(GPS_STATUS_PATH, build_status(self.reader, now, self.boot_id, connected))
            self.last_write = now
        if force or now - self.last_history >= HISTORY_INTERVAL:
            write_json(GPS_HISTORY_PATH, build_history(self.reader, now, self.boot_id))
            self.last_history = now


def watch(reader: GnssReader, pub: Publisher) -> None:
    """Run one gpsd session until it fails, then publish its final state."""
    sock = socket.create_connection((GPSD_HOST, GPSD_PORT), timeout=5)
    reader.new_session()
    try:
        with sock:
            sock.settimeout(WRITE_INTERVAL)
            sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
            buf = b""
            discarding = False  # inside an oversized line: drop through its newline
            while True:
                try:
                    chunk = sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("gpsd closed the connection")
                    buf += chunk
                except socket.timeout:
                    pass
                lines = buf.split(b"\n")
                buf = lines.pop()
                if discarding and lines:
                    lines.pop(0)
                    discarding = False
                if len(buf) > MAX_PENDING:
                    print("[gps-reader] oversized report dropped", file=sys.stderr, flush=True)
                    buf = b""
                    discarding = True
                for line in lines:
                    if len(line) > MAX_PENDING:
                        continue
                    try:
                        msg = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if isinstance(msg, dict):
                        reader.feed(msg, raw_mono())
                pub.maybe_write(raw_mono())
    finally:
        now = raw_mono()
        reader.lose_all(now, "gpsd session ended")
        pub.maybe_write(now, connected=False, force=True)


def main() -> None:
    print("[gps-reader] Starting GPS reader daemon.", flush=True)
    boot_id = read_boot_id()
    first_fix = load_first_fix(boot_id)
    reader = GnssReader(first_fix_mono=first_fix)
    pub = Publisher(reader, boot_id, first_fix_saved=first_fix is not None)
    while True:
        try:
            watch(reader, pub)
        except Exception as e:
            print(f"[gps-reader] gpsd session ended: {e}", file=sys.stderr, flush=True)
            pub.maybe_write(raw_mono(), connected=False, force=True)
        time.sleep(RECONNECT_S)


if __name__ == "__main__":
    main()
