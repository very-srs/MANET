# Install packages

Two kinds of archive are published here, one pair per board.

`*-tools.tar.gz` updates a node that is already running. `node-update.sh`
fetches one and extracts it. It carries the node scripts, systemd units and
supporting files, and no kernel.

`*-install.tar.gz` sets up a new node. It carries everything the tools archive
does, plus the kernel, device trees, modules and radio firmware for that board.
First-boot provisioning downloads it by exact filename, so these are not
renamed.
