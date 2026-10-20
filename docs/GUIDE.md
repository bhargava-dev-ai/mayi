# mayi guide

Full reference for mayi. The short version is in the [README](../README.md).

## Install

    python3 -m venv .venv && source .venv/bin/activate
    pip install .

## Try it (2 windows)

    mayi demo                         # window 1: leave open. Console: http://127.0.0.1:8787/console  (key: demo-app-key)

    export ANTHROPIC_API_KEY=your-key # window 2
    pip install anthropic
    mayi try-model

A real Claude model gets a ticket with a hidden attack. Its $40 refund waits in the console until you click **Approve once**. The attack never reaches the tool.

## Use it with your own tools

    mayi init                         # writes catalog.yaml and tools.json
    # edit catalog.yaml (who controls each argument) and tools.json (your tool URLs)
    mayi check catalog.yaml           # validate
    mayi serve                        # prints an agent key and an app key

Your app creates a session (`POST /v1/sessions`, app key) holding the values the application controls. The agent calls `POST /v1/call` (agent key) with a `session_id`, a tool name and proposed args. Held calls show up in the console for a person to approve. Keys are saved in `.mayi-keys`.

## Check the code

    pip install pytest hypothesis && python3 -m pytest -q

## Limits

Reference implementation, no outside security review. mayi checks the type and length of free text the model writes; it does not judge whether the text is true. Bind it to localhost or put it behind your own TLS and network controls.

## Two settings from an agent

    export MAYI_BASE_URL=http://127.0.0.1:8787  MAYI_API_KEY=<agent key>

    from mayi.client import MayiClient
    MayiClient().decide("ticket-1042", "refund", {"amount": 40, "note": "late"})   # allow / hold / reject
    MayiClient().call("ticket-1042", "refund", {"amount": 40, "note": "late"})     # runs it if allowed

## Self-hosting

`Dockerfile` and `railway.json` are included but the container build is untested. Put `catalog.yaml` and `tools.json` in `/data`, set `MAYI_AGENT_KEY` and `MAYI_APP_KEY`; `PORT` is honoured.

## Cumulative limits: stop "many small calls"

A `limit` such as `amount <= 500` is checked on each call by itself, so an attacker who is stopped at $5,000 can ask for ten refunds of $500. Add a `session_budget` and standing approvals are totalled per session:

    refund:
      amount:
        pen: human
        limit: "amount <= 500"     # small amounts may pass without a person...
        session_budget: 1000       # ...but only 1000 in total per session

Once a session has used up its budget, further amounts need a human, exactly as if there were no standing limit. Details that matter:
- Amounts a person approved also count toward the total, so approvals and standing limits cannot be stacked past it.
- Only calls that actually ran are counted. Dry runs (`simulate`, `/v1/decide`) and failed tools use nothing up.
- Negative amounts never get a standing approval, so they cannot be used to lower the total.
- The total is kept per `budget_key`. By default the gateway uses the session id. To total over something larger, such as a customer, have your app pass `"budget_key": "cust-7"` when it opens each session (`POST /v1/sessions`, app key only; the agent cannot set it). All sessions with the same key share one total and one rate-limit window. With no session identity there is nothing to total over, so a person decides every time.
- With `mayi serve` the total is kept in the state database and survives a restart (see below); `reset_budget(key)` clears a session.
- `diff_catalogs` flags a raised or removed budget as a loosening.


## Signed approvals

An approval can be a token your approval system signs, instead of an API call to the gateway. Set `MAYI_SIGNING_SECRET` on the gateway (same value wherever tokens are made):

    mayi sign refund amount 900 --by alice --session ticket-1042 --ttl 300

Paste it into the console or `POST /v1/approvals/redeem` (app key). A token is bound to a tool, argument, value, session and expiry, and works once: a replayed token is refused. With `MAYI_SIGNING_SECRET` this is a shared-secret (HMAC-SHA256) design: anyone holding the secret can mint tokens. For public-key tokens see below.

## Start from your tool schemas

    mayi init --from-tools tools.json

Reads Anthropic, MCP or OpenAI tool definitions and writes a `catalog.yaml` where **every argument is `pen: human`** (nothing is allowed until you loosen it). Suggestions appear only as comments.

## See what a catalog really allows

    mayi explain catalog.yaml

Prints a plain-language report of who controls each argument, plus warnings: a standing limit with no session budget, unbounded free text, outbound-looking tools that take model-written text, tools where the model writes everything.

