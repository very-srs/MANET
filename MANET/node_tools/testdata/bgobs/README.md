# Reservation-v2 observer captures

`reservation-v2.json` contains every raw histogram record from the 2026-10-17
r3a.2 bench under
`kernel-work/acsflap-20261014/reservation-v2/bench-results/r3a.2/obs/`.
Each entry preserves the parsed record, relative source path, and SHA-256 of
the original file. The iperf JSON files are excluded. Tests use this snapshot
without depending on an ignored kernel-work directory or attached radios.

Main link: 5180 MHz HT20, full reservation repair, threshold bucket 2,
1000 ms requested observation. `far/` compares 5200 and 5745 MHz during
local TX without an injector. Other observations are on 5200 MHz.

Quiet, idle injected controls, and `250clean/idle-*` establish the low-TX
response. The original 250 pps campaign idle value is pacing-confounded:
it reads 68%, whereas the clean rerun reads 49%. The TX/RX injected phases
are also unsuitable for fitting a correction because the old injector
could catch up in bursts. They remain useful regression records, not
calibrated ground truth. The heavy-TX quiet records must be rejected,
never converted to a low synthetic reading.
