/* SPDX-License-Identifier: ISC */
/* Userspace harness for the actual patched driver helpers, inserted below. */
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>
#include <limits.h>
#include <sys/types.h>

#define BIT(n) (1U << (n))
#define MT_TXS0_TXS_FORMAT (3U << 23)
#define MT_TXS_PPDU_FMT 2
#define MT_TXS0_ACK_ERROR_MASK (7U << 16)
#define IEEE80211_TX_STAT_ACK BIT(0)
#define le32_to_cpu(x) (x)
#define le32_get_bits(x, mask) (((x) & (mask)) >> __builtin_ctz(mask))
#define IEEE80211_SKB_CB(skb) (&(skb)->info)
#define mt76_tx_skb_cb(skb) (&(skb)->cb)

typedef uint8_t u8;
typedef uint64_t u64;
typedef uint32_t __le32;
struct mt76_dev { int mutex; };
struct mt7915_dev {
	struct mt76_dev mt76;
	int tmr_probe_busy, tmr_probe_sent, tmr_probe_skipped;
	int tmr_probe_error, tmr_marked;
	bool tmr_peer_set;
	u8 tmr_peer[6];
	unsigned int tmr_mark;
	struct sk_buff *tmr_probe_skb;
};
struct mt76_wcid { int unused; };
struct sk_buff_head { int unused; };
struct ieee80211_tx_info {
	unsigned int flags;
	struct {
		int ampdu_len, ampdu_ack_len;
		struct { int idx; } rates[1];
	} status;
};
struct sk_buff {
	struct ieee80211_tx_info info;
	struct { u8 flags; } cb;
	unsigned int mark;
};
static struct sk_buff frame, *pending;
static int locks, gets, fills, completions;

static void mt76_tx_status_lock(struct mt76_dev *dev, struct sk_buff_head *list)
{
	assert(!locks++);
}
static void mt76_tx_status_unlock(struct mt76_dev *dev, struct sk_buff_head *list)
{
	assert(locks-- == 1);
}
static struct sk_buff *mt76_tx_status_skb_get(struct mt76_dev *dev,
		struct mt76_wcid *wcid, int pid, struct sk_buff_head *list)
{
	assert(locks == 1 && pid == 3);
	gets++;
	return pending;
}
static void mt76_connac2_mac_fill_txs(struct mt76_dev *dev,
		struct mt76_wcid *wcid, __le32 *data)
{
	assert(locks == 1);
	fills++;
}
static void mt76_tx_status_skb_done(struct mt76_dev *dev,
		struct sk_buff *skb, struct sk_buff_head *list)
{
	assert(locks == 1 && skb == pending);
	completions++;
}
/* DRIVER_HELPERS */

#define __user
#define BIT_ULL(n) (1ULL << (n))
#define cpu_to_le16(x) (x)
#define cpu_to_le32(x) (x)
#define WRITE_ONCE(x, v) ((x) = (v))
#define READ_ONCE(x) (x)
#define unlikely(x) (x)
#define min(a, b) ((a) < (b) ? (a) : (b))
#define atomic_read(p) (*(p))
#define atomic_set(p, v) (*(p) = (v))
#define atomic_inc(p) (++*(p))
#define scnprintf snprintf
#define ETH_ALEN 6
#define IEEE80211_FTYPE_DATA 0x0008
#define IEEE80211_STYPE_QOS_NULLFUNC 0x00c0
#define IEEE80211_FCTL_TODS 0x0100
#define IEEE80211_FCTL_FROMDS 0x0200
#define IEEE80211_QOS_CTL_EOSP 0x10
#define MT_TXD3_BA_DISABLE BIT(28)
#define MT_TXD3_REM_TX_COUNT (31U << 11)
#define MT_TXD7_HW_AMSDU BIT(10)
#define NL80211_STA_INFO_PLINK_STATE 0
#define NL80211_STA_INFO_LOCAL_PM 1
#define NL80211_STA_INFO_PEER_PM 2
#define NL80211_PLINK_ESTAB 4
#define NL80211_MESH_POWER_ACTIVE 1
#define MT76_STATE_RUNNING 0
#define MT76_RESET 1
#define MT76_SCANNING 2
struct station_info { u64 filled; int plink_state, local_pm, peer_pm; };
struct ieee80211_hdr {
	uint16_t frame_control, duration_id;
	u8 addr1[6], addr2[6], addr3[6];
	uint16_t seq_ctrl;
	u8 addr4[6];
} __attribute__((packed));
struct mt76_phy { unsigned long state; bool testmode; };
struct mt7915_phy { struct mt7915_dev *dev; struct mt76_phy *mt76; };
struct ieee80211_sta { u8 addr[6]; };
struct file { void *private_data; };
static int signaled, copy_fault, send_error, fail_after, sends, sleeps;
static int sleep_action, nested_busy;
static int rtnl_held, trylocks;
static unsigned int skip_mask, slept_us, try_at[8];
static unsigned int scan_mask, stopped_mask, reset_mask, testmode_mask;
static int state_checks, scan_checks;
static void *current;
static struct file *burst_file;
static ssize_t mt7915_tmr_probe_write(struct file *, const char *, size_t, loff_t *);
static void assert_probe_status(struct file *file);
static int mt7915_tmr_probe_state(struct mt7915_phy *phy);