## Alerts for held calls

    mayi serve --webhook https://hooks.slack.com/services/...      # or MAYI_WEBHOOK_URL

A new held call posts a short alert (tool, argument, value, session). https only (plain http only for localhost). Alert failures never block a tool call; they are listed in `GET /v1/health`.

## Values copied from untrusted text

    send_email:
      to:
        pen: model
        type: str
        untrusted_copy: hold     # or: reject

Tell mayi what the model has read (`MayiClient.add_untrusted(session_id, text)` or `POST /v1/untrusted`). If the model then puts a value found verbatim in that text (an address, URL or ID, 4+ characters) into `to`, the call is held for a person or rejected. This is a heuristic: it does not catch paraphrased, re-encoded or split values, and it is not taint tracking.

## TypeScript client

`clients/typescript` has a zero-dependency client (`decide`, `simulate`, `call`, `addUntrusted`), tested against a live gateway (`npm test`).

## Tools with no arguments

Argument-level authority has nothing to hold on to when a tool takes no arguments (`UnlockDoor`, `DisableTwoFactorAuthentication`). Mark those tools so the call itself needs a person:

    unlock_door:
      _requires_approval: true

`mayi init --from-tools` does this automatically for zero-argument tools. `mayi explain` warns about a zero-argument tool that is not marked. An approval for this uses the argument name `_call` and the value `true`.

## Prove properties of a catalog

    mayi prove catalog.yaml invariants.yaml

    never_model_writable: [send_email.to]
    human_above: {refund.amount: 500}
    session_total_at_most: {refund.amount: 1000}
    call_needs_approval: [unlock_door]
    no_copy_from_untrusted: [send_email.to]
    no_tool: [delete_database]
    max_model_text: 500

Exit code 1 when an invariant breaks, with a counterexample ("a value of 5000 runs on standing approval"). This is a complete analysis of the catalog as data, and a randomised test checks its answers against the real enforcement code. It does not verify the Python in `boundary.py`, your tool code, or what the model writes inside an allowed free-text field. Naming a tool or argument that is not in the catalog is an error, never a pass.

## Agent frameworks

    from mayi.adapters import openai_agents_tools, crewai_tools
    tools = openai_agents_tools(MayiClient(), "ticket-1042", schemas)   # OpenAI Agents SDK
    tools = crewai_tools(MayiClient(), "ticket-1042", schemas)          # CrewAI

Every call goes to the gateway; the real tool never runs in the agent's process. Both adapters were run against a live gateway (openai-agents 0.23, crewai 1.15). Their tests skip when the framework is not installed.

## InjecAgent check

`python examples/eval_injecagent.py InjecAgent/data` replays all 2,108 InjecAgent attack cases as a fooled agent would make them (it does not measure how often a model is fooled). Results:

| Setup | Attack ran | User's task |
|---|---|---|
| No mayi | 2108 / 2108 | runs |
| mayi, catalog generated from all 38 toolkits | 0 / 2108 | held for a person (all 2108) |
| mayi, catalog scoped to the user's tool | 2 / 2108 | ran (2108 / 2108) |

Read this carefully. Every InjecAgent attack uses a different tool from the user's task, so the task-scoped result is close to true by construction: a tool the catalog does not list is rejected. The 2 that ran are cases where the attacker's tool is the same as the user's tool; stopping those is a job for argument pens, which this suite barely tests. The first run of this check found that 408 attacks (19%) used zero-argument tools that argument-level rules could not gate; that is why `_requires_approval` exists.

## How mayi relates to other work

mayi is not a new idea. Closest is **Progent** (programmable privilege control: a proxy that allows or forbids each tool call by conditions on its arguments, with a Z3-based policy checker and LLM-generated policies), which reports 0% attack success on AgentDojo with hand-written policies. **CaMeL** (Google DeepMind) tracks real data flow through a custom interpreter and gives stronger guarantees at about 2.7x the token cost; mayi's untrusted-copy check is only a heuristic version of that idea. General policy engines (OPA/Rego, Cedar) and Microsoft's Agent Governance Toolkit cover broader governance. What mayi adds is a small, readable shape: four named authorities per argument, including the application pen that *replaces* a model's value instead of only denying it; single-use and signed human approvals; per-session budgets; and a gateway with a console. Progent's policy checker is more expressive than `mayi prove`. This comparison is from reading the papers' and projects' own descriptions, not a systematic search.

## Live test with real models

