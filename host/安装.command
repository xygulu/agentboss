#!/bin/bash
# 双击安装：Finder 里双击本文件即可完成部署（首次运行需在弹窗里点「打开」）
cd "$(dirname "$0")" || exit 1
chmod +x ./install.sh
./install.sh
echo
read -p "安装结束，按回车关闭窗口…"
