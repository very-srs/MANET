#!/usr/bin/env python3
"""
Waveshare UPS HAT (E) battery reader.

Hardware: IP2368 power management MCU at I2C bus 1, address 0x2D.
INA219 at 0x40 is internal to the HAT and not directly accessible.

Writes /run/battery_status.json atomically every READ_INTERVAL seconds.
Triggers graceful poweroff when any cell drops below CELL_LOW_MV while discharging.

JSON schema:
  {
    "percentage":   85,       # SOC% from MCU fuel gauge
    "voltage_v":    16.200,   # battery pack voltage in V
    "current_ma":  -312,      # negative=discharging, positive=charging
    "power_w":      0.0,      # VBUS power in W
    "charging":     false,
    "status":       "discharging",  # charging | fast_charging | discharging | idle | unknown
    "cell_mv":      [4050, 4050, 4050, 4050],  # individual cell voltages in mV
    "timestamp":    1713394823
  }

MCU register map (I2C addr 0x2D, little-endian 16-bit values):
  0x02        Status byte (bit6=fast charging, bit7=charging, bit5=discharging)
  0x10-0x15   VBUS: voltage(mV), current(mA), power(mW)  — 3×uint16 LE
  0x20-0x2B   Battery: voltage(mV), current(mA signed), percent, capacity(mAh),
              runtime_to_empty(min), time_to_full(min)   — 6×uint16 LE
  0x30-0x37   Cell voltages V1-V4                        — 4×uint16 LE
"""

import json
import logging
import os
import subprocess
import sys
import time

I2C_BUS              = 1
MCU_ADDR             = 0x2D
READ_INTERVAL        = 30        # seconds
OUTPUT_FILE          = "/run/battery_status.json"
CELL_LOW_MV          = 3150      # mV — matches Waveshare sample
CHARGE_THRESHOLD_MA  = 50        # mA above this = charging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s battery-reader %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("battery-reader")


def open_bus():
    for mod in ("smbus2", "smbus"):
        try:
            m = __import__(mod)
            return m.SMBus(I2C_BUS)
        except ImportError:
            continue
    log.error("Neither smbus2 nor smbus installed — run: apt-get install python3-smbus")
    sys.exit(1)


def read_u16le(data, offset):
    return data[offset] | (data[offset + 1] << 8)


def read_s16le(data, offset):
    v = read_u16le(data, offset)
    return v - 0x10000 if v > 0x7FFF else v


def read_battery(bus):
    # Status byte
    status_data = bus.read_i2c_block_data(MCU_ADDR, 0x02, 1)
    status_byte = status_data[0]

    if status_byte & 0x40:
        status = "fast_charging"
        charging = True
    elif status_byte & 0x80:
        status = "charging"
        charging = True
    elif status_byte & 0x20:
        status = "discharging"
        charging = False
    else:
        status = "idle"
        charging = False

    # VBUS: voltage(mV), current(mA), power(mW)
    vbus_data = bus.read_i2c_block_data(MCU_ADDR, 0x10, 6)
    vbus_mv  = read_u16le(vbus_data, 0)
    vbus_ma  = read_u16le(vbus_data, 2)
    vbus_mw  = read_u16le(vbus_data, 4)

    # Battery
    batt_data = bus.read_i2c_block_data(MCU_ADDR, 0x20, 12)
    batt_mv   = read_u16le(batt_data, 0)
    batt_ma   = read_s16le(batt_data, 2)
    batt_pct  = read_u16le(batt_data, 4)

    # Cell voltages
    cell_data = bus.read_i2c_block_data(MCU_ADDR, 0x30, 8)
    cells = [read_u16le(cell_data, i * 2) for i in range(4)]

    return {
        "percentage": batt_pct,
        "voltage_v":  round(batt_mv / 1000, 3),
        "current_ma": batt_ma,
        "power_w":    round(vbus_mw / 1000, 3),
        "charging":   charging,
        "status":     status,
        "cell_mv":    cells,
        "timestamp":  int(time.time()),
    }


def write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def main():
    log.info("Starting — I2C bus %d MCU addr 0x%02X", I2C_BUS, MCU_ADDR)
    bus = open_bus()

    shutdown_triggered = False
    consecutive_errors = 0

    while True:
        try:
            data = read_battery(bus)
            consecutive_errors = 0
            write_atomic(OUTPUT_FILE, data)

            pct = data["percentage"]
            log.info("%d%% | %.3fV | %dmA | %.3fW | %s | cells=%s",
                     pct, data["voltage_v"], data["current_ma"], data["power_w"],
                     data["status"], data["cell_mv"])

            if not shutdown_triggered and not data["charging"]:
                low_cells = [v for v in data["cell_mv"] if 0 < v < CELL_LOW_MV]
                if low_cells:
                    log.critical("Cell voltage critical %s mV — initiating graceful shutdown", low_cells)
                    shutdown_triggered = True
                    subprocess.run(["systemctl", "poweroff"], check=False)

        except OSError as e:
            consecutive_errors += 1
            log.error("I2C read error (%d consecutive): %s", consecutive_errors, e)
            if consecutive_errors == 1:
                write_atomic(OUTPUT_FILE, {
                    "percentage": None, "voltage_v": None, "current_ma": None,
                    "power_w": None, "charging": None, "status": "unknown",
                    "cell_mv": None, "timestamp": int(time.time()),
                })

        time.sleep(READ_INTERVAL)


if __name__ == "__main__":
    main()
