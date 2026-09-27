# Bill of Materials (BOM)

> **Platform:** Raspberry Pi Compute Module 4 (CM4) on a Waveshare CM4 IO Board.
>
> **HaLow (802.11ah) is provided one of two ways, and the software stack supports both:**
> - **MM6108 over SPI.** Seeed Wi-Fi HaLow HAT. **NA 902–928 MHz only.**
> - **MM8108 over USB.** Lunpid USB MM8108 dongle. **NA 902–928 MHz _and_ EU 863–870 MHz**, required for European deployments.
>
> Lines marked **_confirm_** depend on the exact variant/vendor you buy and should be verified before relying on the totals.

---

## Compute & Carrier

| Name | Description | Cost | Link |
|------|------------|------|------|
| Raspberry Pi Compute Module 4 | BCM2711 quad-core, **4 GB RAM / 32 GB eMMC / no onboard Wi-Fi** (mesh Wi-Fi is supplied by the MT7916). | >$115 _(current street price; supply-dependent)_ | [raspberrypi](https://www.raspberrypi.com/products/compute-module-4/) |
| Waveshare CM4-IO-BASE-A | Mini Base Board (A), Lite. 40-pin GPIO header (SPI for the HaLow HAT), M.2 M-key slot (PCIe, MT7916 via adapter), 2× USB 2.0, Gigabit Ethernet, microSD, USB-C power/programming. 85×56 mm. | $28.99 | [waveshare](https://www.waveshare.com/cm4-io-base-a.htm) |

---

## Radio

| Name | Description | Cost | Link |
|------|------------|------|------|
| AW7916-AED Wi-Fi 6E AX3000 M.2 A/E Key Module | Dual-band, dual concurrent 3×3 802.11ax (MT7916) mesh card | $32.00 | [asiarf](https://asiarf.com/product/wi-fi-6e-m-2-ae-key-module-mt7916-aw7916-aed/) |
| M.2 M Key to A + E Key Adapter | Converts the IO-BASE-A M-key M.2 slot to fit the A+E-key MT7916. CM4 also requires the `pcie-32bit-dma` overlay (BCM2711 PCIe window sits above 4 GB). | $4.13 | [aliexpress](https://www.aliexpress.us/item/2255799988809135.html) |
| **HaLow Option A:** Seeed Wio-WM6108 Wi-Fi HaLow mini-PCIe Module (MM6108 / FGH100M-H, SPI) | 802.11ah over SPI. GPIO 17 reset, GPIO 3/7 power, GPIO 5 IRQ, GPIO 8 CS. **NA 902–928 MHz only.** | $14.26 | [seeed](https://www.seeedstudio.com/Wio-WM6108-Wi-Fi-HaLow-mini-PCIe-Module-p-6394.html) |
| **HaLow Option A:** Seeed WM1302 Pi HAT | Carrier HAT that hosts the WM6108 mini-PCIe module on the 40-pin header | $19.94 | [seeed](https://www.seeedstudio.com/WM1302-Pi-Hat-p-4897.html) |
| **HaLow Option B:** Lunpid USB MM8108 HaLow | 802.11ah over USB-C. **NA 902–928 MHz + EU 863–870 MHz.** Ships without antenna or USB cable. Pre-order. Use instead of Option A for EU. | €39.90 (~$43) | [lunpid](https://lunpid.com/products/usb-mm8108-halow) |
| USB-A → USB-C cable/adapter | Connects the Lunpid dongle (USB-C female) to a USB-A port. Option B only. | ~$5 _(confirm)_ | n/a |

> **HaLow antenna note:** the 915 MHz antenna below suits the NA 902–928 MHz band. For EU 863–870 MHz (Option B), use an 868 MHz antenna instead.

---

