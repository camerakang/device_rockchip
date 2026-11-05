# ARM64 应用与 SDK 启动组件构建

当前候选为 0.10.0；精确应用包、安装工具和摘要见[安装说明](installation-recovery.md)，新完整 IMG 见[镜像构建](full-image.md)。下文构建命令仍适用，历史版本的验证数量与范围保留原日期。

2026-10-24 在现有 RK（Linux 5.10.160、cat UID 1000）完成精确 0.10.0 包内三个原生程序的版本查询：相机 0.3.1、MediaMTX 1.21.2、Caddy 2.11.7，全部退出 0。归档及三个程序分别核验 SHA256，通过 SSH 输入到匿名、已封写的 memfd，执行时启用 `LD_BIND_NOW=1`，未使用 QEMU 或 Go workaround。程序退出后内存 FD 关闭，没有安装文件或启动服务。相机版本来自编译时常量，查询不执行 `gst_init`，故此项只证明这些程序在现有板端的初始加载及版本分支可运行，不证明插件、实际媒体、权限、外设或新内核。见[板端版本查询证据](evidence/board-native-versions-20261024.json)。首次封装因过早设置零文件大小限制而未能写入 memfd，在候选程序执行前失败；修正设置顺序后通过，原始失败记录保留于 `output/ss07/board-native-versions-20261024/failure.json`。

以下为 2026-10-23 的 0.7.0 阶段记录：应用 0.7.0 / 原生相机 0.3.1。原生二进制及依赖未变化，打包前仍核对相机源码摘要、ELF 与已有原生验证证据。新增配置应用/恢复的 117 项管理测试在 ARM64 Python 3.8.10/QEMU 中通过。彼时 0.6.1 完整 rootfs/boot 分区与 Rockchip IMG 已通过离线回读与用户态验证，它不包含 0.7.0 功能。没有安装到目标板或启用服务，新内核实际启动仍未验收。

## 依赖与兼容性

板端只读核验为 Ubuntu 20.04 ARM64、Linux 5.10.160、glibc 2.31、Python 3.8.10、GStreamer 核心 1.18.5。SDK 当前选用 Linux 6.1；本次源码实际生成 **6.1.99-rk3588**。应用 ABI 与新内核启动是两项不同的验证。

| 部分 | 构建/验证版本 |
| --- | --- |
| 交叉编译器 | SDK AArch64 GCC 10.3.1，记录编译器 SHA256 |
| libc / C++ 运行库 | Ubuntu focal glibc 2.31 / libstdc++ 10.5.0 |
| GLib / GStreamer 核心 | 2.64.6 / SDK 1.18.5-1 |
| v4l2src、rtph264pay、udpsink、h264parse | Ubuntu focal 1.16.3 插件；queue/capsfilter 为核心插件 |
| 测试编码器 | x264enc 插件 1.16.2，只进入独立的测试程序 |
| 媒体 / HTTPS | MediaMTX v1.21.2 / Caddy v2.11.7，双层归档和 ELF 摘要校验 |

`packaging/arm64-sysroot.lock.json` 锁定 362 个构建/测试 deb 的 URL、大小、SHA256，以及 SDK 三个 GStreamer deb。初次解析使用隔离 APT 状态和 Ubuntu 签名索引，索引摘要随锁保存。重复构建直接按锁下载/校验，不执行 apt install、包维护脚本或板端命令。这个 sysroot 包含开发和解码测试依赖，不是最小生产 rootfs；应用包不捆绑这些系统库。

编译显式限定 pkg-config、头文件和链接目录，避免引入开发机 x86 库，也排除 SDK 为另一版 libc 生成的 `include-fixed/pthread.h`。`verify-arm64.py` 检查 AArch64 ELF、动态加载器及完整 DT_NEEDED/版本符号依赖；应用不携带构建路径 RPATH。发行库的固定目标子目录（例如 elfutils）按 sysroot 内路径解析。

0.3.1 将 SPS/PPS 注入保留在 h264parse，关闭 RTP payloader 的重复注入。在实测 1.16.3 RTP 插件下，旧配置会在一个多 slice IDR 中重复插入参数集，使捕获的 H.264 不能完成解码；修改后同一夹具解码 21 帧。生产管线仍是 H.264 直通，不解码、不转码，不修改视频时钟。属性语义见 GStreamer 的 h264parse 和 rtph264pay 官方文档。

## 构建和验证入口

以下输出目录必须尚不存在，缓存目录可以复用。宿主需 Python 3.8+、CMake、pkg-config、readelf、dpkg-deb；测试另需 qemu-aarch64-static、bwrap、启用 ARM64 binfmt 的宿主，以及宿主 GStreamer/OpenH264 解码插件。

