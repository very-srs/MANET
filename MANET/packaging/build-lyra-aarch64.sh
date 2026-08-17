#!/usr/bin/env bash
# ==============================================================================
# Build the Lyra v2 codec for aarch64 (CM4) without Bazel
# ==============================================================================
# Lyra upstream builds only with Bazel, and Bazel has no aarch64-Linux config —
# upstream ships host-x86, clang-on-x86 and Android-arm64 only. Adding one is
# possible in principle (Lyra already calls TensorFlow's workspace2(), which
# registers @local_config_embedded_arm and downloads a GCC 8.3 cross toolchain)
# but that path dies compiling protobuf 3.15.4, which GCC 8.3 rejects.
#
# So this builds the pieces directly with CMake instead. The result is a pair of
# static aarch64 binaries whose only shared-library needs are glibc and
# libstdc++ — no TFLite .so, no protobuf, nothing to install on the node.
#
# What gets built, in order:
#   1. TFLite (+XNNPACK) via TFLite's own CMakeLists, cross-compiled.
#      This conveniently also produces fft2d, Eigen, ruy and flatbuffers,
#      all of which Lyra needs.
#   2. abseil, standalone. TFLite's build only produces the absl targets TFLite
#      itself uses — statusor, flags_parse and the random_* family are absent,
#      and Lyra needs all three. Same source, same 20220623 LTS.
#   3. glog.
#   4. Lyra itself, plus the 13 files of multichannel-audio-tools it includes.
#
# Two upstream problems this works around, both recorded so they are not
# rediscovered:
#   * Lyra's WORKSPACE pins com_google_glog to a floating `branch = "master"`.
#     That has since drifted to a glog whose Bazel rules reference @gflags, so
#     even the x86 Bazel build fails at analysis. Pin tag v0.6.0.
#   * multichannel-audio-tools must be pinned to the commit in Lyra's WORKSPACE
#     (14a45c5a...). HEAD needs a newer absl (absl/base/nullability.h) and a
#     pffft that is not vendored.
#
# protobuf is eliminated rather than ported: Lyra used it to read one optional
# int32 from a 2-byte file. See lyra_config_pb_shim.h.
#
# Usage:  build-lyra-aarch64.sh [workdir]
# Output: $workdir/out/{encoder_main,decoder_main} plus model_coeffs/
# ==============================================================================
set -euo pipefail

WORK="${1:-$PWD/lyra-aarch64-build}"
JOBS="${JOBS:-$(nproc)}"
CROSS="${CROSS:-aarch64-linux-gnu}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

LYRA_COMMIT="${LYRA_COMMIT:-47698dadf0010abff6a848e02642f55f806d4842}"   # v1.3.2
AUDIO_DSP_COMMIT="14a45c5a7c965e5ef01fe537bd816ce10a247813"             # per Lyra WORKSPACE
TF_TAG="${TF_TAG:-v2.11.0}"                                             # per Lyra WORKSPACE

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] - LYRA-BUILD: $1"; }
need() { command -v "$1" >/dev/null || { echo "missing required tool: $1" >&2; exit 1; }; }

need cmake; need git; need make
need "${CROSS}-gcc"; need "${CROSS}-g++"

mkdir -p "$WORK"; cd "$WORK"

# -fPIC everywhere is mandatory, not optional: the GStreamer element in
# gst-lyra/ is a shared object, and linking non-PIC aarch64 archives into a
# .so fails with "relocation R_AARCH64_ADR_PREL_PG_HI21 ... can not be used
# when making a shared object". The CLI tools would link fine without it.
CM_ARGS=(
  -DCMAKE_SYSTEM_NAME=Linux
  -DCMAKE_SYSTEM_PROCESSOR=aarch64
  -DCMAKE_C_COMPILER="${CROSS}-gcc"
  -DCMAKE_CXX_COMPILER="${CROSS}-g++"
  -DCMAKE_BUILD_TYPE=Release
  -DCMAKE_POSITION_INDEPENDENT_CODE=ON
)

# --- sources ------------------------------------------------------------------
if [ ! -d src/lyra ]; then
    log "fetching lyra"
    mkdir -p src && git clone -q https://github.com/google/lyra.git src/lyra
    git -C src/lyra checkout -q "$LYRA_COMMIT"
    # Unpin the floating glog branch (see header).
    python3 - "$WORK/src/lyra/WORKSPACE" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
old = '''    remote = "https://github.com/google/glog.git",
    branch = "master"'''
