/* Type registration for the Lyra RTP payloader/depayloader.
 * Defined in gstlyrartp.cc, registered by plugin_init in gstlyra.cc. */

#ifndef GST_LYRA_RTP_H
#define GST_LYRA_RTP_H

#include <gst/gst.h>

G_BEGIN_DECLS

GType gst_rtp_lyra_pay_get_type (void);
GType gst_rtp_lyra_depay_get_type (void);

G_END_DECLS

#endif /* GST_LYRA_RTP_H */
