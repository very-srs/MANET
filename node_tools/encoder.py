#!/usr/bin/env python3
import sys
import base64
import argparse
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
#    parser.add_argument("--has-quorum", action="store_true", help="Flag if node is in a majority partition.")

    # --- Service Status (bools, enum) ---
    parser.add_argument("--is-mumble-server", action="store_true", help="Flag if node is hosting the mumble server.")
    parser.add_argument("--is-ntp-server", action="store_true", help="Flag if node is the NTP server for the mesh.")
    parser.add_argument("--is-tak-server", action="store_true", help="Flag if the node is the TAK server.")

    # --- System Health (integers, float) ---
    parser.add_argument("--uptime-seconds", type=int, default=0)
    parser.add_argument("--battery-percentage", type=int, default=100)
    parser.add_argument("--cpu-load-average", type=float, default=0.0)

    # --- Other ---
#    parser.add_argument("--latitude", type=float, help="GPS Latitude.")
#    parser.add_argument("--longitude", type=float, help="GPS Longitude.")
#    parser.add_argument("--altitude", type=float, help="GPS Altitude.")
#    parser.add_argument("--atak-user", help="ATAK user callsign.")

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
#    node_info.has_quorum = args.has_quorum
    node_info.is_mumble_server = args.is_mumble_server
    node_info.is_ntp_server = args.is_ntp_server
    node_info.is_tak_server = args.is_tak_server
    node_info.uptime_seconds = args.uptime_seconds
    node_info.battery_percentage = args.battery_percentage
    node_info.cpu_load_average = args.cpu_load_average
#    node_info.location.latitude = args.latitude
#    node_info.location.longitude = args.longitude
#    node_info.location.altitude = args.altitude
#    node_info.atak_user = args.atak_user

    # Serialize the message to a binary string
    serialized_message = node_info.SerializeToString()

    # Encode the binary string in Base64 and print to stdout for Alfred
    encoded_message = base64.b64encode(serialized_message).decode('utf-8')
    print(encoded_message)

if __name__ == "__main__":
    main()
