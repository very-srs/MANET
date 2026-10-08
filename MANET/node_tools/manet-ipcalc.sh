#!/bin/bash
# manet-ipcalc.sh
# Drop-in replacement for the Perl ipcalc as used by the mesh scripts.
# Prints the fields they parse (awk '/HostMin/ {print $2}' etc.) in the
# same column layout. The Perl ipcalc costs ~1.5s of CPU per call on a
# CM4, and mesh-ip-manager + the election scripts call it every
# node-manager cycle: this script does the same math in a few ms.
#
# Usage: manet-ipcalc.sh <a.b.c.d/prefix>   (prefix 1-30)

usage() {
    printf '%s\n' 'usage: manet-ipcalc.sh a.b.c.d/prefix' >&2
}

[ $# -eq 1 ] || { usage; exit 1; }
cidr="$1"
ip=${cidr%/*}
prefix=${cidr#*/}

if ! [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    usage
    exit 1
fi
[[ "$prefix" =~ ^[0-9]{1,2}$ ]] || { usage; exit 1; }
prefix=$((10#$prefix))
[ "$prefix" -ge 1 ] && [ "$prefix" -le 30 ] || { usage; exit 1; }

IFS=. read -r a b c d <<< "$ip"
if [ "$a" -gt 255 ] || [ "$b" -gt 255 ] ||
        [ "$c" -gt 255 ] || [ "$d" -gt 255 ]; then
    usage
    exit 1
fi

a=$((10#$a)); b=$((10#$b)); c=$((10#$c)); d=$((10#$d))

int_to_ip() {
    printf '%d.%d.%d.%d\n' \
        "$(( ($1 >> 24) & 255 ))" "$(( ($1 >> 16) & 255 ))" \
        "$(( ($1 >> 8) & 255 ))" "$(( $1 & 255 ))"
}

ip_int=$(( (a << 24) + (b << 16) + (c << 8) + d ))
mask=$(( (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF ))
net=$(( ip_int & mask ))
bcast=$(( net | (~mask & 0xFFFFFFFF) ))
hosts=$(( bcast - net - 1 ))

printf '%s\n' "Address:   $ip"
printf '%s\n' "Netmask:   $(int_to_ip "$mask") = $prefix"
printf '%s\n' "Network:   $(int_to_ip "$net")/$prefix"
printf '%s\n' "HostMin:   $(int_to_ip $((net + 1)))"
printf '%s\n' "HostMax:   $(int_to_ip $((bcast - 1)))"
printf '%s\n' "Broadcast: $(int_to_ip "$bcast")"
printf '%s\n' "Hosts/Net: $hosts"
