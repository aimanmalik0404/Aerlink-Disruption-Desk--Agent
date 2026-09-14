# DECISIONS

> Copy this file to `DECISIONS.md` and work through it. Keep it in your own words — bullets are
> completely fine and usually better than prose. Where a heading doesn't apply to what you built,
> say so rather than deleting it.
>
> This is the document our interview is built around. Please leave real time for it.

Name: Aiman Malik
Time spent: Considerably more than the advertised 3 hours.
How to run it:
        cp .env.example .env   # fill in OPENAI_API_KEY
        python3 env/ops_server.py   # or: cd env && docker compose up
        pip install -r requirements.txt
        python run.py --all --reset      # full sweep over cases/
        python run.py --case cases/case-01   # or: a single case (works for a brand-new, unseen case folder too)

## 1. Approach

I built a Python agent that reads one inbound passenger email, works the case using OpenAI function-calling against a set of tools that map 1:1 onto the Aerlink operations API, and finishes by calling a dedicated submit_case_record tool that produces a structured JSON record (decision, reasoning, sources consulted, real actions taken, uncertainties, human follow-up). It is a ReAct-style loop, not a fixed script: the model decides which tools to call and in what order, because the twelve cases turned out to be too different in shape (single passenger vs. five passengers on one booking, a straightforward cancellation vs. a mid-thread instruction reversal, an embedded prompt-injection attempt) for one rigid pipeline to fit all of them well.

What else I considered. A fully deterministic pipeline (extract → look up entitlement → check availability → act, with the LLM only doing text extraction at fixed points) was the alternative. I rejected it because the cases are too heterogeneous — Chidi Okonkwo's five-passenger booking alone needed five different outcomes from one email, which a fixed sequence of steps cannot express naturally. Instead I used a hybrid: anything mechanical and correctness-critical (retry logic, which flight options are even legal to pick, whether a payment amount is grounded in real numbers, whether every passenger has an actual travel outcome) is enforced in code as a hard guardrail, not left to the model's judgment. Everything else — identifying the passenger, understanding intent, deciding whether to act or escalate — is genuinely the model's call, steered by the system prompt.

## 2. Assumptions

The mock environment has no concept of "now." Flights on the same calendar date as a cancellation are all treated as valid options by the mock data generator, with no simulated wall-clock time. I addressed this by filtering options against the original disrupted flight's own schedule (never pick something that would not be a genuine improvement in arrival time) rather than trying to simulate a "current time" the exercise does not provide.

Vulnerable or dependent passengers with ambiguous or conditional instructions default to escalation, not auto-action. E.g., an elderly wheelchair-using passenger whose family member expressed a preference on her behalf, without her own decision being confirmed. I decided the safer default is a human makes that call, not the agent.
pay_goodwill and pay_compensation (without passenger_ids) are always booking-level actions, never per-passenger, because the real API does not support scoping goodwill to specific passengers. I chose to record these once in duty_of_care_actions as free text (naming the intended passenger if relevant) rather than fabricate a per-passenger structure the API does not actually have.

A refund or goodwill amount must trace to a real number from the booking or entitlement data — I did not invent a single canonical formula for "the right refund amount" (the brief does not specify one), but I required any submitted figure to match one of a small set of defensible candidates derived from the booking's own total_paid_gbp or segment_fare_gbp, rather than allow an arbitrary number.

## 3. How you broke the problem up

system_prompt.py — the agent's standing instructions: how to establish ground truth, when money must come from calculate_entitlement rather than being computed, how to read a thread for the current instruction, how to disaggregate a multi-passenger booking, when to escalate instead of act.

tools.py — the OpenAI function-calling schemas for every ops endpoint, the OpsClient that actually calls the API, and several hard-coded guardrails that live here rather than in the prompt.

agent_loop.py — the ReAct loop itself, plus the guardrails that run specifically when the model tries to finalize a case (submit_case_record), and the fallback that reconstructs real actions taken if a case runs out of budget before finishing.

run.py — the entry point: reads one case or all of them, runs the loop, writes record.json + transcript.json per case, plus a batch summary.json.

check_records.py — a post-hoc script that reconciles what the records claim against the ops server's own live entitlement recalculation and its permanent /_audit log.

