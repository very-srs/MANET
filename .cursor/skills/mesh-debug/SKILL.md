---
name: mesh-debug
description: >-
  Operate MANET mesh nodes over SSH. Covers diagnostics (batman-adv, WiFi,
  bridge, routing, DNS/dnsmasq, Alfred, services), performance testing (iperf3,
  signal quality), provisioning verification, and deploying script updates.
  For flash-time and first-boot context, read MANET/provisioning/README.md
  (linked in the skill body). Use when the user asks to check, debug, diagnose,
  troubleshoot, test, update, deploy to, or provision mesh nodes.
---

# Mesh Node Debugging

## Node Inventory

Read `.cursor/mesh-nodes.env` for the current list of node IPs, SSH user, and password. Use these values for all SSH commands below.

## Related documentation

For **host-side flashing**, **first-boot flow** (Raspberry Pi vs Rock 3A), **provisioning-time options** (EUD wired/wireless/auto, mesh SSID and SAE key, CIDR, DHCP sizing, regulatory domain), **reserved service addresses** (e.g. MediaMTX / Mumble), **default credentials**, and **provisioning troubleshooting** (Ethernet, timeouts, log locations in prose), read `MANET/provisioning/README.md` in this repository.

For **repository layout** and where provisioning sources of truth live, see `MANET/README.md`.

## SSH Access

```bash
sshpass -p '$MESH_PASSWORD' ssh -o StrictHostKeyChecking=no $MESH_USER@$NODE_IP
```

For sudo commands in non-interactive SSH, pipe the password:

```bash
echo $MESH_PASSWORD | sudo -S <command>
```

Always export the full PATH on the node — many tools live outside the default PATH:

```bash
export PATH=$PATH:/usr/sbin:/usr/local/sbin:/sbin
```

## Diagnostic Commands

Run these on the node. Group them into a single SSH session for efficiency.

### 1. Identity & Health

```bash
hostname
uptime
cat /etc/mesh.conf
```

### 2. Interface Inventory

```bash
# Quick overview: names, state, IPs
ip -br addr

# Driver identification per interface
for i in wlan0 wlan1 wlan2 wlan3; do
  [ -e /sys/class/net/$i ] || continue
  DRIVER=$(basename "$(readlink -f /sys/class/net/$i/device/driver 2>/dev/null)" 2>/dev/null)
  MAC=$(cat /sys/class/net/$i/address 2>/dev/null)
  STATE=$(cat /sys/class/net/$i/operstate 2>/dev/null)
  echo "$i: driver=$DRIVER mac=$MAC state=$STATE"
done

# Detailed wireless info (PHY, channel, mode, txpower)
iw dev
```

### 3. Batman-adv Mesh

```bash
# Which interfaces are enslaved to bat0
batctl meshif bat0 if

# Direct mesh neighbors (one-hop peers)
batctl meshif bat0 n

# Full originator table (all reachable nodes, throughput, best next-hop)
batctl meshif bat0 o

# Gateway mode (client or server)
batctl meshif bat0 gw_mode

# Gateway list (which nodes advertise internet)
batctl meshif bat0 gwl
```

### 4. WiFi Station Details

```bash
# Per-interface station dump: signal, bitrate, mesh peering state
for i in wlan0 wlan1 wlan2; do
  echo "--- $i ---"
  iw dev $i station dump 2>/dev/null | head -40
done
```

Key fields to check:

- `signal` / `signal avg`: link quality in dBm (closer to 0 = better)
- `tx bitrate` / `rx bitrate`: negotiated rates
- `mesh plink`: should be `ESTAB` for healthy mesh peers
- `tx retries` / `tx failed`: high values indicate interference or distance issues

### 5. Bridge & Routing

```bash
# Bridge members (should contain bat0 + AP interface)
ls /sys/class/net/br0/brif/

# Routing table
ip route

# IPv6 addresses (SLAAC should give fd01:ed20:ecb4:: prefix)
ip -6 addr show dev br0
```

### 6. Web Status API (fastest overview)

