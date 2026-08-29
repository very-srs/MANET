#!/usr/bin/env python3
"""Decode an Alfred payload into shell-sourceable KEY='value' lines.

  decoder.py identity  <base64> --node-mac <mac>
  decoder.py telemetry <base64>

The node MAC is Alfred's record key for the payload (mesh-registry-builder
reads it from the `-r` output). Identity payloads omit the node's own primary
MAC because that key already carries it, so it is passed back in here to
reassemble MAC_ADDRESS / MAC_ADDRESSES.
"""

import sys
import base64
import argparse
import json

import NodeInfo_pb2
from manet_ids import bytes_to_mac, bytes_to_syncthing_id, int_to_ipv4


def shell_quote(value):
    """Single-quote for `eval`/source safety: ' -> '\\''."""
    return str(value).replace("'", "'\\''")


def emit(key, value):
    print(f"{key}='{shell_quote(value)}'")


def emit_raw(key, value):
    """Unquoted, for numeric fields the shell scripts compare arithmetically."""
    print(f"{key}={value}")


def decode_identity(raw, node_mac):
    ident = NodeInfo_pb2.NodeIdentity()
    ident.ParseFromString(raw)

    macs = [m for m in (bytes_to_mac(b) for b in ident.mac_addresses) if m]
    if node_mac:
        macs.insert(0, node_mac.lower())

    emit('HOSTNAME', ident.hostname)
    emit('MAC_ADDRESS', macs[0] if macs else '')
    emit('MAC_ADDRESSES', ','.join(macs))
    emit('IPV4_ADDRESS', int_to_ipv4(ident.ipv4_address))
    emit_raw('IPV4_CHUNK', ident.ipv4_chunk)
    emit('SYNCTHING_ID', bytes_to_syncthing_id(ident.syncthing_id))


def decode_telemetry(raw):
    t = NodeInfo_pb2.NodeTelemetry()
    t.ParseFromString(raw)

    # float32 round-trips as 43.20000076..., which is noise in a registry file.
    emit_raw('MEAN_THROUGHPUT_MBPS', f'{t.mean_throughput_mbps:.2f}')
    emit_raw('IS_INTERNET_GATEWAY', str(t.is_internet_gateway).lower())
    emit('GATEWAY_IFACE', t.gateway_iface)

    emit_raw('IS_MUMBLE_SERVER', str(t.is_mumble_server).lower())
    emit_raw('IS_NTP_SERVER', str(t.is_ntp_server).lower())
    emit_raw('IS_TAK_SERVER', str(t.is_tak_server).lower())
    emit_raw('IS_MEDIAMTX_SERVER', str(t.is_mediamtx_server).lower())

    emit_raw('UPTIME_SECONDS', t.uptime_seconds)
    # proto3 cannot tell 0 from unset for a scalar, and these boards have no
    # battery reader yet — publishing 0 would paint every node with a red
    # critical-battery badge. Absent means unknown, which renders as "—".
    emit_raw('BATTERY_PERCENTAGE', t.battery_percentage or '')
    emit_raw('CPU_LOAD_AVERAGE', f'{t.cpu_load_average:.2f}')

    if t.HasField('location'):
        emit('GPS_LATITUDE', f'{t.location.latitude_e7 / 1e7:.7f}'.rstrip('0').rstrip('.'))
        emit('GPS_LONGITUDE', f'{t.location.longitude_e7 / 1e7:.7f}'.rstrip('0').rstrip('.'))
        emit('GPS_ALTITUDE', f'{float(t.location.altitude_m):.2f}')
    else:
        emit('GPS_LATITUDE', '')
        emit('GPS_LONGITUDE', '')
        emit('GPS_ALTITUDE', '')
    emit('ATAK_USER', t.atak_user)

    # Kept as strings: every consumer compares these against frequencies read
    # from wpa_supplicant configs, which are text.
    emit('DATA_CHANNEL_2_4', t.data_channel_2_4 or '')
    emit('DATA_CHANNEL_5_0', t.data_channel_5_0 or '')

    emit('HALOW_TX_MCS', t.halow_tx_mcs)
    emit('HALOW_RX_MCS', t.halow_rx_mcs)
    emit('HALOW_MCS_PEER', bytes_to_mac(t.halow_mcs_peer))
    emit('WIFI_24_TX_MCS', t.wifi_24_tx_mcs)
    emit('WIFI_24_RX_MCS', t.wifi_24_rx_mcs)
    emit('WIFI_5_TX_MCS', t.wifi_5_tx_mcs)
    emit('WIFI_5_RX_MCS', t.wifi_5_rx_mcs)

    emit_raw('LAST_SEEN_TIMESTAMP', t.last_seen_timestamp)
    emit_raw('IS_IN_LIMP_MODE', str(t.is_in_limp_mode).lower())
    emit_raw('LAST_TOURGUIDE_TIMESTAMP', t.last_tourguide_timestamp)
    emit('LAST_TOURGUIDE_RADIO', t.last_tourguide_radio)
    emit_raw('PARTITION_SIZE', t.partition_size)
    emit('CONFIG_ACK_VERSION', t.config_ack_version)

    emit('NODE_STATE', {0: 'ACTIVE', 1: 'SHUTTING_DOWN'}.get(t.node_state, 'ACTIVE'))
    emit('EUD_MODE', {0: 'wired', 1: 'wireless', 2: 'auto'}.get(t.eud_mode, 'wired'))
    emit('AP_SSID', t.ap_ssid)
    emit_raw('EUD_COUNT', t.eud_count)

    emit('CHANNEL_REPORT_JSON', json.dumps({'results': [
        {'channel': r.channel, 'noise_floor': r.noise_floor, 'bss_count': r.bss_count}
        for r in t.channel_report.results
    ]}))

    role_names = {0: 'other', 1: 'bat', 2: 'mesh', 3: 'ap',
                  4: 'gateway', 5: 'eud-bridge', 6: 'bridge'}
    state_names = {0: 'UNKNOWN', 1: 'UP', 2: 'DOWN'}
    emit('INTERFACES_JSON', json.dumps([
        {
            'name': i.name,
            'role': role_names.get(i.role, 'other'),
            'state': state_names.get(i.state, 'UNKNOWN'),
            'ipv4': [int_to_ipv4(a) for a in i.ipv4],
            'tx_mcs': i.tx_mcs,
            'rx_mcs': i.rx_mcs,
            'channel': i.channel,
            'freq_mhz': i.freq_mhz,
            'txpower_dbm': i.txpower_dbm,
            'halow_bw': i.halow_bw,
        }
        for i in t.interfaces
    ]))


def main():
    parser = argparse.ArgumentParser(description='Decode an Alfred payload.')
    parser.add_argument('kind', choices=['identity', 'telemetry'])
    parser.add_argument('b64_string')
    parser.add_argument('--node-mac', default='',
                        help="Alfred's record key for this payload (identity only).")
    args = parser.parse_args()

    try:
        raw = base64.b64decode(args.b64_string)
        if args.kind == 'identity':
            decode_identity(raw, args.node_mac)
        else:
            decode_telemetry(raw)
    except Exception as e:
        print(f'Error decoding message: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
