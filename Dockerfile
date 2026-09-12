# ---------- 构建阶段：Node 构建前端 SPA ----------
FROM node:24-slim AS web-build
WORKDIR /build
COPY web/package.json web/pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

# ---------- 运行阶段：Python 运行 FastAPI 并托管前端静态产物 ----------
FROM python:3.13-slim
WORKDIR /api
COPY api/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY api/app ./app
COPY seeds /seeds
# main.py 约定 _DIST = main.py 向上三级 / web / dist，故静态产物放 /web/dist
COPY --from=web-build /build/dist /web/dist
EXPOSE 8000
# 启动前幂等导入题库种子（300 题，重复启动只做增量比对）；PORT 供 PaaS 覆盖
CMD ["sh", "-c", "python -m app.seed && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
