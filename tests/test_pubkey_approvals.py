import pytest

pytest.importorskip("cryptography")

from mayi import Catalog, ToolBoundary
from mayi.cli import main
from mayi.gateway import Gateway
from mayi.signing import (InvalidApproval, generate_keypair, sign_approval, sign_approval_ed25519,
                          verify_approval, verify_approval_ed25519)

CAT = {"refund": {"amount": {"pen": "human"}}}


def gw(**kw):
    ran = []
    g = Gateway(ToolBoundary(Catalog.from_dict(CAT), {"refund": lambda **k: ran.append(k) or "ok"}), "AG", "AP", **kw)
    g.create_session({"session_id": "s", "context": {}})
    return g, ran


def run(g, v):
    return g.call({"session_id": "s", "tool": "refund", "args": {"amount": v}}, False)["outcome"]


def test_round_trip_and_gateway_holds_only_the_public_key():
    priv, pub = generate_keypair()
    g, ran = gw(signing_public_key=pub)
    assert run(g, 40) == "escalated"
    tok = sign_approval_ed25519(priv, "refund", "amount", 40, "alice", session_id="s")
    assert g.redeem_approval({"token": tok})["approved"] is True
    assert run(g, 40) == "executed" and run(g, 40) == "escalated"
    with pytest.raises(ValueError, match="already used"):
        g.redeem_approval({"token": tok})


def test_forgeries_and_wrong_keys_fail():
    priv, pub = generate_keypair()
    other_priv, _ = generate_keypair()
    g, _ = gw(signing_public_key=pub)
    bad = sign_approval_ed25519(other_priv, "refund", "amount", 40, "mallory", session_id="s")
    with pytest.raises(ValueError):
        g.redeem_approval({"token": bad})
    good = sign_approval_ed25519(priv, "refund", "amount", 40, "alice", session_id="s")
    body, sig = good.split(".")
    import base64, json
    p = json.loads(base64.urlsafe_b64decode(body + "=="))
    p["value"] = 99999
    forged = base64.urlsafe_b64encode(json.dumps(p, sort_keys=True, separators=(",", ":")).encode()).rstrip(b"=").decode() + "." + sig
    with pytest.raises(ValueError):
        g.redeem_approval({"token": forged})
    with pytest.raises(InvalidApproval):
        verify_approval_ed25519(pub, sign_approval_ed25519(priv, "t", "a", 1, "x", now=0))  # expired long ago


def test_no_algorithm_confusion_between_secret_and_public_key():
    priv, pub = generate_keypair()
    hm = sign_approval("s" * 32, "refund", "amount", 40, "eve", session_id="s")
    g, _ = gw(signing_public_key=pub)                         # public key only: an HMAC token is not accepted
    with pytest.raises(ValueError):
        g.redeem_approval({"token": hm})
    g2, _ = gw(signing_secret="s" * 32)                       # secret only: a public-key token is not accepted
    with pytest.raises(ValueError):
        g2.redeem_approval({"token": sign_approval_ed25519(priv, "refund", "amount", 40, "a", session_id="s")})
    with pytest.raises(InvalidApproval):                      # and the HMAC verifier refuses a token that claims ed25519
        verify_approval("s" * 32, sign_approval_ed25519(priv, "refund", "amount", 40, "a"))
    g3, _ = gw(signing_secret="s" * 32, signing_public_key=pub)   # both configured: each form works, only with its own key
    assert g3.redeem_approval({"token": hm})["approved"] is True


def test_cli_keygen_and_sign(tmp_path, capsys, monkeypatch):
    key = str(tmp_path / "k")
    assert main(["keygen", "--out", key]) == 0
    assert main(["keygen", "--out", key]) == 1                 # never overwrite a key
    import os, stat
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    capsys.readouterr()
    assert main(["sign", "refund", "amount", "40", "--by", "alice", "--key", key, "--session", "s"]) == 0
    tok = capsys.readouterr().out.strip()
    g, _ = gw(signing_public_key=open(key + ".pub").read())
    assert g.redeem_approval({"token": tok})["approved"] is True
