#!/bin/bash
# manet-ipcalc.sh
# Drop-in replacement for the Perl ipcalc as used by the mesh scripts.
# Prints the fields they parse (awk '/HostMin/ {print $2}' etc.) in the
# same column layout. The Perl ipcalc costs ~1.5s of CPU per call on a
# CM4, and mesh-ip-manager + the election scripts call it every
# node-manager cycle — this script does the same math in a few ms.
#
# Usage: manet-ipcalc.sh <a.b.c.d/prefix>   (prefix 1-30)

cidr="$1"
ip=${cidr%/*}
prefix=${cidr#*/}

[[ "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || exit 1
[[ "$prefix" =~ ^[0-9]+$ ]] || exit 1
[ "$prefix" -ge 1 ] && [ "$prefix" -le 30 ] || exit 1

IFS=. read -r a b c d <<< "$ip"
[ "$a" -le 255 ] && [ "$b" -le 255 ] && [ "$c" -le 255 ] && [ "$d" -le 255 ] || exit 1

int_to_ip() {
    echo "$(( ($1 >> 24) & 255 )).$(( ($1 >> 16) & 255 )).$(( ($1 >> 8) & 255 )).$(( $1 & 255 ))"
}

ip_int=$(( (a << 24) + (b << 16) + (c << 8) + d ))
mask=$(( (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF ))
net=$(( ip_int & mask ))
bcast=$(( net | (~mask & 0xFFFFFFFF) ))
hosts=$(( bcast - net - 1 ))

echo "Address:   $ip"
echo "Netmask:   $(int_to_ip "$mask") = $prefix"
echo "Network:   $(int_to_ip "$net")/$prefix"
echo "HostMin:   $(int_to_ip $((net + 1)))"
echo "HostMax:   $(int_to_ip $((bcast - 1)))"
echo "Broadcast: $(int_to_ip "$bcast")"
echo "Hosts/Net: $hosts"
