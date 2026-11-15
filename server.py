"""Resolved. — the running app behind the demo.

    python seed.py        once, to build resolved.db
    python server.py      then open http://127.0.0.1:8000

Three pieces:
  * mock Paytm systems   - ledger, bank status, settlement, reversal, CRM
  * the agent pipeline   - understand, investigate, propose, GATE, act
  * three pages          - merchant chat, case desk, judge mode

The one rule this file is built around: money moves in exactly one function,
_execute(), and it only accepts a single-use ruling minted by _rule(), which
is the only place gate.gate() is called. tests.py checks that structurally.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from contextvars import ContextVar
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import evaluate
import gate
import lang as langs
import policy

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.getenv("RESOLVED_DB") or os.path.join(HERE, "resolved.db")   # tests point this elsewhere
STATIC = os.path.join(HERE, "static")
_DB_PATH: ContextVar[str] = ContextVar("db_path", default=DB)   # judge-mode sandboxes swap it

app = FastAPI(title="Resolved.")


@app.exception_handler(Exception)
async def _any_error(request: Request, exc: Exception):
    """A crash must still answer in JSON, so the page can say what went wrong."""
    print(f"[error] {request.url.path}: {exc!r}")
    return JSONResponse({"detail": "Something went wrong on our side. Nothing was changed. "
                                   f"({type(exc).__name__})"}, status_code=500)


# ----------------------------------------------------------------- database
def db() -> sqlite3.Connection:
    # autocommit mode; every write path opens its own BEGIN IMMEDIATE so two requests
    # cannot both read the same daily count or the same unsettled payment.
    con = sqlite3.connect(_DB_PATH.get(), timeout=15, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def migrate(path: str = DB) -> None:
    """Bring an older resolved.db up to date instead of failing on stage."""
    if not os.path.exists(path):
        return
    with closing(sqlite3.connect(path)) as con:
        def cols(t):
            return {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
        if "auto_date" not in cols("merchants"):
            con.execute("ALTER TABLE merchants ADD COLUMN auto_date TEXT")
        if "npci_code" not in cols("ledger"):
            con.execute("ALTER TABLE ledger ADD COLUMN npci_code TEXT NOT NULL DEFAULT '00'")
            con.execute("UPDATE ledger SET npci_code = CASE bank_status WHEN 'SUCCESS_LATE'"
                        " THEN 'BT' WHEN 'FAILED_DEBITED' THEN 'YD' ELSE '00' END")
        # U31 means "invalid UPI ID" (fails before any debit); the first rewrite misused it
        con.execute("UPDATE ledger SET npci_code = 'YD' WHERE npci_code = 'U31'")
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for c, typ in (("txn_amount", "INTEGER"), ("txn_time", "TEXT"), ("extra", "TEXT")):
            if c not in cols("tickets"):
                con.execute(f"ALTER TABLE tickets ADD COLUMN {c} {typ}")
        con.commit()


def seed_state(con=None) -> dict:
    """The ledger is seeded relative to the day seed.py ran. On any other day the demo lines
    ("yesterday", "5 days ago") point at the wrong payments, so say so loudly."""
    own = con is None
    con = con or db()
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'seeded_on'").fetchone()
        day = row[0] if row else None
    except sqlite3.OperationalError:
        day = None
    finally:
        if own:
            con.close()
    return {"seeded_on": day, "stale": day != datetime.now().date().isoformat()}


def log(con, ticket_id: str | None, actor: str, event: str, detail: str) -> None:
    con.execute("INSERT INTO audit (ts, ticket_id, actor, event, detail) VALUES (?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), ticket_id, actor, event, detail))


def _next_ticket_id(con) -> str:
    # Called inside BEGIN IMMEDIATE, and tickets are never deleted, so this cannot repeat.
    n = con.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] + 1
    return f"T{n:05d}"


# ------------------------------------------------- NPCI codes -> our states
# Our three states are read from the bank's response code, not typed in by hand.
# Illustrative mapping of a few NPCI UPI codes; confirm against NPCI's current list.
NPCI = {
    "00":  ("SUCCESS",        "approved"),
    "BT":  ("SUCCESS_LATE",   "acquirer or beneficiary bank unavailable (timeout); a later "
                              "status check confirmed success"),
    "YD":  ("FAILED_DEBITED", "do not honour, from the beneficiary bank: declined after the "
                              "customer was debited, so the money never reached the shop"),
}

RBI_TAT_DAYS = 5        # RBI circular, 20 Sep 2019: failed UPI P2M, debited but not credited
RBI_PENALTY = 100       # ₹ per day owed to the customer after T+5
UPI_CEILING = 10_00_000  # assumption: nothing above ₹10 lakh is one UPI payment
LOOKBACK_DAYS = 7       # with no time given, how far back we search


def settlement_cycle(now: datetime) -> dict:
    """Paytm settles on a cycle with a cut-off. Cut-off is a setting (assumed 18:00)."""
    cut = str(policy.load().get("settlement_cutoff", "18:00"))
    hh, mm = (int(x) for x in cut.split(":"))
    cutoff = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    day = now.date() if now < cutoff else now.date() + timedelta(days=1)
    return {"date": day.isoformat(), "cutoff": cut, "today": now < cutoff}


def rbi_clock(txn_time: str | None, bank_status: str | None, now: datetime | None = None):
    """T+5 for a failed, debited payment. None when no RBI clock applies."""
    if not txn_time or bank_status != "FAILED_DEBITED":
        return None
    now = now or datetime.now()
    t = datetime.fromisoformat(txn_time)
    deadline = datetime.combine(t.date() + timedelta(days=RBI_TAT_DAYS), datetime.max.time())
    hours = (deadline - now).total_seconds() / 3600
    days_over = max(0, (now.date() - deadline.date()).days)
    return {"deadline": deadline.isoformat(timespec="minutes"), "hours_left": round(hours, 1),
            "overdue": hours < 0, "owed": days_over * RBI_PENALTY}


# ------------------------------------------------- mock Paytm systems (API)
def _txn_view(r: dict) -> dict:
    code = r.get("npci_code") or "00"
    return r | {"npci_meaning": NPCI.get(code, ("", "unknown code"))[1],
                "rbi": rbi_clock(r["txn_time"], r["bank_status"])}


def _duplicate_of(con, r: dict) -> str | None:
    """Code, not the model, decides whether a payment repeats one just before it."""
    prev = con.execute(
        "SELECT utr, txn_time FROM ledger WHERE merchant_id = ? AND source_account = ?"
        " AND amount = ? AND utr != ? AND txn_time <= ? ORDER BY txn_time DESC LIMIT 1",
        (r["merchant_id"], r["source_account"], r["amount"], r["utr"], r["txn_time"])).fetchone()
    if not prev:
        return None
    gap = datetime.fromisoformat(r["txn_time"]) - datetime.fromisoformat(prev["txn_time"])
    return prev["utr"] if gap <= timedelta(minutes=2) else None


def _ledger_search(con, merchant_id: str, amount: int | None, utr: str | None,
                   since: datetime | None = None, until: datetime | None = None) -> list[dict]:
    if utr:
        # A UTR names one payment anywhere. Whether it is THIS shop's is check 1's call,
        # so a claim on someone else's payment reaches the gate and is refused there.
        rows = con.execute("SELECT * FROM ledger WHERE utr = ?", (utr,)).fetchall()
    else:
        q, a = "SELECT * FROM ledger WHERE merchant_id = ?", [merchant_id]
        if amount is not None:
            q += " AND amount = ?"; a.append(amount)
        if since:
            q += " AND txn_time >= ?"; a.append(since.isoformat(timespec="seconds"))
        if until:
            q += " AND txn_time <= ?"; a.append(until.isoformat(timespec="seconds"))
        rows = con.execute(q + " ORDER BY txn_time DESC LIMIT 10", a).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/ledger/search")
def ledger_search(merchant_id: str, amount: int | None = None, utr: str | None = None):
    """Read-only. Stands in for Paytm's transaction ledger."""
    with closing(db()) as con:
        return [_txn_view(r) for r in _ledger_search(con, merchant_id, amount, utr)]


# ------------------------------------------------------- the gate, and money
HUMAN_MAY_OVERRIDE = {"2", "5"}     # a person may lift the ceiling or the daily cap, nothing else
_RULINGS: dict[int, tuple] = {}     # single-use authorisations, minted only by _rule()
_RULINGS_LOCK = threading.Lock()


class NotAuthorised(Exception):
    pass


class AlreadyDone(Exception):
    pass


def _rule(merchant: dict, txn_row: dict | None, dup_of: str | None, action: str,
          destination: str, matches: int, claimed: int | None, claimed_time: datetime,
          message: str, ticket_id: str, human: str | None = None) -> gate.Decision:
    """The only caller of gate.gate(). Returns its ruling; mints a token if it may act."""
    txn = gate.Txn(txn_row["utr"], txn_row["merchant_id"], txn_row["terminal"],
                   txn_row["amount"], datetime.fromisoformat(txn_row["txn_time"]),
                   txn_row["bank_status"], txn_row["source_account"],
                   duplicate_of=dup_of) if txn_row else None
    g_merchant = gate.Merchant(merchant["merchant_id"], merchant["payout_account"],
                               merchant["auto_today"])
    # check 2 weighs the money that would actually move, never the amount someone typed
    proposal = gate.Proposal(action, txn.amount if txn else (claimed or 0),
                             destination or "", txn, matches)
    ticket = gate.Ticket(ticket_id, merchant["merchant_id"], merchant["terminal"],
                         claimed or 0, claimed_time, message, utr=txn.utr if txn else None)
    decision = gate.gate(proposal, ticket, {merchant["merchant_id"]: g_merchant})
    decision.by = "gate"
    may_act = decision.outcome == "APPROVE"
    if human and decision.outcome == "ESCALATE" and \
            {f[0] for f in decision.failed} <= HUMAN_MAY_OVERRIDE:
        may_act, decision.by = True, f"human:{human}"
    elif human and may_act:
        # the gate passed it outright (a case held only because its message tried to override
        # the rules); the person approving it is the one who acts
        decision.by = f"human:{human}"
    if may_act and txn:
        with _RULINGS_LOCK:
            _RULINGS[id(decision)] = (decision, action, txn.utr, destination)
    return decision


def _execute(con, ruling: gate.Decision, ticket_id: str) -> None:
    """The only place money moves. Takes nothing but a ruling from _rule(), once."""
    with _RULINGS_LOCK:
        entry = _RULINGS.pop(id(ruling), None)
    if not entry or entry[0] is not ruling:
        raise NotAuthorised("no gate ruling authorises this")
    _, action, utr, destination = entry
    if action not in gate.ALLOWED_ACTIONS:
        raise NotAuthorised(f"{action} is not an allowed action")
    if action == "MARK_PAID":
        cyc = settlement_cycle(datetime.now())
        cur = con.execute("UPDATE ledger SET settled = 1, note = ? WHERE utr = ?"
                          " AND settled = 0 AND reversed = 0",
                          (f"marked paid -> {destination}, cycle {cyc['date']}", utr))
        event = "SETTLEMENT_MARK_PAID"
    else:
        cur = con.execute("UPDATE ledger SET reversed = 1, note = ? WHERE utr = ?"
                          " AND settled = 0 AND reversed = 0",
                          (f"{action.lower()} -> {destination}", utr))
        event = "REVERSAL" if action == "REVERSE_TO_SOURCE" else "DUPLICATE_REFUND"
    if cur.rowcount != 1:
        raise AlreadyDone(utr)
    log(con, ticket_id, ruling.by.split(":")[0] if ruling.by != "gate" else "system",
        event, f"{utr} -> {destination}")


# --------------------------------------------------------- understand step
CUR = r"(?:₹|rs\.?|inr|rupees?|रुपये|रुपए|ரூ\.?|ரூபாய்)"
UTR_TOKEN = re.compile(r"\bUTR[\s:#.-]*([A-Za-z0-9]*)", re.I)
BARE_UTR = re.compile(r"(?<![\d.,])(\d{12})(?![\d.,])")
AMOUNT = re.compile(rf"(?<![\w.\-])-?\s*({CUR}\s*)?(\d[\d,]*(?:\.\d+)?)(\s*(?:{CUR}|/-)(?!\s*\d))?", re.I)
# Account and UPI references. Read BEFORE UTRs and amounts: their digits are never money,
# and a 12-digit bank account number must not be taken for a UTR.
_TOK = r"(?P<tok>[A-Za-z0-9][A-Za-z0-9\-]*\d[A-Za-z0-9\-]*)(?!\s*(?:rs\b|rupees?|inr\b|/-|₹))"
ACCOUNT_LABELLED = re.compile(      # "account number N", "account no N", "a/c N", "acc N", "ac no N"
    r"\b(?:bank\s+)?(?:account\s*(?:number|num|no)\b\.?|acct\b\.?|a/c\b|acc(?=[\s.:#])\.?"
    r"(?:\s*(?:number|num|no)\b\.?)?|ac\s*(?:number|num|no)\b\.?)\s*[:#\-]?\s*" + _TOK, re.I)
