"""
Signed approval tokens.

A person (or an approval service) holding the signing secret mints a token that says
"approver X allows tool T to run with argument A = V, in session S, until time E".
The token is a plain string, so it can travel by Slack message, email or ticket and be
presented to the gateway later. The gateway checks it without trusting whoever carried it.

What a token guarantees (HMAC-SHA256, shared secret):
  - it was made by someone who holds the secret, and nothing in it was changed
  - it expires
  - it names one tool, one argument, one exact value and (optionally) one session
  - the gateway accepts each token once (a replay is refused)
What it does not do: with the shared-secret (HMAC) form, anyone holding the secret can mint
tokens (keep it out of the agent's reach), and it cannot be narrowed after minting.

Ed25519 form (needs `pip install "mayi[signing]"`): the approver holds a private key and the
gateway holds only the public key, so a stolen gateway cannot mint approvals. Same payload,
same one-use and expiry rules. A gateway configured with a public key accepts only tokens
signed by the matching private key; one configured with a secret accepts only HMAC tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Optional

from .approvals import ApprovalToken

_VERSION = 1


class InvalidApproval(ValueError):
    """The token is malformed, forged, expired, or for a different session."""


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_approval(secret: str, tool: str, arg: str, value: Any, approved_by: str,
                  ttl_seconds: float = 300, session_id: Optional[str] = None,
                  scope: Optional[dict] = None, now: Optional[float] = None) -> str:
    if not secret:
        raise ValueError("a signing secret is required")
    payload = {"v": _VERSION, "tool": tool, "arg": arg, "value": value, "by": approved_by,
               "exp": (time.time() if now is None else now) + ttl_seconds,
               "sid": session_id, "scope": scope, "jti": secrets.token_hex(8)}
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_approval(secret: str, token: str, session_id: Optional[str] = None,
                    now: Optional[float] = None) -> tuple[ApprovalToken, str]:
    """Returns (ApprovalToken, jti). Raises InvalidApproval for anything wrong, with no detail
    about *why* a signature failed beyond 'invalid'."""
    try:
        body, sig = token.strip().split(".")
        expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise InvalidApproval("invalid approval token")
        p = json.loads(_unb64(body))
    except InvalidApproval:
        raise
    except Exception:
        raise InvalidApproval("invalid approval token") from None
    if p.get("alg", "hmac") != "hmac":
        raise InvalidApproval("invalid approval token")
    return _checked(p, session_id, now)


def _checked(p: dict, session_id: Optional[str], now: Optional[float]) -> tuple[ApprovalToken, str]:
    if p.get("v") != _VERSION or not all(k in p for k in ("tool", "arg", "value", "by", "exp", "jti")):
        raise InvalidApproval("invalid approval token")
    if (time.time() if now is None else now) > float(p["exp"]):
        raise InvalidApproval("approval token expired")
    if p.get("sid") is not None and session_id is not None and p["sid"] != session_id:
        raise InvalidApproval("approval token is for a different session")
    return (ApprovalToken(tool=p["tool"], arg=p["arg"], value=p["value"], approved_by=str(p["by"]),
                          expires_at=float(p["exp"]), scope=p.get("scope")), p["jti"])


def token_session(token: str) -> Optional[str]:
    """The session a token names (read without verifying; the caller must still verify)."""
    try:
        return json.loads(_unb64(token.strip().split(".")[0])).get("sid")
    except Exception:
        return None


# ---------------------------------------------------------------- Ed25519 (public-key) tokens
def _ed():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        raise RuntimeError('public-key approvals need the cryptography package: pip install "mayi[signing]"') from None
    return ed25519, serialization


def generate_keypair() -> tuple[str, str]:
    """(private_key_pem, public_key_pem). Keep the private key with the approver, give the gateway the public one."""
    ed25519, ser = _ed()
    k = ed25519.Ed25519PrivateKey.generate()
    priv = k.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption()).decode()
    pub = k.public_key().public_bytes(ser.Encoding.PEM, ser.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub


def sign_approval_ed25519(private_key_pem: str, tool: str, arg: str, value: Any, approved_by: str,
                          ttl_seconds: float = 300, session_id: Optional[str] = None,
                          scope: Optional[dict] = None, now: Optional[float] = None) -> str:
    ed25519, ser = _ed()
    key = ser.load_pem_private_key(private_key_pem.encode(), password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError("not an Ed25519 private key")
    payload = {"v": _VERSION, "alg": "ed25519", "tool": tool, "arg": arg, "value": value, "by": approved_by,
               "exp": (time.time() if now is None else now) + ttl_seconds,
               "sid": session_id, "scope": scope, "jti": secrets.token_hex(8)}
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return f"{body}.{_b64(key.sign(body.encode()))}"


def token_alg(token: str) -> str:
    """'ed25519' or 'hmac' as the token claims (read without verifying; the verifier decides what it accepts)."""
    try:
        return json.loads(_unb64(token.strip().split(".")[0])).get("alg", "hmac")
    except Exception:
        return "hmac"


def verify_approval_ed25519(public_key_pem: str, token: str, session_id: Optional[str] = None,
                            now: Optional[float] = None) -> tuple[ApprovalToken, str]:
    ed25519, ser = _ed()
    from cryptography.exceptions import InvalidSignature
    try:
        key = ser.load_pem_public_key(public_key_pem.encode())
        if not isinstance(key, ed25519.Ed25519PublicKey):
            raise ValueError("not an Ed25519 public key")
        body, sig = token.strip().split(".")
        key.verify(_unb64(sig), body.encode())
        p = json.loads(_unb64(body))
    except (InvalidSignature, ValueError, TypeError):
        raise InvalidApproval("invalid approval token") from None
    if p.get("alg") != "ed25519":
        raise InvalidApproval("invalid approval token")
    return _checked(p, session_id, now)
