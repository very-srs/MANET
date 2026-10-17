#!/bin/bash
# Measure Ethernet internet download speed and announce BATMAN bandwidth.
# Commands: measure IFACE, announce IFACE, forget, is-ethernet IFACE.
# measure exits: 0 = Mbit/s on stdout, 1 = failed, 2 = metered/untested,
# 3 = cached failure without retest. announce uses measured or fallback speed.
# Tethers and cellular keep batman's default 10/2 Mbit/s and the normal
# internet probe. Ethernet qualifies separately through an HTTP 204 probe.
# Download at most 5 MB per interface/address/router tuple until demotion.
# Advertise upload as one-fifth of download. Only download affects selection.

set -u

RUN_DIR="${MANET_RUN_DIR:-/run}"
RESULT_FILE="$RUN_DIR/manet-uplink-speed"
FAIL_FILE="$RUN_DIR/manet-uplink-speed.failed"
LOCK_FILE="$RUN_DIR/manet-uplink-speed.lock"
ETH_LOCK="${MANET_ETH_DETECT_LOCK:-$RUN_DIR/ethernet-autodetect.lock}"
UPLINK_STATE="$RUN_DIR/manet-uplink.env"
REFRESH_SNAPSHOT=""
TEST_BYTES=5000000
# Ignore tiny responses that cannot give a useful transfer-rate sample.
MIN_TEST_BYTES=250000
# Each download may take up to 15 seconds unless overridden.
TEST_TIMEOUT="${MANET_SPEED_TIMEOUT:-15}"
# Failure cooldown in seconds. Reconciliation asks on every pass.
RETRY_SECS="${MANET_SPEED_RETRY_SECS:-60}"
DEFAULT_BANDWIDTH=10000kbit/2000kbit
# Unknown Ethernet scores below the untested 10/2 Mbit/s metered default.
UNKNOWN_BANDWIDTH=1000kbit/200kbit
SYS_NET="${MANET_SYS_NET:-/sys/class/net}"
UPTIME_FILE="${MANET_UPTIME_FILE:-/proc/uptime}"

# Log transitions even when periodic reconciliation discards stderr.
log() {
    local msg="uplink-speed: $*"
    printf '%s\n' "$msg" | systemd-cat -t manet-uplink-speed 2>/dev/null ||
        printf '%s\n' "$msg" >&2
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
    local iface="$1" ip gw generation
    # Use the first IPv4 CIDR address and first default-via router as cache
    # keys.
    ip=$(ip -4 -o addr show dev "$iface" 2>/dev/null | awk '{split($4, a, "/"); print a[1]; exit}')
    gw=$(ip -4 route show default dev "$iface" 2>/dev/null | awk '{print $3; exit}')
    generation=$(cat "$SYS_NET/$iface/carrier_changes" 2>/dev/null || true)
    printf '%s\n' "$iface ${ip:-none} ${gw:-none} ${generation:-unknown}"
}

# Print the first value for key $2 in the assignment file $1.
field() {
    awk -F= -v k="$2" '$1 == k {print $2; exit}' "$1" 2>/dev/null
}

# One download, at most TEST_BYTES. Prints Mbit/s over the transfer itself
# (DNS, TCP and TLS setup excluded), including useful deadline samples.
download() {
    # Arguments are interface, URL, expected HTTP status and extra curl options.
    local iface="$1" url="$2" expect="$3" out code size start total rc=0
    shift 3
    out=$(curl -4 --noproxy '*' --interface "$iface" \
               -s -o /dev/null -m "$TEST_TIMEOUT" "$@" \
               -w '%{http_code} %{size_download} %{time_starttransfer} %{time_total}' \
               "$url" 2>/dev/null) || rc=$?
    # A timeout can still provide a sample. TLS and other errors cannot.
    [ "$rc" -eq 0 ] || [ "$rc" -eq 28 ] || return 1
    # Curl emits HTTP status, bytes, time to first byte and total seconds.
    read -r code size start total <<< "$out"
    [ "$code" = "$expect" ] && [[ "$size" =~ ^[0-9]+$ ]] || return 1
    [ "$size" -ge "$MIN_TEST_BYTES" ] &&
        [ "$size" -le "$TEST_BYTES" ] || return 1
    [[ "$start" =~ ^[0-9]+(\.[0-9]+)?$ && "$total" =~ ^[0-9]+(\.[0-9]+)?$ ]] || return 1
    # Clamp transfer duration to 1 ms before converting bytes/second to Mbit/s.
    awk -v b="$size" -v s="$start" -v t="$total" \
        'BEGIN { d = t - s; if (d < 0.001) d = 0.001; printf "%.1f\n", b * 8 / d / 1000000 }'
}