ACCOUNT_PLAIN = re.compile(         # "account ACC-NEW-4471", "account 123456789012"
    r"\baccount\s*[:#\-]?\s*(?P<tok>(?=[A-Za-z0-9\-]*[A-Za-z][A-Za-z0-9\-]*\d)[A-Za-z0-9\-]{4,}"
    r"|\d{8,18})\b", re.I)
ACCOUNT_REVERSED = re.compile(      # "1765 is the account number"
    r"\b" + _TOK + r"\s+is\s+(?:my\s+|the\s+|our\s+|his\s+|her\s+)?(?:new\s+)?"
    r"(?:bank\s+)?(?:account|a/c|acc)(?:\s*(?:number|num|no))?\b", re.I)
# Hinglish / romanised Tamil "send it to X": "1765 pe bhej do", "1765 ko transfer karo",
# "1765 mein daal do", "1765-ku anuppunga". The X is a destination, never an amount.
SEND_TO_LOCAL = re.compile(
    r"(?<![\w@.\-])(?P<tok>[A-Za-z0-9._\-]+@[A-Za-z]{2,}|(?:[A-Za-z0-9._]+-)*[A-Za-z0-9._]*\d"
    r"[A-Za-z0-9._]*)\s*-?\s*(?:pe|par|pa|mein|me|main|ko|"
    r"ku|kku|kke)\s+(?:bhej|bhejo|bhejna|daal|dal|dalo|transfer|jama|credit|anupp|anuppu|"
    r"pottu|podu)\w*\b", re.I)
VPA = re.compile(r"(?P<kw>\b(?:upi\s*id|vpa|upi)\s*[:#\-]?\s*)?"
                 r"\b(?P<tok>[A-Za-z0-9._\-]{2,}@[A-Za-z]{2,})\b", re.I)
# A reference is a DESTINATION only when the message points money at it. "My account number
# 123 didn't get ₹4,250" describes the shop's own account and must not reach the risk log.
DIRECTION = re.compile(r"\b(?:send|sent|transfer|pay|deposit|put|credit|move|refund|give)\b"
                       r"|\b(?:to|into)\s+(?:my\s+|the\s+|this\s+|a\s+)?(?:new|other|another|"
                       r"different)\b|\b(?:to|into)\s*$", re.I)
# Attempts to change the rules. Literal and narrow on purpose: a false positive puts a real
# complaint on the risk log, which is worse than the bug this catches.
OVERRIDE = re.compile(
    r"\bignore\s+(?:all\s+|any\s+)?(?:of\s+)?(?:your|ur|the|previous|prior|above|earlier|"
    r"these|those)\s+(?:\w+\s+)?(?:rules|instructions|prompts?|guidelines|polic(?:y|ies)|checks)\b"
    r"|\bignore\s+(?:all\s+)?(?:previous|prior)\s+(?:\w+\s+)?(?:messages?|commands?)\b"
    r"|\b(?:disregard|forget|override|bypass)\s+(?:all\s+|any\s+)?(?:your|ur|the|previous|prior|"
    r"above|earlier)?\s*(?:rules|instructions|prompts?|guidelines|polic(?:y|ies)|checks|gate)\b"
    r"|\byou\s+are\s+now\b|\bact\s+as\s+(?:an?\s+|the\s+)?(?:admin|administrator|developer|"
    r"system|bank)\b|\bsystem\s+prompt\b|\bdeveloper\s+mode\b|\bjailbreak\b", re.I)
WORD_AMOUNT = re.compile(r"\b(hundred|thousand|lakh|lac|crore)\b|ஆயிரம்|நூறு|हज़ार|हजार|सौ|लाख",
                         re.I)
DUPLICATE_WORDS = re.compile(r"\b(twice|two times|double|duplicate|paid again|charged again)\b"
                             r"|இரண்டு முறை|दो बार|डबल", re.I)
TIME_STRIP = re.compile(r"\b\d{1,3}\s*(?:days?|hrs?|hours?|mins?|minutes?)\s*ago\b"
                        r"|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b", re.I)


def claimed_window(message: str, now: datetime) -> dict:
    """Turn 'yesterday', 'this morning', 'just now', '3 days ago', 'at 4:30 pm' into a
    search window. gate.MATCH_WINDOW widens it on both sides. No time -> last 7 days."""
    s = message.lower()
    w = gate.MATCH_WINDOW
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    base, label = None, ""
    if re.search(r"day before yesterday", s):
        base, label = day0 - timedelta(days=2), "the day before yesterday"
    elif m := re.search(r"\b(\d{1,2}|a|one|two|three|four|five|six)\s*days?\s*ago\b", s):
        n = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}.get(
            m.group(1)) or int(m.group(1))
        base, label = day0 - timedelta(days=n), f"{n} day{'s' if n > 1 else ''} ago"
    elif re.search(r"\byesterday\b|நேற்று|\bकल\b|\bkal\b", s):
        base, label = day0 - timedelta(days=1), "yesterday"
    elif re.search(r"\btoday\b|இன்று|आज|\baaj\b", s):
        base, label = day0, "today"

    point = None
    if re.search(r"\b(just now|right now|a moment ago|a minute ago|just paid)\b", s):
        point, label = now, "just now"
    elif m := re.search(r"\b(\d{1,3})\s*(?:mins?|minutes?)\s*ago\b", s):
        point, label = now - timedelta(minutes=int(m.group(1))), f"{m.group(1)} min ago"
    elif m := re.search(r"\b(\d{1,2}|an|one)\s*(?:hrs?|hours?)\s*ago\b", s):
        n = 1 if m.group(1) in ("an", "one") else int(m.group(1))
        point, label = now - timedelta(hours=n), f"{n} hour{'s' if n > 1 else ''} ago"
    elif m := re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(\d{1,2}):(\d{2})\b", s):
        h = int(m.group(1) or m.group(4)); mi = int(m.group(2) or m.group(5) or 0)
        ap = m.group(3)
        if ap == "pm" and h < 12: h += 12
        if ap == "am" and h == 12: h = 0
        if h < 24 and mi < 60:
            d = base or day0
            point = d.replace(hour=h, minute=mi)
            if base is None and point > now:
                point -= timedelta(days=1)
            label = (label + " " if label else "") + f"at {point:%H:%M}"
    if point:
        return {"since": point - w, "until": point + w, "point": point, "label": label,
                "given": True}

    pm = re.search(r"\b(this|yesterday|last)\s+(morning|afternoon|evening|night)\b"
                   r"|\b(tonight)\b", s)
    if pm:
        which, word = pm.group(1) or "this", pm.group(2) or "night"
        if base is None:
            base = day0 - timedelta(days=1) if which in ("yesterday", "last") else day0
        a, b = {"morning": (6, 12), "afternoon": (12, 17), "evening": (17, 23),
                "night": (18, 28)}[word]
        start, end = base + timedelta(hours=a), base + timedelta(hours=b)
        return {"since": start - w, "until": min(end, now) + w, "point": start,
                "label": pm.group(0), "given": True}
    if base is not None:
        return {"since": base - w, "until": min(base + timedelta(days=1), now) + w,
                "point": base, "label": label, "given": True}
    return {"since": now - timedelta(days=LOOKBACK_DAYS), "until": now, "point": now,
            "label": f"no time given, so the last {LOOKBACK_DAYS} days", "given": False}


