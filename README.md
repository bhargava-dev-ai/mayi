<p align="center">
  <img src="https://raw.githubusercontent.com/bhargava-dev-ai/mayi/main/assets/logo.png" width="120" alt="mayi logo">
</p>

<h1 align="center">mayi</h1>

<p align="center"><b>The agent proposes. Your rules decide.</b></p>

<p align="center"><a href="https://bhargava-dev-ai.github.io/mayi/">Website and live demo</a> · <a href="https://pypi.org/project/mayi/">PyPI</a> · <a href="docs/GUIDE.md">Guide</a></p>

<p align="center">
  <a href="https://github.com/bhargava-dev-ai/mayi/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/bhargava-dev-ai/mayi/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://pypi.org/project/mayi/"><img alt="PyPI" src="https://img.shields.io/pypi/v/mayi"></a>
  <img alt="Python" src="https://img.shields.io/pypi/pyversions/mayi">
  <img alt="license" src="https://img.shields.io/badge/license-Apache--2.0-blue">
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/bhargava-dev-ai/mayi/main/assets/attack-demo.gif" width="480" alt="An injected refund of 9000 is held while a refund of 40 runs">
</p>

mayi (as in "may I?") enforces who controls each argument of an AI agent's tool call. Permissions usually say which tool an agent may call. They do not say which value it may pass, so `refund(40)` and `refund(9000)` look the same. mayi sits between the agent and your tools and gives every argument one owner:

| Owner | What happens |
|---|---|
| **application** | your app supplies the value; whatever the model proposed is thrown away |
| **model** | free text, checked for type, length, allowlists and forbidden content |
| **human** | a single-use, expiring approval, or a bounded limit with a session budget |
| **derived** | computed by a formula a human wrote |

The rules are plain code. There is no second model judging the first. Anything not in the catalog is rejected.

## Try it

    pip install mayi
    mayi demo

Open the console at http://127.0.0.1:8787/console (key: `demo-app-key`). To watch a real Claude model get a hidden attack, run `mayi try-model` in a second window with `ANTHROPIC_API_KEY` set.

## Use it with your own tools

    mayi init                  # writes catalog.yaml and tools.json
    mayi check catalog.yaml    # validate your rules
    mayi serve                 # prints an agent key and an app key

Edit `catalog.yaml` to say who controls each argument. Your app opens a session with the values it controls; the agent calls `POST /v1/call`; held calls wait in the console for a person. More in the [guide](https://github.com/bhargava-dev-ai/mayi/blob/main/docs/GUIDE.md).

## Results

| Test | Without mayi | With mayi |
|---|---|---|
| AgentDojo banking, 9 injection tasks (replayed) | 9 / 9 succeed | 0 / 9 |
| InjecAgent, 2,108 attack cases (replayed) | 2,108 / 2,108 | 0 / 2,108 |
| Live Claude Haiku 4.5, 64 runs | 8 of 64 ran | 0 of 64 |

The replayed tests measure what happens after a fooled agent proposes the call, not how often models are fooled. The live test used two models and eight injections written by the author. Details, including where it did not help, are in the [guide](https://github.com/bhargava-dev-ai/mayi/blob/main/docs/GUIDE.md#live-test-with-real-models).

## Works with

Tested: the official MCP Python client (SDK 1.30 and 2.2) through `mayi mcp-proxy`, the OpenAI Agents SDK, CrewAI, LangGraph and the Anthropic SDK. Not yet tested: Claude Desktop, Claude Code, Cursor and other MCP hosts. Reports from people who try them are welcome.

## What is included

- Gateway with an audit log and a console; MCP proxy that puts any MCP server behind the same rules
- Single-use approvals signed with HMAC or Ed25519, two-person approval, session budgets, rate limits
- `mayi prove` to check a catalog for broken invariants, with counterexamples
- Adapters for the OpenAI Agents SDK and CrewAI, and a TypeScript client

## Limits

- No outside security review yet. Do not use mayi alone to protect tools that move money.
- It checks free text the model writes but does not judge what it means.
- The copied-value check is a heuristic. Paraphrased, translated or spelled-out values get through.
- It is not a new idea. Progent and CaMeL came first; the guide compares them.

## Run the tests

    pip install pytest hypothesis && python3 -m pytest -q

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) and the [roadmap](ROADMAP.md). To report a vulnerability, read [SECURITY.md](SECURITY.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
