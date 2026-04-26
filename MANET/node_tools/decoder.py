#!/usr/bin/env python3
import sys
import base64
import argparse
import json
import NodeInfo_pb2


def format_shell_float(value, decimals=2):
    return f"{float(value):.{decimals}f}"


def main():
    parser = argparse.ArgumentParser(description="Decode NodeInfo protobuf message.")
    parser.add_argument("b64_string", help="The Base64 encoded protobuf string.")
    args = parser.parse_args()

    try:
        serialized_message = base64.b64decode(args.b64_string)

        node_info = NodeInfo_pb2.NodeInfo()
        node_info.ParseFromString(serialized_message)

        print(f"HOSTNAME='{node_info.hostname}'")
        if node_info.mac_addresses:
            print(f"MAC_ADDRESS='{node_info.mac_addresses[0]}'")
        else:
            print(f"MAC_ADDRESS=''")
        print(f"MAC_ADDRESSES='{','.join(node_info.mac_addresses)}'")
        print(f"IPV4_ADDRESS='{node_info.ipv4_address}'")
        print(f"IPV4_CHUNK={node_info.ipv4_chunk}")
        print(f"SYNCTHING_ID='{node_info.syncthing_id}'")
        print(f"TQ_AVERAGE={node_info.tq_average}")
        print(f"IS_INTERNET_GATEWAY={str(node_info.is_internet_gateway).lower()}")
        print(f"GATEWAY_IFACE='{node_info.gateway_iface}'")
        print(f"IS_MUMBLE_SERVER={str(node_info.is_mumble_server).lower()}")
        print(f"IS_NTP_SERVER={str(node_info.is_ntp_server).lower()}")
        print(f"IS_TAK_SERVER={str(node_info.is_tak_server).lower()}")
        print(f"IS_MEDIAMTX_SERVER={str(node_info.is_mediamtx_server).lower()}")
        print(f"UPTIME_SECONDS={node_info.uptime_seconds}")
        print(f"BATTERY_PERCENTAGE={node_info.battery_percentage}")
        print(f"CPU_LOAD_AVERAGE={format_shell_float(node_info.cpu_load_average)}")
        print(f"DATA_CHANNEL_2_4='{node_info.data_channel_2_4}'")
        print(f"DATA_CHANNEL_5_0='{node_info.data_channel_5_0}'")
        print(f"LAST_SEEN_TIMESTAMP={node_info.last_seen_timestamp}")
        print(f"IS_IN_LIMP_MODE={str(node_info.is_in_limp_mode).lower()}")
        print(f"LAST_TOURGUIDE_TIMESTAMP={node_info.last_tourguide_timestamp}")
        print(f"LAST_TOURGUIDE_RADIO='{node_info.last_tourguide_radio}'")

        state_names = {0: "ACTIVE", 1: "SHUTTING_DOWN"}
        print(f"NODE_STATE='{state_names.get(node_info.node_state, 'ACTIVE')}'")

        # Config sync ACK
        print(f"CONFIG_ACK_VERSION='{node_info.config_ack_version}'")
        print(f"HALOW_TX_MCS='{node_info.halow_tx_mcs}'")
        print(f"HALOW_RX_MCS='{node_info.halow_rx_mcs}'")
        print(f"HALOW_MCS_PEER='{node_info.halow_mcs_peer}'")
        print(f"WIFI_24_TX_MCS='{node_info.wifi_24_tx_mcs}'")
        print(f"WIFI_24_RX_MCS='{node_info.wifi_24_rx_mcs}'")
        print(f"WIFI_5_TX_MCS='{node_info.wifi_5_tx_mcs}'")
        print(f"WIFI_5_RX_MCS='{node_info.wifi_5_rx_mcs}'")

        report_list = []
        for result in node_info.channel_report.results:
            report_list.append({
                "channel":     result.channel,
                "noise_floor": result.noise_floor,
                "bss_count":   result.bss_count
            })
        report_json = json.dumps({"results": report_list})
        print(f"CHANNEL_REPORT_JSON='{report_json}'")

    except Exception as e:
        print(f"Error decoding message: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