static bool test_bit(unsigned int bit, const unsigned long *state)
{
	if (bit == MT76_SCANNING)
		scan_checks++;
	return *state & BIT(bit);
}
static bool mt76_testmode_enabled(struct mt76_phy *phy) { return phy->testmode; }

static int atomic_cmpxchg(int *p, int from, int to)
{
	int old = *p;
	if (old == from)
		*p = to;
	return old;
}
static void mutex_lock(int *p) { assert(!(*p)++); }
static void mutex_unlock(int *p) { assert((*p)-- == 1); }
static int signal_pending(void *task) { return signaled; }
static int rtnl_trylock(void)
{
	struct mt7915_phy *phy = burst_file->private_data;
	assert(!rtnl_held && !phy->dev->mt76.mutex);
	assert(trylocks < 8 && phy->dev->tmr_probe_busy == 1);
	try_at[trylocks] = slept_us;
	return rtnl_held = !(skip_mask & BIT(trylocks++));
}
static void rtnl_unlock(void) { assert(rtnl_held-- == 1); }
static ssize_t simple_read_from_buffer(void *to, size_t count, loff_t *pos,
				      const void *from, size_t available)
{
	assert(*pos >= 0);
	if ((size_t)*pos >= available)
		return 0;
	count = min(count, available - *pos);
	memcpy(to, (const char *)from + *pos, count);
	*pos += count;
	return count;
}
static int copy_from_user(void *dst, const void *src, size_t n)
{
	if (copy_fault)
		return 1;
	memcpy(dst, src, n);
	return 0;
}
static int is_valid_ether_addr(const u8 *addr)
{
	return !(addr[0] & 1) && memcmp(addr, (u8[6]){}, 6);
}
static void ether_addr_copy(u8 *dst, const u8 *src) { memcpy(dst, src, 6); }
static bool ether_addr_equal(const u8 *a, const u8 *b) { return !memcmp(a, b, 6); }
static void le32p_replace_bits(__le32 *p, unsigned int v, unsigned int mask)
{
	*p = (*p & ~mask) | ((v << __builtin_ctz(mask)) & mask);
}
static char *strim(char *s)
{
	char *end;
	while (isspace((unsigned char)*s))
		s++;
	end = s + strlen(s);
	while (end > s && isspace((unsigned char)end[-1]))
		*--end = 0;
	return s;
}
static int sysfs_streq(const char *a, const char *b)
{
	return !strcmp(a, b) || (strlen(a) == strlen(b) + 1 &&
		!strncmp(a, b, strlen(b)) && a[strlen(b)] == '\n');
}
static int kstrtouint(const char *s, int base, unsigned int *v)
{
	char *end;
	unsigned long n;
	errno = 0;
	n = strtoul(s, &end, base);
	if (*s == '-' || !*s || *end || errno || n > UINT_MAX)
		return -EINVAL;
	*v = n;
	return 0;
}
static int mt7915_tmr_probe_send(struct mt7915_phy *phy)
{
	struct mt7915_dev *dev = phy->dev;
	unsigned int slot = BIT(trylocks - 1);
	int ret;

	assert(rtnl_held && !dev->mt76.mutex);
	if (nested_busy)
		assert(mt7915_tmr_probe_write(burst_file, "1 10000", 7, NULL) == -EBUSY);
	phy->mt76->state = (stopped_mask & slot ? 0 : BIT(MT76_STATE_RUNNING)) |
		(reset_mask & slot ? BIT(MT76_RESET) : 0) |
		(scan_mask & slot ? BIT(MT76_SCANNING) : 0);
	phy->mt76->testmode = testmode_mask & slot;
	mutex_lock(&dev->mt76.mutex);
	state_checks++;
	ret = mt7915_tmr_probe_state(phy);
	mutex_unlock(&dev->mt76.mutex);
	if (ret)
		return ret;
	if (send_error && sends == fail_after)
		return send_error;
	sends++;
	dev->tmr_probe_sent++;
	dev->tmr_marked++;
	return 0;
}
static void usleep_range(unsigned int low, unsigned int high)
{
	struct mt7915_phy *phy = burst_file->private_data;
	assert(!rtnl_held && !phy->dev->mt76.mutex);
	assert(low <= 20000 && high == low + 100);
	sleeps++;
	slept_us += low;
	if (sleep_action == 1)
		assert(mt7915_tmr_probe_write(burst_file, "0", 1, NULL) == 1);
	if (sleep_action == 2)
		signaled = 1;
	assert_probe_status(burst_file);
}
/* PROBE_HELPERS */

