#!/usr/bin/env python3
import sys
import base64
import argparse
import NodeInfo_pb2

def main():
    parser = argparse.ArgumentParser(description="Decode NodeInfo protobuf message.")
    parser.add_argument("b64_string", help="The Base64 encoded protobuf string.")
    args = parser.parse_args()

    try:
        # Decode the Base64 string back to binary
        serialized_message = base64.b64decode(args.b64_string)

        # Create a new NodeInfo message and parse the binary data
        node_info = NodeInfo_pb2.NodeInfo()
        node_info.ParseFromString(serialized_message)

        # Print the fields in a shell-friendly "KEY=VALUE" format
        print(f"HOSTNAME='{node_info.hostname}'")
        print(f"MAC_ADDRESS='{node_info.mac_address}'")
        print(f"BAT0_MAC_ADDRESS='{node_info.bat0_mac_address}'")
        print(f"IPV4_ADDRESS='{node_info.ipv4_address}'")
        print(f"SYNCTHING_ID='{node_info.syncthing_id}'")
        print(f"TQ_AVERAGE={node_info.tq_average}")
        print(f"IS_INTERNET_GATEWAY={str(node_info.is_internet_gateway).lower()}")
#        print(f"HAS_QUORUM={str(node_info.has_quorum).lower()}")
        print(f"IS_MUMBLE_SERVER={str(node_info.is_mumble_server).lower()}")
        print(f"IS_NTP_SERVER={str(node_info.is_ntp_server).lower()}")
        print(f"IS_TAK_SERVER={str(node_info.is_tak_server).lower()}")
        print(f"UPTIME_SECONDS={node_info.uptime_seconds}")
        print(f"BATTERY_PERCENTAGE={node_info.battery_percentage}")
        print(f"CPU_LOAD_AVERAGE={node_info.cpu_load_average}")
#        print(f"LATITUDE={node_info.location.latitude}")
#        print(f"LONGITUDE={node_info.location.longitude}")
#        print(f"ALTITUDE={node_info.location.altitude}")
#        print(f"ATAK_USER='{node_info.atak_user}'")

    except Exception as e:
        print(f"Error decoding message: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()

