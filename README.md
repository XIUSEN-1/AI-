# AI 能力罗盘 · AI Capability Compass

> 2026"数字马力杯"第十三届浙江省大学生服务外包创新应用大赛 · A01「AI 能力测评智能体」参赛作品

面向高校学员的 AI 能力自适应测评智能体：通过**对话式测评、实操任务考核、自适应客观题**三种模式，对学员在 6 个核心维度的 AI 能力进行量化评估，输出能力雷达画像、等级认定与个性化学习路径。

## 能力维度

| 维度 | 考察内容 |
| --- | --- |
| AI 基础认知 | 基本概念、常见模型特点、伦理与安全认知 |
| 提示词工程 | 指令清晰度、任务拆解、上下文编排、少样本示例设计 |
| AI 工具使用 | 通用大模型、办公 AI 插件、数据分析 AI |
| AI 结果评估与优化 | 识别错误、偏见与幻觉；提出改进指令 |
| 人机协同解决问题 | 复杂任务分步借助 AI 完成 |
| AI 伦理与合规 | 隐私保护、版权意识、负责任使用 |

## 技术栈

- **前端**：React 19 + TypeScript + Vite + Tailwind CSS v4 + shadcn/ui + Recharts
- **后端**：Python 3.13 FastAPI + SQLite (WAL) + SQLAlchemy 2.0 + PyJWT
- **大模型**：DeepSeek（OpenAI 兼容接入，provider 可切换）——判题 `deepseek-v4-pro`（双跑一致性，Kappa 0.983）/ 对话与建议 `deepseek-flash`
- **测试**：pytest 268 项（零真实 LLM 调用）+ vitest 27 项 + tsc 构建门禁
- **部署**：单容器（FastAPI 托管前端静态产物），Docker Compose / 秒悟 Meoo 镜像部署

详细选型理由与模块划分见 **[docs/技术栈说明.md](docs/技术栈说明.md)**。

## 功能速览

- **四阶段测评**：自适应客观题（动态难度、能力轨迹可视化）→ 对话式测评（AI 考官三档追问、SSE 流式）→ 实操任务（内置真实 AI 协作窗 + 产物提交）→ 异步并行智能判题（实时进度）
- **报告**：六维雷达、L1~L5 等级、逐题回显（含 AI 评分理由）、AI 生成个性化学习建议（分级建议库 30 格融合）、成长趋势折线、导出 PNG
- **跳过与续答**：对话/实操可跳过（如实标注）；中途退出可断线续答
- **教师端**：建班发邀请码、班级六维看板、共性短板 TOP3、学员成长曲线、CSV 导出
- **管理端**：题库 CRUD（1000 题）、AI 判分人工复核队列、教师账号管理
- **错题本与练习**：按考点汇总错题，一键生成薄弱练习卷（即时判分，不计入正式等级）

## 使用说明

三种角色的完整操作手册见 **[docs/使用说明.md](docs/使用说明.md)**。内置账号：管理员 `admin / admin123`（生产环境务必修改）。

## 目录结构

```
ai-compass/
├── web/      # 前端 SPA
├── api/      # FastAPI 后端
├── seeds/    # 题库种子数据（JSON，版本化）
└── docs/     # 设计文档、提交材料
```

## 开发

```bash
# 后端（api/）
cd api
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
python -m app.seed          # 导入题库种子（1000 题，幂等）
pytest                      # 运行后端全部测试
uvicorn app.main:app --reload --port 8000   # 开发模式（后端）

# 前端（web/，Node + pnpm）
cd web
pnpm install
pnpm dev        # Vite 开发服务器（http://localhost:5173）
pnpm test       # vitest
pnpm build      # 产出 web/dist

# 单进程模式（生产形态：FastAPI 托管 web/dist）
# 先执行 pnpm build，再：
cd ../api && source .venv/Scripts/activate
uvicorn app.main:app --port 8000   # 打开 http://localhost:8000，API 与页面同源
```

## 部署

```bash
# 1) 准备环境变量：复制 api/.env.example 到仓库根目录 .env，填入 DEEPSEEK_API_KEY
#    生产环境务必同时更换 COMPASS_JWT_SECRET（保持默认值启动会打 WARNING 日志）
cp api/.env.example .env

# 2) Docker 一键构建并启动（FastAPI 单容器托管前端产物，题库种子随启动幂等导入）
docker compose up -d --build
# 打开 http://localhost:8000，健康检查：curl http://localhost:8000/api/health
```

公网部署（秒悟 Meoo → 阿里云函数计算）与端口/env/数据卷说明见 **docs/部署指南.md**。

## 开源使用声明

本项目基于以下开源技术构建（详见 docs/提交材料/开源使用声明.md）：React、Vite、Tailwind CSS、shadcn/ui、Recharts、FastAPI、SQLAlchemy、httpx、PyJWT 等。
