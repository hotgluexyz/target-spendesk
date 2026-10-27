# target-spendesk development

Python 3.10, Hotglue Singer SDK 1.0.51. Read README.md for the input and recovery contracts.

- `target_spendesk/target.py` registers streams and owns configuration.
- `target_spendesk/client.py` owns client-credentials bearer auth, company-scoped requests,
  external-reference reconciliation, durable create intents, and shared record loading.
- `target_spendesk/sinks.py` owns native Spendesk payloads. Shape payloads in
  `preprocess_record`; `upsert_record` sends the already-prepared request and checks its outcome.
- Keep `externalId` in state-bearing records. Strip control fields only in copied HTTP bodies.
- Do not retry ambiguous writes. Preserve pending create intents until reconciliation.
- Never treat HTTP 200/207 as per-record success without inspecting API outcomes.
- Success write-back and exportable external references need explicit `exportConfirmed`.
- Keep credentials, Singer data, snapshots, logs and job state out of Git.
- Exercise changes through Singer input and inspect state, not only direct sink methods.
- Do not add ETL-specific Rillet mapping logic here.

Checks (dedicated target venv):

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Sandbox test writes must be narrow and read back. The demo API has known stale-version
and payable-status read-back discrepancies; see README.md. Do not claim remote persistence
solely from a successful SDK bookmark or API acknowledgement.
