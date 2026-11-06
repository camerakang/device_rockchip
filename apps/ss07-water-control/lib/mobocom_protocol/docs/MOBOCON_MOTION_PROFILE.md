# Mobocon 云台调速接入说明

2026-11-02网络版说明：当前SDK为2.2.0-network.1，Control 0x22 UDP；下文固件版本、串口网页预算和台架证据属于历史0x21部署，不代表网络版已验收。schema2/3布局和安全规则保留，TS方法只接受一个bigint UID，不再传node句柄加UID。当前传输和调度以[SDK指南](../mobocom_realtime/SDK_INTEGRATION.md)为准。

版本：`2.1.0-candidate.5`，日期：2026-10-27。公共协议和整合板已实现；Mobocon 客户端由其开发者接入，本次未修改 `electron-vite-vue3`。规范以 [PROTOCOL 第9节](../mobocom_realtime/PROTOCOL.md) 为准，持续变更入口为 [MOBOCON_UPDATES](MOBOCON_UPDATES.md)。

## 范围与生效规则

### 位置工作范围与停车预留（整合板0x80010004/0x00010004）

设备配置的min_deg/max_deg表示保护边界；CAPS位置资源的minimum/maximum表示各内缩5°后的正常工作范围。例如保护±180°对应工作±175°，台架保护±185°对应工作±180°。线上仍为毫度。UI和所有目标生成器直接使用CAPS，不重复内缩，不硬编码范围；未配置或无有效工作区不准入。机械边界没有新增公开配置schema，schema2/3仍只修改速度/加速度。

实际反馈可以短暂或在停车后停留于预留区。UI照实显示反馈，不能用“反馈超出CAPS工作范围”替代设备fault。重新接管时完成有效反馈与固件状态检查，将初始目标约束为`max(minimum, min(maximum, feedback))`，由固件规划回工作区；不把越界反馈原样发给WRITE_TARGETS，也不把非法目标RANGE当成自动复位/重试接管的理由。显式新输入按CAPS提示/限制，停止后不得恢复旧会话目标。固件反馈保护仍使用真实配置边界，不能以目标约束绕过保护。

STOP取消目标/实时调速队列并等待STATUS安全确认、NONE和fault=0；ACK受理不等于停稳。台架窗口估速修复保留连续制动轨迹，停车可使用5°预留区。720/720空载实测最后三次确认耗时938/566/560ms，4687份USB状态无云台故障，实际停车最大绝对值181.727°仍在±185°内；生产机构、满载和任意速度下预留是否足够仍待验收。详见[更新记录](MOBOCON_UPDATES.md)。

schema 2 在停止保持后配置；新增 schema 3 允许当前控制会话在运动中实时改速。三轴可分别设置，但每次必须提交完整协调组。设置本身不会重新使能、接管或清故障。

新整合板：RS422、1 Mbps、8N1、node=2、Gimbal type=0x9007；endpoint=1、resource=1/2/3（Pan/Tilt/Roll）、group=1、unit=0x9001。线上速度为毫度/秒，加速度为毫度/秒²，UI显示度制。例如720°/s编码为720000；1°/s编码为1000；1°/s²编码为1000。按0.001精度转换为正i32后校验，不能发送浮点数或用速度0代替STOP。

空载固件 `0x80010004`（调速能力始于`0x80010003`）速度范围 **1～720°/s**、加速度 **1～720°/s²**，两项线上范围均为1000～720000（含边界），开机默认50/100。正式固件 `0x00010004` 的上限仍来自已批准机械配置。UI必须读取上下限，不能把台架范围写死给其他设备。

本次台架固件把电机位置模式限速从50扩为720°/s（12.566rad/s，裸轴ratio=1），反馈超速阈值为1152°/s；每次运行调速仅修改RAM规划参数，不写CAN限速、电流或PID。回中opcode=2使用独立预设50/100；STOP和必要的目标/软限位制动使用独立的720°/s²，可能高于用户运行加速度。台架CAPS.safeTimeoutUs为2000000，客户端始终按CAPS等待停止确认。±180°端点之间只有360°行程，即使设置720/720，理想静止到静止轨迹峰值也约509°/s；参数读写成功不能证明电机实际达到720°/s。实板已完成低速实时修改及新版本720/720空载运动中STOP复验，实际达到720°/s、超速边界和挂载机构仍未验收。

参数在STOP、故障复位、会话切换和断开重连后保留；重启恢复默认且revision=0。不提供保存开机默认。schema 2/3共享同一RAM参数、revision与最近受理事务；schema 1无线频道独立revision，所有schema共用配置写缓存，应串行化。

