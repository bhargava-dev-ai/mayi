from mayi import Catalog
from mayi.cli import main
from mayi.explain import explain_catalog, render_text

CAT = {"refund": {"ticket_id": {"pen": "application", "source": "ticket_id"},
                  "amount": {"pen": "human", "limit": "amount <= 500"},
                  "note": {"pen": "model", "type": "str", "max_len": 80}},
       "send_reply": {"to": {"pen": "application", "source": "email"}, "body": {"pen": "model", "type": "str"}},
       "wild": {"x": {"pen": "model", "type": "str", "max_len": 5}}}


def test_reports_who_controls_what_and_flags_real_risks():
    r = explain_catalog(Catalog.from_dict(CAT))
    assert r["tools"]["refund"]["application_fixed"] == {"ticket_id": "ticket_id"}
    assert r["tools"]["refund"]["standing_approvals"]["amount"]["limit"] == "amount <= 500"
    w = " | ".join(r["warnings"])
    assert "no session_budget" in w                    # split-the-amount risk
    assert "send_reply.body" in w and "no max_len" in w  # unbounded free text
    assert "sends something outward" in w                # outbound free text
    assert "wild: every argument is model-writable" in w


def test_a_budget_gives_a_stated_worst_case_and_removes_the_warning():
    cat = {"refund": {"amount": {"pen": "human", "limit": "amount <= 500", "session_budget": 1000}}}
    r = explain_catalog(Catalog.from_dict(cat))
    assert r["warnings"] == [] and "at most 1000 in total per session" in r["tools"]["refund"]["worst_case_without_a_human"][0]
    assert "No warnings." in render_text(r)


def test_cli_explain(tmp_path, monkeypatch, capsys):
    import yaml
    monkeypatch.chdir(tmp_path)
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(CAT))
    assert main(["explain", "c.yaml"]) == 0
    out = capsys.readouterr().out
    assert "your app fixes   ticket_id" in out and "Check these:" in out
    assert main(["explain", "c.yaml", "--json"]) == 0
    assert main(["explain", "missing.yaml"]) == 1
