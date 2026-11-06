# Mobocom 纯 Rust SDK

`mobocom-protocol 2.2.0-network.1`，Control `0x22`，独立主机时码 `0x12`。**实施候选，不是硬件验收通过或 Mobocon 应用接入完成。** 与现有 C++/Python/TypeScript 共用[规范](../PROTOCOL.md)、[类型注册表](../DEVICE_TYPES.md)和原始黄金向量，不依赖 C++、FFI、第三方 crate 或异步运行时。

## 构建与引用

```toml
[dependencies]
mobocom-protocol = { path = "../device_protocol/mobocom_realtime/rust" }
# 无标准库设备：default-features = false
```

使用当前 stable Rust / Cargo。测试不要求联网下载 crate；本次使用 rustc 1.99.1 验证，尚未声明更早版本的 MSRV。

```sh
cargo test --manifest-path mobocom_realtime/rust/Cargo.toml --locked --offline --all-targets
cargo check --manifest-path mobocom_realtime/rust/Cargo.toml --no-default-features
cargo clippy --manifest-path mobocom_realtime/rust/Cargo.toml --all-targets -- -D warnings
python3 tools/test_rust_sdk.py
python3 tools/test_isolated_consumers.py --rust-only
```

`--rust-only` 明确只验证 Rust 隔离消费、完整清单与篡改拒绝，不代替默认四语言整体验收。嵌入式编译检查可运行 `cargo check --manifest-path mobocom_realtime/rust/Cargo.toml --no-default-features --target thumbv7em-none-eabihf`（需先安装对应 Rust target）。

从仓库根目录执行。最后一项读取已有 JSON 向量，重放设备/配置流程及确定性 Python/Rust 差分轨迹，不重写期望。差分允许 STATUS 的提示性 `state_version` 不同：既有 C++ 与 Python 对 SAFE 下无操作 STOP 的版本计数存在差异；其他协议字段仍逐字节比较。

## 模块与边界

| 模块 | 内容 |
| --- | --- |
| `wire` | 小端字段、CRC、严格单数据报、固定容量 `Payload/Datagram`、Guard/Target |
| `device_types` | 从唯一 JSON 注册表生成的 DeviceType、用途判断；不按整段数值范围放行 |
| `model` | Profile、资源/动作、INFO/DISCOVER/ACK/STATUS、严格分页 CAPS 收集器 |
| `device` | DeviceCore、Driver、LocalControlInput、会话/安全/配置去重、来源/TTL/租约/动作/故障 |
| `configuration` | schema 1～4、RAM 调速、跟焦来源/位置/按钮及快照关联 |
| `client` | 单 UID 串行、多 UID 并发、原帧重试、独立发现、只读身份探测、重新授权 |
| `scheduler` | 完整资源快照、100 Mbps 预算、显式 tick、终目标交付与安全停止 |
| `udp` | 可选 `std` 非阻塞 IPv4 UDP 适配器，TTL=1；没有后台线程 |
| `timecode` | 独立 0x12 Packet/HostTimeline；失时本场续播、下场重新对齐 |

除 `udp` 外均可 `no_std`，不使用 `alloc` 或 `unsafe`。设备最多 16 资源、16 动作、8 条动作结果、一个待取回复；设备核心借用 Profile 资源/动作切片并拥有 Driver。客户端/调度器使用 const 泛型容量，例如 `Client::<32>`，不是协议节点数限制。每 UID 一个在途事务、一个完成结果槽及一个最新待发目标批次；超过容量明确报错，调用者应及时消费 ticket 结果，结果被后续事务替代时返回 Superseded。大容量对象应按目标平台预算栈/静态存储，不把桌面测试内存占用当作 MCU 保证。

## 设备接入

先完成真实硬件安全初始化，再以真实类别契约构造 `Profile` 和 `DeviceCore::new(profile, uid, fresh_boot_id, driver)`。构造失败不能启用输出。`Profile` 无通用安全默认值；虚拟驱动只位于 `tests/common`，不可用于实体设备。

Driver 必须实现 `validate_targets/apply_targets/start_safe/safe_confirmed/reset_allowed/flush_remote`；动作与配置按需实现。回调有界、非阻塞，实测反馈才返回 `Some(value)`。配置适配器独立维护 revision、私有准入及异步保存状态；schema 2/3/4 成功必须同步 Applied，schema 1 可 Pending。公共核心不实现 Flash、PDMOVIE、CAN、GPIO、机械单位或限速。

每笔网络输入：

1. 保存本数据报的源 endpoint，调用 `node.receive(datagram, monotonic_us)`。
2. 立即 `node.take_reply(monotonic_us)` 并单播回该源；不能等下一数据报再绑定目的地址。
3. 无网络输入时也要独立周期执行 `node.tick(monotonic_us)`，检查 TTL、租约、动作和输出 gap。

`receive` 不交付目标；`tick` 才批量交付最新值。回复最多保留 20 ms，新 receive 清除旧回复。LocalControlInput 使用解码时捕获的 epoch/generation/sequence，不能在出队时换为新凭据。可选 `recover_timeout_fault` 只能清掩码允许、已安全确认的来源/目标超时，不恢复输出。

## 客户端：显式轮询，不是 TS Promise 的逐行替代

```rust
use mobocom_protocol::client::Client;
let mut client = Client::<32>::new(fresh_session_id, 30)?;
client.scan()?; // 一轮 100ms；需要两轮时由应用显式重复
// 所有网络输入均 client.feed(datagram, source_endpoint, monotonic_us)
// 周期、有界地取 client.next_transmission(monotonic_us)，立即发送；失败 send_failed(sequence)
// 可选 std: udp.pump(&mut client, monotonic_us, max_datagrams)
```

