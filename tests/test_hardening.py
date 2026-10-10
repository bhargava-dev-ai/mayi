import datetime
import hashlib
import hmac
import json
import ssl
import threading
import urllib.error
import urllib.request

import pytest

from mayi import Catalog, ToolBoundary
from mayi.gateway import Gateway, serve

CAT = {"t": {"x": {"pen": "model", "type": "str", "max_len": 9}}}


def start(**kw):
    tls = kw.pop("tls", None)
    gw = Gateway(ToolBoundary(Catalog.from_dict(CAT), {"t": lambda **k: "ok"}), kw.pop("ak", "AG"), kw.pop("pk", "AP"), **kw)
    srv = serve(gw, port=0, tls_cert=tls and tls[0], tls_key=tls and tls[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return gw, srv, ("https" if tls else "http") + f"://127.0.0.1:{srv.server_port}"


def get(url, key=None, body=None, ctx=None):
    r = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST",
                               headers={"Authorization": f"Bearer {key}"} if key else {})
    try:
        with urllib.request.urlopen(r, context=ctx) as resp:
            return resp.status, resp.read().decode(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers


def test_key_rotation_accepts_old_and_new_then_only_new():
    gw, srv, base = start(ak="new-agent,old-agent", pk="app-1")
    gw.create_session({"session_id": "s", "context": {}})
    body = {"session_id": "s", "tool": "t", "args": {"x": "a"}}
    assert get(base + "/v1/call", "new-agent", body)[0] == 200
    assert get(base + "/v1/call", "old-agent", body)[0] == 200
    assert get(base + "/v1/call", "nope", body)[0] == 401
    gw2, srv2, base2 = start(ak="new-agent", pk="app-1")
    gw2.create_session({"session_id": "s", "context": {}})
    assert get(base2 + "/v1/call", "old-agent", body)[0] == 401
    with pytest.raises(ValueError):
        start(ak="same", pk="x,same")
    srv.shutdown(); srv2.shutdown()


def test_request_flood_gets_429_and_wrong_keys_lock_the_client_out():
    gw, srv, base = start(requests_per_minute=5)
    codes = [get(base + "/v1/pending", "AP")[0] for _ in range(8)]
    assert codes[:5] == [200] * 5 and set(codes[5:]) == {429}
    srv.shutdown()
    gw, srv, base = start(failed_auth_per_minute=3)
    assert [get(base + "/v1/pending", "bad")[0] for _ in range(3)] == [401] * 3
    assert get(base + "/v1/pending", "AP")[0] == 429          # even the right key waits out the lockout
    srv.shutdown()


def test_metrics_health_headers_and_signed_audit_export():
    gw, srv, base = start(audit_secret="audit-secret")
    gw.create_session({"session_id": "s", "context": {}})
    gw.call({"session_id": "s", "tool": "t", "args": {"x": "a"}}, False)
    gw.call({"session_id": "s", "tool": "t", "args": {"x": "way too long for this"}}, False)
    assert get(base + "/healthz")[0] == 200                      # open, reveals nothing
    assert get(base + "/metrics")[0] == 401 and get(base + "/metrics", "AG")[0] == 403
    code, text, _ = get(base + "/metrics", "AP")
    assert code == 200 and 'mayi_calls_total{tool="t",outcome="executed"} 1' in text and 'outcome="rejected"} 1' in text
    code, text, _ = get(base + "/v1/audit/export", "AP")
    exp = json.loads(text)
    body = json.dumps(exp["records"], sort_keys=True, separators=(",", ":")).encode()
    assert exp["count"] == 2 and exp["digest"] == hashlib.sha256(body).hexdigest()
    assert exp["signature"] == hmac.new(b"audit-secret", body, hashlib.sha256).hexdigest()
    _, _, h = get(base + "/console")
    assert h["X-Frame-Options"] == "DENY" and "default-src 'self'" in h["Content-Security-Policy"]
    srv.shutdown()


def test_tls(tmp_path):
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    c, k = tmp_path / "c.pem", tmp_path / "k.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    gw, srv, base = start(tls=(str(c), str(k)))
    ctx = ssl.create_default_context(cafile=str(c))
    assert get(base + "/healthz", ctx=ctx)[0] == 200
    with pytest.raises(Exception):
        get(base.replace("https", "http") + "/healthz")            # plain HTTP to a TLS port does not work
    with pytest.raises(ValueError):
        serve(gw, port=0, tls_cert=str(c))                          # a cert without a key is refused
    srv.shutdown()
