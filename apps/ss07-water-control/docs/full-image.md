# SS-07 独立完整镜像构建

此入口为 LubanCat-4 V1 / RK3588 组装独立 Rockchip RKFW update IMG，输出与 SDK 原有产品分离。镜像构建、离线解包校验、ARM64 用户态测试和板端实际启动分别记录；前面三项不能代替板端启动或八小时实物验收。提交、推送和烧写仍按用户要求等待明确指令；本轮应用已按验收授权部署，见下文。

2026-10-26 最终验收修订为 **`SS07-0.10.2-LubanCat4-V1-6.1.99-rk3588.img`**，SHA256 `3e5f1447a76875d60511477b24f43b1c9cfd7542fe6b08e82edac4eee859f78f`。它在 0.10.1 基础上包含日志默认查询修复，独立载荷、文件与 inode 权限回读通过；ARM64 管理测试增至 240 项并通过。对应记录位于同一验收目录的 `image-0.10.2/`、`image-readback-0.10.2/` 和 `image-userland-0.10.2/`。最终板端应用已正式升级为 0.10.2，IMG 仍未刷写或启动。后文 0.10.1 和 0.10.0 为保留的阶段记录。

2026-10-26 按用户五路 720p25 验收指令，新生成 **`SS07-0.10.1-LubanCat4-V1-6.1.99-rk3588.img`**，SHA256 `5224d623c9e2fb09e34600072555939f232a8669032389a7189459994d0cd5d7`。它包含正式媒体单元的 `AF_NETLINK` 修复和新的五路 720p25 配置示例；六个载荷、13,826 个 rootfs 条目、300 个模块的独立回读及 ARM64 API/网页用户态检查通过。证据目录为 `output/ss07/acceptance-720p25-20261026/`。本轮已通过官方安装工具在现有 Linux 5.10.160 板端部署并激活 0.10.1 应用，实际五路 LAN 网页解码与恢复验证见[本轮验收](acceptance-720p25.md)；**新 IMG 尚未刷写或启动**，既有系统上的应用验收不证明新内核与镜像可启动。下文 0.10.0 为保留的历史构建记录。

2026-10-24 已生成 **`SS07-0.10.0-LubanCat4-V1-6.1.99-rk3588.img`**，大小 3,364,815,434 字节。六个载荷、13,824 个 rootfs 条目和 300 个模块通过回读检查；镜像内 ARM64 API/网页及 SIGTERM 退出通过。独立检查确认 0.10.0 工厂登记、回滚缓存、113 个应用文件和外置恢复助手；时码、声呐、API 三个进程在私有状态目录运行并正常退出，未配置设备均不可用、零测距，Python 审计记录零次 tty 打开。SHA256 为 `e3301879b2ae43d0a918621c3e2a251c51eadc4be47d6d6d97fa5584bba0e612`，产物与日志位于 `output/ss07/image-20261024/`，见 [0.10.0 镜像证据](evidence/full-image-0.10.0-validation.json)。

历史 0.6.1 IMG 及其内部锁文件、源码归档、验证证据继续保留，见 [旧镜像证据](evidence/full-image-validation.json)。本轮重新构建 rootfs、initrd、boot 文件系统和完整 RKFW，纳入配置应用/恢复、安装与版本恢复、原生 RTSPS、声呐公共框架/归档及 0.9↔0.10 迁移。厂家协议与实物声呐传输驱动仍缺失。启动组件沿用此前在独立源码副本中编译的产物，本轮重新核对其 612 个产物摘要和当前干净 SDK 提交；本轮未重新编译或启动内核，不以旧板端内核证明新镜像可启动。

## 输入与默认状态