static void assert_probe_status(struct file *file)
{
	struct mt7915_phy *phy = file->private_data;
	struct mt7915_dev *dev = phy->dev;
	char expected[80], actual[80] = {};
	loff_t pos = 0;
	int len = snprintf(expected, sizeof(expected),
		"running=%d sent=%d skipped=%d error=%d\n", dev->tmr_probe_busy,
		dev->tmr_probe_sent, dev->tmr_probe_skipped, dev->tmr_probe_error);
	assert(mt7915_tmr_probe_read(file, actual, 5, &pos) == 5);
	assert(mt7915_tmr_probe_read(file, actual + 5, sizeof(actual) - 5, &pos) == len - 5);
	assert(!strcmp(actual, expected));
	assert(mt7915_tmr_probe_read(file, actual, sizeof(actual), &pos) == 0);
}

static void reset_burst_stubs(void)
{
	assert(!rtnl_held);
	signaled = send_error = fail_after = sends = sleeps = 0;
	sleep_action = nested_busy = trylocks = 0;
	skip_mask = slept_us = 0;
	scan_mask = stopped_mask = reset_mask = testmode_mask = 0;
	state_checks = scan_checks = 0;
	memset(try_at, 0, sizeof(try_at));
}

static void test_probe_contention(struct file *file)
{
	struct mt7915_phy *phy = file->private_data;
	struct mt7915_dev *dev = phy->dev;
	const int errors[] = {-EBUSY, -ENOLINK, -ENOMEM, -ENETDOWN, -EOPNOTSUPP};

	/* All first/middle/last combinations, including no skips and all skips.
	 * A completed burst consumes exactly COUNT slots with unchanged pacing.
	 */
	for (unsigned int mask = 0; mask < 8; mask++) {
		int skipped = __builtin_popcount(mask);
		reset_burst_stubs();
		skip_mask = mask;
		nested_busy = 1;
		assert(mt7915_tmr_probe_write(file, "3 50000", 7, NULL) == 7);
		assert(trylocks == 3 && sends == 3 - skipped && sleeps == 6);
		assert(try_at[0] == 0 && try_at[1] == 50000 && try_at[2] == 100000);
		assert(dev->tmr_probe_skipped == skipped && dev->tmr_probe_sent == sends);
		assert(dev->tmr_marked == sends && !dev->tmr_probe_error);
		assert(!dev->tmr_probe_busy && !dev->mt76.mutex && !rtnl_held);
		assert_probe_status(file);
		assert(mt7915_tmr_probe_write(file, "0", 1, NULL) == 1);
		assert(dev->tmr_probe_sent == sends && dev->tmr_probe_skipped == skipped);
	}
	reset_burst_stubs();
	skip_mask = 1;
	assert(mt7915_tmr_probe_write(file, "1 10000", 7, NULL) == 7);
	assert(trylocks == 1 && !sends && !sleeps && dev->tmr_probe_skipped == 1);
	assert(!dev->tmr_probe_error && !dev->tmr_probe_busy);
	assert_probe_status(file);

	/* An error after a skip and a send remains fatal, even for EBUSY.
	 * The failed submission is neither a sent nor a skipped slot.
	 */
	for (unsigned int i = 0; i < sizeof(errors) / sizeof(errors[0]); i++) {
		reset_burst_stubs();
		skip_mask = 1;
		send_error = errors[i];
		fail_after = 1;
		assert(mt7915_tmr_probe_write(file, "4 50000", 7, NULL) == errors[i]);
		assert(trylocks == 3 && sends == 1 && sleeps == 6 && !rtnl_held);
		assert(dev->tmr_probe_sent == 1 && dev->tmr_probe_skipped == 1);
		assert(dev->tmr_probe_error == errors[i] && !dev->tmr_probe_busy);
		assert_probe_status(file);
		assert(mt7915_tmr_probe_write(file, "0", 1, NULL) == 1);
		assert(dev->tmr_probe_error == errors[i]);
		assert(dev->tmr_probe_sent == 1 && dev->tmr_probe_skipped == 1);
	}

	/* Cancellation and signals must stop even if RTNL never becomes free. */
	for (int action = 1; action <= 2; action++) {
		int error = action == 1 ? -ECANCELED : -EINTR;
		reset_burst_stubs();
		skip_mask = 7;
		sleep_action = action;
		assert(mt7915_tmr_probe_write(file, "3 50000", 7, NULL) == error);
		assert(trylocks == 1 && !sends && sleeps == 1 && slept_us == 20000);
		assert(!dev->tmr_probe_sent && dev->tmr_probe_skipped == 1);
		assert(dev->tmr_probe_error == error && !dev->tmr_probe_busy);
		assert(!dev->mt76.mutex && !rtnl_held);
		assert_probe_status(file);
	}
	/* The next admitted burst clears both counters and the previous error. */
	reset_burst_stubs();
	assert(mt7915_tmr_probe_write(file, "1 10000", 7, NULL) == 7);
	assert(dev->tmr_probe_sent == 1 && !dev->tmr_probe_skipped);
	assert(!dev->tmr_probe_error && !dev->tmr_probe_busy);
	assert_probe_status(file);
}