trace_writes.py — a small diagnostic I wrote once check_records.py found problems, to map every real write in the audit log back to the exact case/booking that produced it, rather than guess.

This split exists because I kept discovering, through actually running cases against the live mock server, that telling the model a correctness-critical rule in prose was not reliable enough — it needed to be enforced in code. The cost of this split is more code to maintain and more places a bug could theoretically hide; the benefit is that the things that matter most (right booking, right amount, no runaway spending) do not depend on the model reliably following instructions every single time.

## 4. The operations API

Used: bookings/search, bookings/{ref}, flights/{no}, flights/availability (+ /partners), policy/search, policy/document, customers/{id}/history, entitlements/calculate, stations/{iata}/hotel-allocation, rebooking, vouchers/hotel, payments/compensation, payments/goodwill, refunds, escalations, _audit, _reset.

Used rarely, by design: disruption/feed — it is network-wide and token-heavy relative to the value it adds for a single case; the prompt tells the model it is rarely needed rather than excluding it outright.

Implemented but never actually exercised in testing: rebooking/{id}/cancel — none of my 12 cases needed a correction of a bad rebooking, though the tool is available if the model judges one is needed.

Reshaped, not passed through as-is: search_availability is the biggest change. I made booking_ref a required parameter (the raw API makes it optional), because I found — twice, in real test runs — that the model would otherwise pick a flight that departed before the original disrupted flight, which by the time the case is being worked has already left. The wrapper now uses booking_ref to filter out any option that is not a genuine improvement on the original schedule, filters out options with zero seats available (the mock API does not itself reject a sold-out booking), and sorts what is left soonest-first — so "pick the first result" became a safe default instead of something the model had to reason about correctly every time.

Deliberately not used for money: policy/search and policy/document are never used to derive a compensation or entitlement figure — calculate_entitlement is the sole authority for that, per its own documented note that it takes precedence over anything derived from reading the policy text.

## 5. Prompting

The parts I actually spent time on, and the ones that took multiple attempts:

Ground truth over passenger claims: "The passenger's account of what happened is a starting point, not a fact. Establish what actually happened from Aerlink's own operational records... If the passenger says a flight was 'cancelled' or 'delayed three hours', verify it." — I would defend this line; it is the one thing every case needs to get right first.

Never compute money yourself: "Never derive a compensation figure yourself from policy text, general aviation-regulation knowledge, or arithmetic on distance/delay — even if you are confident you know the rule." This is enforced by a code guardrail too (see §7), because I found prompting alone let a fabricated but plausible-looking number through.

Instructions can reverse mid-thread: "Read for the current instruction, not the first one you encounter... the further instruction governs." Written specifically for a case where a passenger requested a refund, explicitly withdrew it, then asked to be rebooked instead.

One booking, several outcomes: explicit instruction not to collapse a multi-passenger booking's different requests into one action — and, after a real failure, an explicit line that duty-of-care items (a hotel voucher, a share of compensation) are not a substitute for actually deciding whether or how someone travels.

Untrusted embedded content: free text returned by the API (special requests, forwarded email threads) is treated as data to reason about, never as instructions to follow — this mattered directly for a case where a forwarded internal message tried to instruct the agent to skip verification and pay a large discretionary amount.

What I tried that made things worse, or did not hold, and had to be moved into code instead of relying on prompt wording:

    Telling the model never to pick an earlier-departing flight than the original — failed twice in real testing before I moved the rule into search_availability itself.

    Telling the model not to retry an identical failed tool call — it retried a failed hotel voucher call multiple times regardless; now hard-blocked per passenger/night in code.

    Telling the model to size goodwill and refund amounts responsibly — it invented figures (£60, £64.69, and a refund of £0 issued three times with its own reasoning admitting the number was a placeholder) before I added guardrails requiring any amount to trace to a real cap or fare figure.

How I got the record format I wanted: a dedicated submit_case_record tool with a strict JSON schema, rather than parsing free text — this gets structural validation for free and gives the model one unambiguous way to signal "the case is done," which matters for enforcing a turn/cost budget.

## 6. Models and cost

Where:                  
    The entire case-working loop
Model:
	gpt-4o-mini	
