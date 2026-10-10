import json
import os
from mayi.cli import main


def test_init_then_check(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert (tmp_path / "catalog.yaml").exists() and json.load(open("tools.json"))
    assert main(["check", "catalog.yaml"]) == 0
    assert "fail closed" in capsys.readouterr().out


def test_check_rejects_bad_catalog(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    open("bad.yaml", "w").write("refund:\n  amount:\n    pen: banana\n")
    assert main(["check", "bad.yaml"]) == 1


def test_keys_generated_once_and_private(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MAYI_AGENT_KEY", raising=False); monkeypatch.delenv("MAYI_APP_KEY", raising=False)
    from mayi.cli import _keys
    a1, p1 = _keys(); a2, p2 = _keys()
    assert (a1, p1) == (a2, p2) and a1 != p1
    assert oct(os.stat(".mayi-keys").st_mode)[-3:] == "600"
