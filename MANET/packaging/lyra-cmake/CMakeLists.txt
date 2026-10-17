# Bazel-free aarch64 build of the Lyra v2 codec.
#
# Lyra upstream builds only with Bazel, which has no aarch64-Linux config (its
# configs are host-x86, clang-on-x86, and Android). Adding one is possible in
# principle -- Lyra already calls TensorFlow's workspace2(), which registers
# @local_config_embedded_arm -- but that path downloads GCC 8.3, and protobuf
# 3.15.4 does not compile with it.
#
# So this builds the pieces directly instead:
#   * TFLite (+XNNPACK, ruy, abseil, Eigen, fft2d, flatbuffers) comes from
#     TFLite's own CMake build, cross-compiled separately.
#   * glog is cross-built separately.
#   * ghc::filesystem is header-only.
#   * Only the handful of audio_dsp files Lyra actually includes are compiled,
#     rather than the whole multichannel-audio-tools tree.
#   * protobuf is eliminated entirely: Lyra used it to read one optional int32
#     from a two-byte file. See lyra_config.pb.h (hand-written shim).
#
# Configure with -DDEPS_ROOT pointing at the work directory that
# build-lyra-aarch64.sh populated (it holds src/ and build/).

cmake_minimum_required(VERSION 3.16)
project(lyra_aarch64 CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

if(NOT DEFINED DEPS_ROOT)
  message(FATAL_ERROR "-DDEPS_ROOT=<scratch dir> is required")
endif()

# Defaults follow the tree build-lyra-aarch64.sh actually creates, which is
# also the tree gst-lyra/Makefile reads. An earlier revision of this file used a
# flatter layout (deps/, tflite-build/, tfsrc/) that the script never produced,
# so configuring Lyra failed with "Cannot find source file" on every path here.
# Each is overridable so an out-of-tree layout can still be pointed at.
if(NOT DEFINED LYRA_SRC)
  set(LYRA_SRC   "${DEPS_ROOT}/src/lyra")
endif()
if(NOT DEFINED TFLITE_B)
  set(TFLITE_B   "${DEPS_ROOT}/build/tflite")
endif()
if(NOT DEFINED TFLITE_SRC)
  set(TFLITE_SRC "${DEPS_ROOT}/src/tensorflow")
endif()
if(NOT DEFINED AUDIO_DSP)
  set(AUDIO_DSP  "${DEPS_ROOT}/src/audio_dsp")
endif()
if(NOT DEFINED GHC)
  set(GHC        "${DEPS_ROOT}/src/ghc")
endif()
if(NOT DEFINED GLOG)
  set(GLOG       "${DEPS_ROOT}/build/glog-install")
endif()
if(NOT DEFINED ABSL)
  set(ABSL       "${DEPS_ROOT}/build/absl-install")
endif()

# --- the subset of multichannel-audio-tools Lyra includes --------------------
set(AUDIO_DSP_SRCS
  ${AUDIO_DSP}/audio/dsp/mfcc/mel_filterbank.cc
  ${AUDIO_DSP}/audio/dsp/number_util.cc
  ${AUDIO_DSP}/audio/dsp/window_functions.cc
  ${AUDIO_DSP}/audio/dsp/bessel_functions.cc
  ${AUDIO_DSP}/audio/dsp/portable/number_util.c
  ${AUDIO_DSP}/audio/dsp/resampler_q.cc
  ${AUDIO_DSP}/audio/dsp/spectrogram/spectrogram.cc
  ${AUDIO_DSP}/audio/dsp/spectrogram/inverse_spectrogram.cc
  ${AUDIO_DSP}/audio/dsp/portable/read_wav_file.c
  ${AUDIO_DSP}/audio/dsp/portable/read_wav_file_generic.c
  ${AUDIO_DSP}/audio/dsp/portable/write_wav_file.c
  ${AUDIO_DSP}/audio/dsp/portable/write_wav_file_generic.c
  ${AUDIO_DSP}/audio/dsp/portable/rational_factor_resampler.c
  ${AUDIO_DSP}/audio/dsp/portable/rational_factor_resampler_kernel.c
)

# --- Lyra's own sources, minus tests, benchmarks and CLI mains --------------
file(GLOB LYRA_ALL "${LYRA_SRC}/lyra/*.cc" "${LYRA_SRC}/lyra/cli_example/*.cc")
set(LYRA_SRCS "")
foreach(f ${LYRA_ALL})
  if(NOT f MATCHES "_test\\.cc$|_benchmark|_main\\.cc$|benchmark_lib")
    list(APPEND LYRA_SRCS ${f})
  endif()
endforeach()

add_library(lyra STATIC ${LYRA_SRCS} ${AUDIO_DSP_SRCS})

target_include_directories(lyra PUBLIC
  ${LYRA_SRC}
  ${AUDIO_DSP}
  ${GHC}                       # Lyra includes "include/ghc/filesystem.hpp"
  ${GLOG}/include
  ${TFLITE_SRC}
  ${ABSL}/include
  ${TFLITE_B}/eigen
  ${TFLITE_B}/flatbuffers/include
  ${AUDIO_DSP}/third_party/pffft/src   # audio_dsp includes "pffft/pffft.h"
  ${AUDIO_DSP}/third_party             # ... via this root
)

# Lyra's headers say #include "lyra/foo.h", and audio_dsp says "audio/dsp/...",
# so both roots go on the include path above.

# --- link line ---------------------------------------------------------------
# TFLite's CMake build only produces the absl targets TFLite itself needs -
# statusor, flags_parse and the random_* family are absent, and Lyra uses all
# three. So abseil is built standalone (same source, same 20220623 LTS) with
# every target, and used instead of TFLite's partial set.
file(GLOB ABSL_LIBS "${ABSL}/lib/libabsl_*.a")
file(GLOB RUY_LIBS  "${TFLITE_B}/_deps/ruy-build/ruy/libruy_*.a")

set(TFLITE_LIBS
  ${TFLITE_B}/libtensorflow-lite.a
  ${TFLITE_B}/_deps/xnnpack-build/libXNNPACK.a
  ${TFLITE_B}/pthreadpool/libpthreadpool.a
  ${TFLITE_B}/_deps/cpuinfo-build/libcpuinfo.a
  ${TFLITE_B}/_deps/cpuinfo-build/deps/clog/libclog.a
  ${TFLITE_B}/_deps/farmhash-build/libfarmhash.a
  ${TFLITE_B}/_deps/flatbuffers-build/libflatbuffers.a
  ${TFLITE_B}/_deps/fft2d-build/libfft2d_fftsg2d.a
  ${TFLITE_B}/_deps/fft2d-build/libfft2d_fftsg.a
)

# --start-group/--end-group: these archives reference each other (absl's
# debugging/symbolize/stacktrace trio especially), so the linker must be told
# to re-scan rather than make a single pass.
target_link_libraries(lyra PUBLIC
  -Wl,--start-group
  ${TFLITE_LIBS} ${RUY_LIBS} ${ABSL_LIBS} ${GLOG}/lib/libglog.a
  -Wl,--end-group
  pthread dl m
)

# --- CLI tools, matching the Bazel targets ----------------------------------
foreach(tool encoder_main decoder_main)
  add_executable(${tool} ${LYRA_SRC}/lyra/cli_example/${tool}.cc)
  target_link_libraries(${tool} PRIVATE lyra)
endforeach()
