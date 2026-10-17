/* RTP payloader and depayloader for Lyra v2.
 *
 *   rtplyrapay     audio/x-lyra      ->  application/x-rtp, encoding-name=LYRA
 *   rtplyradepay   application/x-rtp ->  audio/x-lyra
 *
 * There is no standardised RTP payload format for Lyra -- Google never
 * registered one -- so this defines the obvious minimal thing and both ends of
 * the mesh speak it: a standard 12-byte RTP header followed by N whole Lyra
 * frames back to back, no payload header of its own.
 *
 * That works without any extra framing because Lyra frames are a fixed size
 * determined by bitrate: 8, 15 or 23 bytes for 3200, 6000 or 9200 bps. The
 * depayloader recovers the geometry from the payload length alone by finding
 * which of those three divides it. With frames-per-packet capped at 6 the
 * mapping is unambiguous -- the reachable lengths are
 *
 *      8,16,24,32,40,48   15,30,45,60,75,90   23,46,69,92,115,138
 *
 * which share no value. (Uncapped it would eventually collide: 120 bytes is
 * both 15 frames of 8 and 8 frames of 15.) So the cap is correctness, not
 * taste, and MAX_FRAMES_PER_PACKET must not be raised without re-checking it.
 *
 * A useful consequence: because the receiver derives the geometry per packet,
 * the sender can change bitrate or frames-per-packet mid-stream with no
 * signalling, no renegotiation and no receiver state. That is what lets
 * mesh-voice.py adapt packing on a live transmission over one-way multicast,
 * where there is no back channel to negotiate over.
 *
 * frames-per-packet is the airtime/burst-loss tradeoff and the reason this is a
 * property rather than a constant. At 6 kbps the headers dominate completely.
 * Measured on the HaLow link (batman frame carrying one 20 ms frame: 101 bytes
 * captured, of which 86 is header), against a listening test at 10 % loss with
 * the burst length varied to match the packing:
 *
 *      frames/pkt   interval   on-air      a lost packet   how it sounded
 *          1         20 ms     40.4 kbps       20 ms       clean
 *          2         40 ms     23.2 kbps       40 ms       barely audible
 *          3         60 ms     17.5 kbps       60 ms       audible glitches
 *          4         80 ms     14.6 kbps       80 ms       unpleasant
 *
 * Hence the default of 2: it takes 43 % off the wire relative to 20 ms while
 * staying at the edge of audibility. 3 and 4 are available for a link where
 * airtime matters more than fidelity, but they are past the knee.
 *
 * (Those kbps figures are from the Ethernet-framed capture point. Real airtime
 * is higher again -- 802.11 header, PHY preamble, ACK and IFS are per frame,
 * not per byte -- which only sharpens the same conclusion: at these bitrates
 * you are paying for packets, not for audio.)
 */

#include <gst/gst.h>
#include <gst/rtp/gstrtpbuffer.h>
#include <gst/rtp/gstrtpbasepayload.h>
#include <gst/rtp/gstrtpbasedepayload.h>
#include <gst/base/gstadapter.h>

#include "gstlyrartp.h"

GST_DEBUG_CATEGORY_STATIC (lyra_rtp_debug);
#define GST_CAT_DEFAULT lyra_rtp_debug

#define LYRA_RTP_CLOCK_RATE 16000
#define LYRA_FRAME_MS       20
#define LYRA_FRAME_DURATION (LYRA_FRAME_MS * GST_MSECOND)
#define LYRA_FRAME_TS       (LYRA_RTP_CLOCK_RATE / 1000 * LYRA_FRAME_MS)  /* 320 */

#define MIN_FRAMES_PER_PACKET 1
#define MAX_FRAMES_PER_PACKET 6      /* see header comment: any higher is ambiguous */
#define DEFAULT_FRAMES_PER_PACKET 2  /* 40 ms; the knee of the listening test */

#define DEFAULT_PT 111

/* The three legal Lyra frame sizes, one per supported bitrate. */
static const guint lyra_frame_sizes[] = { 8, 15, 23 };

enum { PROP_0, PROP_FRAMES_PER_PACKET };

/* --------------------------------------------------------------- payloader */

typedef struct _GstRtpLyraPay {
  GstRTPBasePayload parent;
  GstAdapter *adapter;          /* whole frames awaiting a packet */
  guint frames_per_packet;
  guint pending_frames;
  guint pending_frame_size;     /* size of the frames currently accumulated */
  GstClockTime pending_pts;     /* PTS of the first pending frame */
  gboolean pending_discont;
} GstRtpLyraPay;

