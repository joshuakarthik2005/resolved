"""Resolved. — builds resolved.db: merchants, the UPI ledger, and an empty audit log.

Run:  python seed.py          (safe to re-run; it rebuilds the demo data)

The three demo tickets on slide 4 come from the three transactions marked
DEMO below. Everything here is fake data shaped like real UPI records.
"""
from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta

import os

DB = os.getenv("RESOLVED_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "resolved.db")

SCHEMA = """
DROP TABLE IF EXISTS merchants;
DROP TABLE IF EXISTS ledger;
DROP TABLE IF EXISTS tickets;
DROP TABLE IF EXISTS audit;
DROP TABLE IF EXISTS meta;

CREATE TABLE merchants (
    merchant_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    terminal        TEXT NOT NULL,
    payout_account  TEXT NOT NULL,
    language        TEXT NOT NULL DEFAULT 'en',
    auto_today      INTEGER NOT NULL DEFAULT 0,
    auto_date       TEXT                       -- the day auto_today counts; resets when it changes
);

CREATE TABLE ledger (
    utr             TEXT PRIMARY KEY,
    merchant_id     TEXT NOT NULL,
    terminal        TEXT NOT NULL,
    amount          INTEGER NOT NULL,          -- whole rupees
    txn_time        TEXT NOT NULL,             -- ISO 8601
    bank_status     TEXT NOT NULL,             -- SUCCESS | SUCCESS_LATE | FAILED_DEBITED
    npci_code       TEXT NOT NULL DEFAULT '00', -- response code the bank status is read from
    source_account  TEXT NOT NULL,             -- customer's account
    settled         INTEGER NOT NULL DEFAULT 0,
    reversed        INTEGER NOT NULL DEFAULT 0,
    note            TEXT DEFAULT ''
);

CREATE TABLE tickets (
    ticket_id       TEXT PRIMARY KEY,
    merchant_id     TEXT NOT NULL,
    message         TEXT NOT NULL,
    claimed_amount  INTEGER,
    utr             TEXT,
    created         TEXT NOT NULL,
    status          TEXT NOT NULL,             -- RESOLVED | ESCALATED | BLOCKED | NEEDS_INFO | SUPERSEDED | TAKEN | REJECTED | ANSWERED | ALREADY_DONE
    outcome         TEXT,                      -- APPROVE | ESCALATE | BLOCK
    action          TEXT,
    destination     TEXT,
    model_reason    TEXT,
    checks_passed   TEXT,
    checks_failed   TEXT,
    reply           TEXT,
    latency_ms      INTEGER,
    awaiting        TEXT,                      -- what the shop still has to send, e.g. UTR
    reopened_from   TEXT,                      -- the case this one carries on from
    txn_amount      INTEGER,                   -- the ledger amount actually at stake
    txn_time        TEXT,                      -- when that payment happened (RBI clock starts)
    extra           TEXT                       -- JSON: search window, settlement cycle, customer notice
);

CREATE TABLE audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    ticket_id       TEXT,
    actor           TEXT NOT NULL,             -- agent | gate | human
    event           TEXT NOT NULL,
    detail          TEXT NOT NULL
);

CREATE TABLE meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);
"""

MERCHANTS = [
    ("M001", "Kumar Stores",     "SB-412", "PAYOUT-001", "ta"),
    ("M002", "Anu Bakery",       "SB-228", "PAYOUT-002", "ta"),
    ("M003", "RK Mobiles",       "SB-907", "PAYOUT-003", "en"),
    ("M004", "Sri Lakshmi Mart", "SB-551", "PAYOUT-004", "ta"),
    ("M005", "Delhi Dhaba",      "SB-163", "PAYOUT-005", "hi"),
]


NPCI_FOR = {"SUCCESS": "00", "SUCCESS_LATE": "BT", "FAILED_DEBITED": "YD"}


def build(path: str = DB, quiet: bool = False) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany(
        "INSERT INTO merchants (merchant_id, name, terminal, payout_account, language)"
        " VALUES (?,?,?,?,?)", MERCHANTS)

    now = datetime.now().replace(second=0, microsecond=0)
    rows = []

    def txn(utr, mid, term, amount, minutes_ago, status, source, at=None):
        when = at or (now - timedelta(minutes=minutes_ago))
        rows.append((utr, mid, term, amount, when.isoformat(timespec="seconds"),
                     status, NPCI_FOR[status], source, 0, 0, ""))

    midnight = now.replace(hour=0, minute=0)

    # --- DEMO 1: late success, YESTERDAY evening. The bank confirmed after the
    #     Soundbox gave up. The demo line says "yesterday", and matching is time-scoped.
    txn("UTR300000000001", "M001", "SB-412", 4250, 0, "SUCCESS_LATE", "CUST-884102",
        at=midnight - timedelta(hours=4, minutes=40))

    # --- DEMO 2: true failure. Customer debited, merchant never credited. -----
    txn("UTR300000000002", "M002", "SB-228", 1890, 140, "FAILED_DEBITED", "CUST-551907")

    # --- DEMO 3: two payments, same amount, minutes apart. Gate must block. ---
    txn("UTR300000000003", "M003", "SB-907", 2500, 55, "SUCCESS_LATE", "CUST-220458")
    txn("UTR300000000004", "M003", "SB-907", 2500, 52, "SUCCESS_LATE", "CUST-773115")

    # --- over the auto-limit: escalates to a human ---------------------------
    # The demo line says "today", so this must be today whenever seed.py runs: 200 minutes
    # ago, unless that crosses midnight, then halfway between midnight and now.
    today_at = now - timedelta(minutes=200)
    if today_at < midnight:
        today_at = midnight + (now - midnight) / 2
    txn("UTR300000000005", "M004", "SB-551", 18400, 0, "FAILED_DEBITED", "CUST-610233",
        at=today_at)

    # --- near the RBI T+5 deadline: failed 5 days ago, over the limit ----------
    txn("UTR300000000041", "M004", "SB-551", 12600, 0, "FAILED_DEBITED", "CUST-402771",
        at=midnight - timedelta(days=5) + timedelta(hours=11, minutes=20))

    # --- duplicate: same customer, same amount, 40 seconds apart ---------------
    txn("UTR300000000042", "M005", "SB-163", 640, 75, "SUCCESS", "CUST-318840")
    rows.append(("UTR300000000043", "M005", "SB-163", 640,
                 (now - timedelta(minutes=75) + timedelta(seconds=40)).isoformat(timespec="seconds"),
                 "SUCCESS", "00", "CUST-318840", 0, 0, ""))
    # --- ordinary background traffic so searches look real -------------------
    rng = random.Random(11)
    for i in range(6, 40):
        mid, _, term, _, _ = MERCHANTS[rng.randrange(len(MERCHANTS))]
        txn(f"UTR3000000000{i:02d}", mid, term, rng.randrange(80, 9500),
            rng.randrange(5, 600),
            rng.choice(["SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS_LATE", "FAILED_DEBITED"]),
            f"CUST-{rng.randrange(100000, 999999)}")

    con.executemany("INSERT INTO ledger (utr, merchant_id, terminal, amount, txn_time,"
                    " bank_status, npci_code, source_account, settled, reversed, note)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("INSERT INTO meta VALUES ('seeded_on', ?)", (now.date().isoformat(),))
    con.commit()
    con.close()
    if quiet:
        return
    print(f"{DB} built: {len(MERCHANTS)} merchants, {len(rows)} transactions.")
    print("Demo tickets to type into the chat page:")
    print("  Kumar Stores     ₹4,250  -> late success, agent marks it paid")
    print("  Anu Bakery       ₹1,890  -> true failure, agent reverses to the customer")
    print("  RK Mobiles       ₹2,500  -> two matches, gate blocks and asks for the UTR")
    print("  Sri Lakshmi Mart ₹18,400 -> over the limit, goes to the human console")
    print("  Sri Lakshmi Mart ₹12,600 5 days ago -> escalates, RBI T+5 deadline is tonight")
    print("  Delhi Dhaba      ₹640 twice -> duplicate, refunds the second one")


if __name__ == "__main__":
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8")   # ₹ on a cp1252 Windows console or a redirect
    if "--if-stale" in _sys.argv:
        # The ledger is seeded relative to the day seed.py ran, and the demo lines say
        # "yesterday" and "5 days ago". Data from another day makes demo step 1 miss.
        day = None
        if os.path.exists(DB):
            try:
                c = sqlite3.connect(DB)
                day = c.execute("SELECT value FROM meta WHERE key='seeded_on'").fetchone()[0]
                c.close()
            except Exception:
                day = None
        if day == datetime.now().date().isoformat():
            print(f"{DB} was seeded today; keeping it.")
            raise SystemExit(0)
        print(f"{DB} was seeded on {day or 'an unknown day'}; rebuilding it for today.")
    build()