`packaging/image.lock.json` 固定 Ubuntu Base 20.04.5 ARM64 归档、325 个 deb 的版本/来源/摘要、0.10.2 ARM64 应用包（摘要 `cffb4218b8368750db5433f28353a57a3f3abed20a4dc3076eb420bfa84bf62a`）及板卡设备树。OS 包与已验证的 0.6.1 镜像基线相同。基础归档的官方 SHA256SUMS 已通过 Ubuntu CD Image 签名验证；运行库来自签名索引和本地 SDK 的 GStreamer 1.18.5 包。安装后 base-files 为 Ubuntu 20.04.6，Python 3.8.10、glibc 2.31、GStreamer 核心 1.18.5/插件 1.16.3 与已核验的板端 ABI 对齐。

镜像使用当前源码构建的 Linux 6.1.99-rk3588、LubanCat-4 V1 DTB、匹配模块和 U-Boot。initrd 在新的 ARM64 rootfs 中针对这版内核生成。DDR/SPL/BL31/BL32 来自锁定 rkbin 提交的厂家二进制，来源区别保存在启动组件清单中。没有复用旧 boot、uboot 或其他产品 rootfs。

镜像为无桌面的 USB H.264/Ethernet 应用环境，不包含测试视频源二进制。默认配置仍未绑定设备；`config/board-rk3588.example.json` 保存三个控制器 2＋2＋1 的五路物理路径、H.264 720p25 模式及用户确认的 500000 baud，启用前须复核实际连接。声呐、SE2017 URL、时码其余 UART 参数和数据盘 UUID 保留未知。

默认安全状态是 **unprovisioned**：

- root 与 ss07-admin 密码锁定，没有默认密码、SSH 公钥、应用账户或 TLS 私钥。可在构建时显式传入单个管理员公钥；不自动读取开发机密钥。
- SSH 仅在管理员 authorized_keys 存在时启动，首启生成自己的主机密钥；禁止口令和 root 登录。
- `ss07.target` 已建立，业务/配置 unit 均要求 `/etc/ss07-provisioned`，包含新增声呐服务；独立版本恢复 oneshot 与其启动门控位于 `current` 之外。构建不创建初始化标记。部署需先完成账户、证书、接口地址、配置与权限检查，再按 [安装与恢复说明](installation-recovery.md) 明确激活。
- 有线接口使用 DHCP，UTC/NTP 与有限额的 journal 已配置。原产品的 MIPI/PWM 触发 overlay、屏幕自动加载和压力测试不开启。
- 录像保持关闭，不创建 `/data/ss07`，不写入不存在的 1 TB 数据盘，不把系统盘目录当录像盘。

未提供公钥的默认镜像没有预设远程管理入口。实际部署应先提供授权公钥并准备初始化信息；本次产物不属于开箱即可采集的设备配置。

## 构建

宿主已验证为 x86_64 Ubuntu，Python 3.10、fakeroot 1.28、systemd-tmpfiles 249、e2fsprogs 1.46.5、QEMU 6.2。需要 bwrap/user namespace、ARM64 binfmt、dpkg-deb、tar、fdtget、debugfs、mke2fs、e2fsck、lsinitramfs。另需当前 SDK 构建工具和已完成的 [ARM64 应用与启动组件构建](arm64-build.md)。

所有输出目录必须不存在；缓存可复用。无需 sudo，不在宿主安装包，也不打开宿主摄像头或串口。

```sh
ss07_sdk=/home/forlinx/workspace/LubanCat_SDK
ss07_app="$ss07_sdk/device/rockchip/apps/ss07-water-control"
ss07_out="$ss07_sdk/output/ss07/my-image-build"
ss07_boot="$ss07_sdk/output/ss07/arm64-20261023/sdk-boot-fixed"
ss07_package="$ss07_sdk/output/ss07/acceptance-720p25-20261026/arm64-0.10.2/ss07-0.10.2-linux-arm64.tar.gz"

python3 "$ss07_app/packaging/build-image-rootfs.py" \
  --sdk "$ss07_sdk" --boot-build "$ss07_boot" --application "$ss07_package" \
  --cache "$ss07_out/cache" --output "$ss07_out/rootfs"
# 后续经授权的部署构建可显式添加 --admin-key /path/to/administrator.pub。

python3 "$ss07_app/packaging/assemble-image.py" \
  --sdk "$ss07_sdk" --rootfs-build "$ss07_out/rootfs" --output "$ss07_out/image"

python3 "$ss07_app/tools/verify-image.py" \
  --sdk "$ss07_sdk" --image-build "$ss07_out/image" --output "$ss07_out/readback"

python3 "$ss07_app/tools/test-image-userland.py" \
  --image-verification "$ss07_out/readback" --output "$ss07_out/userland-test"
```

