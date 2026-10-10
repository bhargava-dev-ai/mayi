"""
mayi gateway: run the tool boundary as a small standalone HTTP service.

Two separate credentials, two separate roles:

  agent key  (untrusted)  POST /v1/call, /v1/simulate
                          The agent names a session and proposes tool
                          arguments. It cannot create sessions, cannot
                          issue approvals, cannot read the audit log.
  app key    (trusted)    POST /v1/sessions, /v1/approvals
                          GET  /v1/audit, /v1/report, /v1/catalog
                          The application supplies session context (what
                          application-pen arguments resolve from) and the
                          human approvals.

Real tools live behind upstream URLs. The gateway is the only caller; the
agent never receives the upstream address or credentials.

stdlib only. This is a reference gateway: bind to localhost or put it
behind your own TLS and network controls. It is not a hardened production
server and has had no outside security review.
"""

from __future__ import annotations

import argparse
import hashlib
import secrets
import hmac
import json
import threading
import time
import uuid
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from .approvals import ApprovalSet, ApprovalToken
from .boundary import ToolBoundary
from .signing import InvalidApproval, sign_approval, token_alg, token_session, verify_approval, verify_approval_ed25519
from .catalog import Catalog
from .results import Escalated, Executed, Rejected

MAX_BODY = 1_000_000


