# Networkd-dispatcher scripts

This directory contains the scripts that are triggered by networkd-dispatcher
when an ethernet or USB tether interface changes state.

### 1. carrier
* activated when an interface gets carrier
* calls manet-uplink-dispatch.sh to decide gateway or wired EUD behavior

### 2. off
* cleanup script that returns the node to a baseline when no ethernet connection is present

### 3. no-carrier / degraded
* same cleanup path as off for partially removed links

### 4. routable
* activated when networkd reports the interface as routable
* reconciles gateway/NAT state after DHCP or route changes
