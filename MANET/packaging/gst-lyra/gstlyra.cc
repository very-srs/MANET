/* GStreamer elements wrapping the Lyra v2 codec.
 *
 *   lyraenc   audio/x-raw (S16LE, 16 kHz, mono)  ->  audio/x-lyra
 *   lyradec   audio/x-lyra                       ->  audio/x-raw
 *
 * Why an element rather than calling Lyra from the daemon: mesh-voice.py keeps
 * every other part of the pipeline it was measured with -- multiudpsink's
 * multicast+unicast fanout, the DSCP/TTL marking, rtpjitterbuffer, the valve
 * used for PTT gating -- and only the codec swaps out. It also keeps Python off
 * the audio thread, which is the whole reason the daemon is a supervisor rather
 * than a codec.
 *
 * Deliberately dumb: this element encodes and decodes, and exposes `bitrate` as
 * a runtime-settable property. It does NOT decide when to change bitrate. That
 * policy lives in mesh-voice.py, which is the only place that can see receive
 * statistics, peer count and link state together.
 *
 * Frame geometry is fixed by Lyra: 20 ms frames at 16 kHz = 320 samples in,
 * and 8 / 15 / 23 bytes out for 3200 / 6000 / 9200 bps respectively. Those
 * sizes are distinct, so a receiver can identify the bitrate from the payload
 * length alone -- lyradec uses Lyra's own PacketSizeToNumQuantizedBits() for
 * that and therefore needs no out-of-band agreement on rate.
 */

#include <gst/gst.h>
#include <gst/audio/audio.h>
#include <gst/audio/gstaudioencoder.h>
#include <gst/audio/gstaudiodecoder.h>

#include <memory>
#include <vector>

#include "lyra/lyra_encoder.h"
#include "lyra/lyra_decoder.h"
#include "lyra/lyra_config.h"

#include "gstlyrartp.h"

GST_DEBUG_CATEGORY_STATIC (lyra_debug);
#define GST_CAT_DEFAULT lyra_debug

#define LYRA_SAMPLE_RATE 16000
#define LYRA_CHANNELS    1
#define LYRA_FRAME_MS    20
#define LYRA_SAMPLES_PER_FRAME (LYRA_SAMPLE_RATE / 1000 * LYRA_FRAME_MS)   /* 320 */
#define LYRA_DEFAULT_BITRATE 6000
#define LYRA_DEFAULT_MODEL_PATH "/usr/local/share/lyra/model_coeffs"

enum { PROP_0, PROP_BITRATE, PROP_MODEL_PATH, PROP_ENABLE_DTX };

/* ------------------------------------------------------------------ encoder */

typedef struct _GstLyraEnc {
  GstAudioEncoder parent;
  chromemedia::codec::LyraEncoder *enc;
  gint bitrate;
  gchar *model_path;
  gboolean enable_dtx;
} GstLyraEnc;

typedef struct _GstLyraEncClass { GstAudioEncoderClass parent_class; } GstLyraEncClass;

G_DEFINE_TYPE (GstLyraEnc, gst_lyra_enc, GST_TYPE_AUDIO_ENCODER);

static GstStaticPadTemplate enc_sink_tmpl = GST_STATIC_PAD_TEMPLATE ("sink",
    GST_PAD_SINK, GST_PAD_ALWAYS,
    GST_STATIC_CAPS ("audio/x-raw, format=(string)S16LE, rate=(int)16000, "
                     "channels=(int)1, layout=(string)interleaved"));

static GstStaticPadTemplate enc_src_tmpl = GST_STATIC_PAD_TEMPLATE ("src",
    GST_PAD_SRC, GST_PAD_ALWAYS,
    GST_STATIC_CAPS ("audio/x-lyra, rate=(int)16000, channels=(int)1"));

