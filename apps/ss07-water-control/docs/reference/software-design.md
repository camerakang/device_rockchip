> 历史扩展方案：包含早期四应用范围，已由当前两软件设计取代。不得据此新增跟焦或 mobocon 开发任务。

# SS-07 RK3588 与 mobocon 软件详细设计

本设计将六路视频、五路声呐和 PC 时间轴操作落实到进程、数据模型、接口及界面。运动数据通过 PC 的 RS485 进入接收器；RK3588 仅提供视频和声呐。以下新增模块与 API 为开发契约，不代表已实现。公共协议基线见 [V1.2 接入](../mobocom-integration.md)。

## 1 RK3588 进程与模块

| 进程 | 内部模块 | 输入与输出 | 故障处理 |
| --- | --- | --- | --- |
| ss07-camera@front/back/left/right/down | 设备定位、模式设置、采集、H.264 解析、发布、健康 | V4L2 → 本机媒体服务 | 单通道退出重启；持续失败进入退避 |
| ss07-mediamtx | SDI RTSP 拉取、USB 流接入、WebRTC/RTSP 分发、分段录像 | 六路压缩视频 → 客户端/录像文件 | 重启影响六路分发；采集端重新建立发布连接 |
| ss07-sonar | 串口、厂家编解码、公平轮询、有效性、状态快照 | 五个地址 → 统一读数 | 一个超时不占用下一节点无限等待 |
| ss07-video-api | HTTP、事件推送、配置校验、状态聚合、录像查询、网页 | 服务状态 → 岸端 | 重启不停止采集/录像；重连先发快照 |
| ss07-storage | 完成片段索引、空间策略、导出、片段锁定 | 录像目录 → 索引与容量告警 | 索引重建；禁止删除正在写入的片段 |

USB 采集建议用 C++ 与当前 SDK 的 GStreamer/V4L2 组合，原生 H.264 路径不插入解码器。API 与声呐建议使用 Python 3 独立服务，界面使用 TypeScript；这些是项目实现选择，具体依赖版本在首次构建前锁定。媒体服务使用固定 ARM64 版本及校验和。首版仅接入已验证的直通管线，转码作为按设备能力开放的独立模式。

相机状态机为 ABSENT → OPENING → STREAMING；读帧/发布失败进入 RECOVERING，模式不支持进入 CONFIG_ERROR。建议重试间隔 1/2/5/10 秒，最高 30 秒；连续稳定运行后恢复起始间隔。系统服务重启限额与通道内部重连分开，避免高频崩溃循环。

每个流限制缓冲的字节数和时间跨度；候选上限为该通道约 1 秒码流，实测后冻结。积压恢复需要新的随机访问点及参数集，不能从任意 P 帧继续。采集健康、发布健康和订阅端播放健康分别统计，不以进程仍存在判定画面正常。

## 2 RK 配置与持久化

| 文件或目录 | 内容 | 更新方式 |
| --- | --- | --- |
| /etc/ss07/system.json | schema_version、设备名称、网络与日志策略 | 校验后写临时文件并原子替换 |
| /etc/ss07/cameras.json | 固定通道、身份匹配、模式、码率、GOP、媒体路径 | 一次仅应用受影响通道 |
| /etc/ss07/sonars.json | sensor_id、方向、总线地址、厂家驱动、超时与周期 | 总线暂停轮询后应用，不与旧配置交叠 |
| /etc/ss07/storage.json | 录像目录、容量阈值、保留策略 | 下个清理周期应用 |
| /var/lib/ss07/catalog.sqlite | 完成录像、锁定状态、配置变更记录 | 单写者事务；丢失时扫描重建录像索引 |
| /data/ss07/recordings | 原始分段录像 | 完成后索引，挂载缺失时禁止落到系统盘 |

相机身份匹配优先唯一序列号，其次固定 Hub 端口物理路径；相同无序列号设备不能仅按 VID/PID 区分。开机检测重复绑定并报错，禁止把左右相机悄悄交换。声呐 USB 串口使用稳定设备链接，检查厂家地址冲突。

