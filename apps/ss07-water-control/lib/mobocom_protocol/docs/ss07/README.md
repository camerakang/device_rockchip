# SS-07水上控制系统架构设计

本目录保存SS-07当前架构与两个独立软件的详细设计，控制基线为Mobocom V2.2/0x22 UDP。设计只定义工程职责、接口、部署及验收，不将设备私有实现写入公共核心，也不代表消费工程或硬件已经完成。

新开发交付仅有RK3588视频/声呐系统和云台接收器固件。跟焦为既有系统；mobocon、UE和RS485时码器为外部系统。时码进入mobocon、UE和RK3588记录主机；云台/跟焦控制接收器不接时码，只执行实时目标。

## 阅读顺序

| 文档 | 内容 |
| --- | --- |
| [系统连接与运行](system-runtime.md) | 全系统物理连接、各链路协议与端口、RK服务、对外接口权限、故障隔离与实现验收状态；先读这篇 |
| [系统边界与拓扑](architecture.md) | 两软件职责、完整系统连接与故障边界 |
| [RK3588独立设计](rk3588-design.md) | 六视频、五声呐、媒体/网页/API、存储、镜像与验收 |
| [视频声呐节点契约](video-sonar-node.md) | RK3588作为VIDEO_SONAR(0x9009)网络节点：身份、Profile、发现、免密钥取流、验收 |
| [SS-07与MoBoCon全链路运行](mobocon-runtime.md) | 已部署系统的组成、端口、启动时序、streams字段、WHEP会话、急停与排障 |
| [RK3588网络节点任务书](rk3588-network-node-task.md) | 给RK端实施者：节点服务、lan-monitor、部署验证与回滚 |
| [云台接收器独立设计](gimbal-receiver-design.md) | 摇轮、DeviceCore、实时目标、CAN驱动、标定/保护与交付 |
| [公共V2.2网络接入](mobocom-integration.md) | UID寻址、UDP报文、会话、目标有效期、动作/状态与网络预算 |
| [网络迁移清单](../NETWORK_V22_MIGRATION.md) | 从旧Control串口迁移的字段、接口、部署和验收差异 |
| [编辑定位与反馈](position-telemetry.md) | 持续刷新、真实到位、保持/释放和关键点 |
| [主机时码与播放](timecode-integration.md) | mobocon/UE/RK输入、播放fallback与记录边界 |
| [RK只读时码记录](rk-timecode.md) | 独立接口、时间映射、录像/声呐关联、失时和验收 |
| [CyberGear接口](cybergear-interface.md) | 手册参数、原生CAN、初始化、固件能力和台架检查 |
| [开发任务与验收](implementation-plan.md) | 两工程分工、外部依赖、独立验收和整机共存 |

## 公共规范与设备私有设计

公共报文及状态机以 [PROTOCOL](../../mobocom_realtime/PROTOCOL.md) 为准，接入以 [AI指南](../../mobocom_realtime/AI_INTEGRATION_GUIDE.md)、[SDK指南](../../mobocom_realtime/SDK_INTEGRATION.md) 为准，主机时码以 [timecode](../../timecode/README.md) 为准。真实云台单位、限速、机械安全和反馈参数依 [类别模板](../../mobocom_realtime/DEVICE_PROFILES.md) 在设备工程登记。

本目录于2026年11月2日将控制接入同步至协议实现5568bd3、SDK 2.2.0-network.1。旧0x20/0x21和Control串口不兼容；时码仍为独立0x12。后续公共规范变化时同步审核帧/字段/行为，不要因设计文档存在就跳过消费工程迁移、真实网络、驱动与安全验收。

不迁入旧设备授时、缓存节目/T0方案，亦不重新安排既有跟焦或PC软件开发。尚未确认的引脚、机械限值、UE策略和适配器时序均保留待确认。