static void test_probe_scanning(struct file *file)
{
	struct mt7915_phy *phy = file->private_data;
	struct mt7915_dev *dev = phy->dev;

	/* Every combination of RTNL and scan skips, including overlap and all
	 * slots skipped. No state check occurs unless RTNL was acquired.
	 */
	for (unsigned int rtnl = 0; rtnl < 8; rtnl++) {
		for (unsigned int scan = 0; scan < 8; scan++) {
			int skipped = __builtin_popcount(rtnl | scan);
			reset_burst_stubs();
			skip_mask = rtnl;
			scan_mask = scan;
			assert(mt7915_tmr_probe_write(file, "3 50000", 7, NULL) == 7);
			assert(trylocks == 3 && sleeps == 6 && sends == 3 - skipped);
			assert(try_at[0] == 0 && try_at[1] == 50000 && try_at[2] == 100000);
			assert(state_checks == 3 - __builtin_popcount(rtnl));
			assert(scan_checks == state_checks);
			assert(dev->tmr_probe_skipped == skipped && dev->tmr_probe_sent == sends);
			assert(dev->tmr_marked == sends && !dev->tmr_probe_error);
			assert(!dev->tmr_probe_busy && !dev->mt76.mutex && !rtnl_held);
			assert_probe_status(file);
		}
	}
	reset_burst_stubs();
	scan_mask = 1;
	assert(mt7915_tmr_probe_write(file, "1 10000", 7, NULL) == 7);
	assert(trylocks == 1 && scan_checks == 1 && !sends && !sleeps);
	assert(dev->tmr_probe_skipped == 1 && !dev->tmr_probe_error);
	assert(mt7915_tmr_probe_write(file, "0", 1, NULL) == 1);
	assert(dev->tmr_probe_skipped == 1 && !dev->tmr_probe_sent);
	assert_probe_status(file);

	/* A send, RTNL skip and scan skip precede a fatal state. Scanning on
	 * that fourth slot cannot hide !RUNNING, reset or testmode.
	 */
	for (int fatal = 0; fatal < 3; fatal++) {
		reset_burst_stubs();
		skip_mask = BIT(1);
		scan_mask = BIT(2) | BIT(3);
		if (fatal == 0)
			stopped_mask = BIT(3);
		if (fatal == 1)
			reset_mask = BIT(3);
		if (fatal == 2)
			testmode_mask = BIT(3);
		assert(mt7915_tmr_probe_write(file, "5 50000", 7, NULL) == -ENETDOWN);
		assert(trylocks == 4 && state_checks == 3 && scan_checks == 2);
		assert(sends == 1 && sleeps == 9 && !rtnl_held && !dev->mt76.mutex);
		assert(dev->tmr_probe_sent == 1 && dev->tmr_probe_skipped == 2);
		assert(dev->tmr_probe_error == -ENETDOWN && !dev->tmr_probe_busy);
		assert_probe_status(file);
	}
	/* ENETDOWN from a later submission remains fatal after a scan skip. */
	reset_burst_stubs();
	scan_mask = 1;
	send_error = -ENETDOWN;
	fail_after = 1;
	assert(mt7915_tmr_probe_write(file, "4 10000", 7, NULL) == -ENETDOWN);
	assert(trylocks == 3 && sends == 1 && sleeps == 2);
	assert(dev->tmr_probe_skipped == 1 && dev->tmr_probe_sent == 1);
	assert(dev->tmr_probe_error == -ENETDOWN && !dev->tmr_probe_busy);
	assert(!rtnl_held && !dev->mt76.mutex);
	assert_probe_status(file);

	for (int action = 1; action <= 2; action++) {
		int error = action == 1 ? -ECANCELED : -EINTR;
		reset_burst_stubs();
		scan_mask = 7;
		sleep_action = action;
		assert(mt7915_tmr_probe_write(file, "3 50000", 7, NULL) == error);
		assert(trylocks == 1 && scan_checks == 1 && !sends && sleeps == 1);
		assert(dev->tmr_probe_skipped == 1 && !dev->tmr_probe_sent);
		assert(dev->tmr_probe_error == error && !dev->tmr_probe_busy);
		assert(!dev->mt76.mutex && !rtnl_held);
		assert_probe_status(file);
	}
	reset_burst_stubs();
	assert(mt7915_tmr_probe_write(file, "1 10000", 7, NULL) == 7);
	assert(dev->tmr_probe_sent == 1 && !dev->tmr_probe_skipped);
	assert(!dev->tmr_probe_error && !dev->tmr_probe_busy);
	assert_probe_status(file);
}

