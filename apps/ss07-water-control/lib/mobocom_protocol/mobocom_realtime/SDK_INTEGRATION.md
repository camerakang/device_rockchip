# V2.2 网络 SDK 构建、调用与发布

版本 `2.2.0-network.1`，Control `0x22`，主机授时独立保持 `0x12`。本版本与旧 `0x21` 不兼容，必须同步升级主控和设备。消费工程、Mobocon 应用及实物网络尚未在本次迁移中验收。

## 构建

```powershell
python -m unittest discover -s mobocom_realtime -t .
cmake -S . -B build/v22 -G "MinGW Makefiles"
cmake --build build/v22
ctest --test-dir build/v22 --output-on-failure
cd mobocom_realtime/typescript
npm ci --ignore-scripts --no-audit --no-fund
npm test
```

C++20 使用 `mobocom::protocol`，主机授时另用 `mobocom::timecode`。PlatformIO 名称为 MobocomDeviceProtocol。类型使用[统一注册表](DEVICE_TYPES.md)生成的 DeviceType 常量；新增类型需递增 registry revision 并执行 `python tools/generate_device_types.py`，发布前 `--check` 校验。

## Rust SDK

同版本新增 [mobocom-protocol](rust/README.md)，纯 Rust、无 FFI/第三方 crate，核心支持 `default-features = false`（no_std）。包括 DeviceCore、LocalControlInput、schema 1～4、固定容量轮询 Client/RealtimeScheduler、可选 std UDP 和独立 0x12 HostTimeline。

运行 `python3 tools/test_rust_sdk.py` 验证 Rust 测试、现有黄金向量与 Python 差分；也可 `cargo check --manifest-path mobocom_realtime/rust/Cargo.toml --no-default-features`。Rust CLI 需在 PATH；隔离打包测试新增 Rust 必检，不会静默跳过。快照包含 Cargo.toml/Cargo.lock/源码/测试，排除 target。

Rust 与 TS 的主控接口不同：每次请求返回 Ticket，由应用持续驱动 next_transmission/feed 和消费 take_result；scan 是一轮100ms，reconnect 后由应用显式查询 STATUS 并核对身份。多网卡组播出站接口须由平台 socket 或 OS 路由设置并实测。完整调用、容量/安全边界及已知 C++ 基线失败见 Rust 指南；本次没有改变0x22线协议或升级消费工程。

## 设备适配

构造 `DeviceCore(profile, uid, fresh_boot_id, hooks)`。删除协议节点地址和 save_address 回调。核心固定内存、单任务所有权，Profile、资源/动作数组和 hooks context 必须同寿命，构造后检查 valid()。最多16资源、16动作定义、8动作结果、一个待发回复；最大帧279字节。目标平台内存占用需实际编译测量，不沿用旧版 ABI 数字。

一个 UDP 数据报一帧，直接调用 receive；不分块、不拼接、不逐字节解析。receive 后立即 take_reply，将回复单播回这笔请求的源 IP:port。回复立即可取且最多保留20 ms；下一数据报会清除旧待发回复，因此接收源地址和回复必须成对处理。没有输入时也必须周期 tick，以检查目标新鲜度、租约和安全期限。SDK 不创建 socket、任务、GPIO 或驱动。

推荐设备通过 DHCP 获取地址，不写死 IP；UDP 端口47821，在控制网卡加入239.255.33.21，组播TTL=1。静态配置仍可选。网络必须有 DHCP 服务；SDK不分配地址。传输适配器负责网络接口选择、收发超时、限长及来源关联。UDP send 成功不代表设备执行成功。名称和部署方案见[动态地址与名称发现](../docs/NETWORK_DISCOVERY.md)。

## TypeScript 传输与身份