def understand(message: str) -> dict:
    """Pull amount, UTR and any requested destination out of the message, and say what
    could not be read rather than guessing."""
    problems: list[str] = []
    # account and UPI references first
    refs, dest = [], None
    for pat in (ACCOUNT_LABELLED, ACCOUNT_PLAIN, ACCOUNT_REVERSED, SEND_TO_LOCAL, VPA):
        for m in pat.finditer(message):
            tok = m.group("tok")
            refs.append(tok)
            before = message[max(0, m.start() - 40):m.start()]
            pointed = bool(DIRECTION.search(before)) or pat in (ACCOUNT_REVERSED,
                                                                 SEND_TO_LOCAL) or (
                pat is VPA and bool(m.group("kw")))
            if pointed and dest is None:
                dest = tok
    scrubbed = message
    for tok in sorted(set(refs), key=len, reverse=True):
        # a trailing hyphen is allowed after the token: "1765-ku anuppunga"
        scrubbed = re.sub(r"(?<![\w@.\-])" + re.escape(tok) + r"(?![\w@]|-\w*\d)", " ", scrubbed)
    utrs, bad = [], []
    for m in UTR_TOKEN.finditer(scrubbed):
        tok = m.group(1)
        if not tok or not re.search(r"\d", tok):
            continue                                # "send me the UTR" names no UTR
        (utrs if re.fullmatch(r"\d{12}", tok) else bad).append(tok)
    scrubbed = UTR_TOKEN.sub(" ", scrubbed)
    utrs += BARE_UTR.findall(scrubbed)
    scrubbed = BARE_UTR.sub(" ", scrubbed)
    utrs = list(dict.fromkeys("UTR" + u for u in utrs))
    if bad and not utrs:
        problems.append("bad_utr")
    if len(utrs) > 1:
        problems.append("two_utrs")

    scrubbed = TIME_STRIP.sub(" ", scrubbed)
    scrubbed = re.sub(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b", " ", scrubbed)   # dates
    found: list[Decimal] = []
    for m in AMOUNT.finditer(scrubbed):
        digits = m.group(2).replace(",", "")
        whole = digits.split(".")[0]
        has_cur = bool(m.group(1) or m.group(3))
        if len(whole) >= 13 and not has_cur:
            problems.append("too_big"); continue     # no reference number is this long
        if len(whole) >= 9 and not has_cur:
            continue                                 # a reference or phone number, not money
        if len(whole) < 3 and not has_cur:
            continue                                 # "2 payments", "SB 4" — not an amount
        try:
            found.append(Decimal(digits))
        except InvalidOperation:
            pass
    distinct = list(dict.fromkeys(found))
    amt = None
    if len(distinct) > 1:
        problems.append("two_amounts")
    elif distinct:
        v = distinct[0]
        if v != v.to_integral_value():
            problems.append("paise")
        elif v > UPI_CEILING:
            problems.append("too_big")
        elif v <= 0:
            problems.append("zero")
        else:
            amt = int(v)
    if amt is None and not distinct and not utrs and WORD_AMOUNT.search(message):
        problems.append("words")
    if len(utrs) == 1 and problems and set(problems) <= {"two_amounts", "paise", "zero"}:
        problems, amt = [], None          # one clean UTR names the payment; no need to guess
    return {"amount": amt, "amounts_seen": [str(x) for x in distinct],
            "utr": utrs[0] if len(utrs) == 1 else None, "utrs_seen": utrs, "bad_utr": bad[:1],
            "requested_destination": dest.upper() if dest and "@" not in dest else dest,
            "account_refs": refs,
            "override": (o.group(0) if (o := OVERRIDE.search(message)) else None),
            "duplicate": bool(DUPLICATE_WORDS.search(message)), "problems": problems}


# A shop writes about more than disputed payments. Work out what this message is
# before putting it anywhere near the gate.
DISPUTE_WORDS = re.compile(
    r"\b(debit|debited|paid|payment|pay|not received|never (?:came|arrived|reached|showed|got)|"
    r"missing|didn'?t (?:get|receive|come)|did not (?:get|receive|arrive|reach)|refund|reverse|"
    r"settle|settlement|soundbox|upi|stuck|pending|twice|double|charged)\b", re.I)
HELP_WORDS = re.compile(r"\b(how|what|when|where|why|can i|do i|help|explain)\b", re.I)
PERSON_WORDS = re.compile(r"\b(agent|human|real person|a person|someone|call me|customer care|"
                          r"talk to|speak to)\b", re.I)
PERSON_REPLY = {
    "en": "I cannot hand this chat to a person. If it is about a payment, tell me the amount "
          "and roughly when: anything I cannot settle myself goes to our team with the "
          "evidence. For anything else, use Help in the Paytm for Business app.",
    "ta": "இந்த உரையாடலை ஒருவரிடம் மாற்ற முடியாது. பணம் பற்றியது என்றால் தொகையும் நேரமும் சொல்லுங்கள் — நான் தீர்க்க முடியாததை ஆதாரத்துடன் எங்கள் குழுவுக்கு அனுப்புவேன். மற்றவற்றுக்கு ஆப்பில் Help பயன்படுத்துங்கள்.",
    "hi": "मैं यह बातचीत किसी व्यक्ति को नहीं दे सकता। भुगतान की बात हो तो राशि और समय बताइए — जो मैं खुद नहीं सुलझा सकता वह सबूत के साथ टीम को जाता है। बाकी के लिए ऐप में Help देखिए।",
}


def classify(message: str, facts: dict) -> str:
    """dispute | ask | help | flagged — decided before the gate is involved."""
    if facts["requested_destination"]:
        return "dispute"             # money pointed somewhere: the gate must see it, always
    if facts.get("override") and not (facts["amount"] or facts["utr"]):
        return "flagged"             # tried to change the rules, and asked for nothing checkable
    if facts["problems"]:
        return "ask"                 # we could not read it cleanly; ask, never guess
    if facts["amount"] or facts["utr"]:
        return "dispute"
    # The model may turn a vague message INTO a complaint, never the other way round: seen
    # live, Sarvam called "a customer paid me twice, please refund one" not a dispute.
    if facts.get("intent") == "dispute":
        return "ask"
    if HELP_WORDS.search(message) or not DISPUTE_WORDS.search(message):
        return "help"
    return "ask"                     # a dispute, but no amount: ask for it


# ------------------------------------------------------------ propose step
def have_key() -> bool:
    import agent
    return agent.use_sarvam() or any(
        os.getenv(k) for k in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"))


def model_name() -> str:
    """Which model is deciding, for the case desk and the logs."""
    import agent
    if agent.use_sarvam():
        return f"Sarvam {agent.SARVAM_MODEL}"
    if os.getenv("GROQ_API_KEY"):
        return "Groq"
    return "real model" if have_key() else "stand-in"


def propose(facts: dict, matches: list[dict], merchant: dict) -> dict:
    """The rule stand-in: used with no API key, or when the model's answer cannot be used.
    (With a key, the model proposes inside read_and_propose(), in the same call that reads.)
    Whatever comes back is only a PROPOSAL. The gate decides if it happens."""
    first = matches[0] if matches else None
    if first and facts.get("duplicate") and first.get("duplicate_of"):
        plan = {"action": "REFUND_DUPLICATE", "utr": first["utr"],
                "destination": first["source_account"],
                "reason": "The customer paid the same amount twice; return the second."}
    elif first and first["bank_status"] == "FAILED_DEBITED":
        plan = {"action": "REVERSE_TO_SOURCE", "utr": first["utr"],
                "destination": first["source_account"],
                "reason": "Customer was debited but the payment failed; return it."}
    elif first:
        plan = {"action": "MARK_PAID", "utr": first["utr"],
                "destination": merchant["payout_account"],
                "reason": "Bank confirmed the payment; credit the merchant."}
    else:
        plan = {"action": "NONE", "utr": None, "destination": "",
                "reason": "No matching payment found."}
    plan["planner"] = "stand-in"
    return plan


# ------------------------------------------------- the model reads and proposes, once
DEST_OK = re.compile(r"^[A-Za-z0-9@._\-]{3,40}$")


def _candidates(con, merchant_id: str, facts: dict, window: dict, now: datetime) -> list[dict]:
    """What the model may choose from: this shop's recent payments, plus any payment a UTR in
    the message names (even another shop's, so the gate can refuse it on check 1)."""
    # Kept small on purpose: prompt size is most of the model's time (and of the free-tier
    # token allowance). Payments at the amount the regex saw, else the most recent few.
    since = min(window["since"], now - timedelta(days=LOOKBACK_DAYS))
    q = "SELECT * FROM ledger WHERE merchant_id = ? AND txn_time >= ?"
    args = [merchant_id, since.isoformat(timespec="seconds")]
    if facts.get("amount"):
        q += " AND amount = ?"; args.append(facts["amount"])
    rows = [dict(r) for r in con.execute(q + " ORDER BY txn_time DESC LIMIT 8", args)]
    for u in facts.get("utrs_seen") or []:
        if not any(r["utr"] == u for r in rows):
            rows += [dict(r) for r in con.execute("SELECT * FROM ledger WHERE utr = ?", (u,))]
    rows = rows[:10]
    for r in rows:
        r["duplicate_of"] = _duplicate_of(con, r)
    return rows


def _valid_reading(raw: str) -> tuple[dict, dict] | None:
    """Parse and check the model's answer. Anything malformed returns None, and the caller
    falls back to the regex reading silently. No partial trust: one bad field rejects all."""
    try:
        blob = re.search(r"\{.*\}", raw or "", re.S)
        data = json.loads(blob.group(0)) if blob else None
        rd = data["reading"]
        if not isinstance(rd, dict):
            return None
        amt = rd.get("amount")
        if amt is not None:
            if isinstance(amt, bool) or not isinstance(amt, (int, float)) or amt != int(amt) \
                    or not 0 < amt <= UPI_CEILING:
                return None
            amt = int(amt)
        utr = rd.get("utr")
        if utr is not None:
            digits = re.sub(r"^UTR|\s", "", str(utr), flags=re.I)
            if not re.fullmatch(r"\d{12}", digits):
                return None
            utr = "UTR" + digits
        dest = rd.get("destination")
        if dest is not None and (not isinstance(dest, str) or not DEST_OK.match(dest)):
            return None
        intent = rd.get("intent")
        if intent not in ("dispute", "question", "other"):
            return None
        dup = rd.get("duplicate", False)
        if not isinstance(dup, bool):
            return None
        action = data.get("action")
        if not isinstance(action, str) or not action:
            return None
        p_utr, p_dest = data.get("utr"), data.get("destination")
        if p_utr is not None and not isinstance(p_utr, str):
            return None
        if p_dest is not None and not isinstance(p_dest, str):
            return None
        reading = {"amount": amt, "utr": utr, "destination": dest, "intent": intent,
                   "duplicate": dup}
        plan = {"action": action, "utr": p_utr, "destination": p_dest or "",
                "reason": str(data.get("reason") or "")[:300], "planner": "llm"}
        return reading, plan
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


# The live model call gives up after this, and the regex reading takes over silently. Seen
# live on 15 Nov: two Sarvam calls hung for the full 30 s; a judge must never wait that long.
MODEL_TIMEOUT_S = 6


def read_and_propose(message: str, merchant: dict, candidates: list[dict]):
    """One model call: what the message says, and one proposed action. None on any failure."""
    try:
        import agent
        payload = {"merchant_message": message,
                   "merchant_payout_account": merchant["payout_account"],
                   "recent_transactions": [
                       {"utr": c["utr"], "amount": c["amount"], "time": c["txn_time"],
                        "bank_status": c["bank_status"], "duplicate_of": c.get("duplicate_of"),
                        "customer_source_account": c["source_account"]} for c in candidates]}
        return _valid_reading(agent.ask(json.dumps(payload, ensure_ascii=False,
                                                   separators=(",", ":")),
                                        system=agent.SYSTEM_READ, model=agent.READ_MODEL,
                                        timeout=MODEL_TIMEOUT_S))
    except Exception as exc:                               # keep the demo alive
        print(f"[reader] model unavailable, using the regex reading: {exc!r}")
        return None


def merge_reading(facts: dict, rd: dict) -> dict:
    """The model fills the gaps the regex left; where the regex was unambiguous, it stands.
    A destination either reader sees is ruled on (union): a redirect must never go unseen."""
    f = dict(facts)
    clean_amount = facts["amount"] is not None and not facts["problems"]
    # The model may only read an amount the shop EXPRESSED: as figures in the message, or in
    # words. Seen live: given "paid twice, please refund one" (no amount), it took ₹640 from
    # the payments it was shown. The refund was right, but the shop never said 640.
    digits = re.sub(r"[,\s]", "", facts.get("message") or "")
    expressed = rd["amount"] is not None and (
        str(rd["amount"]) in digits or bool(WORD_AMOUNT.search(facts.get("message") or "")))
    if not clean_amount and not facts["utr"] and expressed:
        f["amount"] = rd["amount"]
        f["problems"] = [p for p in facts["problems"]
                         if p not in ("two_amounts", "words", "paise", "zero")]
    squashed = re.sub(r"[\s\-]", "", facts.get("message") or "")
    if not facts["utr"] and rd["utr"] and "two_utrs" not in facts["problems"] \
            and rd["utr"][3:] in squashed:
        # e.g. "UTR 3000 0000 0003": the regex saw a 4-digit UTR; the model saw the whole one,
        # and it is really there in what the shop wrote. One UTR names the payment.
        f["utr"] = rd["utr"]
        f["amount"] = None if not clean_amount else f["amount"]
        f["problems"] = [p for p in f["problems"]
                         if p not in ("bad_utr", "two_amounts", "paise", "zero")]
    # The model may only point at an account the SHOP WROTE. It can see the shop's payout
    # account and customers' accounts in its input, and a small model will sometimes report
    # one of those as "the destination" (seen live: PAYOUT-002 on a plain reversal).
    said = (facts.get("message") or "").lower()
    if not facts["requested_destination"] and rd["destination"] \
            and rd["destination"].lower() in said:
        f["requested_destination"] = rd["destination"]
    f["duplicate"] = facts["duplicate"] or rd["duplicate"]
    f["intent"] = rd["intent"]
    return f


def rule_on_request(plan: dict, facts: dict) -> dict:
    """When the shop asks for money to go somewhere, THAT request is what the gate rules on.
    A model that quietly proposes the safe account instead would let the redirect attempt
    pass unseen (the case closes as 'already credited'). Code decides what is ruled on."""
    asked = facts.get("requested_destination")
    if asked and plan.get("destination") != asked:
        if plan.get("destination"):
            plan["reason"] = (plan.get("reason") or "") + \
                f" [model proposed {plan['destination']}; the shop asked for {asked}, " \
                "and that request is what the gate rules on]"
        plan["destination"] = asked
    return plan


# --------------------------------------------------------------- reply text
# Every reply is a fixed template filled with figures read from the record.
REPLIES = {
    "MARK_PAID": {
        "en": "Found it — ₹{amt} did reach you, the Soundbox just missed it. {cycle} Receipt: {utr}",
        "ta": "கிடைத்துவிட்டது — ₹{amt} உங்களுக்கு வந்துவிட்டது, சவுண்ட்பாக்ஸ் அறிவிக்கவில்லை. {cycle} ரசீது: {utr}",
        "hi": "मिल गया — ₹{amt} आपको मिल चुका है, साउंडबॉक्स ने बताया नहीं। {cycle} रसीद: {utr}",
    },
    "REVERSE_TO_SOURCE": {
        "en": "That payment of ₹{amt} failed at the bank. I have returned it to the customer — it reaches them within 24 hours. Reference: {utr}",
        "ta": "அந்த ₹{amt} பணம் வங்கியில் தோல்வியடைந்தது. வாடிக்கையாளருக்குத் திருப்பி அனுப்பிவிட்டேன் — 24 மணி நேரத்தில் சேரும். குறிப்பு: {utr}",
        "hi": "वह ₹{amt} का भुगतान बैंक में विफल हुआ। मैंने ग्राहक को वापस भेज दिया है — 24 घंटे में पहुँच जाएगा। संदर्भ: {utr}",
    },
    "REFUND_DUPLICATE": {
        "en": "The customer paid ₹{amt} twice, within two minutes. I have returned the second payment to them; the first stays with you. Reference: {utr}",
        "ta": "வாடிக்கையாளர் ₹{amt} இரண்டு முறை செலுத்தியுள்ளார். இரண்டாவது பணத்தைத் திருப்பி அனுப்பிவிட்டேன்; முதலாவது உங்களிடம் இருக்கும். குறிப்பு: {utr}",
        "hi": "ग्राहक ने ₹{amt} दो बार चुकाए। दूसरा भुगतान उन्हें लौटा दिया है; पहला आपके पास रहेगा। संदर्भ: {utr}",
    },
    "ESCALATE": {
        "en": "I have checked your records and passed this to our team with everything I found. You will hear back shortly.",
        "ta": "உங்கள் பதிவுகளைச் சரிபார்த்து, கண்டறிந்த அனைத்தையும் எங்கள் குழுவிடம் அனுப்பியுள்ளேன். விரைவில் பதில் வரும்.",
        "hi": "मैंने आपके रिकॉर्ड जाँच लिए हैं और सब कुछ हमारी टीम को भेज दिया है। जल्द जवाब मिलेगा।",
    },
    "BLOCK_AMBIGUOUS": {
        "en": "I can see more than one payment of ₹{amt} around that time, so I will not touch the wrong one. Send me the UTR from the customer's app and I will finish this.",
        "ta": "அந்த நேரத்தில் ₹{amt} தொகையில் ஒன்றுக்கு மேற்பட்ட பணப்பரிவர்த்தனைகள் உள்ளன. தவறானதைத் தொட விரும்பவில்லை. வாடிக்கையாளரின் ஆப்பிலிருந்து UTR அனுப்புங்கள்.",
        "hi": "उस समय ₹{amt} के एक से ज़्यादा भुगतान दिख रहे हैं, इसलिए मैं ग़लत वाला नहीं छूऊँगा। ग्राहक के ऐप से UTR भेजिए।",
    },
    "BLOCK_REDIRECT_PAYOUT": {
        "en": "I can only pay you into your registered payout account. Changing that account needs the bank verification in the app — I cannot do it from a message. I have flagged this for our team.",
        "ta": "உங்கள் பதிவு செய்யப்பட்ட கணக்குக்கு மட்டுமே பணம் அனுப்ப முடியும். கணக்கை மாற்ற ஆப்பில் வங்கிச் சரிபார்ப்பு தேவை — செய்தி மூலம் செய்ய முடியாது. இதை எங்கள் குழுவிடம் குறித்துள்ளேன்.",
        "hi": "मैं पैसा सिर्फ़ आपके पंजीकृत पेआउट खाते में भेज सकता हूँ। खाता बदलने के लिए ऐप में बैंक सत्यापन चाहिए — संदेश से यह नहीं हो सकता। टीम के लिए फ़्लैग कर दिया है।",
    },
    "BLOCK_REDIRECT_REVERSAL": {
        "en": "A failed payment can only go back to the account it came from. I have flagged this for our team.",
        "ta": "தோல்வியடைந்த பணம் வந்த கணக்குக்கே மட்டுமே திரும்பச் செல்ல முடியும். இதை எங்கள் குழுவிடம் குறித்துள்ளேன்.",
        "hi": "विफल भुगतान सिर्फ़ उसी खाते में लौट सकता है जहाँ से आया था। टीम के लिए फ़्लैग कर दिया है।",
    },
    "BLOCK_FOREIGN": {
        "en": "That UTR is not a payment to your shop, so I cannot act on it. I have flagged this for our team. Check the UTR on the customer's payment screen.",
        "ta": "அந்த UTR உங்கள் கடைக்கான பணம் அல்ல, அதனால் செயல்பட முடியாது. இதை எங்கள் குழுவிடம் குறித்துள்ளேன்.",
        "hi": "वह UTR आपकी दुकान का भुगतान नहीं है, इसलिए मैं कुछ नहीं कर सकता। टीम के लिए फ़्लैग कर दिया है।",
    },
    "BLOCK_MISMATCH": {
        "en": "That UTR is for ₹{txn_amt}, not ₹{amt}. I will not act while the two disagree. Check both on the customer's payment screen and send them again.",
        "ta": "அந்த UTR ₹{txn_amt} தொகைக்கானது, ₹{amt} அல்ல. இரண்டும் பொருந்தும் வரை செயல்பட மாட்டேன். மீண்டும் சரிபார்த்து அனுப்புங்கள்.",
        "hi": "वह UTR ₹{txn_amt} का है, ₹{amt} का नहीं। दोनों मेल खाने तक मैं कुछ नहीं करूँगा। फिर से जाँचकर भेजिए।",
    },
    "BLOCK_OTHER": {
        "en": "I cannot act on this one. I have flagged it for our team to look at.",
        "ta": "இதில் என்னால் செயல்பட முடியாது. இதை எங்கள் குழுவிடம் குறித்துள்ளேன்.",
        "hi": "इस पर मैं कुछ नहीं कर सकता। टीम के लिए फ़्लैग कर दिया है।",
    },
    "RESOLVED_ON_UTR": {
        "en": "Got it — that UTR tells them apart. ",
        "ta": "கிடைத்தது — அந்த UTR வேறுபாட்டைக் காட்டுகிறது. ",
        "hi": "मिल गया — उस UTR से फ़र्क़ साफ़ हो गया। ",
    },
    "NONE": {
        "en": "I could not find a payment of ₹{amt} to your shop{when}. Send me the UTR from the customer's app and I will look again.",
        "ta": "{when} உங்கள் கடைக்கு ₹{amt} பணம் கிடைக்கவில்லை. வாடிக்கையாளரின் ஆப்பிலிருந்து UTR அனுப்புங்கள், மீண்டும் பார்க்கிறேன்.",
        "hi": "{when} आपकी दुकान पर ₹{amt} का कोई भुगतान नहीं मिला। ग्राहक के ऐप से UTR भेजिए, मैं फिर देखता हूँ।",
    },
    "NONE_UTR": {
        "en": "I could not find UTR {utr} in the payment records. Check it on the customer's payment screen and send it again.",
        "ta": "UTR {utr} பதிவுகளில் கிடைக்கவில்லை. வாடிக்கையாளரின் திரையில் சரிபார்த்து மீண்டும் அனுப்புங்கள்.",
        "hi": "UTR {utr} रिकॉर्ड में नहीं मिला। ग्राहक की भुगतान स्क्रीन पर जाँचकर फिर भेजिए।",
    },
    "ALREADY_SETTLED": {
        "en": "That ₹{amt} was already credited to you. Nothing more to do. Receipt: {utr}",
        "ta": "அந்த ₹{amt} ஏற்கனவே உங்களுக்கு வரவு வைக்கப்பட்டுள்ளது. வேறு எதுவும் செய்ய வேண்டியதில்லை. ரசீது: {utr}",
        "hi": "वह ₹{amt} पहले ही आपके खाते में जमा हो चुका है। और कुछ करने की ज़रूरत नहीं। रसीद: {utr}",
    },
    "ALREADY_REVERSED": {
        "en": "That ₹{amt} was already returned to the customer. Nothing more to do. Reference: {utr}",
        "ta": "அந்த ₹{amt} ஏற்கனவே வாடிக்கையாளருக்குத் திருப்பி அனுப்பப்பட்டது. வேறு எதுவும் செய்ய வேண்டியதில்லை. குறிப்பு: {utr}",
        "hi": "वह ₹{amt} पहले ही ग्राहक को लौटाया जा चुका है। और कुछ करने की ज़रूरत नहीं। संदर्भ: {utr}",
    },
    "ALREADY_NORMAL": {
        "en": "That ₹{amt} reached you normally at {time} — the bank approved it first time. It is in your usual settlement. Receipt: {utr}",
        "ta": "அந்த ₹{amt} {time} மணிக்கு வழக்கம் போல் உங்களுக்கு வந்துவிட்டது. வழக்கமான செட்டில்மென்டில் உள்ளது. ரசீது: {utr}",
        "hi": "वह ₹{amt} {time} पर सामान्य रूप से आपको मिल गया था। यह आपके नियमित सेटलमेंट में है। रसीद: {utr}",
    },
    "OVERRIDE": {
        "en": "My checks are fixed, and a message cannot change them. If this is about a payment, tell me the amount and roughly when.",
        "ta": "என் சரிபார்ப்புகள் மாறாதவை; ஒரு செய்தியால் அவற்றை மாற்ற முடியாது. பணம் பற்றியது என்றால் தொகையும் நேரமும் சொல்லுங்கள்.",
        "hi": "मेरी जाँचें तय हैं, कोई संदेश उन्हें बदल नहीं सकता। भुगतान की बात हो तो राशि और समय बताइए।",
    },
    "ASK_AMOUNT": {
        "en": "I can look into that. How much was the payment, and roughly when? The UTR from the customer's app is even better.",
        "ta": "பார்க்கிறேன். பணம் எவ்வளவு, எப்போது? வாடிக்கையாளரின் ஆப்பிலுள்ள UTR இருந்தால் இன்னும் நல்லது.",
        "hi": "मैं देखता हूँ। भुगतान कितने का था, और लगभग कब? ग्राहक के ऐप का UTR हो तो और अच्छा।",
    },
    "ASK_WORDS": {
        "en": "I read amounts in figures only — please write it like ₹4,250.",
        "ta": "தொகையை எண்களில் மட்டுமே படிக்க முடியும் — ₹4,250 என்பது போல் எழுதுங்கள்.",
        "hi": "मैं राशि सिर्फ़ अंकों में पढ़ता हूँ — कृपया ₹4,250 की तरह लिखिए।",
    },
    "ASK_WHICH_AMOUNT": {
        "en": "I see more than one amount ({seen}). Which one did the customer pay?",
        "ta": "ஒன்றுக்கு மேற்பட்ட தொகைகள் உள்ளன ({seen}). வாடிக்கையாளர் எவ்வளவு செலுத்தினார்?",
        "hi": "मुझे एक से ज़्यादा राशियाँ दिख रही हैं ({seen})। ग्राहक ने कितना चुकाया?",
    },
    "ASK_WHICH_UTR": {
        "en": "You sent two UTRs ({seen}). Which payment is the problem? Send just that one.",
        "ta": "இரண்டு UTR அனுப்பியுள்ளீர்கள் ({seen}). எந்தப் பணத்தில் பிரச்சனை? அதை மட்டும் அனுப்புங்கள்.",
        "hi": "आपने दो UTR भेजे ({seen})। किस भुगतान में दिक्कत है? सिर्फ़ वही भेजिए।",
    },
    "BAD_UTR": {
        "en": "A UPI reference (UTR) is 12 digits; “{bad}” has {n}. It is on the customer's payment screen as “UPI Ref No”.",
        "ta": "UPI குறிப்பு எண் (UTR) 12 இலக்கங்கள்; “{bad}” இல் {n} உள்ளன. வாடிக்கையாளரின் பணம் செலுத்திய திரையில் “UPI Ref No” ஆக இருக்கும்.",
        "hi": "UPI संदर्भ (UTR) 12 अंकों का होता है; “{bad}” में {n} हैं। यह ग्राहक की भुगतान स्क्रीन पर “UPI Ref No” के रूप में होता है।",
    },
    "TOO_BIG": {
        "en": "That amount is far above what a single UPI payment can be. Could you check the figure?",
        "ta": "அந்தத் தொகை ஒரு UPI பணப்பரிவர்த்தனையின் வரம்பை விட மிக அதிகம். தொகையைச் சரிபார்க்க முடியுமா?",
        "hi": "यह राशि एक UPI भुगतान की सीमा से बहुत ज़्यादा है। क्या आप राशि जाँच सकते हैं?",
    },
    "PAISE": {
        "en": "These records hold whole rupees only, so I cannot match ₹{seen}. Send the rupee amount, or better, the UTR.",
        "ta": "இந்தப் பதிவுகளில் முழு ரூபாய் மட்டுமே உள்ளது; ₹{seen} பொருந்தாது. ரூபாய்த் தொகையையோ UTR-ஐயோ அனுப்புங்கள்.",
        "hi": "इन रिकॉर्ड में सिर्फ़ पूरे रुपये हैं, इसलिए ₹{seen} मेल नहीं खाएगा। रुपये की राशि या UTR भेजिए।",
    },
    "ZERO": {
        "en": "That amount is zero. How much did the customer pay?",
        "ta": "அந்தத் தொகை பூஜ்யம். வாடிக்கையாளர் எவ்வளவு செலுத்தினார்?",
        "hi": "वह राशि शून्य है। ग्राहक ने कितना चुकाया?",
    },
}
CYCLE = {
    "en": ("It goes in today's settlement (cut-off {cut}).",
           "Today's {cut} cut-off has passed, so it goes in tomorrow's settlement."),
    "ta": ("இன்றைய செட்டில்மென்டில் சேரும் ({cut} வரம்பு).",
           "இன்றைய {cut} வரம்பு கடந்ததால், நாளைய செட்டில்மென்டில் சேரும்."),
    "hi": ("यह आज के सेटलमेंट में जाएगा (कट-ऑफ़ {cut})।",
           "आज का {cut} कट-ऑफ़ निकल चुका है, इसलिए यह कल के सेटलमेंट में जाएगा।"),
}
WHEN = {"en": " in that window", "ta": "அந்த நேரத்தில்", "hi": "उस समय"}


def has_template(key: str, lang: str) -> bool:
    return lang in REPLIES.get(key, {})


class Reply:
    """Builds the shop's reply from templates, and remembers how, so the very same reply can
    be rendered in the language the message was written in. Both come from fixed
    templates filled with record data; the model writes neither."""

    def __init__(self, lang: str):
        self.lang, self.parts = lang, []

    def used_lang(self) -> str:
        """The language the reply actually goes out in: the shop's choice, or English for a
        reply we have no template for in that language. Never a mix of the two."""
        if all(has_template(k, self.lang) for k, *_ in self.parts):
            return self.lang
        return "en"

    def __call__(self, key: str, amt=None, utr=None, **kw) -> str:
        self.parts.append((key, amt, utr, dict(kw)))
        return self.text()

    def text(self) -> str:
        lang = self.used_lang()
        return "".join(reply_text(k, lang, a, u, **dict(kw))
                       for k, a, u, kw in self.parts).strip()

    def translated(self, lang: str) -> str | None:
        if not self.parts or not all(has_template(k, lang) for k, *_ in self.parts):
            return None
        return "".join(reply_text(k, lang, a, u, **dict(kw))
                       for k, a, u, kw in self.parts).strip()


def reply_text(key: str, lang: str, amt=None, utr=None, **kw) -> str:
    block = REPLIES.get(key, REPLIES["BLOCK_OTHER"])
    tpl = block.get(lang, block["en"])
    cyc = kw.pop("cycle", None)
    cyc_txt = ""
    if cyc:
        pair = CYCLE.get(lang, CYCLE["en"])
        cyc_txt = pair[0 if cyc["today"] else 1].format(cut=cyc["cutoff"])
    return tpl.format(amt=f"{amt:,}" if amt else "—", utr=utr or "—", cycle=cyc_txt,
                      when=kw.pop("when", WHEN.get(lang, WHEN["en"])), **{
                          "txn_amt": "—", "seen": "", "bad": "", "n": "", "note": "",
                          "time": ""} | kw)


ASK_KEY = {"bad_utr": "BAD_UTR", "two_utrs": "ASK_WHICH_UTR", "two_amounts": "ASK_WHICH_AMOUNT",
           "paise": "PAISE", "too_big": "TOO_BIG", "zero": "ZERO", "words": "ASK_WORDS"}

CUSTOMER_SMS = {
    "REVERSE_TO_SOURCE": "Your UPI payment of ₹{amt} to {shop} on {date} did not go through. "
                         "It has been returned to your account. Ref {utr}.",
    "REFUND_DUPLICATE": "You paid {shop} ₹{amt} twice on {date}. The second payment has been "
                        "returned to your account. Ref {utr}.",
    "MARK_PAID": "Your UPI payment of ₹{amt} to {shop} on {date} was received by the shop. "
                 "Ref {utr}. Nothing more to do.",
}


# ---------------------------------------------------------- the one pipeline
class TicketIn(BaseModel):
    merchant_id: str = Field(max_length=20)
    message: str = Field(max_length=1000)


def _record(con, row: dict) -> None:
    keys = list(row)
    con.execute(f"INSERT INTO tickets ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                [row[k] for k in keys])


def _find_awaiting(con, merchant_id: str, utr: str | None) -> dict | None:
    """The open case this UTR answers: the one waiting on a UTR whose amount matches that
    payment. Not simply the latest — two open cases must not swap answers."""
    if not utr:
        return None
    txn = con.execute("SELECT amount FROM ledger WHERE utr = ? AND merchant_id = ?",
                      (utr, merchant_id)).fetchone()
    if not txn:
        return None
    row = con.execute(
        "SELECT * FROM tickets WHERE merchant_id = ? AND awaiting = 'UTR'"
        " AND status IN ('BLOCKED','NEEDS_INFO') AND claimed_amount = ?"
        " ORDER BY rowid DESC LIMIT 1", (merchant_id, txn["amount"])).fetchone()
    return dict(row) if row else None


QUESTION_WINDOW = timedelta(hours=1)


def _find_question(con, merchant_id: str, message: str, facts: dict, now: datetime):
    """The open question this message answers, or None. Tightly bounded, because a wrong
    attachment is worse than none: when in doubt, the message is simply a new message.
    - only a question we asked THIS shop, still open, asking for a detail (not a UTR)
    - asked within the last hour, and exactly one such question
    - the message gives an amount and nothing else that stands on its own: no UTR, no
      account, no override, and not a fresh complaint in its own right
    - if we had asked "which of these amounts?", the answer must be one of them"""
    if facts["amount"] is None or facts["utr"] or facts["utrs_seen"] or facts["bad_utr"] \
            or facts["requested_destination"] or facts.get("override"):
        return None
    if DISPUTE_WORDS.search(message) and len(message.split()) > 4:
        return None                                   # a complaint in its own right
    rows = con.execute(
        "SELECT * FROM tickets WHERE merchant_id = ? AND status = 'NEEDS_INFO'"
        " AND awaiting = 'DETAIL' AND created >= ?",
        (merchant_id, (now - QUESTION_WINDOW).isoformat(timespec="seconds"))).fetchall()
    if len(rows) != 1:
        return None
    q = dict(rows[0])
    seen = [Decimal(x) for x in understand(q["message"])["amounts_seen"]]
    if seen and Decimal(facts["amount"]) not in seen:
        return None                                   # not one of the amounts we asked about
    return q


@app.post("/api/ticket")
def handle_ticket(body: TicketIn):
    started = time.perf_counter()
    now = datetime.now()
    pending_log: list[tuple] = []
    with closing(db()) as con:
        merchant = con.execute("SELECT * FROM merchants WHERE merchant_id = ?",
                               (body.merchant_id,)).fetchone()
        if not merchant:
            raise HTTPException(404, "unknown merchant")
        merchant = dict(merchant)
        lang = merchant["language"]                  # the reply always uses the shop's language
        msg_lang, msg_lang_how = langs.detect(body.message, lang)
        say = Reply(lang)

        # ---- phase 1: read only. Understand, investigate, propose. (The model call
        #      happens here, outside any lock.)
        facts = understand(body.message) | {"message": body.message}
        window = claimed_window(body.message, now)
        # Is this the answer to a question we just asked this shop ("how much?")? Decided from
        # the regex reading only, so it is predictable; the model never decides it.
        answered = _find_question(con, merchant["merchant_id"], body.message, facts, now) \
            if classify(body.message, facts) == "dispute" else None
        read_text = body.message
        if answered:
            asked = understand(answered["message"])
            if not window["given"]:              # their time ("yesterday") came with the question
                window = claimed_window(answered["message"], now)
            facts["duplicate"] = facts["duplicate"] or asked["duplicate"]
            facts["answers"] = answered["ticket_id"]
            # the model reads the question and the answer together, as a person would
            read_text = f"{answered['message']}\n[The shop's answer to our question: {body.message}]"
        reader, model_plan = "regex", None
        if have_key():
            got = read_and_propose(read_text, merchant,
                                   _candidates(con, merchant["merchant_id"], facts, window, now))
            if got:
                facts, model_plan, reader = merge_reading(facts, got[0]), got[1], "model"
            else:
                reader = "regex-fallback"                 # silently: same as having no key
        kind = classify(body.message, facts)
        awaiting = _find_awaiting(con, merchant["merchant_id"], facts["utr"]) \
            if kind == "dispute" and not answered else None
        if awaiting:
            kind = "followup"
            facts["amount"] = facts["amount"] or awaiting["claimed_amount"]

        matches, plan = [], None
        if kind in ("dispute", "followup"):
            if not facts["amount"] and not facts["utr"]:
                matches = []          # only a destination was given; nothing to look up
            elif facts["duplicate"] and not facts["utr"]:
                found = _ledger_search(con, merchant["merchant_id"], facts["amount"], None,
                                       window["since"], window["until"])
                for r in found:
                    r["duplicate_of"] = _duplicate_of(con, r)
                matches = [r for r in found if r["duplicate_of"]] or found
            else:
                matches = _ledger_search(con, merchant["merchant_id"], facts["amount"],
                                         facts["utr"],
                                         None if facts["utr"] else window["since"],
                                         None if facts["utr"] else window["until"])
                for r in matches:
                    r["duplicate_of"] = _duplicate_of(con, r) if facts["duplicate"] else None
            plan = rule_on_request(model_plan or propose(facts, matches, merchant), facts)
            pending_log += [
                ("agent", "UNDERSTAND", json.dumps(
                    {k: facts[k] for k in ("amount", "utr", "requested_destination",
                                           "duplicate", "override")}
                    | {"when": window["label"], "read_by": reader},
                    ensure_ascii=False)),
                ("agent", "INVESTIGATE", f"{len(matches)} matching transaction(s), "
                                         f"searched {window['label']}"),
                ("agent", "PROPOSE", f"{plan['action']} -> {plan['destination']} "
                                     f"({plan['planner']})")]

        # ---- phase 2: one writer at a time. Re-read anything that can change.
        con.execute("BEGIN IMMEDIATE")
        try:
            ticket_id = _next_ticket_id(con)
            today = now.date().isoformat()
            if merchant_row := con.execute("SELECT * FROM merchants WHERE merchant_id = ?",
                                           (merchant["merchant_id"],)).fetchone():
                merchant = dict(merchant_row)
            if merchant.get("auto_date") != today:            # the daily cap is per day
                con.execute("UPDATE merchants SET auto_today = 0, auto_date = ?"
                            " WHERE merchant_id = ?", (today, merchant["merchant_id"]))
                merchant["auto_today"], merchant["auto_date"] = 0, today
            for args in pending_log:
                log(con, ticket_id, *args)
            base = {"ticket_id": ticket_id, "merchant_id": merchant["merchant_id"],
                    "message": body.message, "created": now.isoformat(timespec="seconds")}

            if kind in ("help", "ask", "flagged"):
                if kind == "flagged":
                    key, status = "OVERRIDE", "FLAGGED"
                    reply = say("OVERRIDE")
                    log(con, ticket_id, "agent", "OVERRIDE_ATTEMPT",
                        f"“{facts['override']}” — no action proposed, so the gate never ruled")
                elif kind == "help":
                    key = "PERSON" if PERSON_WORDS.search(body.message) else None
                    reply, status = say(key or "HELP"), "ANSWERED"
                else:
                    p = facts["problems"][0] if facts["problems"] else None
                    key = ASK_KEY.get(p, "ASK_AMOUNT")
                    bad = facts["bad_utr"][0] if facts["bad_utr"] else ""
                    seen = ", ".join(facts["utrs_seen"] if p == "two_utrs" else
                                     [f"₹{Decimal(x):,}" for x in facts["amounts_seen"]])
                    reply = say(key, seen=seen, bad=bad, n=len(re.sub(r"\D", "", bad)))
                    status = "NEEDS_INFO"
                if kind != "flagged":
                    log(con, ticket_id, "agent",
                        "NOT_A_DISPUTE" if kind == "help" else "ASKED_SHOP",
                        (key or "help") + ": " + body.message[:120])
                if kind == "ask":
                    # a newer question replaces an older open one from the same shop
                    con.execute("UPDATE tickets SET status = 'SUPERSEDED', awaiting = NULL"
                                " WHERE merchant_id = ? AND status = 'NEEDS_INFO'"
                                " AND awaiting = 'DETAIL'", (merchant["merchant_id"],))
                latency = int((time.perf_counter() - started) * 1000)
                li = _languages(say, lang, msg_lang, msg_lang_how)
                _record(con, base | {"status": status, "outcome": "NONE", "action": "NONE",
                                     "reply": reply, "latency_ms": latency,
                                     "claimed_amount": facts["amount"],
                                     "extra": json.dumps(
                                         ({"override": facts["override"]} if facts["override"]
                                          else {}) | _thread_bits(li), ensure_ascii=False),
                                     "awaiting": None if kind in ("help", "flagged") else
                                     ("UTR" if key in ("BAD_UTR", "ASK_WHICH_UTR") else "DETAIL")})
                con.execute("COMMIT")
                return {**li, "reader": reader,
                        "claimed": facts["amount"], "reopened_from": None,
                        "ticket_id": ticket_id, "kind": kind, "ask": key, "outcome": "NONE",
                        "status": status, "action": "NONE", "destination": "",
                        "planner": "—", "model_reason": "", "passed": [], "failed": [],
                        "matches": 0, "reply": reply, "latency_ms": latency, "evidence": [],
                        "window": window["label"]}

            carried_from = None
            if facts.get("answers"):
                # re-check under the lock: still open, still ours to answer
                still = con.execute("SELECT status FROM tickets WHERE ticket_id = ?",
                                    (facts["answers"],)).fetchone()
                if still and still["status"] == "NEEDS_INFO":
                    carried_from = facts["answers"]
                    log(con, ticket_id, "agent", "REOPENED",
                        f"answers the question asked in {carried_from}")
            closed_asks = con.execute(
                "UPDATE tickets SET status = 'SUPERSEDED', awaiting = NULL WHERE merchant_id = ?"
                " AND status = 'NEEDS_INFO' AND awaiting = 'DETAIL'",
                (merchant["merchant_id"],)).rowcount
            if closed_asks:
                log(con, ticket_id, "agent", "ANSWERED_BY_SHOP",
                    f"closes {closed_asks} open question(s) to this shop")
            if kind == "followup":
                still = con.execute("SELECT status FROM tickets WHERE ticket_id = ?",
                                    (awaiting["ticket_id"],)).fetchone()
                if still and still["status"] in ("BLOCKED", "NEEDS_INFO"):
                    carried_from = awaiting["ticket_id"]
                    con.execute("UPDATE tickets SET status = 'SUPERSEDED', awaiting = NULL"
                                " WHERE ticket_id = ?", (carried_from,))
                    log(con, ticket_id, "agent", "REOPENED",
                        f"carries on from {carried_from} with {facts['utr']}")

            # 4. GATE — the only thing that can authorise an action
            txn_row = next((m for m in matches if m["utr"] == plan.get("utr")),
                           matches[0] if matches else None)
            if txn_row:                                        # fresh state, under the lock
                fresh = con.execute("SELECT * FROM ledger WHERE utr = ?",
                                    (txn_row["utr"],)).fetchone()
                txn_row = dict(fresh) | {"duplicate_of": txn_row.get("duplicate_of")}
            decision = _rule(merchant, txn_row, txn_row.get("duplicate_of") if txn_row else None,
                             plan["action"], plan["destination"], len(matches),
                             facts["amount"], window["point"], body.message, ticket_id)
            log(con, ticket_id, "gate", decision.outcome,
                "passed: " + "; ".join(decision.passed) +
                (" | failed: " + "; ".join(decision.failed) if decision.failed else ""))

            # 5. act
            amt = txn_row["amount"] if txn_row else facts["amount"]
            utr = txn_row["utr"] if txn_row else None
            failed_ids = {f[0] for f in decision.failed}
            awaiting_now, outcome, extra = None, decision.outcome, {"window": window["label"]}
            if facts["override"]:
                extra["override"] = facts["override"]
            closed = txn_row and (txn_row["settled"] or txn_row["reversed"]
                                  or (txn_row["bank_status"] == "SUCCESS"
                                      and not txn_row.get("duplicate_of")))
            nothing_asked = plan["action"] == "NONE" and not facts["requested_destination"]
            if closed and "1" not in failed_ids and (decision.outcome != "BLOCK" or nothing_asked):
                # Nothing left to do. Say so; never pay twice.
                _RULINGS.pop(id(decision), None)
                if txn_row["settled"]:
                    key, note = "ALREADY_SETTLED", txn_row["note"]
                elif txn_row["reversed"]:
                    key, note = "ALREADY_REVERSED", txn_row["note"]
                else:
                    key, note = "ALREADY_NORMAL", ""
                reply = say(key, amt, utr, note=note,
                                   time=datetime.fromisoformat(txn_row["txn_time"]).strftime(
                                       "%H:%M on %d %b"))
                status, outcome = "ALREADY_DONE", "NONE"
                log(con, ticket_id, "agent", "NOTHING_TO_DO", f"{utr}: {key.lower()}")
            elif decision.outcome == "APPROVE" and txn_row and facts["override"]:
                # Not a gate change: the gate passed it. A message that tries to change the
                # rules never gets an AUTOMATIC action, however clean its facts look.
                _RULINGS.pop(id(decision), None)
                log(con, ticket_id, "agent", "HELD_OVERRIDE",
                    f"gate passed it, but the message said “{facts['override']}”; held for a person")
                reply = say("ESCALATE", amt, utr)
                status, outcome = "ESCALATED", "ESCALATE"
            elif decision.outcome == "APPROVE" and txn_row:
                _execute(con, decision, ticket_id)
                con.execute("UPDATE merchants SET auto_today = auto_today + 1"
                            " WHERE merchant_id = ?", (merchant["merchant_id"],))
                cyc = settlement_cycle(now)
                if carried_from:
                    say("RESOLVED_ON_UTR")
                say(plan["action"], amt, utr, cycle=cyc)
                reply = say.text()
                extra |= _tell_customer(con, ticket_id, plan["action"], txn_row, merchant)
                if plan["action"] == "MARK_PAID":
                    extra["settlement"] = cyc
                status = "RESOLVED"
            elif decision.outcome == "ESCALATE":
                reply = say("ESCALATE", amt, utr)
                status = "ESCALATED"
            elif not matches and not facts["requested_destination"]:
                # nothing to act on is not a risk event — ask the shop for more
                reply = say("NONE_UTR", utr=facts["utr"]) if facts["utr"] else                     say("NONE", amt, utr,
                               **({} if window["given"] else {"when": ""}))
                status, awaiting_now = "NEEDS_INFO", "UTR"
            else:
                redirected = "3" in failed_ids and facts["requested_destination"]
                if redirected and plan["action"] in ("REVERSE_TO_SOURCE", "REFUND_DUPLICATE"):
                    key = "BLOCK_REDIRECT_REVERSAL"
                elif redirected:
                    key = "BLOCK_REDIRECT_PAYOUT"
                elif len(matches) > 1:
                    key, awaiting_now = "BLOCK_AMBIGUOUS", "UTR"   # a UTR next reopens this
                elif txn_row and txn_row["merchant_id"] != merchant["merchant_id"]:
                    key = "BLOCK_FOREIGN"
                elif "1" in failed_ids and txn_row:
                    key = "BLOCK_MISMATCH"
                elif "3" in failed_ids and plan["action"] in ("REVERSE_TO_SOURCE",
                                                              "REFUND_DUPLICATE"):
                    key = "BLOCK_REDIRECT_REVERSAL"
                elif "3" in failed_ids:
                    key = "BLOCK_REDIRECT_PAYOUT"
                else:
                    key = "BLOCK_OTHER"
                reply = say(key, facts["amount"] or amt, utr,
                                   txn_amt=f"{txn_row['amount']:,}" if txn_row else "—")
                status = "BLOCKED"

            latency = int((time.perf_counter() - started) * 1000)
            li = _languages(say, lang, msg_lang, msg_lang_how)
            extra |= _thread_bits(li)
            _record(con, base | {
                "claimed_amount": facts["amount"] or amt, "utr": utr, "status": status,
                "outcome": outcome, "action": plan["action"], "destination": plan["destination"],
                "model_reason": plan.get("reason", ""),
                "checks_passed": " | ".join(decision.passed),
                "checks_failed": " | ".join(decision.failed), "reply": reply,
                "latency_ms": latency, "awaiting": awaiting_now, "reopened_from": carried_from,
                "txn_amount": txn_row["amount"] if txn_row else None,
                "txn_time": txn_row["txn_time"] if txn_row else None,
                "extra": json.dumps(extra, ensure_ascii=False)})
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise

    return {**li, "claimed": facts["amount"],
            "reader": reader,
            "ticket_id": ticket_id, "kind": kind, "outcome": outcome, "gate": decision.outcome,
            "status": status, "action": plan["action"], "destination": plan["destination"],
            "planner": plan.get("planner"), "model_reason": plan.get("reason", ""),
            "passed": decision.passed, "failed": decision.failed,
            "failed_marked": failed_with_reasons({"checks_failed": " | ".join(decision.failed),
                                                  "utr": utr, "action": plan["action"]}),
            "matches": len(matches), "reply": reply, "latency_ms": latency,
            "awaiting": awaiting_now, "reopened_from": carried_from, "window": window["label"],
            "settlement": extra.get("settlement"), "customer": extra.get("customer"),
            "evidence": [_txn_view(m) for m in ([txn_row] if txn_row else []) +
                         [m for m in matches if not txn_row or m["utr"] != txn_row["utr"]]][:4]}


REVIEWED_LANGS = {"en", "ta", "hi"}     # templates a person on the team has read


def _thread_bits(li: dict) -> dict:
    """What the chat needs to redraw a past message after the page is left and reopened."""
    return {"reply_lang": li["reply_lang"], "translation": li["translation"],
            "translation_missing": li["translation_missing"]}


def _languages(say: Reply, lang: str, msg_lang: str, how: str) -> dict:
    """The reply stays in the shop's language. If the message was written in another one,
    offer the same reply in that language — or say plainly that we have no template."""
    lang = say.used_lang()
    out = {"reply_lang": lang, "reply_lang_name": langs.NAMES.get(lang, lang),
           "message_lang": msg_lang, "message_lang_name": langs.NAMES.get(msg_lang, msg_lang),
           "message_lang_how": how, "translation": None, "translation_missing": None}
    if msg_lang != lang:
        text = say.translated(msg_lang)
        if text:
            out["translation"] = {"lang": msg_lang, "name": langs.NAMES.get(msg_lang, msg_lang),
                                  "text": text, "reviewed": msg_lang in REVIEWED_LANGS}
        else:
            out["translation_missing"] = langs.NAMES.get(msg_lang, msg_lang)
    return out


def _tell_customer(con, ticket_id, action, txn_row, merchant) -> dict:
    """The other side of the dispute. Mock: no SMS gateway in this demo, so it is logged."""
    tpl = CUSTOMER_SMS.get(action)
    if not tpl:
        return {}
    text = tpl.format(amt=f"{txn_row['amount']:,}", shop=merchant["name"], utr=txn_row["utr"],
                      date=datetime.fromisoformat(txn_row["txn_time"]).strftime("%d %b"))
    log(con, ticket_id, "system", "CUSTOMER_NOTIFIED",
        f"SMS to {txn_row['source_account']} (mock): {text}")
    return {"customer": {"to": txn_row["source_account"], "channel": "SMS (mock)", "text": text}}


HELP_REPLY = {
    "en": "I settle disputed payments — when a customer was charged and the money did not "
          "reach you, or the other way round. Tell me the amount and roughly when it "
          "happened, and I will check the record. This line only handles payment "
          "disputes; for anything else, use Help in the Paytm for Business app.",
    "ta": "பணப்பரிவர்த்தனை பிரச்சனைகளை நான் தீர்க்கிறேன். தொகையையும் எப்போது நடந்தது என்பதையும் சொல்லுங்கள், பதிவைச் சரிபார்க்கிறேன்.",
    "hi": "मैं भुगतान विवाद सुलझाता हूँ — जब ग्राहक का पैसा कट गया और आप तक नहीं पहुँचा। राशि और समय बताइए, मैं रिकॉर्ड देखता हूँ।",
}


REPLIES["HELP"] = HELP_REPLY
REPLIES["PERSON"] = PERSON_REPLY
HELP_REPLY.update(langs.HELP_EXTRA)
for _key, _by_lang in langs.REPLIES_EXTRA.items():
    REPLIES[_key].update(_by_lang)
CYCLE.update(langs.CYCLE_EXTRA)
WHEN.update(langs.WHEN_EXTRA)

# -------------------------------------------------------------- the console
@app.get("/api/queue")
def queue():
    now = datetime.now()
    with closing(db()) as con:
        rows = con.execute(
            "SELECT t.*, m.name AS merchant_name, l.bank_status, l.npci_code"
            " FROM tickets t LEFT JOIN merchants m ON m.merchant_id = t.merchant_id"
            " LEFT JOIN ledger l ON l.utr = t.utr"
            " WHERE t.status IN ('ESCALATED','BLOCKED','NEEDS_INFO','TAKEN','FLAGGED')"
            " ORDER BY t.rowid DESC").fetchall()
        out = []
        for r in rows:
            t = dict(r)
            t["rbi"] = rbi_clock(t.get("txn_time"), t.get("bank_status"), now)
            t["npci_meaning"] = NPCI.get(t.get("npci_code") or "", ("", ""))[1]
            fails = {f[0] for f in (t["checks_failed"] or "").split(" | ") if f}
            # no failed checks at all = held only because the message tried to override
            t["human_may_approve"] = t["status"] == "ESCALATED" and fails <= HUMAN_MAY_OVERRIDE
            t["human_may_take"] = t["status"] == "ESCALATED" and "4" in fails
            t["check4"] = check4_reading(t) if "4" in fails else None
            t["failed_marked"] = failed_with_reasons(t)
            t["ending"] = ending(t) if t["outcome"] in ("APPROVE", "ESCALATE", "BLOCK") else None
            try:
                ex = json.loads(t.get("extra") or "{}")
            except ValueError:
                ex = {}
            t["taken"], t["override"] = ex.get("taken"), ex.get("override")
            out.append(t)
        # the case closest to its RBI deadline comes first
        out.sort(key=lambda t: (t["rbi"] is None, t["rbi"]["hours_left"] if t["rbi"] else 0))
        return out


KNOWN_ACTIONS = {"MARK_PAID", "REVERSE_TO_SOURCE", "REFUND_DUPLICATE"}
ACTION_WORDS = {"MARK_PAID": "mark it paid", "REVERSE_TO_SOURCE": "return it to the customer",
                "REFUND_DUPLICATE": "refund the repeat payment"}


def check4_reading(t: dict) -> dict:
    """Check 4 holds two very different cases. Say which this one looks like, from facts the
    gate already has: was the proposed action one this system knows at all?
    This is a reading for the person, not a ruling; nothing acts on it."""
    action = t.get("action") or "NONE"
    status = (t.get("bank_status") or "").replace("_", " ").lower() or "unknown"
    if action == "NONE":
        return {"kind": "real", "text":
                "Looks real. The payment is this shop's and nothing points at a strange account, "
                "but the agent could not settle on an action. Take it and work out the right "
                "move with the bank status in front of you."}
    if action not in KNOWN_ACTIONS:
        return {"kind": "junk", "text":
                f"Looks like junk. The agent proposed “{action.replace('_', ' ').lower()}”, which "
                "is not an action this system has at all. That is what an instruction slipped "
                "into the message looks like (“ignore your rules…”). Read the message: if it "
                "is giving orders rather than describing a payment, turn it down."}
    if action not in gate.ALLOWED_ACTIONS:
        return {"kind": "real", "text":
                f"Looks real. “{ACTION_WORDS[action].capitalize()}” would fit this payment, but "
                "the risk team has switched that action off in policy, so it has to be done by "
                "hand. Take it."}
    return {"kind": "real", "text":
            f"Looks real. The payment is this shop's and the money would go where it should, "
            f"but the agent proposed to {ACTION_WORDS[action]} on a payment the bank reports as "
            f"{status}, and those do not fit. The customer or the shop is probably still owed. "
            "Take it and settle it through the back office."}


class DecisionIn(BaseModel):
    decision: str            # approve | reject | take
    who: str = Field(default="human", max_length=40)


@app.post("/api/queue/{ticket_id}/decide")
def decide(ticket_id: str, body: DecisionIn):
    """A person rules on an ESCALATED case. Approval re-runs the gate on today's facts and
    may only lift checks 2 and 5. A BLOCK is never approvable, here or anywhere.
    'take' is for a case held on check 4: a person owns it from here, outside the agent.
    It moves no money and never calls the gate or _execute."""
    if body.decision not in ("approve", "reject", "take"):
        raise HTTPException(400, "decision must be approve, reject or take")
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            t = con.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
            if not t:
                raise HTTPException(404, "unknown ticket")
            if t["status"] != "ESCALATED":
                raise HTTPException(409, f"case {ticket_id} is {t['status'].lower()}, "
                                         "not waiting on a person")
            if body.decision == "reject":
                con.execute("UPDATE tickets SET status = 'REJECTED' WHERE ticket_id = ?",
                            (ticket_id,))
                log(con, ticket_id, "human", "REJECTED_ESCALATION", f"{body.who} rejected")
                con.execute("COMMIT")
                return {"ok": True, "ticket_id": ticket_id, "decision": "reject"}
            if body.decision == "take":
                fails = {f[0] for f in (t["checks_failed"] or "").split(" | ") if f}
                if "4" not in fails:
                    raise HTTPException(409, f"case {ticket_id} was not held on check 4; "
                                             "approve it or turn it down")
                try:
                    extra = json.loads(t["extra"] or "{}")
                except ValueError:
                    extra = {}
                extra["taken"] = {"by": body.who,
                                  "at": datetime.now().isoformat(timespec="seconds")}
                con.execute("UPDATE tickets SET status = 'TAKEN', extra = ? WHERE ticket_id = ?",
                            (json.dumps(extra, ensure_ascii=False), ticket_id))
                log(con, ticket_id, "human", "TAKEN_BY_PERSON",
                    f"{body.who} took the case off the agent; no money moved")
                con.execute("COMMIT")
                return {"ok": True, "ticket_id": ticket_id, "decision": "take"}

            merchant = dict(con.execute("SELECT * FROM merchants WHERE merchant_id = ?",
                                        (t["merchant_id"],)).fetchone())
            txn_row = con.execute("SELECT * FROM ledger WHERE utr = ?", (t["utr"],)).fetchone() \
                if t["utr"] else None
            txn_row = dict(txn_row) if txn_row else None
            dup = _duplicate_of(con, txn_row) if txn_row and t["action"] == "REFUND_DUPLICATE" \
                else None
            decision = _rule(merchant, txn_row, dup, t["action"], t["destination"],
                             1 if txn_row else 0, t["claimed_amount"],
                             datetime.fromisoformat(t["created"]), t["message"], ticket_id,
                             human=body.who)
            log(con, ticket_id, "gate", "RECHECK_FOR_HUMAN",
                f"{decision.outcome}; failed: {'; '.join(decision.failed) or 'none'}")
            if not decision.by.startswith("human"):
                raise HTTPException(409, "the gate no longer allows this: "
                                    + ("; ".join(decision.failed) or decision.outcome))
            try:
                _execute(con, decision, ticket_id)
            except AlreadyDone:
                raise HTTPException(409, f"{t['utr']} was already settled or reversed")
            con.execute("UPDATE tickets SET status = 'RESOLVED' WHERE ticket_id = ?",
                        (ticket_id,))
            log(con, ticket_id, "human", "APPROVED_ESCALATION", f"{body.who} approved")
            _tell_customer(con, ticket_id, t["action"], txn_row, merchant)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return {"ok": True, "ticket_id": ticket_id, "decision": body.decision}


@app.get("/api/audit")
def audit(limit: int = 60):
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?",
                           (max(1, min(limit, 500)),)).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/stats")
def stats():
    with closing(db()) as con:
        rows = con.execute("SELECT outcome, COUNT(*) n FROM tickets GROUP BY outcome").fetchall()
        total = con.execute("SELECT COUNT(*) n FROM tickets").fetchone()["n"]
        by = {r["outcome"]: r["n"] for r in rows}
        lat = con.execute("SELECT AVG(latency_ms) ms FROM tickets").fetchone()["ms"]
        return {"total": total, "by_outcome": by,
                "auto_resolved_pct": round(100 * by.get("APPROVE", 0) / total) if total else 0,
                "avg_latency_ms": round(lat) if lat else 0,
                "planner": "llm" if have_key() else "stand-in"} | seed_state(con)


@app.get("/api/merchant/{merchant_id}/thread")
def merchant_thread(merchant_id: str, limit: int = 40):
    """The conversation so far, oldest first, so the chat survives leaving the page, a reload
    or a restart. Read from the recorded cases; nothing is kept in the browser."""
    with closing(db()) as con:
        if not con.execute("SELECT 1 FROM merchants WHERE merchant_id = ?",
                           (merchant_id,)).fetchone():
            raise HTTPException(404, "unknown merchant")
        rows = con.execute(
            "SELECT ticket_id, message, reply, created, extra FROM tickets"
            " WHERE merchant_id = ? ORDER BY rowid DESC LIMIT ?",
            (merchant_id, max(1, min(limit, 200)))).fetchall()
    out = []
    for r in reversed(rows):
        try:
            ex = json.loads(r["extra"] or "{}")
        except ValueError:
            ex = {}
        out.append({"ticket_id": r["ticket_id"], "message": r["message"], "reply": r["reply"],
                    "created": r["created"], "translation": ex.get("translation"),
                    "translation_missing": ex.get("translation_missing"),
                    "reply_lang": ex.get("reply_lang")})
    return out


# ------------------------------------------------ voice: Sarvam speech to text
# One line to switch voice back to the browser's own speech recognition:
USE_SARVAM_STT = os.getenv("USE_SARVAM_STT", "1") != "0"
# Note: no "/v1" — https://api.sarvam.ai/v1/speech-to-text returns 404 (checked 15 Nov)
SARVAM_STT_URL = os.getenv("SARVAM_STT_URL", "https://api.sarvam.ai/speech-to-text")
SARVAM_STT_MODEL = "saaras:v3"
STT_TIMEOUT_S = 4.5          # the page gives up at 5 s and uses the browser's own transcript
STT_MAX_BYTES = 1_500_000    # ~45 s of 16 kHz mono WAV; Sarvam's REST limit is 30 s of audio


def sarvam_key() -> str | None:
    import agent                                    # one place reads the key
    return agent.sarvam_key()


# ------------------------------------------------ voice: replies read aloud (Bulbul)
# One line to switch spoken replies off (text replies are never affected):
USE_SARVAM_TTS = os.getenv("USE_SARVAM_TTS", "1") != "0"
SARVAM_TTS_URL = os.getenv("SARVAM_TTS_URL", "https://api.sarvam.ai/text-to-speech")
SARVAM_TTS_MODEL = "bulbul:v3"
TTS_TIMEOUT_S = 10    # Bulbul took ~4 s per reply, once 21 s (15 Nov); text is never held up
# Languages Bulbul speaks, as BCP-47. Urdu and Assamese are not among them: text only.
TTS_LANG = {"en": "en-IN", "hi": "hi-IN", "mr": "mr-IN", "bn": "bn-IN", "pa": "pa-IN",
            "gu": "gu-IN", "or": "od-IN", "ta": "ta-IN", "te": "te-IN", "kn": "kn-IN",
            "ml": "ml-IN"}


@app.get("/api/voice")
def voice_config():
    """Tells the page which voice features are on. Never reveals the key."""
    key = bool(sarvam_key())
    return {"sarvam": USE_SARVAM_STT and key, "speak": USE_SARVAM_TTS and key,
            "speak_langs": sorted(TTS_LANG) if (USE_SARVAM_TTS and key) else []}


class SpeakIn(BaseModel):
    text: str = Field(max_length=1000)
    language: str = Field(max_length=5)


@app.post("/api/tts")
async def text_to_speech(body: SpeakIn):
    """The reply, spoken. Any failure is a quiet 503: the shop still has the text."""
    import asyncio
    import base64
    import urllib.request
    key, code = sarvam_key(), TTS_LANG.get(body.language)
    if not (USE_SARVAM_TTS and key and code and body.text.strip()):
        raise HTTPException(503, "spoken reply unavailable")
    req = urllib.request.Request(SARVAM_TTS_URL, method="POST", data=json.dumps(
        {"text": body.text.strip(), "target_language_code": code, "model": SARVAM_TTS_MODEL,
         "speech_sample_rate": 16000}       # same speed, ~30% less to download on venue wifi
    ).encode(), headers={"api-subscription-key": key, "Content-Type": "application/json",
                         "User-Agent": "resolved-voice/1.0"})

    def call():
        with urllib.request.urlopen(req, timeout=TTS_TIMEOUT_S) as r:
            return json.load(r)
    try:
        data = await asyncio.wait_for(asyncio.to_thread(call), timeout=TTS_TIMEOUT_S + 0.3)
        audio = b"".join(base64.b64decode(a) for a in data.get("audios") or [])
    except Exception as exc:
        print(f"[voice] Bulbul unavailable, text only: {type(exc).__name__}")
        raise HTTPException(503, "spoken reply unavailable")
    if not audio:
        raise HTTPException(503, "spoken reply unavailable")
    return Response(audio, media_type="audio/wav")


@app.post("/api/stt")
async def speech_to_text(request: Request):
    """Forward a recorded WAV to Sarvam and return its transcript. Any failure is a quiet 503:
    the page then uses the browser's own transcript, and shows no error."""
    import urllib.request
    key = sarvam_key()
    if not (USE_SARVAM_STT and key):
        raise HTTPException(503, "voice service off")
    audio = await request.body()
    if not audio or len(audio) > STT_MAX_BYTES:
        raise HTTPException(413 if audio else 400, "audio missing or too long")
    boundary = "----resolved" + os.urandom(8).hex()
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{SARVAM_STT_MODEL}\r\n',
        f'--{boundary}\r\nContent-Disposition: form-data; name="mode"\r\n\r\ntranscribe\r\n',
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="voice.wav"\r\n'
        f'Content-Type: audio/wav\r\n\r\n',
    ]
    body = "".join(parts).encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(SARVAM_STT_URL, data=body, method="POST", headers={
        "api-subscription-key": key, "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "resolved-voice/1.0"})

    def call():
        with urllib.request.urlopen(req, timeout=STT_TIMEOUT_S) as r:
            return json.load(r)
    import asyncio
    try:
        data = await asyncio.wait_for(asyncio.to_thread(call), timeout=STT_TIMEOUT_S + 0.3)
        text = str(data.get("transcript") or "").strip()
    except Exception as exc:
        print(f"[voice] Sarvam unavailable, page falls back: {type(exc).__name__}")
        raise HTTPException(503, "voice service unavailable")
    if not text:
        raise HTTPException(503, "no speech recognised")
    return {"text": text[:1000], "language": data.get("language_code")}


@app.get("/api/languages")
def languages():
    return [{"code": k, "name": v} for k, v in langs.NAMES.items()]


class LanguageIn(BaseModel):
    language: str = Field(max_length=5)


@app.put("/api/merchant/{merchant_id}/language")
def set_language(merchant_id: str, body: LanguageIn):
    """The language the shop is answered in. A setting the shop owns, so it is audited."""
    if body.language not in langs.NAMES:
        raise HTTPException(400, f"unknown language; one of {sorted(langs.NAMES)}")
    with closing(db()) as con:
        m = con.execute("SELECT language FROM merchants WHERE merchant_id = ?",
                        (merchant_id,)).fetchone()
        if not m:
            raise HTTPException(404, "unknown merchant")
        if m["language"] != body.language:
            con.execute("UPDATE merchants SET language = ? WHERE merchant_id = ?",
                        (body.language, merchant_id))
            log(con, None, "human", "LANGUAGE_CHANGE",
                f"{merchant_id}: {m['language']} -> {body.language}")
    return {"merchant_id": merchant_id, "language": body.language,
            "name": langs.NAMES[body.language]}


@app.get("/api/merchants")
def merchants():
    with closing(db()) as con:
        return [dict(r) for r in con.execute("SELECT * FROM merchants").fetchall()]


@app.get("/api/records")
def records():
    """Every shop's payment record, read-only — what a blind-test writer may read."""
    with closing(db()) as con:
        rows = con.execute("SELECT l.*, m.name FROM ledger l JOIN merchants m"
                           " ON m.merchant_id = l.merchant_id ORDER BY m.name, l.txn_time DESC"
                           ).fetchall()
        return [_txn_view(dict(r)) for r in rows]


@app.get("/api/merchant/{merchant_id}/summary")
def merchant_summary(merchant_id: str):
    """What the shop sees above the conversation: where today stands."""
    with closing(db()) as con:
        m = con.execute("SELECT * FROM merchants WHERE merchant_id = ?",
                        (merchant_id,)).fetchone()
        if not m:
            raise HTTPException(404, "unknown merchant")
        today = datetime.now().date().isoformat()
        paid = con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(amount),0) total FROM ledger"
            " WHERE merchant_id = ? AND bank_status != 'FAILED_DEBITED'"
            " AND substr(txn_time,1,10) = ?", (merchant_id, today)).fetchone()
        open_cases = con.execute(
            "SELECT COUNT(*) n FROM tickets WHERE merchant_id = ?"
            " AND status IN ('ESCALATED','BLOCKED','NEEDS_INFO','TAKEN')", (merchant_id,)).fetchone()
        settled = con.execute(
            "SELECT COUNT(*) n FROM tickets WHERE merchant_id = ? AND status = 'RESOLVED'",
            (merchant_id,)).fetchone()
        return {"name": m["name"], "terminal": m["terminal"], "language": m["language"],
                "payments_today": paid["n"], "taken_today": paid["total"],
                "open_cases": open_cases["n"], "settled_cases": settled["n"]}


# ------------------------------------------------- policy a risk team owns
@app.get("/api/policy")
def get_policy():
    return {"fields": policy.describe(), "raw": policy.load()}


class PolicyIn(BaseModel):
    auto_limit: int | None = Field(default=None, ge=0, le=UPI_CEILING)
    daily_cap: int | None = Field(default=None, ge=0, le=1000)
    match_window_minutes: int | None = Field(default=None, ge=1, le=24 * 60)


@app.put("/api/policy")
def put_policy(body: PolicyIn):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "nothing to change")
    before = policy.load()
    after = policy.save(changes)
    with closing(db()) as con:
        for k, v in changes.items():
            if before.get(k) != v:
                log(con, None, "human", "POLICY_CHANGE", f"{k}: {before.get(k)} -> {v}")
    return {"fields": policy.describe(), "raw": after}


