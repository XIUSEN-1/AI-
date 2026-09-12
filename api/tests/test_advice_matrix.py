"""分级建议库（seeds/advice_matrix.json，6 维 × L1~L5 共 30 格）与报告融合。"""

import json
from pathlib import Path

from app.engine.adaptive import DIMENSIONS
from app.llm.mock import MockChat
from app.report import generate

MATRIX_PATH = Path(__file__).resolve().parents[2] / "seeds" / "advice_matrix.json"

LEVEL_KEYS = [f"L{i}" for i in range(1, 6)]


def _matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _cells() -> list[tuple[str, str, dict]]:
    matrix = _matrix()
    return [(d, lvl, matrix[d][lvl]) for d in DIMENSIONS for lvl in LEVEL_KEYS]


def test_matrix_covers_30_cells_with_valid_schema():
    matrix = _matrix()
    assert set(matrix) == set(DIMENSIONS)
    for d in DIMENSIONS:
        assert set(matrix[d]) == set(LEVEL_KEYS), f"{d} 缺等级格"
    for d, lvl, cell in _cells():
        assert isinstance(cell["summary"], str) and len(cell["summary"]) >= 20, f"{d}-{lvl} summary"
        assert 2 <= len(cell["resources"]) <= 4, f"{d}-{lvl} 资源须 2~4 项"
        for r in cell["resources"]:
            assert set(r) == {"title", "type", "note"}
            assert all(isinstance(v, str) and v.strip() for v in r.values()), f"{d}-{lvl} 资源字段"
        assert 2 <= len(cell["exercises"]) <= 3, f"{d}-{lvl} 练习须 2~3 项"
        assert all(isinstance(e, str) and e.strip() for e in cell["exercises"]), f"{d}-{lvl} 练习条目"
        assert isinstance(cell["promotion"], str) and cell["promotion"].strip(), f"{d}-{lvl} promotion"


def test_matrix_resources_are_real_names_without_urls():
    """资源只写可检索名称+平台，不编造 URL。"""
    for d, lvl, cell in _cells():
        for r in cell["resources"]:
            blob = r["title"] + r["type"] + r["note"]
            assert "http" not in blob.lower() and "www." not in blob.lower(), f"{d}-{lvl} {r['title']}"


def test_advice_cell_hits_dimension_and_level():
    assert generate._advice_cell("D1", 3) == _matrix()["D1"]["L3"]
    assert generate._advice_cell("D6", 5) == _matrix()["D6"]["L5"]
    assert generate._advice_cell("D9", 1) is None  # 非法维度
    assert generate._advice_cell("D1", 0) is None  # 等级越界
    assert generate._advice_cell("D1", 6) is None


def test_matrix_load_failure_falls_back_to_template(monkeypatch, tmp_path):
    """建议库文件缺失/损坏：不 crash，建议回退旧模板且 source 标记 template。"""
    monkeypatch.setattr(generate, "_ADVICE_MATRIX_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(generate, "_ADVICE_MATRIX", None)
    assert generate._advice_cell("D1", 1) is None
    from tests.test_report import _dims

    dims, gaps = _dims(), ["D1", "D6"]
    advice, source, detail = generate.generate_llm_advice(dims, gaps, None)
    assert source == "template"
    assert advice == _template_for(dims, gaps)
    assert [e["source_type"] for e in detail] == ["template", "template"]

    bad = tmp_path / "broken.json"
    bad.write_text("{不是 JSON", encoding="utf-8")
    monkeypatch.setattr(generate, "_ADVICE_MATRIX_PATH", bad)
    monkeypatch.setattr(generate, "_ADVICE_MATRIX", None)
    assert generate._advice_cell("D1", 1) is None  # 解析失败同样回退


def _template_for(dims: list[dict], gaps: list[str]) -> list[str]:
    by_dim = {x["dimension"]: x for x in dims}
    return [
        f"「{by_dim[d]['name']}」当前 {by_dim[d]['level_name']}："
        + (generate.ADVICE_LOW[d] if by_dim[d]["level"] <= 2 else generate.ADVICE_HIGH[d])
        for d in sorted(gaps)
    ]


def test_llm_prompt_contains_cell_material():
    """LLM 路径：prompt 须追加短板维度命中的格子内容（摘要/资源/练习/晋级标准）。"""
    from tests.test_report import _dims

    chat = MockChat([json.dumps({"advice": ["基于格子组装的建议"]}, ensure_ascii=False)])
    advice, source, detail = generate.generate_llm_advice(_dims(), ["D1", "D6"], chat)
    assert source == "llm"
    assert advice == ["基于格子组装的建议"]
    assert detail == [{"text": "基于格子组装的建议", "source_type": "llm"}]
    text = "\n".join(m["content"] for m in chat.calls[0]["messages"])
    for d in ("D1", "D6"):
        cell = _matrix()[d]["L1"]  # _dims 弱维 level=1
        assert cell["summary"][:15] in text, d
        assert all(r["title"] in text for r in cell["resources"]), d
        assert all(e[:10] in text for e in cell["exercises"]), d
        assert cell["promotion"] in text, d
    assert "分级建议素材" in text  # 素材区块显式标注
    assert "素材" in chat.calls[0]["messages"][0]["content"]  # 系统提示要求基于素材组装


def test_fallback_renders_cell_content():
    """无 LLM 回退：格子内容直渲染（summary+资源列表+练习任务+晋级标准），明细带结构化格子。"""
    from tests.test_report import _dims

    advice, source, detail = generate.generate_llm_advice(_dims(), ["D1", "D6"], None)
    assert source == "cell"
    cell = _matrix()["D1"]["L1"]
    assert cell["summary"][:15] in advice[0]
    assert all(r["title"] in advice[0] for r in cell["resources"])
    assert all(e[:10] in advice[0] for e in cell["exercises"])
    assert cell["promotion"] in advice[0]
    assert [e["text"] for e in detail] == advice
    assert detail[0] == {
        "dimension": "D1",
        "level": 1,
        "text": advice[0],
        "source_type": "cell",
        "cell": cell,
    }
    assert detail[1]["dimension"] == "D6" and detail[1]["source_type"] == "cell"
