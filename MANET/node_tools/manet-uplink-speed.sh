#!/bin/bash
#
# manet-uplink-speed.sh: measure an Ethernet uplink's internet download speed
# and announce it as this node's BATMAN gateway bandwidth.
#
#   manet-uplink-speed.sh measure IFACE    exit 0 when IFACE reaches the internet
#                                          (Mbit/s on stdout), 1 when it does not,
#                                          2 when IFACE is not measured (metered),
#                                          3 when a recent failure stands (no retest)
#   manet-uplink-speed.sh announce IFACE   batctl gw_mode server with IFACE's
#                                          measured bandwidth, or the default
#   manet-uplink-speed.sh forget           drop the measurement (uplink gone)
#   manet-uplink-speed.sh is-ethernet IFACE
#
# Only Ethernet uplinks are measured. A phone tether or cellular modem pays for
# the data, so those keep batman's default 10/2 Mbit/s and the caller's normal
# internet probe. For Ethernet the test doubles as the internet check: a
# captive portal or a filtered network cannot complete the HTTPS download, and
# such a node must not announce itself as a gateway.
#
# The test downloads 5 MB at most, once per uplink: the result is kept while
# the interface keeps the same address and router, and dropped on demotion.
# Only download is measured; upload is announced as a fifth of it, batman's
# usual ratio, and nothing selects on it.

set -u

RUN_DIR="${MANET_RUN_DIR:-/run}"
RESULT_FILE="$RUN_DIR/manet-uplink-speed"
FAIL_FILE="$RUN_DIR/manet-uplink-speed.failed"
LOCK_FILE="$RUN_DIR/manet-uplink-speed.lock"
TEST_BYTES=5000000
TEST_TIMEOUT="${MANET_SPEED_TIMEOUT:-15}"
# A failed test is not repeated for this long; each reconcile pass asks.
RETRY_SECS="${MANET_SPEED_RETRY_SECS:-60}"
DEFAULT_BANDWIDTH=10000kbit/2000kbit
SYS_NET="${MANET_SYS_NET:-/sys/class/net}"
UPTIME_FILE="${MANET_UPTIME_FILE:-/proc/uptime}"

log() {
    printf '%s\n' "uplink-speed: $*" >&2
}

# Drivers of phone tethers and cellular modems: metered, never tested.
is_ethernet() {
    local iface="$1" driver
    [ -e "$SYS_NET/$iface/device" ] || return 1
    [ -d "$SYS_NET/$iface/wireless" ] && return 1
    [ "$(cat "$SYS_NET/$iface/type" 2>/dev/null)" = 1 ] || return 1
    case "$iface" in wlan*|usb*|wwan*) return 1 ;; esac
    driver=$(basename "$(readlink "$SYS_NET/$iface/device/driver" 2>/dev/null)" 2>/dev/null)
    case "$driver" in
        rndis_host|ipheth|cdc_ether|cdc_ncm|cdc_mbim|huawei_cdc_ncm|qmi_wwan|cdc_subset) return 1 ;;
    esac
    return 0
}

# The uplink identity a result belongs to: interface, address and router.
uplink_key() {
    local iface="$1" ip gw
    ip=$(ip -4 -o addr show dev "$iface" 2>/dev/null | awk '{split($4, a, "/"); print a[1]; exit}')
    gw=$(ip -4 route show default dev "$iface" 2>/dev/null | awk '{print $3; exit}')
    printf '%s\n' "$iface ${ip:-none} ${gw:-none}"
}

field() {
    awk -F= -v k="$2" '$1 == k {print $2; exit}' "$1" 2>/dev/null
}

