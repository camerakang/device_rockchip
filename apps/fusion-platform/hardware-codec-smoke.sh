#!/bin/bash
set -euo pipefail
for tool in mpi_enc_test mpi_dec_test timeout; do
    command -v "$tool" >/dev/null
done
if [[ ! -e /dev/mpp_service && ! -e /dev/mpp-service ]]; then
    echo 'MPP 设备不存在，拒绝使用软件编解码代替硬件验证' >&2
    exit 1
fi
test_dir=$(mktemp -d /tmp/mscam-mpp-check.XXXXXX)
trap 'rm -rf "$test_dir"' EXIT
for codec in 7 16777220; do
    # MPP coding types: AVC=7, HEVC=0x01000004; frame format 0 is NV12.
    timeout 30 mpi_enc_test -w 720 -h 544 -f 0 -t "$codec" -n 30 \
        -o "$test_dir/video.$codec" > "$test_dir/encode.log" 2>&1
    test -s "$test_dir/video.$codec"
    timeout 30 mpi_dec_test -t "$codec" -i "$test_dir/video.$codec" \
        -o "$test_dir/decoded.$codec.nv12" > "$test_dir/decode.log" 2>&1
    test -s "$test_dir/decoded.$codec.nv12"
    echo "MPP hardware encode/decode passed: codec=$codec, 720x544"
done