static gboolean
gst_lyra_enc_start (GstAudioEncoder * benc)
{
  GstLyraEnc *self = (GstLyraEnc *) benc;
  auto enc = chromemedia::codec::LyraEncoder::Create (
      LYRA_SAMPLE_RATE, LYRA_CHANNELS, self->bitrate,
      self->enable_dtx, ghc::filesystem::path (self->model_path));
  if (!enc) {
    GST_ELEMENT_ERROR (self, LIBRARY, INIT, (NULL),
        ("LyraEncoder::Create failed (bitrate=%d model_path=%s). The model "
         "directory must contain lyragan.tflite, quantizer.tflite, "
         "soundstream_encoder.tflite and lyra_config.binarypb.",
         self->bitrate, self->model_path));
    return FALSE;
  }
  self->enc = enc.release ();
  GST_INFO_OBJECT (self, "lyra encoder up: %d bps, model %s",
      self->bitrate, self->model_path);
  return TRUE;
}

static gboolean
gst_lyra_enc_stop (GstAudioEncoder * benc)
{
  GstLyraEnc *self = (GstLyraEnc *) benc;
  delete self->enc; self->enc = NULL;
  return TRUE;
}

static gboolean
gst_lyra_enc_set_format (GstAudioEncoder * benc, GstAudioInfo * info)
{
  /* Framing must be declared here, not in start(): GstAudioEncoder only
   * applies it once the input format is negotiated, so setting it earlier is
   * silently ignored and the base class hands over whatever buffer size the
   * upstream element happened to produce (2048 samples, in practice). Lyra
   * requires exactly 320 samples -- 20 ms at 16 kHz -- per Encode() call. */
  gst_audio_encoder_set_frame_samples_min (benc, LYRA_SAMPLES_PER_FRAME);
  gst_audio_encoder_set_frame_samples_max (benc, LYRA_SAMPLES_PER_FRAME);
  gst_audio_encoder_set_frame_max (benc, 1);
  gst_audio_encoder_set_hard_min (benc, TRUE);

  GstCaps *caps = gst_caps_new_simple ("audio/x-lyra",
      "rate", G_TYPE_INT, LYRA_SAMPLE_RATE, "channels", G_TYPE_INT, LYRA_CHANNELS, NULL);
  gboolean ok = gst_audio_encoder_set_output_format (benc, caps);
  gst_caps_unref (caps);
  return ok;
}

static GstFlowReturn
gst_lyra_enc_handle_frame (GstAudioEncoder * benc, GstBuffer * inbuf)
{
  GstLyraEnc *self = (GstLyraEnc *) benc;
  if (inbuf == NULL)                       /* draining at EOS */
    return gst_audio_encoder_finish_frame (benc, NULL, -1);

  GstMapInfo map;
  if (!gst_buffer_map (inbuf, &map, GST_MAP_READ))
    return GST_FLOW_ERROR;

  const int nsamples = map.size / sizeof (int16_t);
  auto encoded = self->enc->Encode (absl::MakeConstSpan (
      reinterpret_cast<const int16_t *> (map.data), nsamples));
  gst_buffer_unmap (inbuf, &map);

  if (!encoded) {
    GST_WARNING_OBJECT (self, "Encode() returned nullopt for %d samples", nsamples);
    return GST_FLOW_ERROR;
  }
  /* DTX (when enabled) legitimately produces an empty packet for silence.
   * Emit nothing rather than a zero-length buffer. */
  if (encoded->empty ())
    return gst_audio_encoder_finish_frame (benc, NULL, LYRA_SAMPLES_PER_FRAME);

  GstBuffer *out = gst_buffer_new_allocate (NULL, encoded->size (), NULL);
  gst_buffer_fill (out, 0, encoded->data (), encoded->size ());
  return gst_audio_encoder_finish_frame (benc, out, LYRA_SAMPLES_PER_FRAME);
}

