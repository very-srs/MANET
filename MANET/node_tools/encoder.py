#!/usr/bin/env python3
"""Encode an Alfred payload for this node.

Two subcommands, matching the two Alfred types:

  encoder.py identity   -> NodeIdentity  (type 67, republished slowly)
  encoder.py telemetry  -> NodeTelemetry (types 68 and 69, every cycle)

Both print base64 on stdout. Splitting them keeps the ~90 bytes of unchanging
identity out of the packet that repeats every few minutes.
"""

import sys
import base64
import argparse
import json

import NodeInfo_pb2
from manet_ids import mac_to_bytes, syncthing_id_to_bytes, ipv4_to_int


def emit(message):
    print(base64.b64encode(message.SerializeToString()).decode('utf-8'), end='')


def build_identity(args):
    ident = NodeInfo_pb2.NodeIdentity()
    ident.hostname = args.hostname

    # The first MAC is br0's, which is also the key Alfred stamps on every
    # record this node publishes (it runs `-i br0`). Publishing it again would
    # just be paying twice for the same six bytes, so it is dropped here and
    # put back by decoder.py from the record key.
    for mac in args.mac_addresses[1:]:
        raw = mac_to_bytes(mac)
        if raw:
            ident.mac_addresses.append(raw)

    if args.syncthing_id:
        raw = syncthing_id_to_bytes(args.syncthing_id)
        if raw:
            ident.syncthing_id = raw
        else:
            print(f'warning: unparseable syncthing id {args.syncthing_id!r}',
                  file=sys.stderr)

    ident.ipv4_chunk = args.ipv4_chunk
    ident.ipv4_address = ipv4_to_int(args.ipv4_address)
    return ident


def build_telemetry(args):
    t = NodeInfo_pb2.NodeTelemetry()

    t.mean_throughput_mbps = args.mean_throughput_mbps
    t.is_internet_gateway = args.is_internet_gateway
    t.gateway_iface = args.gateway_iface

    t.is_mumble_server = args.is_mumble_server
    t.is_ntp_server = args.is_ntp_server
    t.is_tak_server = args.is_tak_server
    t.is_mediamtx_server = args.is_mediamtx_server

    t.uptime_seconds = args.uptime_seconds
    t.battery_percentage = args.battery_percentage
    t.cpu_load_average = args.cpu_load_average

    # Only set with an actual fix; gps-reader reports 0/0 when it has none.
    if args.latitude != 0.0 or args.longitude != 0.0:
        t.location.latitude_e7 = int(round(args.latitude * 1e7))
        t.location.longitude_e7 = int(round(args.longitude * 1e7))
        t.location.altitude_m = int(round(args.altitude))
    if args.atak_user:
        t.atak_user = args.atak_user

    t.data_channel_2_4 = args.data_channel_2_4
    t.data_channel_5_0 = args.data_channel_5_0

    t.halow_tx_mcs = args.halow_tx_mcs
    t.halow_rx_mcs = args.halow_rx_mcs
    if args.halow_mcs_peer:
        raw = mac_to_bytes(args.halow_mcs_peer)
        if raw:
            t.halow_mcs_peer = raw
    t.wifi_24_tx_mcs = args.wifi_24_tx_mcs
    t.wifi_24_rx_mcs = args.wifi_24_rx_mcs
    t.wifi_5_tx_mcs = args.wifi_5_tx_mcs
    t.wifi_5_rx_mcs = args.wifi_5_rx_mcs

    t.last_seen_timestamp = args.timestamp
    t.is_in_limp_mode = args.is_in_limp_mode
    t.last_tourguide_timestamp = args.last_tourguide_timestamp
    t.last_tourguide_radio = args.last_tourguide_radio
    t.partition_size = args.partition_size
    t.config_ack_version = args.config_ack_version

    t.node_state = {
        'ACTIVE': NodeInfo_pb2.NodeTelemetry.ACTIVE,
        'SHUTTING_DOWN': NodeInfo_pb2.NodeTelemetry.SHUTTING_DOWN,
    }.get(args.node_state, NodeInfo_pb2.NodeTelemetry.ACTIVE)

    t.eud_mode = {
        'wired': NodeInfo_pb2.NodeTelemetry.EUD_WIRED,
        'wireless': NodeInfo_pb2.NodeTelemetry.EUD_WIRELESS,
        'auto': NodeInfo_pb2.NodeTelemetry.EUD_AUTO,
    }.get(args.eud_mode, NodeInfo_pb2.NodeTelemetry.EUD_WIRED)
    t.ap_ssid = args.ap_ssid
    t.eud_count = args.eud_count

    # Structured, not a nested JSON string — see the note in NodeInfo.proto.
    try:
        for r in json.loads(args.channel_report_json).get('results', []):
            result = t.channel_report.results.add()
            result.channel = int(r.get('channel', 0))
            result.noise_floor = int(r.get('noise_floor', 0))
            result.bss_count = int(r.get('bss_count', 0))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass

    _add_interfaces(t, args.interfaces_json)
    return t