```sh
ss07_sdk=/home/forlinx/workspace/LubanCat_SDK
ss07_app="$ss07_sdk/device/rockchip/apps/ss07-water-control"
ss07_build=/tmp/ss07-arm64-build
python3 "$ss07_app/packaging/prepare-arm64-sysroot.py" \
  --sdk "$ss07_sdk" --cache "$ss07_build/deb-cache" --output "$ss07_build/sysroot"
python3 "$ss07_app/packaging/build-arm64.py" \
  --sdk "$ss07_sdk" --sysroot "$ss07_build/sysroot" --output "$ss07_build/native" --with-test-source
python3 "$ss07_app/packaging/fetch-media.py" \
  --component mediamtx --arch arm64 --output "$ss07_build/media"
python3 "$ss07_app/packaging/fetch-media.py" \
  --component caddy --arch arm64 --output "$ss07_build/gateway"
python3 "$ss07_app/tools/test-arm64-management.py" \
  --sysroot "$ss07_build/sysroot" --output "$ss07_build/management-test"
python3 "$ss07_app/tools/test-arm64.py" \
  --sysroot "$ss07_build/sysroot" --native-dir "$ss07_build/native/build" \
  --media-dir "$ss07_build/media" --gateway-dir "$ss07_build/gateway" \
  --output "$ss07_build/media-test"
python3 "$ss07_app/packaging/package-arm64.py" \
  --native-build "$ss07_build/native" --media-dir "$ss07_build/media" \
  --gateway-dir "$ss07_build/gateway" --validation "$ss07_build/media-test/result.json" \
  --output "$ss07_build/package"
```

本机 QEMU 6.2 在 Go 1.26 程序中出现堆故障，与 Go 官方问题 #77572 描述一致；本次为媒体/网关的模拟器版本检查显式添加 `--qemu-go-madvfree`，仅向这两个 QEMU 进程设置 `GODEBUG=madvdontneed=0`。该选项记录在证据里，不加入应用服务配置，也不构成 Go 服务的板端运行验收。升级模拟器后应去掉此选项重新验证。参考：`https://github.com/golang/go/issues/77572`。

ARM64 管理测试使用真正的隔离根目录，运行 Python 3.8.10、SQLite 3.31.1、OpenSSL 1.1.1f；仅用 `qemu -L` 会让绝对路径和 `openat` 目录 FD 的解释不一致，不能用于数据盘保护测试。隔离环境只暴露临时设备节点和临时目录。

测试源明确标记 `synthetic_test`。生产二进制拒绝 `synthetic-test`，缺少稳定摄像头节点时返回 `device_open_failed`。ARM64 测试程序不会进入应用包。媒体测试验证必要插件加载、RTP 序号、实际 H.264 解码和 SIGTERM；性能数值不用于估计 RK3588 吞吐。

## 应用包内容与部署前条件

当前 `ss07-0.10.0-linux-arm64.tar.gz` 包含 `/opt/ss07/releases/0.10.0`、相机/媒体/网关 ARM64 程序、网页、候选/运行配置模板、systemd/sysusers/tmpfiles 模板、依赖与验证清单。`SS07-MANIFEST.json` 列出所有文件摘要和符号链接，外层 `SHA256SUMS` 校验归档。它是应用文件树，不是整盘 IMG；不会自动安装系统依赖、启用服务或创建录像目录。

进入板端运行之前须明确授权部署，并在隔离安装流程中检查系统库/插件、账号、TLS 证书、网关 IP/ICE 地址及真实硬件绑定。当前示例配置仍保留缺失参数：SE2017 RTSP URL、时码除 500000 baud 以外的 UART 参数/稳定路径、数据盘 UUID。声呐保持不可用。账号和证书未配置时权限入口不启动；盘未连接时录像保持禁止。

## 当前源码的 SDK 启动组件

```sh
python3 "$ss07_app/packaging/build-sdk-boot.py" \
  --sdk "$ss07_sdk" --output "$ss07_build/sdk-boot" --jobs 8
```

构建入口要求 kernel-6.1/u-boot/rkbin 源仓库干净，按当前 HEAD 导出到新目录。厂家 GPU/WLAN 驱动不支持 `O=` 输出目录，故在这份独立源码副本中构建，不清理原 SDK。产生内核 Image、DTB/overlay、匹配模块以及新编译 U-Boot 打包结果，保留命令、日志、源码归档摘要及 612 个产物摘要。

DDR、SPL、BL31、BL32 是锁定 rkbin 提交中的厂家二进制，不声称从源码重编。这些组件现已与新 rootfs、匹配 initrd/boot 分区组成完整 IMG，没有复用旧 `output/firmware` 或其他产品镜像。离线构建和用户态检查通过不等于 LubanCat-4 已启动通过。

0.6.1 原生/镜像基线证据见 `docs/evidence/arm64-validation.json`、`arm64-qemu.json`、`arm64-management.json` 和 `sdk-boot-build.json`；0.7.0 新增验证见 [配置应用证据](evidence/configuration-validation.json)。最终整机验收仍见 [交付跟踪](delivery-progress.md)。
