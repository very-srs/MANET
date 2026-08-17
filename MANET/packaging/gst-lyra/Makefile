# GStreamer Lyra plugin.
#
# Built natively on an aarch64 node rather than cross-compiled: cross-building
# against GStreamer needs a full aarch64 sysroot with gstreamer/glib headers and
# .so files, which is far more setup than compiling ~500 lines on the target.
# Everything expensive (TFLite, abseil, Lyra itself) is already cross-built by
# MANET/packaging/build-lyra-aarch64.sh; this only links against those archives.
#
# On the node:
#   sudo apt install -y build-essential pkg-config \
#        libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
#   make LYRA_BUILD=/path/to/lyra-aarch64-build
#   sudo make install
#
# Verify:
#   gst-inspect-1.0 lyraenc

LYRA_BUILD ?= $(HOME)/lyra-aarch64-build
LYRA_SRC   ?= $(LYRA_BUILD)/src/lyra
TFLITE_B   ?= $(LYRA_BUILD)/build/tflite
ABSL       ?= $(LYRA_BUILD)/build/absl-install
GLOG       ?= $(LYRA_BUILD)/build/glog-install
GHC        ?= $(LYRA_BUILD)/src/ghc
LYRA_LIB   ?= $(LYRA_BUILD)/build/lyra/liblyra.a

PLUGIN     := libgstlyra.so
PLUGIN_DIR ?= /usr/lib/aarch64-linux-gnu/gstreamer-1.0

GST_PKGS   := gstreamer-1.0 gstreamer-audio-1.0 gstreamer-rtp-1.0 gstreamer-base-1.0
GST_CFLAGS := $(shell pkg-config --cflags $(GST_PKGS))
GST_LIBS   := $(shell pkg-config --libs   $(GST_PKGS))

CXXFLAGS := -O2 -fPIC -std=gnu++17 -Wall \
  -I$(LYRA_SRC) -I$(GHC) -I$(ABSL)/include -I$(GLOG)/include \
  $(GST_CFLAGS)

# Order matters for static archives, and absl's debugging/symbolize/stacktrace
# trio is mutually recursive, hence --start-group.
ARCHIVES := $(LYRA_LIB) \
  $(TFLITE_B)/libtensorflow-lite.a \
  $(TFLITE_B)/_deps/xnnpack-build/libXNNPACK.a \
  $(TFLITE_B)/pthreadpool/libpthreadpool.a \
  $(TFLITE_B)/_deps/cpuinfo-build/libcpuinfo.a \
  $(TFLITE_B)/_deps/cpuinfo-build/deps/clog/libclog.a \
  $(TFLITE_B)/_deps/farmhash-build/libfarmhash.a \
  $(TFLITE_B)/_deps/flatbuffers-build/libflatbuffers.a \
  $(TFLITE_B)/_deps/fft2d-build/libfft2d_fftsg2d.a \
  $(TFLITE_B)/_deps/fft2d-build/libfft2d_fftsg.a \
  $(wildcard $(TFLITE_B)/_deps/ruy-build/ruy/libruy_*.a) \
  $(wildcard $(ABSL)/lib/libabsl_*.a) \
  $(GLOG)/lib/libglog.a

SRCS := gstlyra.cc gstlyrartp.cc

all: $(PLUGIN)

$(PLUGIN): $(SRCS) gstlyrartp.h
	$(CXX) $(CXXFLAGS) -shared -o $@ $(SRCS) \
	  -Wl,--start-group $(ARCHIVES) -Wl,--end-group \
	  $(GST_LIBS) -lpthread -ldl -lm

install: $(PLUGIN)
	install -d $(PLUGIN_DIR)
	install -m 644 $(PLUGIN) $(PLUGIN_DIR)/
	install -d /usr/local/share/lyra
	@echo "Now copy the model weights:  cp -r <build>/out/model_coeffs /usr/local/share/lyra/"

clean:
	rm -f $(PLUGIN)

.PHONY: all install clean