```bash
# Full mesh topology as JSON (all nodes, links, TQ values)
curl -s http://localhost/api/data | python3 -m json.tool

# Local node state only (interfaces, services, IP, channel)
curl -s http://localhost/api/local | python3 -m json.tool
```

### 7. Alfred & Node Manager

```bash
# Read Alfred data store (type 65 = mesh node status)
alfred -r 65

# Node manager journal (IP allocation, registry updates)
journalctl -u node-manager --no-pager -n 40

# Mesh registry (decoded peer data)
cat /var/run/mesh_node_registry

# Which node-manager mode is running (ACS vs static)
readlink -f /usr/local/bin/node-manager.sh
```

### 8. Service Status

```bash
# Check key services
systemctl is-active batman-enslave node-manager wpa_supplicant hostapd radvd alfred mesh-status dnsmasq

# List all running mesh-related services
systemctl list-units --type=service --state=running | grep -E 'bat|mesh|wpa|node|hostap|alfred|radvd|mediamtx|mumble|dnsmasq'

# dnsmasq upstream DNS and DHCP config
cat /etc/dnsmasq.d/mesh-eud.conf /etc/dnsmasq.d/upstream-dns.conf
journalctl -u dnsmasq --no-pager -n 15

# WPA supplicant logs per interface
journalctl -u wpa_supplicant@wlan0 --no-pager -n 20
journalctl -u wpa_supplicant@wlan1 --no-pager -n 20

# HaLow uses a different service name pattern
journalctl -u 'wpa_supplicant-s1g-*' --no-pager -n 20
```

### 9. Interface Naming & Pinning

```bash
# What radio-setup decided
cat /var/lib/mesh_if
cat /var/lib/halow_if
cat /var/lib/no_mesh_if
cat /var/lib/ap_interface
cat /var/lib/iface_map

# Systemd .link files (MAC-to-name pinning)
cat /etc/systemd/network/10-wlan*.link

# WPA supplicant configs
ls /etc/wpa_supplicant/wpa_supplicant-wlan*.conf
```

### 10. Provisioning State

```bash
# Check if initial provisioning completed
systemctl is-enabled radio-setup-run-once 2>/dev/null && echo "STILL PROVISIONING" || echo "provisioning done"

# Check if a post-rename re-run is pending
systemctl is-enabled radio-setup-rerun 2>/dev/null && echo "RERUN PENDING" || echo "no rerun pending"
```

### 11. Logs & Kernel Messages

```bash
# Radio setup log
cat /var/log/radio-setup.log

# Provisioning log (RPi)
cat /boot/firmware/provision.log

# Provisioning log (Rock 3A)
cat /var/log/mesh-provision.log

# Kernel messages for WiFi/mesh
dmesg | grep -iE 'morse|wifi|wlan|bat0|mesh|mt7915|brcmfmac' | tail -30
```

## Common Issues

### Interface naming mismatch after reboot

**Symptom**: wpa_supplicant log says "Driver does not support mesh mode", wrong radio doing AP.
**Cause**: Missing `.link` file for a radio — driver load order changed between boots.
**Fix**: Ensure every detected interface has a `10-wlanX.link` file in `/etc/systemd/network/` pinning its MAC to a stable name. Then reboot.

### No mesh neighbors

**Symptom**: `batctl meshif bat0 n` is empty, `batctl meshif bat0 o` is empty.
**Check**: `iw dev` — verify mesh interfaces show `type mesh point`. Check `wpa_supplicant@wlanX` logs for SAE failures. Verify both nodes share the same mesh SSID and SAE key in `/etc/mesh.conf`.

### batman-adv module not loaded

**Symptom**: `batctl` commands fail with "No such file or directory".
**Check**: `lsmod | grep batman`. If missing: `modprobe batman_adv`.

### No IPv4 on br0

**Symptom**: Node has no 10.x.x.x address.
**Check**: `journalctl -u node-manager` — look for IP allocation errors. Verify Alfred is running and peers are visible (`alfred -r 65`).

### AP not broadcasting