## 能力探测与兼容

分别用READ_CONFIG(2)和READ_CONFIG(3)探测。schema 3返回UNSUPPORTED时回退停止配置；schema 2也不支持才禁用整个调速功能。CAPABILITY表示当前硬件配置或控制准入不足；超时、CRC错误和串口占用是通信问题，不能当作旧固件。不能仅凭type或版本数字猜支持。

| 条件 | schema 2 | schema 3 |
| --- | --- | --- |
| 写入状态 | SAFE/NONE，owner=0，fault=0，safeConfirmed | ACTIVE/MOBOCOM，当前session=owner，fault=0，operation=0 |
| 会话 | 可未SESSION_OPEN，必须匹配guard | 必须已SESSION_OPEN且持有当前控制权 |
| 成功 | revision+1、generation+1、清目标，保持停止 | revision+1；generation/epoch/目标/owner不变 |
| 租约 | 不开启运动 | 不刷新target TTL或lease |
| 停止后旧帧 | 原停止配置的同帧可去重 | 旧generation的写在缓存检查前拒绝 |
| 动作/其他配置 | 校验真实安全条件和BUSY | 回中等动作或配置处理中返回BUSY |

## 线格式

当前使用Control 0x22、小端、无padding和原CRC。READ_CONFIG=0x0050，WRITE_CONFIG=0x0051。schema 1格式不变。schema 2与3布局相同，仅配置号不同；values中的source仍为2（Mobocom），不是schema号。

READ请求payload为 `02 00` 或 `03 00`。RESPONSE：

| 偏移 | 字段 |
| --- | --- |
| 0 | schema:u16，2或3 |
| 2 | revision:u32 |
| 6 | state:u8：Idle=0、Pending=1、Applied=2、Failed=3 |
| 7 | error:u16 |
| 9 | request_session:u64 |
| 17 | request_sequence:u32 |
| 21 | value_length:u16，三轴为87 |
| 23 | source:u8=2，persistence:u8=0，count:u8=3 |
| 26+28×i | endpoint:u16、resource:u16、vmax:i32、amax:i32、min_vmax:i32、max_vmax:i32、min_amax:i32、max_amax:i32 |

三轴读payload=110B、完整帧149B，不能使用128B固定接收上限。

WRITE请求：

| 偏移 | 字段 |
| --- | --- |
| 0 | uid:u64，来自INFO |
| 8 | boot:u64 |
| 16 | generation:u32 |
| 20 | epoch:u64 |
| 28 | schema:u16，2或3 |
| 30 | expected_revision:u32 |
| 34 | source:u8=2，persistence:u8=0，count:u8=3 |
| 37+12×i | endpoint:u16、resource:u16、vmax:i32、amax:i32 |

三轴写payload=73B、完整帧112B；ACK为40B payload、ERROR为44B。SDK通用codec允许1..7资源，本板必须恰好覆盖(1,1)、(1,2)、(1,3)，禁止漏轴、重复或未知资源。按endpoint/resource匹配，不依赖数组顺序。

设备校验全组、范围和revision后同步原子更新；失败不改任何轴或最近受理事务。首次回读可直接是Applied，不必先出现Pending。candidate.5取消原先由回中/停车期限推导的运行参数组合限制；回中和停车已使用独立参数，软限位仍逐周期检查。

## 停止后配置流程

暂停目标发布器、取消未发目标，由明确的STOP进入SAFE/NONE并确认fault=0、安全确认。核对INFO/STATUS中的UID/boot/epoch后读取schema 2。保留UI的旧revision和上下文，使用writeMotionProfile整组提交。应用后generation递增，仍保持停止；下次运动用最新STATUS与全新目标，不恢复旧曲线。

ACK后或写超时后只查询结果。发送前保存UID/boot/epoch、revision、session/sequence、完整帧和期望值。只有身份上下文未变，revision=旧+1、Applied/error0、request_session/sequence匹配且完整三轴值相等，才显示本次应用成功。相同值不能证明本次事务成功；超时、结果被覆盖或上下文变化时显示未确认，不自动回滚或新建写补发。

## 运动中实时调速流程