# Print the measured Mbit/s for an Ethernet interface, reusing keyed state.
# Update RESULT_FILE or FAIL_FILE under LOCK_FILE and preserve exit codes.
measure() {
    local iface="$1" key now mbps
    is_ethernet "$iface" || return 2
    exec 8>>"$LOCK_FILE"
    # Allow two download deadlines plus five seconds for the lock holder.
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
    # Try Cloudflare first, then a byte-range download from OVH on failure.
    mbps=$(download "$iface" "https://speed.cloudflare.com/__down?bytes=$TEST_BYTES" 200) ||
        mbps=$(download "$iface" https://proof.ovh.net/files/10Mb.dat 206 \
                        -r "0-$((TEST_BYTES - 1))") || mbps=""
    # A queued worker never publishes a result for a connection which changed
    # during the download. Serialize result installation with promotion/loss.
    if [ -n "$REFRESH_SNAPSHOT" ]; then
        exec 9>"$ETH_LOCK"
        flock -w 90 9 || return 1
        uplink_refresh_matches "$iface" "$key" || return 1
    fi
    if [ -z "$mbps" ]; then
        # Retries of the same unknown estimate stay quiet.
        if [ "$(field "$FAIL_FILE" KEY)" != "$key" ]; then
            log "$iface bandwidth unknown; announcing $UNKNOWN_BANDWIDTH"
        fi
        printf 'KEY=%s\nAT=%s\n' "$key" "$now" > "$FAIL_FILE"
        rm -f "$RESULT_FILE"
        return 1
    fi
    rm -f "$FAIL_FILE"
    printf 'KEY=%s\nDOWN_MBPS=%s\nMEASURED_AT=%s\n' "$key" "$mbps" "$now" > "$RESULT_FILE.tmp" &&
        mv -f "$RESULT_FILE.tmp" "$RESULT_FILE"
    log "$iface downloads at $mbps Mbit/s"
    printf '%s\n' "$mbps"
}

# Check a worker's original connection under the shared Ethernet policy lock.
uplink_refresh_matches() {
    local iface="$1" key="$2"
    [ -f "$RUN_DIR/manet-uplink/mesh-gateway.state" ] &&
        [ "$(cat "$RUN_DIR/upstream_iface" 2>/dev/null)" = "$iface" ] &&
        [ "$(cat "$SYS_NET/$iface/carrier" 2>/dev/null)" = 1 ] &&
        [ "$(cat "$UPLINK_STATE" 2>/dev/null)" = "$REFRESH_SNAPSHOT" ] &&
        [ "$(uplink_key "$iface")" = "$key" ]
}

# Measure outside policy locks, then announce only the still-selected uplink.
# systemd coalesces concurrent starts of this per-interface oneshot.
refresh_uplink_bandwidth() {
    local iface="$1" key
    is_ethernet "$iface" || return 0
    REFRESH_SNAPSHOT=$(cat "$UPLINK_STATE" 2>/dev/null)
    [ "$(field "$UPLINK_STATE" UPLINK_MODE)" = gateway ] &&
        [ "$(field "$UPLINK_STATE" UPLINK_IFACE)" = "$iface" ] || return 0
    key=$(uplink_key "$iface")
    uplink_refresh_matches "$iface" "$key" || return 0
    # The subshell releases the measurement lock before taking policy again.
    ( measure "$iface" ) >/dev/null || true
    exec 9>"$ETH_LOCK"
    flock -w 90 9 || return 0
    uplink_refresh_matches "$iface" "$key" || return 0
    batctl gw_mode server "$(mesh_limited_bandwidth "$(bandwidth_arg "$iface")")"
}

# batctl requires whole numbers. Bare "server" retains the last value.
bandwidth_arg() {
    local iface="$1" mbps
    if [ -n "$iface" ] && [ "$(field "$RESULT_FILE" KEY)" = "$(uplink_key "$iface")" ]; then
        mbps=$(field "$RESULT_FILE" DOWN_MBPS)
        # Convert Mbit/s to kbit/s with a minimum of 100 kbit/s per direction.
        awk -v m="$mbps" 'BEGIN {
            down = int(m * 1000); if (down < 100) down = 100
            up = int(down / 5); if (up < 100) up = 100
            printf "%dkbit/%dkbit\n", down, up }'
    elif [ -n "$iface" ] && is_ethernet "$iface"; then
        printf '%s\n' "$UNKNOWN_BANDWIDTH"
    else
        printf '%s\n' "$DEFAULT_BANDWIDTH"
    fi
}

# A gateway with only HaLow mesh attachment cannot deliver Ethernet rates.
# Use a conservative 1 Mbit/s ceiling until a measured S1G capacity is available.
mesh_limited_bandwidth() {
    local value="$1" roles="${MANET_IFACE_STATE_DIR:-/var/lib}" iface standard=false
    local members
    [ -s "$roles/halow_if" ] || { printf '%s\n' "$value"; return; }
    members=$(batctl if 2>/dev/null)
    for iface in $(cat "$roles/mesh_if" 2>/dev/null); do
        if printf '%s\n' "$members" | grep -q "^${iface}: active"; then
            standard=true
        fi
    done
    if [ "$standard" = true ]; then printf '%s\n' "$value"; return; fi
    printf '%s\n' "$value" | awk -F'kbit/' '{
        down=$1+0; up=$2+0;
        if (down>1000) down=1000; if (up>1000) up=1000;
        printf "%dkbit/%dkbit\n", down, up
    }'
}

# Print the supported commands to stderr.
usage() {
    printf '%s\n' \
        'usage: manet-uplink-speed.sh {measure|refresh|announce|is-ethernet} IFACE' \
        '       manet-uplink-speed.sh forget' >&2
}

case "${1:-}" in
    refresh)
        [ $# -eq 2 ] || { usage; exit 1; }
        refresh_uplink_bandwidth "$2"
        ;;
    measure)
        [ $# -eq 2 ] || { usage; exit 1; }
        measure "$2"
        ;;
    announce)
        [ $# -eq 2 ] || { usage; exit 1; }
        batctl gw_mode server "$(mesh_limited_bandwidth "$(bandwidth_arg "$2")")"
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
