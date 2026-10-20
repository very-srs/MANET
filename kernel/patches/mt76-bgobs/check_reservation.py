#!/usr/bin/env python3
"""Compile actual EEPROM/antenna functions with a host shim, without a radio."""
from pathlib import Path
import subprocess

import sys
import os
work = Path(sys.argv[2])
src = Path(sys.argv[1])


def function(file, name):
    text = (src / file).read_text()
    start = text.index(name + "(")
    start = text.rfind("\n", 0, start) + 1
    # Include a return type on a separate preceding line.
    if text[start:].startswith(name):
        start = text.rfind("\n", 0, start - 1) + 1
    body = text.index("{", start)
    end, depth = body, 1
    while depth:
        end += 1
        depth += (text[end] == "{") - (text[end] == "}")
    return text[start:end + 1]


shim = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
typedef uint8_t u8;
typedef uint32_t u32;
#define BIT(n) (1U << (n))
#define GENMASK(h,l) (((1U << ((h) + 1)) - 1) & ~((1U << (l)) - 1))
#define FIELD_GET(m,v) (((v) & (m)) >> __builtin_ctz(m))
#define u8_get_bits(v,m) FIELD_GET(m,v)
#define u8p_replace_bits(p,v,m) (*(p) = (*(p) & ~(m)) | ((v) << __builtin_ctz(m)))
#define min_t(t,a,b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define hweight8(v) __builtin_popcount((u8)(v))
#define ffs(v) __builtin_ffs(v)
#define dev_info(...) ((void)0)
#define dev_err(...) ((void)0)
#define mutex_lock(...) ((void)0)
#define mutex_unlock(...) ((void)0)
#define MT_BAND1 1
#define MT_EE_WIFI_CONF 0x190
#define MT_EE_WIFI_CONF0_TX_PATH GENMASK(2,0)
#define MT_EE_WIFI_CONF0_RX_PATH GENMASK(5,3)
#define MT_EE_WIFI_CONF0_BAND_SEL GENMASK(7,6)
#define MT_EE_WIFI_CONF_STREAM_NUM GENMASK(7,5)
#define MT_EE_WIFI_CONF3_TX_PATH_B0 GENMASK(1,0)
#define MT_EE_WIFI_CONF3_TX_PATH_B1 GENMASK(5,4)
#define MT_EE_V2_BAND_SEL_2GHZ 0
#define MT_EE_V2_BAND_SEL_5GHZ 1
#define MT_EE_V2_BAND_SEL_6GHZ 2
#define MT_EE_V2_BAND_SEL_5GHZ_6GHZ 3
#define MT_EE_BAND_SEL_DEFAULT 0
#define MT_EE_BAND_SEL_2GHZ 1
#define MT_EE_BAND_SEL_5GHZ 2
struct mt76_phy {
    u8 band_idx, chainmask, antenna_mask;
    struct { bool has_2ghz, has_5ghz, has_6ghz; } cap;
};
struct mt76_dev { struct { u8 *data; } eeprom; unsigned chip; };
struct mt7915_dev {
    unsigned int rx_probe_count;
    struct mt76_dev mt76;
    struct mt76_phy mphy;
    bool dbdc_support, flash_mode, background_rx_reserved;
    u8 chainshift, chainmask;
};
struct mt7915_phy { struct mt7915_dev *dev; struct mt76_phy *mt76; };
static inline u8 mt7915_band_chainshift(struct mt7915_phy *phy) { struct mt7915_dev *dev = phy->dev; if (!dev->dbdc_support) return 0; return phy->mt76->band_idx * dev->chainshift; }
static inline unsigned short mt7915_band_chainmask(struct mt7915_phy *phy) { return phy->mt76->chainmask >> mt7915_band_chainshift(phy); }
struct wiphy { u32 available_antennas_tx; };
struct ieee80211_hw { struct wiphy *wiphy; struct mt7915_phy *phy; };
#define mt7915_hw_dev(hw) ((hw)->phy->dev)
#define mt7915_hw_phy(hw) ((hw)->phy)
#define is_mt7916(m) ((m)->chip == 0x7906)
#define is_mt7915(m) ((m)->chip == 0x7915)
#define is_mt798x(m) ((m)->chip == 0x7981 || (m)->chip == 0x7986)
#define is_mt7981(m) ((m)->chip == 0x7981)
#define mt76_set_stream_caps(...) ((void)0)
#define mt7915_set_stream_vht_txbf_caps(...) ((void)0)
#define mt7915_set_stream_he_caps(...) ((void)0)
static bool background_radar_2t2r;
static bool enable_6ghz;
'''

tests = r'''
int main(void)
{
    u8 ee[4096], original[4096];
    struct mt7915_dev d = { .mt76 = { .eeprom.data = ee, .chip = 0x7906 },
                            .dbdc_support = true };
    struct mt76_phy band1 = { .band_idx = 1 };
    struct mt7915_phy p0 = { &d, &d.mphy }, p1 = { &d, &band1 };
    struct wiphy wiphy = { .available_antennas_tx = 3 };
    struct ieee80211_hw hw = { &wiphy, &p1 };
    memset(ee, 0xa5, sizeof(ee));
    ee[0x190] = 0x12; ee[0x191] = 0x5b; ee[0x193] = 0x4d;
    memcpy(original, ee, sizeof(ee));
    assert(mt7915_eeprom_reserve_background_rx(&d) == 0);
    assert(!memcmp(ee, original, sizeof(ee)) && !d.flash_mode);
    mt7915_eeprom_parse_hw_cap(&d, &p0);
    mt7915_eeprom_parse_hw_cap(&d, &p1);
    assert(d.mphy.chainmask == 3 && band1.chainmask == 0x1c);
    assert(d.chainmask == 0x1f && band1.antenna_mask == 3);
    background_radar_2t2r = true;
    assert(mt7915_eeprom_reserve_background_rx(&d) == 0);
    assert(d.background_rx_reserved && d.flash_mode);
    assert(ee[0x191] == 0x52 && ee[0x193] == 0x49);
    for (unsigned i=0; i<sizeof(ee); i++)
        if (i != 0x191 && i != 0x193) assert(ee[i] == original[i]);
    d.chainmask = 0;
    mt7915_eeprom_parse_hw_cap(&d, &p0);
    mt7915_eeprom_parse_hw_cap(&d, &p1);
    assert(d.mphy.chainmask == 3 && band1.chainmask == 0x0c);
    assert(d.chainmask == 0x0f && band1.antenna_mask == 3);
    assert(mt7915_set_antenna(&hw, 0, 3, 3) == 0 && band1.chainmask == 0xc);
    assert(mt7915_set_antenna(&hw, 0, 7, 7) == -EINVAL);
    assert(mt7915_set_antenna(&hw, 0, 5, 5) == -EINVAL);
    assert(mt7915_set_antenna(&hw, 0, 1, 1) == 0 && band1.chainmask == 4);
    assert(mt7915_set_antenna(&hw, 0, 3, 3) == 0 && band1.chainmask == 0xc);
    // Every unsupported layout must fail without modifying the image.
    for (unsigned b0=0; b0<256; b0++)
        for (unsigned b1=0; b1<256; b1++) {
            memcpy(ee, original, sizeof(ee));
            ee[0x190]=b0; ee[0x191]=b1;
            u8 before[4096]; memcpy(before, ee, sizeof(ee));
            d.background_rx_reserved = d.flash_mode = false;
            int ret=mt7915_eeprom_reserve_background_rx(&d);
            bool valid = b0==0x12 && (b1==0x5a || b1==0x5b || b1==0xda || b1==0xdb);
            assert((ret == 0) == valid);
            if (!valid) assert(!memcmp(ee, before, sizeof(ee)));
            assert(d.background_rx_reserved == valid);
        }
    // Selectable 5/6 GHz band 1 (as shipped on the bench cards): only as 5 GHz.
    memcpy(ee, original, sizeof(ee)); ee[0x191]=0xdb;
    assert(mt7915_eeprom_reserve_background_rx(&d) == 0 && ee[0x191] == 0xd2);
    mt7915_eeprom_parse_hw_cap(&d, &p1);
    assert(ee[0x191] == 0x52 && band1.cap.has_5ghz && !band1.cap.has_6ghz);
    memcpy(ee, original, sizeof(ee)); ee[0x191]=0xdb; enable_6ghz=true;
    assert(mt7915_eeprom_reserve_background_rx(&d) == -EINVAL && ee[0x191] == 0xdb);
    enable_6ghz=false;
    memcpy(ee, original, sizeof(ee)); d.mt76.chip=0x7915;
    assert(mt7915_eeprom_reserve_background_rx(&d) == 0);
    assert(!memcmp(ee, original, sizeof(ee)));
    d.mt76.chip=0x7906; d.dbdc_support=false;
    assert(mt7915_eeprom_reserve_background_rx(&d) == -EINVAL);
    d.dbdc_support=true; ee[0x193]=0x20;
    assert(mt7915_eeprom_reserve_background_rx(&d) == -EINVAL);
    puts("PASS: EEPROM opt-in, scope, byte preservation, masks and antenna reconfiguration");
}
'''

test_c = work / "stage1-host.c"
test_c.write_text(shim + "\n" + "\n".join([
    function("eeprom.c", "mt7915_eeprom_reserve_background_rx"),
    function("eeprom.c", "mt7915_eeprom_parse_band_config"),
    function("eeprom.c", "mt7915_eeprom_parse_hw_cap"),
    function("main.c", "mt7915_set_antenna"),
]) + tests)
binary = test_c.with_suffix("")
subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-Wno-unused-parameter", "-fsanitize=undefined,address",
                str(test_c), "-o", str(binary)], check=True)
subprocess.run([str(binary)], check=True,
               env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0"})