typedef struct _GstRtpLyraPayClass { GstRTPBasePayloadClass parent_class; } GstRtpLyraPayClass;

G_DEFINE_TYPE (GstRtpLyraPay, gst_rtp_lyra_pay, GST_TYPE_RTP_BASE_PAYLOAD);

static GstStaticPadTemplate pay_sink_tmpl = GST_STATIC_PAD_TEMPLATE ("sink",
    GST_PAD_SINK, GST_PAD_ALWAYS,
    GST_STATIC_CAPS ("audio/x-lyra, rate=(int)16000, channels=(int)1"));

static GstStaticPadTemplate pay_src_tmpl = GST_STATIC_PAD_TEMPLATE ("src",
    GST_PAD_SRC, GST_PAD_ALWAYS,
    GST_STATIC_CAPS ("application/x-rtp, media=(string)audio, "
                     "clock-rate=(int)16000, encoding-name=(string)LYRA, "
                     "encoding-params=(string)1"));

static gboolean
gst_rtp_lyra_pay_set_caps (GstRTPBasePayload * base, GstCaps * caps)
{
  gst_rtp_base_payload_set_options (base, "audio", TRUE, "LYRA",
      LYRA_RTP_CLOCK_RATE);
  return gst_rtp_base_payload_set_outcaps (base, "encoding-params",
      G_TYPE_STRING, "1", NULL);
}

/* Emit one RTP packet holding everything currently in the adapter. */
static GstFlowReturn
gst_rtp_lyra_pay_flush (GstRtpLyraPay * self)
{
  GstRTPBasePayload *base = GST_RTP_BASE_PAYLOAD (self);

  if (self->pending_frames == 0)
    return GST_FLOW_OK;

  const guint payload_len = self->pending_frames * self->pending_frame_size;
  GstBuffer *outbuf =
      gst_rtp_base_payload_allocate_output_buffer (base, payload_len, 0, 0);

  GstRTPBuffer rtp = GST_RTP_BUFFER_INIT;
  if (!gst_rtp_buffer_map (outbuf, GST_MAP_WRITE, &rtp)) {
    gst_buffer_unref (outbuf);
    gst_adapter_clear (self->adapter);
    self->pending_frames = 0;
    return GST_FLOW_ERROR;
  }
  gst_adapter_copy (self->adapter, gst_rtp_buffer_get_payload (&rtp), 0,
      payload_len);

  /* The marker bit means "first packet of a talkspurt", which for us is the
   * first packet after the PTT valve opens. mesh-voice.py gates transmit with
   * a valve, and a valve that reopens marks the next buffer DISCONT, so the
   * flag arriving here is exactly the start of a transmission. Receivers use
   * it to reset their jitter buffer rather than treat the silence as loss. */
  gst_rtp_buffer_set_marker (&rtp, self->pending_discont);
  if (self->pending_discont)
    GST_BUFFER_FLAG_SET (outbuf, GST_BUFFER_FLAG_DISCONT);
  gst_rtp_buffer_unmap (&rtp);

  GST_BUFFER_PTS (outbuf) = self->pending_pts;
  GST_BUFFER_DURATION (outbuf) = self->pending_frames * LYRA_FRAME_DURATION;

  gst_adapter_flush (self->adapter, payload_len);
  self->pending_frames = 0;
  self->pending_discont = FALSE;

  return gst_rtp_base_payload_push (base, outbuf);
}

static GstFlowReturn
gst_rtp_lyra_pay_handle_buffer (GstRTPBasePayload * base, GstBuffer * buf)
{
  GstRtpLyraPay *self = (GstRtpLyraPay *) base;
  GstFlowReturn ret = GST_FLOW_OK;
  const gsize size = gst_buffer_get_size (buf);

  gboolean valid = FALSE;
  for (guint i = 0; i < G_N_ELEMENTS (lyra_frame_sizes); i++)
    if (size == lyra_frame_sizes[i]) valid = TRUE;

  if (!valid) {
    /* Not a Lyra frame for any supported bitrate. Dropping one frame is
     * recoverable; passing it on would corrupt the whole packet's geometry and
     * take the other frames with it. */
    GST_WARNING_OBJECT (self, "dropping %" G_GSIZE_FORMAT "-byte buffer "
        "(expected 8, 15 or 23)", size);
    gst_buffer_unref (buf);
    return GST_FLOW_OK;
  }

  /* bitrate is settable while playing, so the frame size can change between
   * one frame and the next. Frames of different sizes cannot share a packet --
   * the receiver recovers the frame count by division and mixed sizes make
   * that unsolvable -- so a size change forces the pending packet out first. */
  if (self->pending_frames > 0 && size != self->pending_frame_size) {
    GST_DEBUG_OBJECT (self, "frame size %u -> %" G_GSIZE_FORMAT
        ", flushing early", self->pending_frame_size, size);
    ret = gst_rtp_lyra_pay_flush (self);
    if (ret != GST_FLOW_OK) { gst_buffer_unref (buf); return ret; }
  }

  if (self->pending_frames == 0) {
    self->pending_pts = GST_BUFFER_PTS (buf);
    self->pending_frame_size = size;
  }
  if (GST_BUFFER_IS_DISCONT (buf))
    self->pending_discont = TRUE;

  gst_adapter_push (self->adapter, buf);
  self->pending_frames++;

  if (self->pending_frames >= self->frames_per_packet)
    ret = gst_rtp_lyra_pay_flush (self);

  return ret;
}

