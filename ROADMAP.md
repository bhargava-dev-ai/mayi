# Roadmap

What is not done yet, in rough order. None of this is a promise.

- **Outside security review** of the boundary, the gateway and the MCP proxy.
- **Stronger copy detection.** Today it is a verbatim-copy heuristic with some decoding. Paraphrased, translated or spelled-out values get through.
- **More evidence:** more models, injections written by other people, and live tests of the disguised-address and phishing-link cases, which no model has tried yet.
- **More hosts tested** with `mayi mcp-proxy`: Claude Desktop, Claude Code, Cursor.
- **Persist rate-limit timers** and support more than one gateway instance. State is SQLite on one machine today.
- **Test the Docker image build** and publish a container.
- **Govern other MCP features** (resources and prompts) instead of passing them through.
