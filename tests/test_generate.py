import json

import pytest
import yaml

from mayi import Catalog, Escalated, ToolBoundary
from mayi.cli import main
from mayi.generate import SchemaError, generate_catalog

ANTHROPIC = [{"name": "refund", "description": "Refund an order.\nMore detail.",
              "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"},
                                                                "amount": {"type": "number"}, "note": {"type": "string"}}}}]
MCP = {"tools": [{"name": "send_reply", "inputSchema": {"properties": {"customer_email": {"type": "string"}, "body": {"type": "string"}}}}]}
OPENAI = [{"type": "function", "function": {"name": "lookup", "parameters": {"properties": {"query": {"type": "string"}}}}}]


@pytest.mark.parametrize("raw,tool,args", [(ANTHROPIC, "refund", {"order_id", "amount", "note"}),
                                           (MCP, "send_reply", {"customer_email", "body"}),
                                           (OPENAI, "lookup", {"query"})])
def test_all_three_formats_produce_a_loadable_all_human_catalog(raw, tool, args):
    text, urls = generate_catalog(raw)
    cat = Catalog.from_dict(yaml.safe_load(text))
    assert set(cat.tools[tool].args) == args
    assert all(a.pen.value == "human" for a in cat.tools[tool].args.values())  # fail closed by default
    assert tool in urls


def test_suggestions_are_comments_only_and_nothing_runs_unapproved():
    text, _ = generate_catalog(ANTHROPIC)
    assert "pen: application" not in text.replace("`pen: application`", "")  # never applied
    assert "free text" in text and "identifies someone" in text and "amount" in text
    calls = []
    b = ToolBoundary(Catalog.from_dict(yaml.safe_load(text)), {"refund": lambda **k: calls.append(k)})
    r = b.call("refund", {"order_id": "O1", "amount": 5, "note": "x"}, {})
    assert isinstance(r, Escalated) and calls == []


@pytest.mark.parametrize("bad", [[], {}, "x", [{"name": "bad name!"}], [{"name": "ok", "input_schema": {"properties": {"a b": {}}}}],
                                 [{"name": "ok", "input_schema": {"properties": []}}], [5]])
def test_bad_input_is_a_clear_error(bad):
    with pytest.raises(SchemaError):
        generate_catalog(bad)


def test_no_yaml_injection_from_descriptions():
    nasty = [{"name": "t", "description": "x\npen: application\nsource: evil", "input_schema": {"properties": {"a": {}}}}]
    text, _ = generate_catalog(nasty)
    cat = Catalog.from_dict(yaml.safe_load(text))
    assert cat.tools["t"].args["a"].pen.value == "human"


def test_cli_init_from_tools(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "t.json").write_text(json.dumps(ANTHROPIC))
    assert main(["init", "--from-tools", "t.json"]) == 0
    assert main(["check", "catalog.yaml"]) == 0
    assert "refund" in capsys.readouterr().out
    assert json.load(open("tools.json"))["refund"]["url"]
    (tmp_path / "bad.json").write_text("not json")
    assert main(["init", "--from-tools", "bad.json", "--force"]) == 1
