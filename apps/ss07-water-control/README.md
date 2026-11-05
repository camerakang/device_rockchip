# SS-07 RK3588 独立工程

2026-11-05（0.10.6）：新增 mobocon 等 PC 程序使用的 API 客户端密钥（`ss07 api-client`，Bearer 访问状态、配置接口和 WebRTC/WHEP 拉流）和可选的局域网明文 RTSP/TCP 拉流端口 `plain_port`，六路视频继续由 MediaMTX 直通转发，详见 [mobocon 拉流与配置接口](docs/mobocon-video-api.md)。尚未部署。

2026-11-05（0.10.5）：监看页布局调整（配色不变）。概览四项移入顶栏；左侧导航收窄为 76px 图标栏；视频区和右栏按窗口高度铺满，1600×1000 与 1366×768 下无需滚动；视频面板新增“1＋5 / 均分”布局切换（浏览器本地记住）；帧时间叠加在画面左下角；时码、存储、设备合并为“系统状态”一张卡片。尚未部署到目标板。

2026-11-04（0.10.4）：网页改为系统控制台布局：顶部状态栏（服务连接、时钟、操作者），左侧导航切换“实时监看 / 录像回放 / 声呐记录 / 系统维护”，监看页顶部为视频在线、声呐有效、外部时码和录像四项概览，六路视频带在线指示与实时帧率，右侧栏集中显示声呐、时码、存储和设备信息；窄屏时导航改为顶部横向标签。配色、圆角和字号取自 [Tabler](https://github.com/tabler/tabler)（MIT）暗色主题，图标为 [Tabler Icons](https://github.com/tabler/tabler-icons)（MIT）内联 SVG，均为本地静态文件，无构建步骤和外部 CDN。接口与元素 ID 不变。已部署到 192.168.3.233（安装事务 47e30ed4，COMMITTED），现场配置保留，浏览器复核六路画面正常。

2026-11-04（0.10.3）：标准 `ss07-api.service` 启用 `serve --direct-access`，网页打开后自动进入监看、录像和配置界面，无需用户名密码，也不显示退出按钮。所有能访问网页的用户具有维护操作权限；后台仍按浏览器分配独立会话，保留 HTTPS、同源/CSRF 校验和操作审计（操作者记为 `local-operator`）。此变更已部署到 192.168.3.233，安装事务已提交，原现场配置保留。手动启动该模式需要 `--public-origin`（或 `--gateway-config`）与 `--access-audit`，不需要账号文件；移除 `--direct-access` 可继续使用原账号模式。原生 RTSPS 的独立账号机制保持不变。

五路 USB 当前按用户确定的 **H.264 720p25、2＋2＋1 接线**推进[项目验收](docs/acceptance-720p25.md)。720p25 是正式验收模式；既有 1080p25 失败记录保留，生产链路、实盘和长期运行等仍须分别验证。

当前开发版本为 **0.10.5（控制台布局调整）**，0.10.4 为控制台界面，0.10.3 为网页直接进入。0.10.1 新增媒体服务 WebRTC 所需的 AF_NETLINK 权限，0.10.2 修正维护日志接口空查询的默认参数处理；既有应用与 IMG 的冻结证据保留，后文历史版本记录不代表包含这些修订。工作区已增加[声呐公共框架](docs/sonar-foundation.md)、[数据盘采样归档](docs/sonar-archive.md)、[服务配方及恢复接入](docs/sonar-services.md)和[0.9↔0.10 配置迁移](docs/sonar-migration.md)：公平轮询、独立诊断与归档队列、原接收时刻关联、文件/索引恢复、保留与锁定、受权限限制的查询/下载、网页预览及保留数据的升级恢复。厂家驱动仍为空，板端实际权限和外设验收待完成。0.10.0 应用包与完整 IMG 已生成并通过下述软件验证；旧 0.9.0 包及 0.6.1 IMG 保留。

发布基线 **0.9.0** 采用 **C++/GStreamer 视频直通 + MediaMTX 分发/独立录像 + Python 3.8+ 管理服务**。已有五路原生 H.264 采集进程、SE2017 RTSP 接入口、六路 WebRTC、分段录像、SQLite 索引、回放导出、数据盘保护、只读 0x12 时码片段关联与 epoch 查询，以及 HTTPS 权限维护。0.7.0 完成逐服务配置应用与恢复；0.8.0 增加有摘要校验的安装与版本恢复；0.9.0 增加默认关闭的原生 RTSPS/TCP 播放器访问、账户撤权隔离和访问审计。

已生成 **0.10.0 ARM64 应用包与完整 RKFW IMG**。新 rootfs/initrd/boot 和完整镜像通过六载荷、13,824 个文件系统条目/权限及 300 个模块回读检查，QEMU 中运行镜像 API/网页及未配置时码/声呐服务通过。启动组件来自当前干净 SDK 源码对应的 Linux 6.1.99、DTB、模块和 U-Boot 独立构建产物。目标板尚未部署，新内核启动、真实六路、声呐、时码及 LAN 仍未验收；范围见[镜像证据](docs/evidence/full-image-0.10.0-validation.json)和交付进度。

2026-10-24 补充[现有板端原生版本查询](docs/evidence/board-native-versions-20261024.json)：精确包内相机、MediaMTX、Caddy 通过匿名内存文件执行，全部退出 0，未安装文件或启动服务。此项证明初始程序加载及版本分支，未验证采集、插件管线或实际服务权限。

[交付进度与缺口](docs/delivery-progress.md) · [MOBOCON 开发文档](docs/mobocon-development.md) · [mobocon 拉流与配置接口](docs/mobocon-video-api.md) · [原生播放器访问](docs/native-rtsp.md) · [安装与版本恢复](docs/installation-recovery.md) · [配置应用与恢复](docs/configuration-application.md) · [完整镜像](docs/full-image.md) · [ARM64 构建](docs/arm64-build.md) · [权限与维护](docs/auth-maintenance.md) · [原生视频](docs/native-video.md) · [录像与回放](docs/recording.md) · [只读时码](docs/timecode-runtime.md) · [项目需求](docs/requirements.md) · [基础接口](docs/foundation.md) · [实物探测记录](docs/target-probe-192.168.3.233.md)

在本目录运行：

```sh
make validate
make test
make run
# 浏览器访问 http://127.0.0.1:8077/
# 另一终端检查 API/SSE
curl http://127.0.0.1:8077/api/v1/status
curl -N http://127.0.0.1:8077/api/v1/events
```

Ctrl-C 或 SIGTERM 停止服务。指定解释器使用 `make PYTHON=/path/to/python test` 或 `SS07_PYTHON=/path/to/python ./tools/ss07 serve`。

```sh
./tools/ss07 probe > /tmp/ss07-probe.json
./tools/ss07 inventory --probe /tmp/ss07-probe.json
./tools/ss07 init-config --output /tmp/ss07-system.json
./tools/ss07 serve --config /tmp/ss07-system.json
./tools/ss07 check-cameras --config /tmp/ss07-system.json --probe /tmp/ss07-probe.json
```

初始化不覆盖已有文件。保存配置通过 `update-config --config ... --candidate ... --expected-revision ...`；仅保存候选值。完整服务配置下，维护页面可预览影响、明确应用，再读取逐服务确认与失败恢复结果。上面的 `make run` 是本机只读开发模式；启用账户、HTTPS 和维护进程后才开放 HTTP 配置保存及录像锁定，配置应用还需独立协调器，步骤见对应说明。设备参数默认 null，枚举/结构校验通过不代表实际采集通过。

```sh
# 输出目录必须尚不存在；不部署、不启用服务
make package OUTPUT=/tmp/ss07-0.10.0-package
make stage DESTDIR=/tmp/ss07-0.10.0-rootfs
cd /tmp/ss07-0.10.0-package
sha256sum -c SHA256SUMS
tar -xzf ss07-0.10.0.tar.gz
./ss07-0.10.0/tools/ss07 serve
```

上述命令生成源码包或默认暂存树。包含已编译相机、MediaMTX 和 Caddy 的 ARM64 应用包使用 [独立构建入口](docs/arm64-build.md)，包含文件摘要、ABI 和测试证据，不包含测试视频源二进制。所有暂存流程都不启用服务。实际部署、提交、推送和烧写仍等待明确指令。完整系统与硬件验收未完成。

已有 0.4.0 录像库需要先停止录像并按 [数据库迁移说明](docs/timecode-runtime.md#录像索引升级) 保存备份、显式迁移；程序不会覆盖或自动降级旧库。时码 500000 baud 已确认，其余 UART 参数及稳定设备绑定为空时服务保持不可用，不尝试打开串口。USB 接收时刻未经 SOF 校准，关联始终标为 ESTIMATED 或 UNAVAILABLE。

以下保留原始总体设计入口；当前实现状态以上述基础工程说明为准。


# SS-07 水上控制系统

当前设计范围仅包含两个独立软件：RK3588视频与声呐系统，以及云台接收器电机控制固件。跟焦系统已经设计完成，作为既有系统保留；mobocon、UE电脑和RS485时码器仅定义外部接口/依赖，不纳入本次应用开发交付。

| 软件 | 独立设计 | 独立交付 |
| --- | --- | --- |
| RK3588系统 | [RK3588详细设计](docs/rk3588-design.md) | 六路视频、五声呐、监看/录像/配置应用、完整SDK镜像 |
| 云台接收器 | [云台固件详细设计](docs/gimbal-receiver-design.md) | 摇轮/PC输入、三轴CyberGear控制、位置反馈、仲裁、V2.0实时目标执行固件 |

- [系统边界与拓扑](docs/architecture.md)
- [两个软件的开发与验收](docs/implementation-plan.md)
- [时码输入与播放边界](docs/timecode-integration.md)：时码接mobocon、UE和RK只读记录主机，控制接收器不接时码。
- [RK时码记录关联](docs/rk-timecode.md)：独立接口、时码映射、录像/声呐标签与失时处理。
- 云台接口资料：[CyberGear](docs/cybergear-interface.md)、[公共V2.0](docs/mobocom-integration.md)、[PC定位/遥测](docs/position-telemetry.md)。

RK3588不转发运动命令；两软件分别构建、部署、更新和验收，最终验证共存。RS485时码器接mobocon、UE和RK只读记录主机，不接云台或跟焦接收器；已采用V2.0主机求值并持续下发目标的播放方式。船岸视频使用网线，PC通过RS485访问云台接收器。

架构设计的协议库入口：`device_protocol/docs/ss07/README.md`；本目录保留SDK开发所需的对应设计副本。

原始交接时仅有设计文档；RK 当前实现见上文。SS-07 完整镜像已构建并通过离线检查，尚未烧写或验证实物启动。docs/reference保存早期扩大范围的方案，仅作历史参考；receiver-integration.md仅提供已有接收器基础资料，其跟焦扩展不属于当前任务。