```ts
import {Client,Wire,Source} from './index';
// transport 由应用实现：send(bytes, endpoint|null): Promise<void>，close(): Promise<void>
const client = new Client(transport as Wire);
const uid = 0x9007000000000004n;
await client.discover([uid]); // 从回复源 endpoint 学习当前地址，不要求 setEndpoint
// 每个完整数据报调用 client.feed(datagram, {address: remoteAddress, port: remotePort})
const device = await client.inspect(uid);
await client.openSession(uid, device.status.boot, device.status.epoch, device.status.generation);
const ready = await client.getStatus(uid);
// 仅在已确认 SAFE、无故障且安全完成后，由用户显式接管。
const owned = await client.switchSourceAndWait(
  uid, ready.boot, ready.generation, Source.Mobocom,
  targetTtlUs, leaseTtlUs, switchWaitUs, ready.epoch);
```

UID 必须是 bigint，不可经 number 转换；ConfigurationRequest、Schedule 和 finish 的 Map 键也使用 bigint。所有写配置方法只有一个 UID 参数，既用于帧头目标，也用于配置 payload 身份，不再接受另一个数字节点句柄。

`Wire.send` 的 endpoint 为 null 表示固定组播；适配器必须明确实现该路由。`feed` 提供来源时核验单播 endpoint，正常网络接入应始终提供；换址INFO验证必须提供来源，不能缺省。已显式设置 endpoint 的旧测试传输可省略普通响应来源。setEndpoint 用于初始配置/测试，不能直接覆盖已有地址，拒绝覆盖本身不标记重复UID。地址变化使用scan/reconnect验证新旧地址，无需重建Client。

同 UID 单播串行、不同 UID 并发，响应按完整事务身份分发。scan/discover 独立于控制队列，默认2轮、每轮100 ms，不等待或阻塞目标/保活。并发扫描或扫描中轮换会话被拒绝；关闭会取消扫描，全体停止使当前扫描失效。所有五种状态均响应；发现不清故障、不改变控制权/目标，也不刷新TTL。建议UI每秒最多一次扫描。保存用户确认的必需 UID 名册，并检查 missing/conflicts；空扫描不能证明无设备，发现成功也不能证明SAFE。

稳定地址扫描不影响运行；发现该UID换址/boot变化则立即挂起该UID，取消旧请求/重试、清待发目标和安全凭据，其他UID继续。`scan`返回`reconnected`（已验证更新）、`unconfirmed`（探测失败/身份不一致）、`conflicts`（多个在线地址确认同UID）、`missing`（必需UID未确认）。换址额外向新旧地址发送一次有界INFO，响应等待使用transactionTimeoutMs，使用独立只读会话，不推进设备当前控制会话的事务缓存。只有单一有效UID/来源/boot/type/firmware匹配才更新映射；冲突消失后再扫描可解除地址冲突，但不会解除运动挂起。

`await client.reconnect(uid, rounds=2)`用于应用检测失联后的有界重发现：即使地址不变，也先使该UID旧控制上下文失效；只执行DISCOVER/INFO/STATUS，返回`{device, endpoint, status, needsRearm}`。检测重连期间boot/epoch变化则失败。失败保持挂起，可退避重试；没有SDK内置轮询定时器，应用负责限频触发。`getEndpoint(uid)`返回地址副本，`needsRearm(uid)`提示待用户重接管，`connectionVersion(uid)`供独立发布器隔离旧批次。重连期间不允许普通单播；地址确认后查询、STOP/ESTOP和显式RESET_FAULT可用，其他写/保活抛ConnectionChangedError。