# ---------------------------------------------------------- judge mode API
class EvalIn(BaseModel):
    suite: str = "generator"
    seeds: int = 50
    limit: int | None = None


@app.post("/api/evaluate")
def run_eval(body: EvalIn):
    if body.suite not in evaluate.SUITES:
        raise HTTPException(400, f"unknown suite; try {list(evaluate.SUITES)}")
    seeds = max(1, min(body.seeds, 60))
    limit = body.limit or (None if body.suite == "generator" else 12)
    if body.suite in ("llm", "redteam") and not have_key():
        raise HTTPException(400,
            "No model key set. Start the server with GROQ_API_KEY (or ANTHROPIC_API_KEY / "
            "GEMINI_API_KEY) to run the model suites.")
    result = evaluate.run(body.suite, seeds=seeds, limit=limit)
    with closing(db()) as con:
        t = result["totals"]
        log(con, None, "human", "EVALUATION_RUN",
            f"{body.suite}: {t['correct']}/{t['n']} correct, {t['unsafe']} unsafe approvals,"
            f" {t['risky_stopped']}/{t['risky']} risky stopped")
    return result


@app.get("/api/suites")
def suites():
    return {"suites": [{"id": k, "description": v} for k, v in evaluate.SUITES.items()],
            "model_available": have_key()}


