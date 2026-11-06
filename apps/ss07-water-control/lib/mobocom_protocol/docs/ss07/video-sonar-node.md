# SS-07 视频声呐节点（VIDEO_SONAR 0x9009）接入契约

修订：2026年11月5日。本文定义 SS-07 的 RK3588 视频/声呐系统作为 Mobocom V2.2/0x22 UDP 网络节点的身份、报文行为和视频取流约定。它取代 `mobocom-integration.md` 与 `architecture.md` 旧文字中“RK 不属于 Control”的表述；其余云台接收器、时码设计不变。

**状态：2026-11-06 起，RK3588 端已实现并部署（固件 0.10.10），Mobocon 端已接入。** 已实测发现与核对、免密钥取流、五路出画、sdi 无信号判断、会话计数、单播急停与复位；尚未验证 SDI 接入后的恢复、DHCP 换址、多台同网和满载时延。完整连接、启动时序与排障见 [SS-07 与 MoBoCon 全链路连接与运行](mobocon-runtime.md)。

## 1 目标与边界

Mobocon 与所有新设备统一使用网络协议。SS-07 的 RK3588 以 UID 出现在 Mobocon 设备列表，Mobocon 通过组播 DISCOVER 学到它的当前 IP，不再要求用户手填 IP，也不再使用 Bearer 密钥取流。

Control 只承担身份、在线发现、版本核对和安全待机语义。视频、声呐读数、录像、配置仍走 RK 现有的 HTTPS/WebRTC/RTSP 接口，不把媒体或配置塞进 0x22。RK 不接收运动命令，也不转发云台、跟焦命令。时码仍是独立的 0x12，与本文无关。

## 2 类型登记

| 项目 | 值 |
| --- | --- |
| device_type | `0x9009`，符号 `VIDEO_SONAR`，名称“视频声呐系统”，正式类型 |
| 登记 | `mobocom_realtime/device_types.json` revision 3，已生成 C++/Python/Rust/TS 常量和黄金向量 |
| profile_version | 1 |
| Control | 0x22 UDP，端口 47821，组播 239.255.33.21，TTL=1 |

登记只确认类别身份，不代表任何能力。已有值不得改名或复用，0x900a 起为未登记空位。

## 3 节点身份

UID：64 位非零整数，出厂后固定。建议首次启动按 `/etc/machine-id` 派生并写入 `/var/lib/ss07/mobocom-node.json`，之后只读该文件；文件丢失才重新生成，并在日志中告警（UID 变化会让 Mobocon 视为新设备）。不得使用 0 或 `0xFFFFFFFFFFFFFFFF`。JSON、日志、API 中的 UID 一律用十进制字符串，禁止转 JavaScript number。

boot_id：每次 `ss07-mobocom-node` 服务启动生成新的非零随机 u64，进程存活期间不变。Mobocon 凭 boot 变化判断设备重启。

firmware：u32，建议 `主版本<<24 | 次版本<<16 | 修订号`，与 `/opt/ss07/current` 发布版本一致，非零。

## 4 Profile

公共核心要求至少一个资源、来源掩码非零，没有“纯监视节点”的特例。SS-07 采用最小占位 Profile，不声明任何运动能力：

| 字段 | 值 | 说明 |
| --- | --- | --- |
| source_mask | `0x04`（仅 MOBOCOM） | 不支持 REMOTE |
| 资源 | 1 项：endpoint=1, resource=1, unit=`0x9009`, flags=0, min=0, max=0, group=0, vmax=0, amax=0 | 占位，非持续输出，无反馈位 |
| 动作 | 0 项 | |
| max_target_hz / update_gap_us | 100 / 10000 | 沿用默认，不会被使用 |
| target_min/max_us | 20000 / 1000000 | |
| lease_min/max_us | 50000 / 3000000 | |
| safe_timeout_us / switch_timeout_us | 100000 / 100000 | |
| auto_reset_mask | 0 | |

实现语言：使用协议库的 Rust SDK（`mobocom_realtime/rust`，crate `mobocom-protocol`）或 C++ SDK。Python `reference.py` 是对照模型，只用于生成向量与测试，不作为板端实现。

驱动 Hook（Rust `Driver` trait）约定：`validate_targets` 恒返回 false；`apply_targets` 恒返回 false（不会被调用）；`start_safe` 立即返回 true；`safe_confirmed` 恒 true（没有任何输出需要停）；`reset_allowed` 恒 true；`flush_remote` 空操作。动作、反馈、配置相关方法保持 trait 默认实现（拒绝/None/UNSUPPORTED）。

