# MANET Install Packages

This directory contains the software packages required for installing and updating MANET mesh nodes.

## Package Types

There are two categories of platform specific tar archives available in this directory:

### 1. Tools Archives
* **Naming Convention:** `*-tools.tar.gz`
* **Contents:** The most recent release of the MANET tools.
* **Purpose:** These archives are used to update an existing mesh node to the current version of the software.
* **Installation:** These packages are installed using the `node-update.sh` script.

### 2. Install Archives
* **Naming Convention:** `*-install.tar.gz`
* **Contents:** Includes the MANET tools found in the tools archive, plus the system kernel and other static files required for a full system setup.
* **Purpose:** These archives are designed for the initial installation and bootstrapping of a new node.
