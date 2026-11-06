# SS-07 Mobocom V2.2网络Control接入

## 1 已核验基线

| 项目 | 基线 |
| --- | --- |
| 仓库 | git@codeup.aliyun.com:629f6370487c500c27f60603/device_protocol.git |
| 分支/提交 | feat/network-only-v22，5568bd3c0e1a7d0782b941a5061568c0d979409c |
| 规范 | mobocom_realtime/PROTOCOL.md |
| 接入 | AI_INTEGRATION_GUIDE.md、SDK_INTEGRATION.md、DEVICE_PROFILES.md |
| SDK | 2.2.0-network.1，协议库软件验证通过，消费固件/应用/硬件未验收 |
| Control / 时码 | 0x22 UDP / 独立0x12 |
| 设备 / 主机 | DeviceCore、LocalControlInput / Client、RealtimeScheduler、HostTimeline |

新源码已移除旧时间轴实现，不创建设备授时、节目下载、缓存播放、T0或旧协议兼容执行路径。时码只进mobocon/UE；mobocon求值时间线并周期下发目标。本目录提供系统设计与协议接入契约，消费固件/PC实现和硬件验收分别在对应工程完成。

## 2 帧与网络契约

**RK3588视频声呐系统**以`VIDEO_SONAR(0x9009)`纳入同一Control网络，仅做UID发现与只读状态，不属于运动节点；契约、占位Profile与黄金向量见[视频声呐节点](video-sonar-node.md)。


Control：D5 7D，version=0x22，kind/flags，src_uid/dst_uid各u64，sequence:u32（不回绕），command:u16，session:u64，payload_length:u16，Payload，CRC16。帧头37B、总长39+N，N≤240，最大279B；多字节小端，CRC16 CCITT-FALSE覆盖version到Payload。一个UDP数据报恰好一帧，缺字节、尾随字节和多帧拼接均丢弃，不使用流解析器。

Ethernet/UDP（推荐DHCP），端口47821、组播239.255.33.21、TTL=1，主控选择控制网卡。同UID单播串行、不同UID并发。默认事务超时30ms，可配置30～2000ms；普通请求最多原帧重试两次，没有recoverIdle或串口回复窗。设备receive后立即take_reply并单播到该请求源endpoint；未取回复保留20ms，新receive清除旧回复。

单播STOP/ESTOP在对应UID队列中优先；组播STOP/ESTOP空payload立即发送、不等待在途事务、无回复，之后逐设备查安全完成。DISCOVER空payload组播，五种状态均单播回应；默认两轮、每轮100ms，可运行中后台扫描而不阻塞目标/保活，并核对必需UID名册。UID到当前IP:port映射冲突时禁止单播；IP推荐DHCP分配，名称见[发现方案](../NETWORK_DISCOVERY.md)。

## 3 使用现有命令，不另发明定位协议

| 命令 | 作用 |
| --- | --- |
| 0x0001/0004/0005 | HELLO/INFO/CAPS，取得能力和身份 |
| 0x0002 DISCOVER | 26B回复，UID在帧头；收集endpoint映射 |
| 0x0006 SESSION_OPEN | 单播20B：boot:u64、epoch:u64、generation:u32；安全待机建立新会话 |
| 0x0010 SET_SOURCE | guard=boot:u64 + generation:u32 + epoch:u64，取得REMOTE/MOBOCOM来源 |
| 0x0011 HEARTBEAT | 续来源租约，不续目标年龄 |
| 0x0020 WRITE_TARGETS | guard、count及endpoint/resource/value批次 |
| 0x0021/0022 | EXECUTE_ACTION/ACTION_STATUS，明确准备/零位/校准动作 |
| 0x0030 STATUS | 来源/状态/序号、安全确认及资源目标/反馈 |
| 0x0040/0041/0042 | STOP/ESTOP/RESET_FAULT |

ACK的accepted证明受理，applied证明交给驱动，都不证明机械到位。STATUS资源记录已有target、feedback、target_age和有效位；但没有反馈采样时间，CAPS也没有到位容差/稳定时间。传感器年龄由驱动判断反馈是否有效，到位参数在真实类别/PC契约明确，若需额外线上字段再经公共库扩展。

## 4 控制与目标保护

公共来源NONE/REMOTE/MOBOCOM，状态SAFE/SWITCHING/ACTIVE/STOPPING/FAULT。PC编辑和播放都是MOBOCOM内部业务，不能重新创建PC_POSITIONING/PC_TIMELINE公共来源。安全待机先SESSION_OPEN并读取新epoch/generation，再显式切换来源，安全确认后递增generation/建立owner，排空旧无线队列；没有输入也周期tick。guard绑定boot/generation/epoch，所有权绑定session。

持续资源有每资源target_ttl，来源有lease_ttl。新合法目标刷新对应资源及租约；HEARTBEAT仅刷新租约。重复包、查询或其他资源更新不刷新旧资源年龄。任一活动持续资源过期停止该节点并锁存故障；不会自动回退无线或中止其他节点。

## 5 编辑、播放和结束

编辑定位：确认MOBOCOM控制权 → 按协商频率刷新完整协调组目标 → 查询真实反馈/偏差 → 到位稳定后确认关键点。需要保持继续刷新；退出发STOP并查SAFE/NONE及真实安全完成。finish等待最后目标交给驱动再停止，不是机械到位助手，不能替代定位确认。

播放：主机HostTimeline提供本场时间，业务求值后setTargets，RealtimeScheduler按协商频率发最新快照。取消旧待发送目标，不对被替代的目标重试；驱动负责声明的限速/限加速度和耦合约束，公共核心不插值。正常结束交付最后目标后STOP；需要持续保持则不调用结束停止流程。

## 6 容量与带宽

SDK每设备最多16资源/16动作定义、一个当前会话、固定建立/安全/配置缓存、8动作结果、1待回复。不累积历史会话；只读陌生会话不占槽。序号接近耗尽时停止发布并确认安全，轮换session及epoch，不要求重启设备。旧事务不能重执行，重连不恢复运动。

100Mbps Ethernet/IPv4/UDP预算：每个N字节数据报占max(46,N+28)+38字节，请求/回复分别计费。三资源WRITE_TARGETS为84B、ACK79B；目标、分页状态、保活累计超过80%拒绝配置。没有16设备地址上限，动作/配置、重试、交换机排队和主机调度需另留余量。预算不证明实际100Hz或安全时延；参考数据见[REPORT](../../mobocom_realtime/REPORT.md)。

## 7 SDK接入和验收

消费者使用lib/mobocom_protocol和lock，公共package_sdk.py发布后verify完整摘要；禁止改vendor。云台工程提供真实Profile与DriverHooks：validate_targets、apply_targets、start_safe、safe_confirmed、reset_allowed、flush_remote；动作和真实反馈提供相应Hooks。构造DeviceCore(profile,uid,boot,hooks)，没有save_address或node_id。所有方法同一任务调用，其他任务投递有界事件。

必要测试为严格数据报解码、超大UID、换IP按UID重连与双在线地址歧义、旧队列/调度失效且不恢复运动、并发乱序、来源/旧epoch与代次、最新目标/组限制、每资源过期与租约、动作去重、停止优先、真实反馈/限速、安全不确认和故障复位。公共软件报告不代替云台MCU构建、网络丢包/拥塞下停止、失电姿态和实际延迟验证。RK与已设计跟焦不因本次文档升级而自动迁移。