static void
gst_lyra_enc_set_property (GObject * obj, guint id, const GValue * v, GParamSpec * ps)
{
  GstLyraEnc *self = (GstLyraEnc *) obj;
  switch (id) {
    case PROP_BITRATE: {
      gint br = g_value_get_int (v);
      /* Runtime-settable: this is the hook the adaptive-bitrate policy in
       * mesh-voice.py uses. Lyra keeps the same weights across rates, so
       * switching costs nothing and needs no re-init. */
      if (self->enc && !self->enc->set_bitrate (br)) {
        GST_WARNING_OBJECT (self, "set_bitrate(%d) rejected; keeping %d",
            br, self->bitrate);
        break;
      }
      self->bitrate = br;
      GST_INFO_OBJECT (self, "bitrate now %d bps", br);
      break;
    }
    case PROP_MODEL_PATH:
      g_free (self->model_path); self->model_path = g_value_dup_string (v); break;
    case PROP_ENABLE_DTX:
      self->enable_dtx = g_value_get_boolean (v); break;
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID (obj, id, ps);
  }
}

static void
gst_lyra_enc_get_property (GObject * obj, guint id, GValue * v, GParamSpec * ps)
{
  GstLyraEnc *self = (GstLyraEnc *) obj;
  switch (id) {
    case PROP_BITRATE:    g_value_set_int (v, self->bitrate); break;
    case PROP_MODEL_PATH: g_value_set_string (v, self->model_path); break;
    case PROP_ENABLE_DTX: g_value_set_boolean (v, self->enable_dtx); break;
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID (obj, id, ps);
  }
}

static void
gst_lyra_enc_finalize (GObject * obj)
{
  GstLyraEnc *self = (GstLyraEnc *) obj;
  delete self->enc;
  g_free (self->model_path);
  G_OBJECT_CLASS (gst_lyra_enc_parent_class)->finalize (obj);
}

