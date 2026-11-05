# 应用安装、初始化与恢复（0.10.3）

2026-11-04 已部署 0.10.3 网页直接进入模式到 192.168.3.233，事务 `b5a0cc96-5205-4dbc-8d5f-61e636c579b1` 已 COMMITTED。包为 SDK `output/ss07/direct-access-20261104/arm64/ss07-0.10.3-linux-arm64.tar.gz`，SHA256 `2b80d37e6c45af967501149d53afd4e5da173423246808ed1da8e22f5e0c3111`。旧 0.10.2 版本保留；配置、证书与内核未更换。安装前发现时码 SQLite 热事务日志，暂停该服务后在 `/var/lib/ss07-deploy/direct-access-20261104-timecode-backup` 完整备份数据库及伴随文件，使用 SQLite 标准恢复，完整性检查为 ok；之后恢复服务并通过正式安装工具完成升级。下面的旧发布记录保留为历史依据。

本流程只管理 SS-07 应用，不写启动分区，不更新内核，不操作其他项目。当前版本为 0.10.2，包含生产媒体服务 WebRTC 所需的 `AF_NETLINK` 权限修复和日志空查询默认参数修复，保留 0.10.0 的声呐服务和 0.9↔0.10 配置迁移。2026-10-26 按五路 720p25 验收指令已完成板端正式安装、预检、激活及 0.10.0→0.10.1→0.10.2 升级，最终事务 `4f136cce-92b1-4a5d-b5e2-ab7b345d2b3f` 为 COMMITTED，软件健康检查通过，硬件整体状态仍为未验收。见[本轮验收](acceptance-720p25.md)。

当前精确 ARM64 包为 SDK 的 `output/ss07/acceptance-720p25-20261026/arm64-0.10.2/ss07-0.10.2-linux-arm64.tar.gz`，SHA256 `cffb4218b8368750db5433f28353a57a3f3abed20a4dc3076eb420bfa84bf62a`。新的源码包与 IMG、原始日志和摘要单独保存在该验收目录。下文 0.10.0 路径和摘要为历史冻结记录；安装当前修订时应使用 0.10.2 独立工具及其核验后的包，不能把旧示例包当作包含权限修复的版本。

Python 管理进程只传有界状态、配置和元数据。C++/GStreamer、MediaMTX 与 Caddy 继续承担视频与 HTTPS 数据路径。本流程不增加视频解码、逐帧 Python 复制或重新编码。

## 安装包与受管路径

工具为 `tools/ss07-release.py`。`inspect` 可在开发机校验 ARM64 包；其他命令要求在目标 ARM64 系统以 root 明确执行，没有替代生产数据盘或模拟硬件的命令行开关。

| 路径 | 用途 |
| --- | --- |
| `/opt/ss07/releases/<version>` | 经摘要验证的版本内容，安装后不能原地修改 |
| `/opt/ss07/current` | 当前版本的相对符号链接 |
| `/opt/ss07/recovery/restore.py` | 版本目录之外的标准库启动恢复程序 |
| `/var/lib/ss07-deploy` | root 0700；安装锁、私有包缓存、版本收据、安装/激活事务和备份 |
| `/etc/ss07/system.json`、`/etc/ss07-active/system.json` | 候选与运行配置；升级保留现场值 |
| `/etc/ss07-credentials`、`/etc/ss07-tls`、`/etc/ss07-gateway` | 独立账户、证书/私钥和 HTTPS/ICE 配置；升级保留 |
| `/etc/ss07-provisioned` | 显式激活成功所需的初始化标记；镜像构建和安装均不预先创建 |

预期 SHA256 必须来自单独核验的发布记录。先复制和核对同一组归档字节，再有界解包；逐文件清单必须匹配。归档中的绝对路径、越界路径、重复项、额外文件、硬链接、设备节点和非白名单符号链接均拒绝。导入目录原子发布且不覆盖已有目录。摘要证明内容一致性，不能单独证明发布者身份。

包上限为压缩 128 MiB、展开流 256 MiB、1024 个普通文件。安装前检查系统盘余量，复制版本和数据库备份时另保留余量。最多保留 32 个安装事务，达到上限需离线检查并归档历史；程序不会自行删除版本、备份或录像来腾空间。

首次安装只在配置文件缺失时放入默认值，不启动业务。已有同版本只能在内容和整包摘要相同的情况下复用。未知来源、没有版本收据的手工安装不能直接升级，应先核对来源并制定迁移；不能伪造收据跳过检查。

## 后续部署操作顺序

以下是明确获准部署后使用的操作说明，本次没有执行这些设备操作。`/path/to/ss07-0.10.0` 表示已核验摘要并解出的源码包，可在 `current` 损坏时提供独立安装工具。建议以 `/usr/bin/python3 -I` 运行该工具。安装、跨版本回滚及后续恢复均保留并使用这份 **0.10.0 独立工具**，不能在回滚后改用 `current` 指向的旧版工具处理新事务。

本次候选对应的冻结产物如下，路径相对于 SDK 根目录；完整记录为 `output/ss07/image-20261024/delivery.json`。源码包中的旧版安装说明由本页修订取代，归档本身及其摘要保持不变。

