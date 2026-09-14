## User
based on the conversation i pasted, the claude session gave me the four attached files:
now give me the requirements file i need for this as requirements.txt, the run file as run.py and the verification script as check_records.py

## Claude
A few notes on what I filled in, since these decisions matter for your DECISIONS.md:

**`run.py`** — `.env` is loaded before `tools`/`agent_loop` are imported, since `tools.py` reads `OPS_BASE_URL`/`OPS_API_KEY` at import time into module-level defaults. A fresh `OpsClient` is created per case (not reused across the batch), because the entitlement guardrail in `agent_loop._reconcile_entitlement` inspects `ops_client.call_log`, and that needs to contain only the current case's calls. One case crashing writes an error-stub `record.json` and continues the batch rather than aborting it. `summary.json` gets per-case status/cost/turns plus a `status_counts` rollup.

**`check_records.py`** — this re-implements the entitlement check independently (fresh `calculate_entitlement` call, not the one the model saw mid-run) rather than just replaying the guardrail's own log, so it also catches a case where the *ops server's* answer has since changed or where the guardrail was bypassed some other way. I extended it past what the original guardrail covered, as flagged in the transcript: a `NOT_PAYABLE`/`INSUFFICIENT_DATA` check (hard fail), a downgrade-reimbursement check (warn only, since I don't know your exact field name for that in the entitlement response — you'll want to check `downgrade_reimbursement_gbp` against whatever the server actually calls it), and a write-action reconciliation that sums actual `pay_compensation`/`pay_goodwill`/`issue_refund` calls from the transcript and compares totals against what `passenger_outcomes` claims — that's the check most likely to catch "action taken but not recorded" or the reverse.

One thing to verify once you have the live server up: `check_records.py` assumes `calculate_entitlement`'s response shape matches what `agent_loop.py` already assumes (`response["passengers"]` list, each with `passenger_id`/`total_payable_gbp`). If the real API reference differs from that, tell me the actual shape and I'll adjust both files' field names.

## Files_created
run.py
requirements.txt
check_records.py

## Chat_link
https://claude.ai/share/ed61323a-b631-4f97-b279-ca1de6757d63