static gboolean
gst_rtp_lyra_pay_sink_event (GstRTPBasePayload * base, GstEvent * event)
{
  GstRtpLyraPay *self = (GstRtpLyraPay *) base;

  switch (GST_EVENT_TYPE (event)) {
    case GST_EVENT_EOS:
      /* Send the partial packet rather than swallowing the tail of a
       * transmission. A short packet is legal: the receiver divides. */
      gst_rtp_lyra_pay_flush (self);
      break;
    case GST_EVENT_FLUSH_STOP:
      gst_adapter_clear (self->adapter);
      self->pending_frames = 0;
      self->pending_discont = TRUE;
      break;
    default:
      break;
  }
  return GST_RTP_BASE_PAYLOAD_CLASS (gst_rtp_lyra_pay_parent_class)
      ->sink_event (base, event);
}

static GstStateChangeReturn
gst_rtp_lyra_pay_change_state (GstElement * element, GstStateChange transition)
{
  GstRtpLyraPay *self = (GstRtpLyraPay *) element;

  if (transition == GST_STATE_CHANGE_READY_TO_PAUSED) {
    gst_adapter_clear (self->adapter);
    self->pending_frames = 0;
    self->pending_frame_size = 0;
    self->pending_discont = TRUE;
  }
  return GST_ELEMENT_CLASS (gst_rtp_lyra_pay_parent_class)
      ->change_state (element, transition);
}

static void
gst_rtp_lyra_pay_set_property (GObject * object, guint id, const GValue * value,
    GParamSpec * pspec)
{
  GstRtpLyraPay *self = (GstRtpLyraPay *) object;
  if (id == PROP_FRAMES_PER_PACKET)
    self->frames_per_packet = g_value_get_uint (value);
  else
    G_OBJECT_WARN_INVALID_PROPERTY_ID (object, id, pspec);
}

static void
gst_rtp_lyra_pay_get_property (GObject * object, guint id, GValue * value,
    GParamSpec * pspec)
{
  GstRtpLyraPay *self = (GstRtpLyraPay *) object;
  if (id == PROP_FRAMES_PER_PACKET)
    g_value_set_uint (value, self->frames_per_packet);
  else
    G_OBJECT_WARN_INVALID_PROPERTY_ID (object, id, pspec);
}

static void
gst_rtp_lyra_pay_finalize (GObject * object)
{
  GstRtpLyraPay *self = (GstRtpLyraPay *) object;
  g_clear_object (&self->adapter);
  G_OBJECT_CLASS (gst_rtp_lyra_pay_parent_class)->finalize (object);
}

static void
gst_rtp_lyra_pay_class_init (GstRtpLyraPayClass * klass)
{
  GObjectClass *gobject_class = G_OBJECT_CLASS (klass);
  GstElementClass *element_class = GST_ELEMENT_CLASS (klass);
  GstRTPBasePayloadClass *base = GST_RTP_BASE_PAYLOAD_CLASS (klass);

  gobject_class->set_property = gst_rtp_lyra_pay_set_property;
  gobject_class->get_property = gst_rtp_lyra_pay_get_property;
  gobject_class->finalize = gst_rtp_lyra_pay_finalize;

  g_object_class_install_property (gobject_class, PROP_FRAMES_PER_PACKET,
      g_param_spec_uint ("frames-per-packet", "Frames per packet",
          "Lyra frames (20 ms each) per RTP packet. Higher cuts header "
          "overhead but turns each lost packet into a longer burst. Settable "
          "while playing: mesh-voice.py adapts it to measured loss.",
          MIN_FRAMES_PER_PACKET, MAX_FRAMES_PER_PACKET,
          DEFAULT_FRAMES_PER_PACKET,
          (GParamFlags) (G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS
                         | GST_PARAM_MUTABLE_PLAYING)));

  element_class->change_state = gst_rtp_lyra_pay_change_state;
  base->set_caps = gst_rtp_lyra_pay_set_caps;
  base->handle_buffer = gst_rtp_lyra_pay_handle_buffer;
  base->sink_event = gst_rtp_lyra_pay_sink_event;

  gst_element_class_add_static_pad_template (element_class, &pay_sink_tmpl);
  gst_element_class_add_static_pad_template (element_class, &pay_src_tmpl);
  gst_element_class_set_static_metadata (element_class, "RTP Lyra payloader",
      "Codec/Payloader/Network/RTP", "Packetize Lyra frames into RTP",
      "MANET");
}

