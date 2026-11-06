# Mobocon 接入：RS422 跟焦控制

2026-11-02网络版说明：本页保留历史串口固件和实板证据，不代表Control 0x22已接入或验收。当前上游为UDP，同UID串行、不同UID并发；下游PDMOVIE互锁不变。TS接口统一bigint UID，无node句柄，示例已更新；网络端点与发现流程见[SDK指南](../mobocom_realtime/SDK_INTEGRATION.md)。

日期：2026-10-28。公共 SDK `2.1.0-candidate.6`，线协议仍为 `0x21`。整合板正式固件 `0x00010007` / 空载台架 `0x80010007` 起实现 schema 4。本次交付固件和公共 SDK；**Mobocon 源码未修改，客户端接入尚未验收；2026-10-29 已完成无主板 USB、无云台电机条件下的 RS422 切有线与 RS485 跟焦网页实测，范围见文末**。拉取 `device_protocol/main` 后，按消费工程 `mobocom-sdk.lock.json` 的 `source_commit` 固定协议源码；随包 `sdk-manifest.json` 应对应相同提交且 `source_dirty=false`。候选版本号是SDK版本，不等同于Git标签。

## 当前接口：candidate.8 单 RS422 输出

2026-10-29：一体板正式 `0x00010008` / 台架 `0x80010008` 起，电脑只需已有RS422连接即可同时控制云台和跟焦。板端到电机的PDMOVIE链路仍保留；不再要求电脑独立连接RS485。固件需先升级，新网页读取schema4的`wiredCommands`能力，不满足时禁用位置/按钮并提示升级。

