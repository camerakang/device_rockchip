# Mobocon 接入 SS-07 视频声呐节点：开发与测试文档（RK 端 0.10.10）

本文写给修改 Mobocon 的开发者（含 AI 助手）。目标是让 Mobocon 不填 IP、不持有密钥，就能在设备列表里发现 SS-07 的 RK3588（类型 `VIDEO_SONAR 0x9009`），并显示六路画面和状态。文中每条测试都给出操作、预期结果和判定方法，预期值来自 RK 端在 192.168.3.233 上的实测（0.10.9 起，0.10.10 补充了 SDI 状态和会话查询）。

协议契约以协议库文档为准：`device_protocol` 仓库的 `docs/ss07/video-sonar-node.md` 和黄金向量 `docs/ss07/ss07_node_vectors.json`（本工程 vendor 副本在 `lib/mobocom_protocol/docs/ss07/`）。本文与契约冲突时以契约为准，并通知 RK 端修正本文。

## 1 范围

Mobocon 需要实现的内容：

1. 组播 DISCOVER 发现 `type=0x9009` 的节点，以 UID 为键记录当前 IP。
2. 对每个节点发 INFO、CAPS、STATUS 做兼容性核对。
3. 用发现到的 IP 取 `/api/v1/streams`、`/api/v1/status`、`/api/v1/sonars`，显示六路状态。
4. 用 WHEP（WebRTC）免密钥播放六路画面；需要转存或交给其他程序时用 RTSP。
5. 全局急停时显示“已急停”，解除急停时随其他设备一起发单播 RESET_FAULT。
6. RK 重启或换 IP 后自动重新发现、重建拉流。

Mobocon 对 VIDEO_SONAR 节点只允许发送：DISCOVER、INFO、CAPS、STATUS、全局 STOP/ESTOP、单播 RESET_FAULT。禁止发送 SESSION_OPEN、SET_SOURCE、WRITE_TARGETS、HEARTBEAT、WRITE_CONFIG，界面上也不能出现“接管、使能、运动”类控件。RK 不接收也不转发任何云台、跟焦命令。

## 2 测试环境

| 项目 | 值 |
| --- | --- |
| RK 板 | LubanCat-4（RK3588），192.168.3.233，软件 0.10.10 |
| 节点 UID | `8455818509601193611`（`0x75591a2235f3be8b`），固定不变 |
| firmware | `0x000a000a`（即 0.10.10；0.10.9 为 `0x000a0009`） |
| Control | UDP 47821，组播 239.255.33.21，TTL=1，Mobocom V2.2（INFO 中的 wire_version 为 `0x22`，即十进制 34） |
| HTTPS API / WHEP 信令 | `https://192.168.3.233:8443`，自签名证书 |
| WebRTC 媒体 | UDP 8189 |
| RTSP（明文） | `rtsp://192.168.3.233:8555/<通道>`，仅 TCP |
| RTSPS | `rtsps://192.168.3.233:8322/<通道>` |
| 通道 | `front`、`back`、`left`、`right`、`down`、`sdi` |
| 视频 | 五路 USB 为 H.264 1280×720 约 25 fps；`sdi` 来自 SE2017 编码器，1920×1080 |
| 免密钥白名单 | `192.168.3.0/24`（`/etc/ss07-gateway/lan-monitor.json`） |
| 同网段其他节点 | 192.168.3.78 上有一台 `0x9006` 设备也会回应 DISCOVER |

Mobocon 电脑的要求：

- 有线接入同一台交换机，地址在 192.168.3.0/24，和 RK 在同一个二层网段。组播 TTL=1，不能经过路由器或 NAT。经 NAT 访问时，免密钥白名单看到的是 NAT 地址，测试结果无效。
- Windows 防火墙放行入站 UDP 47821（含组播）和 UDP 8189，以及出站 TCP 8443、8555、8322。
- 多网卡电脑要让用户选择控制网卡，组播从该网卡发出（`IP_MULTICAST_IF`）。
- 交换机开了 IGMP snooping 但没有查询器时，组播可能被丢弃，需要开启 IGMP 查询器或关闭 snooping。

## 3 实现要点

### 3.1 协议编解码

使用协议库的 SDK 编解码（Rust `mobocom-protocol` crate、C++ SDK，或测试用的 Python `mobocom_realtime/wire.py`），不要手写报文。下面的偏移只用于核对：

