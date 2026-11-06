# Control 0x22 网络迁移清单

适用版本：SDK `2.2.0-network.1`，实现基线 `feat/network-only-v22` 的 `5568bd3`。当前是实施候选，协议库软件已验证，不表示Mobocon、消费固件或实物网络已验收。完整字段以[规范](../mobocom_realtime/PROTOCOL.md)为准，调用以[SDK指南](../mobocom_realtime/SDK_INTEGRATION.md)为准。

## 迁移边界

移除的是主控至设备的Control RS485/RS422承载，替换为Ethernet/UDP（推荐DHCP）；不再提供旧Control串口兼容路径。独立0x12主机时码、私有PDMOVIE/CAN/PWM和既有USB CDC业务不由本次迁移改变。

旧0x20/0x21固件不能混用，需同步升级主控与全部参与设备。verify能够校验旧受管快照，仅用于安装前完整性检查，不代表协议兼容。

## 字段和接口

| 项目 | 0x22要求 |
| --- | --- |
| 帧 | 帧头37B，payload最多240B，总长39+N，最大279B；一个UDP数据报一帧 |
| 寻址 | src_uid/dst_uid为u64；主控src=0，设备回复src=UID、dst=0；请求dst=0用于全体组播 |
| 网络 | UDP47821，组播239.255.33.21，TTL=1；应用明确选择控制网卡 |
| 身份 | UID是设备绑定键；IP:port是当前位置，不要求固件静态IP，不再有node_id或ASSIGN_ID |
| INFO/HELLO | 33B：uid、type、firmware、boot、0x22、profile_version、epoch |
| DISCOVER | 空payload组播；所有状态节点26B回复：type、firmware、boot、epoch、generation；UID取帧头 |
| SESSION_OPEN | 仅单播，20B：boot、epoch、generation；不再重复携带UID |
| C++/Python构造 | DeviceCore(profile, uid, boot, hooks或driver)，删除节点地址参数和save_address |
| TS身份 | Client、ConfigurationRequest、Schedule及finish的Map键使用bigint UID |
| TS传输 | send(bytes, endpoint或null): Promise<void>、close()；feed(datagram, from)传完整数据报和源endpoint |
| 旧接口 | 删除流Parser、发现槽、enumerate/assign、recoverIdle、wireEndUs及baudRate |

JSON传递UID/session/boot/epoch等u64时使用十进制字符串，在TS边界转换为bigint；不可先经number转换再转回。持久设备树不要使用列表索引或旧节点号绑定设备。

## 主控顺序

1. 选择控制网卡，配置DHCP服务/地址和防火墙；实现UDP单播、固定组播及来源关联。
2. scan/discover自动学习地址，或显式setEndpoint；默认2轮，每轮100ms，可运行中后台扫描。核对必需UID名册及conflicts/missing，不能把无回复等同无设备。
3. INFO/CAPS/STATUS确认UID、类别、boot及安全状态；只有SAFE、无故障且safeConfirmed后才由用户显式SESSION_OPEN。
4. 读取新epoch/generation，显式切到MOBOCOM并确认ACTIVE、owner=当前会话，再发新目标。不能给旧队列换新凭据继续播放。
5. 同UID单播串行、不同UID并发；扫描独立后台收集，不阻塞目标/保活，同时只允许一次扫描。普通请求最多同帧重试两次；实时调速及跟焦单次接口不自动重试。
6. 单播STOP/ESTOP在对应UID队列优先，全体STOP/ESTOP立即组播空payload且无回复。之后逐设备确认安全；send成功或ACK不表示机械停止。

IP变化或boot变化时挂起该UID，以独立只读INFO探测候选与旧endpoint：单一地址确认UID后更新映射，多个在线地址确认同UID才冲突，无匹配则unconfirmed。可在同一Client调用reconnect(uid)重发现/验证/查询；清旧队列和凭据，needsRearm保持，用户显式安全建会话、切源和新目标后才可启动。旧调度批次不能复用，其他UID继续运行。详见[换址重连](NETWORK_DISCOVERY.md#换址重发现与重连)。

## 设备适配

设备工程需确认MCU及PHY/网卡实际具备网络能力，不能把协议升级当作硬件已支持Ethernet。推荐设备网络适配器通过DHCP获取地址，协议不负责地址分配；友好名称通过可选mDNS提供，详见[动态地址与名称发现](NETWORK_DISCOVERY.md)。

所有核心接口由一个所有者任务调用。每个完整数据报receive后立即take_reply，并将回复发送到该请求源endpoint；回复最多保留20ms，下一receive会丢弃旧待发回复。不可把旧回复送给下一请求源。无输入也周期tick，不允许网络等待阻塞本地目标保护与安全回调。

组播只接受空DISCOVER及空STOP/ESTOP，其余丢弃。UID目标还需匹配；单播DISCOVER丢弃。CRC用于检错，不提供认证；在可信隔离控制网络部署，远程UDP停止不能替代物理急停。

## 调度和验收

budget与RealtimeScheduler.configure不再接受baudRate。100Mbps链路每N字节数据报按max(46,N+28)+38字节计费，累计超过80%拒绝。hostOverheadUs仅用于调度期限余量；交换机排队、重试、VLAN及额外配置/动作需另外预算和实测。

本次全状态发现及按UID重连更新的软件证据：Python73项、C++四个测试程序、TS1059项检查及隔离SDK构建/清单/篡改拒绝通过，详见[REPORT](../mobocom_realtime/REPORT.md)。不扩展为Mobocon、消费固件或实板验收结论。

消费工程另行验收：MCU构建与固定SDK摘要、DHCP续租/重连与组播网卡、多设备UID与重复IP、运行中扫描、名称展示、丢包/乱序/拥塞、在途停止与断线保护、重启/旧会话拒绝、实际输出延迟与抖动、机械限位和失电安全。若共用视频网络，增加视频满载条件；未验证项保持未通过。

回滚需同时恢复匹配版本的主控、固件和部署配置。先停止并确认真实安全，不允许运行中混用旧串口和新UDP控制路径。
