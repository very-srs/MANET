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

    args = parser.parse_args()

    # Create a new NodeInfo message
    node_info = NodeInfo_pb2.NodeInfo()

    # === Populate the message from the arguments ===
    node_info.hostname = args.hostname
    node_info.mac_addresses.extend(args.mac_addresses)
    node_info.ipv4_address = args.ipv4_address
    node_info.syncthing_id = args.syncthing_id
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
        "SHUTTING_DOWN": NodeInfo_pb2.NodeInfo.SHUTTING_DOWN
    }
    node_info.node_state = state_map.get(args.node_state, NodeInfo_pb2.NodeInfo.ACTIVE)
    
    try:
        if args.channel_report_json and args.channel_report_json != "{}":
            report_data = json.loads(args.channel_report_json)
            if "results" in report_data:
                for result in report_data["results"]:
                    scan_result = node_info.channel_report.results.add()
                    scan_result.channel = result.get("channel", 0)
                    scan_result.noise_floor = result.get("noise_floor", 0)
                    scan_result.bss_count = result.get("bss_count", 0)
    except Exception as e:
        print(f"Error parsing channel-report-json: {e}", file=sys.stderr)

    # Serialize the message to a binary string
    serialized_message = node_info.SerializeToString()

    # Encode the binary string in Base64 and print to stdout for Alfred
    encoded_message = base64.b64encode(serialized_message).decode('utf-8')
    print(encoded_message)

if __name__ == "__main__":
    main()
