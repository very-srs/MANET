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
| **Subtotal (Compute & Carrier)** |  | **~$144+** _(CM4 at >$115)_ |  

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
| Copper Heatsink 15×15×3mm (4 pcs) | Thermal management for the MT7916 / module | $9.19 | [amazon](https://www.amazon.com/dp/B0F8V5729T) |
| **Subtotal (Radio)** |  | **$79.52** _(with HaLow Option A; Option B priced separately)_ |  |

> **HaLow antenna note:** the 915 MHz antenna below suits the NA 902–928 MHz band. For EU 863–870 MHz (Option B), use an 868 MHz antenna instead.

---

## Antennas

| Name | Description | Cost | Link |
|------|------------|------|------|
| U.FL IPX to RP-SMA Female Pigtail Coax Cable (5 pcs) | Connects WiFi card to external antennas | $8.99 | [amazon](https://www.amazon.com/dp/B093247PHX) |
| Dual Band WiFi 2.4/5/5.8 GHz 8 dBi MIMO RP-SMA Antennas (4-Pack) | Generic antennas for 802.11ax card | $11.99 | [amazon](https://www.amazon.com/dp/B07W4T7HX2) |
| 915 MHz Antenna 3 dBi Omni SMA Male | Generic 915 MHz antenna (NA HaLow use; see EU note above) | $9.99 | [amazon](https://www.amazon.com/dp/B0CTXKBMH9) |
| **Subtotal (Antennas)** |  | **$30.97** |  |

---

## PTT

| Name | Description | Cost | Link |
|------|------------|------|------|
| USB to 3.5mm Jack Audio Adapter | Provides audio interface for PTT setup | $12.99 | [amazon](https://www.amazon.com/dp/B0FF4XLZ3) |
| Female-to-Female Dupont Jumper Wires (40 pcs, 20 cm) | Wiring for connections; plan quantity if building multiple units | $3.99 | [amazon](https://www.amazon.com/dp/B0BRTJQGS6) |
| Amphenol U-283/U (GC283/U) SINCGARS 6 Pin Connector | Military communications connector for NATO-style interface | $14.95 | [ebay](https://www.ebay.com/itm/275879772929) |
| 3.5mm TRS to Bare Wire Pigtail | Audio wiring interface cable | $11.89 | [amazon](https://www.amazon.com/dp/B082VW49VV) |
| Conference Call Amplified Push-To-Talk | External amplified PTT device | $20.00 | [northernstrategic](https://northernstrategic.com/products/conference-call-amplified-ptt) |
| **Subtotal (PTT)** |  | **$63.82** |  |

---

## Enclosure
> - The enclosure BOM has not finalized.  Changes will happen, listed here only for a reference not for actually ordering

| Name | Description | Cost | Link |
|------|------------|------|------|
| Standoffs (CM4 IO Board ↔ PiSugar) | Mounting standoffs between boards | $1.00 | [mouser](https://www.mouser.com/ProductDetail/Essentra/R907-2) |
| M2.5 Screws | Fasteners for assembly | $6.00 | [amazon](https://www.amazon.com/dp/B0DDYDSGBY) |
| **Power Option 1 (planned):** PiSugar 3 Plus | UPS / power management module for SBC | $50.00 | [pisugar](https://www.pisugar.com/products/pisugar-3-plus-raspberry-pi-ups) |
| Pogo Pins for PiSugar | Electrical contacts for PiSugar connection | $12.00 | [digikey](https://www.digikey.com/en/products/detail/adam-tech/PH-MVP-4355/9831576) |
| PiSugar Battery (1000 mAh) | Backup battery | $10.00 | [mouser](https://www.mouser.com/ProductDetail/TinyCircuits/ASR00012) |
| **Power Option 2 (alternative):** Waveshare UPS HAT (E) | IP2368-based UPS. Also supported by `battery-reader.py` (I²C MCU @ 0x2D); that monitor was contributed from an RPi5 build in a large enclosure. Use instead of the PiSugar if preferred. | _confirm_ | [waveshare](https://www.waveshare.com/) |
| UPS HAT (E) battery | 18650 cells (Option 2 only) | _confirm_ | n/a |
| Ethernet Plug (1K FGG/EGG, 8 Pin) | Ruggedized Ethernet connector | $24.10 | [aliexpress](https://www.aliexpress.us/item/3256801810967302.html) |
| Ethernet Plug Cap (BHG, 1K Series) | Protective cap for Ethernet connector | $7.67 | [aliexpress](https://www.aliexpress.us/item/3256802111495954.html) |
| Momentary LED Switch (Tri-color, 12mm) | Panel-mount switch with LED ring | $2.26 | [aliexpress](https://www.aliexpress.us/item/3256806568472829.html) |
| Volume / Power knob | Vishay P16SNP103MAB15 10 kΩ 20% Linear IP67 | $21.63 | [mouser](https://www.mouser.com/ProductDetail/Vishay-Sfernice/P16SNP103MAB15) |
| MOSFET for switching power | DMP3130L-7 | $0.57 | [mouser](https://www.mouser.com/ProductDetail/Diodes-Incorporated/DMP3130L-7) |
| Case heatsink | SK 665 75 SA. Thermal solution should still be validated for the enclosure (the Rock 3A → CM4 move was driven by enclosure overheating). | $9.00 | n/a |
| **Subtotal (Enclosure)** |  | **$144.23** _(PiSugar path; UPS HAT (E) priced separately)_ |  |

---

# Total Cost

| Section | Cost |
|--------|------|
| Compute & Carrier | ~$144+ _(CM4 at >$115)_ |
| Radio | $79.52 _(HaLow Option A)_ |
| Antennas | $30.97 |
| PTT | $63.82 |
| Enclosure | $144.23 _(PiSugar path)_ |
| **Grand Total** | **~$463+** _(CM4 street price drives the floor; Lunpid Option B, UPS HAT (E), and the USB-C cable are not included in this figure)_ |

> **Notes**
> - Prices are indicative and were last reviewed June 2026; verify before ordering.