| 产物 | 路径 | SHA256 |
| --- | --- | --- |
| 0.10.0 源码及独立安装工具 | `output/ss07/image-20261024/source-package/ss07-0.10.0.tar.gz` | `6e489406366392b65afcd1a78f0f85f85e5d318d575190afee4d1b639657d78d` |
| 0.10.0 ARM64 应用 | `output/ss07/sonar-migration-20261023/arm64-candidate-03/ss07-0.10.0-linux-arm64.tar.gz` | `1b1a359f5fd3ad9a5493fb2c97e5db6491c567574ff0e3629785eb844ebc1075` |

先在开发机核对源码归档 SHA256，再解出到独立目录。下面的 `inspect` 只在执行主机的临时目录验证应用包，不连接目标板；其成功不代表已经安装。设备安装前将同一组已核验文件复制到目标 ARM64 主机，再以 root 执行 `install`。

```sh
/usr/bin/python3 -I /path/to/ss07-0.10.0/tools/ss07-release.py inspect \
  --archive /path/to/ss07-0.10.0-linux-arm64.tar.gz \
  --sha256 1b1a359f5fd3ad9a5493fb2c97e5db6491c567574ff0e3629785eb844ebc1075

# 目标 ARM64 上，以 root 执行；首次只安装，不激活。
/usr/bin/python3 -I /path/to/ss07-0.10.0/tools/ss07-release.py install \
  --archive /path/to/ss07-0.10.0-linux-arm64.tar.gz \
  --sha256 1b1a359f5fd3ad9a5493fb2c97e5db6491c567574ff0e3629785eb844ebc1075
```

安装会准备固定的 sysusers/tmpfiles 和业务 unit，不创建默认密码、证书或数据盘。准备初始化输入时：

1. 真实探测后准备 `system.json`，先验证，再分别安装到候选与运行配置位置。初始配置保持 root 拥有、0644；运行目录为 root 0755。激活前要求两份 revision 相同。升级则允许保留尚未应用的候选值。
2. 通过已有 `tools/ss07 init-accounts --accounts /etc/ss07-credentials/accounts.json --username admin` 无回显输入密码；账户应为 root:ss07-web 0640，目录 root:ss07-web 2750。没有预设账户或密码。
3. 提供实际证书链及配套未加密服务私钥，例如 `/etc/ss07-tls/server.crt` 和 `server.key`。证书 root 拥有、不可被组/其他用户写入；私钥 root:ss07-gateway 0640，目录 root:ss07-gateway 0750。客户端对签发 CA 的信任需另行配置。
4. 按 `config/gateway.example.json` 准备 `/etc/ss07-gateway/network.json`，root 拥有、0644。填实际 RK 的监听/ICE 地址、HTTPS origin 和证书路径。`192.168.3.47` 是 SE2017 候选地址，不能据此填写 RK 的接口地址。
5. 可选的原生播放器访问按 [RTSPS 说明](native-rtsp.md) 配置 `/etc/ss07-gateway/rtsp.json`，缺省关闭。它与 HTTPS 共用证书和账户。
6. 执行只读预检，再明确激活：

```sh
/usr/bin/python3 -I /opt/ss07/current/tools/ss07-release.py check-ready
/usr/bin/python3 -I /opt/ss07/current/tools/ss07-release.py activate
/usr/bin/python3 -I /opt/ss07/current/tools/ss07-release.py status
```

预检检查 schema、账户角色/哈希结构、目录与秘密文件权限、证书/私钥匹配、SAN、有效期、服务器用途、端口冲突，以及监听/ICE 地址确实属于本机。它不验证浏览器信任、LAN 路由、ICE 连通性或硬件性能。生产依赖检查使用本机 Python、原生二进制及 GStreamer 插件。

激活验证通过后才写标记，启动固定服务，并检查 API 版本、配置摘要、同一 worker 实例持续推进的心跳及稳定窗口，再启用 target。已配置但缺失的外设仍可以报告降级，返回值包含 `hardware_verified=false`。启动失败删除标记、停止本次服务并记录失败；激活未完成时重启会清除本次标记，避免把中断当成已验收。

用户已确认五个 Hub 路径依次作为 front/back/left/right/down；时码 USB-RS485 的 baud 为 500000。声呐尚无实物/协议，时码其余 UART 参数及稳定路径仍为 null；录像按名义 1 TB 规划，当前缺盘，默认禁止录像。初始化不能用这些规划值冒充真实设备探测。

## 升级与主动回滚

再次执行 `install` 即升级。安装工具先验证当前版本、包、数据契约、依赖和配置，然后持久化事务和待恢复文件；先停止配置协调器，确认没有未完成的配置应用，再停止应用服务。SQLite 使用在线备份接口创建一致备份并检查完整性；所有业务写者已停止。

之后写入 unit/系统目录规则、切换 `current`、准备服务目录，再启动升级前已运行的服务。健康检查通过回环接口携带已配置的网关 Host，要求 API 返回新版本、worker 加载预期配置、心跳推进，并持续稳定。原来健康的服务不能以新故障代替成功；已有硬件降级必须保持相同状态与原因。

