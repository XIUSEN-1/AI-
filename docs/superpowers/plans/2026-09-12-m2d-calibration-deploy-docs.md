# M2d 校准实验·可视化·部署·提交材料 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** ①评分准确性校准实验（50 样本真实 LLM 判分 vs 独立标注，产出验证报告——评分权重 20% 的证明材料）；②报告页成长趋势折线 + 导出 PNG；③部署就绪（Dockerfile/compose/env 加固/Meoo 指南）；④赛题四份提交材料文档 + 演示视频脚本。

**Architecture:** 校准脚本独立（`api/scripts/calibration.py`，不入 pytest）：样本=脚本按质量档位用 flash 生成（优/中/差/跑题 ×6 维度覆盖）→ judge 管线真实判分 → 独立标注（同 DeepSeek 换判分官 prompt、不含 rubric 之外提示，作"人工基准"代理——报告如实声明标注方法）→ Pearson/Spearman/quadratic Kappa → `docs/评分准确性验证报告.md` + `docs/calibration/samples.csv`。前端：ReportPage 加历次趋势折线（`/api/reports/mine` 已有数据）与 html-to-image 导出按钮。部署：多阶段 Dockerfile + compose + CORS/JWT env 化（默认值不变）+ README「部署」节（Meoo 指引）。

**Tech Stack:** 同前 + html-to-image（pnpm add）。

## Global Constraints

- 校准脚本真实调用 LLM（.env key），单测只测纯函数（Kappa/相关系数计算用构造数据）。
- 提交材料文档中文、面向评委（功能设计文档含测评流程图 mermaid、每维度题例各 1、三模式说明；技术架构含 mermaid 架构图+选型表+核心流程时序；未来发展文档呼应赛题"与 AI 智能·教学辅具数据打通、学习路径推荐、市场推广"；开源使用声明列全部开源件与用途；演示视频脚本按现有真实系统分镜 5-6 分钟）。
- 文档数据必须来自真实仓库（题数 300、维度表、里程碑）——禁止编造指标。
- CORS：`COMPASS_CORS_ORIGINS` env（逗号分隔，默认现状）；JWT：secret 为默认值时启动打 WARNING 日志。
- 每任务 commit；全量门禁；中文。

---

### Task 1: 校准实验（脚本真实运行 + 验证报告）

**Files:** Create `api/scripts/calibration.py`、`docs/评分准确性验证报告.md`、`docs/calibration/samples.csv`
**Interfaces:** 校准纯函数 `kappa_quadratic(a: list[int], b: list[int]) -> float`、`pearson(x, y) -> float`（放 `api/app/judge/stats.py`，可测）；脚本参数 `--samples 50 --base-url`；样本分布 6 维度覆盖、4 档质量各 ~25%。
**Test:** `api/tests/test_stats.py`（构造数据验证 kappa/pearson 数学正确性）。
- [ ] stats TDD → 脚本 → **真实运行一次**（输出/指标原样进报告）→ `docs/评分准确性验证报告.md`（方法学如实：AI 双盲标注代理人工基准、指标、分档混淆矩阵、局限）→ commit `feat: 评分准确性校准实验与验证报告`

### Task 2: 成长趋势 + 导出 PNG

**Files:** Modify `web/src/pages/ReportPage.tsx`（mine 拉历史→趋势折线卡（≥2 条显示）；导出按钮 html-to-image 下载报告卡）；`web/package.json` +html-to-image
**Test:** vitest 趋势卡渲染冒烟；build。
- [ ] 门禁 → commit `feat: 报告成长趋势与导出 PNG`

### Task 3: 部署就绪 + 提交材料

**Files:** Create `Dockerfile`、`docker-compose.yml`、`docs/部署指南.md`、`docs/提交材料/功能设计文档.md`、`docs/提交材料/技术架构文档.md`、`docs/提交材料/未来发展文档.md`、`docs/提交材料/开源使用声明.md`、`docs/提交材料/演示视频脚本.md`；Modify `api/app/main.py`（CORS env）、`api/app/config.py` 或 main（JWT 弱密钥 WARNING）、`README.md`（部署节）、`api/.env.example`（JWT_SECRET/COMPASS_CORS_ORIGINS）
**Test:** build + docker 不实际构建（写文档级）；JWT 警告有测试（monkeypatch settings 默认值 → caplog）。
- [ ] 材料齐 → commit `feat: 部署就绪与赛题提交材料`

---

## Self-Review

- spec §9（校准）/§7（趋势导出）/§10（部署清单）全覆盖；提交材料=赛题"提交材料"四件套+视频脚本。
- 校准样本真实判分依赖 key——实现者真实运行并把原始输出留报告（verification 纪律）。
- 演示视频仅脚本（拍摄/剪辑留用户）。