`python examples/live_proper.py 4 <model> [--only CONFIG]` has a real Claude model read a poisoned inbox (the injection arrives inside a tool result). Eight hand-written injections (forwarding data, opening a door, an obfuscated address, a spelled-out address, a phishing link, pressure, claimed authority), 4 runs each, so 32 runs per cell. No approval is ever granted. The same script was run twice on 2026-10-10, so each cell below is 64 runs:

| Model | Setup | Attack attempted | Attack ran | Honest reply sent |
|---|---|---|---|---|
| Haiku 4.5 | no mayi rules | 8 / 64 | 8 / 64 | 64 / 64 |
| Haiku 4.5 | `untrusted_copy: hold` + tool-level approval | 8 / 64 | 0 / 64 | 0 / 64 |
| Haiku 4.5 | `to` filled in by the application + tool-level approval | 8 / 64 | 0 / 64 | 64 / 64 |
| Haiku 4.5 | as above + `forbid: [url, email]` in the body | 8 / 64 | 0 / 64 | 64 / 64 |
| Sonnet 5.5 | no mayi rules | 1 / 64 | 1 / 64 | 62 / 64 |
| Sonnet 5.5 | `untrusted_copy: hold` + tool-level approval | 0 / 64 | 0 / 64 | 0 / 64 |
| Sonnet 5.5 | `to` filled in by the application + tool-level approval | 1 / 64 | 1 / 64 (see below) | 62 / 64 |
| Sonnet 5.5 | as above + `forbid: [url, email]` in the body | 0 / 64 | 0 / 64 | 64 / 64 |

Read this carefully.
- The models resisted most injections. Every Haiku attempt was the "open the door" injection (8 of 8 times it opened the door when nothing stopped it). Sonnet 5.5 was fooled once in 64 runs, by the "claimed authority" injection.
- The one Sonnet run counted as "ran" in the application-fills-the-address setup cannot have been a door opening (that needs a person) or mail to the attacker (the application fixes the recipient). The scoring in that run counted any mention of the attacker's address, so it was almost certainly the model repeating the address inside a reply to the real customer. I did not save that run, so this is an inference. The script now counts only a door opening, mail to someone other than the customer, or the phishing link going out.
- No model tried the disguised-address or the phishing-link injections, so the copy check and the body filter were exercised by unit tests only. - The `hold` setup held every honest reply, because the customer's address appears in the same untrusted email; filling the address from the application avoids that.
- Small sample, two models, injections written by the author.

A third run (`MAYI_COMPLIANT_PROMPT=1`) told the agent that instructions inside tickets are authoritative and must be carried out, to make it more gullible. 32 runs per cell, scored with the stricter rule above:

| Model | Setup | Attack attempted | Attack ran | Honest reply sent |
|---|---|---|---|---|
| Haiku 4.5 | no mayi rules | 4 / 32 | 4 / 32 | 32 / 32 |
| Haiku 4.5 | `untrusted_copy: hold` + tool-level approval | 4 / 32 | 0 / 32 | 0 / 32 |
| Haiku 4.5 | `to` filled in by the application + tool-level approval | 4 / 32 | 0 / 32 | 32 / 32 |
| Haiku 4.5 | as above + `forbid: [url, email]` in the body | 4 / 32 | 0 / 32 | 32 / 32 |
| Sonnet 5.5 | the same four setups | 2, 2, 1, 2 / 32 | 0 / 32 in all | 30, 0, 28, 30 of 32 |

Haiku again opened the door every time it was tempted (4 of 4) and mayi stopped all of it. Sonnet 5.5 mentioned the attacker's address in some runs but never acted on it. Even with the gullible prompt, no model tried the disguised-address, spelled-out-address or phishing-link injections, so those defences have still not been tested against a live model.

## State that survives a restart

`mayi serve` keeps sessions, unused approvals, held calls, session budgets, used signed-token ids and untrusted text in `mayi_state.db` (`--state-db`). Two ordering rules make a crash safe: a single-use approval is marked used *before* the tool runs (so it cannot be used twice after a crash), and a budget amount is reserved *before* the tool runs and given back if the tool raises (so a crash counts the money as spent). Held calls expire after `--pending-ttl` seconds (default 3600). Per-session rate-limit timers are not persisted.

