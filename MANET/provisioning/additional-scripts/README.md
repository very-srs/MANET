# Additional setup scripts

Drop your own setup scripts in this directory and they are baked into the image
at flash time. Each one runs **once**, as **root**, on the node — after
`radio-setup.sh` has finished and the node is a working mesh node.

Nothing here is required. An empty directory is the normal case and the
flashers say nothing about it.

## How it works

```
additional-scripts/            (this directory, on your machine)
  10-site-routes.sh
  20-org-ssh-keys.sh
        |
        |  linux.sh / windows.ps1 validate each file, then append one
        |  quoted heredoc per script to the generated firstrun.sh
        v
  firstrun.sh                  (baked into the image by rpi-imager)
        |
        |  first boot writes them out, 0755
        v
  /var/lib/manet-user-scripts/ (on the node)
        |
        |  radio-setup.sh enables + starts manet-user-scripts.service
        |  as its last act, once provisioning has actually completed
        v
  run once, in order, logged to /var/log/manet-user-scripts.log
```

They run *after* provisioning, not during it, so the mesh, the AP, the radios
and the network are already up when your script starts.

## Where things land on the node

The scripts go into one flat directory. The bookkeeping files are **siblings of it,
not inside it** — anything inside that directory is treated as a script.

| Path | What |
|---|---|
| `/var/lib/manet-user-scripts/` | your scripts, mode `0755`, flat — no subdirectories |
| `/var/lib/manet-user-scripts.state` | one line per completed script: `name<TAB>exit<TAB>epoch` |
| `/var/lib/manet-user-scripts.done` | marker; its presence is what stops a re-run |
| `/var/log/manet-user-scripts.log` | full output of every run, appended |

Nothing is nested and nothing is renamed — `10-site-routes.sh` in this directory
arrives as `/var/lib/manet-user-scripts/10-site-routes.sh`, byte for byte, with CRLF
stripped and the executable bit set.

Two more files belong to the machinery rather than to your scripts:
`/usr/local/bin/manet-user-scripts.sh` (the runner) and
`/etc/systemd/system/manet-user-scripts.service` (the one-shot unit). Both arrive in
the install tarball, not from this directory.

## Naming and order

Scripts run in lexical order, so use a numeric prefix:

| Name | Runs |
|------|------|
| `10-site-routes.sh` | first |
| `20-org-ssh-keys.sh` | second |
| `90-report-home.sh` | last |

Filenames must match `[A-Za-z0-9][A-Za-z0-9._-]*` — no spaces.

Anything ending in `.disabled`, `.bak`, `.orig` or `~`, and anything starting
with `.`, is ignored. That is how to park a script without deleting it, and it
is what keeps the bundled example inert.

## What is accepted

Every file needs a **shebang on line 1**. A file without one is skipped with a
notice, not treated as an error — a `README.md` or a notes file living here is
fine and must never be executed on a node.

The flasher checks each file **before anything is written to a card**:

| Check | Failure |
|-------|---------|
| Filename `[A-Za-z0-9][A-Za-z0-9._-]*` | FAIL |
| Non-empty | FAIL |
| No NUL bytes | FAIL — binaries cannot go in a heredoc |
| Valid UTF-8 | FAIL |
| `#!` on line 1 | SKIP, with a notice |
| `bash -n` clean (shell shebangs only) | FAIL |

Any FAIL aborts the run with the filename and the reason, before the card is
touched. Non-shell scripts (`#!/usr/bin/env python3` and friends) get every
check except the syntax parse — checking those would mean running that
interpreter on the flashing machine.

> On Windows the syntax check needs a `bash` — Git for Windows or WSL. Without
> one the script is still embedded, and the report says
> `shell (not syntax-checked, no bash here)`.

CRLF line endings are stripped automatically, so a script written in a Windows
editor works.

## Data files

There is no `data/` directory, and none is needed. A quoted heredoc carries any
text verbatim, **including a heredoc nested inside your script**, so small data
lives next to the code that uses it:

```bash
#!/bin/bash
install -d -m 700 /home/radio/.ssh
cat > /home/radio/.ssh/authorized_keys <<'KEYS'
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... ops@example.org
KEYS
chown -R radio:radio /home/radio/.ssh
```

Anything **large or binary** should be fetched at run time instead:

```bash
curl -fsSL --retry 3 https://files.example.org/bundle.tar.gz -o /tmp/b.tar.gz
tar xzf /tmp/b.tar.gz -C /opt
```

The node confirms internet connectivity during provisioning, so it has a
working path by the time your scripts run. Fetching also keeps the payload off
the boot partition — see the warning below.

Embedded scripts warn above **256 KB** total and are refused above **2 MB**.
Neither is a limit of `rpi-imager` or FAT32; they are there because a payload
that big wants to be fetched, not baked.

## Do not put secrets in here

`firstrun.sh` is stored **unencrypted on the FAT32 boot partition and is never
deleted**, and it tees its own log to `/boot/firmware/firstrun.log`. Anyone who
pulls the eMMC or SD card and puts it in a laptop reads everything in it.

The card is already a credential — the `radio` and admin passwords are baked in
the clear — but a node password can be rotated. A site CA private key or a
long-lived client certificate cannot be un-leaked. Fetch those at run time over
an authenticated channel, or provision them out of band.

## Failures

Failures are **advisory**. A script that exits non-zero is recorded and
reported; it never marks the node unprovisioned, because a broken site hook
must not make a working mesh node claim it failed to set itself up.

On the node:

```bash
manet-user-scripts.sh --list        # what is staged, and what has run
cat /var/log/manet-user-scripts.log
sudo manet-user-scripts.sh --force  # re-run everything
```

You can also add a script to a node directly, by dropping it in
`/var/lib/manet-user-scripts/` and running the runner. The same rules apply there — the
runner re-checks the shebang and the `.disabled`/`.bak`/`.orig`/`~` suffixes itself, so
a config file copied in beside your scripts is skipped rather than executed. (Without
that check it *would* run: `exec` of a file with no shebang does not fail, the kernel
refuses it and the shell falls back to interpreting it.)

The SSH login banner reports the outcome next to the provisioning line:

```
  MANET node provisioned (v0.542), 4m 12s ago
  Setup scripts  : 2 of 3 ran, 1 FAILED
     ! 20-org-ssh-keys.sh            exit 1
     log: /var/log/manet-user-scripts.log
```

Each script gets **300 seconds** by default, then it is killed. Change it per
node with `user_script_timeout=` in `/etc/mesh.conf`.

A script that is cut off by a power loss part-way through the set is re-run on
the next boot; the ones that already completed are not. A script that ran and
*failed* has had its turn — it is not retried automatically. `--force` is the
way back.
