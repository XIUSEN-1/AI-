"""题库查重：题干字符 3-gram Jaccard 相似度（同维度比对，跨维度不比对）。

供 test_bank_content 与后续题库批次复用：
- dedup_pairs(questions, threshold) 返回同维度内题干相似度 > threshold 的题对。
- 同维度且共享任一 tag 的题对，题干相似度须 < 0.4（考点级查重）。
"""

from pathlib import Path

from app.seed import load_seed_files

SEEDS = Path(__file__).resolve().parent.parent.parent / "seeds" / "questions"

OVERALL_LIMIT = 0.6
TAG_LEVEL_LIMIT = 0.4


def stem_ngrams(stem: str, n: int = 3) -> set[str]:
    """题干去空白后按字符切 3-gram 集合（中英文统一按字符处理）。"""
    cleaned = "".join(stem.split())
    return {cleaned[i : i + n] for i in range(len(cleaned) - n + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def stem_similarity(qa: dict, qb: dict) -> float:
    return jaccard(stem_ngrams(qa["stem"]), stem_ngrams(qb["stem"]))


def dedup_pairs(
    questions: list[dict], threshold: float = OVERALL_LIMIT
) -> list[tuple[str, str, float]]:
    """返回同维度内题干相似度 > threshold 的题对 [(id_a, id_b, score)]，跨维度不比对。"""
    pairs: list[tuple[str, str, float]] = []
    qs = list(questions)
    for i in range(len(qs)):
        for j in range(i + 1, len(qs)):
            a, b = qs[i], qs[j]
            if a.get("dimension") != b.get("dimension"):
                continue
            score = stem_similarity(a, b)
            if score > threshold:
                pairs.append((a["id"], b["id"], round(score, 3)))
    return sorted(pairs, key=lambda p: -p[2])


def same_tag_pairs(questions: list[dict]) -> list[tuple[str, str, float]]:
    """返回同维度且共享任一 tag 的题对及其题干相似度。"""
    out: list[tuple[str, str, float]] = []
    qs = list(questions)
    for i in range(len(qs)):
        for j in range(i + 1, len(qs)):
            a, b = qs[i], qs[j]
            if a.get("dimension") != b.get("dimension"):
                continue
            if not set(a.get("tags", [])) & set(b.get("tags", [])):
                continue
            out.append((a["id"], b["id"], round(stem_similarity(a, b), 3)))
    return out


def test_dedup_pairs_helper_behavior():
    base = {"dimension": "DX", "stem": "大模型按语言概率生成文本，缺乏事实校验机制。"}
    near = {"dimension": "DX", "stem": "大模型按语言概率生成文本，缺乏事实校验机制！"}
    far = {"dimension": "DX", "stem": "把讲座录音转成文字稿应优先用语音转写工具。"}
    assert dedup_pairs(
        [{"id": "X1", **base}, {"id": "X2", **near}], threshold=0.5
    ), "近似题干应被检出"
    assert dedup_pairs([{"id": "X1", **base}, {"id": "X3", **far}], threshold=0.5) == []
    cross = {"dimension": "DY", "stem": base["stem"]}
    assert dedup_pairs([{"id": "X1", **base}, {"id": "Y1", **cross}], threshold=0.5) == []


def test_bank_no_high_similarity_pairs():
    pairs = dedup_pairs(load_seed_files(SEEDS))
    assert not pairs, f"同维度题干相似度 > {OVERALL_LIMIT} 的题对：{pairs}"


def test_bank_same_tag_pairs_below_threshold():
    offenders = [
        (a, b, s) for a, b, s in same_tag_pairs(load_seed_files(SEEDS)) if s >= TAG_LEVEL_LIMIT
    ]
    assert not offenders, f"同维度同 tag 题对相似度 >= {TAG_LEVEL_LIMIT}：{offenders}"