1. 连续目标控制已建立，STATUS为ACTIVE/MOBOCOM、owner=当前session、fault=0、operation=0。一次READ_CONFIG(3)探测能力并取得参数。运行中不要调用包含多页CAPS的inspect。
2. 草稿采用一个在途配置和一个最新待提交值；连续input事件合并，不能积压历史参数。每笔写/读只在最新目标ACK后、剩余target TTL足够的时隙插入，随后立即发送最新目标。
3. 单次WRITE_CONFIG(3)，带当前guard、revision和完整组。成功保持generation、epoch、目标与速度/位置状态；不能清目标、重开会话、暂停目标流或把新vmax直接裁剪到实际速度。
4. SDK实时方法只发送一次。ACK丢失后仍持续目标流，穿插READ_CONFIG(3)按上述事务关联确认；确认前不发下一笔写。参数写本身不刷新100ms目标TTL或300ms租约。
5. STOP/ESTOP、断连、会话/代次变化取消待发调速。另一窗口或会话不能热调速，回中动作期间BUSY。

当前网络预算包含112B写+79B ACK或41B读+149B回复，并分别加Ethernet/IP/UDP开销；网络排队和主机调度另留余量。历史串口网页频率不适用于本版本，Mobocon按实际设备数、状态/配置频率和实测网络重新验收。

参考实现：`integrated_control_comm/tools/gimbal_web/continuous.cjs`（ACK后时隙）、`speed-profile.cjs`（单次写与关联确认）、`public/app.js`（草稿合并）。不要求复制网页的自动故障恢复策略。

## SDK入口与客户端修改点

| 语言/模块 | 入口或改动 |
| --- | --- |
| C++ | mobocom_motion_profile.hpp：MotionProfileSchema=2、LiveMotionProfileSchema=3；原codec共用；DeviceCore配置钩子 |
| Python | motion_profile.py：MOTION_PROFILE_SCHEMA / LIVE_MOTION_PROFILE_SCHEMA、motion_limits、motion_profile；配合configuration_write |
| TypeScript | Client.readMotionProfile/writeMotionProfile（停止）；readLiveMotionProfile/writeLiveMotionProfile（实时单次） |
| 关联记录 | 写方法的onRequest提供node/session/sequence/frame副本，在第一次发送前保存；u64跨JSON使用十进制字符串 |
| src-tauri/src/commands/mobocom/protocol.rs | 支持schema 2/3共用布局、正i32、完整长度/资源校验；保留schema 1 |
| src-tauri/src/commands/mobocom/client.rs / BusHandle | 同一串口事务队列，给实时写/读提供有界单次时隙，STOP优先并取消未发送项 |
| 云台服务与路由 | 读取/停止应用/实时应用/查询确认，保护owner与guard；不隐式RESET或接管 |
| src/api/deviceCommands.ts | 新增实时类型，保留上下文、revision、关联事务、完整组与范围 |
| 云台界面 | 度制输入、动态范围、统一/各轴调速；草稿/待确认/已应用分开；当前会话运动中可修改，旧固件停止后配置 |

这些客户端路径来自之前核对的Mobocon基线，重构后需开发者重新定位。停止配置完整示例为 [motion_profile_example.ts](../mobocom_realtime/typescript/motion_profile_example.ts)。实时调用时由业务总线调度器提供辅助时隙，依次调用readLiveMotionProfile、writeLiveMotionProfile、readLiveMotionProfile并严格关联确认；不能直接在input事件里并发操作串口。新增 [实时黄金向量](../mobocom_realtime/live_motion_profile_vectors.json)，保留原schema 2向量。

## 错误处理与验收

UNSUPPORTED回退能力；CAPABILITY保留硬件准入限制；OWNER/SESSION/GENERATION/BOOT/EPOCH取消陈旧意图并重新识别；BUSY等待动作或配置结束；RANGE/GROUP/MALFORMED修正参数；CONFLICT重新读取；超时只查询已发事务。失败必须保留错误码及设备快照，不能自动STOP/RESET后重写。

STOP后分别核对安全确认和故障：`safeConfirmed=true`只证明类别停止条件满足，`fault!=0`仍须显示故障并禁止恢复旧目标。2026-10-27旧台架`0x80010003`的720/720测试在0.844秒内确认停止，但锁存Driver故障4，不能显示“无故障完成”；`0x80010004`后续修复和通过记录见上方范围章节。设定720°/s与实测峰值也必须区分；旧次±180°往返实测约480/481/497°/s。详见[客户端更新记录](MOBOCON_UPDATES.md)。

软件已覆盖三语言向量、owner与缓存、STOP后旧写拒绝、热写不续租、丢ACK后查询、目标流不中断、STOP取消队列、原子范围检查和动态制动。实板证据见整合板仓库docs中的验收记录。Mobocon客户端接入仍待开发者完成并做端到端验收；此前10°/s时Roll速度保护原因未定位，扩大设备上限不能当作旧问题已修复。
