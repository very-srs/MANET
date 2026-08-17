// Minimal stand-in for lyra/lyra_config.pb.h.
//
// Lyra's only use of protobuf is reading lyra_config.binarypb to check one
// optional int32 ("identifier") against kVersionMinor. That file is two bytes
// -- 08 03, i.e. field 1, varint, value 3 -- so pulling in the whole protobuf
// runtime (which does not cross-compile with the toolchain we use) to parse it
// is not worth it. This decodes the same wire format by hand.
//
// Wire format, proto2 optional int32 field 1:
//   key   = (field_number << 3) | wire_type  =  (1 << 3) | 0  = 0x08
//   value = base-128 varint, little-endian groups of 7 bits
// Absent file or absent field means identifier stays 0, matching protobuf's
// default-value semantics for a missing optional field.

#ifndef LYRA_LYRA_CONFIG_PB_SHIM_H_
#define LYRA_LYRA_CONFIG_PB_SHIM_H_

#include <cstdint>
#include <istream>

namespace third_party {
namespace lyra_codec {
namespace lyra {

class LyraConfig {
 public:
  int32_t identifier() const { return identifier_; }

  // Mirrors google::protobuf::MessageLite::ParseFromIstream: true on success,
  // false if the bytes are not a well-formed message.
  bool ParseFromIstream(std::istream* input) {
    identifier_ = 0;
    if (input == nullptr) return false;
    int c;
    while ((c = input->get()) != std::istream::traits_type::eof()) {
      const uint8_t key = static_cast<uint8_t>(c);
      const uint32_t field = key >> 3;
      const uint32_t wire_type = key & 0x07;
      if (wire_type != 0) {
        // Only varint fields are expected in this message. Anything else means
        // the file is not the config we understand; fail rather than guess.
        return false;
      }
      uint64_t value = 0;
      int shift = 0;
      while (true) {
        const int b = input->get();
        if (b == std::istream::traits_type::eof()) return false;
        value |= static_cast<uint64_t>(b & 0x7F) << shift;
        if ((b & 0x80) == 0) break;
        shift += 7;
        if (shift > 63) return false;
      }
      if (field == 1) identifier_ = static_cast<int32_t>(value);
      // Unknown fields are skipped, as protobuf would.
    }
    return true;
  }

 private:
  int32_t identifier_ = 0;
};

}  // namespace lyra
}  // namespace lyra_codec
}  // namespace third_party

#endif  // LYRA_LYRA_CONFIG_PB_SHIM_H_
