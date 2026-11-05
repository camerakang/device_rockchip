# RK3588 第 1 阶段：独立工程与真实探测

本文为 0.1.0 历史阶段记录，当前状态见 [完整交付进度](delivery-progress.md)。

本轮开发日期：2026-10-21（Asia/Shanghai）。可运行版本 0.1.0。本文记录当前实现；原始设计文档及 reference 保留作为设计背景。只有 API 和探测工具已实现，硬件服务不是运行中的空壳进程。

## 已核对的工作区与版本

SDK 根目录由 repo 管理，不是顶层 Git 仓库。`repo status` 显示各子仓库在 `SS-07-水上控制系统` 分支；初始未提交内容只有 device/rockchip 下整份 SS-07 文档目录（未跟踪）。未发现 SDK 根、上级目录或 device 子树的适用 AGENTS.md。外部 device_protocol 的 AGENTS.md、SKILL.md、规范、接入指南、timecode README 与 Python 参考实现已读取，未改其工作树。

| 来源 | 核验结果 |
| --- | --- |
| device/rockchip HEAD | f6307ce935a643c1fa846bd38bcd7c3ae016a3dd |
| kernel-6.1 HEAD | 6d69dd475bce666e8889f4b3bcc79f38bf1b3646 |
| u-boot HEAD | ce67cf027de25347c5c790c3087f6e663f4f3d56 |
| device_protocol 本地 HEAD | c6f23e003cf1c3aeb27a488d152b73327b1827f0，匹配交接 |
| device_protocol 远端 main | 03b33695c2e3e9e81d4faa6213fbe1c9fb3f7697，已 fetch 读取差异，没有切换/合并 |
| 公共协议最新版本 | 2.1.0-candidate.2、Control 0x21；旧设计的 V2.0/0x20 描述已过时 |
| RK 时码 | 仍为独立 0x12；26B、小端、CRC16 CCITT-FALSE，reference.py 无变化；尚未引入运行依赖 |

最新 Control 改动包括设备类型注册表、单会话恢复、防重放、LIMIT_TRIP 等，均不构成本工程的运动控制职责。时码串口参数未由格式文档冻结，不能从 Control 的 1Mbps 推定时码波特率。RK 后续仅复用时码解码规则，不套用 HostTimeline 播放策略。

用户提供的外部仓库仅登记边界：

- `git@codeup.aliyun.com:629f6370487c500c27f60603/device_protocol.git`：公共协议与设计事实源。
- `git@codeup.aliyun.com:629f6370487c500c27f60603/versatile/fans_ctrller.git`：风机设备，非声呐协议，未克隆或修改。
- `git@codeup.aliyun.com:629f6370487c500c27f60603/interfocal/virtual_lens_radio_comm.git`：接收器设备，外部独立项目，未克隆或修改。

## 工程模块与接口

| 模块 | 本轮实现/接口 | 后续职责与所有权 |
| --- | --- | --- |
| `ss07.config` | schema 文件 + 严格 JSON 读取 + 结构/跨字段校验；CLI validate | 配置维护持久化与逐服务应用需后续独立实现 |
| `ss07.probe` | CLI probe 输出 JSON 实测清单，逐命令记录状态、输出、退出码 | 离线能力证据，不作为实时健康源 |
| `ss07.api` | 仅回环 HTTP，只读快照；最多 16 个请求线程、连接 3 秒超时 | 聚合 worker 状态，不持有相机/串口 |
| camera worker（未实现） | 每个固定方向独立进程 | 独占该 V4L2 节点；C++/V4L2/GStreamer 原生 H.264 优先，模式必须逐设备核验 |
| media（未实现） | 五 USB 发布源 + 一 SE2017 RTSP 源 | 固定版本 ARM64 MediaMTX，网页分发与分段；版本/摘要待目标机验证 |
| sonar worker（未实现） | 五方向节点配置，未知地址/协议为 null | 独占声呐适配器，公平轮询；厂家协议/有效范围/超时未定 |
| timecode worker（未实现） | API 只返回 UNAVAILABLE | 独占另一个串口、零发送；映射按 boot_id/epoch/mapping_id 分段 |
| storage（未实现） | 仅报告精确挂载点是否挂载，始终 recording_enabled=false | 完成片段索引 SQLite、锁定/清理、侧录；挂载身份及可写性核验后才能录像 |

