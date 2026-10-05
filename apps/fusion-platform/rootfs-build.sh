#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 FAKEROOTDONTTRYCHOWN=1
apt-get install -y --no-install-recommends \
    systemd-sysv udev initramfs-tools e2fsprogs dosfstools sudo openssh-server \
    network-manager iproute2 iputils-ping net-tools ethtool curl wget ca-certificates gnupg \
    rsyslog locales tzdata dbus dbus-x11 xfce4 lightdm xserver-xorg-core \
    xserver-xorg-input-libinput xserver-xorg-video-modesetting x11-xserver-utils \
    fonts-noto-cjk alsa-utils bluez usbutils pciutils bc i2c-tools v4l-utils \
    python3 python3-pil python3-pip python3-venv git make build-essential gcc-12 g++-12 \
    cmake ninja-build pkg-config libopencv-dev libspdlog-dev nlohmann-json3-dev \
    libavcodec-dev libavformat-dev libavutil-dev libswscale-dev libswresample-dev \
    ffmpeg libssl-dev libudev-dev libusb-1.0-0-dev libuvc-dev libasound2-dev \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev gstreamer1.0-tools \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly gstreamer1.0-libav libsrtp2-dev libusrsctp-dev \
    libjpeg-dev nginx u-boot-tools device-tree-compiler gpiod libgpiod-dev

apt-get install -y --no-install-recommends \
    /sdk/ubuntu22.04/packages/arm64/mpp/librockchip-mpp1_1.5.0-1_arm64.deb \
    /sdk/ubuntu22.04/packages/arm64/mpp/librockchip-mpp-dev_1.5.0-1_arm64.deb \
    /sdk/ubuntu22.04/packages/arm64/mpp/librockchip-vpu0_1.5.0-1_arm64.deb \
    /sdk/ubuntu22.04/packages/arm64/mpp/rockchip-mpp-demos_1.5.0-1_arm64.deb \
    /sdk/ubuntu22.04/packages/arm64/rga2/librga2_2.2.0-1_arm64.deb \
    /sdk/ubuntu22.04/packages/arm64/rga2/librga-dev_2.2.0-1_arm64.deb

update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-12 120
update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-12 120
ldconfig
pkg-config --exists rockchip_mpp librga opencv4 libavcodec libavformat
dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > /build-cache/packages.lock
echo 'Fusion platform runtime and development packages installed'
