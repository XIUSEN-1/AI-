#!/bin/sh
# Meoo 镜像部署启动脚本：绑定 0.0.0.0:${PORT:-9000}
set -e
cd /code/api

# FC 容器工作目录只读：数据库放可写的 /tmp（Meoo 无持久化，实例生命周期内有效）
export COMPASS_DB="${COMPASS_DB:-/tmp/compass.db}"

# 首次启动自动建表/迁移并导入 1000 题种子（幂等）
python -m app.seed

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-9000}"