`fresh_session_id` 由主机 OS 熵源生成非零 u64，不使用时间戳假装安全随机数。`next_transmission` 的 `endpoint=None` 表示固定组播；`feed` 必须携带来源。默认事务超时 30ms，构造参数允许 30～2000ms；普通事务最多三次发送，重试字节不变。所有配置写和 schema 3/4 读仅单次发送。适配器不得无限排队；每个 tick 的收发量必须有界。

`UdpTransport::bind(interface_ipv4, local_port)` 明确绑定所选网卡地址。Rust std 不提供设置出站 `IP_MULTICAST_IF` 的方法：多网卡场景须验证 OS 组播路由，或由平台代码配置 socket 后交给 `from_socket`，不能仅因 bind 成功便宣称选卡验收通过。设备监听使用 `UdpTransport::device(interface)` 加入 `239.255.33.21:47821`。应用自行管理 DHCP、mDNS 与 socket 生命周期。

只读发现示例（无接管、无目标）：

```sh
cargo run --manifest-path mobocom_realtime/rust/Cargo.toml --example discover -- <控制网卡IPv4> <新的非零session-u64>
```

事务返回 `Ticket`：持续驱动 I/O，使用 `take_result(ticket)` 获取 `Frame` 或 `ClientError`；普通同 UID 忙时返回 Busy，不建立无限任务队列。`queue_targets` 合并不同资源的最新未发送值，已发送帧不变，替代后取消旧重试。`write_targets` 是需要显式 ticket 的单事务入口。主控流程：

1. `scan` 完成后检查 `peers()` 的 seen/state、必需 UID 名册与 `scan_overflow`。空扫描不是无设备证明。
2. `get_info` → 循环 `get_caps`/`Capabilities::push` → `get_status`，确认类别契约和真实安全。保留未知 type 原值供显示/查询/停止，不自动套用运动资源。
3. 用户确认后 `open_session(uid, guard)`；读 STATUS，再显式 `set_source`，轮询到 ACTIVE/MOBOCOM、owner=本 session，使用新 guard。
4. 配置 `RealtimeScheduler`，选完整协调组、覆盖已有活跃资源，提供完整目标快照后 `start`。应用每周期驱动 scheduler.tick 与 client I/O，不能以 UI 帧率保证实时性。
5. 正常结束 `scheduler.finish(uid, final_values, now, timeout)`：终目标 → applied 序号 → STOP → SAFE/safeConfirmed。applied 不代表机械到位。

`stop(0, None, estop)` 立即进入最高发送优先级，不等待事务/扫描，取消旧重试和队列；组播无 ACK，必须逐节点读安全状态。单播 STOP 提供显式查询得到的 guard；Guard 失效时重新查询，不能拿旧 ACK 当作当前状态。

`renew_session(new_id)` 必须在停发、无在途事务且查询确认所有未挂起节点 SAFE 后调用；随后逐设备显式 open_session，再由用户接管。它不会隐式停止、清故障或恢复运动。

## 重连、配置与失败处理

`reconnect(uid)` 先使该 UID 旧连接代次失效，再开始一轮发现；与 TS 的组合式 reconnect 不同，Rust 调用者在 `scan_complete` 后自行 `get_status`，核对 boot/epoch 与已确认身份。每 UID 最多 4 个候选地址，使用独立只读会话 INFO 探测新旧地址；多个在线地址确认同 UID 才 Conflict，无人确认/身份不符为 Unconfirmed。历史歧义地址在后续扫描仍被探测。候选容量溢出保持未确认，不静默选一个；此时需先排查网络身份冲突，再显式重建客户端。

重连后 `needs_rearm=true`；只允许查询、安全停止、显式复位和安全建会话。成功 open_session 才解除挂起，再显式切源、重新 configure、提供新目标。旧 scheduler 永久绑定旧连接版本，不因重新建会话自动恢复。UDP 探测不提供认证或网络分区下的唯一性保证。

配置写前保存 `request_record(ticket)`（session/sequence/UID/完整帧）及 guard/revision/期望值，写超时只读关联，不新建物理动作“补发”。`ConfigurationSnapshot::confirms` 只比较配置关联和值；调用者还必须核对 UID、boot、epoch 与当前连接版本。schema 3、跟焦写应放入目标 ACK 后且 TTL 余量足够的空档；SDK 不代替应用证明此预算。跟焦输出先探测 wired_commands，再按私有 UART/按钮忙窗口准入；配置 Applied 不是电机到位。

## 验证范围与已知基线问题

原始向量不变。Rust 20 项集成/回归测试、1,437 个现有向量/codec步骤、14,400 个固定种子差分步骤通过；黄金向量逐字节比较，只有随机差分允许上述提示性版本计数差异。UDP localhost、主机和 `thumbv7em-none-eabihf` 的 `no_std` 编译、Clippy 与 Rust-only 隔离消费/清单/篡改拒绝检查通过。状态机差分不是穷举证明；没有真实多网卡/DHCP/组播网络、RTOS、机械安全或物理性能验收。

本次 Linux/GCC 13.3 全库回归中，既有 C++ `protocol_motion_profile` 未通过；未修改的 Git HEAD 独立副本同项也失败（断言/崩溃），不是 Rust 通过即可忽略的发布门槛。本任务未改动该 C++ 实现/向量，须单独排查；不能声明整个四语言发行验收已通过。

SDK 打包包含 `.rs`、`Cargo.toml`、`Cargo.lock`、示例与测试，排除 `target/`。类型注册表本次未新增 ID 或修改 revision；生成器新增 Rust 输出。Mobocon 状态见[客户端更新记录](../../docs/MOBOCON_UPDATES.md)。不自动升级消费工程，不烧录、不开放硬件输出门槛。
