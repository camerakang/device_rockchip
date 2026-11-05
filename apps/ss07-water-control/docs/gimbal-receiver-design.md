# SS-07云台接收器V2.0电机控制软件独立设计

本软件运行于独立云台接收器，接受摇轮NRF24或mobocon实时Control，驱动Yaw/Pitch/Roll三台CyberGear。使用公共V2.0 DeviceCore，不接时码、不缓存节目、不维护统一T0。RK、跟焦、mobocon和UE不属于本固件开发交付。

## 1 输入输出与工程

| 接口 | 负责内容 |
| --- | --- |
| NRF24 | 既有帧校验、绑定、新鲜度和摇轮轴映射，进入LocalControlInput |
| Control RS485 0x20 | 公共帧、发现/能力、来源、实时目标、动作、状态和停止 |
| CAN 1Mbps | CyberGear身份、参数、运动、反馈、故障 |
| 显示/维护 | 显式来源选择、标定维护、状态和有界日志 |

独立PlatformIO/Arduino/FreeRTOS工程和固件目标，复用现有Receiver通信/显示基础；实际云台MCU/GPIO/UART/CAN必须按自己的原理图配置，不照搬跟焦板。USB虚拟CDC/ACK若已有业务保留原格式，不混入Control或日志。

## 2 模块与任务所有权

| 模块 | 功能 |
| --- | --- |
| BoardProfile/UartTransport | 一路Control收发、真实请求尾/回复窗口、DE/RE/CAN及板级配置 |
| RadioAdapter/WheelMapper | 原帧验证、序号、generation、轴语义、输入事件 |
| DeviceCore/LocalControlInput | 公共唯一事务/来源/目标年龄/安全与动作核心 |
| GimbalProfile | 三轴整数单位、范围、协调组、反馈、频率、TTL/租约和安全期限 |
| DriverHooks/MotorBackend | 批次耦合校验、限速/限加速度、CAN目标、安全动作、真实反馈 |
| CalibrationStore/UI | 标定版本、零位/外传动/限位、显示与维护 |

所有DeviceCore方法同一任务调用，UART/无线/ISR经有界队列传事件。独立周期tick无输入也执行，满足真实update_gap。receive只受理最新目标，tick交付驱动；实时队列周期目标最新值优先，动作不可覆盖。不要另写一套SourceArbiter或Run状态机。

必要Hooks：validate_targets、apply_targets、start_safe、safe_confirmed、reset_allowed、flush_remote、save_address。有动作增加validate_action/start_action/poll_action/cancel_action；实测反馈才提供feedback。回调非阻塞，不在回复窗口等待机械完成；不能仅替换apply_targets就认为适配完成。

## 3 来源和状态

公共source为NONE/REMOTE/MOBOCOM，state为SAFE/SWITCHING/ACTIVE/STOPPING/FAULT。PC编辑与播放均为MOBOCOM来源，区别留在PC业务，不增加公共PC_POSITIONING或PC_TIMELINE来源。

硬件安全初始化后构造Core并检查valid。开机是否显式选择REMOTE按产品配置，不恢复旧队列/速度。切换先清旧目标、取消动作并安全操作，确认后generation递增，建立owner；旧无线队列必须排空，事件保留解码时的generation，不能出队时改成新代次绕过过滤。恢复连接不自动夺权、复位或回退无线。

boot每次变化且非零，UID持久唯一；guard绑定boot/generation，MOBOCOM所有权绑定session。停止/失效撤销来源，故障解除按RESET_FAULT与真实安全条件，不自动恢复旧目标。

## 4 实时目标与定位

资源候选Yaw/Pitch/Roll位置，界面度，协议整数毫度候选；编号/单位在真实类别发布时冻结，不能使用虚拟双轴云台夹具当真实三轴配置。协调组资源整批提交，validate_targets校验旧有效/待交付与新目标合成快照及机械耦合。

apply_targets把最新目标交给真实驱动，并负责声明的速度/加速度限制、方向/外传动转换及CAN更新。公共核心不插值，轨迹求值在mobocon；接收器私有驱动可实现机械约束所需的平滑，不形成缓存节目执行器。

持续资源独立target_ttl，来源独立lease_ttl；新目标续对应资源和租约，HEARTBEAT只续租约。查询、重复事务或另一个轴更新不续过期轴。目标年龄达到TTL、租约到期或刷新gap超限停止本节点并锁存故障，不自动切换摇轮。