static void
gst_lyra_enc_class_init (GstLyraEncClass * klass)
{
  GObjectClass *gobject = G_OBJECT_CLASS (klass);
  GstElementClass *element = GST_ELEMENT_CLASS (klass);
  GstAudioEncoderClass *base = GST_AUDIO_ENCODER_CLASS (klass);

  gobject->set_property = gst_lyra_enc_set_property;
  gobject->get_property = gst_lyra_enc_get_property;
  gobject->finalize = gst_lyra_enc_finalize;

  g_object_class_install_property (gobject, PROP_BITRATE,
      g_param_spec_int ("bitrate", "Bitrate",
          "Target bitrate in bps (3200, 6000 or 9200). Settable while PLAYING.",
          3200, 9200, LYRA_DEFAULT_BITRATE,
          (GParamFlags) (G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS |
                         GST_PARAM_MUTABLE_PLAYING)));
  g_object_class_install_property (gobject, PROP_MODEL_PATH,
      g_param_spec_string ("model-path", "Model path",
          "Directory holding the Lyra .tflite weights", LYRA_DEFAULT_MODEL_PATH,
          (GParamFlags) (G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));
  g_object_class_install_property (gobject, PROP_ENABLE_DTX,
      g_param_spec_boolean ("enable-dtx", "Enable DTX",
          "Discontinuous transmission: emit nothing for background noise",
          FALSE, (GParamFlags) (G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

  gst_element_class_add_static_pad_template (element, &enc_sink_tmpl);
  gst_element_class_add_static_pad_template (element, &enc_src_tmpl);
  gst_element_class_set_static_metadata (element, "Lyra audio encoder",
      "Codec/Encoder/Audio", "Encodes speech with Google Lyra v2",
      "MANET mesh <mesh@localhost>");

  base->start = gst_lyra_enc_start;
  base->stop = gst_lyra_enc_stop;
  base->set_format = gst_lyra_enc_set_format;
  base->handle_frame = gst_lyra_enc_handle_frame;
}

static void
gst_lyra_enc_init (GstLyraEnc * self)
{
  self->enc = NULL;
  self->bitrate = LYRA_DEFAULT_BITRATE;
  self->model_path = g_strdup (LYRA_DEFAULT_MODEL_PATH);
  self->enable_dtx = FALSE;
}

/* ------------------------------------------------------------------ decoder */

typedef struct _GstLyraDec {
  GstAudioDecoder parent;
  chromemedia::codec::LyraDecoder *dec;
  gchar *model_path;
} GstLyraDec;

typedef struct _GstLyraDecClass { GstAudioDecoderClass parent_class; } GstLyraDecClass;

G_DEFINE_TYPE (GstLyraDec, gst_lyra_dec, GST_TYPE_AUDIO_DECODER);

static GstStaticPadTemplate dec_sink_tmpl = GST_STATIC_PAD_TEMPLATE ("sink",
    GST_PAD_SINK, GST_PAD_ALWAYS,
    GST_STATIC_CAPS ("audio/x-lyra, rate=(int)16000, channels=(int)1"));

static GstStaticPadTemplate dec_src_tmpl = GST_STATIC_PAD_TEMPLATE ("src",
    GST_PAD_SRC, GST_PAD_ALWAYS,
    GST_STATIC_CAPS ("audio/x-raw, format=(string)S16LE, rate=(int)16000, "
                     "channels=(int)1, layout=(string)interleaved"));

static gboolean
gst_lyra_dec_start (GstAudioDecoder * bdec)
{
  GstLyraDec *self = (GstLyraDec *) bdec;
  auto dec = chromemedia::codec::LyraDecoder::Create (
      LYRA_SAMPLE_RATE, LYRA_CHANNELS, ghc::filesystem::path (self->model_path));
  if (!dec) {
    GST_ELEMENT_ERROR (self, LIBRARY, INIT, (NULL),
        ("LyraDecoder::Create failed (model_path=%s)", self->model_path));
    return FALSE;
  }
  self->dec = dec.release ();
  GstAudioInfo info;
  gst_audio_info_set_format (&info, GST_AUDIO_FORMAT_S16LE,
      LYRA_SAMPLE_RATE, LYRA_CHANNELS, NULL);
  gst_audio_decoder_set_output_format (bdec, &info);
  return TRUE;
}

static gboolean
gst_lyra_dec_stop (GstAudioDecoder * bdec)
{
  GstLyraDec *self = (GstLyraDec *) bdec;
  delete self->dec; self->dec = NULL;
  return TRUE;
}

static GstFlowReturn
gst_lyra_dec_handle_frame (GstAudioDecoder * bdec, GstBuffer * inbuf)
{
  GstLyraDec *self = (GstLyraDec *) bdec;

  /* inbuf == NULL means the jitter buffer told us a packet is missing (it is
   * configured with do-lost=true). Lyra handles this itself: DecodeSamples()
   * with no packet set runs concealment and then comfort noise, so a gap
   * produces plausible audio rather than a hole. This is why lyradec does not
   * need a separate PLC element. */
  /* rtplyradepay signals a lost frame as a zero-length GAP buffer -- one per
   * missing 20 ms frame, since a lost RTP packet carries frames-per-packet of
   * them and the base class would only ever ask us to conceal once. Treat it
   * exactly like inbuf == NULL: fall through to DecodeSamples() with no packet
   * set, which is Lyra's own concealment. */
  if (inbuf != NULL && gst_buffer_get_size (inbuf) == 0)
    inbuf = NULL;

  if (inbuf != NULL) {
    GstMapInfo map;
    if (!gst_buffer_map (inbuf, &map, GST_MAP_READ))
      return GST_FLOW_ERROR;
    bool ok = self->dec->SetEncodedPacket (
        absl::MakeConstSpan (map.data, map.size));
    if (!ok) {
      /* Not a valid Lyra packet for any supported bitrate. Drop it and let the
       * next DecodeSamples() conceal, rather than failing the stream: on a
       * mesh a corrupt packet is expected, an aborted pipeline is not. */
      GST_WARNING_OBJECT (self, "SetEncodedPacket rejected %" G_GSIZE_FORMAT
          " bytes (valid sizes are 8/15/23)", map.size);
    }
    gst_buffer_unmap (inbuf, &map);
  }

  auto pcm = self->dec->DecodeSamples (LYRA_SAMPLES_PER_FRAME);
  if (!pcm) {
    GST_WARNING_OBJECT (self, "DecodeSamples returned nullopt");
    return GST_FLOW_ERROR;
  }

  GstBuffer *out = gst_buffer_new_allocate (NULL, pcm->size () * sizeof (int16_t), NULL);
  gst_buffer_fill (out, 0, pcm->data (), pcm->size () * sizeof (int16_t));
  return gst_audio_decoder_finish_frame (bdec, out, 1);
}

static void
gst_lyra_dec_set_property (GObject * obj, guint id, const GValue * v, GParamSpec * ps)
{
  GstLyraDec *self = (GstLyraDec *) obj;
  switch (id) {
    case PROP_MODEL_PATH:
      g_free (self->model_path); self->model_path = g_value_dup_string (v); break;
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID (obj, id, ps);
  }
}

static void
gst_lyra_dec_get_property (GObject * obj, guint id, GValue * v, GParamSpec * ps)
{
  GstLyraDec *self = (GstLyraDec *) obj;
  switch (id) {
    case PROP_MODEL_PATH: g_value_set_string (v, self->model_path); break;
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID (obj, id, ps);
  }
}

static void
gst_lyra_dec_finalize (GObject * obj)
{
  GstLyraDec *self = (GstLyraDec *) obj;
  delete self->dec;
  g_free (self->model_path);
  G_OBJECT_CLASS (gst_lyra_dec_parent_class)->finalize (obj);
}

static void
gst_lyra_dec_class_init (GstLyraDecClass * klass)
{
  GObjectClass *gobject = G_OBJECT_CLASS (klass);
  GstElementClass *element = GST_ELEMENT_CLASS (klass);
  GstAudioDecoderClass *base = GST_AUDIO_DECODER_CLASS (klass);

  gobject->set_property = gst_lyra_dec_set_property;
  gobject->get_property = gst_lyra_dec_get_property;
  gobject->finalize = gst_lyra_dec_finalize;

  g_object_class_install_property (gobject, PROP_MODEL_PATH,
      g_param_spec_string ("model-path", "Model path",
          "Directory holding the Lyra .tflite weights", LYRA_DEFAULT_MODEL_PATH,
          (GParamFlags) (G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

  gst_element_class_add_static_pad_template (element, &dec_sink_tmpl);
  gst_element_class_add_static_pad_template (element, &dec_src_tmpl);
  gst_element_class_set_static_metadata (element, "Lyra audio decoder",
      "Codec/Decoder/Audio", "Decodes speech with Google Lyra v2",
      "MANET mesh <mesh@localhost>");

  base->start = gst_lyra_dec_start;
  base->stop = gst_lyra_dec_stop;
  base->handle_frame = gst_lyra_dec_handle_frame;
}

static void
gst_lyra_dec_init (GstLyraDec * self)
{
  self->dec = NULL;
  self->model_path = g_strdup (LYRA_DEFAULT_MODEL_PATH);
  /* Ask the base class to call us for missing frames so Lyra's concealment
   * runs instead of leaving a gap. */
  gst_audio_decoder_set_plc_aware (GST_AUDIO_DECODER (self), TRUE);
  gst_audio_decoder_set_plc (GST_AUDIO_DECODER (self), TRUE);
}

/* ------------------------------------------------------------------ plugin */

static gboolean
plugin_init (GstPlugin * plugin)
{
  GST_DEBUG_CATEGORY_INIT (lyra_debug, "lyra", 0, "Lyra codec");
  return gst_element_register (plugin, "lyraenc", GST_RANK_PRIMARY,
             gst_lyra_enc_get_type ())
      && gst_element_register (plugin, "lyradec", GST_RANK_PRIMARY,
             gst_lyra_dec_get_type ())
      && gst_element_register (plugin, "rtplyrapay", GST_RANK_PRIMARY,
             gst_rtp_lyra_pay_get_type ())
      && gst_element_register (plugin, "rtplyradepay", GST_RANK_PRIMARY,
             gst_rtp_lyra_depay_get_type ());
}

#ifndef PACKAGE
#define PACKAGE "gst-lyra"
#endif

GST_PLUGIN_DEFINE (GST_VERSION_MAJOR, GST_VERSION_MINOR,
    lyra, "Google Lyra v2 speech codec", plugin_init,
    "1.0", "Apache-2.0", "MANET", "https://github.com/very-srs/MANET")
