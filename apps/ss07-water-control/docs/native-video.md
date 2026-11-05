# 原生视频与媒体集成 0.3.0

2026-10-26 最新明亮场景复测：用户调整蓝口 back 的朝向后，单路 H.264 1080p25 从此前约 13.6fps 改善到 **24.977fps**，750 AU 经 libavcodec/Chromium 完整解码；未修改曝光、接线或驱动参数。当前“2＋2＋1”五路 720p25 延长检查各 1000 AU，共同出帧 **33.325 秒**，约 24.91～25.01fps，libavcodec/Chromium 各完整解码 1000 帧；先前 750 缓冲轮仅共同 22.892 秒的失败也保留。当前五路 1080p25 仍仅 front/left/back 出帧，down/right 明确报 STREAMON ENOSPC，未降低目标。

上述为有限采集与开发机离线解码：V4L2 仍有丢帧告警，OpenH264 在 1000 AU 五路检查中为 962/880/1000/1000/980 帧，不能宣称零丢帧或全客户端通过。浏览器为顺序 WebCodecs 解码，合成时间戳不代表原始帧率，也没有验证生产 WebRTC/网页。所有采集均恢复原模式，结束时五台空闲；仅新增文档、探测证据和独立输出中的配置候选，未部署或改写冻结运行文件。见[最新说明](target-probe-192.168.3.233.md#2026-10-26-明亮场景复测与五路模式对照)和[完整证据](evidence/usb-bright-five-camera-20261026.json)。

2026-10-26 后续实物更新：分到三个控制器的三路 1080p25 有限采集各 750 源缓冲/750 AU，经 libavcodec 各解码 750 帧，共同出帧 29.598 秒。五路 1080p25 的错开启动复核仍只有三路出帧，另两路 STREAMON ENOSPC；随后“2＋2＋1”的五路 720p25 对照全部启动，但一路约 12.2fps 并超时、另一路约 22.8fps，解码也有精确计数失败，不能作为五路方案通过。单路及交换插口试验确认慢帧率随其中一台相机移动，待明亮无遮挡场景复测；OpenH264 对部分实际码流的计数差异也保留。生产配置仍冻结，旧映射已失效；当前路径、所有失败及边界见[最新实物记录](target-probe-192.168.3.233.md#2026-10-26-三控制器五路对照与慢相机定位)和[三控制器证据](evidence/usb-three-controllers-20261026.json)。没有运行生产 worker 或部署应用，下文保留早先接线的阶段记录。

同日补充[实际码流回放兼容性](evidence/usb-browser-replay-20261026.json)：三段原始采集文件通过开发机 RTP/锁定 MediaMTX/Chromium WebRTC 短时解码并持续出帧，统计分别为 100/101/101 帧，本次丢包/丢帧均 0。使用独立诊断页面明确标记回放，重建的 25fps 测试时钟不代表慢相机的实际帧率；没有验证生产 API/UI、RK 实时采集或整段帧数，离线解码差异仍保留。

十个原始文件另做[完整离线解码计数](evidence/usb-fullclip-decode-20261026.json)：Chromium WebCodecs 经结束 flush 与本机 libavcodec 数量一致，九段全匹配，慢相机超时的 720p 文件仍为 537/539，无解码报错，缺少的输出对应零起算 AU 4、5。两条 API 可能共享 FFmpeg 实现，原因仍未确认；合成时间戳不代表原始采集节奏，此项也没有验证 WebRTC 或 RK 运行。交换后的 1080p 文件为 122/124 AU 并全部解码，不能以各 125 源缓冲宣称 125 解码帧。

后续[539 AU 头字段诊断](evidence/usb-h264-order-20261026.json)确认开头非 IDR `frame_num` / POC LSB 回退，解码器出现两次乱序无输出日志，与缺口索引对应；全部原始头字段与本机解码日志匹配。此项支持定位到文件内的图像顺序问题，未确定物理来源，也未解释持续低帧率；原码流、运行代码和验收条件未改动。

2026-10-26 并发更新：五台共用 `fc800000` 的 1080p25 尝试仅一路出帧，底层第二路 STREAMON 报 ENOSPC；720p25 也仅两路成功。整个 Hub 改接 Type-C 后，同控制器双路 1080p25 仍失败。按现场操作将 front 分到 `fc880000`、back 留在 Type-C `fc000000` 后，双路同时采集约 30 秒，各 749 AU 并经开发机 libavcodec 完整解码，约 25fps，无管线错误/告警，各自恢复原模式。750 源缓冲与 749 AU 的边界差异未定，不作为无丢帧结论。五路并发尚未通过；接线改变使下文旧 `fc800000` 配置映射失效，最新观测路径及失败记录见[实物说明](target-probe-192.168.3.233.md#2026-10-26-并发限制与分控制器双路复测)和[并发证据](evidence/usb-concurrency-20261026.json)。尚未运行生产 worker 或部署应用，当前包和配置示例保持冻结。

2026-10-26 实物增量：相机 Hub 重接后五路恢复枚举，既有五条 `fc800000.usb` 稳定路径重新核验一致；五路均声明 H.264 1080p25。使用板端现有 GStreamer 对 front 有限采集，复核 125 源缓冲/125 AU，并在开发机完整解码 125 帧，短时约 25fps；两轮探测后均恢复原模式。首轮精确计数失败保留，不宣称持续无丢帧。此次没有运行生产 C++ worker、安装应用或验证浏览器/五路并发/拔插；见[证据](evidence/usb-cameras-20261026.json)及[实物说明](target-probe-192.168.3.233.md#2026-10-26-hub-重接后的五相机与单路出帧)。下文旧阶段记录按原日期保留。

本文保留 0.3.0 原生视频阶段记录；网络监听和权限的后续变化见 [0.6.0 权限与维护](auth-maintenance.md)。应用 0.6.1 已新增 [ARM64 构建/ABI/QEMU 验证](arm64-build.md)，原生 0.3.1 修复多 slice IDR 的重复参数集注入。仍将媒体管理/RTSP/WHEP HTTP 绑定回环，另由明确配置的 HTTPS 入口和 ICE UDP 地址提供浏览器接入；以下历史验证结果保持原日期和范围。

2026-10-23。当前实现将视频字节留在 C++/GStreamer 与 MediaMTX 进程中，Python 只处理配置、子进程生命周期和每秒有界状态。管理服务继续使用 Python 3.8+ 标准库。网页使用浏览器原生 WebRTC API，无 npm 运行依赖。

## 数据路径与边界

`V4L2 H.264 → h264parse → AU caps → 有界 queue → rtph264pay → 本机 UDP → MediaMTX → RTSP / WebRTC`。

每方向独立 C++ 进程；Python supervisor 只接收小型 JSON 计数，不接收像素、视频帧或 RTP 包。生产程序没有编码器或测试源；只有单独编译的 `ss07-camera-test` 可以生成测试画面。SE2017 通过 MediaMTX 的 RTSP/TCP 静态源拉流，默认 URL 未配置。

板端只读检查确认现有 GStreamer 1.18.5 有 v4l2src、h264parse、rtph264pay、udpsink、queue；先前未安装 rtspclientsink，所以当前选择本机 RTP 输入。MediaMTX 固定 v1.21.2，其 amd64/arm64 压缩包摘要已对照官方 checksums.sha256，见 `packaging/dependencies.lock.json`。C++ 构建最低 GStreamer 1.18，开发机实测 1.20.3；ARM64 相机程序及实际板端运行尚未验证。

生产程序每次打开前核验稳定设备链接、V4L2 capture/streaming 能力、H.264 离散尺寸/帧间隔；实际协商 caps 必须一致。持有已核验设备 FD 后交给 v4l2src 通过 `/proc/self/fd` 打开，避免重新解析枚举链接。supervisor 每次重连检查 by-id 唯一性；全部零或重复序列号要求 by-path。真实 USB 拔插中的 FD/驱动行为仍需板端验收。

单路队列上限为 60 个 AU、8 MiB、1 秒中的先到者；不任意丢弃 H.264 参考帧，队列溢出触发该路重建。3 秒无 AU 或协商失败退出原生进程，supervisor 独立退避 1/2/4/8/16/30 秒，稳定 60 秒后回到初始退避。退避期间状态不会伪装成采集中。每个 worker 通过文件锁防止重复实例。

采集状态依据原生 AU 到达；发布状态依据 MediaMTX ready 且字节计数持续增加。SDI 状态中的 `media_activity_age_ms` 仅代表媒体入口活动，不冒充曝光/解码帧年龄。网页还检查实际解码帧数；3 秒停滞或状态断开显示遮罩并重建/关闭会话。主辅切换不重新打开相机。API 重启与采集/分发进程分离。

## 本机构建与运行

构建工具：CMake ≥3.16、支持 C++17 的编译器、pkg-config、目标 GStreamer 开发包。运行插件由上述 lock 文件列出。生产构建不需要 x264；x264 仅用于开发机集成测试。

```sh
make native
python3 packaging/fetch-media.py --arch amd64 --output /tmp/ss07-media-amd64
# ARM64 使用 --arch arm64；不将 amd64 相机程序放进 ARM64 rootfs。
./tools/ss07 media-config --config config/board-rk3588.example.json
```

上面的媒体下载仅写新目录，不安装或启动服务；可加 `--archive /path/to/downloaded.tar.gz` 离线校验。Ubuntu 开发包也可以下载并解压到独立构建目录，用 PKG_CONFIG_PATH 指向该目录；不能把开发机的库复制为板端 ABI 依据。

以下是目标环境准备并获准运行后的命令，不代表本轮已在设备上执行：

```sh
./tools/ss07 media-worker --config /etc/ss07/system.json \
  --state-dir /run/ss07 --binary /opt/ss07/current/bin/mediamtx
./tools/ss07 camera-worker --config /etc/ss07/system.json \
  --state-dir /run/ss07 --binary /opt/ss07/current/bin/ss07-camera --channel front
./tools/ss07 serve --config /etc/ss07/system.json --state-dir /run/ss07
```

另外四路以相应 channel 启动。systemd 模板为 `ss07-camera@.service`、`ss07-media.service`、`ss07-api.service`，暂存时不会启用。缺配置的通道持续报告 UNAVAILABLE，不打开设备。

API 8077、RTSP 8554、WebRTC 信令 8889、ICE UDP 8189、媒体管理 9997、本机 RTP 19000/19002/19004/19006/19008；可通过可选 media 配置改端口，冲突拒绝。所有监听均限回环。WHEP 信令由 API 的固定 `/media/{channel}/whep[/session-id]` 路由代理，不代理 MediaMTX 管理接口。网页只能访问固定通道，维护 HTTP 仍未开放。生产 LAN 入口与角色鉴权属于后续交付。

监看媒体服务始终 `record=false`，不创建录像目录。0.4.0 新增独立录像实例和数据盘身份约束，见 [录像与回放](recording.md)；缺盘或录像退出不主动停止此监看实例。

## 用户确认的配置

用户允许自行定义方向，按现有探测的 Hub 物理路径固定：

| 方向 | `platform-fc800000.usb-usb-0:` 后缀 |
| --- | --- |
| front | `1.2:1.0-video-index0` |
| back | `1.3.1:1.0-video-index0` |
| left | `1.3.2:1.0-video-index0` |
| right | `1.3.3:1.0-video-index0` |
| down | `1.3.4:1.0-video-index0` |

`config/board-rk3588.example.json` 采用以上映射、枚举支持的 H.264 1920×1080 25/1 fps 候选模式。已与 2026-10-21 保存的五台相机枚举逐项检查，结果 ENUMERATED；不等于实际出帧或带宽验证。换 Hub 口需重新绑定与安装标记。

2026-10-23 12:01（Asia/Shanghai）再次通过 SSH 检查 192.168.3.233：仍为 LubanCat-4 / Ubuntu 20.04.6 / Linux 5.10.160，但本次 video_devices=0、serial_devices=0，USB 拓扑仅见一个 480M Hub，未见五台相机。原计划的短时采集在设备存在检查处终止，没有打开相机或改模式。新证据见 [当天真实探测](evidence/rk3588-192.168.3.233-probe-20261023.json)。不能把 10 月 21 日的枚举当成目前已连接；待设备恢复后重查物理路径，禁止自动换成其他节点。

时码是 USB-RS485，用户确认波特率 500000；适配器稳定路径、数据位、校验与停止位尚未确认，仍保留 null。声呐当前没有实物，厂家协议继续缺失。数据盘按名义 1 TB 规划，目前未连接；容量不代表实际 UUID、可用空间、写速或保留时长。

## 可重现软件验证

本轮 37 个单元/接口/暂存测试在开发机 CPython 3.8.20 和 3.10.12 下通过。C++ 以 GCC 11.4.0、GStreamer 1.20.3、`-Wall -Wextra -Wpedantic -Werror` 构建通过；systemd 模板静态检查退出码为 0。最终六路媒体及 Chromium 测试通过，证据分别为 [软件验证](evidence/native-validation.json)、[媒体集成](evidence/native-media-test.json)、[浏览器](evidence/native-browser-test.json)、[暂存与退出](evidence/native-staging-test.json)。

```sh
make test
cmake -S . -B build/native -DSS07_BUILD_TEST_SOURCE=ON
cmake --build build/native -j2
python3 tools/test-media.py \
  --camera-test build/native/ss07-camera-test \
  --mediamtx /tmp/ss07-media-amd64/mediamtx \
  --output /tmp/ss07-media-test
# 有 Node 22+ 与 Chromium 时追加：
# --browser-check tools/test-browser-media.cjs --chromium /path/to/chromium
```

集成测试在临时端口启动两个真实 MediaMTX 进程和六个原生测试源：五路本机 RTP，一路经另一个媒体实例提供 RTSP 输入。验证六路 H.264 持续接收、单源停止与恢复、浏览器六路解码计数增长、主画面切换、断线遮罩与会话清理。API 硬件状态在该测试中仍为不可用，测试程序显式连接测试源，不发布伪造硬件快照。所有日志和 JSON 标记 synthetic_test、hardware_verified=false。

测试源码编译选项默认关闭，暂存/安装只接收 `ss07-camera`，不纳入 `ss07-camera-test`。当前开发机测试模式为 640×360/25fps；结果不能作为五 USB 1080p25、六路板端性能、端到端 300ms 或 8 小时验收结论。

可生成带本机原生程序的独立暂存树用于检查：

```sh
python3 packaging/stage-rootfs.py --destdir /tmp/ss07-native-rootfs \
  --native-dir build/native --media-dir /tmp/ss07-media-amd64
```

它只生成新目录，不安装到主机/板端、不启用服务、不生成完整 IMG。源码包单独包含 CMakeLists.txt、C++/Python/网页/配置、测试与依赖摘要；硬件测试与完整镜像仍为未完成交付。

实现依据：MediaMTX 官方 v1.21.2 配置与 API、官方 RTP/GStreamer/WebRTC 文档；GStreamer 官方 pad probe、v4l2src、h264parse、rtph264pay 与 queue 文档。依赖版本和资产摘要锁在工程内，不在运行时追踪 latest。