static void
gst_rtp_lyra_pay_init (GstRtpLyraPay * self)
{
  self->adapter = gst_adapter_new ();
  self->frames_per_packet = DEFAULT_FRAMES_PER_PACKET;
  self->pending_frames = 0;
  self->pending_frame_size = 0;
  self->pending_discont = TRUE;
  GST_RTP_BASE_PAYLOAD (self)->pt = DEFAULT_PT;
}

/* ------------------------------------------------------------- depayloader */

typedef struct _GstRtpLyraDepay {
  GstRTPBaseDepayload parent;
  guint last_frame_size;    /* remembered so packet_lost knows what to conceal */
  guint last_frame_count;
} GstRtpLyraDepay;

typedef struct _GstRtpLyraDepayClass { GstRTPBaseDepayloadClass parent_class; } GstRtpLyraDepayClass;

G_DEFINE_TYPE (GstRtpLyraDepay, gst_rtp_lyra_depay, GST_TYPE_RTP_BASE_DEPAYLOAD);

static GstStaticPadTemplate depay_sink_tmpl = GST_STATIC_PAD_TEMPLATE ("sink",
    GST_PAD_SINK, GST_PAD_ALWAYS,
    GST_STATIC_CAPS ("application/x-rtp, media=(string)audio, "
                     "clock-rate=(int)16000, encoding-name=(string)LYRA"));

static GstStaticPadTemplate depay_src_tmpl = GST_STATIC_PAD_TEMPLATE ("src",
    GST_PAD_SRC, GST_PAD_ALWAYS,
    GST_STATIC_CAPS ("audio/x-lyra, rate=(int)16000, channels=(int)1"));

static gboolean
gst_rtp_lyra_depay_set_caps (GstRTPBaseDepayload * base, GstCaps * caps)
{
  GstCaps *out = gst_caps_new_simple ("audio/x-lyra",
      "rate", G_TYPE_INT, LYRA_RTP_CLOCK_RATE,
      "channels", G_TYPE_INT, 1, NULL);
  gboolean ok = gst_pad_set_caps (GST_RTP_BASE_DEPAYLOAD_SRCPAD (base), out);
  gst_caps_unref (out);
  return ok;
}

/* Recover the frame size from the payload length. Unambiguous while
 * frames-per-packet <= MAX_FRAMES_PER_PACKET; see the header comment. */
static gboolean
lyra_split_payload (guint len, guint * frame_size, guint * n_frames)
{
  for (guint i = 0; i < G_N_ELEMENTS (lyra_frame_sizes); i++) {
    const guint s = lyra_frame_sizes[i];
    if (len % s == 0) {
      const guint n = len / s;
      if (n >= MIN_FRAMES_PER_PACKET && n <= MAX_FRAMES_PER_PACKET) {
        *frame_size = s;
        *n_frames = n;
        return TRUE;
      }
    }
  }
  return FALSE;
}

static GstBuffer *
gst_rtp_lyra_depay_process (GstRTPBaseDepayload * base, GstRTPBuffer * rtp)
{
  GstRtpLyraDepay *self = (GstRtpLyraDepay *) base;
  const guint len = gst_rtp_buffer_get_payload_len (rtp);
  guint frame_size, n_frames;

  if (!lyra_split_payload (len, &frame_size, &n_frames)) {
    GST_WARNING_OBJECT (self, "payload of %u bytes is not a whole number of "
        "Lyra frames; dropping", len);
    return NULL;
  }
  self->last_frame_size = frame_size;
  self->last_frame_count = n_frames;

  /* One buffer per frame: lyradec decodes exactly one 20 ms frame per call,
   * and splitting here rather than downstream keeps the PTS of each frame
   * right, which is what the jitter buffer and the sink both rely on. */
  const GstClockTime base_pts = GST_BUFFER_PTS (rtp->buffer);
  GstBufferList *frames = gst_buffer_list_new_sized (n_frames);

  for (guint i = 0; i < n_frames; i++) {
    GstBuffer *frame = gst_rtp_buffer_get_payload_subbuffer (rtp,
        i * frame_size, frame_size);
    if (!frame) continue;
    if (GST_CLOCK_TIME_IS_VALID (base_pts))
      GST_BUFFER_PTS (frame) = base_pts + i * LYRA_FRAME_DURATION;
    GST_BUFFER_DURATION (frame) = LYRA_FRAME_DURATION;
    if (i == 0 && gst_rtp_buffer_get_marker (rtp))
      GST_BUFFER_FLAG_SET (frame, GST_BUFFER_FLAG_DISCONT);
    gst_buffer_list_add (frames, frame);
  }

  gst_rtp_base_depayload_push_list (base, frames);
  return NULL;   /* already pushed */
}

