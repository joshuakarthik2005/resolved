"""Resolved. — the policy a risk team owns.

The five gate checks never change. What a risk owner may tune is the numbers
behind them: the auto-approve ceiling, the daily cap, how wide a time window
counts as the same payment, and which actions are permitted at all.

Those live in policy.json so they can be changed without touching gate.py or
the model. Every change is written to the audit trail by the server.
"""
from __future__ import annotations

import json
import os
from datetime import timedelta

import gate

PATH = os.getenv("RESOLVED_POLICY") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy.json")

DEFAULTS = {
    "auto_limit": 10_000,          # check 2: ₹ ceiling for acting without a human
    "daily_cap": 5,                # check 5: auto-actions per merchant per day
    "match_window_minutes": 10,    # check 1: how close in time counts as the same payment
    "allowed_actions": ["MARK_PAID", "REVERSE_TO_SOURCE", "REFUND_DUPLICATE"],   # check 4
    "settlement_cutoff": "18:00",  # not a gate check: which settlement cycle a credit joins.
                                   # An assumption for the demo, not Paytm's published figure.
}

LABELS = {
    "auto_limit": ("Auto-approve ceiling (₹)",
                   "Above this the agent proposes but a human decides. Check 2."),
    "daily_cap": ("Daily auto-actions per merchant",
                  "A ceiling on how often one merchant can trigger automatic action. Check 5."),
    "match_window_minutes": ("Match window (minutes)",
                             "How far outside the time the shop gives (\u201cjust now\u201d, "
                             "\u201cat 4 pm\u201d, \u201cyesterday\u201d) a payment can be and "
                             "still count. Wider finds more, but risks the wrong match. With no "
                             "time given, the last 7 days are searched. Check 1."),
}


def load() -> dict:
    data = dict(DEFAULTS)
    if os.path.exists(PATH):
        try:
            with open(PATH, encoding="utf-8") as fh:
                data.update(json.load(fh))
        except (json.JSONDecodeError, OSError):
            pass                                   # a broken file must not stop the demo
    return data


def save(data: dict) -> dict:
    merged = load() | {k: v for k, v in data.items() if k in DEFAULTS}
    with open(PATH, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
    apply(merged)
    return merged


def apply(data: dict | None = None) -> dict:
    """Push the policy into gate.py's module constants.

    gate.py keeps its logic exactly as it was proven; only the numbers move.
    """
    data = data or load()
    gate.AUTO_LIMIT = int(data["auto_limit"])
    gate.DAILY_CAP = int(data["daily_cap"])
    gate.MATCH_WINDOW = timedelta(minutes=int(data["match_window_minutes"]))
    # an action added to the code after a policy.json was written is still listed
    gate.ALLOWED_ACTIONS = set(data["allowed_actions"]) | (
        {"REFUND_DUPLICATE"} if "REVERSE_TO_SOURCE" in data["allowed_actions"] else set())
    return data


def describe() -> list[dict]:
    """What the policy editor shows: current value, label, and why it exists."""
    cur = load()
    out = []
    for key, (label, why) in LABELS.items():
        out.append({"key": key, "label": label, "why": why,
                    "value": cur[key], "default": DEFAULTS[key]})
    return out


apply()
