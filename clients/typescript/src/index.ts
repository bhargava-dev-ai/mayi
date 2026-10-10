/**
 * Client for a mayi gateway. Uses only the agent key: it can ask what would
 * happen to a tool call and run calls the rules allow. It cannot create
 * sessions or approve anything.
 *
 *   const mayi = new MayiClient({ baseUrl: "http://127.0.0.1:8787", apiKey: process.env.MAYI_API_KEY! });
 *   const d = await mayi.decide("ticket-1042", "refund", { amount: 40, note: "late" });
 */
export type Decision = "allow" | "hold" | "reject";

export interface DecideResult { decision: Decision; reason?: string; [k: string]: unknown }
export interface CallResult { outcome: "executed" | "escalated" | "rejected" | "simulated"; reason?: string; [k: string]: unknown }

export interface MayiOptions {
  baseUrl?: string;
  apiKey?: string;
  timeoutMs?: number;
  fetch?: typeof fetch;
}

export class MayiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "MayiError";
  }
}

export class MayiClient {
  private baseUrl: string;
  private apiKey: string;
  private timeoutMs: number;
  private fetchImpl: typeof fetch;
  private tokens = new Map<string, string>();

  constructor(opts: MayiOptions = {}) {
    const env = typeof process !== "undefined" ? process.env : {};
    this.baseUrl = (opts.baseUrl ?? env.MAYI_BASE_URL ?? "http://127.0.0.1:8787").replace(/\/+$/, "");
    const key = opts.apiKey ?? env.MAYI_API_KEY;
    if (!key) throw new Error("set MAYI_API_KEY (the agent key) or pass apiKey");
    this.apiKey = key;
    this.timeoutMs = opts.timeoutMs ?? 30_000;
    this.fetchImpl = opts.fetch ?? fetch;
  }

  private async post<T>(path: string, body: unknown): Promise<T> {
    const res = await this.fetchImpl(this.baseUrl + path, {
      method: "POST",
      headers: { Authorization: `Bearer ${this.apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    const text = await res.text();
    let data: unknown;
    try { data = JSON.parse(text); } catch { data = { error: text }; }
    if (!res.ok) throw new MayiError(res.status, String((data as { error?: string }).error ?? res.statusText));
    return data as T;
  }

  /** Remember the token the application received when it created this session. */
  bind(sessionId: string, sessionToken: string) {
    this.tokens.set(sessionId, sessionToken);
  }

  private body(sessionId: string, tool: string, args: Record<string, unknown>, actor?: string) {
    const t = this.tokens.get(sessionId);
    return { session_id: sessionId, tool, args, ...(actor ? { actor } : {}), ...(t ? { session_token: t } : {}) };
  }

  /** Dry run. Nothing executes and no approval is used. */
  decide(sessionId: string, tool: string, args: Record<string, unknown>, actor?: string) {
    return this.post<DecideResult>("/v1/decide", this.body(sessionId, tool, args, actor));
  }

  simulate(sessionId: string, tool: string, args: Record<string, unknown>, actor?: string) {
    return this.post<CallResult>("/v1/simulate", this.body(sessionId, tool, args, actor));
  }

  /** Runs the tool through the gateway if, and only if, the rules allow it. */
  call(sessionId: string, tool: string, args: Record<string, unknown>, actor?: string) {
    return this.post<CallResult>("/v1/call", this.body(sessionId, tool, args, actor));
  }

  /** Report untrusted text the model has read, so verbatim copies of it can be held or rejected. */
  addUntrusted(sessionId: string, text: string) {
    const t = this.tokens.get(sessionId);
    return this.post<{ registered: number }>("/v1/untrusted", { session_id: sessionId, text, ...(t ? { session_token: t } : {}) });
  }
}