编辑定位是PC持续刷新同一目标并核对反馈，不是发送一帧后本地限时运动。移动/到位保持期间都需刷新；PC查询STATUS的target/feedback有效位，真实类别定义到位容差/稳定时间。退出发STOP并确认真实安全，finish的“最后目标applied”不等于到位。详见 [定位与反馈](position-telemetry.md)。

## 5 摇轮路径

先读取发送端实际载荷，冻结轴是绝对角、增量还是速度；不将focus/zoom/iris直接映射为三轴。校验绑定、CRC、有限数与新鲜度；未知ts回绕/重启语义先抓包。有效输入通过LocalControlInput.targets/action/heartbeat，不绕过公共来源门控。

若增量/速度转位置，在私有映射中积分并限制，重复/旧包不重放，恢复不补执行旧增量。绝对位置接管需要软匹配防跳变。本地也遵守目标/租约/刷新gap，不能用旧3000ms通信恢复阈值替代机械保护参数。

## 6 CyberGear与反馈

原生CAN为1Mbps、29bit扩展帧、DLC8；主站ID候选0，电机1/2/3绑定各UID。类型/参数/字节序详见 [CyberGear接口](cybergear-interface.md)，不发送调试器AT封装到CAN。

启动保持安全：身份/故障/固件能力检查 → 标定和姿态核验 → 停止状态下设置模式/限制并回读 → 显式动作/授权使能 → 接受新的目标。当前位置未知时不设机械零、不自动归零；电机易失配置启动重建，但不恢复旧目标。

位置模式用于PC位置目标，摇轮若需速度模式另定义真实类别/动作，并先停再切模式。项目度转rad，外传动单独映射，不重复内部减速比。反馈采用类型2/21和固件支持的0x7019/701B/701C，验证计圈、零位和异常。过期/错误反馈Hooks返回无效；STATUS没有反馈样本时间，target_age不能冒充采样年龄。

标定保存schema/version、轴身份、方向、零位、外传动、范围/速度/加速度、电流限制和校验。维护显式动作，先停止目标控制，动作占用资源与持续目标冲突则BUSY；完成不靠固定延时伪造。

## 7 停止、故障和动作

start_safe按STOP、ESTOP、过期、来源超时、驱动故障等reason执行真实安全动作；safe_confirmed只在真实条件满足时返回true。正常STOP、制动、保持、卸力分别写进类别，Pitch/Roll未验证不默认卸力或无轨迹归零。

动作同节点一次一个，公共操作ID和硬超时、最近8条结果；查询不续动作期限，NOT_FOUND不推断成功。安全/动作均异步，回复只ACK受理。软件停止不替代物理急停；CAN断线、bus-off、Core任务阻塞/复位由已验证电机看门狗或独立措施兜底，CAN_TIMEOUT单位/访问/动作仍需台架确认。

## 8 PC播放与时码边界

mobocon读取0x12主机时码并求值时间线，经0x20 WRITE_TARGETS连续下发；本固件不使用DeviceNode/Clock/准备token/节目缓存/T0。时码失效HostTimeline本场本地续播、不跳转，PC仍持续发送；Control失联则目标/来源保护停止，不继续缓存播放。UE策略单独定义和验收。

正常结束PC先停普通调度、末目标交付后STOP，再查询SAFE/NONE；需要保持继续刷新，不调用结束停止。设备仅证明applied或安全状态，不保证主机业务最后姿态机械到位。详情见 [公共接入](mobocom-integration.md) 和 [主机时码](timecode-integration.md)。

## 9 硬件、构建和验收

只需一路Control RS485与CAN，取消TimeCode接口/捕获。真实MCU、引脚、隔离、终端与线长核验后冻结。驱动供电16–28V，测满电、制动回馈、压降和负载温升。固定C++20、SDK lib/mobocom_protocol及lock，不改vendor；不按旧五次节目验证分配缓存/8KiB验证任务，Core/队列/驱动内存与任务栈按目标编译器实测。

交付独立固件/摘要、板型配置、类别/标定契约、锁定工具链/SDK、安装/升级恢复说明、CAN向量和验收记录。升级先停止安全，重启不恢复来源和目标；本次没有安装SDK或烧写。

验收顺序：板级/Control窗口 → 单轴身份/反馈/停止 → 三轴标定/机械约束 → REMOTE/MOBOCOM交接 → 编辑定位/保持/资源TTL → 动作/故障/复位 → mobocon实时播放/时码fallback → 实际适配器满载/断线 → RK满载共存。

记录实际目标频率、驱动延迟/抖动、三轴批次生效、单轴未刷新、保活不续目标、失联最坏停止、安全不确认、卡死/失电保护。100Hz与跨设备同步均不自动承诺；未配置真实安全/单位/限速时不开放输出。
