# 动态地址、后台发现与设备名称

Control 0x22 不要求设备写死 IP。推荐 **DHCP 分配地址 + Control DISCOVER 确认设备 + 可选 mDNS/DNS-SD 显示名称**。UID 是持久身份，地址是当前位置，名称只是可重复、可修改的标签。26B DISCOVER 不增加名称字段，保持现有帧布局。

## 网络部署

```text
DHCP路由器/服务器
       |
     交换机 ------ Mobocon（选择控制网卡）
       |              |-- Control组播发现 239.255.33.21:47821
       |              |-- 可选mDNS服务浏览
       |              `-- UID对应当前地址的UDP单播控制
       |-- 云台接收器（DHCP、唯一UID、名称）
       |-- 跟焦接收器（DHCP、唯一UID、名称）
       `-- 风机等设备（DHCP、唯一UID、名称）
```

设备在自己的网络适配器启用 DHCP，获得地址后绑定 UDP47821 并加入 Control 组播；地址/接口变化时重新绑定。普通无管理交换机并不是DHCP服务器，只有交换机和电脑的直连网络需另配DHCP服务；本库不会自动在Mobocon上开启DHCP，也未实现IPv4链路本地地址回退。设备无有效地址时应显示“未入网”，不能谎报Control在线。

在同一控制局域网发现，不扫描整个IP段，不以Ping代替设备协议。多网卡明确绑定控制接口，组播TTL=1，不承诺跨路由/VLAN自动发现。设备 IP 可变化，Mobocon 保存 UID 名册而不是把 IP 作为设备树主键。

## 所有状态响应且不打断运行

空payload DISCOVER 在 SAFE、SWITCHING、ACTIVE、STOPPING、FAULT 均返回身份/代次。请求来自任意合法主机会话，不需要获得运动控制权。发现不清故障、不给目标续期、不改变来源/owner/generation，不取消动作，不输出目标。正常的tick及安全超时检查仍运行，不因发现跳过。

TS Client 每轮后台收集100ms，默认两轮；不占用单播控制队列。稳定地址的目标、保活及不同UID事务继续发送；若某UID换址/重启，则仅挂起该UID并验证连接，不能沿新地址续播。扫描不会先STOP，也不会自动OPEN/RESET/接管。每个Client同时只允许一个扫描；发现期间拒绝会话轮换，关闭取消扫描，全体STOP/ESTOP仍立即发送并使扫描失效。

设备核心不是网络调度器。适配器必须保证周期tick和驱动刷新优先，限制每周期UDP处理个数、待发队列容量和mDNS工作量，发送不得阻塞。每次receive后立即take_reply，绑定本请求来源，不能让发现覆盖尚未取走的控制回复。建议UI每秒最多一次扫描；大量设备同时回复、拥塞和恶意组播流量仍须实网压测，本次软件测试不证明零抖动。

## 名称服务约定

以下为本项目接入约定，不是已注册的通用服务标准。由设备适配器发布 `_mobocom._udp.local.`，SRV端口47821，A记录为当前DHCP地址。hostname建议 `mobocom-<完整UID十六进制>`；instance建议可读名称加短UID，避免同名设备碰撞。

| TXT键 | 约定 |
| --- | --- |
| `uid` | 非零u64十进制字符串，主控用bigint解析，不能先转number |
| `name` | UTF-8显示名，建议不超过64字节，例如“云台-摄影棚A” |
| `type` | 注册表u16的四位十六进制字符串，例如`9007` |
| `fw` | 固件u32十进制字符串，仅展示 |
| `wire` | 固定字符串`22` |

mDNS是可选的名称/候选地址来源，不是授权或认证。Mobocon先收集服务，再通过Control组播DISCOVER校验UID与来源endpoint；只有两者UID/地址一致时合并名称，type/fw以Control实际回复为准。不能仅因收到TXT就创建运动会话。名称缺失时显示“类别 + 短UID”；同名设备保留完整UID和地址供辨认。

若用户在Mobocon改名，默认仅保存本机 `UID→别名`，显示优先级为“本机别名、mDNS name、类别+短UID”。不借发现写Flash，本次没有设备远程改名命令。显示名需按文本渲染、去控制字符及限制长度，不当作HTML或路径。

## 换址重发现与重连

正常换IP不再永久标为重复设备，也不要求重建Client。发现新endpoint或boot变化后，立即挂起该UID写入、清旧目标/安全凭据、取消排队任务及旧地址在途请求/重试，并递增本地连接代次；其他UID继续。已交给网络适配器/网络的数据报不可撤回，适配器仍须限制发送积压/过期数据，设备TTL/guard保护不能省略。

