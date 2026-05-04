#!/usr/bin/env python3
"""
gps-reader.py — reads GPS fix from gpsd and writes /run/gps_status.json.

Runs as a daemon every POLL_INTERVAL seconds. Writes has_fix=false if
gpsd is unavailable (device not plugged in, service not running, no fix).
Designed to be robust: any error produces a safe no-fix status rather than
crashing the daemon.
"""

import json
import os
import socket
import sys
import time

GPS_STATUS_PATH = "/run/gps_status.json"
GPSD_HOST = "127.0.0.1"
GPSD_PORT = 2947
POLL_INTERVAL = 5   # seconds between gpsd queries
GPSD_TIMEOUT = 10   # seconds to wait for a TPV message


def write_status(has_fix: bool, lat: float = 0.0, lon: float = 0.0,
                 alt: float = 0.0, hdop: float = 99.9) -> None:
    status = {
        "has_fix":   has_fix,
        "latitude":  round(lat, 7) if has_fix else 0.0,
        "longitude": round(lon, 7) if has_fix else 0.0,
        "altitude":  round(alt, 2) if has_fix else 0.0,
        "hdop":      round(hdop, 2),
        "timestamp": int(time.time()),
    }
    tmp = GPS_STATUS_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(status, f)
        os.rename(tmp, GPS_STATUS_PATH)
    except OSError as e:
        print(f"[gps-reader] write error: {e}", file=sys.stderr, flush=True)


def query_gpsd() -> dict | None:
    """
    Connect to gpsd, enable JSON watch, and return the first TPV message.
    Returns None if gpsd is unreachable or no TPV arrives within timeout.
    """
    try:
        sock = socket.create_connection((GPSD_HOST, GPSD_PORT), timeout=5)
    except (OSError, ConnectionRefusedError):
        return None

    try:
        with sock:
            sock.settimeout(GPSD_TIMEOUT)
            rf = sock.makefile("r")
            rf.readline()  # discard VERSION banner
            sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
            deadline = time.monotonic() + GPSD_TIMEOUT
            while time.monotonic() < deadline:
                try:
                    line = rf.readline()
                except socket.timeout:
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if msg.get("class") == "TPV":
                    return msg
    except (OSError, socket.timeout):
        pass
    return None


def main() -> None:
    def log(msg: str) -> None:
        print(f"[gps-reader] {msg}", flush=True)

    log("Starting GPS reader daemon.")

    while True:
        try:
            tpv = query_gpsd()

            if tpv is None:
                # gpsd not running or device not present
                write_status(has_fix=False)
            else:
                # TPV mode: 0=unknown, 1=no fix, 2=2-D fix, 3=3-D fix
                mode = tpv.get("mode", 0)
                has_fix = mode >= 2
                write_status(
                    has_fix=has_fix,
                    lat=float(tpv.get("lat", 0.0)),
                    lon=float(tpv.get("lon", 0.0)),
                    alt=float(tpv.get("alt", 0.0)),
                    hdop=float(tpv.get("hdop", 99.9)),
                )
        except Exception as e:
            print(f"[gps-reader] unexpected error: {e}", file=sys.stderr, flush=True)
            write_status(has_fix=False)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
