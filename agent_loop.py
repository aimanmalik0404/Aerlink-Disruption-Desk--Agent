"""The core loop: one case in, one validated record + full transcript out."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from openai import OpenAI

from system_prompt import SYSTEM_PROMPT, build_case_prompt
from tools import ALL_TOOLS, OpsClient, dispatch, get_cached_booking

DEFAULT_MODEL = "gpt-4o-mini"


@dataclass
class CaseBudget:
    max_turns: int = 20          # model round-trips
    max_tool_calls: int = 25     # ops-API calls (excludes submit_case_record)
    max_cost_usd: float = 0.20   # soft ceiling; forces escalation if exceeded


@dataclass
class CaseRun:
    case_id: str
    record: dict | None = None
    transcript: list = field(default_factory=list)
    tool_calls_made: int = 0
    turns_used: int = 0
    estimated_cost_usd: float = 0.0
    forced_escalation_reason: str | None = None


# Rough per-1M-token standard pricing, keep in sync with whatever model you actually run.
# Source: OpenAI's published pricing; check before trusting this for a different model.
_PRICE_PER_1M = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
}


def _estimate_cost(model: str, usage) -> float:
    prices = _PRICE_PER_1M.get(model)
    if not prices or usage is None:
        return 0.0
    return (usage.prompt_tokens / 1_000_000) * prices["input"] + \
           (usage.completion_tokens / 1_000_000) * prices["output"]


def _reconcile_entitlement(ops_client: OpsClient, submitted: dict) -> list[str]:
    """Hard guardrail: cross-check every calculate_entitlement result seen this case against
    the amounts the model is about to finalize. Returns a list of problems (empty = clean)."""
    problems = []

    entitlement_calls = [
        c for c in ops_client.call_log
        if c["path"] == "/entitlements/calculate" and c["status"] == 200
    ]
    if not entitlement_calls:
        # No entitlement lookup at all. Only a problem if the record claims a disruption payout.
        for p in submitted.get("passenger_outcomes", []):
            if p.get("outcome") in ("COMPENSATED",) and p.get("amount_gbp"):
                problems.append(
                    f"passenger {p.get('passenger_id')} marked COMPENSATED with amount "
                    f"{p.get('amount_gbp')} but calculate_entitlement was never called this case."
                )
        return problems

    latest = entitlement_calls[-1]["response"]
    by_passenger = {p["passenger_id"]: p for p in latest.get("passengers", [])}

    for p in submitted.get("passenger_outcomes", []):
        pid = p.get("passenger_id")
        if p.get("outcome") != "COMPENSATED":
            continue
        authoritative = by_passenger.get(pid)
        if authoritative is None:
            problems.append(
                f"passenger {pid} marked COMPENSATED but does not appear in the most recent "
                f"calculate_entitlement result for this booking."
            )
            continue
        expected = authoritative.get("total_payable_gbp")
        submitted_amount = p.get("amount_gbp")
        if expected is not None and submitted_amount is not None and \
                round(float(expected), 2) != round(float(submitted_amount), 2):
            problems.append(
                f"passenger {pid}: record shows amount_gbp={submitted_amount} but "
                f"calculate_entitlement's total_payable_gbp is {expected}. Use the "
                f"entitlement service's figure, not your own."
            )
    return problems


# Outcomes that actually address whether/how a passenger travels. VOUCHER_ISSUED and COMPENSATED
# are real actions but don't answer that question on their own -- a passenger can be fully
# compensated and still have nobody have decided what happens to their journey.
_CORE_DISPOSITION_OUTCOMES = {"REBOOKED", "REFUNDED", "ESCALATED", "NO_ACTION_NEEDED"}


def _check_core_disposition(ops_client: OpsClient, submitted: dict) -> list[str]:
    """Hard guardrail: every passenger on the booking (per the booking record actually fetched
    this case, not the model's memory of it) must have at least one passenger_outcomes entry
    whose outcome resolves their journey -- rebooked, refunded, escalated, or explicitly no
    action needed. This exists because a wheelchair-using passenger on a 5-passenger booking was
    given a hotel voucher and a goodwill share, but nobody ever rebooked her, refunded her, or
    escalated her -- and the case still closed as RESOLVED. Duty-of-care items are real and
    worth recording, but they are not a substitute for deciding what happens to someone's trip."""
    problems = []
    booking_ref = submitted.get("booking_ref")
    if not booking_ref:
        return problems

    booking = get_cached_booking(ops_client, booking_ref)
    if booking is None:
        return problems  # can't verify the passenger list without having fetched it; don't block

    outcomes_by_pid: dict[str, set] = {}
    for p in submitted.get("passenger_outcomes", []):
        pid = p.get("passenger_id")
        if pid:
            outcomes_by_pid.setdefault(pid, set()).add(p.get("outcome"))

    for passenger in booking.get("passengers", []):
        pid = passenger.get("passenger_id")
        name = f"{passenger.get('given_name', '')} {passenger.get('surname', '')}".strip()
        pid_outcomes = outcomes_by_pid.get(pid, set())
        if not (pid_outcomes & _CORE_DISPOSITION_OUTCOMES):
            problems.append(
                f"passenger {pid} ({name or 'name unknown'}) has no REBOOKED / REFUNDED / "
                f"ESCALATED / NO_ACTION_NEEDED outcome -- only {sorted(pid_outcomes) or ['none']} "
                f"recorded. A duty-of-care item (voucher, compensation) is not a decision about "
                f"whether or how this passenger travels. Add an outcome that actually resolves "
                f"their journey, or escalate specifically for them if you cannot."
            )
    return problems


def _check_payment_scoping(submitted: dict) -> list[str]:
    """Hard guardrail: pay_goodwill has no passenger_ids field in the ops API -- it is always a
    booking-level action -- so GOODWILL_PAID must never appear as a per-passenger outcome (a
    single real payment was previously recorded as four separate per-passenger payments,
    overstating the actual spend by 3x). The tool schema no longer offers GOODWILL_PAID as an
    outcome at all; this is a runtime backstop in case a model emits it anyway."""
    problems = []
    for p in submitted.get("passenger_outcomes", []):
        if p.get("outcome") == "GOODWILL_PAID":
            problems.append(
                f"passenger {p.get('passenger_id')}: GOODWILL_PAID must not appear in "
                f"passenger_outcomes -- pay_goodwill has no passenger_ids field in the ops API, "
                f"so a goodwill payment is always booking-level, never scoped to one passenger. "
                f"Remove this entry and record the payment once in duty_of_care_actions instead, "
                f"naming which passenger(s) it relates to in the text if relevant."
            )
    return problems


def run_case(case_id: str, inbound_text: str, meta: dict, ops_client: OpsClient,
             openai_client: OpenAI | None = None, model: str = DEFAULT_MODEL,
             budget: CaseBudget | None = None) -> CaseRun:
    openai_client = openai_client or OpenAI()
    budget = budget or CaseBudget()
    run = CaseRun(case_id=case_id)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_case_prompt(case_id, inbound_text, meta)},
    ]

    while True:
        run.turns_used += 1

        if run.turns_used > budget.max_turns or run.estimated_cost_usd > budget.max_cost_usd:
            run.forced_escalation_reason = (
                f"Turn or cost budget exceeded (turns={run.turns_used}, "
                f"cost=${run.estimated_cost_usd:.4f}) before the case was resolved."
            )
            run.record = _forced_escalation_record(case_id, meta, run.forced_escalation_reason,
                                                     ops_client)
            return run

        response = openai_client.chat.completions.create(
            model=model,
            messages=messages,
            tools=ALL_TOOLS,
            tool_choice="auto",
        )
        run.estimated_cost_usd += _estimate_cost(model, getattr(response, "usage", None))
        choice = response.choices[0].message
        run.transcript.append({"role": "assistant", "content": choice.content,
                                "tool_calls": [tc.model_dump() for tc in (choice.tool_calls or [])]})

        if not choice.tool_calls:
            # Model spoke without acting. Nudge it back toward tools/finishing rather than
            # silently ending the case.
            messages.append(choice)
            messages.append({
                "role": "user",
                "content": "Continue working the case with the available tools, or call "
                            "submit_case_record if you are done.",
            })
            continue

        messages.append(choice)

        for tc in choice.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            if name == "submit_case_record":
                problems = _reconcile_entitlement(ops_client, args)
                problems += _check_core_disposition(ops_client, args)
                problems += _check_payment_scoping(args)
                if problems:
                    messages.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": json.dumps({
                            "accepted": False,
                            "problems": problems,
                            "instruction": "Fix these and call submit_case_record again.",
                        }),
                    })
                    run.transcript.append({"role": "tool", "name": name, "rejected": problems})
                    continue
                run.record = args
                run.transcript.append({"role": "tool", "name": name, "accepted": True})
                return run

            if run.tool_calls_made >= budget.max_tool_calls:
                result = {"error": "tool_budget_exceeded",
                          "message": "Tool call budget for this case has been reached. "
                                     "Escalate or finalize with what you have."}
            else:
                result = dispatch(ops_client, name, args)
                run.tool_calls_made += 1

            run.transcript.append({"role": "tool", "name": name, "args": args, "result": result})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})


