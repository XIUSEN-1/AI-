"""真实 LLM 判题冒烟脚本（不入 pytest）：对 1 道 D2 进阶开放题跑真实 judge_answer。

worktree 无 api/.env，Key 在主仓 .env 时用 --env-file 指过去（或直接用环境变量）：

    cd api
    python scripts/smoke_llm.py --env-file C:/Users/tsbf-cjh1/Documents/ai-compass/api/.env

退出码：0 = 判题成功；1 = 判题降级（关键词兜底，LLM 路径未走通）；2 = LLM 不可用（无 Key/上游异常）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_API_DIR))  # 任意 cwd 下运行都能 import app.*

SAMPLE_SUBMISSION = """1）缺少的关键要素至少有：任务目标（整理到什么程度、用于什么场景未说明）、输出格式（无任何结构约定）、背景材料（未提供记录本体，也未说明会议类型与篇幅）、受众（谁看这份纪要）、约束（篇幅、保密、语言风格）。
2）改写示例："你是资深会议秘书。请将下面的项目周会记录整理为结构化纪要。会议记录：【在此粘贴原始记录】。输出格式分三节：一、决议事项（含负责人与截止时间）；二、待办清单（表格：事项/负责人/截止日）；三、遗留讨论点。约束：忠实原文、不得虚构内容，中文输出，全文不超过 500 字。"
3）任务目标缺失导致 AI 只能猜整理方向、产出泛化；格式约定直接解决"杂乱不可用"；材料占位保证信息完整；受众与约束分别控制详略与安全边界。"""


def _load_env_file(path: Path) -> None:
    """把 --env-file 的 KEY=VALUE 注入环境（已存在的环境变量优先，与 config 语义一致）。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _load_question(code: str) -> dict:
    seed_path = _API_DIR.parent / "seeds" / "questions" / f"{code.split('-')[0]}.json"
    questions = json.loads(seed_path.read_text(encoding="utf-8"))["questions"]
    for q in questions:
        if q["id"] == code:
            return q
    raise SystemExit(f"种子库中找不到题目 {code}（查找路径 {seed_path}）")


def main() -> int:
    parser = argparse.ArgumentParser(description="真实 LLM 判题冒烟（M2a 唯一真实 API 验证）")
    parser.add_argument("--env-file", type=Path, default=None, help="API Key 所在 .env（worktree 无 .env 时指向主仓）")
    parser.add_argument("--code", default="D2-A01", help="种子库开放题 id（默认 D2-A01）")
    args = parser.parse_args()

    if args.env_file is not None:
        _load_env_file(args.env_file)

    from app.config import get_settings
    from app.judge.pipeline import judge_answer
    from app.llm.provider import ProviderUnavailableError, chat_completion

    question = _load_question(args.code)

    def chat_fn(messages: list[dict], **kwargs) -> str:
        return chat_completion(messages, **kwargs)  # model_role 等由 judge_answer 传入

    print(f"题目：{question['id']}（{question['dimension']} / {question['tier']} / 难度 {question['difficulty']} / {question['type']}）")
    print(f"题干：{question['stem'][:60]}…")
    print(f"模型：{get_settings().model_judge}")
    print("-" * 60)

    try:
        result = judge_answer(question, SAMPLE_SUBMISSION, chat_fn)
    except ProviderUnavailableError as exc:
        print(f"[FAIL] LLM 不可用：{exc}")
        return 2

    print(f"score        = {result.score}")
    print(f"hits         = {result.hits}")
    print(f"strengths    = {result.strengths}")
    print(f"gaps         = {result.gaps}")
    print(f"rationale    = {result.rationale}")
    print(f"degraded     = {result.degraded}")
    print(f"needs_review = {result.needs_review}")
    print(f"runs         = {result.runs}")
    return 1 if result.degraded else 0


if __name__ == "__main__":
    raise SystemExit(main())