**Symptom**: EUD SSID not visible.
**Check**: `systemctl status hostapd`, `cat /etc/hostapd/hostapd.conf`, `iw dev` — verify AP interface is in `type AP` mode on the expected channel.

### EUD can ping IPs but no DNS / no browsing

**Symptom**: Client connected to AP can `ping 8.8.8.8` but can't browse or resolve names.
**Cause**: dnsmasq's upstream DNS is unreachable (e.g. network blocks port 53 to public resolvers).
**Check**:

```bash
# What upstream DNS is dnsmasq using?
cat /etc/dnsmasq.d/mesh-eud.conf /etc/dnsmasq.d/upstream-dns.conf

# Test if dnsmasq can actually resolve
python3 -c "
import socket, struct
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
query = b'\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x06google\x03com\x00\x00\x01\x00\x01'
br0_ip = '$(ip -4 -o addr show br0 | awk \"{print \\$4}\" | cut -d/ -f1 | head -1)'
s.sendto(query, (br0_ip, 53))
resp, _ = s.recvfrom(512)
print(f'OK: {struct.unpack(\"!H\", resp[6:8])[0]} answers')
"

# What DNS did the node get via DHCP on end0?
resolvectl dns end0

# What's in the systemd-resolved uplink file (dnsmasq should read this)?
cat /run/systemd/resolve/resolv.conf
```

**Fix**: `mesh-eud.conf` should have `resolv-file=/run/systemd/resolve/resolv.conf` (not `no-resolv`) so dnsmasq uses DHCP-provided DNS. `upstream-dns.conf` should have `server=1.1.1.1` and `server=8.8.8.8` as fallbacks only.

## Performance Testing

### iperf3 between nodes

Run the server on one node, client on the other. Use br0 IPs (the 10.x.x.x mesh addresses).

```bash
# On node A (server)
iperf3 -s -1

# On node B (client) — replace with node A's br0 IP
iperf3 -c 10.30.2.111 -t 10
```

### Per-radio link quality

```bash
# Check which radio batman prefers (higher throughput = preferred path)
batctl meshif bat0 o

# Signal quality thresholds
#   > -50 dBm  = excellent (close range)
#   -50 to -65 = good
#   -65 to -75 = usable
#   < -75      = marginal, expect retransmissions
```

## Provisioning Verification

Pi vs Rock 3A first-boot stages and host flashing are documented in `MANET/provisioning/README.md`. After a node's first boot, verify all three phases completed:

```bash
# Phase 1: firstrun (RPi only) — should exist and show completion
tail -5 /boot/firmware/firstrun.log

# Phase 2: provision-mesh — should show "Provisioning complete"
tail -5 /boot/firmware/provision.log       # RPi
tail -5 /var/log/mesh-provision.log        # Rock 3A

# Phase 3: radio-setup — should show interface detection and service setup
tail -20 /var/log/radio-setup.log

# Confirm all phases finished (both should say "inactive")
systemctl is-enabled radio-setup-run-once 2>/dev/null || echo "done"
systemctl is-enabled mesh-provision 2>/dev/null || echo "done"
```

## Deploying Script Changes

### Via node-update (official path)

```bash
# Manual trigger (checks internet, compares versions, downloads tarball)
sudo node-update.sh

# Check current version
cat /etc/manet_version.txt
```

### Manual file deployment

The tools tarball extracts to `/`, so scripts live at their final paths. Key destinations:

```bash
# Mesh scripts
/usr/local/bin/*.sh
/usr/local/bin/*.py

# Systemd units
/etc/systemd/system/*.service

# After copying updated scripts:
chmod +x /usr/local/bin/*
systemctl daemon-reload

# Restart order for most changes:
systemctl restart node-manager        # picks up script changes
systemctl restart batman-enslave      # re-enslaves interfaces
systemctl restart alfred              # reconnects gossip
```

## Parallel Multi-Node Check

When checking multiple nodes, SSH into all of them in parallel (separate shell calls) to compare state side-by-side. Always check that both sides of a mesh link agree on SSID, channel, and SAE key.
