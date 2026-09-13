#!/bin/sh
# Meoo 镜像部署构建脚本：在远程 Linux 构建机 /code 目录执行
# 前端产物 web/dist 已随源码上传（本地 pnpm build），此处仅装后端依赖
set -e
cd /code
echo "[setup] 安装后端依赖（阿里云 PyPI 镜像）"
pip install -r api/requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --no-cache-dir
echo "[setup] 完成（数据库表结构与种子在 start.sh 首次启动时自动初始化）"
