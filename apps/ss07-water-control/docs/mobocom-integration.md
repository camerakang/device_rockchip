# SS-07 Mobocom V2.0实时Control接入

## 1 已核验基线

| 项目 | 基线 |
| --- | --- |
| 仓库 | git@codeup.aliyun.com:629f6370487c500c27f60603/device_protocol.git |
| 分支/提交 | main，633eb917a129481ac36996d2d285563faae17cb3 |
| 规范 | mobocom_realtime/PROTOCOL.md |
| 接入 | AI_INTEGRATION_GUIDE.md、SDK_INTEGRATION.md、DEVICE_PROFILES.md |
| SDK | 2.0.0-candidate.1，硬件未验收 |
| Control / 时码 | 0x20 / 独立0x12 |
| 设备 / 主机 | DeviceCore、LocalControlInput / Client、RealtimeScheduler、HostTimeline |

新源码已移除旧时间轴实现，不创建设备授时、节目下载、缓存播放、T0或旧协议兼容执行路径。时码只进mobocon/UE；mobocon求值时间线并周期下发目标。本次只核验并更新设计，不安装SDK到固件/PC，不烧录或修改公共库。

## 2 帧与串口契约

Control：D5 7D，version=20，kind/flags，src/dst各u16，sequence:u32（不回绕），command:u16，session:u64，payload_length:u16，Payload，CRC16。总长27+N，N≤240，最大267字节；多字节小端，CRC16 CCITT-FALSE覆盖version到Payload。不同于旧25+N帧和u16序号，不能复用旧偏移。

Control一条总线一个主站/未完成事务。节点在真实请求尾后20–1000µs内开始回复；最长回复2670µs。主控等待完整回复尾后至少20µs，或真实请求发送结束后至少3700µs；PC默认响应超时30ms，超时需recoverIdle(3700)终止/排空发送并满足空闲窗口，再原帧重试，最多两次。write返回不等于真实TX完成，禁止以固定sleep代替可靠恢复。

STOP/ESTOP优先于待发送普通工作，但不抢占已有回复窗。DISCOVER为32个2ms时隙、64ms静默，发现/分配在实体停止后执行。UID/boot/地址冲突检查以完整所需名册为准。

## 3 使用现有命令，不另发明定位协议

| 命令 | 作用 |
| --- | --- |
| 0x0001/0004/0005 | HELLO/INFO/CAPS，取得能力和身份 |
| 0x0010 SET_SOURCE | guard=boot:u64 + generation:u32，取得REMOTE/MOBOCOM来源 |
| 0x0011 HEARTBEAT | 续来源租约，不续目标年龄 |
| 0x0020 WRITE_TARGETS | guard、count及endpoint/resource/value批次 |
| 0x0021/0022 | EXECUTE_ACTION/ACTION_STATUS，明确准备/零位/校准动作 |
| 0x0030 STATUS | 来源/状态/序号、安全确认及资源目标/反馈 |
| 0x0040/0041/0042 | STOP/ESTOP/RESET_FAULT |

ACK的accepted证明受理，applied证明交给驱动，都不证明机械到位。STATUS资源记录已有target、feedback、target_age和有效位；但没有反馈采样时间，CAPS也没有到位容差/稳定时间。传感器年龄由驱动判断反馈是否有效，到位参数在真实类别/PC契约明确，若需额外线上字段再经公共库扩展。

## 4 控制与目标保护

公共来源NONE/REMOTE/MOBOCOM，状态SAFE/SWITCHING/ACTIVE/STOPPING/FAULT。PC编辑和播放都是MOBOCOM内部业务，不能重新创建PC_POSITIONING/PC_TIMELINE公共来源。切换先安全确认再递增generation/建立owner，排空旧无线队列；没有输入也周期tick。guard绑定boot/generation，所有权绑定session。

持续资源有每资源target_ttl，来源有lease_ttl。新合法目标刷新对应资源及租约；HEARTBEAT仅刷新租约。重复包、查询或其他资源更新不刷新旧资源年龄。任一活动持续资源过期停止该节点并锁存故障；不会自动回退无线或中止其他节点。

## 5 编辑、播放和结束

编辑定位：确认MOBOCOM控制权 → 按协商频率刷新完整协调组目标 → 查询真实反馈/偏差 → 到位稳定后确认关键点。需要保持继续刷新；退出发STOP并查SAFE/NONE及真实安全完成。finish等待最后目标交给驱动再停止，不是机械到位助手，不能替代定位确认。

播放：主机HostTimeline提供本场时间，业务求值后setTargets，RealtimeScheduler按协商频率发最新快照。取消旧待发送目标，不对被替代的目标重试；驱动负责声明的限速/限加速度和耦合约束，公共核心不插值。正常结束交付最后目标后STOP；需要持续保持则不调用结束停止流程。

## 6 容量与带宽

SDK最多16资源/16动作定义、8会话记录、8动作结果、1待回复。同boot会话记录不逐出，第9个返回SPACE；重连尽量复用会话，不能当HELLO会清历史。操作结果淘汰为NOT_FOUND；旧事务不能重执行。序号/generation不回绕，耗尽安全处理后重启/新会话。

1Mbps、8N1、16节点/20m为参考。事务预算=(请求字节+回复字节)×10µs+1000+20+实际适配器开销，计入状态/保活/动作，超过80%拒绝配置。不能把100Hz需求理解为全部节点保证。三资源WRITE_TARGETS请求64B、ACK59B，100Hz单节点仅目标约22.5%，状态和USB开销另计；两节点不等于现场已通过。

## 7 SDK接入和验收

消费者使用lib/mobocom_protocol和lock，公共package_sdk.py发布后verify完整摘要；禁止改vendor。云台工程提供真实Profile与DriverHooks：validate_targets、apply_targets、start_safe、safe_confirmed、reset_allowed、flush_remote、save_address；动作和真实反馈提供相应Hooks。一个DeviceCore一个物理节点，所有方法同一任务调用，其他任务投递有界事件。

必要测试为帧/流解析、来源/旧代次、最新目标/组限制、每资源过期与租约、动作去重、恢复发送/停止优先、真实反馈/限速、安全不确认和故障复位。公共软件报告不代替云台MCU构建、物理发送/停止、失电姿态和实际延迟验证。RK与已设计跟焦不因本次文档升级而自动迁移。

## 8 本次核验记录

2026年10月8日读取上述提交的规范、接入指南、类别模板、主机时码及关键C++/TS源码；独立脚本核对69条已发布Control请求向量的0x20版本、27+N长度、u32序号、命令偏移和CRC，均通过。设计本地链接、代码围栏和空白检查通过。

没有运行全量公共SDK回归、安装消费工程快照、编译ESP32或进行物理层/电机测试；仓库报告中的测试数量是上游记录，不作为本次硬件或消费者验收。当前仍为实施候选。