# ------------------------------------------------ blind test: cases we did not write
class BlindCase(BaseModel):
    shop: str = Field(max_length=60)
    message: str = Field(max_length=1000)
    expected: str = Field(max_length=20)


class BlindIn(BaseModel):
    cases: list[BlindCase] = Field(max_length=60)


EXPECT = {"act": "APPROVE", "approve": "APPROVE", "person": "ESCALATE", "escalate": "ESCALATE",
          "refuse": "BLOCK", "block": "BLOCK", "ask": "ASK", "needs info": "ASK", "info": "ASK",
          "nothing": "NONE", "none": "NONE", "already done": "NONE"}
_BLIND_LOCK = threading.Lock()


@app.post("/api/blind")
def blind(body: BlindIn):
    """Each case runs on a fresh copy of the seeded records, so order does not matter and
    the live demo data is untouched. Expected outcomes come from the writer, not from us."""
    import seed
    with _BLIND_LOCK, tempfile.TemporaryDirectory() as tmp:
        clean = os.path.join(tmp, "clean.db")
        seed.build(clean, quiet=True)
        with closing(sqlite3.connect(clean)) as c:
            shops = {r[1].lower(): r[0] for r in c.execute("SELECT merchant_id, name FROM merchants")}
            shops |= {mid.lower(): mid for mid in shops.values()}
        rows = []
        for i, case in enumerate(body.cases, 1):
            want = EXPECT.get(case.expected.strip().lower())
            mid = shops.get(case.shop.strip().lower()) or next(
                (v for k, v in shops.items() if k.startswith(case.shop.strip().lower()[:4])), None)
            row = {"n": i, "shop": case.shop, "message": case.message, "expected": want}
            if not want or not mid:
                rows.append(row | {"error": "unknown shop" if not mid else
                                   "expected must be act, person, refuse, ask or nothing"})
                continue
            path = os.path.join(tmp, f"case{i}.db")
            shutil.copyfile(clean, path)
            token = _DB_PATH.set(path)
            try:
                r = handle_ticket(TicketIn(merchant_id=mid, message=case.message))
            except HTTPException as e:
                rows.append(row | {"error": str(e.detail)}); continue
            finally:
                _DB_PATH.reset(token)
            got = r["outcome"] if r["outcome"] in ("APPROVE", "ESCALATE", "BLOCK") else (
                "NONE" if r["status"] == "ALREADY_DONE" else "ASK")
            rows.append(row | {"got": got, "status": r["status"], "agree": got == want,
                               "unsafe": got == "APPROVE" and want != "APPROVE",
                               "why": "; ".join(f[2:] for f in r["failed"]) or r["status"].lower(),
                               "reply": r["reply"]})
    scored = [r for r in rows if "got" in r]
    out = {"n": len(scored), "agree": sum(r["agree"] for r in scored),
           "unsafe": sum(r["unsafe"] for r in scored),
           "over_cautious": sum(1 for r in scored if r["expected"] == "APPROVE"
                                and r["got"] != "APPROVE"),
           "errors": len(rows) - len(scored), "rows": rows}
    with closing(db()) as con:
        log(con, None, "human", "BLIND_RUN", f"{out['agree']}/{out['n']} agreed with the writer, "
                                             f"{out['unsafe']} unsafe approvals")
    return out