def http_tool(url: str, headers: Optional[dict] = None, timeout: float = 15.0) -> Callable[..., Any]:
    """Build a tool callable that POSTs the final arguments as JSON to `url`."""
    def call(**kwargs: Any) -> Any:
        req = urllib.request.Request(
            url, data=json.dumps(kwargs).encode(), method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 (operator-configured URL)
            return json.loads(r.read() or b"null")
    return call


class Gateway:
    def __init__(self, boundary: ToolBoundary, agent_key: str, app_key: str,
                 signing_secret: Optional[str] = None, notifier: Optional[Callable[[dict], None]] = None,
                 store: Optional[Any] = None, pending_ttl: Optional[float] = None,
                 signing_public_key: Optional[str] = None, audit_secret: Optional[str] = None,
                 requests_per_minute: int = 600, failed_auth_per_minute: int = 20,
                 require_session_token: bool = False):
        if not agent_key or not app_key or agent_key == app_key:
            raise ValueError("agent_key and app_key must both be set and different")
        self.boundary = boundary
        self._agent_keys = [k.strip() for k in agent_key.split(",") if k.strip()]  # "new,old" while rotating
        self._app_keys = [k.strip() for k in app_key.split(",") if k.strip()]
        if set(self._agent_keys) & set(self._app_keys):
            raise ValueError("agent_key and app_key must both be set and different")
        self._sessions: dict[str, dict] = {}
        self._approvals: dict[str, ApprovalSet] = {}
        self._untrusted: dict[str, list] = {}
        self._events: list[dict] = []
        self._pending: dict[tuple, dict] = {}
        self._signing_secret = signing_secret
        self._public_key = signing_public_key
        self._audit_secret = audit_secret
        self._require_token = require_session_token
        self._token_hash: dict[str, str] = {}  # session id -> sha256 of its token (the token itself is never kept)
        self.limiter = RateLimiter(requests_per_minute, failed_auth_per_minute)
        self._notifier = notifier
        self.alert_errors: list[str] = []  # last alert failures, readable by the operator
        self._used_jti: set[str] = set()
        self._partial: dict[tuple, set] = {}
        self._lock = threading.Lock()
        self._store = store
        self._pending_ttl = pending_ttl
        if store is not None:
            self._load_state()
        inner = boundary.audit_log

        def tee(rec: dict) -> None:
            self._events.append(rec)
            if inner:
                inner(rec)
        boundary.audit_log = tee

    # -- durable state -------------------------------------------------
    def _new_set(self, sid: str) -> ApprovalSet:
        def used(tok: ApprovalToken) -> None:
            if self._store is not None and tok.store_id is not None:
                self._store.mark_used(tok.store_id)
        return ApprovalSet(on_use=used)

    @staticmethod
    def _pkey(key: tuple) -> str:
        return json.dumps(list(key), default=str)

    def _load_state(self) -> None:
        st = self._store
        self._sessions.update(st.load_sessions())
        self._token_hash.update(st.load_session_token_hashes())
        for sid in self._sessions:
            self._approvals[sid] = self._new_set(sid)
        self._untrusted.update(st.load_untrusted())
        now = time.time()
        for (aid, sid, tool, arg, value, by, exp, scope) in st.load_approvals():
            if sid in self._approvals and (exp is None or exp > now):
                self._approvals[sid].add(ApprovalToken(tool=tool, arg=arg, value=json.loads(value), approved_by=by,
                                                       expires_at=exp, scope=json.loads(scope) if scope else None, store_id=aid))
        for k, body in st.load_pending().items():
            self._pending[tuple(json.loads(k))] = body

    def _expire_pending(self) -> None:
        if not self._pending_ttl:
            return
        cutoff = time.time() - self._pending_ttl
        for k in [k for k, v in self._pending.items() if v["ts"] < cutoff]:
            self._pending.pop(k)
            if self._store is not None:
                self._store.delete_pending(self._pkey(k))

    # -- auth ---------------------------------------------------------
    def role(self, token: str) -> Optional[str]:
        role = None
        for k in self._app_keys:       # compare against every key, no early exit
            if hmac.compare_digest(token.encode(), k.encode()):
                role = "app"
        for k in self._agent_keys:
            if hmac.compare_digest(token.encode(), k.encode()) and role is None:
                role = "agent"
        return role

    # -- operations ---------------------------------------------------
    def create_session(self, body: dict) -> dict:
        sid, ctx = body.get("session_id"), body.get("context")
        if not isinstance(sid, str) or not sid or not isinstance(ctx, dict):
            raise ValueError("need session_id (string) and context (object)")
        with self._lock:
            self._sessions[sid] = ctx
            self._approvals.setdefault(sid, self._new_set(sid))
            self._untrusted.setdefault(sid, [])
            if self._store is not None:
                self._store.save_session(sid, ctx)
            out = {"session_id": sid}
            if body.get("bind") or self._require_token:
                tok = secrets.token_urlsafe(24)
                h = hashlib.sha256(tok.encode()).hexdigest()
                self._token_hash[sid] = h
                if self._store is not None:
                    self._store.save_session_token_hash(sid, h)
                out["session_token"] = tok  # shown once; hand it to the agent runtime for this session only
            else:
                self._token_hash.pop(sid, None)
        return out

    def _session_ok(self, body: dict) -> bool:
        """An agent may use a session only if it holds that session's token (when the session has one).
        The app key is trusted to act on any session."""
        if body.get("_role") == "app":
            return True
        h = self._token_hash.get(body.get("session_id"))
        if h is None:
            return not self._require_token
        tok = body.get("session_token")
        return isinstance(tok, str) and hmac.compare_digest(hashlib.sha256(tok.encode()).hexdigest(), h)

    def add_untrusted(self, body: dict) -> dict:
        """Register text the model has read from an untrusted source. It can only tighten: a value
        copied from it is held or rejected where the catalog says so."""
        sid, text = body.get("session_id"), body.get("text")
        if sid not in self._sessions or not isinstance(text, str) or not self._session_ok(body):
            raise ValueError("need a known session_id and text (string)")
        with self._lock:
            self._untrusted.setdefault(sid, []).append(text[:200_000])
            del self._untrusted[sid][:-50]
            if self._store is not None:
                self._store.add_untrusted(sid, text[:200_000])
        return {"registered": len(self._untrusted[sid])}

    def add_approval(self, body: dict) -> dict:
        sid = body.get("session_id")
        if sid not in self._sessions:
            raise ValueError("unknown session")
        for f in ("tool", "arg", "value", "approved_by"):
            if f not in body:
                raise ValueError(f"missing {f}")
        ttl = body.get("ttl_seconds", 300)
        tok = ApprovalToken(
            tool=body["tool"], arg=body["arg"], value=body["value"],
            approved_by=str(body["approved_by"]),
            expires_at=time.time() + float(ttl) if ttl is not None else None,
            scope=body.get("scope"),
        )
        with self._lock:
            return self._grant_checked(sid, tok)

    def _grant_checked(self, sid: str, tok: ApprovalToken) -> dict:
        """Grant the approval, unless the catalog wants two different people for this value and only one has signed."""
        spec = self.boundary.catalog.tools.get(tok.tool)
        spec = spec.args.get(tok.arg) if spec else None
        v, t = tok.value, (spec.dual_control_above if spec else None)
        if t is not None and isinstance(v, (int, float)) and not isinstance(v, bool) and v > t:
            if self._store is not None:
                who = set(self._store.add_partial(sid, tok.tool, tok.arg, v, tok.approved_by))
            else:
                who = self._partial.setdefault((sid, tok.tool, tok.arg, json.dumps(v)), set())
                who.add(tok.approved_by)
            if len(who) < 2:
                return {"approved": False, "waiting_for": "a second, different approver", "approvers": sorted(who)}
            if self._store is not None:
                self._store.clear_partial(sid, tok.tool, tok.arg, v)
            else:
                self._partial.pop((sid, tok.tool, tok.arg, json.dumps(v)), None)
            tok.approvers, tok.approved_by = tuple(sorted(who)), " + ".join(sorted(who))
        self._grant(sid, tok)
        return {"approved": True, "approved_by": tok.approved_by, "expires_at": tok.expires_at}

    def _grant(self, sid: str, tok: ApprovalToken) -> None:
        if self._store is not None:
            tok.store_id = self._store.add_approval(sid, tok)
        self._approvals[sid].add(tok)
        for k in [k for k in self._pending if k[:3] == (sid, tok.tool, tok.arg)
                  and k[3] == json.dumps(tok.value, sort_keys=True, default=str)]:
            self._pending.pop(k)
            if self._store is not None:
                self._store.delete_pending(self._pkey(k))

    def redeem_approval(self, body: dict) -> dict:
        """Accept a signed approval token minted elsewhere. Needs the app key (the carrier of a
        token is not trusted; the signature is what is trusted)."""
        if not (self._signing_secret or self._public_key):
            raise ValueError("signed approvals are not enabled (set MAYI_SIGNING_SECRET or MAYI_APPROVER_PUBKEY)")
        tok = body.get("token")
        if not isinstance(tok, str):
            raise ValueError("missing token")
        sid = body.get("session_id") or token_session(tok)
        if sid not in self._sessions:
            raise ValueError("unknown session")
        try:
            if token_alg(tok) == "ed25519":
                if not self._public_key:
                    raise InvalidApproval("invalid approval token")
                approval, jti = verify_approval_ed25519(self._public_key, tok, session_id=sid)
            else:
                if not self._signing_secret:
                    raise InvalidApproval("invalid approval token")
                approval, jti = verify_approval(self._signing_secret, tok, session_id=sid)
        except InvalidApproval as e:
            raise ValueError(str(e)) from None
        with self._lock:
            if self._store is not None:
                if not self._store.claim_jti(jti):
                    raise ValueError("approval token already used")
            elif jti in self._used_jti:
                raise ValueError("approval token already used")
            self._used_jti.add(jti)
            return self._grant_checked(sid, approval)

    def call(self, body: dict, dry_run: bool) -> dict:
        sid, tool, args = body.get("session_id"), body.get("tool"), body.get("args", {})
        if sid not in self._sessions or not self._session_ok(body):
            return {"outcome": "rejected", "reason": "unknown session"}
        if not isinstance(tool, str) or not isinstance(args, dict):
            return {"outcome": "rejected", "reason": "malformed request"}
        with self._lock:  # serialise per gateway so single-use approvals cannot race
            self._expire_pending()
            res = self.boundary.call(
                tool, args, self._sessions[sid], approvals=self._approvals[sid],
                actor=body.get("actor") or "agent", dry_run=dry_run, budget_key=sid,
                untrusted=self._untrusted.get(sid),
            )
        if isinstance(res, Escalated) and not dry_run:
            key = (sid, res.tool, res.arg, json.dumps(res.proposed_value, sort_keys=True, default=str))
            with self._lock:
                is_new = key not in self._pending
                if is_new:
                    self._pending[key] = {
                        "id": uuid.uuid4().hex[:10] + "-" + sid, "session_id": sid, "tool": res.tool,
                        "arg": res.arg, "value": res.proposed_value, "args": args, "ts": time.time()}
                    if self._store is not None:
                        self._store.save_pending(self._pkey(key), self._pending[key])
            if is_new and self._notifier:
                try:
                    self._notifier({"session_id": sid, "tool": res.tool, "arg": res.arg,
                                    "value": res.proposed_value, "reason": res.reason})
                except Exception as e:  # an alert must never break a tool call; keep it visible instead
                    self.alert_errors.append(f"{type(e).__name__}: {e}")
                    del self.alert_errors[:-20]
        return serialise(res)

    def decide(self, body: dict) -> dict:
        """One plain answer, no side effects: allow / hold / reject, with the reason."""
        r = self.call(body, True)
        out = {"simulated": "allow", "escalated": "hold"}.get(r.get("outcome"), "reject")
        d = {"decision": out}
        if out == "hold":
            d["reason"] = f"needs human approval for {r.get('arg')}"
            d["arg"] = r.get("arg")
        elif out == "reject":
            d["reason"] = r.get("reason", "rejected")
        if out == "allow":
            d["args"] = r.get("args")
        return d

    def pending(self) -> list[dict]:
        with self._lock:
            self._expire_pending()
            return sorted(self._pending.values(), key=lambda p: p["ts"])

    def deny(self, body: dict) -> dict:
        with self._lock:
            keys = [k for k, v in self._pending.items() if v["id"] == body.get("id")]
            for k in keys:
                self._pending.pop(k)
                if self._store is not None:
                    self._store.delete_pending(self._pkey(k))
        return {"denied": len(keys)}

    def audit(self) -> list[dict]:
        return list(self._events)

    def audit_export(self) -> dict:
        """All audit records this process holds, with a digest and (if MAYI_AUDIT_SECRET is set) an HMAC over it."""
        recs = json.loads(json.dumps(self._events, default=str))
        body = json.dumps(recs, sort_keys=True, separators=(",", ":")).encode()
        out = {"count": len(recs), "records": recs, "digest": hashlib.sha256(body).hexdigest(), "signature": None}
        if self._audit_secret:
            out["signature"] = hmac.new(self._audit_secret.encode(), body, hashlib.sha256).hexdigest()
        return out

    def metrics(self) -> str:
        counts: dict[tuple, int] = {}
        for e in self._events:
            k = (str(e.get("tool")), str(e.get("outcome")).lower())
            counts[k] = counts.get(k, 0) + 1
        lines = ["# HELP mayi_calls_total Tool calls by tool and outcome.", "# TYPE mayi_calls_total counter"]
        for (tool, outcome), n in sorted(counts.items()):
            lines.append(f'mayi_calls_total{{tool="{_prom(tool)}",outcome="{_prom(outcome)}"}} {n}')
        lines += ["# TYPE mayi_pending_approvals gauge", f"mayi_pending_approvals {len(self._pending)}",
                  "# TYPE mayi_alert_errors gauge", f"mayi_alert_errors {len(self.alert_errors)}"]
        return "\n".join(lines) + "\n"


def _prom(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class RateLimiter:
    """Per-client sliding windows: overall requests, and failed sign-ins (which lock the client out briefly)."""

    def __init__(self, per_minute: int, failed_per_minute: int):
        self.per_minute, self.failed_per_minute = per_minute, failed_per_minute
        self._hits: dict[str, list] = {}
        self._fails: dict[str, list] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _trim(lst: list, now: float) -> None:
        while lst and now - lst[0] > 60:
            lst.pop(0)

    def allow(self, client: str) -> bool:
        now = time.monotonic()
        with self._lock:
            fails = self._fails.setdefault(client, [])
            self._trim(fails, now)
            if len(fails) >= self.failed_per_minute:
                return False
            hits = self._hits.setdefault(client, [])
            self._trim(hits, now)
            if len(hits) >= self.per_minute:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:  # bound memory under a spray of addresses
                self._hits.clear(); self._fails.clear()
            return True

    def failed(self, client: str) -> None:
        with self._lock:
            self._fails.setdefault(client, []).append(time.monotonic())


def serialise(res: Any) -> dict:
    if isinstance(res, Executed):
        return {"outcome": "simulated" if res.simulated else "executed",
                "tool": res.tool, "executed_as": res.executed_as,
                "args": res.args, "result": res.result}
    if isinstance(res, Escalated):
        return {"outcome": "escalated", "tool": res.tool, "arg": res.arg,
                "proposed_value": res.proposed_value, "reason": res.reason}
    if isinstance(res, Rejected):
        return {"outcome": "rejected", "tool": res.tool, "reason": res.reason, "detail": res.detail}
    return {"outcome": "rejected", "reason": "internal"}


CONSOLE = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>mayi console</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#ddd;--ok:#1a7f37;--bad:#b42318;--warn:#9a6700}
@media(prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--mut:#999;--line:#333;--ok:#4ac26b;--bad:#ff7b72;--warn:#d29922}}
body{font:14px system-ui;background:var(--bg);color:var(--fg);margin:0;padding:16px;max-width:980px;margin:auto}
h1{font-size:18px}h2{font-size:15px;margin-top:24px}
table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid var(--line);padding:6px;text-align:left;vertical-align:top;font-size:13px}
button,input{font:inherit;padding:5px 10px;border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:4px}
code{font-size:12px;word-break:break-all}.mut{color:var(--mut)}
.executed{color:var(--ok)}.rejected{color:var(--bad)}.escalated{color:var(--warn)}
</style>
<h1>mayi console</h1>
<p class=mut>Trusted side. Enter the app key (kept in this tab only).</p>
<input id=k type=password placeholder="app key"> <input id=who placeholder="your name (approver)"> <button onclick="go()">Connect</button>
<span id=err class=rejected></span>
<h2>Waiting for a human</h2><div id=pend class=mut>Not connected.</div>
<h2>Signed approval</h2><input id=tok placeholder="paste an approval token" size=60> <button onclick="redeem()">Redeem</button> <span id=rmsg class=mut></span>
<h2>Audit log</h2><div id=aud></div>
<h2>Catalog (who writes each argument)</h2><div id=cat></div>
<script>
let K="",timer;const $=i=>document.getElementById(i);
const esc=s=>String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
async function api(p,b){const r=await fetch(p,{method:b?"POST":"GET",headers:{Authorization:"Bearer "+K,"Content-Type":"application/json"},body:b?JSON.stringify(b):undefined});
 if(!r.ok)throw new Error((await r.json()).error||r.status);return r.json()}
async function go(){K=$("k").value;$("err").textContent="";try{await refresh();clearInterval(timer);timer=setInterval(refresh,2000)}catch(e){$("err").textContent=" "+e.message}}
async function redeem(){try{const r=await api("/v1/approvals/redeem",{token:$("tok").value.trim()});$("rmsg").textContent=" approved by "+r.approved_by;$("tok").value="";refresh()}catch(e){$("rmsg").textContent=" "+e.message}}
async function approve(i,yes){const p=window.P[i];
 if(!yes){await api("/v1/deny",{id:p.id})}
 else{const sc=null;
  await api("/v1/approvals",{session_id:p.session_id,tool:p.tool,arg:p.arg,value:p.value,approved_by:$("who").value||"console",ttl_seconds:300,scope:sc})}
 refresh()}
async function refresh(){
 const [p,a,c]=await Promise.all([api("/v1/pending"),api("/v1/audit"),api("/v1/catalog")]);window.P=p;
 $("pend").innerHTML=p.length?"<table>"+p.map((x,i)=>`<tr><td><b>${esc(x.tool)}</b>.${esc(x.arg)} = <code>${esc(JSON.stringify(x.value))}</code><br><span class=mut>session ${esc(x.session_id)} · args <code>${esc(JSON.stringify(x.args))}</code></span></td>
 <td><button onclick="approve(${i},true)">Approve once</button> <button onclick="approve(${i},false)">Deny</button></td></tr>`).join("")+"</table>":"Nothing waiting.";
 $("aud").innerHTML="<table><tr><th>tool</th><th>outcome</th><th>proposed</th><th>detail</th></tr>"+a.slice(-40).reverse().map(e=>{const o=String(e.outcome).toLowerCase();
  return `<tr><td>${esc(e.tool)}</td><td class=${o}>${esc(o)}</td><td><code>${esc(JSON.stringify(e.model_proposed))}</code></td><td><code>${esc(e.reason||e.escalated_arg||"")}</code></td></tr>`}).join("")+"</table>";
 $("cat").innerHTML="<table>"+Object.entries(c.tools).map(([t,as])=>`<tr><td><b>${esc(t)}</b></td><td>${Object.entries(as).map(([n,pen])=>`${esc(n)}: <i>${esc(pen)}</i>`).join(" · ")}</td></tr>`).join("")+"</table>"}
</script>""".replace("window.P[i];\n if(!yes)","window.P[i];\n if(!yes)")


def make_handler(gw: Gateway):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep stdout quiet; the audit log is the record
            pass

        def _send(self, code: int, obj: Any) -> None:
            data = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _role(self) -> Optional[str]:
            h = self.headers.get("Authorization", "")
            return gw.role(h[7:]) if h.startswith("Bearer ") else None

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("body too large")
            b = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(b, dict):
                raise ValueError("body must be an object")
            return b

        def _gate(self) -> Optional[str]:
            """Rate limit, then authenticate. Returns the role, or None after sending the refusal."""
            client = self.client_address[0]
            if not gw.limiter.allow(client):
                self._send(429, {"error": "too many requests"})
                return None
            role = self._role()
            if role is None:
                gw.limiter.failed(client)
                self._send(401, {"error": "unauthorized"})
            return role

        def do_POST(self):
            role = self._gate()
            if role is None:
                return
            routes = {  # path -> (required role, handler)
                "/v1/call": ("agent", lambda b: gw.call(b, False)),
                "/v1/simulate": ("agent", lambda b: gw.call(b, True)),
                "/v1/decide": ("agent", gw.decide),
                "/v1/sessions": ("app", gw.create_session),
                "/v1/untrusted": (("agent", "app"), gw.add_untrusted),
                "/v1/approvals": ("app", gw.add_approval),
                "/v1/deny": ("app", gw.deny),
                "/v1/approvals/redeem": ("app", gw.redeem_approval),
            }
            if self.path not in routes:
                return self._send(404, {"error": "not found"})
            need, fn = routes[self.path]
            if role not in ((need,) if isinstance(need, str) else need):  # fail closed: a key only works for its own role
                return self._send(403, {"error": f"{self.path} requires the {need if isinstance(need, str) else 'agent or app'} key"})
            try:
                b = self._body()
                b["_role"] = role  # set by the server after parsing, so a caller cannot claim a role in the body
                self._send(200, fn(b))
            except (ValueError, json.JSONDecodeError) as e:
                self._send(400, {"error": str(e)})
            except Exception:
                self._send(500, {"error": "internal error"})

        def do_GET(self):
            if self.path in ("/", "/console"):
                data = CONSOLE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
                self.end_headers()
                return self.wfile.write(data)
            if self.path == "/healthz":
                return self._send(200, {"ok": True})
            role = self._gate()
            if role is None:
                return
            if role != "app":
                return self._send(403, {"error": "requires the app key"})
            if self.path == "/metrics":
                data = gw.metrics().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            if self.path == "/v1/audit/export":
                return self._send(200, gw.audit_export())
            if self.path == "/v1/audit":
                return self._send(200, gw.audit())
            if self.path == "/v1/health":
                return self._send(200, {"ok": True, "alerts_enabled": gw._notifier is not None,
                                        "signed_approvals_enabled": bool(gw._signing_secret or gw._public_key),
                                        "public_key_approvals": bool(gw._public_key), "state_persisted": gw._store is not None,
                                        "recent_alert_errors": gw.alert_errors})
            if self.path == "/v1/pending":
                return self._send(200, gw.pending())
            if self.path == "/v1/catalog":
                c = gw.boundary.catalog
                return self._send(200, {"tools": {t: {a: s.pen.value for a, s in sp.args.items()}
                                                  for t, sp in c.tools.items()}})
            self._send(404, {"error": "not found"})
    return H


def serve(gw: Gateway, host: str = "127.0.0.1", port: int = 8787,
          tls_cert: Optional[str] = None, tls_key: Optional[str] = None) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(gw))
    if tls_cert or tls_key:
        import ssl
        if not (tls_cert and tls_key):
            raise ValueError("TLS needs both a certificate and a key")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(tls_cert, tls_key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    return srv


def main() -> None:
    import os
    ap = argparse.ArgumentParser(prog="mayi-gateway")
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--tools", required=True, help='JSON: {"tool": {"url": "...", "headers": {...}}}')
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--audit-db", default="mayi_audit.db")
    a = ap.parse_args()
    from .audit import SqliteAuditLog
    tools = {n: http_tool(c["url"], c.get("headers")) for n, c in json.load(open(a.tools)).items()}
    b = ToolBoundary(Catalog.from_yaml(a.catalog), tools, audit_log=SqliteAuditLog(a.audit_db))
    gw = Gateway(b, os.environ["MAYI_AGENT_KEY"], os.environ["MAYI_APP_KEY"])
    s = serve(gw, a.host, a.port)
    print(f"mayi gateway on http://{a.host}:{a.port}")
    s.serve_forever()


if __name__ == "__main__":
    main()