| 报文 | 字段 |
| --- | --- |
| DISCOVER 回复 | payload 26 字节；开头按小端 `<H I Q>` 依次为 device_type、firmware、boot；UID 在帧头 `src_uid` |
| INFO 回复 | kind=RESPONSE，payload 33 字节，含 device_type `0x9009` |
| CAPS | 请求 payload 为 u16 页号；页 0 和页 1 都应有 RESPONSE |
| STATUS | 请求 payload `00 00`；回复按小端 `<Q I I B B H>` 依次为 boot、generation、version、state、source、fault；偏移 52 为 safe_confirmed，偏移 56 起 u64 为 epoch |
| 状态值 | state：0=SAFE，4=FAULT；fault：0=无，7=ESTOP |
| RESET_FAULT | 单播，payload 为 `guard(boot, generation, epoch)`（取自最近一次 STATUS），回复 kind=ACK |

UID 是 64 位无符号整数。在 JSON、日志、界面和 JavaScript 中一律用十进制字符串或 BigInt 保存，不能转成 JavaScript number，否则会丢精度（`8455818509601193611` 超过 2^53）。

### 3.2 发现与身份

1. 向 239.255.33.21:47821 发 DISCOVER（`dst_uid=0`），收集 500 ms 内的所有回复。
2. 只保留 device_type 为 `0x9009` 的回复。以帧头 UID 为键，回复的源 IP 作为该设备当前 IP，源端口应为 47821。
3. 对每个新设备单播 INFO、CAPS（页 0、1）、STATUS。type、profile_version=1、wire_version=`0x22`（十进制 34，不是 22）任一不符，标记为“不兼容”，不取流。
4. 定期重发 DISCOVER（建议 2–5 s）。boot 变化表示 RK 节点重启，IP 变化表示换址；两种情况都丢弃旧的 WHEP 会话，重新取 `/streams` 并重建拉流。

### 3.3 状态

节点常态是 `state=SAFE, source=NONE, fault=0, safe_confirmed=1`。相机掉线、声呐离线、磁盘告警等业务异常不会进入 Control 的 fault，而是通过 HTTP 接口给出。

`GET /api/v1/streams` 的 `data.streams` 是六个对象，实际字段如下（`front` 实测）：

```json
{"channel_id":"front","capture_state":"STREAMING","publish_state":"PUBLISHING","availability":"available",
 "format":"H264","width":1280,"height":720,"fps_observed":24.94,"bitrate_observed":27776.8,
 "last_frame_age_ms":403,"restart_count":0,"live_url":"/media/front/whep",
 "native_url":"rtsps://192.168.3.233:8322/front","rtsp_url":"rtsp://192.168.3.233:8555/front","error":null}
```

以 `availability` 判断能否播放，`capture_state`、`publish_state` 和 `error` 用于提示原因。`rtsp_url` 和 `native_url` 不含用户名和密钥。声呐厂家协议尚未接入，`/api/v1/sonars` 没有测量值，界面显示“无数据”，不要用模拟值填充。

`sdi` 路另有三个字段：`input_signal` 为编码器报告的 SDI 输入状态（`present` 有信号、`no_signal` 无信号、`unknown` 读不到编码器状态），`input_info` 为编码器原文（如 `SDI NoSignal N/A`），`input_error` 为读取失败原因。SDI 无信号时，SE2017 仍会输出一路全黑的 1920×1080 码流，因此这时 `availability=no_signal`、`capture_state=NO_SIGNAL`、`error=sdi_no_signal`，并且不提供 `live_url`、`rtsp_url`、`native_url`；`/api/v1/status` 的告警里会有 `SDI_NO_SIGNAL`。`input_signal=unknown` 时仍按码流是否到达判断 `available`。`sdi` 的 `width`、`height` 取自转发码流，`fps_observed` 为 RK 实测的转发帧率，`bitrate_observed` 为接收码率（bit/s）。

`availability` 的取值：`available` 可播放；`no_signal` 码流在但没有输入信号（目前只有 `sdi`）；`unavailable` 无码流或设备异常。

`GET /api/v1/media/sessions` 返回本机（按来源 IP；带密钥时按客户端）当前持有的 WHEP 会话和全局计数，不需要板子登录权限：

```json
{"data":{"sessions":[{"channel_id":"front","location":"/media/front/whep/<id>"}],
 "count":1,"limit_per_client":12,"total":3,"limit_total":48}}
```

`total` 含其他客户端和网页的会话，但不列出它们的地址。会话结束后最多约 1 秒从列表中消失。

所有响应为 `{"data": ...}`，错误为 `{"error": {"code", "message", "details"}}`。

### 3.4 拉流

WHEP 流程：

1. `POST https://<ip>:8443/media/<通道>/whep`，`Content-Type: application/sdp`，请求体为 SDP offer（recvonly 视频），不带 Authorization 和 Cookie。
2. 返回 201，响应体是 SDP answer，`Location` 是会话地址。
3. 停止播放、切换设备、检测到 boot 或 IP 变化时，`DELETE <Location>`。只有创建会话的同一来源 IP 能关闭它。