Mobocon 对 VIDEO_SONAR 只使用：DISCOVER、INFO、CAPS、STATUS，全局 STOP/ESTOP，以及急停后的 RESET_FAULT。禁止 SESSION_OPEN、SET_SOURCE、WRITE_TARGETS、HEARTBEAT、WRITE_CONFIG。核心在无会话时会拒绝写目标（参考核实测返回 SESSION=21），但仍需客户端约束；Mobocon 不得显示或提供“接管/使能/运动”界面。

## 5 状态语义

节点常态为 `state=SAFE, source=NONE, fault=0, safe_confirmed=1`。适配器不得主动制造 FAULT：相机掉线、声呐离线、磁盘告警等业务异常不进入 Control 的 fault 码，一律经 HTTP `/api/v1/status` 和 `/api/v1/streams` 呈现。

STOP（单播回 ACK，组播无回复）：状态保持 SAFE/NONE。

ESTOP（单播或组播）：公共核心按规范锁存 `state=FAULT, fault=7(ESTOP)`，这是协议行为，不得在适配器里屏蔽。由于 `safe_confirmed`、`reset_allowed` 恒为 true，单播 RESET_FAULT（携带当前 guard）立即回到 SAFE/fault=0，generation 加 1。Mobocon 全局急停后应把 SS-07 显示为“已急停”，并在用户解除急停时随其他设备一起发 RESET_FAULT。

无论 STOP、ESTOP 还是 FAULT，都不得停止视频、录像、声呐轮询或重启任何服务；Control 状态只表示协议层，与画面是否可用无关。FAULT 期间视频照常可取。

Mobocon 不会发送 SESSION_OPEN；若其他主机误发，按核心规则处理即可，任何 Control 命令都不得影响媒体服务。

## 6 发现与重连

适配器行为（`ss07-mobocom-node`）：

1. 绑定 UDP 0.0.0.0:47821，对控制网卡加入 239.255.33.21（Rust `UdpTransport::device(接口IPv4)` 即此行为，TTL=1、非阻塞）。接口 IP 变化（DHCP 续租、换址）后重新创建 socket 并加入。
2. 每个数据报原样交给核心；帧校验、`dst_uid` 过滤和组播命令限制由核心完成，适配器不重复解析。接收缓冲用 `MAX_FRAME+1`（280B），超长数据报被截断后由核心判为非法。
3. 调用 `receive(raw, now)` 后立即 `take_reply(now)`，回复单播到本次请求的源 IP:port，不绑定到后续请求。
4. `now` 使用单调时钟（微秒）。同时周期调用 `tick(now)`（10 ms 量级）。
5. DISCOVER 在任何状态都回 26B 应答；回复内含 type/firmware/boot/epoch/generation，UID 在帧头。

Mobocon 用 UID 记设备，用 DISCOVER 回复的源 endpoint 记当前 IP；视频主机使用同一 IP（见第 7 节）。换 IP 或 RK 重启后，Mobocon 重新 DISCOVER，boot 变化即视为重启，随后重取流描述并重建 WebRTC 会话；由于 SS-07 无运动状态，重连不需要“重新授权”，但需要重新拉流。

IP 推荐 DHCP 或固定地址，均不影响协议。可选发布 mDNS `_mobocom._udp.local.`（SRV 47821，hostname `mobocom-<UID十六进制>`，instance 如 `SS07-<短UID>`），仅用于显示名；没有 mDNS 也能工作。

## 7 视频与声呐取流约定（免密钥）

视频主机地址取自 DISCOVER 回复的源 IP，不在协议里重复携带。固定端口和路径如下：

| 用途 | 地址 |
| --- | --- |
| 状态/流描述 | `GET https://<ip>:8443/api/v1/streams`、`/api/v1/status`、`/api/v1/sonars`、`/api/v1/timecode` |
| WebRTC 信令 | `POST https://<ip>:8443/media/<path>/whep`，`DELETE <Location>` |
| WebRTC 媒体 | UDP 8189 |
| RTSP（明文，局域网） | `rtsp://<ip>:8555/<path>` |
| RTSPS | `rtsps://<ip>:8322/<path>` |
| 路径 | `front`、`back`、`left`、`right`、`down`、`sdi` |

这些端口和路径沿用 `apps/ss07-water-control/docs/mobocon-video-api.md` 的现有约定，不新增。