_NOT_REVIEWED_NOTE = (
    "Action was taken by the agent before the case's turn/cost budget was reached; the case "
    "was force-escalated before a final reviewed record was produced, so this action has NOT "
    "been confirmed correct by a completed pass over the whole case."
)


def _actions_from_call_log(ops_client: OpsClient) -> tuple[list[dict], list[str], str | None]:
    """Reconstructs what was actually done from the ops client's own call log -- used when a
    case is force-escalated before the model itself ever called submit_case_record, so the
    human picking up the case isn't blind to real, permanent, already-executed writes (a
    rebooking, a payment, a voucher) just because no structured record was ever produced for
    them. Returns (passenger_outcomes, duty_of_care_actions, booking_ref_guess)."""
    passenger_outcomes: list[dict] = []
    duty_of_care_actions: list[str] = []
    booking_ref_guess: str | None = None

    for c in ops_client.call_log:
        if c["status"] not in (200, 201):
            continue
        path = c["path"]
        body = c["body"] or {}
        resp = c["response"] or {}

        if path == "/rebooking":
            booking_ref_guess = booking_ref_guess or body.get("booking_ref")
            for pid in body.get("passenger_ids", []) or []:
                passenger_outcomes.append({
                    "passenger_id": pid,
                    "outcome": "REBOOKED",
                    "amount_gbp": body.get("fare_gbp", 0.0),
                    "detail": f"Rebooked onto {body.get('flight_no')} on {body.get('date')} "
                              f"(rebooking_id={resp.get('rebooking_id')}).",
                    "reasoning": _NOT_REVIEWED_NOTE,
                })

        elif path == "/vouchers/hotel":
            booking_ref_guess = booking_ref_guess or body.get("booking_ref")
            for pid in body.get("passenger_ids", []) or []:
                passenger_outcomes.append({
                    "passenger_id": pid,
                    "outcome": "VOUCHER_ISSUED",
                    "amount_gbp": resp.get("rate_gbp", 0.0),
                    "detail": f"Hotel voucher issued for {body.get('station')} on "
                              f"{body.get('night')} (voucher_id={resp.get('voucher_id')}).",
                    "reasoning": _NOT_REVIEWED_NOTE,
                })

        elif path == "/refunds":
            booking_ref_guess = booking_ref_guess or body.get("booking_ref")
            for pid in body.get("passenger_ids", []) or []:
                passenger_outcomes.append({
                    "passenger_id": pid,
                    "outcome": "REFUNDED",
                    "amount_gbp": body.get("amount_gbp"),
                    "detail": f"Refund issued (refund_id={resp.get('refund_id')}).",
                    "reasoning": _NOT_REVIEWED_NOTE,
                })

        elif path == "/payments/compensation":
            booking_ref_guess = booking_ref_guess or body.get("booking_ref")
            pids = body.get("passenger_ids") or []
            if pids:
                for pid in pids:
                    passenger_outcomes.append({
                        "passenger_id": pid,
                        "outcome": "COMPENSATED",
                        "amount_gbp": body.get("amount_gbp"),
                        "detail": f"Compensation paid (payment_id={resp.get('payment_id')}).",
                        "reasoning": _NOT_REVIEWED_NOTE,
                    })
            else:
                duty_of_care_actions.append(
                    f"Compensation of GBP {body.get('amount_gbp')} paid against booking "
                    f"{body.get('booking_ref')} (payment_id={resp.get('payment_id')}) -- "
                    f"specific passenger(s) not recorded in the write itself. {_NOT_REVIEWED_NOTE}"
                )

        elif path == "/payments/goodwill":
            booking_ref_guess = booking_ref_guess or body.get("booking_ref")
            duty_of_care_actions.append(
                f"Goodwill payment of GBP {body.get('amount_gbp')} paid against booking "
                f"{body.get('booking_ref')} (payment_id={resp.get('payment_id')}), reason: "
                f"\"{body.get('reason')}\". {_NOT_REVIEWED_NOTE}"
            )

        elif path == "/rebooking/cancel" or (path.startswith("/rebooking/") and path.endswith("/cancel")):
            duty_of_care_actions.append(
                f"Rebooking {resp.get('rebooking_id', '(unknown)')} was cancelled "
                f"(GBP 65 cancellation fee recorded). {_NOT_REVIEWED_NOTE}"
            )

        elif path == "/escalations":
            duty_of_care_actions.append(
                f"An escalation was already raised by the agent before this case hit its "
                f"budget (escalation_id={resp.get('escalation_id')}, "
                f"queue={body.get('queue', 'GENERAL')}): {body.get('summary')}"
            )

    return passenger_outcomes, duty_of_care_actions, booking_ref_guess