后续 worker → API 采用本机有界状态快照接口（拟用 Unix socket），只传元数据，不传媒体。协议版本 1 的快照至少含 service、instance_id、boot_id、seq（十进制字符串）、observed_monotonic_us（字符串）、observed_at_utc、state、error、payload；单条上限 64 KiB，每服务仅保留最新快照。API 按本机单调时间计算年龄，服务重启清旧快照；缺心跳标记 stale/不可用，不将最后成功状态永久保留。接入时实现来源权限、长度校验和重启/乱序测试，当前未开放该写入接口。

配置命令是后续独立接口：expected_revision + 校验后的候选配置；worker 回报 applied_revision/error。持久值、运行值、逐服务应用结果分开；失败保留旧运行配置。此轮禁止 HTTP 修改，避免在应用/回滚机制尚未实现时提供虚假成功。

记录接口后续独立于状态快照：完成片段 recording_id、媒体时间范围、本机接收时间及 TimeTag；声呐 sample_id 对应接收时刻。映射查事件时间所在的历史段，绝不直接套用最新时码。不改 PTS/DTS，不宣称曝光同步。

## 配置契约 v1

`config/system.schema.json` 使用 JSON Schema 2020-12；运行时用标准库执行该文件实际采用的子集（type、enum、properties、required、additionalProperties、$ref、pattern、长度/整数边界）。这不是通用 JSON Schema 引擎；扩展 schema 关键字必须同步校验器和测试。采用单一 system.json 聚合初期配置，是原设计多个文件的阶段性简化。

五 USB 通道键固定为 front/back/left/right/down，第六路是 sdi。所有设备绑定、模式、串口参数和声呐地址默认 null，不带虚构硬件参数。四字符格式及有理数帧率仅表达请求模式，validate 的通过仅表示结构合法（hardware_verified=false）。阶段 2 打开设备前必须将请求逐项与真实枚举结果比对，不支持则 CONFIG_ERROR。

绑定只允许 `/dev/v4l/by-id|by-path/...` 与 `/dev/serial/by-id|by-path/...`；优先唯一序列号，否则使用固定 Hub 物理端口。重复字符串绑定、当前存在且解析到相同节点的别名、声呐重复地址均拒绝。离线设备无法验证别名等价，worker 每次打开前还必须重新解析并核对两个串口确属独立适配器。探测表的 USB 属性与物理路径用于人工确认身份，不自动给左右相机分配方向。

拒绝未知字段、重复 JSON 键、非有限数、布尔冒充整数、非法端口/帧率和超过 64 KiB 的 JSON。时码 read_only 必须 true，wire_version 必须 0x12。声呐 address 是厂家定义的字符串标识，尚未规定总线地址范围或编码。配置 URL 不允许内嵌凭据或查询参数；认证方案待设备确认后采用外部凭据文件。API 仅允许 127.0.0.1；尚未实现权限模型，不提供公网/局域网直接监听选项。

## HTTP 契约 v1

成功：`{"request_id":"UUID", "data":...}`；业务错误：`{"request_id":"UUID", "error":{"code":"...","message":"...","details":[]}}`。每响应 no-store。测试和 `docs/evidence/status-example.json` 给出真实运行返回值。

| 接口 | 当前行为 |
| --- | --- |
| GET /api/v1/status | API 活性、本机时间、六路状态、五声呐、时码、存储及告警 |
| GET /api/v1/streams | 通用元数据 + streams 数组 |
| GET /api/v1/sonars | 通用元数据 + sonars 数组 |
| GET /api/v1/timecode | 通用元数据 + timecode 对象 |
| GET /api/v1/config | 已加载持久配置、内容 SHA256 revision、实际 api 运行配置、应用结果、mutable=false |
| PUT/POST/PATCH/DELETE | 405 READ_ONLY_PHASE；不修改配置或设备 |
| 未实现接口（包括 events、recordings） | 404 NOT_FOUND |

schema_version、device_id、boot_id、service_instance_id、seq、observed_at_utc 随状态返回；seq 是本服务实例内请求快照序号，结合 instance_id 区分重启。boot_id 来自 Linux，读取失败为 null；不伪造。所有 u64 微秒/序号以十进制字符串表示；未观测帧率、距离、时码、年龄用 null，声呐 valid=false。NOT_IMPLEMENTED 表示没有 worker，不推断设备物理缺失。API 的 RUNNING 不代表视频可用。尚无实时采样，故不生成虚构 age_ms。

当前服务只供本机开发验证，不提供 SSE、录像读取、维护认证、网页、配置修改或生产反向代理。stdlib HTTP 服务不作为最终网络部署方案。

## 实测环境与依赖

完整原始证据见 `evidence/host-probe.json`。当前运行主机是 Ubuntu 22.04.5 x86_64、内核 6.8.0-138-generic、Python 3.10.12，非目标 RK3588。探测时间以 JSON 内 UTC 为准。

