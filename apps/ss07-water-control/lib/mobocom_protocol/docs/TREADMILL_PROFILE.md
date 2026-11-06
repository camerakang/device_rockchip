# TREADMILL (0x9003) 速度型类别契约

状态：实施候选。wire `0x22` 与类型 ID 不变；实体参数须实机标定，软件测试不代替机械安全验收。

| 项 | 值 |
|---|---|
| type | `TREADMILL=0x9003`，profile_version 1 |
| 来源 | `source_mask=6`：REMOTE（设备本地 RS485）+ MOBOCOM |
| 资源 | endpoint 1 / resource 1：目标线速度，单位码 `0x9003`，**cm/s**，范围 −600…600（初始配置），flags=3 |
| 反馈 | 双驱实测速度平均值（cm/s）。任一驱动器数据过期、无效、故障时反馈无效 |
| 动作 | 无。无 HOME / SET_ZERO / SET_RANGE（增量式编码器，不发布绝对位置） |
| 时序 | max_target_hz 100；target TTL 20 ms～1 s；lease 50 ms～3 s；update_gap 20 ms；safe/switch 5 s |
| 自动恢复 | `auto_reset_mask=0`：目标过期／租约超时后不自动恢复，不重播旧目标 |

## 安全行为（设备工程实现）

- 目标过期、租约到期、RS485 超时、EtherCAT 过程数据丢失、驱动器故障、双驱速度偏差持续超限：双驱同时 Quick Stop，进入 FAULT。
- 双驱偏差阈值 `max(5 cm/s, |目标|×10%)`，连续 3 个周期超限才触发（DRIVER 故障）。
- `STOP` 为受控减速；`ESTOP` 与上述故障为 Quick Stop。上电后驱动器保持未使能，首个合法控制命令才使能。
- 增量编码器：不提供位置资源；上电无需回零。
- UDP STOP 不能替代物理急停。
