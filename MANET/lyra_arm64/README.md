# Lyra codec artifacts (aarch64)

Two files belong in this directory:

- `libgstlyra.so`: the GStreamer plugin providing `lyraenc`, `lyradec`,
  `rtplyrapay` and `rtplyradepay`
- `model_coeffs/`: the Lyra v2 model weights

Voice defaults to Lyra, so a node needs both. They are committed here because
nodes cannot build them. The build itself runs on a development machine and is
not part of this repository.

A tarball built without them ships without the codec. Nodes from it fall back
to opus, and on a mesh running Lyra they can neither hear nor be heard.
