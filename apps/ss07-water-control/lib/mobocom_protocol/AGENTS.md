# AI 仓库入口

回答和思考使用中文。本仓库是所有设备共用的 Mobocom V2.2 网络实时协议唯一维护源。先读 [SKILL.md](SKILL.md)、[规范](mobocom_realtime/PROTOCOL.md)和 [AI 接入指南](mobocom_realtime/AI_INTEGRATION_GUIDE.md)。

时码接 Mobocom/UE；SS-07 的 RK3588 可只读接入用于视频/声呐记录关联，实体控制接收器仍不接时码；设备执行实时目标，跟焦和云台是两台独立接收器。禁止恢复设备授时、节目下载、T0 或旧协议。Control=0x22 网络专用 UDP，主机纯授时独立保持0x12。原有虚拟拍摄 CDC/ACK 属于设备工程，保持原格式。

公共事务、来源、目标新鲜度和故障状态机在本库维护；实际单位、限速、安全和驱动放设备工程。虚拟参数不用于硬件。修改前保留已有用户改动，按实际变更做必要验证。只改文档时核对链接与 diff；改协议时同步规范、三语言模型/SDK、向量及对应测试。

影响 Mobocon 的协议或接入行为变更，同时维护 [客户端更新记录](docs/MOBOCON_UPDATES.md)：写清版本、客户端修改点、兼容性、调用流程与验收状态。客户端尚未实现时明确标注，不把文档交付当作客户端接入完成。

设备类型ID唯一来源为mobocom_realtime/device_types.json；使用DeviceType常量，先查mobocom_realtime/DEVICE_TYPES.md。禁止在消费工程临时分配。修改注册表须递增revision、运行tools/generate_device_types.py并验证生成物；type不替代资源unit、UID或类别版本。

未经任务授权不升级相邻固件或网页快照，不烧录、不开放硬件验收门槛。当前是实施候选，软件通过不代表物理验收完成。