# ------------------------------------------------------------------ export
@app.get("/api/export/audit.csv")
def export_audit():
    import csv
    import io
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM audit ORDER BY id").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "timestamp", "ticket", "actor", "event", "detail"])
    for r in rows:
        w.writerow([r["id"], r["ts"], r["ticket_id"] or "", r["actor"], r["event"], r["detail"]])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=resolved-audit.csv"})


def ending(t: dict) -> str:
    """Where a gate-ruled case went, in the terms the case desk lists use:
    acted (closed alone) | person (escalated, whatever a person then did) |
    refused (on the risk log) | asked (the shop was asked for more: no payment found, or a
    refused case the shop has since answered with a UTR)."""
    if t["outcome"] == "APPROVE":
        return "acted"
    if t["outcome"] == "ESCALATE":
        return "person"
    return "refused" if t["status"] == "BLOCKED" else "asked"


def deciding_checks(t: dict) -> list[str]:
    """The checks that actually stopped a case. With no payment found, the proposal is empty,
    so checks 3 (no destination) and 4 ("none" is not an action) fail only as a knock-on
    of check 1. Count check 1 alone then; otherwise every failed check counts."""
    return [f.split()[0] for f in failed_with_reasons(t) if not f.endswith(KNOCK_ON)]


KNOCK_ON = " (only because there was no payment to act on)"