Why:
    Cheap, non-reasoning chat model — no hidden reasoning-token cost blowups in a long tool-calling loop (unlike newer reasoning-tier models), and judgment quality was sufficient once the correctness-critical rules were moved into code guardrails rather than left entirely to the model.

Actual cost of a full run over the twelve cases: $0.1419 (well under the $2 requirement). Total tokens (in/out): Not separately logged in the current implementation — cost was estimated per API call from OpenAI's own usage object and summed across the run (see agent_loop.py's _estimate_cost). I did not cross-check this estimate against the OpenAI billing dashboard before writing this up; worth doing before the actual submission run. How I measured it: Same mechanism as above — an in-code running total, written to outputs/summary.json after each batch run.

What I did to keep cost down: a per-case turn cap (20), a tool-call cap (25), and a soft cost ceiling ($0.20/case) that forces an escalation rather than letting a confused case run indefinitely — this actually triggered once during testing, on a case that got stuck searching for hotel accommodation across too many stations and dates before the guardrails existed. I did not need to spend more on a stronger model to fix that; a cheaper, mechanical fix in code solved it without touching the model at all.

## 7. Failure and safety

When a dependency doesn't respond: flights/availability is documented as intermittently returning 503 — this is retried automatically in code (not left to the model) with backoff, since it is a known, mechanical failure pattern.

When the system is not sure: calculate_entitlement returning INSUFFICIENT_DATA, a lookup returning not_found, or genuine ambiguity in a passenger's request all get written into uncertainties/human_follow_up rather than silently resolved one way or the other.

What stops it doing something expensive, wrong, or irreversible — this is the section I iterated on most, because testing kept surfacing real gaps:
    A hard turn/tool-call/cost budget, so a confused case degrades to a forced escalation instead of looping indefinitely.

    search_availability structurally excludes any flight option that departs before the original disrupted flight, or has no seats left — the model cannot select one even if it tries.

    A hotel voucher can't be issued twice for the same passenger and night, or beyond the entitlement's max_nights cap, per passenger — tracked in code, not the prompt.

    A goodwill or refund payment must be traceable to a real duty-of-care cap or a real fare figure from the booking before it's allowed through.

    A COMPENSATED outcome's amount is cross-checked against the live calculate_entitlement result before a case can be finalized.

    A case cannot be finalized while any real passenger on the booking has no outcome that actually resolves their journey (rebooked, refunded, escalated, or explicitly no action needed) — added after testing found a wheelchair-using passenger on a five-person booking was given a hotel voucher and a goodwill share but was never actually rebooked, refunded, or escalated, and the case still closed as "resolved."

    A real, booking-level goodwill or compensation payment cannot be recorded as several fabricated per-passenger payments (this happened once in testing and would have overstated real spend).

Worst case if it gets a case badly wrong: real money or inventory spent against the wrong booking or amount, with the mistake never surfacing to a human because the written record looked clean. The guardrails above target exactly this category of mistake; when the system cannot verify something (e.g., calculate_entitlement was never called), the relevant guard fails toward blocking the action rather than silently allowing it.

If a case is forced to escalate on budget, the escalation record still reconstructs every real action already taken from the ops server's own call log — a human is never left blind to real, already-executed actions just because the agent itself never finished.

## 8. How you know it works

check_records.py re-runs calculate_entitlement live for every case that claims a COMPENSATED outcome and checks the figure still matches, and reconciles the total compensation and refund amounts claimed across all records against the ops server's own permanent /_audit log. It flags mismatches rather than silently passing.

What it caught in my final full run: a £240 discrepancy, which I traced (using trace_writes.py) to one case where a real compensation payment and a real downgrade-reimbursement goodwill payment — both individually correct — were merged into a single fabricated COMPENSATED line in the written record instead of two separate outcome entries. It also flagged that real hotel vouchers were sometimes recorded as free text in duty_of_care_actions rather than as a structured VOUCHER_ISSUED outcome — a documentation-completeness gap across several cases, not a financial error (the money and inventory were always spent correctly).

Where it is blind: it cannot judge whether the reasoning in a record is actually good, only whether the numbers reconcile structurally. It also cannot exactly reconcile rebooking/voucher counts when multiple passengers are grouped into one API call versus recorded as separate outcome entries — documented as a known limitation rather than fixed, since a proper fix needs an action_ref field linking each outcome to the exact write ID it corresponds to, which I did not have time to add.

