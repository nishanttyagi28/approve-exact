# approve-exact

Approve the exact side effect, then execute it at most once.

An agent may propose a payment effect: amount in integer paise, currency, customer, description, and idempotency key. A provider call happens only after a human signs that exact effect hash, the signature and expiry check pass at execute time, and the store claims the approved row once. On provider timeout the row becomes `unknown` so the same key is never resent with a new one.

This library is for the approval boundary only. It does not choose who may approve, and it does not replace your provider’s own idempotency rules.

## Status

Work in progress: core effect hashing, signed approvals and guarded execution are done; verify, Razorpay test adapter and CLI coming next.

## Install

```bash
pip install -e .
```

## Tests

```bash
pytest -q
```