if old in s:
    s = s.replace(old, '''    remote = "https://github.com/google/glog.git",
    tag = "v0.6.0",''')
    open(p, "w").write(s)
    print("  patched WORKSPACE: glog master -> v0.6.0")
PY
fi

if [ ! -d src/audio_dsp ]; then
    log "fetching multichannel-audio-tools @ ${AUDIO_DSP_COMMIT:0:8}"
    git clone -q https://github.com/mchinen/multichannel-audio-tools.git src/audio_dsp
    git -C src/audio_dsp checkout -q "$AUDIO_DSP_COMMIT"
fi

[ -d src/ghc ]  || { log "fetching ghc::filesystem"; git clone -q --depth 1 -b v1.5.14 https://github.com/gulrak/filesystem.git src/ghc; }
[ -d src/glog ] || { log "fetching glog v0.6.0";     git clone -q --depth 1 -b v0.6.0 https://github.com/google/glog.git src/glog; }
[ -d src/tensorflow ] || { log "fetching tensorflow $TF_TAG (shallow)"; git clone -q --depth 1 -b "$TF_TAG" https://github.com/tensorflow/tensorflow.git src/tensorflow; }

# Drop in the protobuf shim as lyra_config.pb.h, which is what Lyra includes.
cp "$REPO_ROOT/MANET/packaging/lyra_config_pb_shim.h" src/lyra/lyra/lyra_config.pb.h
log "installed protobuf shim"

# --- 1. TFLite ----------------------------------------------------------------
if [ ! -f build/tflite/libtensorflow-lite.a ]; then
    log "building TFLite + XNNPACK for aarch64 (slow: ~20 min)"
    mkdir -p build/tflite && (cd build/tflite && \
      cmake "${CM_ARGS[@]}" -DTFLITE_ENABLE_XNNPACK=ON \
        -DCMAKE_CXX_FLAGS="-O3 -march=armv8-a" -DCMAKE_C_FLAGS="-O3 -march=armv8-a" \
        "$WORK/src/tensorflow/tensorflow/lite" >/dev/null && \
      make -j"$JOBS" tensorflow-lite >/dev/null)
fi
log "TFLite ready"

# --- 2. abseil (complete) -----------------------------------------------------
if [ ! -f build/absl-install/lib/libabsl_statusor.a ]; then
    log "building abseil for aarch64"
    ABSL_SRC="$WORK/build/tflite/abseil-cpp"
    mkdir -p build/absl && (cd build/absl && \
      cmake "${CM_ARGS[@]}" -DCMAKE_CXX_STANDARD=17 -DBUILD_TESTING=OFF \
        -DCMAKE_INSTALL_PREFIX="$WORK/build/absl-install" "$ABSL_SRC" >/dev/null && \
      make -j"$JOBS" install >/dev/null)
fi
log "abseil ready"

# --- 3. glog ------------------------------------------------------------------
if [ ! -f build/glog-install/lib/libglog.a ]; then
    log "building glog for aarch64"
    mkdir -p build/glog && (cd build/glog && \
      cmake "${CM_ARGS[@]}" -DBUILD_SHARED_LIBS=OFF -DWITH_GFLAGS=OFF -DWITH_GTEST=OFF \
        -DWITH_UNWIND=OFF -DBUILD_TESTING=OFF \
        -DCMAKE_INSTALL_PREFIX="$WORK/build/glog-install" "$WORK/src/glog" >/dev/null && \
      make -j"$JOBS" install >/dev/null)
fi
log "glog ready"

# --- 4. Lyra ------------------------------------------------------------------
log "building lyra"
mkdir -p build/lyra && (cd build/lyra && \
  cmake "${CM_ARGS[@]}" -DCMAKE_CXX_FLAGS="-O3" \
    -DDEPS_ROOT="$WORK" "$REPO_ROOT/MANET/packaging/lyra-cmake" >/dev/null && \
  make -j"$JOBS" >/dev/null)

mkdir -p out
cp build/lyra/encoder_main build/lyra/decoder_main out/
cp -r src/lyra/lyra/model_coeffs out/
"${CROSS}-strip" out/encoder_main out/decoder_main 2>/dev/null || true

log "done — binaries in $WORK/out"
file out/encoder_main | sed 's/^/  /'
ls -la out/encoder_main out/decoder_main | sed 's/^/  /'
echo
echo "  Verify on a node:"
echo "    ./encoder_main --input_path=in.wav --output_dir=. --bitrate=6000 --model_path=model_coeffs"
echo "    ./decoder_main --encoded_path=in.lyra --output_dir=. --bitrate=6000 --model_path=model_coeffs"