- `/dev/video*`、`ttyUSB*`、`ttyACM*` 数量均为 0；不能冻结视频模式、设备身份或串口参数。
- GStreamer 1.20.3，v4l2src/rtspsrc 可查询；h264parse、rtspclientsink、mpph264enc、mppvideodec 查询失败；ffmpeg 未安装。当前缺 v4l2-ctl，探测工具在有设备但缺工具时会明确返回 missing_tool。
- 工程运行依赖只有 Python >=3.10 标准库；当前锁定验证解释器为 3.10.12，无 pip/npm 下载或第三方包升级。
- SDK AArch64 GCC 实测 10.3.1（20210621）；本阶段纯 Python，不调用交叉编译器。
- 旧 fusion 根文件系统包清单记录 ARM64 GStreamer 1.22.9-1、MPP 1.5.0-1、RGA 2.2.0-1、python3 包 3.10.6-1~22.04.1。它们是已有产品产物证据，不是 SS-07 板端验收或版本锁定。

probe 不连接 SE2017、不扫描网络、不打开串口、不改 UART 参数、不流式采集。V4L2 命令仅查询模式/能力；GStreamer 使用临时私有 registry。每子命令 4 秒超时、输出 64 KiB 上限，视频/串口列表各最多 64 项；失败也写入报告。命令全部失败仍可产出有效报告，调用者必须检查各项 state，不能只看 probe 退出码。

## SDK 构建结论

根 build.sh 链接到 `device/rockchip/common/scripts/build.sh`；Makefile 转发 SDK 构建目标，10-kernel/70-loader/30-rootfs/80-firmware/90-updateimg 等 build-hooks 负责组件和打包，日志通常在 output/log。当前 output/defconfig 指向 LubanCat_rk3588_ubuntu_linux6.1_xfce_defconfig，内核 lubancat_linux_rk3588_defconfig、generic DTS、extboot，同时含 RK_UBUNTU_FOCAL。

现有 fusion-platform/build-image.sh 则下载固定摘要 Ubuntu 22.04.5 ARM64 基础，通过 bwrap/fakeroot/QEMU 组装 rootfs，依赖旁边 mscam-core，并链接既有 extboot.img/uboot.img/loader。该脚本不是 SS-07 的完整源码镜像入口；不可直接运行后声称 SS-07 启动组件已重编。本轮未触发 SDK 构建，未修改这些配置/应用/产物。

本轮 `packaging/build-app.py` 只校验、测试、生成独立源码应用包与 SHA256，文件级清单记录所有打包内容摘要，输出目录必须是新目录。后续独立完整镜像入口需在选择板型、DTS、基础系统和板端 ABI 后实现；包含 repo manifest/dirty 清单、源码重编启动组件/内核/模块、rootfs 包锁定、构建日志与冷启动验收，不能直接复用旧镜像宣称完成。

## 验证与下一阶段门槛

本阶段软件验证覆盖严格配置、错误/超时/超量命令输出、真实本机 HTTP 服务、缺设备的诚实状态、只读拒绝、未知路径及慢连接隔离。测试中的显式样例只用于软件契约验证，不进入默认配置或冒充硬件结果。验证日志见 evidence/tests.txt，包解压自检见 evidence/package-smoke.txt。

进入单路 USB 视频前需要：

1. 获得目标 RK3588 的只读探测证据（后续获准在板端运行工具），核对板型/系统、GStreamer/MPP ABI、设备节点权限和当前服务占用。
2. 接一台实际 USB 相机与供电 Hub，记录唯一序列号或固定物理路径，确认 V4L2 capture 节点（排除 metadata 节点），保存格式、分辨率、帧间隔枚举及 USB 拓扑；实测带宽/供电。
3. 若存在原生 H.264，冻结单路实测模式与 profile/GOP；否则先验证板端转码资源，不启用五路软件编码回退。
4. 确认目标媒体工具/插件齐备，固定 MediaMTX ARM64 版本及 SHA256，再完成采集—本机发布—浏览器播放；验证拔插后身份不交换、真实帧年龄与失败退避。

其他待确认输入：声呐厂家/型号/协议全文、地址设置、波特率/校验/应答/量程/时序/互扰；时码发生器与独立适配器的串口参数、接线、零发送证据及接收延迟；SE2017 地址/认证/编码参数；录像盘身份/文件系统/写速。视频采集、声呐驱动、时码接收/映射、录像、网页、systemd 部署、完整 IMG 和满载验收均未接入。