API 不返回 CORS 头。Electron 中由主进程（Node）发送 WHEP 请求，经 IPC 把 SDP 交给渲染进程的 `RTCPeerConnection`；Rust 或其他原生后端同理，由后端代理信令。自签名证书通过信任网关证书解决（如 Node 的 `NODE_EXTRA_CA_CERTS`），不要在产品里关闭证书校验。示例代码见 [mobocon-video-api.md](mobocon-video-api.md)。

RTSP 只支持 TCP 传输，每路最多 4 个外部读者。白名单内不带用户名即可读取。

### 3.5 错误处理

| 结果 | 含义 | Mobocon 处理 |
| --- | --- | --- |
| 401 | 来源不在白名单，或 RK 未开启免密钥模式 | 提示“RK 未允许本机免密钥访问”，显示本机 IP |
| 403 `LAN_MONITOR_FORBIDDEN` | 免密钥模式不允许的接口（配置、录像、日志等） | Mobocon 不应调用这些接口；出现即为客户端缺陷 |
| 404 | 关闭了不属于本机的 WHEP 会话 | 忽略，清理本地记录 |
| 503 `MEDIA_SESSION_LIMIT` | 本机 WHEP 会话已达 12 个，或全局 48 个 | 检查是否有会话泄漏，先 DELETE 旧会话 |
| 网络超时 | RK 不可达或正在重启 | 退避重试，继续 DISCOVER |

## 4 测试用例

每个用例记录：Mobocon 版本、电脑 IP 与系统、时间、结果（通过/失败）、证据（日志、截图或抓包文件）。需要 RK 端配合的步骤标为“RK 端操作”，由 RK 维护者执行，Mobocon 开发者不需要板子的登录权限。

### A 发现与核对

| 编号 | 操作 | 预期 |
| --- | --- | --- |
| A1 | 启动 Mobocon，选择 192.168.3.0/24 的网卡，执行发现 | 100 ms 内收到 192.168.3.233 的回复；设备列表出现 UID `8455818509601193611`、类型“视频声呐系统（0x9009）”、固件 0.10.10 |
| A2 | 同上，观察其他设备 | 192.168.3.78 的 0x9006 设备按其自身类型处理，不被当成 SS-07 |
| A3 | 连续发现 100 次，间隔 1 s | 成功率 100%，记录回复时间的最大值和平均值 |
| A4 | 选择 INFO/CAPS/STATUS 核对 | INFO 33 字节含 0x9009；CAPS 页 0、1 均有回复；STATUS 为 SAFE、fault=0、safe_confirmed=1；设备标为“兼容” |
| A5 | 查看 UID 的保存与显示 | 配置文件、日志、界面中的 UID 都是 `8455818509601193611`，末位没有被四舍五入 |
| A6 | 断开 Mobocon 网线 10 s 再接回 | 设备先显示离线，恢复后自动重新出现，UID 不变，无需手工操作 |

### B 状态与画面

| 编号 | 操作 | 预期 |
| --- | --- | --- |
| B1 | 打开 SS-07 设备页 | 不出现 IP 或密钥输入框；显示 UID、IP、固件、免密钥可用 |
| B2 | 读取六路状态 | 五路 USB `availability=available`，1280×720，约 25 fps。sdi 宽高为 1920×1080、帧率约 60；当前现场没有接 SDI 摄像机，应为 `availability=no_signal`、`input_signal=no_signal`，Mobocon 显示“SDI 无信号”，不播放黑画面。接上 SDI 摄像机后应变为 `available` |
| B3 | 同时播放所有 `availability=available` 的通道（现场无 SDI 摄像机时为五路 USB） | 均有画面，持续 10 分钟无卡死；记录 Mobocon 的 CPU 和内存 |
| B4 | 测量延迟：在 front 相机前放一个毫秒计时器，同时拍下计时器和 Mobocon 画面 | 记录差值；参考值 WebRTC 100–200 ms，以实测为准 |
| B5 | 逐路停止播放 | 每路都发送了 `DELETE <Location>` 并得到 200；关闭 Mobocon 后，`GET /api/v1/media/sessions` 的 `count` 在播放时等于打开的路数，全部关闭后 1 秒内回到 0 |
| B6 | 反复开关同一路 20 次 | 不出现 503；结束后 `/api/v1/media/sessions` 的 `count` 为 0 |
| B7 | 用 RTSP 拉一路：`ffplay -rtsp_transport tcp rtsp://192.168.3.233:8555/front` | 不带用户名即可播放 |
| B8 | 查看声呐区域 | 显示“无数据”或未接入，不出现假数值 |
| B9 | 抓包检查 Mobocon 发往 8443 的请求 | 不含 Authorization、Cookie；不访问 `/api/v1/config`、`/recordings`、`/logs` |

