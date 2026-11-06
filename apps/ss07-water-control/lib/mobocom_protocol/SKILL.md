# device_protocol 开发入口

使用 [V2.2 网络规范](mobocom_realtime/PROTOCOL.md)、[类别模板](mobocom_realtime/DEVICE_PROFILES.md)、[AI 接入](mobocom_realtime/AI_INTEGRATION_GUIDE.md)、[SDK 指南](mobocom_realtime/SDK_INTEGRATION.md)和 [主机纯授时](timecode/README.md)。Control统一0x22 UDP、UID寻址，不保留旧串口兼容路径；旧时间轴只保留在Git历史。

所有设备使用同一 DeviceCore，主控使用同一 Client。跟焦与云台分别实例化；不要将私有 PDMOVIE/CAN/PWM、单位、GPIO、安全值或 RTOS 平台写入公共核心。运行路径固定容量、单任务所有权；所有回调非阻塞；没有输入时仍执行周期 tick。禁止用 ACK 冒充到位或安全完成。

type使用[统一注册表](mobocom_realtime/DEVICE_TYPES.md)和DeviceType常量，不自行分配。新增类型先改device_types.json、递增revision，再生成并验证三语言常量/类型向量。类型只标识逻辑类别，不代替CAPS和真实安全契约。

修改公共行为时同步 C++、Python、TypeScript、规范和黄金向量。受影响的协议测试通过后再生成发行快照；向量变化需审查原因，不能通过重写期望掩盖错误。文档变更只检查链接和 Git 空白，不机械重复全量测试。

影响 Mobocon 客户端时，同步维护 [客户端更新记录](docs/MOBOCON_UPDATES.md)，记录具体修改要求、兼容条件和各层验收状态；接入完成须有客户端提交及验证证据。

设备固定快照由 tools/package_sdk.py 生成，verify 校验完整摘要。禁止直接修改消费工程 vendor。消费工程升级、Git 提交/推送和烧录须有对应任务授权。

当前版本实施候选，不承诺跨设备电气同步。带宽测算使用100 Mbps网络封装预算，实际网络和主机调度需另测；主机时码丢失用本地时间继续本场，恢复不跳转，下场重新对齐。真实类别参数未知则保留未配置，不能用虚拟夹具解锁真实驱动。
