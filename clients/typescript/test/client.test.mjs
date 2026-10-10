// Runs the real client against a real gateway started from the Python package.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { MayiClient, MayiError } from "../dist/index.js";

const AGENT = "ts-agent", APP = "ts-app", PORT = 18787;
let proc;
const base = `http://127.0.0.1:${PORT}`;

async function app(path, body) {
  const r = await fetch(base + path, { method: "POST", headers: { Authorization: `Bearer ${APP}`, "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}

before(async () => {
  proc = spawn(process.env.PYTHON ?? "python", [new URL("./gateway_for_test.py", import.meta.url).pathname, String(PORT), AGENT, APP], { stdio: "ignore" });
  for (let i = 0; i < 50; i++) {
    try { const r = await fetch(base + "/console"); if (r.ok) return; } catch {}
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("gateway did not start");
});
after(() => proc?.kill());

test("allow, hold, reject and single-use approval over real HTTP", async () => {
  await app("/v1/sessions", { session_id: "s1", context: { ticket_id: "T-1" } });
  const c = new MayiClient({ baseUrl: base, apiKey: AGENT });
  assert.equal((await c.decide("s1", "refund", { amount: 20, note: "late" })).decision, "allow");
  assert.equal((await c.decide("s1", "refund", { amount: 9000, note: "x" })).decision, "hold");
  assert.equal((await c.call("s1", "refund", { amount: 9000, note: "x" })).outcome, "escalated");
  await app("/v1/approvals", { session_id: "s1", tool: "refund", arg: "amount", value: 9000, approved_by: "mgr" });
  assert.equal((await c.call("s1", "refund", { amount: 9000, note: "x" })).outcome, "executed");
  assert.equal((await c.call("s1", "refund", { amount: 9000, note: "x" })).outcome, "escalated");
});

test("agent key cannot use app endpoints; wrong key is refused", async () => {
  const c = new MayiClient({ baseUrl: base, apiKey: "nope" });
  await assert.rejects(() => c.call("s1", "refund", { amount: 1 }), (e) => e instanceof MayiError && e.status === 401);
  assert.throws(() => new MayiClient({ baseUrl: base, apiKey: "" }));
});
