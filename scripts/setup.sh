#!/bin/sh
# Meoo 镜像部署构建脚本：在远程 Linux 构建机 /code 目录执行
set -e
cd /code

echo "[setup] 安装后端依赖"
pip install -r api/requirements.txt

echo "[setup] 构建前端"
cd web
corepack enable 2>/dev/null || npm install -g pnpm@10
pnpm install --silent
pnpm build
cd /code

echo "[setup] 完成（数据库表结构与种子在 start.sh 首次启动时自动初始化）"