配置的 draft、校验、持久化和实际生效状态分开。生效失败报告具体通道并恢复原配置；网页不能显示尚未应用的值为运行值。批量变更不承诺跨服务原子生效，返回逐项结果。设备密码独立保存，不出现在配置导出中。

## 3 视频与声呐数据契约

所有状态携带 schema_version、device_id、boot_id、seq、observed_at_utc；年龄用本机单调时钟计算并输出 age_ms，不跨主机比较单调计数。JSON 中 u64 标识采用十进制字符串。

| 对象 | 必备字段 | 语义 |
| --- | --- | --- |
| CameraStatus | channel_id、capture_state、publish_state、format、width/height、fps_observed、bitrate_observed、last_frame_age_ms、restart_count、error | fps 是测量值，配置 fps 另列 |
| StreamDescriptor | channel_id、path、codec、live_url、availability | URL 只含连接信息，不暴露管理凭据 |
| SonarSample | sensor_id、direction、address、seq、distance_mm、valid、reason、quality、age_ms | 不支持测距的成像声呐采用另一个类型 |
| Recording | recording_id、channel_id、start/end_utc、duration_ms、size_bytes、complete、locked、availability | 文件路径由服务器内部映射 |
| SystemHealth | cpu、memory、temperature、disk_free、network、service_states、alerts | 可用字段按硬件能力标记 |

声呐 reason 建议为 timeout、crc_error、out_of_range、unconfigured、stale、device_fault；厂家原始错误码另列。质量未知用 null。具体过期门槛取配置的采样周期与超时预算，不能对不同型号统一假定。若声呐没有测量时间，仅记录收到时间。

## 4 RK API 草案

所有业务接口置于 /api/v1；反向代理统一网页与事件接口，媒体协议按所锁定版本配置。鉴权后的修改请求必须校验来源，避免浏览器跨站误操作。

| 方法与路径 | 功能 | 权限 |
| --- | --- | --- |
| GET /status | 全量系统快照 | 监看 |
| GET /streams | 六路描述及状态 | 监看 |
| GET /sonars | 五路最新读数与年龄 | 监看 |
| GET /events | SSE 状态变化与告警，断线后重新获取快照 | 监看 |
| GET /recordings | 通道/时间筛选、分页查询 | 录像读取 |
| GET /recordings/{id}/content | 完成片段读取，支持范围请求 | 录像读取 |
| POST /recordings/{id}/lock | 锁定或解除锁定 | 维护 |
| GET /config | 脱敏后的实际配置和 revision | 维护 |
| PUT /config | 带 expected_revision 的配置修改 | 维护 |

成功返回 data 与 request_id；失败返回 error.code、message、details。建议使用 CONFIG_INVALID、REVISION_CONFLICT、DEVICE_UNAVAILABLE、STORAGE_UNAVAILABLE。配置冲突返回 HTTP 409；校验错误返回 422。导出和删除只接受 recording_id，校验路径归属与符号链接，不把用户字符串直接拼接为文件路径。

API 不包含任何电机使能、运动或时间轴接口。SSE 只传状态，不传视频帧；建议状态 1Hz、告警立即推送，事件队列有界，慢客户端断开重建。不同监看客户端使用同一采集/发布源，不各自打开相机。

## 5 岸端视频界面

首屏显示一路可切换主画面和五路辅助画面，分别显示名称、连接状态和帧年龄；可在主画面旁看对应方向声呐读数。浏览器不可解码或没有新帧时显示原因并允许重连，冻结画面加离线遮罩。

录像页按通道和时间查询，显示录像缺口与锁定状态。维护页处理设备绑定、相机模式、声呐配置、存储策略与日志导出。观看不要求打开配置页；运行故障采用告警，不把调试寄存器或 GPIO 信息放进日常操作界面。

## 6 mobocon 分层

