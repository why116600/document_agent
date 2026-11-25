from pathlib import Path
import json
import sys
from unittest.mock import Mock, patch

import pytest
from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from document_agent.backend.bootstrap import ensure_import_paths

ensure_import_paths()
from document_agent.agent import agent_core
from document_agent.skills.publish import publish_skill
from intent_node import route_after_intent


@pytest.fixture
def prepare(tmp_path, monkeypatch):
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    monkeypatch.setenv("DOCUMENT_SKILLS_DIR", str(skill_dir))
    (skill_dir / "weekly.json").write_text(
        json.dumps({"id": "weekly", "name": "周报规范"}), encoding="utf-8"
    )
    publish_skill("weekly")

    def run(request, reply, filename=None, *, no_model=False):
        files = []
        if filename:
            path = tmp_path / filename
            doc = Document()
            doc.add_paragraph("待校对的正文")
            doc.save(path)
            files.append(str(path))

        llm = Mock()
        def structured(schema, **kwargs):
            parsed = schema.model_validate(reply) if reply is not None else None
            return Mock(invoke=Mock(return_value={"parsed": parsed}))

        llm.with_structured_output.side_effect = structured
        with patch.object(agent_core, "get_deepseek_llm", return_value=None if no_model else llm):
            core = agent_core.AgentCore()
            return core.prepare_task(request, files, output_dir=str(tmp_path / "out"))

    return run


def test_edit_document_named_standard(prepare):
    result = prepare("请修改操作规范中的错字", {"task_type": "rewrite"}, "操作规范.docx")
    assert route_after_intent(result) == "continue"
    assert result["user_intent"] == "rewrite"
    assert Path(result["rewrite_file"]).name == "操作规范.docx"
    assert result["save_path"] != result["rewrite_file"]


def test_edit_without_document_asks_for_target(prepare):
    result = prepare("请修改操作规范中的错字", {"task_type": "rewrite"})
    assert route_after_intent(result) == "end"
    assert "docx" in result["error"]
    assert result["error_code"] != "MAIN_ENTRY_MANAGEMENT_FORBIDDEN"


def test_write_about_management(prepare):
    result = prepare("写一份文档，说明如何发布 Skill", {"task_type": "new"})
    assert route_after_intent(result) == "continue"
    assert result["user_intent"] == "new"


def test_apply_published_skill_to_document(prepare):
    result = prepare("按周报 Skill 修改这份报告",
                     {"task_type": "rewrite", "skill": "weekly"}, "报告.docx")
    assert route_after_intent(result) == "continue"
    assert result["skill"]["id"] == "weekly"
    assert Path(result["rewrite_file"]).name == "报告.docx"


def test_manage_skill_with_attachment(prepare):
    result = prepare("把周报 Skill 的触发词改成“每周总结”",
                     {"task_type": "rewrite", "management_request": True,
                      "management_action": "update"}, "报告.docx")
    assert route_after_intent(result) == "end"
    assert result["error_code"] == "MAIN_ENTRY_MANAGEMENT_FORBIDDEN"


def test_document_and_skill_share_a_name(prepare):
    result = prepare("修改周报规范", {"task_type": "rewrite", "ambiguous_skill_target": True},
                     "周报规范.docx")
    assert route_after_intent(result) == "end"
    assert result["error_code"] == "INTENT_TARGET_AMBIGUOUS"
    assert not result["save_path"]


@pytest.mark.parametrize("no_model", [False, True])
@pytest.mark.parametrize("request_text, filename", [
    ("修改周报规范", "周报规范.docx"),
    ("把周报 Skill 的触发词改成“每周总结”", None),
])
def test_unparsed_skill_change_stops(prepare, no_model, request_text, filename):
    result = prepare(request_text, None, filename, no_model=no_model)
    assert route_after_intent(result) == "end"
    assert result["error_code"] == "INTENT_TARGET_AMBIGUOUS"
    assert not result["save_path"]


def test_unparsed_document_rewrite_keeps_original_error(prepare):
    result = prepare("请润色这份报告", None, "报告.docx")
    assert result["error_code"] == "INTENT_PARSE_FAILED"
    assert result["intent_diagnostics"]["fallback"] == "error"


@pytest.mark.parametrize("request_text", ["写一份工作总结", "写一份文档，说明如何发布 Skill"])
def test_unparsed_new_document_keeps_fallback(prepare, request_text):
    result = prepare(request_text, None)
    assert route_after_intent(result) == "continue"
    assert result["user_intent"] == "new"
    assert result["intent_diagnostics"]["fallback"] == "new"