def _forced_escalation_record(case_id: str, meta: dict, reason: str,
                               ops_client: OpsClient) -> dict:
    passenger_outcomes, duty_of_care_actions, booking_ref_guess = \
        _actions_from_call_log(ops_client)

    sources_consulted = sorted({
        f"{c['method']} {c['path']}" for c in ops_client.call_log if c["status"] == 200
    })

    human_follow_up = [
        "Case was not resolved automatically -- a human must review and complete it.",
        reason,
    ]
    if passenger_outcomes or duty_of_care_actions:
        human_follow_up.append(
            f"IMPORTANT: {len(passenger_outcomes)} passenger-level action(s) and "
            f"{len(duty_of_care_actions)} other action(s) were already executed against this "
            f"booking by the agent before it ran out of budget -- see passenger_outcomes and "
            f"duty_of_care_actions below. These are real, permanent, and were NOT reviewed by "
            f"a completed pass over the case. Check them against /_audit and the booking record "
            f"before taking any further action, rather than assuming nothing has happened yet."
        )

    return {
        "case_id": case_id,
        "booking_ref": meta.get("booking_ref") or booking_ref_guess,
        "overall_status": "ESCALATED",
        "passenger_outcomes": passenger_outcomes,
        "duty_of_care_actions": duty_of_care_actions,
        "sources_consulted": sources_consulted,
        "uncertainties": [reason],
        "human_follow_up": human_follow_up,
    }