升级会短暂中断监看与录像；客户端需要重新登录，录像目录保留可检查的间隙。它不承诺无缝升级或整机 A/B 恢复。

```sh
# 主动退回上一个经验证的包；expected-version 防止误操作过期版本。
/usr/bin/python3 -I /path/to/ss07-0.10.0/tools/ss07-release.py rollback --expected-version 0.9.0
```

该示例适用于从 0.9.0 升级到 0.10.0 后退回缓存中的 0.9.0；首次安装没有可回滚的旧版本。执行前查看状态，确认实际旧版本及缓存，不把示例版本号用于其他安装历史。跨契约降级还要求两份配置的声呐归档均关闭、配置历史完整，详见[迁移约束](sonar-migration.md)。

主动回滚是新的完整安装事务，使用已保留且重新核验的包缓存。它保留当前现场配置、账户、TLS 和兼容数据库的最新记录；不会拿升级前的数据库快照覆盖新录像索引。第一次工厂镜像构建同时登记内容及包缓存，以支持第一次现场升级之后的回滚；仅复制文件、手工切换链接不具备这个保证。

已发布的 0.8.0/0.9.0 路径支持 0.7.0 已交付精确摘要包的相同数据契约：config/accounts=1，catalog=2，timecode_journal/audit/application_journal/worker_snapshot=1。0.10.0 本地候选新增声呐数据库和配置兼容修订 2，并提供[0.9↔0.10 双向迁移](sonar-migration.md)。必须使用新版本绝对路径的维护工具完成该转换；降级要求归档关闭和配置历史一致，保留声呐数据及可恢复的原文件备份。未知契约和其他数据库 schema 变更仍需要经验证的离线迁移与恢复方案。旧 catalog schema 1→2 的准备见 [时码与录像索引说明](timecode-runtime.md#录像索引升级)。不要直接替换单个 `.sqlite` 文件而遗漏 WAL、侧录和原始片段。

## 中断与恢复

安装事务经历 PREPARED、STOPPING、SWITCHING、STARTING、VERIFYING，成功后 COMMITTED。失败恢复先确认服务已停止，验证旧版本、备份摘要和受管文件未被外部修改，再恢复代码链接/系统模板；检查数据库契约并启动原服务，最终 ROLLED_BACK。

`ss07-release-recover.service` 在业务及配置恢复服务之前运行，程序位于 `current` 之外。中断后启动只恢复文件和兼容性，记录 RECOVERY_PENDING；此时尚未证明运行健康。使用状态中的 operation_id 完成恢复确认：

```sh
/usr/bin/python3 -I /path/to/ss07-0.10.0/tools/ss07-release.py status
/usr/bin/python3 -I /path/to/ss07-0.10.0/tools/ss07-release.py recover --operation-id '<状态中的 operation_id>'
```

状态在当前程序内容损坏时仍报告完整性错误和事务 ID。停止失败、旧版本被修改、备份损坏、文件出现外部修改或数据库 schema 不兼容都会保留可见错误；不能删除事务/门控标记来掩盖失败。兼容数据库、原始录像和升级后新增记录始终保留。

## 验证与范围

单元测试覆盖导入边界、首次安装、同版本检查、配置/秘密文件保留、数据行保留、互斥、空间不足、停止失败、启动失败、主动回滚、启动恢复、工厂收据和初始化检查。`tools/test-release-installation.py` 使用已交付 0.7.0 与当前 ARM64 包中的 Python 代码，启动本机原生媒体进程、HTTPS 和六路编码测试源，在短私有运行目录中检查跨版本恢复。只在测试脚本中替换生命周期后端和录像卷；生产没有这些绕过选项。

0.8.0 的 152 项测试已在 ARM64 Python 3.8.10/QEMU 下通过，运行库为 SQLite 3.31.1/OpenSSL 1.1.1f；10 种 service 模板和 target 通过目标 systemd 245 静态解析。跨版本原生进程联测通过升级、主动回滚、API 启动失败自动恢复及未完成事务的启动恢复。验收证据在源码工程的 `docs/evidence/installation-*.json` 和外部发布记录中，ARM64 运行包不重复包含所有证据。

本轮已验证实际板端服务身份、LAN 证书/ICE 和五路生产监看；物理掉电、完整外设、新 IMG 启动和八小时六路监看录像仍待实物验收。本机进程测试、ARM64 用户态 QEMU 与 systemd 静态解析不能代替这些验收。

当前 0.10.0 的 239 项测试、精确 0.9↔0.10 包升级/回滚/失败恢复及 ARM64 包核验见[迁移验证](evidence/sonar-migration-validation.json)；完整 IMG 回读和其中实际应用的用户态运行见[镜像验证](evidence/full-image-0.10.0-validation.json)。2026-10-24 下午只读复查仍无 USB 相机、USB 串口和录像数据盘，SE2017 候选地址的 HTTP 可达、554 拒绝连接，见[最新现场记录](evidence/hardware-readiness-20261024-followup.json)。
