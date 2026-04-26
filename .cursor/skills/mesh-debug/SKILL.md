---
name: mesh-debug
description: >-
  Debug MANET mesh nodes over SSH. Provides commands for diagnosing batman-adv
  mesh, WiFi interfaces, bridge, routing, Alfred gossip, and service health.
  Use when the user asks to check, debug, diagnose, or troubleshoot mesh nodes,
  WiFi, networking, batman, or connectivity issues.
---

# Mesh Node Debugging

## SSH Access

Nodes use the `radio` user. Password is set during provisioning (commonly stored in mesh.conf or known by the operator).

```bash
sshpass -p '$PASSWORD' ssh -o StrictHostKeyChecking=no radio@$NODE_IP
```

For sudo commands in non-interactive SSH, pipe the password:

```bash
echo $PASSWORD | sudo -S <command>
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
systemctl is-active batman-enslave node-manager wpa_supplicant hostapd radvd alfred mesh-status

# List all running mesh-related services
systemctl list-units --type=service --state=running | grep -E 'bat|mesh|wpa|node|hostap|alfred|radvd|mediamtx|mumble|dnsmasq'

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

## Parallel Multi-Node Check

When checking multiple nodes, SSH into all of them in parallel (separate shell calls) to compare state side-by-side. Always check that both sides of a mesh link agree on SSID, channel, and SAE key.
