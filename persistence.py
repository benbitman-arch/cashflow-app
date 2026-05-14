"""Persist the latest parsed snapshot to a local SQLite DB.

For Streamlit Cloud, this DB lives in the working directory. To survive
redeploys, commit `cashflow.db` to the repo after each save (or replace this
module with a Postgres backend).
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent / "cashflow.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )"""
    )
    return c


def save_snapshot(
    payments_out: pd.DataFrame,
    payments_in: pd.DataFrame,
    current_bank: float,
    safety_buffer: float,
    terms_days: list[int],
) -> None:
    with _conn() as c:
        payments_out_s = payments_out.copy()
        payments_in_s = payments_in.copy()
        if "due_date" in payments_out_s:
            payments_out_s["due_date"] = payments_out_s["due_date"].astype(str)
        if "value_date" in payments_in_s:
            payments_in_s["value_date"] = payments_in_s["value_date"].astype(str)
        payments_out_s.to_sql("payments_out", c, if_exists="replace", index=False)
        payments_in_s.to_sql("payments_in", c, if_exists="replace", index=False)
        c.executemany(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            [
                ("current_bank", str(current_bank)),
                ("safety_buffer", str(safety_buffer)),
                ("terms_days", ",".join(str(t) for t in terms_days)),
                ("saved_at", date.today().isoformat()),
            ],
        )


def load_snapshot() -> dict | None:
    if not DB_PATH.exists():
        return None
    with _conn() as c:
        try:
            payments_out = pd.read_sql("SELECT * FROM payments_out", c)
            payments_in = pd.read_sql("SELECT * FROM payments_in", c)
        except Exception:
            return None
        if "due_date" in payments_out:
            payments_out["due_date"] = pd.to_datetime(payments_out["due_date"]).dt.date
        if "value_date" in payments_in:
            payments_in["value_date"] = pd.to_datetime(payments_in["value_date"]).dt.date
        settings = dict(c.execute("SELECT key, value FROM settings").fetchall())
    return {
        "payments_out": payments_out,
        "payments_in": payments_in,
        "current_bank": float(settings.get("current_bank", 0) or 0),
        "safety_buffer": float(settings.get("safety_buffer", 0) or 0),
        "terms_days": [int(x) for x in settings.get("terms_days", "0,30,45,60,75").split(",") if x],
        "saved_at": settings.get("saved_at"),
    }
