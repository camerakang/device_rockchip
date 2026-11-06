# Mobocon 客户端更新与接入记录

## 2026-11-05：SS-07 RK3588 登记为 VIDEO_SONAR(0x9009) 网络节点（仅文档与类型登记）

`device_types.json` revision 2→3，新增正式类型 `VIDEO_SONAR=0x9009`（视频声呐系统），已重新生成四语言常量、向量与说明；Control 0x22、SDK 2.2.0-network.1、报文布局均不变。SS-07 的 RK3588 作为只读网络节点加入同一组播 239.255.33.21:47821：用占位 Profile（source_mask=4、单个占位资源、无动作）响应 DISCOVER/INFO/CAPS/STATUS，常态 SAFE/NONE，不含运动能力。契约见 [SS-07 视频声呐节点](ss07/video-sonar-node.md)，RK 实施任务见[任务书](ss07/rk3588-network-node-task.md)，黄金向量见 [ss07_node_vectors.json](ss07/ss07_node_vectors.json)。

Mobocon 待修改：按 UID 发现 type=0x9009 设备并记录回复源 IP；只发 DISCOVER/INFO/CAPS/STATUS、全局 STOP/ESTOP 和急停后的 RESET_FAULT，不对其发会话、来源、目标、保活、配置命令；全局 ESTOP 会让该节点锁存 FAULT/ESTOP(7)，解除急停时需一并 RESET_FAULT，FAULT 不影响取流；用同一 IP 访问 `https://<ip>:8443/api/v1/streams`、`/media/<path>/whep`、`rtsp://<ip>:8555/<path>`；去掉 API 密钥配置，WHEP 信令继续由 Rust 侧代理以接受自签证书；boot/IP 变化后重建拉流会话。免密钥依赖 RK 新增的 `lan-monitor` 只读模式（默认关闭，需 `allow_cidrs`）。

**RK3588 端尚未修改或部署，Mobocon 端尚未改造，lan-monitor 与节点服务均未实现；没有真实网络、六路出画或安全验收。** 软件验证：类型登记生成检查与 Python 全部单测通过；节点黄金向量由 Python 对照模型生成，并经 Rust DeviceCore 重放 5/5 逐字节一致。板端实现规定用 Rust（或 C++）SDK。占位资源是公共核心“至少一个资源”的限制所致；如需无资源的纯监视节点，应另行提议协议库改动。

## 2.2.0-network.1：新增纯 Rust SDK（接入候选）

新增 [Rust crate](../mobocom_realtime/rust/README.md)：纯 Rust DeviceCore、协议/类型注册表、配置 schema 1～4、轮询 Client/RealtimeScheduler、std UDP 适配与独立0x12 HostTimeline。Control仍0x22，原有 C++/Python/TS 线格式与黄金向量不变，不兼容旧0x21；不要求现有TS客户端因新增语言修改线上字段。SDK打包增加Rust源码、Cargo元数据和测试，排除target。

如 Mobocon 采用 Rust 后端，需新增有界周期I/O：next_transmission/feed/take_result，处理Ticket、Busy/Superseded、连接代次和超时；同UID串行、不同UID并发。scan每次一轮100ms；reconnect只启动重新发现/INFO确认，应用随后显式读STATUS核对boot/epoch。换址挂起后仍须用户显式open_session、切源、重新配置与新目标，禁止自动续播。多网卡组播出站路由须平台配置并实测。

流程：发现并检查必需UID/冲突/容量 → INFO/CAPS/STATUS → 用户安全确认/open_session → 显式MOBOCOM来源 → 完整目标调度 → finish等待applied后STOP并确认SAFE。配置写均单次发送，首次发送前留存完整帧与关联，超时只读确认；实时调速/跟焦插入目标ACK后的可用TTL预算。实际调用差异见Rust指南，不能把TS Promise接口机械改名。

验收：Rust编译、no_std检查、Clippy、软件向量/差分与本机UDP检查通过；Python/TS回归通过。Linux/GCC13.3既有C++ `protocol_motion_profile`失败，未修改HEAD独立副本也失败，本任务不擅自修订该状态机或重写向量，四语言整体验收仍受阻。**Mobocon应用尚未实现/验收Rust接入，未升级相邻固件或网页、未烧录、未完成真实网络/机械安全验收。**

## 2026-11-03：robot_track 加入 Control 连接拓扑（仅文档）