static void test_probe_state(void)
{
	struct mt7915_dev dev = {};
	struct mt76_phy mphy = {};
	struct mt7915_phy phy = { .dev = &dev, .mt76 = &mphy };
	const int active_result[8] = {
		-ENETDOWN, 0, -ENETDOWN, -ENETDOWN,
		-ENETDOWN, MT7915_TMR_PROBE_SKIPPED, -ENETDOWN, -ENETDOWN,
	};

	/* Enumerate all radio states with active, canceled and idle bursts. */
	mutex_lock(&dev.mt76.mutex);
	for (int busy = 0; busy <= 2; busy++) {
		dev.tmr_probe_busy = busy;
		for (unsigned int state = 0; state < 8; state++) {
			for (int testmode = 0; testmode <= 1; testmode++) {
				int expected;
				mphy.state = state;
				mphy.testmode = testmode;
				if (busy != 1)
					expected = -ECANCELED;
				else if (testmode)
					expected = -ENETDOWN;
				else
					expected = active_result[state];
				assert(mt7915_tmr_probe_state(&phy) == expected);
			}
		}
	}
	mutex_unlock(&dev.mt76.mutex);
	reset_burst_stubs();
}

static void test_probes(void)
{
	struct mt7915_dev dev = { .tmr_peer_set = true, .tmr_peer = {2, 1} };
	struct mt76_phy mphy = {};
	struct mt7915_phy phy = { .dev = &dev, .mt76 = &mphy };
	struct file file = { .private_data = &phy };
	struct ieee80211_sta sta = { .addr = {2, 1} };
	struct sk_buff skb = { .mark = 119 };
	struct station_info sinfo = { .filled = 7, .plink_state = NL80211_PLINK_ESTAB,
		.local_pm = NL80211_MESH_POWER_ACTIVE, .peer_pm = NL80211_MESH_POWER_ACTIVE };
	struct { struct ieee80211_hdr hdr; u8 qos[2]; } packet = {};
	u8 peer[6] = {2, 1, 2, 3, 4, 5}, local[6] = {2, 6, 7, 8, 9, 10};
	__le32 txd[8];
	const char *invalid[] = {"", "0 10000", "-1 10000", "4097 10000",
		"1 9999", "1 1000001", "121 1000000", "4294967297 10000",
		"1 4294977296", "0 garbage", "1 10000 extra"};
	unsigned int n, interval;
	char buf[64];

	burst_file = &file;
	assert(mt7915_tmr_selected(&dev, &sta, &skb));
	dev.tmr_mark = 118;
	assert(!mt7915_tmr_selected(&dev, &sta, &skb));
	dev.tmr_mark = 119;
	assert(mt7915_tmr_selected(&dev, &sta, &skb));
	dev.tmr_probe_busy = 1;
	assert(!mt7915_tmr_selected(&dev, &sta, &skb));
	dev.tmr_probe_skb = &skb;
	assert(mt7915_tmr_selected(&dev, &sta, &skb));
	dev.tmr_peer_set = false;
	assert(mt7915_tmr_selected(&dev, &sta, &skb));
	dev.tmr_probe_skb = NULL;
	dev.tmr_probe_busy = 0;
	assert(!mt7915_tmr_selected(&dev, &sta, &skb));
	dev.tmr_peer_set = true;
	assert(!mt7915_tmr_selected(&dev, NULL, &skb));
	for (unsigned int i = 0; i < sizeof(invalid) / sizeof(invalid[0]); i++) {
		strcpy(buf, invalid[i]);
		assert(mt7915_tmr_probe_parse(buf, &n, &interval) == -EINVAL);
	}
	strcpy(buf, "4096 10000\n");
	assert(!mt7915_tmr_probe_parse(buf, &n, &interval) && n == 4096);
	strcpy(buf, "120 1000000");
	assert(!mt7915_tmr_probe_parse(buf, &n, &interval) && interval == 1000000);
	strcpy(buf, " 0 \n");
	assert(!mt7915_tmr_probe_parse(buf, &n, &interval) && !n);

	assert(!mt7915_tmr_probe_link(&sinfo));
	sinfo.plink_state = 3;
	assert(mt7915_tmr_probe_link(&sinfo) == -ENOLINK);
	sinfo.plink_state = NL80211_PLINK_ESTAB;
	sinfo.filled = 3;
	assert(mt7915_tmr_probe_link(&sinfo) == -ENOLINK);
	sinfo.filled = 7;
	sinfo.local_pm = 2;
	assert(mt7915_tmr_probe_link(&sinfo) == -EOPNOTSUPP);
	sinfo.local_pm = NL80211_MESH_POWER_ACTIVE;
	sinfo.peer_pm = 0;
	assert(mt7915_tmr_probe_link(&sinfo) == -EOPNOTSUPP);

	mt7915_tmr_probe_header(&packet.hdr, peer, local);
	assert(sizeof(packet) == 32 && packet.hdr.frame_control == 0x03c8);
	assert(!memcmp(packet.hdr.addr1, peer, 6) && !memcmp(packet.hdr.addr3, peer, 6));
	assert(!memcmp(packet.hdr.addr2, local, 6) && !memcmp(packet.hdr.addr4, local, 6));
	assert(!packet.hdr.seq_ctrl && !packet.hdr.duration_id);
	assert(packet.qos[0] == 0x17 && !packet.qos[1]);
	memset(txd, 0xff, sizeof(txd));
	mt7915_tmr_probe_txwi(txd);
	assert((txd[3] & MT_TXD3_BA_DISABLE) && !(txd[7] & MT_TXD7_HW_AMSDU));
	assert(le32_get_bits(txd[3], MT_TXD3_REM_TX_COUNT) == 1);
	assert(txd[2] == UINT_MAX && txd[6] == UINT_MAX);

	assert(mt7915_tmr_probe_write(&file, "1\0 10000", 8, NULL) == -EINVAL);
	copy_fault = 1;
	assert(mt7915_tmr_probe_write(&file, "1 10000", 7, NULL) == -EFAULT);
	copy_fault = 0;
	dev.tmr_peer_set = false;
	assert(mt7915_tmr_probe_write(&file, "1 10000", 7, NULL) == -ENOLINK);
	dev.tmr_peer_set = true;
	dev.tmr_peer[0] = 1;
	assert(mt7915_tmr_probe_write(&file, "1 10000", 7, NULL) == -ENOLINK);
	dev.tmr_peer[0] = 2;
	nested_busy = 1;
	assert(mt7915_tmr_probe_write(&file, "3 50000", 7, NULL) == 7);
	assert(sends == 3 && sleeps == 6 && dev.tmr_probe_sent == 3);
	assert(!dev.tmr_probe_busy && !dev.tmr_probe_error && !dev.mt76.mutex);
	reset_burst_stubs();
	sleep_action = 1;
	assert(mt7915_tmr_probe_write(&file, "3 50000", 7, NULL) == -ECANCELED);
	assert(sends == 1 && sleeps == 1 && dev.tmr_probe_error == -ECANCELED);
	assert(!dev.tmr_probe_busy && !dev.mt76.mutex);
	reset_burst_stubs();
	sleep_action = 2;
	assert(mt7915_tmr_probe_write(&file, "3 50000", 7, NULL) == -EINTR);
	assert(sends == 1 && !dev.tmr_probe_busy);
	reset_burst_stubs();
	send_error = -ENOLINK;
	fail_after = 1;
	assert(mt7915_tmr_probe_write(&file, "3 10000", 7, NULL) == -ENOLINK);
	assert(sends == 1 && dev.tmr_probe_sent == 1 && !dev.tmr_probe_busy);
	assert(mt7915_tmr_probe_write(&file, "0", 1, NULL) == 1);
	assert(!dev.tmr_probe_busy && !dev.mt76.mutex);
	test_probe_contention(&file);
	test_probe_scanning(&file);
}