### C 急停与安全

| 编号 | 操作 | 预期 |
| --- | --- | --- |
| C1 | Mobocon 执行全局 STOP | SS-07 无回复（组播 STOP 规定无回复），状态仍为 SAFE；画面不中断 |
| C2 | Mobocon 执行全局 ESTOP | SS-07 显示“已急停”（STATUS 为 FAULT、fault=7）；六路画面继续播放 |
| C3 | 解除急停 | Mobocon 用最新 STATUS 的 boot/generation/epoch 发单播 RESET_FAULT，收到 ACK；状态回到 SAFE，generation 加 1 |
| C4 | 检查 SS-07 设备页的控件 | 没有接管、使能、运动或参数写入控件 |
| C5 | 抓包检查 Mobocon 发往 47821 的报文 | 只有 DISCOVER、INFO、CAPS、STATUS、STOP、ESTOP、RESET_FAULT |

### D 重启、换址与异常

| 编号 | 操作 | 预期 |
| --- | --- | --- |
| D1 | RK 端操作：`sudo systemctl restart ss07-mobocom-node` | Mobocon 在下一轮发现中看到 boot 变化，提示设备重启；UID 不变；画面基本不受影响（媒体服务没有重启），如 Mobocon 按规则重建拉流，应在 5 s 内恢复 |
| D2 | RK 端操作：`sudo systemctl restart ss07-media` | 节点 boot 不变，设备保持在线；画面中断后 Mobocon 自动重连，30 s 内各路回到重启前的状态 |
| D3 | RK 端操作：重启整板 | Mobocon 显示离线；RK 启动后自动重新发现，boot 变化，各路自动回到重启前的状态，无需手工操作 |
| D4 | RK 端操作：改用另一个 IP（如 DHCP 或临时改静态地址），测试后恢复 192.168.3.233 | Mobocon 通过 DISCOVER 得到新 IP，按 UID 识别为同一设备，用新 IP 重建拉流 |
| D5 | RK 端操作：把 `lan-monitor.json` 的白名单改为不含 Mobocon 电脑的地址，测试后恢复 | 约 1 s 内 Mobocon 的 HTTP 请求得到 401，界面显示明确提示；已开的画面被断开；恢复后 Mobocon 自动重新拉流 |
| D6 | 拔掉一路 USB 相机后再插回（RK 端或现场操作） | 该路 `availability` 变为不可用并显示原因；其他五路不受影响；插回后该路自动恢复。Control 状态始终为 SAFE |

### E 交叉核对（可选）

Mobocon 结果有疑问时，在同一台电脑上运行本工程的协议检查脚本，确认问题在 Mobocon 还是 RK 端：

```sh
python3 tools/test-mobocom-node.py --target 192.168.3.233 --interface-ip <本机192.168.3.x地址>
```

脚本使用协议库的 Python 编解码，检查发现、INFO、CAPS、STATUS、STOP、ESTOP、RESET_FAULT 和异常帧，全部通过时输出 11 项 `"ok": true`。脚本需要本工程目录（含 `lib/mobocom_protocol`）。只想跳过急停检查时加 `--skip-estop`。

## 5 RK 端已验证与待验证

RK 端在 2026-11-06 已验证：黄金向量 5/5 逐字节一致；板上组播发现 0.2–0.9 ms；协议检查 11/11；节点与媒体服务互相重启互不影响；白名单来源免密钥六路 WHEP 和 RTSP 可用、受限接口 403、非白名单 401；同一来源第 13 个 WHEP 会话 503。详细记录见 [mobocon-network-node.md](mobocon-network-node.md) 第 10 节。

以下项目要靠 Mobocon 现场测试补齐，对应用例：Windows 主机组播发现（A1、A3）、DHCP 实际换址（D4）、六路满载下的发现成功率与延迟（A3 与 B3 同时进行）、端到端播放延迟（B4）。

## 6 反馈格式

发现问题时，请向 RK 端提供：用例编号、Mobocon 版本、电脑 IP、发生时间（精确到秒，北京时间）、Mobocon 日志片段、相关抓包（UDP 47821 或 TCP 8443），以及 HTTP 状态码和响应体中的 `error.code`。RK 端会据此对照 `journalctl -u ss07-mobocom-node -u ss07-api -u ss07-media` 的同一时段日志。
