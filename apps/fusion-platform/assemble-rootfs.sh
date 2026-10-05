#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 FAKEROOTDONTTRYCHOWN=1
platform=/sdk/device/rockchip/apps/fusion-platform
install -d /boot /lib/firmware
touch /boot/build-host
cp -a /sdk/ubuntu22.04/overlay-firmware/usr/lib/firmware/. /lib/firmware/
apt-get install -y --no-install-recommends wireless-regdb
apt-get install -y --no-install-recommends \
    /sdk/ubuntu22.04/packages/arm64/gstreamer/libgstreamer1.0-0_1.22.9-1_arm64.deb \
    /sdk/ubuntu22.04/packages/arm64/gstreamer/libgstreamer1.0-dev_1.22.9-1_arm64.deb \
    /sdk/ubuntu22.04/packages/arm64/gstreamer/gir1.2-gstreamer-1.0_1.22.9-1_arm64.deb \
    /sdk/ubuntu22.04/packages/arm64/gstreamer/gstreamer1.0-tools_1.22.9-1_arm64.deb \
    /sdk/ubuntu22.04/packages/arm64/gst-rkmpp/gstreamer1.0-rockchip1_1.14-4_arm64.deb \
    /sdk/ubuntu22.04/packages/arm64/libmali/libmali-valhall-g610-g24p0-x11-gbm_1.9-1_arm64.deb \
    /sdk/ubuntu22.04/packages/arm64/xserver/xserver-common_21.1.7-3-ubuntu22.04_all.deb \
    /sdk/ubuntu22.04/packages/arm64/xserver/xserver-xorg-core_21.1.7-3-ubuntu22.04_arm64.deb \
    /sdk/ubuntu22.04/packages/arm64/xserver/xserver-xorg-legacy_21.1.7-3-ubuntu22.04_arm64.deb
kernel_release=$(cat /sdk/kernel-6.1/include/config/kernel.release)
mapfile -t image_debs < <(find /sdk -maxdepth 1 -name "linux-image-${kernel_release}_*_arm64.deb" | sort -V)
mapfile -t header_debs < <(find /sdk -maxdepth 1 -name "linux-headers-${kernel_release}_*_arm64.deb" | sort -V)
apt-get install -y --reinstall --no-install-recommends "${image_debs[-1]}" "${header_debs[-1]}"

cp -a /sdk/ubuntu22.04/overlay/. /
install -d /lib/firmware /boot /opt/mscam/calibration/images /opt/mscam/config \
    /apprun/releases /usr/share/mscam/fonts /usr/share/mscam/platform
# Hardware packages are preinstalled; avoid the vendor's first-boot package/NPU extraction.
touch /usr/local/first_boot_flag
install -m 755 /sdk/device/rockchip/common/data/configure-double-imx287m.py /usr/local/bin/configure-double-imx287m
install -m 755 /sdk/device/rockchip/common/data/imx287m-trigger-pwm.py /usr/local/bin/imx287m-trigger-pwm
install -m 755 "$platform/hardware-codec-smoke.sh" /usr/local/bin/mscam-hardware-codec-check
install -m 644 "$platform/mscam-core.service" /etc/systemd/system/mscam-core.service
install -m 644 /mscam-src/resources/fonts/mscam-osd-subset.ttf /usr/share/mscam/fonts/
gcc-12 -O2 -Wall -Wextra -Werror -std=c11 -pthread \
    /sdk/device/rockchip/apps/dual-imx287m-web/capture-worker.c -o /build-cache/capture-worker
install -d /opt/dual-imx287m-web/static
install -m 755 /build-cache/capture-worker /opt/dual-imx287m-web/
install -m 644 /sdk/device/rockchip/apps/dual-imx287m-web/app.py /opt/dual-imx287m-web/
install -m 644 /sdk/device/rockchip/apps/dual-imx287m-web/static/* /opt/dual-imx287m-web/static/
install -m 644 /sdk/device/rockchip/apps/dual-imx287m-web/dual-imx287m-web.service /etc/systemd/system/
systemctl enable dual-imx287m-web.service NetworkManager.service ssh.service lightdm.service
systemctl mask NetworkManager-wait-online.service systemd-networkd-wait-online.service
systemctl disable networking.service 2>/dev/null || true
rm -f /etc/systemd/system/sysinit.target.wants/usbdevice.service
install -d /etc/systemd/system/getty.target.wants
ln -sf /lib/systemd/system/serial-getty@.service /etc/systemd/system/getty.target.wants/serial-getty@ttyFIQ0.service

if ! id cat >/dev/null 2>&1; then
    useradd -m -u 1000 -s /bin/bash -G sudo,video,audio,plugdev cat
    printf 'cat:temppwd\n' | chpasswd
fi
printf 'cat ALL=(ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/lubancat-cat
chmod 440 /etc/sudoers.d/lubancat-cat
printf 'lubancat\n' > /etc/hostname
printf '127.0.0.1 localhost\n127.0.1.1 lubancat\n' > /etc/hosts
ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
install -d /etc/lightdm/lightdm.conf.d
printf '[Seat:*]\nautologin-user=cat\nautologin-session=xfce\n' > /etc/lightdm/lightdm.conf.d/20-lubancat.conf
ln -sf /run/NetworkManager/resolv.conf /etc/resolv.conf
rm -f /etc/ssh/ssh_host_*
install -m 755 "$platform/ssh-hostkeys" /usr/local/bin/mscam-ssh-hostkeys
install -m 644 "$platform/ssh-hostkeys.service" /etc/systemd/system/mscam-ssh-hostkeys.service
systemctl enable mscam-ssh-hostkeys.service
rm -f /etc/systemd/system/ssh-hostkeys.service

ldconfig
apt-mark hold librockchip-mpp1 librockchip-mpp-dev librockchip-vpu0 rockchip-mpp-demos \
    librga2 librga-dev libmali-valhall-g610-g24p0-x11-gbm \
    libgstreamer1.0-0 libgstreamer1.0-dev gir1.2-gstreamer-1.0 gstreamer1.0-tools gstreamer1.0-rockchip1 \
    "linux-image-${kernel_release}" "linux-headers-${kernel_release}"
dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > /usr/share/mscam/platform/packages.lock
cp /usr/share/mscam/platform/packages.lock /build-cache/packages.lock
rm -f /usr/sbin/policy-rc.d /etc/apt/apt.conf.d/99-rootfs-build
apt-get clean
rm -rf /var/lib/apt/lists/* /var/log/apt/*
rm -rf /home/forlinx
echo 'Rootfs assembled; fusion application service is installed but not enabled'
