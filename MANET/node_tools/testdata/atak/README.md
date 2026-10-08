# ATAK test fixtures

The XML samples use synthetic identities and positions in the equatorial
Atlantic near 0.25 N, 30.25 W. All event timestamps share an artificial epoch
of 2000-01-01T00:00:00Z; elapsed intervals, creation order and expiry intervals
are preserved. Device metadata, headings, heights and battery readings are
test values. The endpoint uses the documentation network 192.0.2.0/24.

Keep the XML structure and protocol distinctions: GPS/manual/unknown provenance,
creator versus transmission time, delivered versus read receipts, and point
versus range/bearing events. These fixtures test those wire semantics; they do
not certify a particular handset or deployment. The duplicate archive element
in the point sample is intentional parser coverage.

`nft-readback.json` retains the nftables 1.1 JSON layout, including omitted
redundant protocol matches. It contains no host addresses or identifiers;
`end0`, `br0` and `manet_atak` are interface/table names used by the service.

`packets.py` constructs packets from the samples. `scenario.py` contains only
assertions for an injected transport and clock. Neither includes a network
client, shell runner or hardware control implementation.

Tests resolve this directory relative to their own files. The package builders
copy the node_tools tree recursively, so these fixtures accompany the tests
under `/usr/local/bin/testdata/atak/`. Keep them together when copying tests;
no optional download or external source tree is needed for these ATAK tests.
The service-unit check reads the source unit in a checkout and the installed
unit under `/etc/systemd/system/` in a node filesystem layout.
