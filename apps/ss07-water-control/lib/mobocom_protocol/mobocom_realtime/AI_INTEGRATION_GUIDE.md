# AI 使用 Mobocom V2.2 接入设备

Control `0x22`、SDK `2.2.0-network.1`，Ethernet/UDP（推荐DHCP），旧0x21不兼容。先读[规范](PROTOCOL.md)、[类别模板](DEVICE_PROFILES.md)、[SDK指南](SDK_INTEGRATION.md)和[客户端更新记录](../docs/MOBOCON_UPDATES.md)。历史candidate条目不作为当前传输接口。

## 边界

公共协议唯一维护源是device_protocol。设备只实现Profile、DriverHooks、网络适配器及LocalControlInput，不复制事务/来源/故障状态机，不直接修改vendor快照。实体节点不接时码、不下载节目、不维护T0；0x12时码供Mobocom/UE和SS-07只读记录主机。原有NRF24到USB CDC虚拟拍摄格式不改。

跟焦、云台、风扇分别按逻辑设备实例化核心。下游PDMOVIE/CAN/PWM、GPIO、真实单位、安全值、限速及传感器归设备工程。移除的是上游Control的RS485承载，不擅自改写私有驱动。

## 类别与安全

使用[类型注册表](DEVICE_TYPES.md)的DeviceType常量，未知类型只允许展示、查询和停止，不套用其它类别运动。UID区分设备实例，endpoint/resource是板内资源，不是网络地址。

接入前填写资源单位、范围、持续性、协调组、限速/限加速度、输出刷新和安全期限；动作填写opcode、参数、占用资源、硬超时。未知真实参数保持未配置，不能用虚拟夹具开启物理输出。反馈无效时不得报告到位。

## 固件接口

```cpp
mobocom::DeviceCore node(profile, uid, fresh_boot_id, hooks);
mobocom::LocalControlInput local(node);
// 一个完整 UDP 数据报，源 endpoint 由适配器保存。
node.receive(bytes, length, monotonic_us);
std::uint8_t reply[mobocom::MaxFrame];
std::size_t reply_length;
if (node.take_reply(monotonic_us, reply, sizeof(reply), reply_length)) {
    udp_send_to_request_source(reply, reply_length);
}
// 即使没有网络输入，也必须独立周期执行。
node.tick(monotonic_us);
```

构造后检查valid；Profile、数组、hooks context须同寿命。所有调用由同一任务执行，ISR/无线输入通过有界队列投递。一个数据报只解码一次，不缓存半帧。回复立即可取，保留20 ms，下一receive清旧回复；必须在处理下一个请求前取走并绑定正确源endpoint。

必要hooks是validate_targets、apply_targets、start_safe、safe_confirmed、reset_allowed、flush_remote；有动作时增加validate_action、start_action、poll_action、cancel_action，真实反馈才实现feedback。没有save_address。所有回调有界非阻塞，不能在网络处理内等电机/Flash完成。

receive不交付运动目标，周期tick交付最新值并检查TTL/租约/输出gap。apply_targets仅表示交给驱动。start_safe启动真实安全操作，safe_confirmed只在真实条件满足时返回true；不得用ACK或固定等待伪造安全完成。

遥控数据在解码时捕获epoch/generation及本地sequence，队列不能在出队时换新凭据；来源切换flush_remote清旧FIFO与队列。开机REMOTE需完成安全初始化后显式select_local_source并确认ACTIVE，不能直接改核心内部状态。

## 网络与主控

推荐DHCP获取IP、UDP47821、组播239.255.33.21、TTL=1，部署选择控制网卡。主机请求src_uid=0；普通单播dst_uid=设备UID，组播dst_uid=0。仅空payload的DISCOVER和STOP/ESTOP可组播，SESSION_OPEN必须单播。

Client使用bigint UID；不再有node_id或enumerate/assign。Wire提供send(bytes, endpoint|null)、close，接收调用feed(datagram, from)。初始扫描学习当前endpoint；换址或boot变化时挂起该UID，以独立只读会话INFO验证新旧地址，只有多个在线地址确认同UID才冲突，单一匹配者更新映射。应用失联后可调用reconnect(uid)，无需重建Client；清旧目标/凭据，查询成功不解除needsRearm，用户显式安全建会话/切源/新目标才可重启，禁止错误回调自动接管。独立发布器还须校验connectionVersion；旧调度批次不得复用。所有状态都回应发现，必需UID名册用于确认缺失，不能将空结果当作无设备证明。设备名通过可选mDNS发布，方案见[动态地址与名称发现](../docs/NETWORK_DISCOVERY.md)，本库不创建mDNS服务或Mobocon UI。

先inspect确认INFO/CAPS/STATUS与类别能力；无故障SAFE且safeConfirmed时openSession(uid,boot,epoch,generation)，查询新上下文，显式切到MOBOCOM并确认ACTIVE及owner。随后目标、动作和配置绑定本次boot/epoch/generation。调用示例见[SDK指南](SDK_INTEGRATION.md)。

同UID单播串行，不同UID并发。扫描独立后台收集，不阻塞目标/保活，同时只允许一次扫描；全体STOP/ESTOP立即组播，无回复，之后逐设备查安全状态。每个事务有独立等待者，响应按会话/序号/命令/UID/来源关联。超时最多原帧重试两次，单次物理配置命令例外；禁止将超时动作换新序号重发。

## 调度、结束与恢复

RealtimeScheduler按100 Mbps网络预算和协商TTL工作，资源集合需完整协调组且覆盖已活跃资源；运行中只更新值。目标快照合并/替换，SupersededError需处理。主机发布任务应独立于UI，不用延长TTL掩盖卡顿。网络预算通过不等于实时性通过。

终目标用finish等待applied后STOP，再查安全完成。applied不表示机械到位；有反馈时按类别容差和稳定时间确认，无反馈只记录命令关键点。持续保持需继续刷新目标，HEARTBEAT不刷新目标TTL。

State(detail0)先查询，不等于FAULT。STOP完成SAFE无需复位；FAULT仅在真实安全和reset_allowed成立时显式恢复，SWITCHING/STOPPING有界等待。用户“复位并接管”流程必须逐步确认状态、建立新epoch并发当前新目标；失败中止，不重播旧运动、不影响其它设备。

可选recover_timeout_fault(now)只清掩码允许的来源/目标超时，在tick和硬件故障处理后调用；急停、驱动、限位和安全超时继续锁存。软件恢复不代表可重新运动。

schema1维护配置、schema2停止调速、schema3实时调速、schema4跟焦控制保持独立安全门槛，详见规范第9节。配置结果通过revision、session/sequence、身份及读回值关联；schema3和跟焦命令只发送一次，丢ACK只读确认，不清公共故障或偷偷接管。

## Rust 接入

新增[纯 Rust SDK](rust/README.md)，路径 `mobocom_realtime/rust`。DeviceCore/Driver/LocalControlInput、主控 Client/RealtimeScheduler、schema 1～4 与独立时码已提供；核心 no_std、固定容量，std 只增加 UDP 适配。按指南驱动轮询/ticket，不套用 TS Promise 调用。所有真实安全、驱动、配置 revision/保存与私有准入仍在适配器实现。

## 验收

修改公共行为必须同步 C++、Python、TypeScript、Rust、规范及向量；运行四语言测试和隔离打包验证。只声明实际测试证据。本库软件通过不代表消费固件已升级、Mobocon已实现或实物网络/机械安全已验收。不得未经授权升级相邻工程、烧录或解锁硬件门槛。
