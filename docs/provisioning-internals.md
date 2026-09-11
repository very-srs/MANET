# Provisioning internals

How the flashers build an image: the template tokens, where the substitution
lists live, and how operator setup scripts are embedded. Operator-facing
instructions are in
[`MANET/provisioning/README.md`](../MANET/provisioning/README.md).

This file is for someone developing on the project. It is not user
documentation.

---

### Template tokens

`flash-a-radio.sh` and `windows.ps1` bake mesh settings into the image by substituting
`__TOKEN__` placeholders in those templates at flash time. Edit the templates
using the tokens, not concrete values; a leftover `__MESH_SSID__` in a flashed
image means the substitution list in the flasher was not updated.

Tokens (same set on Linux and Windows):

`__HARDWARE_MODEL__` `__EUD_CONNECTION__` `__LAN_AP_SSID__` `__LAN_AP_KEY__`
`__MAX_EUDS_PER_NODE__` `__INSTALL_MEDIAMTX__` `__INSTALL_MUMBLE__`
`__VOICE_ENABLED__` `__MESH_SSID__` `__MESH_SAE_KEY__` `__LAN_CIDR_BLOCK__`
`__AUTO_CHANNEL__` `__RADIO_PW__` `__REGULATORY_DOMAIN__`
`__HALOW_REGULATORY_DOMAIN__` `__ADMIN_PW__` `__AUTO_UPDATE__`

Adding a new flash-time setting means the token in both templates **and** the
`sed` / `-replace` list in both flashers. The lists live in `flash-a-radio.sh`
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

Both launchers work from a standalone copy and download what they need, so each
carries a list of files that has to be kept in step with what the flasher
actually reads at run time. Add a new run-time file to the wrong one and a
standalone copy comes up short while a checkout carries on working, which is
the hardest version of this bug to notice.

| Launcher | The list |
|---|---|
| `Flash a Radio.cmd` | the `FILES` line near the top |
| `flash-a-radio.sh` | `FLASHER_FILES` in the Bootstrap block |

Both resolve the branch to a commit through the GitHub API before fetching, and
pull from URLs pinned to that commit. `raw.githubusercontent` caches a branch
URL for several minutes, so fetching `.../main/...` shortly after a change hands
back the previous file and the flasher appears not to have changed at all. A
query string does not help, because that cache ignores it.

`flash-a-radio.sh` also refreshes itself. Overwriting a running bash script
corrupts the rest of the parse, since bash reads it lazily by byte offset, so
the new copy is written and then started with `exec` instead of being spliced
in. `MANET_FLASHER_UPDATED` in the environment is what stops that looping.

Three modes, decided in this order: a folder carrying `.manet-flasher-home` or
named `manet-flasher` is one of ours and gets refreshed; a folder holding
`firstrun.sh.template` is a checkout and is used exactly as it stands; anything
else means the script is on its own and builds itself a folder. The order
matters, because the first run downloads templates into the managed folder, so
a checkout test running first would match from the second run onward and
nothing would ever be refreshed again.
