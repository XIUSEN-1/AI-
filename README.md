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

- **前端**：React 19 + TypeScript + Vite + Tailwind CSS + shadcn/ui + Recharts
- **后端**：Python FastAPI + SQLite (WAL) + SQLAlchemy 2.0
- **大模型**：DeepSeek（OpenAI 兼容接入，provider 可切换），用于对话式测评、LLM 判题与报告生成
- **部署**：单容器（FastAPI 托管前端静态产物），秒悟 Meoo → 阿里云

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
python -m app.seed          # 导入题库种子（90 题，幂等）
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

## 开源使用声明

本项目基于以下开源技术构建（详见 docs/开源使用声明.md）：React、Vite、Tailwind CSS、shadcn/ui、Recharts、FastAPI、SQLAlchemy、openai-python 等。
