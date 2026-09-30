# Provisioning internals

How the flashers build an image: the template tokens, where the substitution
lists live, and how operator setup scripts are embedded. Operator-facing
instructions are in
[`MANET/provisioning/README.md`](../MANET/provisioning/README.md).

This file is for someone developing on the project. It is not user
documentation.

---

### Template tokens

`linux-flasher.sh` and `windows.ps1` bake mesh settings into the image by substituting
`__TOKEN__` placeholders in those templates at flash time. Edit the templates
using the tokens, not concrete values; a leftover `__MESH_SSID__` in a flashed
image means the substitution list in the flasher was not updated.

Tokens (same set on Linux and Windows):

`__HARDWARE_MODEL__` `__EUD_CONNECTION__` `__LAN_AP_SSID__` `__LAN_AP_KEY__`
`__MAX_EUDS_PER_NODE__` `__INSTALL_MEDIAMTX__` `__INSTALL_MUMBLE__`
`__VOICE_ENABLED__` `__MESH_SSID__` `__MESH_SAE_KEY__` `__LAN_CIDR_BLOCK__`
`__AUTO_CHANNEL__` `__RADIO_PW__` `__REGULATORY_DOMAIN__`
`__HALOW_REGULATORY_DOMAIN__` `__ADMIN_PW__` `__AUTO_UPDATE__`
`__RELEASE_VERSION__` `__INSTALL_URL__` `__INSTALL_SHA256__`

Adding a new flash-time setting means the token in both templates **and** the
`sed` / `-replace` list in both flashers. The lists live in `linux-flasher.sh`
(`flash_rpi` and the Rock 3A path) and in `windows.ps1`, in
`Expand-ProvisioningTokens`, which is one list covering the Raspberry Pi path,
the Rock 3A path, and the window.

The window adds no third list. It writes the same `.mesh-configs/*.conf` file
that `windows.ps1` and `flash-a-radio.sh` already share, then calls
`Build-ProvisioningScript`, so a setting added in the two places above reaches
it with no further work.

Scripts from `additional-scripts/` are inserted **after** substitution and are
never token-substituted, so a script containing a literal `__ADMIN_PW__`
retains it.

They are inserted at an anchor rather than appended. Both templates carry the
line:

```
# >>> MANET_ADDITIONAL_SCRIPTS <<<
```

The flashers replace that line with the generated heredocs, and remove it when
there is nothing to embed. An anchor is required because neither template
executes to its final line: `firstrun.sh.template` ends with completion
messages and `rock3a-provision.sh.template` ends with `reboot`, so an appended
block would never run. A template with the anchor removed causes the flasher to
abort rather than produce an image whose scripts have no effect.

---

## What each launcher fetches

Both launchers select a published release before downloading setup code. Stable
reads `/releases/latest/download/manet-release.json`; development lists published
releases and selects by `published_at` and release ID, excluding drafts and
unrelated releases without a MANET manifest. Both then use the selected tag's
fixed download URL for `manet-flasher.zip` and verify its size and SHA-256.

The publisher builds the ZIP from committed provisioning sources, with a copy
of `manet_release.py`. `MANET/releases/publish.py:PROVISIONING` lists its inputs.
The Linux launcher runs `linux-flasher.sh`; Windows runs the GUI or console
engine. `MANET_RELEASE_FILE` passes the selected manifest, and
`MANET_FLASHER_WORK` keeps saved settings, downloaded images, tool paths and
operator setup scripts outside the temporary extracted bundle. No source files
are overwritten. `--local-scripts` explicitly runs a checkout's scripts against
the selected published packages.

The Linux launcher passes its embedded Python program with `-c`, preserving
stdin for the interactive engine. The Windows launcher carries its PowerShell
bootstrap below the batch entry point and passes channel options through UAC.
Network or verification failures stop startup; there is no fallback to a
cached build from a different channel.

At flash time `prepare-release.py` (Linux) and `Expand-ProvisioningTokens`
(Windows) insert the install URL, digest and version. First boot checks all three
before extraction. The selected package cannot drift to another release between
imaging and first boot. Deleted prereleases require reflashing old test images.

---

## CM4 disk enumeration

The Linux CM4 path takes a disk inventory before `rpiboot` and polls afterward
for at most sixty seconds. One new writable disk must report the same nonzero
size in two consecutive one-second observations. Multiple new disks are an
error, even if one is still reporting zero size. `lsblk` failures stop the flow;
existing disks and loop devices cannot become the automatic target.

`flash-target.py` is included in the release flasher bundle. It checks the whole
disk and all descendant devices, rejects active mapped storage and mounts outside
`/media` or `/run/media`, and reports disk identity for confirmation. The selected
fingerprint includes name, major/minor number, capacity, model, serial, WWN and
the kernel disk sequence when available. `prepare` rechecks it, unmounts allowed
desktop mounts, then rechecks identity and mount state. Linux calls it before
wipefs, dd and rpi-imager. Batch flashing requires typed confirmation for each
target and blank input never authorizes a write. These checks are tested with
simulated inventories; physical unplug/replug behavior still needs a bench test.

## Finding a usable rpi-imager

The flasher passes `--first-run-script`, which is the only way the generated
setup script reaches the card. Builds before 1.8 do not have that option and
reject it *after* the image has been written, so the card looks flashed and
boots a stock Raspberry Pi OS. Ubuntu 22.04 still ships 1.7.2.

Capability is tested, not inferred from the version, and the test has three
outcomes rather than two:

| Outcome | How it shows |
|---|---|
| usable | takes the option |
| too-old | `Unknown option 'first-run-script'` |
| broken | dies in the dynamic loader before parsing anything |

The third case is why a version comparison alone is not enough. Ubuntu 24.04's
1.8.5 package is new enough on paper, and on a 22.04 host it will not start at
all because it wants a newer `GLIBCXX`. A version check reads that as fine. The
probe passes `--first-run-script` with no device, so the option parser rejects
it long before anything could be opened.

`resolve_rpi_imager` takes the first match:

1. a copy in the working folder, when it is *strictly* newer than the system
   one, on the assumption it was put there deliberately
2. the system `rpi-imager`, when the probe says it works
3. the package manager, when its candidate is 1.8.0 or later
4. the AppImage from raspberrypi.org, unpacked once into the working folder

Nothing replaces a working system install. That rule exists because doing it
once, from a non-interactive test where an empty answer took the `[Y/n]`
default, removed a working 1.9.6 from a machine and left 1.7.2 in its place.

The AppImage is unpacked with `--appimage-extract` rather than run directly.
That costs about 110 MB in the folder and avoids FUSE entirely, so no root is
needed to set it up and no re-extraction happens per flash. Raspberry Pi
publishes it for x86_64 only, with no arm64 `.deb` or AppImage, so an arm64
host falls back to its distribution's package or to Flathub, which does build
`org.raspberrypi.rpi-imager` for aarch64.

No checksum is published next to the AppImage, so the download is judged by
whether it runs and accepts the option. Two version banner formats exist and
both print on stderr: `rpi-imager version 1.7.2` on 1.x, and
`Raspberry Pi Imager v2.0.11.1` on 2.x.