依赖按锁校验后形成隔离本地 APT 仓库，ARM64 包维护脚本在新 user/mount/PID/network namespace 中真实执行。构建只读挂载 cgroup 供 systemd 245 静态校验使用，不启动宿主服务。Focal fakeroot 1.24 不支持 tmpfiles 使用的部分空路径 fchownat 操作，因此服务目录由宿主 fakeroot 1.28 + tmpfiles 按同一配置初始化，保存 UID/GID 状态后创建 ext4。

默认 rootfs 文件系统为 3 GiB，boot 为 128 MiB、U-Boot 分区为 8 MiB；SDK GPT 参数的 rootfs 为 grow 分区，fstab 配置文件系统扩展。rootfs 构建大小受限于 2～3.5 GiB，保持本 SDK 打包载荷小于 4 GiB。此空间是系统盘初始布局，与尚未接入的名义 1 TB 录像盘无关。

产物是厂商 RKFW 更新格式，不能按 raw 整盘文件理解。构建程序没有烧写入口。输入版本和摘要固定，文件系统创建时间等仍影响最终镜像字节；不承诺不同构建时间生成相同 SHA256。

## 验证与证据解释

`rootfs/manifest.json` 的 complete 仅在包安装、目标架构配置/插件/unit 检查、目录权限初始化和清理全部成功后成立。失败目录保留 complete=false，不复用为成功镜像。

`image/manifest.json` 的 build_complete 表示新文件系统、启动脚本和 RKFW 打包完成。它保留 verification_complete=false；后续校验写独立结果，不修改已被引用的构建清单。`readback/result.json` 才记录最终 IMG 的解包检查，`userland-test/result.json` 记录从该 IMG 解出的实际程序运行。

离线检查覆盖：外层 SHA256/RKFW 标头、六个载荷与打包输入一致、两个文件系统 fsck、rootfs 所有文件内容/符号链接与 inode UID/GID/模式、内核/DTB/initrd 与当前构建一致、模块版本及摘要、默认密码锁定、机器身份清除和服务目录权限。afptool 2.29 会重建解包后的 package-file，校验器同时检查嵌入的原始字节和重建后的文件映射。导出文件的 chown 可能清除 setgid 位，因此权限取自镜像 inode，不拿宿主导出副本代替。

用户态测试在只读镜像文件树和隔离网络中运行安装的 ARM64 Python/原生相机程序，验证 API/网页/状态及 SIGTERM 正常退出。它不运行 Linux 6.1 内核，不启动整套 systemd 服务，也不提供 RK3588 性能或硬件接入证明。

本轮另在该回读树运行时码、声呐和 API。工厂版本收据、保留的导入缓存、独立恢复助手和未配置状态均独立检查。首次附加检查因输出绑定在只读根目录而失败，改为绑定隔离的 `/tmp` 后通过，原失败日志保留。tty 打开审计验证 hook 已在三个进程加载；零次打开只证明当前未配置路径，没有测量 RS485 电气零 TX、真实串口权限、完整 systemd 或数据盘行为。脚本、命令和摘要见镜像证据。

实际烧写前仍需完成部署授权、明确管理员入口与初始化参数；随后验证新内核启动、USB/网口/串口/数据盘、服务权限、真实采集和满载故障恢复。配置逐服务应用、安装升级回滚及声呐厂家驱动的进度见 [交付跟踪](delivery-progress.md)。
