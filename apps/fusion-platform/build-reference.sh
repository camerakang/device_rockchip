#!/bin/bash
set -euo pipefail
source_dir=/build-cache/mscam-reference-source
build_dir=/build-cache/mscam-reference-build
mkdir -p "$source_dir" "$build_dir"
tar -C /mscam-src --exclude=.git --exclude=build --exclude=build-release -cf - . | tar -C "$source_dir" -xf -
if ! grep -q '#ifdef AV_FRAME_FLAG_KEY' "$source_dir/src/codec/ffmpeg_decoder.cpp"; then
    patch -d "$source_dir" -p1 < /sdk/device/rockchip/apps/fusion-platform/patches/ffmpeg44-compat.patch
fi
cmake -S "$source_dir" -B "$build_dir" -DCMAKE_BUILD_TYPE=Release \
    -DMSCAM_REQUIRE_RKMPP=ON -DMSCAM_ENABLE_ZLM=ON \
    -DMSCAM_RELEASE_VERSION=fusion-base-v1-reference
cmake --build "$build_dir" -j "${MSCAM_BUILD_JOBS:-4}"
(cd "$source_dir" && "$build_dir/mscam_tests")
install -d /opt/mscam/sdk/bin
install -m 755 "$build_dir/mscam_cored" "$build_dir/mscam_ctl" /opt/mscam/sdk/bin/
