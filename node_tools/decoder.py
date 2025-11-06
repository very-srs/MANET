#!/usr/bin/env python3
import sys
import base64
import argparse
import json
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
        if node_info.mac_addresses:
            print(f"MAC_ADDRESS='{node_info.mac_addresses[0]}'")
        else:
            print(f"MAC_ADDRESS=''")
        print(f"MAC_ADDRESSES='{','.join(node_info.mac_addresses)}'")
        print(f"IPV4_ADDRESS='{node_info.ipv4_address}'")
        print(f"SYNCTHING_ID='{node_info.syncthing_id}'")
        print(f"TQ_AVERAGE={node_info.tq_average}")
        print(f"IS_INTERNET_GATEWAY={str(node_info.is_internet_gateway).lower()}")
        print(f"IS_MUMBLE_SERVER={str(node_info.is_mumble_server).lower()}")
        print(f"IS_NTP_SERVER={str(node_info.is_ntp_server).lower()}")
        print(f"IS_TAK_SERVER={str(node_info.is_tak_server).lower()}")
        print(f"IS_MEDIAMTX_SERVER={str(node_info.is_mediamtx_server).lower()}")
        print(f"UPTIME_SECONDS={node_info.uptime_seconds}")
        print(f"BATTERY_PERCENTAGE={node_info.battery_percentage}")
        print(f"CPU_LOAD_AVERAGE={node_info.cpu_load_average}")
        
        # --- Print Channel Data ---
        print(f"DATA_CHANNEL_2_4='{node_info.data_channel_2_4}'")
        print(f"DATA_CHANNEL_5_0='{node_info.data_channel_5_0}'")
        
        # --- Print Timestamp ---
        print(f"LAST_SEEN_TIMESTAMP={node_info.last_seen_timestamp}")
        
        # --- Print Limp Mode ---
        print(f"IS_IN_LIMP_MODE={str(node_info.is_in_limp_mode).lower()}")
        
        # --- Print Tourguide Tracking ---
        print(f"LAST_TOURGUIDE_TIMESTAMP={node_info.last_tourguide_timestamp}")
        print(f"LAST_TOURGUIDE_RADIO='{node_info.last_tourguide_radio}'")
        
        # --- Print Node State ---
        state_names = {
            0: "ACTIVE",
            1: "SHUTTING_DOWN"
        }
        print(f"NODE_STATE='{state_names.get(node_info.node_state, 'ACTIVE')}'")

        # Re-serialize channel report to JSON for shell consumption
        report_list = []
        for result in node_info.channel_report.results:
            report_list.append({
                "channel": result.channel,
                "noise_floor": result.noise_floor,
                "bss_count": result.bss_count
            })
        report_json = json.dumps({"results": report_list})
        # Use single quotes around the JSON for safety in shell
        print(f"CHANNEL_REPORT_JSON='{report_json}'")

    except Exception as e:
        print(f"Error decoding message: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