[连接拓扑](../README.md#连接拓扑)增加与云台、跟焦平级的轨道车节点；[轨道车接入说明](ROBOT_TRACK_INTEGRATION.md)明确 Control 网口与内部 EtherCAT 专用网口分离，整套轨道车仅一个 UID，不经过其它接收器或 SS-07 RK3588 转发。类型沿用正式 `RAIL_CAR=0x9006`，Control `0x22`、SDK `2.2.0-network.1`、注册表和报文均不变，不更新消费工程快照。

客户端待核对／实现：按 UID 发现并校验类别版本；将位置资源 1/1 的 µm 转换为界面 mm；1/2、1/3、1/4 只读；读取 CAPS 的当前 Range、限速和动作超时；按设备契约安全建会话、切源、ENABLE、持续刷新位置目标及 STOP。当前轨道车只支持 MOBOCOM，SET_VMAX / SET_AMAX 要求驱动器失能，不推定支持公共 schema 3 实时调速。换址后只读验证重连，用户显式重新授权，不重播旧目标。

多设备同网不代表机械同步或故障整组联停；轨道车跟随延迟与驱动响应须实测，编排策略另行确认。类别资源、Action 与安全语义以 robot_track 仓库 `docs/MOBOCOM_CONTRACT.md` 为准。**本次仅修改文档，未修改或验证 Mobocon 源码，未部署、烧录或执行实网／机械安全验收，不代表客户端接入完成。**

## 2026-11-02：按UID换址重连与运动重新授权

Control保持0x22/26B发现布局，设备端Python/C++核心和黄金向量不变。TS Client不再把缓存旧IP与本轮新IP直接判为重复设备：发现换址/boot变化先挂起该UID，以独立只读会话INFO验证候选/旧地址；单一有效UID映射更新，多个在线地址确认同UID才冲突，无有效匹配报告unconfirmed。历史冲突消失后可在同一Client重新扫描解除地址歧义。

新增`reconnect(uid, rounds=2)`，只执行DISCOVER/INFO/STATUS，返回device/endpoint/status/needsRearm；应用检测失联后限频/退避调用，不内置无限重试。scan报告增加reconnected/unconfirmed，getEndpoint返回地址副本。验证匹配完整事务、来源及帧头/载荷UID，不能信任mDNS名或只看IP。稳定地址扫描不影响控制；只挂起变化的UID。

挂起时取消旧排队/在途请求及重试、清目标/安全凭据、递增connectionVersion，拒绝目标/保活/来源/动作/配置写。重连不自动STOP/RESET/OPEN/切源；用户显式安全建会话成功才解除needsRearm，再显式接管和提供新目标。旧RealtimeScheduler槽位即使随后重新建会话也不能复活，start/setTargets/finish拒绝旧连接代次；自动会话轮换跳过待重接管设备。其他UID继续运行。

本次为SDK实现，不代表Mobocon应用已接入或实网验收通过。调用流程、网络分区/丢包限制和人工恢复要求见[换址重连](NETWORK_DISCOVERY.md#换址重发现与重连)；精确软件验证结果见[REPORT](../mobocom_realtime/REPORT.md)。

## 2026-11-02：所有状态后台发现与动态地址

Control保持0x22，DISCOVER保持26B。C++/Python取消SAFE限制，SAFE、SWITCHING、ACTIVE、STOPPING、FAULT均响应；不清故障、不改变控制权/目标/动作/缓存，也不续租。TS scan/discover改为独立后台收集，不再占用控制队列；同时只允许一次扫描，扫描期间拒绝会话轮换，关闭取消扫描，全体停止仍立即发送并使扫描失效。

设备推荐DHCP，不再要求写死IP；Mobocon按UID学习回复源endpoint。初版将地址变化锁定冲突的限制已由上方“按UID换址重连”更新替代，不再要求重建Client，仍禁止自动恢复运动。可选mDNS `_mobocom._udp`提供名称及候选地址，Control回复校验UID后合并显示；本机别名优先，缺失时回退类别+短UID。GitHub方案、TXT约定、生命周期和验收要求见[动态地址与名称发现](NETWORK_DISCOVERY.md)。

本次修改协议库/SDK、向量和测试，未修改消费固件或Mobocon应用。DHCP/mDNS socket适配与名称UI属于待接入工作，不把文档方案当成应用已实现。运行中扫描的物理输出抖动、拥塞及安全仍需实网验收。

## 2026-11-02：2.2.0-network.1 纯网络 Control 0x22

文档补充：[网络迁移清单](NETWORK_V22_MIGRATION.md)集中列出帧/SDK差异、主控与固件迁移顺序、部署及验收要求；SS-07接入、系统拓扑和云台设计已同步为0x22 UDP。此次补充只修改文档，不代表SS-07或Mobocon消费工程已迁移。历史实板记录保留其原测试范围。

破坏性变更：上游Control从串口总线迁移为Ethernet/UDP，帧头37B、总长39+payload、最大279B，src_uid/dst_uid为u64。旧0x21帧、node_id、ASSIGN_ID、串口Parser/排空/发现时隙不再支持；时码0x12及设备私有PDMOVIE/CAN驱动不变。历史条目的串口接入和实板结果不能作为当前网络版本验收依据。

客户端需要替换串口服务为UDP数据报适配器，选择controlInterface，固定组播239.255.33.21:47821、TTL=1。每个数据报一帧，feed携带源endpoint；按UID维护当前IP:port映射，发现冲突禁止发送。UID必须无损存储，TS用bigint、JSON用十进制字符串；禁止number节点句柄与UID混用。

流程：scan/discover或显式endpoint配置（默认2轮、每轮100ms）→检查必需UID名册、冲突与设备类别→INFO/CAPS/STATUS确认SAFE和真实安全→单播openSession(uid,boot,epoch,generation)→查询新上下文→显式接管。INFO为33B，DISCOVER为26B且UID在帧头，SESSION_OPEN为20B。协议不分配IP，推荐网络适配器使用DHCP。

TS Client以sequence独立分发事务，同UID单播串行、不同UID并发，扫描后台收集不阻塞控制事务。组播STOP/ESTOP立即发送空payload；普通超时最多同帧重试两次。配置方法移除重复node参数，ConfigurationRequest/Schedule/finish键统一uid:bigint。RealtimeScheduler不再有baudRate，预算改为100Mbps Ethernet/IP/UDP封装，保护期限仍需实测。

软件验收使用Python参考模型、C++黄金向量重放、TS向量/并发/UID/调度测试及隔离SDK构建；精确结果见本次提交验证记录。[接入指南](../mobocom_realtime/SDK_INTEGRATION.md)已同步。**Mobocon应用尚未实现/验收本次迁移，消费固件未自动升级，真实网络与物理安全未验收。** 主控与全部参与设备必须同步迁移，不能混用旧版控制帧。

## 2026-10-29：candidate.8 单 RS422 跟焦输出

一体板0x00010008/台架0x80010008新增schema4有线位置/按钮命令，电脑可通过同一RS422连接控制云台和跟焦。SDK新增`focusPositions`、`focusButton`、`Client.writeFocusCommand`及状态`wiredCommands`。位置归一化为0～65535；私有PDMOVIE转换由板端实现。保留来源、guard/revision、按钮忙窗、配置去重和云台目标流隔离。

客户端：先升级SDK、探测wiredCommands，完成Wired交接，再通过同一事务队列发送；每次以revision+1及session/sequence关联读回。旧固件禁止输出；新固件flags扩展需升级旧SDK。丢ACK只查结果，不重发物理动作。取消独立电脑RS485连接UI。参考网页已实现；Mobocon应用仍需独立适配提交。用户已自行烧录并确认单RS422跟焦正常路径测试无问题；日志确认正式0x00010008/boot48，接管、R1=1508输出及停止分别关联revision2/3/4且Applied/error=0。采集时云台FAULT/fault4且无有效反馈，云台实际运动并发、全行程、无线恢复和按钮互锁等仍待专项实测。参考网页需重启加载新SDK；Mobocon独立客户端验收状态不变。详见[跟焦控制说明](MOBOCON_FOCUS_CONTROL.md)。


本文是协议库面向 Mobocon 开发者的持续更新入口：每次影响客户端的协议变更，在此记录需要修改的模块、调用流程、兼容要求和验收结果。最新更新放在前面；字段与公共状态机以 [PROTOCOL](../mobocom_realtime/PROTOCOL.md) 为准，SDK 使用见 [SDK_INTEGRATION](../mobocom_realtime/SDK_INTEGRATION.md)。

本文中的“建议新增”是客户端待实现的设计，不是现有 Mobocon API。只有提供客户端提交和验证证据后，才能将接入状态改为已完成。

## 2026-10-29 · 跟焦无 USB 正常路径实板验证（协议不变）

正式整合板 `0x00010007`，未接主板 USB、未接云台电机，仅通过 RS422（1Mbps）读取/切换 schema 4，通过独立 RS485（38400、8N1）测试 R1。云台 FAULT/fault=4 未阻止跟焦配置；有线切源已关联确认 Applied、revision 递增及输出/总线空闲，R1 零端由用户确认，参考网页后续测试获用户确认正常。完整边界见[实板记录](FOCUS_RS422_RS485_VALIDATION_2026-10-29.md)。

客户端修改要求：分别识别 RS422/RS485 串口，避免误选系统蓝牙虚拟串口；未成功识别一体板前禁用跟焦读写；跟焦状态提示不应被云台缺 CAN 的故障提示覆盖。schema 4 门控独立于云台运动使能，既有跟焦来源/事务关联和发送互锁不变。参考实现为整合板 `tools/gimbal_web/focus-control.cjs`、`public/focus.js`，不是 Mobocon 已交付的客户端实现。

SDK 仍为 candidate.6，线格式仍为 0x21；本次仅补充文档，不改变协议、三语言代码或消费工程固定快照。**Mobocon 源码与客户端验收仍待完成**；无线恢复专项、云台运动并发、按钮忙和异常恢复等场景不能由本次正常路径测试代替。

## 2026-10-29 · candidate.7：设备本地超时恢复

新增C++/Python `DeviceCore.recover_timeout_fault(now)`，由设备工程显式调用。风机0x00020103启用：来源超时/目标过期在安全确认后自行回SAFE/NONE，即使电脑已断开；其他设备不会因升级SDK自动启用。线上版本仍0x21、字段/CRC不变，风机类别仍3。

Mobocon针对该风机停止旧目标发布并查询恢复结果，不再依靠发送RESET_FAULT。恢复只到待机；必须由用户启动新控制，用最新generation，不自动续播。旧主控遇到RESET竞态时查询SAFE即可，不能循环清错/接管。网页已增加按固件版本的只查询处理；Mobocon应用源码未修改，正式客户端接入未验证。板端烧录与COM79结果见风机工程本地恢复报告。

## 2026-10-28 · candidate.6：RS422直接切换跟焦有线 / 无线

整合板正式`0x00010007`、台架`0x80010007`新增schema 4，通过现有RS422节点执行跟焦有线/无线/停止及状态回读，不再依赖USB控制台。SDK、三语言codec、单次Client接口和14条新增黄金事务已实现。**Mobocon源码未修改、客户端验收待完成；新接口部分正常路径已于2026-10-29实板确认，见上方补充。** 拉取`device_protocol/main`，按整合固件lock的`source_commit`固定本次源码；交付快照应为candidate.6、`source_dirty=false`，内容摘要与manifest/lock一致。旧`v1.0.6-bench.1`不包含此接口。

开发者按[完整跟焦切源接入说明](MOBOCON_FOCUS_CONTROL.md)修改协议codec、RS422事务服务、RS485发送互锁、typed API及跟焦来源UI。使用READ_CONFIG/WRITE_CONFIG的schema=4，不调用云台SET_SOURCE、STOP或RESET来切跟焦；不改现有PDMOVIE位置/校准帧。原RS422只有一个串口所有者，配置写/查询插入云台目标流的空闲时隙，保持目标TTL和租约。

关键验收：有线切换确认关联、revision+1、Applied、Wired及output_idle/bus_quiet后才开放RS485；无线切换前停发并排空电脑RS485队列；校准忙时等待；丢ACK仅查询；旧revision/旧boot/非法来源拒绝；云台运行与故障时不因切跟焦而改变公共会话或目标。运行来源不保存NVS，断电仍恢复既有startup。SDK与固件软件检查记录见整合板`docs/focus-source-rs422-validation.md`，硬件结果须后续独立记录。

## 2026-10-27 · 测试网页迁入整合板仓库

Mobocon 开发者现只需拉取 `integrated_control_comm` 的 `main` 分支至 `de626f2` 或后续版本，即可同时取得固件和 `tools/gimbal_web` 参考实现。仓库地址为 `https://codeup.aliyun.com/629f6370487c500c27f60603/integrated_control_comm.git`。原云台仓库 `interfocal/virtual_lens_radio_comm` / `codex/gimbal-live-360` 仅保留网页迁移说明；迁移前网页版本为 `8379591`。

整合板仓库根目录运行 `npm.cmd --prefix tools/gimbal_web ci`、`npm.cmd --prefix tools/gimbal_web start`；默认网页地址为 `http://127.0.0.1:8765/`，整合板选择 USB→RS422、1000000 bps。先断开并停止旧网页服务，再启动新目录；本机端口占用时可通过 `GIMBAL_WEB_PORT` 指定其他端口。

参考文件为 `tools/gimbal_web/continuous.cjs`（目标与配置时隙）、`speed-profile.cjs`（事务确认）、`controller.cjs`（CAPS 范围与接管初始目标）和 `public/app.js`（输入合并与页面）。SDK、线格式、1～720 调速范围及 5° 工作区策略均未因迁移改变；candidate.5 固定快照随网页提供。

迁移后41项网页测试、SDK摘要和文档链接校验通过，新目录服务已完成页面加载及串口枚举验证；本次未执行电机运动或烧录。Mobocon 源码未修改，其接入验收仍由客户端开发者完成。

## 2026-10-27 · 工作范围内缩5°与STOP修复（线格式不变）

整合板台架`0x80010004`、正式构建`0x00010004`增加工作区预留：CAPS中各位置资源的minimum/maximum已是保护边界向内各缩5°后的合法目标范围。例如保护边界±180°时CAPS返回−175000/175000；当前裸轴台架保护边界±185°，因此CAPS返回−180000/180000。线上位置单位仍为毫度，**客户端不能再减一次5°**，也不能把CAPS直接当作原始机械保护边界。

Mobocon需要同步以下处理：

1. 滑块、数字输入、往返端点、时间线求值和新接管初始目标均使用最新CAPS范围；重连、重启及重新读能力后刷新。越界输入提示或约束目标，保留有效控制会话；不得把RANGE当作必须反复STOP/复位/重新接管的故障。
2. **停车反馈可能合法地处于预留区。** 接管前读取有效反馈；例如反馈180.6°而工作上限180°，新目标取180°，由固件从当前位置规划回工作区。反馈显示仍为180.6°，不能篡改读数。缺失/过期反馈、固件真实保护故障不允许借此接管。使用完整三轴目标，丢弃旧会话目标和往返任务。
3. STOP先取消目标/实时调速队列，再以当前guard发送并等待STATUS的safeConfirmed、NONE及fault=0。ACK只表示受理；不得以速度0代替STOP。停止期间允许设备利用预留区制动，不应仅因反馈超过CAPS工作范围就报机械越界或循环复位。

固件修正的是台架窗口估速在减速段重建停车轨迹引发的Driver误判。反馈与近期指令轨迹相符时连续减速，真实越界、失联、超速和电机故障仍保护。5°是设备工作区策略，不是任何负载/速度都足够的制动距离，也不是新增可远程修改机械边界的配置接口；未配置机械范围仍不准入。设备尚无公开的机械边界读写schema，不能把schema2/3调速写入当作范围配置。

验收：同一整合板boot22通过COM78/1Mbps网页进行720°/s、720°/s²、±180°往返及5次运动中STOP；4687份USB状态fault全为0，CAN/RS422错误0。最后三次STOP到安全确认938/566/560ms；最高停车位置绝对值181.727°，处于±185°保护范围内。网页原先直接以反馈建目标导致的RANGE重试已修复，并在180.6°停车现场验证重新接管成功。最终SAFE/NONE、safeConfirmed=true、fault0。完整证据见整合板`docs/gimbal-stop-margin-acceptance.md`；参考网页`integrated_control_comm/tools/gimbal_web/controller.cjs`。

公共协议/SDK仍为candidate.5，无线格式或三语言SDK变更，不更新固定快照。**Mobocon源码未修改、客户端接入尚未验收**；本轮不证明生产机械配置、满载和长期稳定性通过。下方旧版本失败记录保留。

## 2026-10-27 · candidate.5补充：720/720空载运动与STOP旧版本结果

整合板既有台架`0x80010003`、boot21，COM78/1Mbps网页三轴720°/s、720°/s²、±180°往返约38.49秒，每轴至少10次完整往返。1769份USB反馈中Pan/Tilt/Roll绝对速度峰值为479.677/480.866/496.647°/s，实际位置均在±180.04°内；360°行程按720°/s²加减速的理想峰值约509.117°/s，不能把720设置值显示成实际测速。

STOP TX到安全确认0.844秒，但STOP ACK已带Driver故障4；最终FAULT/NONE、safeConfirmed=true，三轴仍使能保持。因此**已停止保持，不等于无故障停车通过**。CAN/RS422错误计数为0。停止时Roll的窗口差分速度推算制动终点超出软限位，与减速段速度估计滞后的现象相符，尚未修复复验。完整证据及限制见整合板仓库`docs/gimbal-720-motion-acceptance.md`。本轮重启后默认50/100回读已确认；Mobocon客户端仍未接入验收。

客户端需要分别展示“设定上限、实测速度、停止安全确认、故障码”，保留STOP ACK和后续STATUS；safeConfirmed=true且fault非0时显示“已停止，存在故障”，禁止自动恢复旧目标或把任务标为无故障完成。本轮不改变线格式或SDK，不升级消费工程快照，不修改Mobocon源码。

## 2026-10-27 · candidate.5：速度/加速度 1～720 与运行中调速

新增 `READ_CONFIG/WRITE_CONFIG(schema=3)`，值布局与 schema 2 相同，共享 RAM 参数与 revision。schema 2 继续只在停止后写；schema 3 只接受 ACTIVE/MOBOCOM 的当前控制会话，无运行中动作。成功不改 generation、不清目标、不续 target TTL/lease，STOP 后旧帧拒绝。

整合板空载固件 `0x80010003` 提供速度 **1～720°/s**、加速度 **1～720°/s²**，线上两项均为1000～720000（含边界），默认仍为50/100。正式构建 `0x00010003` 仍受各轴已批准机械配置上限限制，客户端始终读取设备范围。低运行加速度不用于回中/停车；回中50/100（台架），独立制动720。台架电机位置模式限速同步为720°/s，反馈超速阈值1152°/s，安全确认期限为2s；客户端按CAPS的 safeTimeoutUs 等待。720为设置上限，±185°短行程下不保证达到720°/s，高速制动及超速边界尚未实板验收。

实板验收（2026-10-27）：COM77同一整合板烧录 `0x80010003`，boot20；COM78/1Mbps网页读取三轴1～720范围。停止状态下720/720、1/1、50/100分别回读确认，revision1/2/3；三轴±30°往返中实时改25/50、1/1，revision4/5，boot/epoch/generation一直为20/2/2，目标继续刷新。STOP后generation变3并安全确认；1/1参数下回中到零，随后零位保持时实时写720/720及50/100，revision6/7。两段512份USB状态无云台故障、CAN及RS422错误，最终50/100、SAFE/NONE，串口已释放。720上限只验证参数读写，未验证实物达到720°/s；低速均匀性、满载、高速制动和重启默认恢复仍未验收。完整证据在固件仓库 `docs/gimbal-live-speed-acceptance.md`。Mobocon客户端尚未接入。

Mobocon 开发者需增加实时能力探测、动态范围、运动中参数输入、单次有界事务插入、最新值合并、关联读回和STOP取消。TS新增 `readLiveMotionProfile/writeLiveMotionProfile`，旧固件只回退停止配置。以1Mbps三轴50Hz目标/5Hz状态为例，网页最多4组写读/秒，并预留总线预算；51200诊断链路只开放停止配置。详见 [完整接入说明](MOBOCON_MOTION_PROFILE.md)。

软件验证：公共C++三个测试程序、Python51项、TypeScript469项；网页40项仿真/行为测试；固件两个环境各68项云台测试、各29项整合测试。实时黄金向量覆盖owner、原帧去重、STOP后旧写拒绝以及控制代次与租约保持。实板记录由固件仓库维护；**Mobocon源码未修改、客户端接入仍待其开发者完成**。

## 2026-10-27 · candidate.4：云台运行速度与加速度

**协议库和一体板固件已实现调速，并通过测试网页完成新台架固件的实板调速。Mobocon 开发者按 [完整接入说明](MOBOCON_MOTION_PROFILE.md) 更新客户端；Mobocon 源码未修改，客户端尚未验收。** 此条取代上一轮未实现的设计建议。

| 项目 | 本次变更 |
| --- | --- |
| SDK / 线版本 | `2.1.0-candidate.4` / 仍为 `0x21` |
| 读取/写入 | 沿用 `READ_CONFIG=0x0050`、`WRITE_CONFIG=0x0051`，新增独立 schema 2；schema 1 双频道不变 |
| 数据 | 三轴完整协调组的当前 vmax/amax、可调上下限、revision、事务关联；本板单位为毫度/秒、毫度/秒² |
| 条件 | SAFE/NONE、fault=0、安全确认；允许停止保持使能，Active/FAULT 不接受调速 |
| 生效 | 同一任务验证全组三轴后原子应用 RAM，成功 generation+1、清旧目标，不接管或重新启动 |
| 持久性 | 本次启动有效；STOP/会话切换保留；硬件重启恢复默认；不写 Flash |
| SDK | 三语言 codec、C++/Python 公共安全门槛、TypeScript Client.readMotionProfile/writeMotionProfile；发送前 onRequest 留存事务支持全丢 ACK 回读 |
| 固件 | `integrated_control_comm` 正式 `0x00010002` / 空载 `0x80010002`；台架启动默认仍为 50°/s、100°/s² |
| 兼容 | 使用 READ_CONFIG(schema=2) 探测，不凭类型或固件大小猜支持；CAPS 硬上限和旧目标格式不变 |
| 交付状态 | 源码提交见本仓库 Git 历史；固定 SDK 包的内容摘要、source_commit 与 source_dirty 以随包 manifest 和消费工程 lock 为准 |

开发者优先阅读 [MOBOCON_MOTION_PROFILE.md](MOBOCON_MOTION_PROFILE.md)：包含完整偏移/长度、Rust/Tauri 修改位置、UI 约束、调用顺序、SDK 示例、错误处理和验收清单。三轴写 payload=73B、读 payload=110B；不要复用 schema 1 的固定 36B 写长度。u64 跨 JSON 继续用十进制字符串。

写 ACK 后必须回读匹配 request_session/sequence、revision、Applied 和三轴值；发送前保留事务，写超时只查结果。TypeScript 已提供可选 onRequest 回调，Rust 端需要等效实现。下一次运动使用最新 generation/epoch 和全新目标。Mobocon 客户端接入仍待验收。

2026-10-27 网页验收：COM78 / 1 Mbps / node2，UID `0000C03CDB0AF6E8`，新台架 INFO=`0x80010002`、boot19。25°/s、50°/s² 与 50°/s、100°/s² 两组写入均经关联读回确认，再分别完成三轴 ±30° / ±180° 往返、回中及 STOP；436 份 CAN/USB 状态无故障，总线错误为 0。另将三轴分别设为 20/30/40°/s、40/60/80°/s²，断开重连保持通过；最终恢复 50/100，revision=4、Applied/error0 并停止。重启检查因电机仍使能保持被维护门槛拒绝，实板默认恢复未验证。完整证据在固件仓库 `docs/gimbal-speed-web-acceptance.md`。网页参考实现为 `integrated_control_comm/tools/gimbal_web` 的 `speed-profile.cjs` 与 `public/app.js`，无需照搬它的自动恢复策略。

此前 10°/s 的 Roll 速度保护问题尚未定位；新接口只改变运行规划参数，保护硬上限和 STOP 制动参数独立，不能把提高保护阈值当作旧问题已修复。

## 2026-10-26 · candidate.3：通过 RS422 配置双路无线频道

### 交付与接入状态

| 项目 | 本次基线 |
| --- | --- |
| SDK / Control 线版本 | `2.1.0-candidate.3` / `0x21`；时码仍为独立的 `0x12` |
| 公共协议实现提交 | `4bee97dbd0964694a4748b922c7bbbe382eab83c`，本仓库 `main` |
| 固件仓库 / 提交 | `integrated_control_comm` / `08cdcff58e9ce7728b31a24be2581badc6cd512b` |
| 支持固件 | 该接收板 `INFO.firmware >= 0x00010001`；仍须用 `READ_CONFIG` 探测扩展支持 |
| Mobocon 核对基线 | `electron-vite-vue3` 仓库，提交 `14136edc81490c0725807b20cb3bf2d36397ff77` |
| 当前状态 | 公共库、板端已实现；板端已做实板联调；**Mobocon 客户端尚未接入、尚未验收** |
| 本次文档维护 | 未修改 Mobocon 源码；下述客户端路径基于以上提交核对，后续重构时需重新定位 |

客户端仓库地址：`https://gitee.com/yangjianxun1998/electron-vite-vue3.git`。该基线实际为 **Tauri 2 + React 19 + TypeScript + Rust**，设备串口事务在 Rust 层。

固件仓库地址：`https://codeup.aliyun.com/629f6370487c500c27f60603/integrated_control_comm.git`。其中 `docs/mobocon-radio-config.md` 是板级接线与完整报文说明，`tools/radio_config.py` 是独立参考客户端，`docs/validation.md` 记录实板证据；这些路径属于固件仓库。

### 功能与连接参数

无屏幕时，通过电脑的 USB→RS422 适配器读取、保存接收板的“跟焦手柄频道”和“云台摇轮频道”。两项作为一个配置一起提交，允许取不同值。改频道不包含电机使能、位置控制或标定。

| 参数 | 接入要求 |
| --- | --- |
| 串口 | 用户明确选择；`1000000` baud、8N1、无软/硬件流控；不可缺省回退到 COM1 |
| 寻址 | 源节点 `0`；本板默认目的节点 `2`，按实际配置寻址；两条配置命令只允许单播 |
| 节点类型 | 当前整合板沿用 `DeviceType.Gimbal = 0x9007`，不新增跟焦运动节点 |
| 设备身份 | 使用 INFO 的 UID 和 boot；COM 号仅用于打开端口，不能代替身份 |
| 配置 schema | `1`，DualRadio |
| 频道 | 线协议整数 `0..15`；界面显示通道 `1..16`，发送减 1、读回加 1 |
| 就绪状态 | 每路 ready 仅表示本地无线芯片就绪，不表示手柄/摇轮在线 |

现有设备的 51200 波特率入口保持各自定义。不要为了新板改动全部串口的默认速率，也不要复用已有的电机通道、RS485 node_id 或 CAN ID 字段存储无线频道。本板的射频寄存器映射为逻辑频道 × 8，由固件处理，客户端发送逻辑频道即可。

当前整合板上的跟焦运动路径是“手柄 nRF24 → 本板 → PDMOVIE RS485”。本次维护扩展挂在已有 Gimbal 节点上，没有把私有跟焦运动改成公共 Gimbal 资源，也不能据此建立一个虚构的 FocusMotor 节点。

### Mobocon 修改清单

| 修改位置（相对 Mobocon 仓库） | 开发者需要完成的内容 |
| --- | --- |
| `src-tauri/src/commands/mobocom/protocol.rs` | 增加 `ReadConfig=0x0050`、`WriteConfig=0x0051` 命令和 `command_name` 映射；实现配置快照、36B 写入 payload 的严格编解码；独立定义配置状态与私有跟焦来源枚举 |
| `src-tauri/src/commands/mobocom/client.rs` | 在 `handle_reply` 的读命令集合加入 `0x0050`，接受 RESPONSE；写入仍校验 ACK。发送前留存本次写的 session、sequence、完整帧及预期值，超时后仍可查询关联结果 |
| 同文件的 `MobocomClient` / `BusHandle` | 复用单串口线程与串行事务队列；同帧重试保持字节不变；为维护流程提供可取得写入关联信息的接口。当前 `BusHandle::exchange` 失败返回错误，不能假设失败后已拿到成功回执 |
| `src-tauri/src/commands/` 下建议新增维护服务和命令路由，并在 `mod.rs` 注册 | 提供连接、读取、保存、查询本次结果、断开；使用通用串口设施，管理端口独占和维护状态，不触发运动接管或自动故障恢复 |
| `src/api/deviceCommands.ts` | 按现有约束添加 typed 封装，内部调用 `deviceApi.send`；业务页面经 `@/api` 导入，不直接 `invoke` 或操作串口 |
| `src/subWindow/content/modal/communication_settings/index.tsx` | 增加“一体板无线频道”设置区：串口、节点、两路频道、保存进度、两路 ready、跟焦来源。现有 `IpRow.channel` 是总线节点地址，必须保留其语义 |
| `src/tauri.ts` 及现有串口枚举设施 | 复用 `networkApi.listSerialPorts()` 供用户选端口；连接生命周期结束释放端口。若设计为新窗口，按客户端约定同步 `src/config/windows.json` |

`gyro_route.rs` 的 `gyro_init` 用于既有云台运动初始化，固定旧入口语义且缺省会回退 COM1。新维护功能不能借此初始化：它需要在云台未配置、没有 CAN、公共节点处于 FAULT/NONE 时也能工作。不要先调用 `SESSION_OPEN`、`SET_SOURCE`、`RESET_FAULT`、使能动作或完整运动 CAPS 预检来“准备”配置。

建议新增应用命令名为 `radio_config_connect`、`radio_config_read`、`radio_config_write`、`radio_config_result`、`radio_config_disconnect`。这些只是 **Mobocon 内部路由建议名，尚未实现**；线上只有下文的正式协议命令。保存任务应由原生层持有，退出页面时结束轮询并释放连接；若设备结果尚未确认，保留“结果未确认”，不能宣称已取消设备端保存。

### 数据合同与枚举

复用现有 `0x21` 帧、CRC 和 ACK/ERROR。所有多字节字段小端、无 padding；帧总长 `27 + payload_length`，payload 上限 240B。完整帧布局和 CRC 范围见规范第 2 节。接收层需同时核对地址、session、sequence、command 和 kind，并支持半包、粘包及噪声。

`READ_CONFIG (0x0050)` 请求 payload 为 `schema:u16`，schema 1 即 `01 00`。成功 **RESPONSE** payload 固定 27B：

| 偏移 | 字段 | 类型 / 要求 |
| --- | --- | --- |
| 0 | schema | u16，必须匹配本次请求的 `1` |
| 2 | revision | u32，本 boot 内的配置版本 |
| 6 | state | u8：Idle=0、Pending=1、Applied=2、Failed=3 |
| 7 | error | u16，公共 Error 枚举；成功为 0 |
| 9 | request_session | u64，最近受理写的客户端 session |
| 17 | request_sequence | u32，最近受理写的 sequence |
| 21 | value_length | u16，schema 1 必须为 `4` |
| 23 | focus_channel | u8，0..15 |
| 24 | wheel_channel | u8，0..15 |
| 25 | ready_mask | bit0=跟焦模块就绪，bit1=摇轮模块就绪，其余位为 0 |
| 26 | focus_source | u8：Wireless=0、Wired=1、Stopped=2、Fault=3 |

`focus_source` 是私有跟焦来源，不能按公共 `Source` 的 None=0、Remote=1、Mobocom=2 解读。配置 `state` 也不能使用公共运动状态枚举。

`WRITE_CONFIG (0x0051)` 请求 payload 固定 36B：

| 偏移 | 字段 | 类型 / 来源 |
| --- | --- | --- |
| 0 | uid | u64，INFO |
| 8 | boot | u64，当前 STATUS，与 INFO 一致 |
| 16 | expected_generation | u32，当前 STATUS |
| 20 | epoch | u64，当前 STATUS，与 INFO 一致 |
| 28 | schema | u16=`1` |
| 30 | expected_revision | u32，保存前 READ_CONFIG 的值 |
| 34 | focus_channel | u8，0..15 |
| 35 | wheel_channel | u8，0..15 |

成功应答使用原有 40B ACK，表示请求已受理；ERROR 为 44B，即 `error:u16, detail:u16` 加 40B ACK 快照。ACK 中目标序号、operation_id 都不是配置事务完成标志，不能复用动作完成判断。

原生层保留 u64；跨 Tauri JSON 边界统一用十进制字符串，TypeScript 需要运算时用 `bigint`，禁止经过 JS `number`。建议 DTO 的核心类型如下，**这是新增应用层类型建议，不是已发布 SDK 类型**：

```ts
type U64Decimal = string
type ConfigurationState = 0 | 1 | 2 | 3
type FocusSource = 0 | 1 | 2 | 3

interface RadioConfigurationDto {
  schema: 1
  revision: number
  state: ConfigurationState
  error: number
  requestSession: U64Decimal
  requestSequence: number
  focusChannel: number // 逻辑值 0..15；显示时加 1
  wheelChannel: number
  focusReady: boolean
  wheelReady: boolean
  focusSource: FocusSource
}

interface RadioWriteReceiptDto {
  uid: U64Decimal
  boot: U64Decimal
  epoch: U64Decimal
  generation: number
  session: U64Decimal
  sequence: number
  previousRevision: number
  focusChannel: number
  wheelChannel: number
}
```

写入回执上下文须在分配 sequence、构造完整帧后、第一次发送前保存在原生维护服务中；即使写 ACK 丢失或 Tauri 调用超时，也能通过查询本次结果取到它。只保留 `await write()` 的成功返回值不足以处理丢 ACK。

### 保存流程与界面状态

1. 打开用户选择的串口，确保该端口没有另一条控制连接；同一连接生命周期复用一个非零随机 session 和递增 sequence。
2. 发送 INFO `0x0004`（空 payload），确认身份；READ_CONFIG 探测 schema 1。明确的 UNSUPPORTED 显示“不支持此配置”；超时或坏帧显示通信异常，不能推断为旧固件。
3. 用户点击保存时校验两项为 1..16 的整数，编码成 0..15；重新读取 INFO、READ_CONFIG 和 STATUS `0x0030` 的第 0 页（payload=`00 00`）。核对身份与 boot/epoch 一致，取最新 revision、generation，期间状态变化则重新读取。
4. 只有公共状态为 SAFE 或 FAULT、source=NONE、无 owner 时才尝试写；最终以设备的私有输出和存储条件校验为准。不要自动清故障或切换来源以强行满足条件。
5. 原生层记录本次写上下文，发送 WRITE_CONFIG。ACK 后显示“正在保存”；写应答超时但请求可能已经发出时，也进入结果查询，不能直接提示“未保存”。
6. 先等 250ms，再每 250ms READ_CONFIG；本板建议总等待上限 5 秒。查询超时可以在下一轮继续，总时限内不并发堆积请求。定期核对 STATUS；连接重开或身份/boot/epoch 变化时重新识别，并将原操作标为结果未确认。
7. 仅当 `schema=1`、`state=Applied`、`error=0`、`request_session/sequence` 匹配本次写、`revision=previousRevision+1`、两频道等于请求值，且 boot/epoch 未变化，才显示“已保存并应用”。
8. 确认保存后刷新模块 ready 和跟焦来源。另一个客户端更新、Failed、超时、重启等情况按下表显示，不能仅凭频道碰巧相同报告本次成功。

| 读回 / 通信情况 | 界面行为 |
| --- | --- |
| Idle | 显示当前值；通常是开机后尚无本 boot 的受理写，不能用来确认刚才的写成功 |
| Pending 且关联本次写 | 显示“正在保存”，禁止再次提交；其中频道仍是旧提交值 |
| Applied 且完整成功条件满足 | 显示“已保存并应用”；模块 ready 分别显示 |
| Applied 但一路 ready=false | 显示“已保存，该无线模块未就绪”；不得显示两条无线链路都已连通 |
| Failed 且关联本次写 | 显示错误码和当前读回值；保存成功但应用超时也可能读回新值，不自动回滚 |
| 关联字段不是本次写、boot/epoch 变化或 5 秒未确认 | 显示“结果未确认”，保留重新读取和诊断；不得自动创建新写或覆盖其他客户端配置 |
| 串口已打开但有 CRC/帧错误 | 保留连接、展示原始 HEX 和错误计数；不把坏数据更新为有效配置，不放行写入 |

配置 revision 在受理时递增，失败也消耗；重启后从 0 开始，Idle 的关联字段清零。设备只保存最近受理写的结果，查询是必要的确认步骤，不能把它当永久操作日志。

同一事务的底层重试必须使用原始完整帧，不能重新调用写方法生成新序号；查询不会淘汰配置写缓存。收到 ACK 后转为查询，不继续重发。revision 冲突应刷新并让用户重新提交，旧 boot/epoch 请求禁止继续重试。遇到明确拒绝的 ERROR 时显示拒绝原因，不将其他写的结果关联为本次成功。

Flash/NVS 操作可能短暂影响查询响应。PC 事务超时应覆盖实测 USB/驱动延迟，参考工具使用 500ms；它与上述 5 秒保存总时限、MCU 的 20..1000µs 回复起始窗口是三个不同约束。TypeScript Client 默认 30ms，Rust 当前按波特率计算事务超时；开发者需要为维护连接选择并验证合适配置，不能靠放宽超时宣称硬件时序通过。

### 保存后为什么手柄暂时不能带动电机

本板受理频道写入后会停止跟焦输出，清除旧位置、按钮边沿及无线 FIFO；成功或失败后都保持 Stopped。界面应显示“频道已保存，跟焦输出已停止”，让用户知道保存结果与运动状态分别是什么。

**以下限制仅适用于未实现 schema 4 的旧固件（早于正式 `0x00010007` / 台架 `0x80010007`）：RS422 配置接口没有恢复私有跟焦来源的命令。** 不要添加一个没有固件支持的“恢复手柄”按钮，也不要向 RS422 发送 ASCII `focus wireless`、PDMOVIE 或公共 `SET_SOURCE` 来代替恢复。

本次台架通过独立 USB 调试串口的 `focus wireless` 显式恢复。另一路径是在用户确认后重启，按已保存的 `focus.startup` 恢复；启动配置不一定是 Wireless，客户端不能承诺重启必然恢复手柄。新版固件已通过 schema 4 提供正式恢复接口，客户端按[跟焦切源说明](MOBOCON_FOCUS_CONTROL.md)实现；旧固件返回 UNSUPPORTED 时保留上述兼容处理。

### 错误码处理

| Error | 客户端处理 |
| --- | --- |
| 1 MALFORMED / 9 RANGE | 校验长度、UID、频道整数与范围，修正请求后再提交 |
| 2 UNSUPPORTED | 禁用保存，显示扩展或 schema 不支持；不能仅凭 Gimbal 类型判定支持 |
| 3 STATE / 13 SAFETY | 显示当前状态及拒绝原因；用户处理输出/存储条件，客户端不自动清故障或使能。板端 USB 全量保存后待重启也可返回 SAFETY |
| 4 BOOT / 5 GENERATION / 20 EPOCH | 重新识别和读取，禁止延续旧写上下文 |
| 7 SEQUENCE / 8 CONFLICT | 可能是旧序号、同序号不同内容或 revision 冲突；刷新后由用户重新提交 |
| 11 BUSY | 另一配置或维护握手未完成；只查询，不堆积写操作 |
| 12 SPACE | revision 耗尽；停止写入，经用户重启后重新识别 |
| 18 TIMEOUT / 19 DRIVER | 异步应用未确认或保存失败；显示实际读回值，保留诊断，不自动回滚 |

`error_code=11 (BUSY)` 与 `fault=11 (LIMIT_TRIP)` 属于不同命名空间，不能共用显示/恢复分支。candidate.2 已定义 LIMIT_TRIP 为物理限位触发后的故障，禁止自动清除；退出条件以设备类别契约为准。当前 Mobocon 基线已有 LIMIT_TRIP 常量，此处是兼容要求，不要求重复新增。

### SDK 对照与客户端验收

| 对照材料 | 用途 |
| --- | --- |
| [PROTOCOL 第 9 节](../mobocom_realtime/PROTOCOL.md) | 配置状态机、幂等与 schema 1 的唯一规范 |
| [TypeScript configuration.ts](../mobocom_realtime/typescript/configuration.ts) | `configurationSnapshot`、`configurationWrite`、`radioChannels`、`dualRadioConfiguration` 字段校验 |
| [TypeScript client.ts](../mobocom_realtime/typescript/client.ts) | `Client.readConfiguration` / `writeConfiguration`；后者成功返回 `{ack, session, sequence}` |
| [Python 配置模型](../mobocom_realtime/configuration.py) / [C++ 配置定义](../mobocom_realtime/cpp/mobocom_configuration.hpp) | 与 Rust 实现逐字段对照 |
| [configuration_vectors.json](../mobocom_realtime/configuration_vectors.json) | 8 组精确请求/应答字节：Idle、写 ACK、Pending、同帧重试、冲突、旧序号、Busy、不支持 schema |
| [test_configuration.py](../mobocom_realtime/tests/test_configuration.py) | 配置模型及边界用例参考；客户端还需构造自己的完成/失败与 UI 测试 |

现有黄金向量没有 Applied/Failed 完成场景，客户端应补充模拟响应。向量中的 UID=2、boot=20、session=100 仅为测试夹具，不能发给真实板子。现成 TypeScript `writeConfiguration` 在超时后会抛错，不能假设能在 catch 中读取未返回的 receipt；原生层发送前保留上下文的要求仍需实现。

以下是 **Mobocon 待完成的验收项**，不得用协议库测试通过替代：

- [ ] Rust 编解码逐字节匹配 8 组向量；READ_CONFIG RESPONSE 不再被误判为 ACK；长度、schema、枚举、CRC 错误被拒绝。
- [ ] UI 通道 1/16 对应逻辑值 0/15；逻辑值 16、负数、小数被拒绝；无线频道不覆盖原总线地址字段。
- [ ] 大于 `2^53` 的 UID、boot、epoch、session 跨 Rust/JSON/TS 往返无精度损失。
- [ ] 模拟 ACK 丢失、Pending→Applied、Pending→Failed、5 秒未确认、另一客户端更新和重启；仅完整关联成功条件允许成功提示。
- [ ] 明确 ERROR、旧 revision/boot/epoch、同序号不同 payload 不触发盲目新写；保存失败不自动回滚、不恢复运动。
- [ ] 未配置云台或未接 CAN 时仍能读写维护配置；公共运动状态/来源不满足、私有条件拒绝时正确报错，不自动 SESSION_OPEN/RESET_FAULT/使能。
- [ ] 一路无线模块未就绪仍可保存期望频道，UI 区分保存、芯片 ready、无线对端在线和运动状态。
- [ ] 在真实 USB→RS422 连接上完成双频道保存/回读；用户重启后确认 boot 变化且频道保持，再独立确认手柄与电机行为。
- [ ] 串口协议异常仍保留 HEX 诊断；断开/页面生命周期结束释放端口；不存在另一条后台运动连接争用同一串口。
- [ ] 保存后明确提示跟焦 Stopped；支持 schema 4 的固件按关联切源流程恢复，不支持时不展示可执行的 RS422 恢复功能；旧 51200 云台及其他设备连接不受影响。

已完成的 **板端证据**（2026-10-26，详见固件仓库 `docs/validation.md`）：COM77 烧录、COM78 RS422 联调，UID `0000C03CDB0AF6E8`；跟焦逻辑频道 `9→0`，摇轮 `2→3→2` 保存/应用，重启读回 `0/2` 保持；USB 显式恢复 Wireless 后，手柄通道 1 的数据与 PDMOVIE 输出已记录，用户确认电机正常跟随。上述 COM 号和 UID 只标识该台架，产品中不得硬编码。

摇轮模块当时未就绪，尚无摇轮射频端到端验证。Mobocon UI、全负载硬件时序、断电发生在 Flash 写入过程中的行为均未在本条更新中验收。本次更新文档本身只核对源码、链接和 diff，不追加构建或硬件通过声明。

## 后续维护约定

影响 Mobocon 的命令、字段、枚举、状态机、错误码、时序或设备接入条件变化，都应与代码同步更新本文；在上述历史记录前新增版本条目，不覆盖旧版本的事实。文档勘误可以直接修正，涉及行为变化则另开条目并指出替代关系。

每条记录至少包含以下信息，未确认的内容写“待实现”或“未验证”：

| 记录项 | 必填内容 |
| --- | --- |
| 日期与版本 | 更新日期、SDK / 线版本、协议实现提交；不要把纯文档提交当成协议版本升级 |
| 支持范围 | 设备/固件基线、兼容性、是否破坏性变更及升级顺序 |
| 客户端基线 | 核对的 Mobocon 提交、现有路径和建议新增项；交付文档不等于修改已落地 |
| 修改要求 | 变更前后字段、调用顺序、UI 状态、失败处理，必要时给出迁移示例 |
| 验收与状态 | 软件用例、实板步骤与证据，区分公共库、固件和客户端结果；接入完成后填写客户端提交 |
| 未完成项 | 缺失接口、未验收设备/场景、需要用户执行的操作 |

涉及公共行为的变更还须同步规范、C++/Python/TypeScript 和对应测试/向量；本文是客户端迁移入口，不替代这些维护工作。只修改本文和入口链接时，核对内容、相对链接与 Git diff 即可，不需要重打 SDK 或更新设备 vendor。