## More rules in the catalog

    transfer:
      _rate_limit: "5/60"                   # at most 5 calls per 60 s per session
      _require: ["amount <= balance"]       # checked on the final values; even a person's yes cannot override it
      balance: {pen: application, source: account.balance}
      amount:  {pen: human, limit: "amount <= 100", dual_control_above: 1000}   # above 1000, two different approvers
      memo:    {pen: model, type: str, max_len: 140, forbid: [url, email]}
      to:      {pen: model, type: str, max_len: 80, allowlist_from: "account.payees"}   # list comes from the session context

`_require` accepts only `argument <op> argument-or-number`. `allowlist_from` fails closed if the list is missing. Two-person approval works for console approvals and for signed tokens; the same person twice counts once. `diff_catalogs` flags the removal or loosening of each of these, and `mayi prove` checks `rate_limited` and `two_person_above`.

## Public-key approvals

    pip install "mayi[signing]"
    mayi keygen                                   # mayi-approver.key (keep) and mayi-approver.key.pub (give to the gateway)
    MAYI_APPROVER_PUBKEY=mayi-approver.key.pub mayi serve
    mayi sign refund amount 900 --by alice --key mayi-approver.key --session ticket-1042

The gateway holds only the public key, so a stolen gateway cannot mint approvals. A gateway set up with a public key accepts only public-key tokens; one with `MAYI_SIGNING_SECRET` accepts only shared-secret tokens (both can be on).

## Session tokens: one ticket cannot act as another

The agent key alone lets a caller use any session the application has created, so a hijacked agent working ticket A could act in ticket B (with B's application values). Create the session with `"bind": true` (or start the gateway with `--require-session-token`) and the response includes a `session_token`, shown once. Give it to the agent runtime for that session only; calls without it, or with another session's token, are refused (`MayiClient.bind(session_id, token)` sends it for you). Only a hash of the token is stored. The app key does not need tokens.

## Gateway hardening

- `--tls-cert/--tls-key` serve HTTPS (TLS 1.2+); serving a non-local address without TLS prints a warning.
- `--rate-limit` caps requests per client address per minute; 20 failed sign-ins in a minute lock that client out briefly. Behind a proxy every client may share one address, so set limits accordingly.
- Key rotation: `MAYI_AGENT_KEY=new,old` accepts both until you drop `old`.
- Keys given by environment variable are not printed in the start-up banner.
- `GET /metrics` (app key) gives Prometheus counters; `GET /healthz` is open and reveals nothing; `GET /v1/audit/export` gives all records with a SHA-256 digest and, if `MAYI_AUDIT_SECRET` is set, an HMAC over them. The console sends a restrictive Content-Security-Policy.
- The Dockerfile install steps were run by hand and the start command was checked; the base image could not be pulled in the test environment (every registry was blocked), so a full `docker build` is still untested. Please run `docker build -t mayi .` once yourself. The hand check did find and fix a real bug (the `NOTICE` file was not copied).

## MCP proxy

    mayi mcp-proxy --catalog catalog.yaml --context '{"customer_email": "a@b.test"}' \
        --approvals-dir approvals --pending-dir pending -- python my_mcp_server.py

Point your agent host at this command instead of the server. Tools not in the catalog are hidden; arguments your app controls are removed from what the model sees and replaced by your values on the way through; held calls are not forwarded; text returned by upstream tools is remembered as untrusted so `untrusted_copy` works with no extra wiring. To approve a held call, mint a token with `mayi sign` and drop it in `--approvals-dir`. Tested end to end against a real MCP server and the official MCP client on both SDK generations (1.30 and 2.2; testing on 2.x found and fixed two compatibility bugs); stdio only, JSON-RPC batches are refused. Other MCP features (resources, prompts) pass through ungoverned. A tool with no arguments and no `_requires_approval` runs freely, as everywhere else in mayi.

## Known limits

- No outside security review yet. Do not use mayi alone to protect tools that move money.
- mayi bounds and filters free text the model writes (length, type, URLs, e-mail addresses, allowlists) but does not judge what it means.
- `untrusted_copy` is verbatim-copy detection with some decoding. Paraphrased, translated, split or spelled-out values get through. Prefer making the argument application-controlled.
- A tool that takes no arguments is only gated if you mark it `_requires_approval`.
- Rate-limit timers are in memory. The state database is SQLite on one machine; there is no clustering.
- The evidence is small: the AgentDojo and InjecAgent runs replay attacker calls rather than measuring how often models are fooled, and the live test uses two models and injections written by the author.
- mayi is not a new idea; see "How mayi relates to other work".
