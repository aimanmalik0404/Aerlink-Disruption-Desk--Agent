"""OpenAI tool schemas + the ops-API client that backs them.

The model only ever sees the schemas in ALL_TOOLS and the JSON results dispatch() returns.
Retry/backoff for the flaky availability endpoint lives entirely in this file, not in the prompt
and not in the model's control -- it calls search_availability once, we retry underneath it.
"""

from __future__ import annotations

import os
import time
import json
import requests

OPS_BASE_URL = os.environ.get("OPS_BASE_URL", "http://127.0.0.1:8642")
OPS_API_KEY = os.environ.get("OPS_API_KEY", "aerlink-ops-local-key")


class OpsClient:
    def __init__(self, base_url: str = OPS_BASE_URL, api_key: str = OPS_API_KEY):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-Ops-Key": api_key})
        self.call_log: list[dict] = []  # every request/response, for the transcript + guardrail

    # -- low-level -----------------------------------------------------------

    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        resp = self.session.request(method, url, params=params, json=body, timeout=15)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": "invalid_response", "message": resp.text[:500]}
        entry = {
            "method": method, "path": path, "params": params, "body": body,
            "status": resp.status_code, "response": payload,
        }
        self.call_log.append(entry)
        return {"_http_status": resp.status_code, **payload}

    # -- reads -----------------------------------------------------------------

    def search_bookings(self, q: str) -> dict:
        return self._request("GET", "/bookings/search", params={"q": q})

    def get_booking(self, booking_ref: str) -> dict:
        return self._request("GET", f"/bookings/{booking_ref}")

    def get_flight(self, flight_no: str, date: str) -> dict:
        return self._request("GET", f"/flights/{flight_no}", params={"date": date})

    def search_availability(self, from_: str, to: str, date: str, booking_ref: str,
                             after: str | None = None, partners: bool = False,
                             page: int = 1, page_size: int = 20) -> dict:
        """Wraps GET /flights/availability[/partners] with three hard-coded, non-negotiable
        behaviours -- moved into code after the model failed to reliably follow the equivalent
        prose instructions across repeated runs:

        1. Retry on the documented flaky-503 pattern (a fresh (from,to,date,scope) query always
           fails on its first attempt, then roughly one in seven thereafter).
        2. booking_ref is now REQUIRED (not optional). It's used to fetch
           arrival_delay_vs_original_minutes per option and to hard-filter out any option that
           would arrive no later than the passenger's original scheduled arrival -- i.e. an
           option the passenger cannot realistically still catch.
        3. Options with seats_available <= 0 are filtered out. The mock server never re-validates
           seat count at /rebooking -- it will happily confirm a booking against a sold-out
           option -- so this has to be enforced here or nowhere.

        Remaining options are sorted soonest-arrival-first, so "pick the top result" is already
        the right answer without the model reasoning about raw clock times or seat counts.
        """
        path = "/flights/availability/partners" if partners else "/flights/availability"
        params = {"from": from_, "to": to, "date": date, "page": page, "page_size": page_size,
                   "booking_ref": booking_ref}
        if after:
            params["after"] = after

        last = None
        result = None
        for attempt in range(4):  # covers the guaranteed-fail-once pattern plus margin
            result = self._request("GET", path, params=params)
            if result.get("_http_status") != 503:
                break
            last = result
            time.sleep(0.6 * (attempt + 1))
        else:
            return last

        rows = result.get("results", [])
        has_baseline = any("arrival_delay_vs_original_minutes" in r for r in rows)

        kept = [r for r in rows if r.get("seats_available", 0) > 0]
        excluded_sold_out = len(rows) - len(kept)

        excluded_early = 0
        if has_baseline:
            before = len(kept)
            kept = [r for r in kept if r.get("arrival_delay_vs_original_minutes", -1) >= 0]
            excluded_early = before - len(kept)
            kept.sort(key=lambda r: r["arrival_delay_vs_original_minutes"])

        result["results"] = kept

        notes = []
        if excluded_sold_out:
            notes.append(f"{excluded_sold_out} option(s) excluded with no seats available.")
        if excluded_early:
            notes.append(
                f"{excluded_early} option(s) excluded because they would arrive no later than "
                f"the passenger's original scheduled arrival -- these have already effectively "
                f"departed and are not real options."
            )
        if has_baseline:
            notes.append("Remaining results are sorted soonest-arrival-first.")
        else:
            notes.append(
                "booking_ref was not recognised, so no baseline arrival time was available -- "
                "remaining results are unsorted. Verify booking_ref is correct before trusting "
                "any option here for a rebooking."
            )
        if not kept and result.get("total_pages", 1) > page:
            notes.append(
                f"This page had no usable options after filtering, but "
                f"{result.get('total_pages')} pages exist in total -- try page={page + 1} "
                f"before concluding there is no own-carrier availability."
            )
        result["_filtering_note"] = " ".join(notes)
        return result

    def policy_search(self, q: str, limit: int = 5) -> dict:
        return self._request("GET", "/policy/search", params={"q": q, "limit": limit})

    def policy_document(self) -> dict:
        return self._request("GET", "/policy/document")

    def get_customer_history(self, customer_id: str) -> dict:
        return self._request("GET", f"/customers/{customer_id}/history")

    def calculate_entitlement(self, booking_ref: str, passenger_id: str | None = None) -> dict:
        params = {"booking_ref": booking_ref}
        if passenger_id:
            params["passenger_id"] = passenger_id
        return self._request("GET", "/entitlements/calculate", params=params)

    def get_hotel_allocation(self, station: str, night: str) -> dict:
        return self._request("GET", f"/stations/{station}/hotel-allocation", params={"night": night})

    def get_disruption_feed(self) -> dict:
        return self._request("GET", "/disruption/feed")

    def reset(self) -> dict:
        return self._request("POST", "/_reset")

    def get_audit(self) -> dict:
        return self._request("GET", "/_audit")

    # -- writes ------------------------------------------------------------

    def create_rebooking(self, booking_ref: str, passenger_ids: list[str], option_id: str,
                          flight_no: str, date: str, cabin: str | None = None,
                          fare_gbp: float | None = None, notes: str | None = None) -> dict:
        body = {"booking_ref": booking_ref, "passenger_ids": passenger_ids,
                "option_id": option_id, "flight_no": flight_no, "date": date}
        if cabin is not None:
            body["cabin"] = cabin
        if fare_gbp is not None:
            body["fare_gbp"] = fare_gbp
        if notes is not None:
            body["notes"] = notes
        return self._request("POST", "/rebooking", body=body)

    def cancel_rebooking(self, rebooking_id: str) -> dict:
        return self._request("POST", f"/rebooking/{rebooking_id}/cancel")

    def issue_hotel_voucher(self, booking_ref: str, station: str, night: str,
                             passenger_ids: list[str], notes: str | None = None) -> dict:
        body = {"booking_ref": booking_ref, "station": station, "night": night,
                "passenger_ids": passenger_ids}
        if notes is not None:
            body["notes"] = notes
        return self._request("POST", "/vouchers/hotel", body=body)

    def pay_compensation(self, booking_ref: str, amount_gbp: float,
                          passenger_ids: list[str] | None = None,
                          reason: str | None = None) -> dict:
        body = {"booking_ref": booking_ref, "amount_gbp": amount_gbp}
        if passenger_ids is not None:
            body["passenger_ids"] = passenger_ids
        if reason is not None:
            body["reason"] = reason
        return self._request("POST", "/payments/compensation", body=body)

    def pay_goodwill(self, booking_ref: str, amount_gbp: float, reason: str) -> dict:
        body = {"booking_ref": booking_ref, "amount_gbp": amount_gbp, "reason": reason}
        return self._request("POST", "/payments/goodwill", body=body)

    def issue_refund(self, booking_ref: str, passenger_ids: list[str], amount_gbp: float,
                      reason: str | None = None) -> dict:
        body = {"booking_ref": booking_ref, "passenger_ids": passenger_ids, "amount_gbp": amount_gbp}
        if reason is not None:
            body["reason"] = reason
        return self._request("POST", "/refunds", body=body)

    def escalate_to_human(self, summary: str, requested_decision: str,
                           booking_ref: str | None = None, queue: str | None = None,
                           recommendation: str | None = None,
                           blocking_clause: str | None = None) -> dict:
        body = {"summary": summary, "requested_decision": requested_decision}
        for k, v in (("booking_ref", booking_ref), ("queue", queue),
                     ("recommendation", recommendation), ("blocking_clause", blocking_clause)):
            if v is not None:
                body[k] = v
        return self._request("POST", "/escalations", body=body)