用户显式确认安全并调用openSession成功后才解除挂起，再按正常流程显式切源、重新configure并提供当前新目标。不得把错误回调写成自动openSession/切源/续播。会话自动轮换跳过待重接管的UID；旧RealtimeScheduler批次因连接代次变化持续拒绝setTargets/start/finish，即使后来已重新建会话也不恢复旧目标。完整示例及验收边界见[换址重连](../docs/NETWORK_DISCOVERY.md#换址重发现与重连)。

默认事务超时30 ms，可用第四参数 `{transactionTimeoutMs:250}` 调整，范围30～2000整数。普通事务最多重试两次，字节不变；不会刷新设备 TTL。适配器不得无限积压延迟发送，应有自身超时与关闭取消能力。没有 recoverIdle 或 TX结束时间接口。

单播 STOP/ESTOP 在对应UID队列中优先；组播 STOP/ESTOP 立即发送空payload，不等待扫描和在途请求，也没有回复。必须逐设备查询实际安全状态；不能将 send 或 ACK 显示为“全部已停止”。

## 调度与配置

`budget(devices, hostOverheadUs=0)` 使用100 Mbps网络预算：每个N字节数据报按 `max(46,N+28)+38` 字节计费，目标、分页状态、保活累计超过80%拒绝。没有波特率参数、16节点地址上限或串口方向保护。hostOverheadUs 是调度期限余量，不冒充链路占用；额外配置/动作与网络排队需另留预算。

`RealtimeScheduler.configure(schedules, hostOverheadUs)` 验证所有权、频率、已协商保护期限及资源集合。Schedule 的 uid 为 bigint；resources 可选，默认全部资源。集合必须非空、不重复、包含完整协调组、不可遗漏已有活跃资源；运行中只能改值。setTargets/finish 需要完整所选集合。

调度器按周期发送最新快照，不积压旧目标；refreshQueuedTargets 原位更新待发批次，已发帧不变且过时重试取消。直接 writeTargets 合并同UID未发送的不同资源。SupersededError 是替代结果，调用者应处理。单UID失败停止其发布，其它UID继续；主动stop/finish后的迟到错误不再作为运行故障重复上报。

正常结束先停发布，再 client.finish 或 scheduler.finish：发送终目标、查询 applied、STOP，之后另查安全完成。applied 仅表示交付驱动，不是机械到位；开环不可报告虚假反馈。

schema1维护配置、schema2停止调速、schema3运行调速、schema4跟焦控制的布局和安全门槛见[规范第9节](PROTOCOL.md)。readLiveMotionProfile/writeLiveMotionProfile、readFocusControl/writeFocusSource/writeFocusCommand 均单次事务；物理按钮丢ACK不可自动重发。onRequest 在首次发送前保留 uid/session/sequence/frame，后续只读关联 revision、Applied、身份和期望值。不换序号补写、不借配置清故障或恢复运动。下游PDMOVIE的忙窗口仍归私有驱动，不是上游Control串口。

## State错误与显式恢复

State(detail=0)不是SAFE或无故障；查询最新STATUS。普通STOP确认SAFE/NONE/fault=0/safeConfirmed后可再次显式接管。FAULT需真实安全确认与reset_allowed成立后显式复位，不恢复旧目标或所有权；SWITCHING/STOPPING有界等待，不能循环清故障。

ProtocolError.snapshot包含ACK布局的故障/代次快照，可诊断但不能为旧运动更新凭据。设备可选 recover_timeout_fault(now) 仅处理掩码允许的来源/目标超时，在tick和硬件故障检查后调用；其余故障保持锁存。主机观察恢复后丢弃旧队列，等待用户重新启动。

接近序号耗尽时先停止调度及其它调用，再 await renewSession()；它停止已观察的设备、确认安全、轮换会话并建立新epoch，不恢复运动。boot跨重启变化；epoch隔离会话；generation在新epoch从1开始。

## 固定快照

```powershell
python tools/package_sdk.py
python tools/test_isolated_consumers.py
# 仅在消费工程迁移另获授权时安装
python tools/package_sdk.py --install <device-project>
python tools/package_sdk.py --verify <device-project>/lib/mobocom_protocol
```

包/lock固定0x22、授时0x12、版本、完整摘要、source_commit和source_dirty。未提交源码生成的包必须标记dirty，不能声称来自已提交版本。安装前校验旧受管0x20/0x21/0x22快照，拒绝未知协议、篡改或无清单目录。旧版本可通过完整性校验，不代表其线协议兼容。

本次不自动升级相邻固件、网页或Mobocon，也不烧录。部署仍需验证真实网络丢包/乱序/拥塞、输出延迟和抖动、失联停车、限位与物理安全。