收集窗口结束后，以独立于控制会话的只读INFO探测所有候选、缓存旧地址及尚未解决的歧义地址。必须匹配session/sequence/command、帧头和INFO载荷UID、实际来源IP/port及合法0x22布局；新地址的boot/type/firmware还须与DISCOVER一致。这样不会因INFO先于旧事务完成而污染当前会话的序号缓存。

| 探测结果 | 处理 |
| --- | --- |
| 仅一个地址确认该UID，且与本轮DISCOVER一致 | 更新通信地址，报告reconnected，不标重复设备 |
| 多个地址确认同一UID | 标记conflicts并禁止单播，不能任选一个；含多接口/重复UID的地址歧义 |
| 无人确认，或身份/来源不一致 | 标记unconfirmed；必需UID同时missing，保留挂起，不转发旧目标 |
| 下次扫描只有一个有效地址 | 可解除地址冲突并恢复查询，仍需用户重接管 |

旧IP被另一UID使用时不算原设备重复。一次扫描内先后见到旧/新IP，也按当前INFO结果判定，而不是按历史地址个数直接判冲突。UDP丢包可能造成漏检，探测超时不是“旧设备一定消失”的证明；UID/CRC不是认证，本方案仅适用可信控制网，不保证网络分区或恶意克隆下的唯一性。

应用检测失联后调用`await client.reconnect(uid)`，默认两轮，可配置1～32轮；先失效该UID上下文，再发现/验证/STATUS，返回`{device, endpoint, status, needsRearm}`。重连时boot/epoch再次变化则失败；没有后台无限重试线程，应用按限频/退避调用，失败仍挂起。mDNS只提供提示，不能跳过Control验证或直接覆盖endpoint。

```ts
const result = await client.reconnect(uid);
// 更新设备树中的在线状态、地址和故障；这里不建会话、不恢复调度。
showDevice(result.device, result.endpoint, result.status, result.needsRearm);
```

重连成功后仅允许查询、用户显式STOP/ESTOP/RESET_FAULT和SESSION_OPEN；不自动停机/清故障/接管，不刷新运动租约。目标、保活、来源、动作、配置写保持禁止。即使当前仍ACTIVE且owner还是旧会话，也不能续发旧目标；等待本地TTL安全处理或由用户显式停止，再确认SAFE/NONE、无故障和safeConfirmed，显式openSession成功才解除挂起，之后显式切源并提供新目标。

RealtimeScheduler保存连接代次，换址后旧槽位不再发送目标或保活，旧setTargets/start/finish均拒绝复用；用户后来重新建会话也不能让旧槽位复活。应用应按正常用户启动流程重新配置该UID发布器，不能通过重启整个场景让其他设备重播。mDNS离线通知只是提示，实际失联保护以目标TTL/租约和STATUS为准。本库没有修改Mobocon UI或增加自动重启动作。

## GitHub参考方案

已核对以下仓库源文档/示例，选型不代表本库已安装依赖或完成固件接入：

- [espressif/esp-protocols 的 mDNS](https://github.com/espressif/esp-protocols/tree/master/components/mdns)：ESP设备优先使用官方组件。`examples/query_advertise/main/mdns_example_main.c`演示hostname、instance、service/TXT发布及查询；将示例HTTP服务改为上述UDP约定，不复制示例凭据。
- [watson/bonjour](https://github.com/watson/bonjour)：Node侧参考，README提供`find({type, protocol})`、service.name/txt及up/down事件；若Mobocon有Node/Electron宿主，可在后台进程浏览`type:'mobocom', protocol:'udp'`。正式引入前单独核查维护状态、依赖、许可证与多网卡支持；此处不认定它是当前最佳维护选择。

若Mobocon是纯网页，UDP和mDNS需由桌面宿主/本地网络服务提供，不把发现依赖塞进实时调度线程。技术栈尚未在本仓库确认，因此本次不修改Mobocon、不安装运行时依赖。没有mDNS时，Control原生DISCOVER仍能按UID找到设备，仅缺少设备自报的友好名称。

## 验收边界

本库回归覆盖五种状态响应、运行目标和故障锁存不变、发现不续租，以及TS后台扫描不阻塞目标/保活；重连覆盖换址、旧IP复用、双在线UID、扫描跨轮换址、重启/故障、错误UID/来源/布局/boot/会话、旧请求/凭据/调度失效和关闭/全体停止取消。此次只修改主控SDK重连行为，0x22线布局、Python/C++设备核心和黄金向量不变。

设备固件仍需安装新SDK并实现DHCP/可选mDNS；Mobocon仍需接入后台浏览与名称展示。实网另验：地址获取/续租/重连、重复UID、多网卡、防火墙/组播、mDNS丢失、运行中反复扫描的输出周期/延迟/丢包、故障设备可见和物理安全。未做这些实测前，不宣称实板或Mobocon接入验收完成。