With a month instead of three hours, I would add that action_ref linking to make reconciliation exact rather than approximate, build a proper regression suite that re-runs all 12 cases (plus deliberately adversarial ones) on every prompt or guardrail change instead of relying on manual re-runs, and for production I would want alerting on any case that hits the turn/cost budget wall, on any check_records-style reconciliation failure in near-real-time, and on drift in escalation rate or average payout over time.

## 9. AI assistants

There were three sessions: Claude and Chatgpt. Transcripts are in transcripts/.

Session / file	Tool (What I was doing in it):	
Claude (claude.ai):
    Understanding the brief, architecture discussion, relaying code between sessions, catching and root-causing bugs by reading real transcripts, writing system_prompt.py, tools.py, and agent_loop.py across many rounds based on bugs I found and relayed back to it.
Claude:
    Read the full case set up front and wrote run.py, check_records.py, and requirements.txt.
Chatgpt:
    Explained all the 12 cases in eaiser words for better understanding.

Where I overrode them: Early on, I initially treated a "the model rebooked someone onto a flight that had effectively already departed" bug as a minor, document-do not-fix issue. Then the session pushed back and traced the actual root cause (no selection criterion given to the model, and booking_ref never passed to the availability search), and I changed my recommendation to fix it properly rather than just note it — a case where I was wrong to deprioritize something and said so.

Where I let them run: I accepted several complete guardrail rewrites (the per-passenger hotel voucher tracking, the refund-grounding logic) largely as given once I had checked the reasoning was sound, rather than re-deriving them myself — the code comments explained the exact real failure each guard was written to prevent, which was enough to trust it.

How I drove them: In small, reviewed steps — after nearly every code change, I actually ran the case against the live mock server and read the real transcript.json/record.json before accepting a fix as done, rather than trusting it worked because it looked reasonable on paper. Several "fixes" that looked correct in the prompt text turned out not to hold in practice until tested. 

Anything they got wrong that took a while to notice: the search_availability tool schema was updated to require booking_ref in the client code but the schema still described it as optional for a full round — this only surfaced when I actually reasoned through what would happen if the model followed the (wrong) schema description. More significantly, an "early-departure flight" fix that read correctly in the prompt text did not actually change model behavior — it took a full second real test run to discover the fix had not held, which is what pushed the eventual move to a code-level guardrail instead.

## 10. What you left out, and what you'd do next

Left out: exact per-passenger fare apportionment for genuinely uneven fare structures (the refund guard checks a small set of defensible candidates, not a guaranteed-correct formula for every case); the action_ref linking mentioned in §8; a mechanical de-duplication of identical failed tool calls (guards stop them from succeeding twice, but the model can still waste a turn retrying); verification of goodwill "authority" against actual policy clauses every time (currently a keyword-based heuristic, not a real policy check).

What I would fix first with another day: the action_ref linking, since it would make the audit reconciliation exact instead of approximate, and a guardrail verifying every real write is reflected somewhere in the final record, structured or not — testing found a real, recurring pattern of hotel vouchers being paid for but under-recorded.

Something I shipped that I am not fully happy with: the downgrade-reimbursement case (Tomas Ferreira). The math is correct and grounded in calculate_entitlement, but there is no dedicated API endpoint for a downgrade reimbursement, so it is paid via pay_goodwill as a workaround, and it took several iterations to get the written record to correctly split this into two distinct entries instead of merging them into one fabricated total. It works, but it is a workaround around a gap in the API rather than a clean fit.

## Anything else

I went well past the 3-hour timebox on this, and I want to be upfront about why rather than quietly not mention it. A large part of the extra time was genuine debugging: running real cases against the live mock server surfaced several real correctness bugs (an already-departed rebooking, fabricated compensation figures, an unaddressed passenger's travel disposition, a mislabeled payment) that would have been easy to miss by only reading the code or the prompt. Given the choice between finishing faster with those bugs still in the system, or taking longer to actually find and fix them, I chose the latter — but I recognize that is a real trade-off against the brief's own framing of "three focused hours," and I would rather say so than imply this was a clean three-hour build.