# --------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# --------------------------------------------------------------------------

def _tool(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


READ_TOOLS = [
    _tool("search_bookings",
          "Search the booking record by reference, email, phone, or passenger name/surname. "
          "Use this when you don't already have a booking_ref stated outright in the message.",
          {"q": {"type": "string", "description": "Reference, email, phone, or passenger name."}},
          ["q"]),

    _tool("get_booking",
          "Fetch the full booking record: passengers, segments, disruption info, special "
          "requests, fare. Always fetch this before acting on a booking mentioned by reference.",
          {"booking_ref": {"type": "string"}}, ["booking_ref"]),

    _tool("get_flight",
          "Fetch the operational record for one flight on one date: status, cause_code, delay "
          "minutes. This is ground truth about what happened -- use it instead of trusting the "
          "passenger's description of the disruption.",
          {"flight_no": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"}},
          ["flight_no", "date"]),

    _tool("search_availability",
          "Search seat availability for a route and date, own-carrier by default. booking_ref "
          "is required -- it's used to filter out options the passenger cannot realistically "
          "catch and to sort remaining options soonest-arrival-first, so the first result is "
          "already the right pick unless the passenger has stated another preference (e.g. "
          "cost, a specific cabin). Retries on transient failures are handled for you -- if you "
          "get a result (even empty), it is final; do not repeat the identical call.",
          {
              "from_": {"type": "string", "description": "Origin IATA code."},
              "to": {"type": "string", "description": "Destination IATA code."},
              "date": {"type": "string", "description": "YYYY-MM-DD"},
              "booking_ref": {"type": "string", "description": "Required. The booking being re-routed; used to filter/sort by arrival_delay_vs_original_minutes."},
              "after": {"type": "string", "description": "Optional HH:MM, only departures at or after this local time."},
              "partners": {"type": "boolean", "description": "Search partner-carrier inventory instead of Aerlink's own. Try own-carrier first unless it's known to have none."},
              "page": {"type": "integer"},
              "page_size": {"type": "integer"},
          },
          ["from_", "to", "date", "booking_ref"]),

    _tool("policy_search",
          "Keyword search over the Passenger Care Policy for qualitative rules (assistance, "
          "YTP, discretionary authority, duty-of-care detail). Matching is literal with no "
          "synonyms -- try several distinct keyword phrasings before concluding nothing is "
          "there. Never use this to derive a compensation figure; use calculate_entitlement.",
          {"q": {"type": "string"}, "limit": {"type": "integer"}}, ["q"]),

    _tool("policy_document",
          "Fetch the complete Passenger Care Policy text. Expensive -- use only when "
          "policy_search across several rewordings has failed to surface something you need.",
          {}, []),

    _tool("get_customer_history",
          "Fetch a customer's record and case history, including any flags.",
          {"customer_id": {"type": "string"}}, ["customer_id"]),

    _tool("calculate_entitlement",
          "THE sole source of truth for compensation, downgrade reimbursement, and duty-of-care "
          "triggers. Always call this for any disruption case before recording or paying any "
          "amount. Never compute these figures yourself.",
          {"booking_ref": {"type": "string"},
           "passenger_id": {"type": "string", "description": "Optional, restrict to one passenger."}},
          ["booking_ref"]),

    _tool("get_hotel_allocation",
          "Rooms remaining in Aerlink's hotel allocation at a station for one night. Check "
          "before promising or issuing a hotel voucher.",
          {"station": {"type": "string"}, "night": {"type": "string", "description": "YYYY-MM-DD"}},
          ["station", "night"]),

    _tool("get_disruption_feed",
          "Network-wide operations feed for the current window. Rarely needed for a single "
          "case; useful for corroborating a widescale event mentioned in a message.",
          {}, []),
]

WRITE_TOOLS = [
    _tool("create_rebooking",
          "Confirms new seats and issues a new itinerary. Real action: spends inventory and "
          "changes the passenger's journey. Requires option_id from a prior search_availability "
          "call for the same route/date.",
          {
              "booking_ref": {"type": "string"},
              "passenger_ids": {"type": "array", "items": {"type": "string"}},
              "option_id": {"type": "string"},
              "flight_no": {"type": "string"},
              "date": {"type": "string"},
              "cabin": {"type": "string"},
              "fare_gbp": {"type": "number"},
              "notes": {"type": "string"},
          },
          ["booking_ref", "passenger_ids", "option_id", "flight_no", "date"]),

    _tool("cancel_rebooking",
          "Cancels a rebooking made via create_rebooking. Incurs a GBP 65 fee and does not "
          "restore released inventory -- only use this to correct a genuine mistake.",
          {"rebooking_id": {"type": "string"}}, ["rebooking_id"]),

    _tool("issue_hotel_voucher",
          "Issues a hotel voucher against station allocation. Real action: spends money and a "
          "finite room allocation. Check get_hotel_allocation first if availability is in doubt.",
          {
              "booking_ref": {"type": "string"}, "station": {"type": "string"},
              "night": {"type": "string"},
              "passenger_ids": {"type": "array", "items": {"type": "string"}},
              "notes": {"type": "string"},
          },
          ["booking_ref", "station", "night", "passenger_ids"]),

    _tool("pay_compensation",
          "Pays statutory compensation. Real action: moves money. amount_gbp must trace back to "
          "a calculate_entitlement result for this booking.",
          {
              "booking_ref": {"type": "string"}, "amount_gbp": {"type": "number"},
              "passenger_ids": {"type": "array", "items": {"type": "string"}},
              "reason": {"type": "string"},
          },
          ["booking_ref", "amount_gbp"]),

    _tool("pay_goodwill",
          "Pays a discretionary goodwill amount not covered by statutory compensation (e.g. an "
          "unattended duty-of-care failure). Real action: moves money. reason is required.",
          {"booking_ref": {"type": "string"}, "amount_gbp": {"type": "number"}, "reason": {"type": "string"}},
          ["booking_ref", "amount_gbp", "reason"]),

    _tool("issue_refund",
          "Refunds all or part of a booking. Real action: moves money.",
          {
              "booking_ref": {"type": "string"},
              "passenger_ids": {"type": "array", "items": {"type": "string"}},
              "amount_gbp": {"type": "number"}, "reason": {"type": "string"},
          },
          ["booking_ref", "passenger_ids", "amount_gbp"]),

    _tool("escalate_to_human",
          "Hands a decision to a human team. Use for anything requiring authority, judgment "
          "about a vulnerable/dependent passenger's ambiguous instruction, YTP cases, or "
          "anything you can't resolve confidently.",
          {
              "summary": {"type": "string"},
              "requested_decision": {"type": "string", "description": "The specific thing a human needs to decide, not a restatement of the problem."},
              "booking_ref": {"type": "string"},
              "queue": {"type": "string", "description": "e.g. SUPERVISOR, YTP, SPECIAL_ASSISTANCE, OPS_LIAISON, CUSTOMER_CONDUCT, LOST_PROPERTY, GENERAL"},
              "recommendation": {"type": "string"},
              "blocking_clause": {"type": "string"},
          },
          ["summary", "requested_decision"]),
]

# The finalize tool. Not an ops-API call -- intercepted by the agent loop.
SUBMIT_TOOL = _tool(
    "submit_case_record",
    "Finalize this case. Call this exactly once, when and only when the case is fully worked "
    "(resolved, partially resolved with the remainder escalated, or escalated in full). This is "
    "the only way to end a case.",
    {
        "case_id": {"type": "string"},
        "booking_ref": {"type": "string"},
        "overall_status": {"type": "string", "enum": ["RESOLVED", "ESCALATED", "MIXED", "NO_ACTION_NEEDED"]},
        "passenger_outcomes": {
            "type": "array",
            "description": "One entry per passenger whose situation was distinctly addressed.",
            "items": {
                "type": "object",
                "properties": {
                    "passenger_id": {"type": "string"},
                    "name": {"type": "string"},
                    "outcome": {"type": "string", "enum": [
                        "REBOOKED", "REFUNDED", "COMPENSATED", "VOUCHER_ISSUED",
                        "ESCALATED", "NO_ACTION_NEEDED"]},
                    "amount_gbp": {"type": "number"},
                    "detail": {"type": "string"},
                    "reasoning": {"type": "string", "description": "Cite the specific policy sections / entitlement result this rests on."},
                },
                "required": ["passenger_id", "outcome", "detail", "reasoning"],
                "additionalProperties": False,
            },
        },
        "duty_of_care_actions": {
            "type": "array", "items": {"type": "string"},
            "description": "Any care actions (vouchers, etc.) not already captured per-passenger.",
        },
        "sources_consulted": {
            "type": "array", "items": {"type": "string"},
            "description": "Which tools/endpoints were consulted and why, in brief.",
        },
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "human_follow_up": {
            "type": "array", "items": {"type": "string"},
            "description": "What a human still needs to do, if anything. Empty list if none.",
        },
        "passenger_reply_draft": {
            "type": "string",
            "description": "Optional draft reply to the passenger, in the language they wrote in.",
        },
    },
    ["case_id", "overall_status", "passenger_outcomes", "sources_consulted",
     "uncertainties", "human_follow_up"],
)

ALL_TOOLS = READ_TOOLS + WRITE_TOOLS + [SUBMIT_TOOL]


def _hotel_voucher_guard(client: OpsClient, args: dict) -> dict | None:
    """Blocks issue_hotel_voucher calls that would double-book a passenger for a night they
    already have, exceed the entitlement service's own max_nights cap *for that passenger*, or
    voucher a passenger who already has a confirmed rebooking departing on or before the
    requested night (a passenger already flying out that day or earlier doesn't need overnight
    accommodation starting that night or any later one). Returns an error dict to short-circuit
    the call, or None to let it through.

    Tracking is per (passenger_id, night), not per (booking_ref, night) -- a booking can carry
    several passengers who each legitimately need a room on the same night (e.g. a family
    travelling together), and blocking on night alone would incorrectly refuse the second and
    subsequent passengers' vouchers for a night nobody has actually double-covered."""
    booking_ref = args.get("booking_ref")
    night = args.get("night")
    requested_ids = set(args.get("passenger_ids") or [])

    rebooking_calls = [
        c for c in client.call_log
        if c["path"] == "/rebooking" and c["status"] == 201
        and c["body"].get("booking_ref") == booking_ref
    ]
    already_travelling = set()
    for c in rebooking_calls:
        rb_date = c["body"].get("date")
        rb_pids = set(c["body"].get("passenger_ids") or [])
        if rb_date and night and rb_date <= night:  # ISO YYYY-MM-DD strings sort correctly
            already_travelling |= (rb_pids & requested_ids)

    if already_travelling:
        return {
            "error": "passenger_already_travelling",
            "message": f"Passenger(s) {sorted(already_travelling)} already have a confirmed "
                       f"rebooking departing on or before {night} -- they don't need overnight "
                       f"accommodation starting that night or any later night. Do not issue a "
                       f"hotel voucher for them for this night. Passengers not in this list, if "
                       f"any, are not affected and can still be voucher'd for this night in a "
                       f"separate call.",
        }

    entitlement_calls = [
        c for c in client.call_log
        if c["path"] == "/entitlements/calculate" and c["status"] == 200
        and c["response"].get("booking_ref") == booking_ref
    ]
    if not entitlement_calls:
        return {
            "error": "entitlement_not_checked",
            "message": "calculate_entitlement has not been called for this booking_ref yet. "
                       "Call it first to establish the hotel entitlement (if any) before "
                       "issuing a voucher.",
        }

    hotel_entitlement = next(
        (e for e in entitlement_calls[-1]["response"].get("duty_of_care", {}).get("entitlements", [])
         if e.get("type") == "hotel"),
        None,
    )
    if hotel_entitlement is None:
        return {
            "error": "no_hotel_entitlement",
            "message": "The most recent calculate_entitlement result for this booking does not "
                       "include a hotel entitlement. Do not issue a hotel voucher without one.",
        }
    max_nights = hotel_entitlement.get("max_nights", 3)

    prior_vouchers = [
        c for c in client.call_log
        if c["path"] == "/vouchers/hotel" and c["status"] == 201
        and c["body"].get("booking_ref") == booking_ref
    ]

    nights_by_passenger: dict[str, set] = {}
    for c in prior_vouchers:
        v_night = c["body"].get("night")
        for pid in c["body"].get("passenger_ids") or []:
            nights_by_passenger.setdefault(pid, set()).add(v_night)

    already_covered = {pid for pid in requested_ids if night in nights_by_passenger.get(pid, set())}
    if already_covered:
        return {
            "error": "night_already_covered",
            "message": f"Passenger(s) {sorted(already_covered)} already have a hotel voucher "
                       f"for {night}. A passenger needs one room per night, not one per "
                       f"station -- do not issue another voucher for the same passenger and "
                       f"night. Passengers not in this list, if any, are not affected and can "
                       f"still be voucher'd for this night in a separate call.",
        }

    at_cap = {pid for pid in requested_ids
              if len(nights_by_passenger.get(pid, set())) >= max_nights}
    if at_cap:
        return {
            "error": "hotel_night_cap_reached",
            "message": f"Passenger(s) {sorted(at_cap)} already have {max_nights} night(s) "
                       f"covered, which is this booking's hotel entitlement cap per passenger. "
                       f"Do not issue further hotel vouchers for these passengers -- escalate "
                       f"to a human if you believe more nights are genuinely needed for them. "
                       f"Passengers not in this list, if any, are not affected.",
        }
    return None


def _goodwill_guard(client: OpsClient, args: dict) -> dict | None:
    """Blocks a pay_goodwill call whose amount is grounded in nothing checkable. A model left to
    size a discretionary goodwill payment on its own paid GBP 64.69 for "expenses incurred while
    waiting" with no cap cited and no policy lookup this case -- and that exact figure happened
    to match an unrelated flight's fare from earlier in the same conversation, suggesting the
    number came from stray context rather than reasoning. This makes an ungrounded amount
    structurally blocked rather than merely discouraged in the prompt.

    "Grounded" means either: (a) the amount matches a single duty-of-care cap, or a sum of a
    small number of them, from the latest calculate_entitlement result for this booking, or
    (b) policy_search or policy_document was consulted at some point this case (a proxy for "the
    model actually looked up goodwill authority" -- we don't hardcode specific policy limits
    here since we don't want to assume numbers we haven't verified against the real policy text).
    """
    booking_ref = args.get("booking_ref")
    amount = args.get("amount_gbp")
    if amount is None:
        return None  # malformed call; let the server's own validation reject it

    policy_consulted = any(
        c["path"] in ("/policy/search", "/policy/document") and c["status"] == 200
        for c in client.call_log
    )

    entitlement_calls = [
        c for c in client.call_log
        if c["path"] == "/entitlements/calculate" and c["status"] == 200
        and c["response"].get("booking_ref") == booking_ref
    ]
    caps = []
    if entitlement_calls:
        for e in entitlement_calls[-1]["response"].get("duty_of_care", {}).get("entitlements", []):
            if "cap_gbp" in e:
                caps.append(float(e["cap_gbp"]))

    def _traceable(target: float) -> bool:
        if not caps:
            return False
        from itertools import combinations_with_replacement
        for r in range(1, 5):  # bounded: a handful of cap-periods is plenty for any real case
            for combo in combinations_with_replacement(caps, r):
                if abs(sum(combo) - target) < 0.01:
                    return True
        return False

    if policy_consulted or _traceable(float(amount)):
        return None

    cap_list = ", ".join(f"GBP {c:.2f}" for c in caps) if caps else "(no duty-of-care caps found)"
    return {
        "error": "goodwill_amount_not_grounded",
        "message": f"GBP {amount} does not match any duty-of-care cap or simple sum of caps "
                   f"from calculate_entitlement for this booking ({cap_list}), and "
                   f"policy_search/policy_document has not been consulted this case for "
                   f"discretionary goodwill authority. Either call policy_search for "
                   f"\"goodwill authority\" first, or size the amount against a specific cap "
                   f"above and say so, then retry.",
    }


def get_cached_booking(client: OpsClient, booking_ref: str) -> dict | None:
    """Returns the most recent full get_booking response for this booking_ref already seen this
    case, or None if get_booking hasn't been called for it yet. Shared by _refund_guard (to find
    real fare figures) and agent_loop's core-disposition check (to find the full passenger list)
    -- both need "what does the booking record actually say", not what the model claims it says."""
    calls = [
        c for c in client.call_log
        if c["method"] == "GET" and c["status"] == 200
        and c["path"].startswith("/bookings/") and c["path"] != "/bookings/search"
        and not c["path"].endswith("/history")
        and c["response"].get("booking_ref", "").upper() == (booking_ref or "").upper()
    ]
    return calls[-1]["response"] if calls else None


# Keywords suggesting a goodwill payment is standing in for undelivered hotel accommodation
# specifically (as opposed to meals, transport, or a general apology) -- used only to decide
# whether _goodwill_hotel_substitute_check applies at all, not to size or validate the amount.
_HOTEL_SUBSTITUTE_KEYWORDS = ("hotel", "accommodation", "overnight", "room", "stay")


def _goodwill_hotel_substitute_check(client: OpsClient, args: dict) -> dict | None:
    """Partial, heuristic guard -- NOT as airtight as _hotel_voucher_guard's equivalent check,
    and deliberately conservative about when it blocks. pay_goodwill has no passenger_ids and no
    night field, so there is no structured way to know which passenger or which night a given
    payment is meant to cover; this infers intent from the reason text instead, which is weaker.

    It only blocks when BOTH: (a) the reason text suggests this payment substitutes for hotel
    accommodation specifically, and (b) EVERY passenger on the booking already has a confirmed
    rebooking departing on or before the disruption date -- i.e. nobody on the booking is left
    without a same-day journey, so no one plausibly still needs overnight accommodation. If only
    some passengers are covered (a mixed group, e.g. most of a family rebooked same-day but one
    passenger's journey still unresolved), this deliberately does NOT block, because the payment
    could still be legitimate for whoever isn't yet covered -- pay_goodwill's lack of passenger
    scoping means we cannot tell. That gap is real and is not closed by this check; it relies on
    the system prompt's instruction to reason about actual nights owed before paying, same as it
    did before this guard existed.
    """
    booking_ref = args.get("booking_ref")
    reason = (args.get("reason") or "").lower()
    if not any(k in reason for k in _HOTEL_SUBSTITUTE_KEYWORDS):
        return None

    booking = get_cached_booking(client, booking_ref)
    if booking is None:
        return None
    all_pids = {p.get("passenger_id") for p in booking.get("passengers", [])}
    if not all_pids:
        return None

    entitlement_calls = [
        c for c in client.call_log
        if c["path"] == "/entitlements/calculate" and c["status"] == 200
        and c["response"].get("booking_ref") == booking_ref
    ]
    disruption_date = None
    if entitlement_calls:
        affected = entitlement_calls[-1]["response"].get("journey", {}).get("affected_flight", "")
        if ":" in affected:
            disruption_date = affected.split(":", 1)[1]
    if not disruption_date:
        return None  # can't establish the baseline date; don't block on incomplete information

    rebooking_calls = [
        c for c in client.call_log
        if c["path"] == "/rebooking" and c["status"] == 201
        and c["body"].get("booking_ref") == booking_ref
    ]
    already_travelling = set()
    for c in rebooking_calls:
        rb_date = c["body"].get("date")
        if rb_date and rb_date <= disruption_date:
            already_travelling |= set(c["body"].get("passenger_ids") or [])

    if all_pids <= already_travelling:
        return {
            "error": "hotel_substitute_not_needed",
            "message": f"Every passenger on this booking already has a confirmed rebooking "
                       f"departing on or before the disruption date ({disruption_date}), so "
                       f"nobody appears to be without a same-day journey. A goodwill payment "
                       f"described as a hotel/accommodation substitute does not look needed. If "
                       f"this is genuinely for something else, reword the reason so it doesn't "
                       f"read as a hotel substitute; otherwise reconsider whether this payment "
                       f"should be made at all.",
        }
    return None


def _refund_guard(client: OpsClient, args: dict) -> dict | None:
    """Blocks a refund whose amount isn't traceable to the booking's own fare data. A model left
    to size a refund on its own paid GBP 0 three times over for a passenger who genuinely wanted
    off the booking, with its own summary admitting the figure was a placeholder. This makes an
    ungrounded refund (most dangerously, a silent GBP 0 that shortchanges a real passenger)
    structurally blocked rather than merely discouraged in the prompt.

    "Grounded" means the amount matches, within a cent: the booking's total_paid_gbp; an equal
    share of total_paid_gbp across all passengers on the booking (times the number of passengers
    in this call); or a segment's segment_fare_gbp (likewise, times the number of passengers in
    this call) -- segment_fare_gbp is used per-passenger elsewhere in this API (see the
    entitlement service's downgrade calculation), so treating it as a per-passenger figure here
    is consistent with how the rest of the system already treats it. This is a reasonable set of
    candidates, not a guarantee of correctness for every possible fare structure (e.g. a
    booking with genuinely different fares per passenger on the same segment would need manual
    judgement) -- it exists to catch the specific, observed failure of an unexplained figure,
    most importantly an unexplained GBP 0.
    """
    booking_ref = args.get("booking_ref")
    amount = args.get("amount_gbp")
    passenger_ids = args.get("passenger_ids") or []
    n_this_call = len(passenger_ids) or 1

    booking = get_cached_booking(client, booking_ref)
    if booking is None:
        return {
            "error": "booking_not_checked",
            "message": "get_booking has not been called for this booking_ref yet this case. "
                       "Call it first to see the booking's real fare data before issuing a "
                       "refund.",
        }

    total_paid = booking.get("total_paid_gbp")
    segments = booking.get("segments", [])
    n_passengers_total = len(booking.get("passengers", [])) or 1

    candidates = set()
    if total_paid is not None:
        candidates.add(round(float(total_paid), 2))
        candidates.add(round(float(total_paid) / n_passengers_total * n_this_call, 2))
    for seg in segments:
        fare = seg.get("segment_fare_gbp")
        if fare is not None:
            candidates.add(round(float(fare), 2))
            candidates.add(round(float(fare) * n_this_call, 2))

    if amount is None:
        traceable = False
    else:
        traceable = any(abs(float(amount) - c) < 0.01 for c in candidates)

    # A genuine GBP 0 is only acceptable if it's actually among the traceable candidates
    # (e.g. a segment or booking that really did cost nothing) -- not as an unexplained default.
    if not traceable:
        candidate_list = ", ".join(f"GBP {c:.2f}" for c in sorted(candidates)) or "(none found)"
        return {
            "error": "refund_amount_not_grounded",
            "message": f"GBP {amount} does not match any fare figure derivable from this "
                       f"booking's own record for {n_this_call} passenger(s): {candidate_list}. "
                       f"Size the refund from total_paid_gbp or the affected segment's "
                       f"segment_fare_gbp, apportioned across the passengers in this call, and "
                       f"say which figure you used in the outcome's reasoning. If you genuinely "
                       f"cannot determine a fair amount from the booking record, escalate this "
                       f"passenger to a human instead of guessing or defaulting to zero.",
        }
    return None


def dispatch(client: OpsClient, name: str, args: dict) -> dict:
    """Executes every tool except submit_case_record, which the agent loop intercepts directly."""
    if name == "issue_hotel_voucher":
        blocked = _hotel_voucher_guard(client, args)
        if blocked is not None:
            return blocked
    if name == "pay_goodwill":
        blocked = _goodwill_guard(client, args)
        if blocked is not None:
            return blocked
        blocked = _goodwill_hotel_substitute_check(client, args)
        if blocked is not None:
            return blocked
    if name == "issue_refund":
        blocked = _refund_guard(client, args)
        if blocked is not None:
            return blocked

    mapping = {
        "search_bookings": client.search_bookings,
        "get_booking": client.get_booking,
        "get_flight": client.get_flight,
        "search_availability": lambda **kw: client.search_availability(
            from_=kw.pop("from_"), **kw),
        "policy_search": client.policy_search,
        "policy_document": client.policy_document,
        "get_customer_history": client.get_customer_history,
        "calculate_entitlement": client.calculate_entitlement,
        "get_hotel_allocation": client.get_hotel_allocation,
        "get_disruption_feed": client.get_disruption_feed,
        "create_rebooking": client.create_rebooking,
        "cancel_rebooking": client.cancel_rebooking,
        "issue_hotel_voucher": client.issue_hotel_voucher,
        "pay_compensation": client.pay_compensation,
        "pay_goodwill": client.pay_goodwill,
        "issue_refund": client.issue_refund,
        "escalate_to_human": client.escalate_to_human,
    }
    fn = mapping.get(name)
    if fn is None:
        return {"error": "unknown_tool", "message": f"No such tool: {name}"}
    try:
        return fn(**args)
    except TypeError as exc:
        return {"error": "bad_arguments", "message": str(exc)}