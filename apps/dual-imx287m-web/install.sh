#!/bin/bash
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "$0")" && pwd)
target_dir=/opt/dual-imx287m-web
if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi
python3 -c 'from PIL import Image'
make -C "$project_dir" all
systemctl stop dual-imx287m-web.service 2>/dev/null || true
install -d "$target_dir/static"
install -m 755 "$project_dir/capture-worker" "$target_dir/"
install -m 644 "$project_dir/app.py" "$target_dir/"
install -m 644 "$project_dir"/static/* "$target_dir/static/"
helper_dir=${1:-"$project_dir/../../common/data"}
if [[ -f "$helper_dir/configure-double-imx287m.py" ]]; then
    install -m 755 "$helper_dir/configure-double-imx287m.py" /usr/local/bin/configure-double-imx287m
    install -m 755 "$helper_dir/imx287m-trigger-pwm.py" /usr/local/bin/imx287m-trigger-pwm
fi
test -x /usr/local/bin/configure-double-imx287m
test -x /usr/local/bin/imx287m-trigger-pwm
install -m 644 "$project_dir/dual-imx287m-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dual-imx287m-web.service
echo '网页测试程序已安装，访问 http://开发板IP:8080'
