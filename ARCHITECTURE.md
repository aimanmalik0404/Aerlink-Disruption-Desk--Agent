# Architecture

## Shape

Four pieces:

- `system_prompt.py` — the agent's standing instructions. This is where the judgment-call
  guidance lives (escalate-by-default for vulnerable/ambiguous binding decisions, instruction
  recency, per-passenger disaggregation).
- `tools.py` — the OpenAI function-calling schema for every ops endpoint, plus the Python
  functions that actually call the API. This is also where the two hard-coded, non-negotiable
  rules live in *code* rather than in the prompt: availability retry behaviour, and the
  entitlement-authority guardrail.
- `agent_loop.py` — the ReAct loop itself: one case in, one structured record out.
- `run.py` (not included below, described at the end) — the thing you actually invoke: reads a
  case, runs the loop, writes `record.json` + `transcript.json` per case, and a summary across a
  batch.

## Where the hybrid boundary sits

You asked for hard-coded correctness-critical logic and LLM judgment for everything else. Concretely:

**Hard-coded (in `tools.py`, invisible to the model as "logic" — it just sees clean results):**
- Availability retry/backoff. The model calls `search_availability` once; the wrapper retries
  on `503` internally (the server's own comment tells you the failure pattern: first call to any
  distinct query always fails, then every 7th). It also respects the 30 req/10s ceiling by not
  firing anything in parallel.
- Entitlement authority. The model is *told* in the prompt that `calculate_entitlement` is
  authoritative. But telling isn't enforcing, so `agent_loop.py` also checks it: before accepting
  a `submit_case_record` call, it looks at every `calculate_entitlement` result seen during the
  case and cross-checks the amounts the model is about to record and pay against them. If they
  don't match, the submission is rejected and the model is told why, in the same turn — it doesn't
  get to finalize a case with a compensation figure it invented or misread.
- Budget and turn caps, so a confused case degrades to a forced escalation rather than looping
  until the exercise's spending cap bites.

**LLM judgment (steered by the prompt, not scripted):**
- Who the passenger is and which booking they mean.
- What actually happened (reading the operational record rather than the passenger's account).
- What's being asked for, including tracking which instruction is *current* when a thread
  contradicts itself (Greg Whitmore).
- Splitting a multi-passenger booking into per-passenger outcomes (Chidi Okonkwo's five).
- Whether to act or escalate, with the vulnerable/dependent-passenger default you specified.
- Drafting the reasoning trail and the passenger-facing summary.

## The loop

1. System prompt + a rendered case prompt (raw message + any `meta.json` fields) go in as the
   first two messages.
2. Standard ReAct: call the model with `tools=ALL_TOOLS`, execute whatever it calls, feed results
   back as `tool` messages, repeat.
3. The loop only ends when the model calls `submit_case_record` — there's no other way out except
   the hard turn/cost cap, which forces an escalation record rather than silently stopping.
4. Every request/response pair is appended to an in-memory transcript and written to disk
   alongside the final record — this is both your "way to check it behaves correctly" artifact and
   half of your audit trail (the other half is the ops server's own `/_audit`).

## Why a single `submit_case_record` tool rather than parsing free text

Because the record shape is a hard requirement (decision, reasoning, sources, actions,
uncertainties, human follow-ups) and because it's the thing the entitlement guardrail needs to
inspect mechanically. Making it a tool call means you get JSON-schema validation for free from the
OpenAI API (malformed structure is rejected before it reaches you), and it gives the agent a clear,
single, unambiguous "I am done" signal — which matters for the turn-cap logic.

## What I left for you to wire up

- `run.py`: reads `cases/case-XX/{inbound.txt,meta.json}`, instantiates `OpsClient` and
  `OpenAI()`, calls `run_case(...)`, writes outputs. This is mechanical — deliberately not
  included so you're not just shipping code you didn't write.
- The `.env` loading (`python-dotenv` or plain `os.environ`).
- Whatever lightweight check you use for requirement 7 — a natural fit given the guardrail above
  is a script that re-runs `submit_case_record` payloads against `/entitlements/calculate` and
  the audit log for a handful of cases and asserts the amounts reconcile.