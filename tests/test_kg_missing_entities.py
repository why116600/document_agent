"""DEF-014 回归：精确标识判定不能只看实体名。

这些用例是纯离线检查，不连接图谱、不调用模型。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from document_agent.backend.bootstrap import ensure_import_paths

ensure_import_paths()

from kg_retrieve_node import KGSearchPlan, _missing_plan_entities


def _plan(*names: str) -> KGSearchPlan:
    return KGSearchPlan(entities=list(names))


def test_entity_name_still_hits():
    entities = {"e1": {"id": "e1", "name": "星桥项目"}}
    assert _missing_plan_entities(_plan("星桥"), entities, {}, {}) == []


def test_identifier_in_chunk_text_hits():
    entities = {"e1": {"id": "e1", "name": "星桥项目"}}
    chunks = {"c1": {"id": "c1", "text": "测试标识：KG-DEMO-20261102"}}
    assert _missing_plan_entities(_plan("KG-DEMO-20261102"), entities, chunks, {}) == []


def test_identifier_in_chunk_snippet_hits():
    chunks = {"c1": {"id": "c1", "snippet": "原文片段…KG-DEMO-20261102…"}}
    assert _missing_plan_entities(_plan("KG-DEMO-20261102"), {}, chunks, {}) == []


def test_identifier_in_document_name_hits():
    documents = {
        "d1": {
            "id": "d1",
            "full_name": "KG-DEMO-20261102说明.docx",
            "path": "/docs/KG-DEMO-20261102说明.docx",
        }
    }
    assert _missing_plan_entities(_plan("KG-DEMO-20261102"), {}, {}, documents) == []


def test_entity_alias_counts_as_evidence():
    entities = {
        "e1": {"id": "e1", "name": "星桥项目", "aliases": ["KG-DEMO-20261102"]}
    }
    assert _missing_plan_entities(_plan("KG-DEMO-20261102"), entities) == []


def test_absent_identifier_is_still_missing():
    entities = {"e1": {"id": "e1", "name": "星桥项目"}}
    chunks = {"c1": {"id": "c1", "text": "星桥项目演示组，借用期限三天"}}
    assert _missing_plan_entities(
        _plan("KG-DEMO-20261111-NOMATCH"), entities, chunks, {}
    ) == ["KG-DEMO-20261111-NOMATCH"]


def test_generic_entity_does_not_mask_specific_name():
    entities = {"e1": {"id": "e1", "name": "设备"}}
    assert _missing_plan_entities(_plan("云杉设备"), entities, {}, {}) == ["云杉设备"]


def test_full_width_and_case_variants_hit():
    chunks = {"c1": {"id": "c1", "text": "标识 kg－demo－20261102"}}
    assert _missing_plan_entities(_plan("KG-DEMO-20261102"), {}, chunks, {}) == []


def test_short_plan_name_is_ignored():
    assert _missing_plan_entities(_plan("A"), {}, {}, {}) == []


def test_empty_plan_is_not_missing():
    assert _missing_plan_entities(_plan(), {}, {}, {}) == []
