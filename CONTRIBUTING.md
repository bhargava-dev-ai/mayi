# Contributing

Thanks for looking at mayi. The most useful contributions right now are attempts to break it.

## Ways to help

- **Try to bypass a catalog.** If you find a call that runs when the rules say it should not, open an issue with the catalog and the call. For a security hole, read [SECURITY.md](SECURITY.md) first.
- **Test another agent host or framework** and report what worked and what did not.
- **Add or improve an adapter** in `mayi/adapters.py`.
- **Improve the evidence:** more models, injections written by someone other than the author, other benchmarks.

## Set up

    python3 -m venv .venv && source .venv/bin/activate
    pip install -e ".[dev]"
    python3 -m pytest -q

## Pull requests

- Keep the core deterministic: no model calls in the decision path.
- Add a test for every change in behaviour. For a security fix, add a test that fails without it.
- Keep the README honest. If a change adds a limit, add it to the limits list.
