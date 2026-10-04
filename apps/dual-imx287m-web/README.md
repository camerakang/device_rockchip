# LubanCat4 双 IMX287M 网页测试程序

浏览器访问 `http://开发板IP:8080`，同时查看 CAM0 / CAM1。支持共同硬件触发、自由运行、RAW8/10/12、曝光调整、预览帧率、自动对比度、截图和全屏；显示实际采集帧率、V4L2 序号丢帧和相机触发丢失计数。

默认 720×544 RAW12、共同触发 319.4 Hz、曝光 1000 µs、网页预览 20 fps。此板上 320 Hz 外触发会降至约 160 fps，319.4 Hz 实测可正常采集。两块 ADP-MV1 的 J3-3 接 40Pin 物理第 12 针并共地。

## 安装到开发板

依赖：Linux 6.1 下已启用双 MVCAM 和触发 PWM 的设备树、`gcc`、`make`、`python3-pil`、`v4l-utils`、`i2c-tools`。本 SDK 中的 `common/data/configure-double-imx287m.py` 与 `imx287m-trigger-pwm.py` 提供硬件配置。

将项目复制到开发板后执行：

```sh
sudo bash install.sh /path/to/device/rockchip/common/data
```

如果 `/usr/local/bin/configure-double-imx287m` 与 `imx287m-trigger-pwm` 已安装，可直接执行 `sudo bash install.sh`。安装目录 `/opt/dual-imx287m-web`，服务会开机启动。控制相机需要 root 权限。

```sh
sudo systemctl status dual-imx287m-web
sudo journalctl -u dual-imx287m-web -n 80
sudo systemctl stop dual-imx287m-web  # 释放相机并停止 PWM
sudo systemctl restart dual-imx287m-web
```

手动运行：`make && sudo python3 app.py --port 8080`，添加 `--no-autostart` 可只启动网页。运行其它相机工具前先停止此服务，避免争用相机设备。

## 采集与预览

C 程序通过 V4L2 MMAP 持续接收全部帧，按照设备报告的 stride 解码 Rockchip compact RAW10/12，并只将最新的抽样帧送到 Python。Python 将灰度画面编码为 JPEG，通过 MJPEG 输出；慢浏览器不会阻塞相机采集。网页画面是 8 位预览，截图也是 JPEG，不是原始 RAW12 文件。

实际采集 FPS 根据 V4L2 时间戳计算；预览 FPS 根据后端输出计算，浏览器显示速度还取决于网络和客户端。丢帧数字是 V4L2 序号缺口，不能代替所有硬件错误诊断。触发计数寄存器是 16 位计数，会回绕。接收时间差仅在抽样帧序号相同时显示，不能用来证明曝光同步精度。曝光受内部帧周期限制，参数不符合约束时界面给出错误。

网页提供局域网控制，没有登录认证。API：`GET /api/status`、`GET /stream/{0,1}.mjpg`、`GET /snapshot/{0,1}.jpg`、`POST /api/start`、`POST /api/stop`。相机进程退出时后台会停止共同触发，并在界面报错；可点击“应用并启动”重试。

## 验证

```sh
make test
```

包含 compact RAW12 解码向量、配置约束、分段管道读取及 HTTP 参数/路径验证。硬件验证需在开发板上查看两路帧率、丢帧、触发丢失和实时画面。

2026-10-04 在 `192.168.3.234` 实测：两路 RAW12 共同触发约 319.4 fps，预览约 19.5 fps；RAW8 共同触发 30 fps、RAW10 自由运行 30 fps 正常；这些测试区间两路 V4L2 丢帧均为 0，硬件触发丢失为 0。浏览器收到两路 720×544 JPEG，桌面和手机宽度页面正常。主动终止一个采集进程后，共同 PWM 自动停止，网页重新启动成功。本地及开发板上的解码自检和 6 项 Python 测试通过。当前镜头画面主要为灰度噪声和亮度渐变，以上验证不代表光学成像质量已确认。
