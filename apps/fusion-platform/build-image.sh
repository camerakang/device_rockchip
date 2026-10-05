#!/bin/bash
set -euo pipefail
platform_dir=$(cd -- "$(dirname -- "$0")" && pwd)
sdk_dir=$(cd "$platform_dir/../../../.." && pwd)
build_dir=${MSCAM_PLATFORM_BUILD_DIR:-"$sdk_dir/output/fusion-base"}
core_dir=${MSCAM_CORE_ROOT:-"$sdk_dir/../mscam-core"}
root_dir="$build_dir/rootfs"
cache_dir="$build_dir/cache"
image_name=LubanCat4_fusion_base_v1_ubuntu22.04_linux6.1.img
mkdir -p "$root_dir" "$cache_dir"
base_name=ubuntu-base-22.04.5-base-arm64.tar.gz
base_sha=075d4abd2817a5023ab0a82f5cb314c5ec0aa64a9c0b40fd3154ca3bfdae979f
if [[ ! -f "$cache_dir/$base_name" ]]; then
    curl -fL --retry 3 "https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/$base_name" -o "$cache_dir/$base_name"
fi
printf '%s  %s\n' "$base_sha" "$cache_dir/$base_name" | sha256sum -c -
if [[ ! -e "$root_dir/bin/bash" ]]; then
    tar --no-same-owner -xzf "$cache_dir/$base_name" -C "$root_dir"
fi
rm -f "$root_dir/etc/resolv.conf"
cp -L /etc/resolv.conf "$root_dir/etc/resolv.conf"
cp "$sdk_dir/ubuntu22.04/sources.list" "$root_dir/etc/apt/sources.list"
printf 'APT::Sandbox::User "root";\n' > "$root_dir/etc/apt/apt.conf.d/99-rootfs-build"
printf '#!/bin/sh\nexit 101\n' > "$root_dir/usr/sbin/policy-rc.d"
chmod +x "$root_dir/usr/sbin/policy-rc.d"
mkdir -p "$root_dir/build-cache" "$root_dir/sdk" "$root_dir/mscam-src"
namespace=(bwrap --unshare-user --uid 0 --gid 0 --bind "$root_dir" / \
    --ro-bind "$sdk_dir" /sdk --ro-bind "$core_dir" /mscam-src \
    --bind "$cache_dir" /build-cache --proc /proc --dev /dev --tmpfs /run --tmpfs /tmp \
    --setenv FAKEROOTDONTTRYCHOWN 1 --setenv HOME /root)
if [[ ! -x "$root_dir/usr/bin/fakeroot" ]]; then
    "${namespace[@]}" /bin/bash -c 'apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends fakeroot ca-certificates'
fi
fake_args=(-s /build-cache/fakeroot.state)
if [[ -f "$cache_dir/fakeroot.state" ]]; then
    fake_args+=(-i /build-cache/fakeroot.state)
fi
if [[ ${MSCAM_ROOTFS_PREPARED:-0} != 1 ]]; then
    "${namespace[@]}" /usr/bin/fakeroot "${fake_args[@]}" /bin/bash -c \
        'apt-get update; bash /sdk/device/rockchip/apps/fusion-platform/rootfs-build.sh; bash /sdk/device/rockchip/apps/fusion-platform/assemble-rootfs.sh'
fi
if [[ -n ${MSCAM_REFERENCE_BUILD:-} ]]; then
    install -d "$root_dir/opt/mscam/sdk/bin"
    install -m 755 "$MSCAM_REFERENCE_BUILD/mscam_cored" "$MSCAM_REFERENCE_BUILD/mscam_ctl" "$root_dir/opt/mscam/sdk/bin/"
else
    "${namespace[@]}" /usr/bin/fakeroot "${fake_args[@]}" /bin/bash /sdk/device/rockchip/apps/fusion-platform/build-reference.sh
fi
for binary in "$root_dir"/opt/mscam/sdk/bin/*; do
    file "$binary" | grep -q 'ARM aarch64'
done
"${namespace[@]}" /bin/bash -c 'set -e; for binary in /opt/mscam/sdk/bin/*; do ! ldd "$binary" | grep "not found"; done'

# The package database records the exact selected ABI, including MPP/RGA.
install -m 644 "$platform_dir/README.md" "$root_dir/usr/share/mscam/platform/README.md"
{
    printf 'platform=fusion-base-v1\n'
    printf 'kernel=%s\n' "$(git -C "$sdk_dir/kernel-6.1" rev-parse HEAD)"
    printf 'device=%s\n' "$(git -C "$sdk_dir/device/rockchip" rev-parse HEAD)"
    printf 'uboot=%s\n' "$(git -C "$sdk_dir/u-boot" rev-parse HEAD)"
    printf 'core=%s\n' "$(git -C "$core_dir" rev-parse HEAD)"
    printf 'core_require_rkmpp=ON\ncore_enable_zlm=ON\ndji=excluded\n'
} > "$root_dir/usr/share/mscam/platform/sources.lock"
cp "$root_dir/usr/share/mscam/platform/sources.lock" "$cache_dir/sources.lock"
(cd "$root_dir" && sha256sum opt/mscam/sdk/bin/* usr/share/mscam/fonts/mscam-osd-subset.ttf) \
    > "$root_dir/usr/share/mscam/platform/runtime.sha256"
printf 'fusion-base-v1\n' > "$root_dir/etc/mscam-platform-release"
rm -f "$root_dir/boot/build-host" "$root_dir/usr/sbin/policy-rc.d" "$root_dir/etc/apt/apt.conf.d/99-rootfs-build"
ln -sfn /run/NetworkManager/resolv.conf "$root_dir/etc/resolv.conf"
rm -rf "$root_dir/home/forlinx" "$root_dir/root/.cache/gstreamer-1.0"
rm -rf "$root_dir/sdk" "$root_dir/mscam-src" "$root_dir/build-cache"
mkdir -p "$build_dir/firmware"
root_image="$build_dir/firmware/rootfs.img"
fakeroot -i "$cache_dir/fakeroot.state" -- sh -c \
    'mke2fs -F -t ext4 -b 4096 -m 0 -L rootfs -U 614e0000-0000-4b53-8000-1d28000054a9 -d "$1" "$2" 917504' \
    platform-image "$root_dir" "$root_image"
e2fsck -fn "$root_image"
cp "$sdk_dir/output/firmware/parameter.txt" "$sdk_dir/output/firmware/package-file" "$build_dir/firmware/"
ln -sfn "$sdk_dir/kernel-6.1/extboot.img" "$build_dir/firmware/boot.img"
ln -sfn "$sdk_dir/u-boot/uboot.img" "$build_dir/firmware/uboot.img"
ln -sfn "$sdk_dir/u-boot/rk3588_spl_loader_v1.18.113.bin" "$build_dir/firmware/MiniLoaderAll.bin"
(cd "$build_dir/firmware" && "$sdk_dir/tools/linux/Linux_Pack_Firmware/rockdev/afptool" -pack . update-packed.img)
"$sdk_dir/tools/linux/Linux_Pack_Firmware/rockdev/rkImageMaker" -RK3588 \
    "$build_dir/firmware/MiniLoaderAll.bin" "$build_dir/firmware/update-packed.img" \
    "$build_dir/firmware/$image_name" -os_type:android
(cd "$build_dir/firmware" && sha256sum "$image_name" > "$image_name.sha256")
echo "Image created: $build_dir/firmware/$image_name"