static gboolean
gst_rtp_lyra_depay_packet_lost (GstRTPBaseDepayload * base, GstEvent * event)
{
  GstRtpLyraDepay *self = (GstRtpLyraDepay *) base;

  /* rtpjitterbuffer do-lost=true calls this for every packet it gave up on.
   * One lost packet is frames-per-packet lost FRAMES, and lyradec conceals
   * exactly one frame per call, so the count has to be reconstructed here or
   * the output drifts short by 20 ms per frame every time a packet goes
   * missing. We use the geometry of the last packet that did arrive, which is
   * correct unless the bitrate changed during the very gap we are concealing.
   *
   * A zero-length buffer is the concealment request: lyradec runs
   * DecodeSamples() with no packet set, which is Lyra's own PLC -- the
   * generative model continues from its recurrent state. */
  guint n = self->last_frame_count ? self->last_frame_count : 1;

  GstClockTime pts = GST_CLOCK_TIME_NONE;
  guint64 duration = 0;
  const GstStructure *s = gst_event_get_structure (event);
  if (s) {
    gst_structure_get_clock_time (s, "timestamp", &pts);
    if (gst_structure_get_uint64 (s, "duration", &duration) && duration > 0)
      n = MAX (1, (guint) (duration / LYRA_FRAME_DURATION));
  }

  GstBufferList *gaps = gst_buffer_list_new_sized (n);
  for (guint i = 0; i < n; i++) {
    GstBuffer *gap = gst_buffer_new_allocate (NULL, 0, NULL);
    GST_BUFFER_FLAG_SET (gap, GST_BUFFER_FLAG_GAP);
    if (i == 0) GST_BUFFER_FLAG_SET (gap, GST_BUFFER_FLAG_DISCONT);
    if (GST_CLOCK_TIME_IS_VALID (pts))
      GST_BUFFER_PTS (gap) = pts + i * LYRA_FRAME_DURATION;
    GST_BUFFER_DURATION (gap) = LYRA_FRAME_DURATION;
    gst_buffer_list_add (gaps, gap);
  }
  GST_DEBUG_OBJECT (self, "concealing %u lost frame(s)", n);
  gst_rtp_base_depayload_push_list (base, gaps);

  /* Do NOT unref event: GstRTPBaseDepayload's event handler owns it and unrefs
   * it after this vfunc returns. Unreffing here is a double-unref -- it shows
   * up as "gst_mini_object_unref: assertion REFCOUNT_VALUE > 0 failed" once per
   * lost packet, which is a use-after-free, not just a noisy log line. */
  return TRUE;
}

static void
gst_rtp_lyra_depay_class_init (GstRtpLyraDepayClass * klass)
{
  GstElementClass *element_class = GST_ELEMENT_CLASS (klass);
  GstRTPBaseDepayloadClass *base = GST_RTP_BASE_DEPAYLOAD_CLASS (klass);

  base->set_caps = gst_rtp_lyra_depay_set_caps;
  base->process_rtp_packet = gst_rtp_lyra_depay_process;
  base->packet_lost = gst_rtp_lyra_depay_packet_lost;

  gst_element_class_add_static_pad_template (element_class, &depay_sink_tmpl);
  gst_element_class_add_static_pad_template (element_class, &depay_src_tmpl);
  gst_element_class_set_static_metadata (element_class, "RTP Lyra depayloader",
      "Codec/Depayloader/Network/RTP", "Extract Lyra frames from RTP",
      "MANET");
}

static void
gst_rtp_lyra_depay_init (GstRtpLyraDepay * self)
{
  self->last_frame_size = 0;
  self->last_frame_count = 0;
  GST_DEBUG_CATEGORY_INIT (lyra_rtp_debug, "lyrartp", 0, "Lyra RTP");
}