def _add_interfaces(t, interfaces_json):
    """Populate the interface list so peers render from the registry.

    Health and fault text are deliberately not published — the viewer derives
    them from role and state, rather than every node shipping prose on every
    cycle.
    """
    roles = {
        'bat': NodeInfo_pb2.NodeTelemetry.ROLE_BAT,
        'mesh': NodeInfo_pb2.NodeTelemetry.ROLE_MESH,
        'ap': NodeInfo_pb2.NodeTelemetry.ROLE_AP,
        'gateway': NodeInfo_pb2.NodeTelemetry.ROLE_GATEWAY,
        'eud-bridge': NodeInfo_pb2.NodeTelemetry.ROLE_EUD_BRIDGE,
        'bridge': NodeInfo_pb2.NodeTelemetry.ROLE_BRIDGE,
    }
    states = {
        'UP': NodeInfo_pb2.NodeTelemetry.STATE_UP,
        'DOWN': NodeInfo_pb2.NodeTelemetry.STATE_DOWN,
    }
    try:
        entries = json.loads(interfaces_json)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get('name'):
            continue
        iface = t.interfaces.add()
        iface.name = str(entry['name'])
        iface.role = roles.get(str(entry.get('role', '')).lower(),
                               NodeInfo_pb2.NodeTelemetry.ROLE_OTHER)
        iface.state = states.get(str(entry.get('state', '')).upper(),
                                 NodeInfo_pb2.NodeTelemetry.STATE_UNKNOWN)
        for addr in entry.get('ipv4', []) or []:
            packed = ipv4_to_int(addr)
            if packed:
                iface.ipv4.append(packed)
        iface.tx_mcs = str(entry.get('tx_mcs', '') or '')
        iface.rx_mcs = str(entry.get('rx_mcs', '') or '')
        iface.channel = str(entry.get('channel', '') or '')
        iface.freq_mhz = str(entry.get('freq_mhz', '') or '')
        iface.txpower_dbm = str(entry.get('txpower_dbm', '') or '')
        iface.halow_bw = str(entry.get('halow_bw', '') or '')
        iface.txpower_cap_dbm = str(entry.get('txpower_cap_dbm', '') or '')


def main():
    parser = argparse.ArgumentParser(description='Encode an Alfred payload.')
    sub = parser.add_subparsers(dest='kind', required=True)

    # ── identity (Alfred type 67) ────────────────────────────────────────────
    p = sub.add_parser('identity', help='static node identity')
    p.add_argument('--hostname', required=True)
    p.add_argument('--mac-addresses', nargs='+', type=str, required=True,
                   help="All interface MACs, br0's first (it is dropped: "
                        "Alfred's record key already carries it).")
    p.add_argument('--ipv4-address', default='')
    p.add_argument('--syncthing-id', default='')
    p.add_argument('--ipv4-chunk', type=int, default=0)

    # ── telemetry (Alfred types 68 and 69) ───────────────────────────────────
    p = sub.add_parser('telemetry', help='volatile node state')
    p.add_argument('--timestamp', type=int, required=True)

    p.add_argument('--mean-throughput-mbps', type=float, default=0.0,
                   help='Mean BATMAN_V throughput across originators, Mbit/s.')
    p.add_argument('--is-internet-gateway', action='store_true')
    p.add_argument('--gateway-iface', type=str, default='')

    p.add_argument('--is-mumble-server', action='store_true')
    p.add_argument('--is-ntp-server', action='store_true')
    p.add_argument('--is-tak-server', action='store_true')
    p.add_argument('--is-mediamtx-server', action='store_true')

    p.add_argument('--uptime-seconds', type=int, default=0)
    p.add_argument('--battery-percentage', type=int, default=0)
    p.add_argument('--cpu-load-average', type=float, default=0.0)

    p.add_argument('--latitude', type=float, default=0.0)
    p.add_argument('--longitude', type=float, default=0.0)
    p.add_argument('--altitude', type=float, default=0.0)
    p.add_argument('--atak-user', type=str, default='')

    p.add_argument('--data-channel-2-4', type=int, default=0, help='MHz')
    p.add_argument('--data-channel-5-0', type=int, default=0, help='MHz')
    p.add_argument('--channel-report-json', type=str, default='{}')

    p.add_argument('--halow-tx-mcs', type=str, default='')
    p.add_argument('--halow-rx-mcs', type=str, default='')
    p.add_argument('--halow-mcs-peer', type=str, default='')
    p.add_argument('--wifi-24-tx-mcs', type=str, default='')
    p.add_argument('--wifi-24-rx-mcs', type=str, default='')
    p.add_argument('--wifi-5-tx-mcs', type=str, default='')
    p.add_argument('--wifi-5-rx-mcs', type=str, default='')

    p.add_argument('--interfaces-json', type=str, default='[]',
                   help='[{"name","role","state","ipv4":[],"tx_mcs","rx_mcs",'
                        '"channel","freq_mhz","txpower_dbm","halow_bw",'
                        '"txpower_cap_dbm"}]')
    p.add_argument('--eud-mode', type=str, default='wired',
                   choices=['wired', 'wireless', 'auto'])
    p.add_argument('--ap-ssid', type=str, default='')
    p.add_argument('--eud-count', type=int, default=0)

    p.add_argument('--is-in-limp-mode', action='store_true')
    p.add_argument('--last-tourguide-timestamp', type=int, default=0)
    p.add_argument('--last-tourguide-radio', type=str, default='')
    p.add_argument('--partition-size', type=int, default=0)
    p.add_argument('--node-state', type=str, default='ACTIVE',
                   choices=['ACTIVE', 'SHUTTING_DOWN'])
    p.add_argument('--config-ack-version', type=str, default='')

    args = parser.parse_args()
    emit(build_identity(args) if args.kind == 'identity' else build_telemetry(args))


if __name__ == '__main__':
    main()
