# Networkd-dispatcher scripts

This directory contains the scripts that are triggered by physically connecting an ethernet cable to the node

### 1. carrier
* activated when the ethernet interface becomes active
* calls the ethernet-autodetect.sh script to handle gw and network logic

### 2. off
* cleanup script that returns the node to a baseline when no ethernet connection is present