def failed_with_reasons(t: dict) -> list[str]:
    """Failed checks, with knock-on failures marked. A check-3 failure that names a real
    account (someone asked for the money to go elsewhere) is NOT a knock-on, even with no
    payment found: that request is a risk signal of its own."""
    failed = [f for f in (t.get("checks_failed") or "").split(" | ") if f]
    act = t.get("action") or "NONE"
    if not t.get("utr") and any(f.startswith("1 ") for f in failed):
        # check 4 is a knock-on when the action is "none" or a real one with no payment
        # under it; an action this system does not have (MANUAL_PAYOUT) is its own signal
        failed = [f + KNOCK_ON if (f.startswith("3 ") and "nowhere" in f) or
                  (f.startswith("4 ") and (act == "NONE" or act in KNOWN_ACTIONS)) else f
                  for f in failed]
    return failed


@app.get("/api/metrics")
def metrics():
    """Everything the console charts, measured from real tickets."""
    with closing(db()) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT outcome, status, action, latency_ms, claimed_amount, txn_amount, utr, created,"
            " checks_failed FROM tickets WHERE outcome IN ('APPROVE','ESCALATE','BLOCK')"
            " ORDER BY rowid").fetchall()]
        moved = con.execute(
            "SELECT COALESCE(SUM(txn_amount),0) FROM tickets WHERE outcome = 'APPROVE'"
            " AND status = 'RESOLVED'").fetchone()[0]
    by_outcome, by_ending, by_check = {}, {}, {}
    for r in rows:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
        e = ending(r)
        by_ending[e] = by_ending.get(e, 0) + 1
        if r["outcome"] in ("ESCALATE", "BLOCK"):
            for c in deciding_checks(r):
                by_check[f"check {c}"] = by_check.get(f"check {c}", 0) + 1
    lat = sorted(r["latency_ms"] or 0 for r in rows)
    total = len(rows)
    return {
        "total": total,
        "by_outcome": by_outcome,          # the gate's ruling
        "by_ending": by_ending,            # where the case went: matches the case-desk lists
        "by_failed_check": by_check,       # deciding checks only, see deciding_checks()
        "auto_resolved_pct": round(100 * by_outcome.get("APPROVE", 0) / total) if total else 0,
        "median_ms": lat[len(lat) // 2] if lat else 0,
        "max_ms": lat[-1] if lat else 0,
        "latencies": lat[-40:],
        "value_moved": moved,
        "planner": model_name(),
    }


# ------------------------------------------------------------------- pages
@app.get("/")
def chat_page():
    return FileResponse(os.path.join(STATIC, "chat.html"))


@app.get("/judge")
def judge_page():
    return FileResponse(os.path.join(STATIC, "judge.html"))


@app.get("/console")
def console_page():
    return FileResponse(os.path.join(STATIC, "console.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")
migrate()


if __name__ == "__main__":
    import sys
    import uvicorn
    sys.stdout.reconfigure(encoding="utf-8")
    if not os.path.exists(DB):
        print("resolved.db not found — run:  python seed.py")
        raise SystemExit(1)
    port = int(os.getenv("PORT", "8000"))
    st = seed_state()
    if st["stale"]:
        print("!" * 74)
        print(f"  The demo data was seeded on {st['seeded_on'] or 'an unknown day'}, not today.")
        print("  Lines like 'yesterday' will miss. Stop this, run:  python seed.py")
        print("!" * 74)
    print(f"Resolved. running on http://127.0.0.1:{port}     console: /console")
    print("planner:", model_name() if have_key() else
          "stand-in (set SARVAM_API_KEY or GROQ_API_KEY for a model)",
          "  - to use Groq instead:  set PLANNER=groq" if model_name().startswith("Sarvam") else "")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
