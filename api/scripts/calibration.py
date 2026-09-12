"""评分准确性校准实验脚本（真实调用 LLM，不入 pytest）。

流程（spec §9）：
  1. 从种子题库抽取全部主观 open 题（6 维度覆盖）；
  2. flash（model_chat）按四档质量档位生成作答：优 / 中 / 差 / 跑题（各 ~25%）；
  3. judge_answer 真实判分（v4-pro 双跑 + 降级，与线上管线同一路径）；
  4. 独立标注：同 v4-pro 模型换一个独立的评分官 prompt（只看题面+评分要点+作答，
     与判分管线互不可见、盲于质量档位），作"人工基准"的 AI 代理；
  5. 计算 Pearson / Spearman / 二次加权 Kappa / 一致率 / 混淆矩阵，
     写 docs/calibration/samples.csv，控制台输出完整指标（原样进验证报告）。

用法（worktree api/ 下，api/.env 提供 DEEPSEEK_API_KEY）：
    python scripts/calibration.py --samples 50 [--base-url URL] [--out DIR] [--seed 42]

退出码：0 = 全部样本成功；1 = 部分样本失败（已跳过，指标按实际 N 计算）；2 = LLM 不可用。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from pathlib import Path

_API_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _API_DIR.parent
sys.path.insert(0, str(_API_DIR))  # 任意 cwd 下运行都能 import app.*

from app.judge.pipeline import JudgeResult, judge_answer
from app.judge.stats import kappa_quadratic, pearson, spearman
from app.llm.provider import ProviderUnavailableError, chat_completion

SEEDS_DIR = _REPO_ROOT / "seeds" / "questions"
TIERS = ["优", "中", "差", "跑题"]

# 四档质量生成提示词（generator 可见评分要点；judge/标注官全程盲于档位）
GEN_PROMPTS = {
    "优": (
        "你是一位能力出众的学员。请认真回答下面的题目。要求：\n"
        "1. 逐条覆盖【评分要点】中的每一条；\n"
        "2. 每条要点都给出具体、可操作的展开（方法/步骤/示例），不说空话；\n"
        "3. 用序号分点作答，语言自然，像真人学员的认真作答。\n\n"
        "【题目】{stem}\n【评分要点】{points}"
    ),
    "中": (
        "你是一位中等水平的学员。请回答下面的题目。要求：\n"
        "1. 只覆盖【评分要点】中约一半的要点，覆盖到的要点有具体内容；\n"
        "2. 其余要点遗漏或只用一句空话带过；\n"
        "3. 分点作答，篇幅中等。\n\n"
        "【题目】{stem}\n【评分要点】{points}"
    ),
    "差": (
        "你是一位准备不足的学员。请回答下面的题目。要求：泛泛而谈，只说"
        "“要认真学习”“要与时俱进”这类正确的套话，不涉及【评分要点】中的任何具体内容，"
        "篇幅较短，不用分点结构。\n\n【题目】{stem}"
    ),
    "跑题": (
        "请写一段与下面题目完全无关的回答（答非所问）：文字本身通顺连贯，"
        "但与题目问的事情毫无关系（例如谈谈今天的天气、讲一个无关的经历）。"
        "绝对不要提及题目要求回答的内容。\n\n【题目】{stem}"
    ),
}

# 独立标注官（人工基准代理）：只看题面+评分要点+作答，输出先于一切参考的独立评分
ANNOTATOR_SYSTEM = (
    "你是一名独立评审专家，正在为一次 AI 能力测评的评分质量校准做人工标注。"
    "你会看到题目、评分要点和一位学员的作答。请独立判断作答质量并给出 0~4 的整数评分："
    "0=完全未触及题目要求，1=初步触及但严重不足，2=基本达标，3=良好，4=优秀。"
    "评分只依据题面、评分要点与作答本身，不受任何其他因素影响。"
    "你只输出一个 JSON 对象，不得包含任何其他文字或代码块标记，"
    "键固定为：score（0~4 整数）、rationale（简体中文一句话评分理由）。"
)


def load_open_questions() -> list[dict]:
    """从种子题库加载全部主观 open 题（按 D1..D6 顺序，维度轮转天然均匀）。"""
    questions: list[dict] = []
    for dim in range(1, 7):
        path = SEEDS_DIR / f"D{dim}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        questions.extend(q for q in data["questions"] if q["type"] == "open")
    if not questions:
        raise SystemExit(f"种子题库中无 open 题（{SEEDS_DIR}）")
    return questions


def build_plan(questions: list[dict], n: int, seed: int) -> list[tuple[dict, str]]:
    """样本计划：题目按维度轮转（48 题循环覆盖 6 维度），四档质量各 ~25% 且随机配对
    （固定 seed 可复现；打乱档位避免档位与特定题目绑定）。"""
    rng = random.Random(seed)
    tiers = [TIERS[i % 4] for i in range(n)]
    rng.shuffle(tiers)
    picked = [questions[i % len(questions)] for i in range(n)]
    return list(zip(picked, tiers))


def with_retry(action, *, what: str, tag: str):
    """LLM 调用失败（网络/上游）重试一次，仍失败抛 ProviderUnavailableError。"""
    for attempt in (1, 2):
        try:
            return action()
        except ProviderUnavailableError as exc:
            if attempt == 1:
                print(f"  [{tag}] {what} 失败（{exc}），重试一次…")
                time.sleep(2)
            else:
                raise


def generate_answer(question: dict, tier: str, tag: str) -> str:
    """flash 按档位生成作答；空响应视为失败。"""
    prompt = GEN_PROMPTS[tier].format(
        stem=question["stem"], points="；".join((question.get("rubric") or {}).get("points") or [])
    )
    answer = with_retry(
        lambda: chat_completion(
            [{"role": "user", "content": prompt}],
            model_role="chat",
            temperature=0.7,
            timeout=90,
        ),
        what="生成作答",
        tag=tag,
    ).strip()
    if not answer:
        raise ProviderUnavailableError("生成作答为空")
    return answer


def annotate(question: dict, answer: str, tag: str) -> tuple[int, str]:
    """独立标注官：独立 prompt + JSON 输出（0~4 分 + 理由），与判分管线互不可见。"""
    points = "；".join((question.get("rubric") or {}).get("points") or [])
    user = f"【题目】{question['stem']}\n\n【评分要点】{points}\n\n【学员作答】{answer}"
    messages = [
        {"role": "system", "content": ANNOTATOR_SYSTEM},
        {"role": "user", "content": user},
    ]
    raw = with_retry(
        lambda: chat_completion(messages, model_role="judge", temperature=0.0, json_mode=True, timeout=90),
        what="独立标注",
        tag=tag,
    )
    try:
        data = json.loads(raw)
        score = int(data["score"])
        rationale = str(data["rationale"]).strip()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ProviderUnavailableError(f"标注输出解析失败：{exc}") from exc
    if not 0 <= score <= 4:
        raise ProviderUnavailableError(f"标注分越界：{score}")
    return score, rationale


def confusion_matrix(judge_scores: list[int], ann_scores: list[int]) -> list[list[int]]:
    """5×5 混淆矩阵：行=judge 判分，列=独立标注分。"""
    matrix = [[0] * 5 for _ in range(5)]
    for j, a in zip(judge_scores, ann_scores):
        matrix[j][a] += 1
    return matrix


def render_matrix(matrix: list[list[int]]) -> str:
    header = "judge\\标注 " + " ".join(f"{c:>7}" for c in range(5))
    rows = [header]
    for i, row in enumerate(matrix):
        rows.append(f"    {i}     " + " ".join(f"{v:>7}" for v in row))
    return "\n".join(rows)


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["题号", "维度", "档位", "judge分", "标注分", "judge理由摘要", "标注理由摘要"])
        for r in rows:
            writer.writerow(
                [
                    r["question_id"],
                    r["dimension"],
                    r["tier"],
                    r["judge"].score,
                    r["ann_score"],
                    r["judge"].rationale[:80],
                    r["ann_rationale"][:80],
                ]
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="评分准确性校准实验（真实 LLM 判分 vs 独立标注）")
    parser.add_argument("--samples", type=int, default=50, help="样本数（默认 50）")
    parser.add_argument("--base-url", default=None, help="覆盖 DEEPSEEK_BASE_URL（如指向代理网关）")
    parser.add_argument("--out", type=Path, default=_REPO_ROOT / "docs" / "calibration", help="CSV 输出目录")
    parser.add_argument("--seed", type=int, default=42, help="档位洗牌随机种子（默认 42，保证可复现）")
    args = parser.parse_args()

    if args.base_url:
        os.environ["DEEPSEEK_BASE_URL"] = args.base_url  # env 优先于 .env，且先于首次 get_settings()

    from app.config import get_settings

    settings = get_settings()
    if not settings.deepseek_api_key:
        print("[FAIL] 缺少 DEEPSEEK_API_KEY（api/.env 或环境变量）")
        return 2

    questions = load_open_questions()
    plan = build_plan(questions, args.samples, args.seed)
    print("=" * 64)
    print("AI 能力罗盘 · 评分准确性校准实验")
    print(f"样本数={len(plan)}  开放题库={len(questions)} 道（6 维度）")
    print(f"judge 模型={settings.model_judge}（双跑）  生成模型={settings.model_chat}  标注模型={settings.model_judge}（独立 prompt）")
    print(f"base_url={settings.base_url}  seed={args.seed}  开始时间={time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 64)

    rows: list[dict] = []
    failed = 0
    started = time.time()
    for i, (question, tier) in enumerate(plan, 1):
        qid = question["id"]
        tag = f"{i}/{len(plan)}"
        print(f"[{tag}] {qid}（{question['dimension']}）· {tier}档：生成中…")
        try:
            answer = generate_answer(question, tier, tag)
            result: JudgeResult = judge_answer(
                question, answer, lambda ms, **kw: chat_completion(ms, **kw)
            )
            ann_score, ann_rationale = annotate(question, answer, tag)
        except ProviderUnavailableError as exc:
            failed += 1
            print(f"  [{tag}] {qid} 样本失败已跳过：{exc}")
            continue
        rows.append(
            {
                "question_id": qid,
                "dimension": question["dimension"],
                "tier": tier,
                "answer": answer,
                "judge": result,
                "ann_score": ann_score,
                "ann_rationale": ann_rationale,
            }
        )
        print(
            f"  [{tag}] judge={result.score}（runs={result.runs}）标注={ann_score} "
            f"| 标注理由：{ann_rationale[:50]}"
        )

    elapsed = time.time() - started
    n = len(rows)
    print("=" * 64)
    print(f"完成：成功 {n} 份 / 失败跳过 {failed} 份，耗时 {elapsed:.0f}s")
    if n < 2:
        print("[FAIL] 有效样本不足 2，无法计算指标")
        return 2

    judge_scores = [r["judge"].score for r in rows]
    ann_scores = [r["ann_score"] for r in rows]

    def dist(scores: list[int]) -> dict[int, int]:
        return {k: scores.count(k) for k in range(5)}

    matrix = confusion_matrix(judge_scores, ann_scores)
    exact = sum(1 for j, a in zip(judge_scores, ann_scores) if j == a) / n
    adjacent = sum(1 for j, a in zip(judge_scores, ann_scores) if abs(j - a) <= 1) / n
    degraded = sum(1 for r in rows if r["judge"].degraded)
    needs_review = sum(1 for r in rows if r["judge"].needs_review)

    print("===== 校准指标汇总（judge 判分 vs 独立标注） =====")
    print(f"有效样本 N            = {n}")
    print(f"Pearson r             = {pearson(judge_scores, ann_scores):.4f}")
    print(f"Spearman rho          = {spearman(judge_scores, ann_scores):.4f}")
    print(f"二次加权 Kappa        = {kappa_quadratic(judge_scores, ann_scores):.4f}")
    print(f"精确一致率            = {exact:.1%}")
    print(f"相邻一致率（|Δ|≤1）   = {adjacent:.1%}")
    print(f"judge 分档分布        = {dist(judge_scores)}")
    print(f"标注分档分布          = {dist(ann_scores)}")
    print(f"判分降级/需复核样本   = {degraded}/{needs_review}")
    print(f"四档样本数            = { {t: sum(1 for r in rows if r['tier'] == t) for t in TIERS} }")
    print("混淆矩阵（行=judge 判分，列=独立标注分）：")
    print(render_matrix(matrix))

    csv_path = args.out / "samples.csv"
    write_csv(csv_path, rows)
    print(f"原始样本已写出：{csv_path}（{n} 行）")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