| 模块 | 职责 | 约束 |
| --- | --- | --- |
| UI | 设备列表、编辑定位、时间轴、执行状态与故障 | 不直接写串口 |
| ProjectStore | 工程、关键点、标定引用、自动保存、迁移 | 编辑不会产生运动 |
| DeviceRegistry | UID/NodeID/boot、类别与能力、连接状态 | UID 是持久身份，NodeID 可重新分配 |
| PositioningService | 能力检查、单次定位、动作状态、位置/偏差 | 一次定位一个节点；无线互斥 |
| ProgramService | 单位转换、轨迹编译、各节点节目和校验 | 使用公共 compileProgram 及真实类别校验 |
| RunCoordinator | 名册、下载、准备、可信 T0、提交、结果 | 复用 MasterService，不承诺原子启动 |
| SerialTransport | 唯一串口所有者、分片接收、事务调度 | 复用 Client/Wire，整个总线一笔未完成事务 |
| ClockProvider | 发生器 epoch、时间估计及可信误差上界 | 无可信来源时禁止开始时间轴 |

首版建议 PC 桌面进程持有串口，网页 UI 通过受限 IPC 调用业务服务，避免多个页面同时控制总线。桌面容器与操作系统支持矩阵在开发开始时冻结；TypeScript 协议 SDK 保留独立性。重新连接先读取节点状态，不自动发送目标、校准、CLEAR 或 Run。

总线调度顺序建议为用户停止 → 当前动作/准备/运行状态 → 用户定位/下载操作 → 背景位置查询。优先级只能作用于下一合法事务，不能打断公共窗口。查询每次读取一个节点快照，界面默认手动刷新，可选约 2Hz；下载时可降频，遇忙显示等待。

## 7 mobocon 页面与编辑模型

| 页面 | 操作与结果 |
| --- | --- |
| 连接 | 选 Control 端口，发现节点，显示 UID、角色、版本、反馈及授时能力 |
| 设备定位 | 输入 focus 或三轴角度，显示范围，点击移动；显示目标/实际/偏差/到位或失败，提供停止 |
| 时间轴 | 添加关键点、时间、过渡和轨迹预览；从实际位置或明确选择的目标生成关键点 |
| 执行 | 核验名册、下载进度、准备结果、授时、T0、逐节点提交及状态 |
| 标定维护 | 显式使能/零位/行程等类别操作，记录标定版本及影响 |

工程建议保存 schema_version、project_id、时间单位、节点 UID、资源、类别版本、单位、标定版本、关键点、初终态、速度/加速度约束与准备参数。关键点另存 sample_source、采样时间和有效性；整数位置保存类别单位，不把界面浮点角度作为协议原值。节目产物记录编译器/SDK 摘要、每节点 program_id、CRC、所需能力与名册。

界面角度转换为毫度仅是云台类别候选；跟焦可采用行程万分比候选，最终在公共类别发布时冻结。轨迹在两端按相同量化规则校验；画面预览不是范围/导数证明。位置、使能或标定变化使旧准备失效，编辑后的工程重新编译下载。

定位流程见 [定位设计](../position-telemetry.md)。无实际反馈时界面显示目标和“需要人工检查”，不会自动点亮到位。时间轴自然结束、故障、用户中止与部分启动分别呈现；不将某一个 ACK 显示为全系统成功。

## 8 开发目录建议

RK 应用建议在本目录后续加入 src/camera、src/sonar、src/api、web、config、systemd、packaging、tools；当前阶段保留为文档，不创建占位程序。跟焦/云台消费公共 SDK，驱动与板级配置放在接收器工程。mobocon 拥有 app/ui、app/services、app/transport、categories、lib/mobocom_v12 和 SDK lock。

所有应用各自发布版本，同时由 SS-07 release manifest 记录经过联调的组合。网页、RK API、公共协议与设备类别分别版本化，类别变化不得由 UI 隐式猜测兼容性。
