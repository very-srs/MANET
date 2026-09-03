# Lyra codec artifacts (aarch64)

Two artifacts belong in this directory, and every tarball builder installs them:

- `libgstlyra.so`: GStreamer plugin providing `lyraenc`, `lyradec`,
  `rtplyrapay`, `rtplyradepay`
- `model_coeffs/`: Lyra v2 model weights

They are **not** produced automatically by any build in this repository; see
"Lyra codec plugin" in `MANET/packaging/README.md` for the two-stage build.

While they are absent, every builder emits a warning and the resulting tarball
ships without the codec. Affected nodes fall back to opus, and on a mesh
configured for lyra (the default) can neither hear nor be heard.
