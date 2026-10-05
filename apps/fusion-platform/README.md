# LubanCat4 fusion-base-v1

固定基础系统面向 LubanCat4 / LubanCat4 V1、CAM0 + CAM1 的 MV-MIPI-IMX287M 和共同触发。基础系统与融合应用分开交付，后续融合算法、RAW 转换、帧配对、界面和推流通过应用更新部署，无需重新烧录 rootfs。

## 固定范围

- Ubuntu 22.04 ARM64、XFCE，Linux 6.1.99-rk3588。
- 双 MVCAM、CIF/DCPHY、J9 物理第 12 针 PWM14_M2、MPP 与 RGA 内核支持。
- SDK 配套 MPP/RGA 运行库和头文件、MPP 测试程序、Rockchip GStreamer 插件。
- GStreamer 核心固定为 SDK 配套 1.22.9，与 Rockchip 插件匹配；关键硬件包及内核设置 apt hold。
- OpenCV、FFmpeg、OpenSSL、spdlog/fmt、OpenMP、GStreamer、USB/UVC、ALSA 的运行库及开发包。
- GCC 12、CMake、pkg-config、Python/Pillow，允许在板端用相同 ABI 编译更新应用。
- OSD 字体、`/opt/mscam` 持久化目录、`/apprun/releases` 应用版本目录和 `mscam-core.service` 模板。
- 强制启用 RKMPP 的 ARM64 参考 Core 和 CLI，位于 `/opt/mscam/sdk/bin`，用于验证依赖和后续应用开发，默认不启动。
- 现有双 IMX287M 网页测试程序，默认 RAW12、319.4 Hz、20 fps 预览。

不包含 DJI APP、Payload-SDK 或 Widget。融合使用 OpenCV/CPU/OpenMP；H.264/H.265/MJPEG 等压缩视频应显式选择 RKMPP 后端。RAW12 是未压缩像素数据，需要像素解包，不能把它作为压缩视频交给硬件解码器。现有测试网页使用 Pillow JPEG，供采集验证；它不代表未来正式融合码流的硬件编码实现。

当前 Core 产品配置仍针对 VIS/UV/IR 三路 USB 设备，因此不会自动启动参考 Core。IMX287M 采集类型、双路序号配对、RAW12 数据处理和双路产品配置属于下一步应用开发。只要保持已固定的内核接口和运行库 ABI，这些更新不需要重做系统镜像；新硬件或需要不同内核接口的功能不在此范围内。

## 构建

在 SDK 根目录准备当前已编译的内核、U-Boot 和内核 Debian 包，以及旁边的 `mscam-core` 源码（先执行 `git submodule update --init --recursive`）。参考 Core 会在 ARM64 根文件系统内编译、运行测试；首次通过 QEMU 编译比较慢。可设置 `MSCAM_REFERENCE_BUILD=/path/to/arm64-build` 使用已经通过测试的 ARM64 构建目录。源码尚未合入 FFmpeg 4.4 兼容修正时，构建脚本自动在源码副本应用 `patches/ffmpeg44-compat.patch`。

```sh
MSCAM_CORE_ROOT=/path/to/mscam-core bash device/rockchip/apps/fusion-platform/build-image.sh
```

主机使用 binfmt/QEMU、bubblewrap 用户命名空间和 fakeroot 构建 ARM64 根文件系统，不修改主机的软件包或已连接开发板的系统。包版本清单保存在镜像的 `/usr/share/mscam/platform/packages.lock`。镜像校验和与构建、硬件测试证据保存在 `output/fusion-base`。构建完成的 IMG 是 Rockchip 完整升级固件，使用 Loader 模式“升级固件”烧录。

## 开发和应用更新

```sh
# 网页测试： http://开发板IP:8080
sudo mscam-hardware-codec-check
sudo systemctl stop dual-imx287m-web

# 应用按版本放到 /apprun/releases/<version>/，包括 core/mscam_cored 和 config/mscam.conf。
# 原子切换 /apprun/current 后启用服务。默认不要求 DJI 专用的 eth1 或固定 IP。
sudo systemctl enable --now mscam-core
```

`mscam-core.service` 与测试网页互斥，避免同时占用相机。每台设备的标定数据位于 `/opt/mscam/calibration`，界面配置位于 `/opt/mscam/config`；应用版本替换时保留这些目录。测试网页与未来的 Core 都可能使用 8080，切换前先停止测试服务。

运行库已包含，并不意味着任意 FFmpeg/OpenCV 调用会自动走硬件。正式程序必须选择 RKMPP，初始化失败时应报错，不能静默切换软件编码。`mscam-hardware-codec-check` 使用 MPP 的编码与解码程序做往返验证，缺少 MPP 设备时直接失败。

## 基础版本验证

2026-10-05：参考 Core 使用新根文件系统的依赖交叉编译为 ARM64，`MSCAM_REQUIRE_RKMPP=ON`、ZLMediaKit 开启；在新根文件系统内通过 105 个测试用例、705 项断言。采集程序 compact RAW12 自检和网页的 6 个 Python 测试通过。新系统运行库放到 LubanCat4 的独立 chroot，MPP H.264/H.265 的 720×544 编码/解码往返通过；Core RKMPP/RGA 路径通过 H.264 编码/解码 30 帧和 H.265 编码 30 帧。GStreamer 1.22.9 能加载 Rockchip 插件，`videotestsrc → mpph264enc → h264parse → mppvideodec → fakesink` 的 30 帧管线正常到达 EOS。

这些验证没有替换开发板原系统，最终 IMG 仍需烧录后验证启动、桌面和双相机。源码提交、软件包版本和参考程序校验分别保存在镜像 `/usr/share/mscam/platform/sources.lock`、`packages.lock`、`runtime.sha256`。固定版本指这份经过校验的 IMG；从联网 apt 源重建时需按版本清单核对，不能假定远端软件包永远不变。
