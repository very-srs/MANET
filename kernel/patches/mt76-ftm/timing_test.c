/* SPDX-License-Identifier: ISC */
/* Userspace harness for the actual patched driver helpers, inserted below. */
#include <assert.h>
#include <stddef.h>
#include <stdint.h>

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
typedef uint32_t __le32;
struct mt76_dev { int unused; };
struct mt7915_dev { struct mt76_dev mt76; };
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

int main(void)
{
	struct mt7915_dev dev = {};
	struct mt76_wcid wcid = {};
	__le32 txs[8] = {};

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