- 沿用下文的切源、身份与revision校验。Wired交接确认后，以同一RS422队列调用`Client.writeFocusCommand`；每笔输出都推进跟焦revision，下一笔必须使用新读回值。
- 位置values用`focusPositions(mask,[a,b,c])`编码：0～65535归一化位置。整合板将其四舍五入映射到PDMOVIE 0～7200；轴顺序为R1/G2/B3。网页仍显示电机协议位置0～7200，发送时转换，不扩大校准行程。
- 按钮values用`focusButton(0)`校准、`focusButton(1)`换速度档。整合板分别执行1次3000ms长按、5次200ms短按；需要实际外接按钮板。忙窗口分别3270/1870ms，切源和停止不能提前清掉。
- `writeFocusCommand(node,uid,boot,generation,epoch,revision,values,onRequest)`单次发送。ACK丢失只查询schema4；验证Applied/error=0、revision+1、原session/sequence及Wired。不能因超时新建第二次校准动作。
- flags增加bit3 wired_commands；新SDK兼容旧flags，新固件与旧SDK的严格flags解析不兼容，需配套升级。字节布局见[规范9.4.1](../mobocom_realtime/PROTOCOL.md#941-schema-4-有线输出扩展candidate8)。
- 本次参考网页已接入；Mobocon客户端源码未修改。单RS422正常路径已获用户实物确认，记录见下节；下文旧双接口实板记录按原范围保留。

### 2026-10-29 单 RS422 正常路径实板确认

2026-10-29用户已自行烧录并通过参考网页完成单RS422测试，反馈“测试没有问题”。只读日志确认正式固件0x00010008、node2、UID=211367605499624、boot48/epoch1；Wired接管sequence26对应revision2，R1=1508位置命令sequence46对应revision3，Stopped命令sequence136对应revision4，均关联Applied/error=0。最终outputIdle/busQuiet=true、wiredCommands=true。原始日志时间12:28:23～12:28:40 UTC，即北京时间20:28:23～20:28:40。

新版固件启动后旧网页后台曾报“跟焦切源状态布局”，重启加载candidate.8 SDK后正常。部署必须同步更新固件与网页后台。

参考实现为integrated_control_comm提交248714b、协议源a93a2e0；证据和摘要维护在整合板仓库`docs/rs422-focus-control-2026-10-29.md`及`docs/evidence/rs422-focus-2026-10-29.json`。本次位置输出是协议受理记录加用户实物反馈，非独立电机位置反馈或下游线路抓包。

采集时云台STATUS为FAULT/fault4、activeMask=0、三轴反馈无效，因此实板结论限于单RS422跟焦接管/位置/停止及公共云台故障隔离。云台实际运动并发、全行程、无线恢复、按钮互锁及异常/长期运行仍待专项实测；软件并发回归通过不替代这些实板项目。Mobocon自身客户端仍未修改或验收。线格式、SDK版本及控制代码本次不变。

## 历史接口：candidate.6/7 双接口切源

以下描述用于维护旧客户端及2026-10-29实板证据；新接入按上方candidate.8流程实现，不再新建电脑RS485发送服务。

## 1. 接口边界

使用现有一体板 RS422 连接：1000000 baud、8N1、src=0、默认 dst=2；以 INFO 的 UID/boot 识别主板，不能硬编码 COM 号或本次台架 UID。跟焦切源是挂在现有 Gimbal 节点上的板级功能，**不是云台 `SET_SOURCE`，也不是新建 FocusMotor 运动节点**。位置与按键协议仍由 Mobocon 的独立 RS485 端口直发 PDMOVIE 电机/按键板。

新增 schema `4`（FocusControl），复用 `READ_CONFIG=0x0050` / `WRITE_CONFIG=0x0051`、原帧格式/CRC/ACK/ERROR。读取 schema 4 成功才启用 UI；旧固件返回 UNSUPPORTED 时提示升级。通信超时/坏帧不等于不支持，保留连接与原始诊断。

云台处于 ACTIVE、REMOTE、SAFE、STOPPING 或 FAULT 时均可使用，未接 CAN 也可使用。不需 SESSION_OPEN，不自动 STOP、RESET_FAULT 或重新接管云台。配置期间仍要按原预算持续刷新云台目标。切源不改变云台 generation、epoch、owner、目标、限速，不续 target TTL / lease，不消除云台故障。板端只推进跟焦自己的代次。

## 2. 字节布局

全部小端、无 padding。READ_CONFIG 请求 payload=`04 00`，总帧29B。成功 RESPONSE payload 为31B，总帧58B：

| payload偏移 | 字段 | 类型 / 含义 |
| --- | --- | --- |
| 0 | schema | u16=4 |
| 2 | revision | u32，当前跟焦 generation，开机从1开始 |
| 6 | state | u8：Idle=0 或 Applied=2；见下文 |
| 7 | error | u16，正常0 |
| 9 | request_session | u64，当前代次对应的RS422切源事务 |
| 17 | request_sequence | u32，同上 |
| 21 | value_length | u16=8 |
| 23 | focus_source | u8：Wireless=0、Wired=1、Stopped=2、Fault=3 |
| 24 | flags | bit0 output_idle，bit1 radio_ready，bit2 bus_quiet；其余0 |
| 25 | reserved | u16=0 |
| 27 | button_remaining_ms | u32，已发出的按键动作忙窗口剩余毫秒，向上取整 |

`focus_source` 与公共 Source(None/Remote/Mobocom) 不同。`output_idle` 仅表示本板队列、UART发送和按键忙窗口已空闲，**不代表电机已停止或校准完成**。`radio_ready` 仅代表本地无线模块就绪，不证明手柄在线。`bus_quiet` 表示本板未发送/等待回显、RX缓冲/事件已排空、无待组帧，且至少20ms没观察到外部字节/线路错误；不是独占租约，无法阻止另一主站随后发送。

成功的远程切源同步推进 revision 一次，并关联 session/sequence，state=Applied。切到同一来源也推进一次，用于明确清除旧输入；同帧重试不重复执行。USB切源、合法外部RS485命令让出、故障、频道维护等改变跟焦generation后，schema 4 返回新revision、Idle及零关联，表示先前远程结果已被后续动作取代。读回的是当前来源，不把历史请求值冒充当前状态。

WRITE_CONFIG 请求 payload 固定35B，总帧62B：

| payload偏移 | 字段 | 类型 / 来源 |
| --- | --- | --- |
| 0 | uid | u64，INFO |
| 8 | boot_id | u64，STATUS，与INFO一致 |
| 16 | expected_generation | u32，**云台公共STATUS** |
| 20 | session_epoch | u64，公共STATUS，与INFO一致 |
| 28 | schema | u16=4 |
| 30 | expected_revision | u32，**schema 4返回的跟焦generation** |
| 34 | focus_source | u8：0无线、1有线、2停止；3及其它值拒绝 |

不要把两种generation互换。写入不保存NVS、不改变 `focus.startup`；重启恢复保存的启动来源。ACK仍为公共40B快照（整帧67B），ERROR为error:u16、detail:u16及40B快照（整帧71B）。ACK里的source/generation是云台状态，不能拿它判断跟焦已切换或可发送RS485。

## 3. Mobocon 调用顺序

**切到电脑有线：**

1. 暂停并排空电脑RS485目标/按键发送队列，保持暂停。RS422继续服务云台。
2. 单播INFO、READ_CONFIG(4)、STATUS，校验UID、boot、epoch，记录公共generation和schema 4 revision。
3. WRITE_CONFIG(4, source=1)。发送前保存session、sequence、完整帧及旧revision。ACK仅表示切源受理，不代表当前校准动作结束。
4. 每50ms以有界事务查询READ_CONFIG(4)，总等待建议5秒。确认身份未变、关联session/sequence匹配、revision=旧值+1、Applied/error=0、source=Wired，并且output_idle=true、bus_quiet=true，才允许启动电脑RS485发送。
5. 等待期间显示“切换中”及button_remaining_ms。超时显示“未确认，RS485发送保持暂停”，禁止自动发电机指令或另起写事务。

**切回手柄无线：**

1. 停止并排空电脑RS485位置、校准、换速及自动重发队列，保持发送关闭；等待READ_CONFIG(4)的output_idle=true、bus_quiet=true、radio_ready=true。
2. 重新读取guard/revision，WRITE_CONFIG(4, source=0)。按同样的事务关联回读，确认Applied、revision+1及source=Wireless即可；无线恢复后可能持续发送，所以**不要要求output_idle一直为true**。
3. 保持电脑RS485不发送，观察新手柄帧产生的控制；切源会清旧目标、按键边沿及无线FIFO，不回放旧动作。

source=2仅停止本板跟焦输出，不负责停止电脑RS485发送，不能当作PDMOVIE急停。云台STOP与跟焦停止仍各自独立。读取/切换均不包含跟焦位置或校准命令。

写超时可以查询本次关联结果；不要换新sequence重发“碰碰运气”。同帧同上下文重试由公共配置缓存去重，但返回缓存ACK后仍需查询当前状态；USB/外部切源可已覆盖原结果。UID、boot、公共generation/epoch不匹配、revision冲突或关联被覆盖时重新展示当前状态，由用户明确重新提交。schema 4在查重前严格校验公共guard，旧云台代次也不能命中成功缓存。全部配置schema共享写缓存，客户端串行化配置写。

云台运行期间使用SDK单次事务入口，插在最新目标ACK后的剩余TTL预算中；每次读或写后继续刷新目标。禁止为切源暂停整条RS422的目标流、循环重发旧配置或让后台轮询抢占目标。各u64在Rust保持u64，跨JSON用字符串，在TypeScript用bigint。

## 4. SDK与客户端改动点

SDK新增C++ `mobocom_focus_control.hpp`、Python `focus_control.py`、TypeScript `focus_control.ts`；提供独立FocusControlSource及严格codec。TypeScript Client新增 `readFocusControl` / `writeFocusSource`，每次只发送一次，不自动停云台、不自动保存或等待机械完成。

```ts
import {FocusControlSource} from '@mobocom/protocol'
// identity和status取自同一主板；先按上文暂停并排空独立RS485发送器。
const before = await client.readFocusControl(identity.uid)
const status = await client.getStatus(identity.uid)
if (identity.boot !== status.boot || identity.epoch !== status.epoch) throw Error('设备上下文已变化')
let pending // 应用需持久保留本次内存事务，即使write抛出超时也不丢失
await client.writeFocusSource(identity.uid, status.boot, status.generation,
  status.epoch, before.configuration.revision, FocusControlSource.Wired,
  request => { pending = request })
// ACK或通信超时后均由上层按第3节查询关联。
// 直到Applied + source=Wired + outputIdle + busQuiet，才启动RS485发送器。
```

Mobocon开发者需要修改（本次未修改客户端）：

| 模块 | 要求 |
| --- | --- |
| `src-tauri/src/commands/mobocom/protocol.rs` | 增加schema4、独立FocusControlSource、35B写/31B读payload编解码；沿用0x0050/0x0051与ACK/ERROR |
| `src-tauri/src/commands/mobocom/client.rs` / BusHandle | 增加单次查询/切源、发送前保留回执关联；复用现有RS422串行事务队列，不新开同一串口 |
| 跟焦RS485发送服务 | 有线接管确认前禁发；无线切换前终止并排空全部目标/按键/重发；切源结果未知时保持关闭 |
| `src/api/deviceCommands.ts`及后端路由 | 提供typed状态与切源方法；业务页通过统一API调用 |
| 连接/跟焦控制页面 | 有线、无线、停止三态操作；显示当前来源、切换中、按键剩余忙时间、无线模块就绪和明确错误 |
| 云台实时调度 | 将切源/回读放入目标间隙；保留云台会话与目标流，不调用云台SET_SOURCE或自动清故障 |

以上源码路径沿用此前接入记录，开发者以自己的当前客户端结构为准。先拉取协议仓库，再按整合固件lock记录的 `source_commit` 检出本次协议版本；固定SDK由该干净提交生成。接入时同时核对版本 `2.1.0-candidate.6` 和内容摘要，不只比较版本号。

## 5. 错误与验收

| 错误 | 处理 |
| --- | --- |
| MALFORMED(1) / RANGE(9) | UID、长度或source非法，修正输入 |
| UNSUPPORTED(2) | 设备未实现schema4，提示升级；不要回退云台SET_SOURCE |
| BOOT(4) / GENERATION(5) / EPOCH(20) / CONFLICT(8) | 丢弃旧上下文，重新读取，禁止旧请求继续控制 |
| SEQUENCE(7) | 请求序号过旧或复用错误，重新识别事务 |
| BUSY(11) | 频道保存中，或无线恢复时UART/按键/外部总线未空闲；保持RS485停发，显示状态 |
| SPACE(12) | 跟焦revision耗尽；停止后重启重新识别 |
| SAFETY(13) | 无线模块未就绪、存储/启动状态不允许恢复、USB保存待重启 |
| DRIVER(19) | 当前跟焦Fault直接请求无线，或跟焦IO无效；保留诊断，不循环强制恢复 |

软件验收覆盖：三语言codec和14条新增黄金事务，重复/旧帧、广播拒绝、guard/revision冲突、非法来源/长度、缺CAN/公共故障、ACTIVE目标与租约不变、按键忙窗口、旧无线队列清理、外部切源使历史关联失效。旧运动/频道/调速向量保持不变。

### 2026-10-29 实板补充（参考网页，非 Mobocon 客户端）

使用整合板正式固件 `0x00010007`，INFO boot=47、node=2；仅连接电脑 RS422 和 RS485，没有连接主板 USB 诊断口或云台电机。公共 STATUS 为 FAULT/fault=4、activeMask=0，仍能正常读取 schema 4，写入 Wired 并关联回读 Applied/error=0、revision 递增、output_idle=true、bus_quiet=true。随后独立 RS485 发送 R1=0，用户确认正确零端；用户继续使用参考网页测试后确认“测试没有问题”。

已验证：无需主板 USB 或云台电机即可完成跟焦状态读取、有线接管和位置测试。硬件确认是本次正常路径短时验收；用户未逐项反馈的网页操作不扩展为单独通过项。详细范围见[无 USB 跟焦实板记录](FOCUS_RS422_RS485_VALIDATION_2026-10-29.md)。

仍待验收：仅 RS422 切回无线后的跟随/校准专项记录；云台持续运动中的双向切源；按钮忙窗口的实物互锁；丢 ACK、拔线、重连、重启和频道保存后的异常恢复；RS485 线级无双主驱动；长期运行。Mobocon 自身客户端仍须有独立提交与验收证据。

客户端应让跟焦配置入口独立于云台运动准入：不能用云台 FAULT、反馈缺失、safeConfirmed=false 禁止 schema 4；但必须继续执行跟焦 guard/revision/关联结果和总线交接检查。界面区分“云台未连接”与“跟焦读取失败”。
