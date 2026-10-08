# AGENTS.md

## Project

An AI agent may propose a payment, but a provider call happens only if a human approved that exact effect hash (`sha256("approve-exact/v1\n" + canonical_json(effect))`, including amount in paise, currency, customer, description, and idempotency key), the HMAC-signed approval is valid and unused at execute time, the row is claimed once, and the provider is called with that same key (timeouts go to `unknown` and are reconciled, never resent with a new key; `verify` re-reads the provider record field by field).

Planned layout: `src/approve_exact/{__init__,effect,approval,store,executor,cli}.py`, `src/approve_exact/adapters/{__init__,base,fake,razorpay}.py`, `tests/test_{effect,approval,store,executor,razorpay,razorpay_live,cli}.py`, `examples/agent_demo.py`, `README.md`, `THREAT_MODEL.md`, `AGENTS.md`, `LICENSE`, `pyproject.toml`, `.gitignore`, `.env.example`, `.github/workflows/ci.yml`.

## Commands

Setup:

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

Checks:

```bash
ruff check . && ruff format --check . && mypy src && pytest -q
```

Live test (manual only):

```bash
pytest -m live
```

## Dependencies allowlist

- Runtime: `httpx`
- Dev: `pytest`, `hypothesis`, `ruff`, `mypy`
- Anything else: ask first.

## Always

- Run all checks before saying a step is done.
- Keep every file under ~300 lines.
- Type hints everywhere.
- One module = one job (no god files, no `utils.py`).
- Amounts are integers in paise, never floats.
- Tests for every refusal path.
- Plain, short docstrings.

## Ask first

- Adding any dependency not on the allowlist.
- Creating files or folders outside the planned layout.
- Any shell command that installs, deletes, or moves files.
- Changing the hash format or the status list.
- Touching CI.
- Any network call to Razorpay.

## Never

- Commit secrets, keys, `.env`, or the SQLite file.
- Use Razorpay keys that don't start with `rzp_test_`.
- Push, create a remote repo, force-push, rewrite or back-date history.
- Add `Co-authored-by` or tool/assistant trailers.
- Write text in any file or commit saying it was produced by AI, an assistant, or a code tool.
- Mention Linux or rendering in any file.
- Put test counts, coverage numbers, or star counts in the README.
- Add dashboards, web UI, multi-tenant, quorum, sagas, policy languages, or an LLM inside the library.

## Commits & PRs

- One milestone = one PR-sized change, made of small commits.
- Plain lowercase imperative messages (e.g. `add canonical effect hash`, `refuse expired approvals`).
- No emojis, no prefixes like `feat:`.
- I say OK before each commit.
