# Additional setup scripts

Setup scripts placed in this directory are embedded in the image at flash time.
Each runs **once**, as **root**, on the node, after `radio-setup.sh` has
finished and the node is fully configured.

The directory is optional. When it is empty the flashers report nothing and
provisioning is unchanged.

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
        |  radio-setup.sh enables and starts manet-user-scripts.service
        |  after provisioning completes
        v
  run once, in order, logged to /var/log/manet-user-scripts.log
```

They run after provisioning rather than during it, so the mesh, the AP, the
radios and the network are already available to them.

## Where things land on the node

The scripts occupy a single flat directory. The state and marker files are
**siblings of that directory, not members of it**, because every file inside it
is treated as a script.

| Path | What |
|---|---|
| `/var/lib/manet-user-scripts/` | the scripts, mode `0755`; flat, no subdirectories |
| `/var/lib/manet-user-scripts.state` | one line per completed script: `name<TAB>exit<TAB>epoch` |
| `/var/lib/manet-user-scripts.done` | marker; its presence is what stops a re-run |
| `/var/log/manet-user-scripts.log` | full output of every run, appended |

Files are neither nested nor renamed: `10-site-routes.sh` in this directory
arrives as `/var/lib/manet-user-scripts/10-site-routes.sh` byte for byte, with
CRLF stripped and the executable bit set.

Two further files implement the mechanism rather than belonging to it:
`/usr/local/bin/manet-user-scripts.sh` and
`/etc/systemd/system/manet-user-scripts.service`. Both are delivered by the
install tarball, not from this directory.

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

## Languages

Scripts are executed by the interpreter named in their shebang, so any language
installed on the node can be used. A freshly flashed node provides:

| Shebang | Version on a stock node |
|---|---|
| `#!/bin/bash` | bash 5.2 |
| `#!/bin/sh`, `#!/bin/dash` | dash |
| `#!/usr/bin/env python3` | Python 3.13 |
| `#!/usr/bin/perl` | Perl 5.40 |
| `#!/usr/bin/lua` | Lua 5.1 |
| `#!/usr/bin/awk -f` | mawk |

Nothing else is installed. Ruby, Node, PHP, Tcl and similar are absent, and a
script written for one of them fails with exit 127 and a log line naming the
missing interpreter.

Those languages remain usable, but the interpreter has to be installed first.
Because scripts run in filename order, an earlier script can prepare the
environment for a later one:

```
10-install-ruby.sh     #!/bin/bash — apt-get install -y ruby
20-configure-site.rb   #!/usr/bin/ruby — now runs
```

A script whose shebang names an interpreter absent from the list above is still
embedded; the flasher only reports it, since an earlier script may be
installing it.

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
| Interpreter present on a stock node | reported, not rejected |
| `bash -n` clean (shell shebangs) | FAIL |
| `ast.parse` clean (python shebangs) | FAIL |

Any FAIL aborts the run with the filename and the reason, before the card is
touched.

Only shell and Python are syntax-checked, because only those can be checked
without executing the script: `bash -n` parses without running, and Python's
`ast.parse` does not execute imports. Perl is deliberately excluded — `perl -c`
runs `BEGIN` blocks, which would execute operator code on the flashing machine,
and it resolves `use` statements against that machine's module path, so a
script using a module present only on the node would be rejected incorrectly. A
wrong rejection blocks a flash, which is worse than an unchecked script. Other
languages are accepted at their shebang and reported as
`not syntax-checked`.

> The shell check needs `bash` and the Python check needs `python3`. On Windows
> these come from Git for Windows, WSL, or a Python installation. When one is
> absent the script is still embedded and the report says so.

CRLF line endings are stripped automatically, so a script written in a Windows
editor works.

## Data files

There is no separate `data/` directory. A quoted heredoc carries any text
verbatim, **including a heredoc nested inside the script**, so small data files
can be written by the script that uses them:

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

The node confirms internet connectivity during provisioning, so a working path
exists by the time these scripts run. Fetching also keeps the payload off the
boot partition; see the warning below.

Embedded scripts produce a warning above **256 KB** in total and are refused
above **2 MB**. Neither figure is a limit imposed by `rpi-imager` or FAT32. They
are set because a payload of that size is better retrieved at run time than
embedded in the image.

## Do not put secrets in here

`firstrun.sh` is stored **unencrypted on the FAT32 boot partition and is never
deleted**, and it tees its own log to `/boot/firmware/firstrun.log`. Anyone who
pulls the eMMC or SD card and puts it in a laptop reads everything in it.

The card already carries credentials: the `radio` and admin passwords are
written to it in the clear. A node password can be rotated after exposure,
whereas a site CA private key or a long-lived client certificate cannot.
Retrieve those at run time over an authenticated channel, or provision them out
of band.

## Failures

Failures are **advisory**. A script that exits non-zero is recorded and
reported, but never marks the node unprovisioned: a failing site hook must not
cause a functioning mesh node to report a failed setup.

On the node:

```bash
manet-user-scripts.sh --list        # what is staged, and what has run
cat /var/log/manet-user-scripts.log
sudo manet-user-scripts.sh --force  # re-run everything
```

Scripts can also be added to a node directly by placing them in
`/var/lib/manet-user-scripts/` and invoking `manet-user-scripts.sh`. The same
rules apply: the shebang requirement and the `.disabled`, `.bak`, `.orig` and
`~` suffixes are re-checked on the node, so a configuration file placed
alongside the scripts is skipped rather than executed. Without that check it
would be executed, because a file with no shebang does not fail to run — the
kernel refuses it and the shell falls back to interpreting it.

The SSH login banner reports the outcome next to the provisioning line:

```
  MANET node provisioned (v0.543), 4m 12s ago
  Setup scripts  : 2 of 3 ran, 1 FAILED
     ! 20-org-ssh-keys.sh            exit 1
     log: /var/log/manet-user-scripts.log
```

Each script gets **300 seconds** by default, then it is killed. Change it per
node with `user_script_timeout=` in `/etc/mesh.conf`.

A script interrupted by power loss part-way through the set is re-run on the
next boot; those that already completed are not. A script that ran and failed
is not retried automatically — use `--force` to run the set again.
