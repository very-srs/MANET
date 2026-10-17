#!/usr/bin/env python3
"""Exercise the real two-PHY initialization order and firmware EEPROM upload."""
import os
from pathlib import Path
import subprocess
import sys

src, work = map(Path, sys.argv[1:])


def function(file, name):
    text = (src / file).read_text()
    start = text.index(name + '(')
    start = text.rfind('\n', 0, start) + 1
    if text[start:].startswith(name):
        start = text.rfind('\n', 0, start - 1) + 1
    end = text.index('{', start)
    depth = 1
    while depth:
        end += 1
        depth += (text[end] == '{') - (text[end] == '}')
    return text[start:end + 1]


shim = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <stddef.h>
typedef uint8_t u8;
typedef uint32_t u32;
#define BIT(n) (1U << (n))
#define GENMASK(h,l) (((1U << ((h) + 1)) - 1) & ~((1U << (l)) - 1))
#define FIELD_GET(m,v) (((v) & (m)) >> __builtin_ctz(m))
#define u8_get_bits(v,m) FIELD_GET(m,v)
#define u8p_replace_bits(p,v,m) (*(p) = (*(p) & ~(m)) | ((v) << __builtin_ctz(m)))
#define min_t(t,a,b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define hweight8(v) __builtin_popcount((u8)(v))
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
#define MT_EE_MAC_ADDR 4
#define MT_EE_MAC_ADDR2 10
#define ETH_ALEN 6
struct work_struct { int unused; };
struct mt76_phy {
    u8 band_idx, chainmask, antenna_mask, macaddr[6];
    unsigned long state;
    struct { bool has_2ghz, has_5ghz, has_6ghz; } cap;
};
struct mt76_dev {
    struct { u8 *data; } eeprom;
    unsigned chip;
    struct { int idx, hw_key_idx, tx_info; } global_wcid;
    void *wcid[1];
};
struct mt7915_phy;
struct mt7915_dev {
    struct mt76_dev mt76;
    struct mt76_phy mphy;
    struct work_struct init_work;
    bool dbdc_support, flash_mode, background_rx_reserved;
    u8 chainshift, chainmask;
    void *cal;
    struct mt7915_phy *phy0;
};
struct mt7915_phy { struct mt7915_dev *dev; struct mt76_phy *mt76; };
static bool background_radar_2t2r, enable_6ghz;
static u8 uploaded[4096];
static int uploads, upload_error, registered;
#define is_mt7916(m) ((m)->chip == 0x7906)
#define is_mt7915(m) ((m)->chip == 0x7915)
#define is_mt798x(m) ((m)->chip == 0x7981 || (m)->chip == 0x7986)
#define dev_info(...) ((void)0)
#define dev_err(...) ((void)0)
#define INIT_WORK(...) ((void)0)
#define INIT_DELAYED_WORK(...) ((void)0)
#define mt76_wr(...) ((void)0)
#define set_bit(...) ((void)0)
#define mt7915_dma_init(...) 0
#define mt7915_mcu_init(...) 0
#define mt7915_mcu_apply_group_cal(...) 0
#define mt76_wcid_alloc(...) 0
#define MT_WCID_TX_INFO_SET 1
#define rcu_assign_pointer(p,v) ((p) = (v))
#define is_valid_ether_addr(...) true
#define mt76_eeprom_override(...) 0
#define mt7915_init_wiphy(...) ((void)0)
#define mt76_register_phy(...) (++registered, 0)
#define mt76_unregister_phy(...) ((void)0)
#define mt7915_thermal_init(...) 0
#define mt7915_init_debugfs(...) ((void)0)
#define mt7915_mac_init(...) ((void)0)
#define mt7915_txbf_init(...) ((void)0)
#define container_of(p,t,m) ((t *)((char *)(p) - offsetof(t,m)))
static int mt7915_mcu_set_eeprom(struct mt7915_dev *dev) {
    uploads++;
    memcpy(uploaded, dev->mt76.eeprom.data, sizeof(uploaded));
    return upload_error;
}
'''

init_stub = r'''
/* IO is mocked; all EEPROM transformation and PHY parsing below is real C. */
static int mt7915_eeprom_init(struct mt7915_dev *dev) {
    int ret = mt7915_eeprom_reserve_background_rx(dev);
    if (ret) return ret;
    mt7915_eeprom_parse_hw_cap(dev, dev->phy0);
    return 0;
}
'''

tests = r'''
int main(void) {
    u8 ee[4096], original[4096];
    struct mt7915_dev d;
    struct mt76_phy m1;
    struct mt7915_phy p0 = { &d, &d.mphy }, p1 = { &d, &m1 };
    for (int reserve = 0; reserve <= 1; reserve++) {
      for (int selectable = 1; selectable >= 0; selectable--) {
        for (int fail = 0; fail <= 1; fail++) {
          memset(&d, 0, sizeof(d)); memset(&m1, 0, sizeof(m1));
          memset(ee, 0xa5, sizeof(ee));
          ee[0x190] = 0x12; ee[0x191] = selectable ? 0xdb : 0x5b;
          ee[0x192] = 0x48; ee[0x193] = 0x4d;
          memcpy(original, ee, sizeof(ee));
          d.mt76.eeprom.data = ee; d.mt76.chip = 0x7906;
          d.dbdc_support = true; d.phy0 = &p0; m1.band_idx = 1;
          background_radar_2t2r = reserve;
          uploads = registered = 0; upload_error = fail ? -EIO : 0;
          int ret = mt7915_init_hardware(&d, &p1);
          if (reserve) {
            assert(uploads == 1);
            /* Catch a stale selectable band in the actual early upload. */
            assert(uploaded[0x191] == 0x52);
            assert(uploaded[0x193] == 0x49);
            assert(d.chainmask == 0x0f && m1.chainmask == 0x0c);
            assert(m1.cap.has_5ghz && !m1.cap.has_6ghz);
            assert(ret == (fail ? -EIO : 0));
            if (fail) { assert(!registered); continue; }
          } else {
            assert(ret == 0 && uploads == 0);
          }
          assert(mt7915_register_ext_phy(&d, &p1) == 0);
          mt7915_init_work(&d.init_work);
          assert(uploads == 1 && registered == 1);
          assert(!memcmp(uploaded, ee, sizeof(ee)));
          assert(ee[0x191] == (reserve ? 0x52 : 0x5b));
          assert(ee[0x193] == (reserve ? 0x49 : 0x4d));
          for (unsigned i = 0; i < sizeof(ee); i++)
            if (i != 0x191 && i != 0x193) assert(ee[i] == original[i]);
          assert(d.mphy.chainmask == 3 && m1.antenna_mask == 3);
          assert(m1.chainmask == (reserve ? 0x0c : 0x1c));
        }
      }
    }
    // No secondary PHY must never upload a partial reserved image.
    ee[0x190] = 0x12; ee[0x191] = 0xdb; ee[0x193] = 0x4c;
    background_radar_2t2r = true; uploads = 0;
    assert(mt7915_init_hardware(&d, NULL) == -EINVAL && uploads == 0);
    puts("PASS: real init order, selected-band upload, OFDM paths, no later drift, upload failure and default-off");
}
'''

source = shim + '\n'.join(function('eeprom.c', name) for name in (
    'mt7915_eeprom_reserve_background_rx',
    'mt7915_eeprom_parse_band_config', 'mt7915_eeprom_parse_hw_cap'))
source += init_stub + '\n'.join(function('init.c', name) for name in (
    'mt7915_init_work', 'mt7915_init_hardware', 'mt7915_register_ext_phy')) + tests
test_c = work / 'initialization-host.c'
test_c.write_text(source)
binary = test_c.with_suffix('')
subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                '-Wno-unused-parameter', '-fsanitize=undefined,address',
                '-fno-pie', '-no-pie', str(test_c), '-o', str(binary)], check=True)
subprocess.run([str(binary)], check=True,
               env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
