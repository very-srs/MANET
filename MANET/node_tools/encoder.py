#!/usr/bin/env python3
import sys
import base64
import argparse
import json
import NodeInfo_pb2

def main():
    parser = argparse.ArgumentParser(description="Encode NodeInfo protobuf message.")

    # --- Core Identity (strings) ---
    parser.add_argument("--hostname", required=True, help="Node's hostname.")
    parser.add_argument("--mac-addresses", nargs='+', type=str, required=True, help="Node's interface MAC addresses (first is primary).")
    parser.add_argument("--ipv4-address", default="", help="Node's assigned IPv4 address.")
    parser.add_argument("--syncthing-id", default="", help="Node's Syncthing Device ID.")
    parser.add_argument("--ipv4-chunk", type=int, default=0, help="Chunk number claimed for EUD IP allocation.")

    # --- Network & Election Metrics (float, bools) ---
    parser.add_argument("--tq-average", type=float, default=0.0, help="Average TQ to other mesh nodes.")
    parser.add_argument("--is-internet-gateway", action="store_true", help="Flag if node has internet.")

    # --- Service Status (bools) ---
    parser.add_argument("--is-mumble-server", action="store_true", help="Flag if node is hosting the mumble server.")
    parser.add_argument("--is-ntp-server", action="store_true", help="Flag if node is the NTP server for the mesh.")
    parser.add_argument("--is-tak-server", action="store_true", help="Flag if the node is the TAK server.")
    parser.add_argument("--is-mediamtx-server", action="store_true", help="Flag if node is hosting the MediaMTX server.")

    # --- System Health (integers, float) ---
    parser.add_argument("--uptime-seconds", type=int, default=0)
    parser.add_argument("--battery-percentage", type=int, default=100)
    parser.add_argument("--cpu-load-average", type=float, default=0.0)

    # --- Channel Hopping Data ---
    parser.add_argument("--channel-report-json", type=str, default="{}", help="JSON string of the channel scan report.")
    parser.add_argument("--data-channel-2-4", type=str, default="", help="Current 2.4GHz data channel (for helper broadcast).")
    parser.add_argument("--data-channel-5-0", type=str, default="", help="Current 5.0GHz data channel (for helper broadcast).")

    # --- Timestamp ---
    parser.add_argument("--timestamp", type=int, required=True, help="Unix epoch timestamp of this report.")

    # --- Jamming Detection ---
    parser.add_argument("--is-in-limp-mode", action="store_true", help="Flag if node has detected jamming and is in limp mode.")

    # --- Tourguide Tracking ---
    parser.add_argument("--last-tourguide-timestamp", type=int, default=0,
                        help="Unix timestamp of last helper broadcast by this node")
    parser.add_argument("--last-tourguide-radio", type=str, default="",
                        help="Which radio (wlan0/wlan1) was used for last helper broadcast")

    # --- Node State ---
    parser.add_argument("--node-state", type=str, default="ACTIVE",
                        choices=["ACTIVE", "SHUTTING_DOWN"],
                        help="Node operational state")

    # --- Config Sync ---
    parser.add_argument("--config-ack-version", type=str, default="",
                        help="SHA-256 prefix of the pending config this node has staged (ACK signal).")
    parser.add_argument("--gateway-iface", type=str, default="", help="Upstream gateway interface name (end0, usb0, enxXXX, etc.)")
    parser.add_argument("--halow-tx-mcs", type=str, default="")
    parser.add_argument("--halow-rx-mcs", type=str, default="")
    parser.add_argument("--halow-mcs-peer", type=str, default="")
    parser.add_argument("--wifi-24-tx-mcs", type=str, default="")
    parser.add_argument("--wifi-24-rx-mcs", type=str, default="")
    parser.add_argument("--wifi-5-tx-mcs", type=str, default="")
    parser.add_argument("--wifi-5-rx-mcs", type=str, default="")

    args = parser.parse_args()

    # Create a new NodeInfo message
    node_info = NodeInfo_pb2.NodeInfo()

    # === Populate the message from the arguments ===
    node_info.hostname = args.hostname
    node_info.mac_addresses.extend(args.mac_addresses)
    node_info.ipv4_address = args.ipv4_address
    node_info.syncthing_id = args.syncthing_id
    node_info.ipv4_chunk = args.ipv4_chunk
    node_info.tq_average = args.tq_average
    node_info.is_internet_gateway = args.is_internet_gateway
    node_info.is_mumble_server = args.is_mumble_server
    node_info.is_ntp_server = args.is_ntp_server
    node_info.is_tak_server = args.is_tak_server
    node_info.is_mediamtx_server = args.is_mediamtx_server
    node_info.uptime_seconds = args.uptime_seconds
    node_info.battery_percentage = args.battery_percentage
    node_info.cpu_load_average = args.cpu_load_average

    # --- Populate Channel Data ---
    node_info.data_channel_2_4 = args.data_channel_2_4
    node_info.data_channel_5_0 = args.data_channel_5_0

    # --- Populate Timestamp ---
    node_info.last_seen_timestamp = args.timestamp

    # --- Populate Limp Mode ---
    node_info.is_in_limp_mode = args.is_in_limp_mode

    # --- Populate Tourguide Tracking ---
    node_info.last_tourguide_timestamp = args.last_tourguide_timestamp
    node_info.last_tourguide_radio = args.last_tourguide_radio

    # --- Populate Node State ---
    state_map = {
        "ACTIVE": NodeInfo_pb2.NodeInfo.ACTIVE,
        "SHUTTING_DOWN": NodeInfo_pb2.NodeInfo.SHUTTING_DOWN,
    }
    node_info.node_state = state_map.get(args.node_state, NodeInfo_pb2.NodeInfo.ACTIVE)

    # --- Populate Config ACK ---
    node_info.config_ack_version = args.config_ack_version

    # --- Populate link MCS summary ---
    node_info.gateway_iface = args.gateway_iface
    node_info.halow_tx_mcs = args.halow_tx_mcs
    node_info.halow_rx_mcs = args.halow_rx_mcs
    node_info.halow_mcs_peer = args.halow_mcs_peer
    node_info.wifi_24_tx_mcs = args.wifi_24_tx_mcs
    node_info.wifi_24_rx_mcs = args.wifi_24_rx_mcs
    node_info.wifi_5_tx_mcs = args.wifi_5_tx_mcs
    node_info.wifi_5_rx_mcs = args.wifi_5_rx_mcs

    # --- Populate Channel Scan Report ---
    try:
        report_data = json.loads(args.channel_report_json)
        results = report_data.get("results", [])
        for r in results:
            scan_result = node_info.channel_report.results.add()
            scan_result.channel     = int(r.get("channel", 0))
            scan_result.noise_floor = int(r.get("noise_floor", 0))
            scan_result.bss_count   = int(r.get("bss_count", 0))
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    # Serialize and encode as Base64
    serialized = node_info.SerializeToString()
    encoded    = base64.b64encode(serialized).decode('utf-8')
    print(encoded, end='')

if __name__ == "__main__":
    main()
