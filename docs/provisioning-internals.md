# Provisioning internals

How the flashers build an image: the template tokens, where the substitution
lists live, and how operator setup scripts are embedded. Operator-facing
instructions are in
[`MANET/provisioning/README.md`](../MANET/provisioning/README.md).

This file is for someone developing on the project. It is not user
documentation.

---

### Template tokens

`linux.sh` and `windows.ps1` bake mesh settings into the image by substituting
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
`sed` / `-replace` list in both flashers. The lists live in `linux.sh`
(`flash_rpi` and the Rock 3A path) and in `windows.ps1`, in
`Expand-ProvisioningTokens`, which is one list covering the Raspberry Pi path,
the Rock 3A path, and the window.

The window adds no third list. It writes the same `.mesh-configs/*.conf` file
that `windows.ps1` and `linux.sh` already share, then calls
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

