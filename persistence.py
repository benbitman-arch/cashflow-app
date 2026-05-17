"""Snapshot persistence.

Reads from a JSON file in the GitHub repo (so the same data is shared across
all visitors), and writes back via the GitHub Contents API. The token only
needs `Contents: read-write` on this single repo.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from typing import Any

import pandas as pd
import requests

GH_REPO_DEFAULT = "benbitman-arch/cashflow-app"
GH_FILE_PATH = "data/snapshot.json"
GH_BRANCH = "main"
RAW_URL = f"https://raw.githubusercontent.com/{{repo}}/{GH_BRANCH}/{GH_FILE_PATH}"
API_URL = f"https://api.github.com/repos/{{repo}}/contents/{GH_FILE_PATH}"


def _serialize_df(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty:
        return []
    records = []
    for _, row in df.iterrows():
        rec = {}
        for col, val in row.items():
            if isinstance(val, (date, datetime)):
                rec[col] = val.isoformat()
            elif val is None:
                rec[col] = None
            else:
                try:
                    if pd.isna(val):
                        rec[col] = None
                        continue
                except (TypeError, ValueError):
                    pass
                rec[col] = val
        records.append(rec)
    return records


def _deserialize_df(records: list[dict], date_cols: list[str]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    for col in date_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    return df


def build_snapshot(
    payments_out_by_source: dict[str, pd.DataFrame],
    payments_in: pd.DataFrame,
    excluded_omd: pd.DataFrame,
    current_bank: float,
    safety_buffer: float,
    terms_days: list[int],
    weekly_sales: float = 0.0,
    customer_terms_days: int = 30,
    fm_trading_weekly: float = 0.0,
    payment_plans: list[dict] | None = None,
    customer_payment_delay_days: int = 0,
) -> dict[str, Any]:
    return {
        "version": 5,
        "saved_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "payments_out_by_source": {
            src: _serialize_df(df) for src, df in payments_out_by_source.items()
        },
        "payments_in": _serialize_df(payments_in),
        "excluded_omd": _serialize_df(excluded_omd),
        "current_bank": float(current_bank),
        "safety_buffer": float(safety_buffer),
        "terms_days": list(terms_days),
        "weekly_sales": float(weekly_sales),
        "customer_terms_days": int(customer_terms_days),
        "fm_trading_weekly": float(fm_trading_weekly),
        "payment_plans": list(payment_plans or []),
        "customer_payment_delay_days": int(customer_payment_delay_days),
    }


def parse_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    payments_out_by_source = {
        src: _deserialize_df(recs, ["due_date"])
        for src, recs in (snap.get("payments_out_by_source") or {}).items()
    }
    return {
        "payments_out_by_source": payments_out_by_source,
        "payments_in": _deserialize_df(
            snap.get("payments_in") or [],
            ["value_date", "reference_date", "original_value_date"],
        ),
        "excluded_omd": _deserialize_df(snap.get("excluded_omd") or [], ["value_date"]),
        "current_bank": float(snap.get("current_bank") or 0),
        "safety_buffer": float(snap.get("safety_buffer") or 0),
        "terms_days": list(snap.get("terms_days") or [0, 30, 45, 60, 75]),
        "weekly_sales": float(snap.get("weekly_sales") or 0),
        "customer_terms_days": int(snap.get("customer_terms_days") or 30),
        "fm_trading_weekly": float(snap.get("fm_trading_weekly") or 0),
        "payment_plans": list(snap.get("payment_plans") or []),
        "customer_payment_delay_days": int(snap.get("customer_payment_delay_days") or 0),
        "saved_at": snap.get("saved_at"),
    }


def load_from_github(repo: str = GH_REPO_DEFAULT, token: str | None = None) -> dict | None:
    """Read the snapshot from GitHub. Uses the raw CDN (no auth needed for public repos)."""
    url = RAW_URL.format(repo=repo)
    headers = {"Cache-Control": "no-cache"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def save_to_github(
    snapshot: dict, token: str, repo: str = GH_REPO_DEFAULT
) -> tuple[bool, str]:
    """Push the snapshot JSON to GitHub via the Contents API. Returns (ok, message)."""
    url = API_URL.format(repo=repo)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # Fetch current SHA if file already exists
    sha = None
    try:
        r = requests.get(url, headers=headers, params={"ref": GH_BRANCH}, timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception as e:
        return False, f"GitHub GET failed: {e}"

    payload_text = json.dumps(snapshot, ensure_ascii=False, indent=2)
    content_b64 = base64.b64encode(payload_text.encode("utf-8")).decode("ascii")
    body = {
        "message": f"snapshot: update {snapshot.get('saved_at', '')}",
        "content": content_b64,
        "branch": GH_BRANCH,
    }
    if sha:
        body["sha"] = sha

    try:
        r = requests.put(url, headers=headers, json=body, timeout=15)
    except Exception as e:
        return False, f"GitHub PUT failed: {e}"

    if r.status_code in (200, 201):
        return True, "saved"
    return False, f"GitHub PUT returned {r.status_code}: {r.text[:200]}"