**免密钥模式（lan-monitor，本次新增，默认关闭）**：现状要求 `Authorization: Bearer` 或 RTSP 用户名密钥。为让 Mobocon 不持有密钥，RK 增加一个只读局域网监看模式：

| 项目 | 要求 |
| --- | --- |
| 配置 | `/etc/ss07-gateway/lan-monitor.json`：`{"schema_version":1,"enabled":true,"allow_cidrs":["192.168.3.0/24"]}`；缺失或 `enabled=false` 即关闭 |
| 生效条件 | 请求源 IP 落在 `allow_cidrs`，且未带 Authorization/RTSP 凭据；带有效 Bearer 的请求仍按原角色处理 |
| 授予权限 | 仅 `monitor` 的只读子集：`GET /api/v1/status|streams|sonars|timecode|events`、WHEP 创建/删除、RTSP/RTSPS 读 |
| 绝不授予 | 录像读取、配置读写与 plan/apply/recover、日志、API 客户端管理 |
| 会话限制 | 每个来源 IP 最多 12 个 WebRTC 会话（与现有客户端上限一致），总数仍受 48 限制 |
| 默认源范围 | 不得默认 `0.0.0.0/0`；`allow_cidrs` 为空视为关闭 |
| 审计 | 首次启用、拒绝来源（非白名单）与会话建立写日志，不含凭据 |

该模式依赖局域网可信，与协议“Control 使用可信局域网”一致。它不是认证，网络内任何白名单主机都能看画面；需要更强保护时关闭该模式，回到 Bearer。

声呐读数仍通过 `GET /api/v1/sonars` 取得；当前厂家协议未接入时没有测量值，Mobocon 只显示“无数据”。

## 8 Mobocon 客户端职责

1. 设备发现：向 239.255.33.21:47821 发 DISCOVER，收集 `type=0x9009` 的回复，以 UID 为键保存 IP:port；多网卡时由用户选择控制网卡。
2. 核对：对每个候选发 INFO、CAPS、STATUS，核对 type、profile_version=1、wire 版本字节 `0x22`（十进制 34，不是 22）；不一致则标记不兼容，不取流。
3. 取流：用该 IP 调 `/api/v1/streams`（2 s 轮询或 SSE），以各路 `availability` 为拉流判据（只有 `available` 才拉流，缺字段的旧固件按 `available` 处理），`state` 类字段只用于显示；WebRTC 的 WHEP 信令经 Rust 侧代理发送（自签证书不进入网页）。
4. 重连：发现 boot 或 IP 变化后，丢弃旧 WHEP 会话并重建。窗口或进程被直接关闭时也必须回收会话，否则板端约 20 s 后才释放配额。
5. 配置界面：不再有 API 密钥输入；显示 UID、IP、固件、lan-monitor 是否可用（401/403 时给出明确提示）。
6. 不得对 VIDEO_SONAR 发运动类命令。

## 9 黄金向量

[ss07_node_vectors.json](ss07_node_vectors.json) 由 Python 对照模型以上述 Profile 生成，UID `0x9009000000000001`、boot `0x1122334455667788`、firmware `0x00010000`，覆盖组播 DISCOVER（26B）、INFO（33B）、CAPS 页 0/1、STATUS 页 0，now 从 1000000µs 起每条 +1000，sequence 从 1 递增。已用 Rust `DeviceCore` 加上述 Driver 逐条重放，5/5 逐字节一致。RK 实现须用同样参数复现，作为实现正确的最低证据；向量不代表真实设备验收。

## 10 验收

| 项 | 条件 |
| --- | --- |
| 向量 | RK 适配器对全部向量逐字节一致 |
| 发现 | Windows 主机组播 DISCOVER 100 ms 内收到回复，UID/类型/固件正确；DHCP 换址后再次发现得到新 IP |
| 重启 | 重启 `ss07-mobocom-node`，boot 变化，视频/录像/声呐不中断；重启视频服务，Control 不中断 |
| 隔离 | 组播 STOP/ESTOP、错误帧、超长帧、CRC 错误、重放帧不影响媒体服务，进程不崩溃 |
| 免密钥 | 白名单源无密钥可取六路 WHEP/RTSP；非白名单源仍 401；配置/录像接口在无密钥时 401/403 |
| 回滚 | 删除 lan-monitor.json 与停用节点服务即回到原状态，原 Bearer 流程可用 |
| 负载 | 六路视频满载下，DISCOVER 成功率与回复延迟达标，视频无新增丢帧 |

以上均须在板上实测；协议库软件测试不能代替。