# One download, at most TEST_BYTES. Prints Mbit/s over the transfer itself
# (DNS, TCP and TLS setup excluded) when every byte arrived.
download() {
    local iface="$1" url="$2" expect="$3" out code size start total
    shift 3
    out=$(curl --interface "$iface" -s -o /dev/null -m "$TEST_TIMEOUT" "$@" \
               -w '%{http_code} %{size_download} %{time_starttransfer} %{time_total}' \
               "$url" 2>/dev/null) || return 1
    read -r code size start total <<< "$out"
    [ "$code" = "$expect" ] && [ "$size" = "$TEST_BYTES" ] || return 1
    [[ "$start" =~ ^[0-9]+(\.[0-9]+)?$ && "$total" =~ ^[0-9]+(\.[0-9]+)?$ ]] || return 1
    awk -v b="$size" -v s="$start" -v t="$total" \
        'BEGIN { d = t - s; if (d < 0.001) d = 0.001; printf "%.1f\n", b * 8 / d / 1000000 }'
}

measure() {
    local iface="$1" key now mbps
    is_ethernet "$iface" || { log "$iface is not an Ethernet uplink; not measured"; return 2; }
    exec 8>>"$LOCK_FILE"
    flock -w $((TEST_TIMEOUT * 2 + 5)) 8 || return 1
    key=$(uplink_key "$iface")
    if [ "$(field "$RESULT_FILE" KEY)" = "$key" ]; then
        field "$RESULT_FILE" DOWN_MBPS
        return 0
    fi
    now=$(cut -d. -f1 "$UPTIME_FILE")
    if [ "$(field "$FAIL_FILE" KEY)" = "$key" ] &&
            [ $((now - $(field "$FAIL_FILE" AT))) -lt "$RETRY_SECS" ]; then
        return 3
    fi
    mbps=$(download "$iface" "https://speed.cloudflare.com/__down?bytes=$TEST_BYTES" 200) ||
        mbps=$(download "$iface" https://proof.ovh.net/files/10Mb.dat 206 \
                        -r "0-$((TEST_BYTES - 1))") || mbps=""
    if [ -z "$mbps" ]; then
        printf 'KEY=%s\nAT=%s\n' "$key" "$now" > "$FAIL_FILE"
        rm -f "$RESULT_FILE"
        log "$iface ($key) could not download the speed test; no internet through it"
        return 1
    fi
    rm -f "$FAIL_FILE"
    printf 'KEY=%s\nDOWN_MBPS=%s\nMEASURED_AT=%s\n' "$key" "$mbps" "$now" > "$RESULT_FILE.tmp" &&
        mv -f "$RESULT_FILE.tmp" "$RESULT_FILE"
    log "$iface downloads at $mbps Mbit/s"
    printf '%s\n' "$mbps"
}

# batctl takes whole numbers; a bare "server" would keep the last value.
bandwidth_arg() {
    local iface="$1" mbps
    if [ -n "$iface" ] && [ "$(field "$RESULT_FILE" KEY)" = "$(uplink_key "$iface")" ]; then
        mbps=$(field "$RESULT_FILE" DOWN_MBPS)
        awk -v m="$mbps" 'BEGIN {
            down = int(m * 1000); if (down < 100) down = 100
            up = int(down / 5); if (up < 100) up = 100
            printf "%dkbit/%dkbit\n", down, up }'
    else
        printf '%s\n' "$DEFAULT_BANDWIDTH"
    fi
}

usage() {
    printf '%s\n' \
        'usage: manet-uplink-speed.sh {measure|announce|is-ethernet} IFACE' \
        '       manet-uplink-speed.sh forget' >&2
}

case "${1:-}" in
    measure)
        [ $# -eq 2 ] || { usage; exit 1; }
        measure "$2"
        ;;
    announce)
        [ $# -eq 2 ] || { usage; exit 1; }
        batctl gw_mode server "$(bandwidth_arg "$2")"
        ;;
    forget)
        [ $# -eq 1 ] || { usage; exit 1; }
        # The failure record stays: it is keyed to its uplink, and clearing it
        # on every pass without an uplink would retest each time.
        rm -f "$RESULT_FILE"
        ;;
    is-ethernet)
        [ $# -eq 2 ] || { usage; exit 1; }
        is_ethernet "$2"
        ;;
    *)
        usage
        exit 1
        ;;
esac