int main(void)
{
	struct mt7915_dev dev = {};
	struct mt76_wcid wcid = {};
	__le32 txs[8] = {};

	test_probe_state();
	test_probes();

	/* Ordinary traffic still samples rates; probes still complete with ACK
	 * and failure information, including when DMA completion preceded TXS.
	 */
	for (int marked = 0; marked < 2; marked++)
		for (int error = 0; error < 2; error++)
			for (int dma_done = 0; dma_done < 2; dma_done++) {
				frame = (struct sk_buff){};
				frame.cb.flags = (marked ? MT7915_TX_CB_TMR : 0) | dma_done;
				pending = &frame;
				fills = completions = gets = 0;
				txs[0] = error ? MT_TXS0_ACK_ERROR_MASK : 0;
				mt7915_mac_add_txs_skb(&dev, &wcid, 3, txs);
				assert(!locks && fills == !marked && completions == 1);
				assert(gets == 1 && frame.info.status.ampdu_len == 1);
				assert(frame.info.status.ampdu_ack_len == !error);
				assert(frame.info.status.rates[0].idx == -1);
			}
	/* Missing/timed-out skbs do not update rate or complete twice. */
	pending = NULL;
	fills = completions = 0;
	mt7915_mac_add_txs_skb(&dev, &wcid, 3, txs);
	assert(!fills && !completions && !locks);
	/* PPDU reports never enter the skb lifecycle. */
	pending = &frame;
	txs[0] = MT_TXS_PPDU_FMT << 23;
	gets = 0;
	mt7915_mac_add_txs_skb(&dev, &wcid, 3, txs);
	assert(!gets && !fills && !completions && !locks);

	return 0;
}
