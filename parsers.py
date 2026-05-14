"""Parsers for the three Excel inputs.

Column lookup uses normalized header matching so future files can rearrange
columns or vary whitespace as long as the human-readable names stay the same.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
from typing import Iterable

import pandas as pd

from exclude_list import is_excluded


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip()).casefold()


def _find_col(df: pd.DataFrame, candidates: Iterable[str], label: str) -> str:
    wanted = {_norm(c) for c in candidates}
    for col in df.columns:
        if _norm(col) in wanted:
            return col
    raise ValueError(
        f"{label}: expected one of {list(candidates)!r}; got {list(df.columns)!r}"
    )


def _to_date(v) -> date | None:
    try:
        if v is None or pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        ts = pd.to_datetime(v, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.date()
    except Exception:
        return None


def _read_excel(file) -> pd.DataFrame:
    """Read an xls/xlsx upload (BytesIO, path, or Streamlit UploadedFile)."""
    if hasattr(file, "read"):
        data = file.read()
        try:
            file.seek(0)
        except Exception:
            pass
        bio = BytesIO(data)
        # try xls first (xlrd), then xlsx (openpyxl)
        try:
            return pd.read_excel(bio, sheet_name=0, engine="xlrd")
        except Exception:
            bio.seek(0)
            return pd.read_excel(bio, sheet_name=0, engine="openpyxl")
    return pd.read_excel(file, sheet_name=0)


@dataclass
class ParseResult:
    payments: pd.DataFrame  # cols: source, party, amount_usd, due_date, info
    excluded: pd.DataFrame  # rows dropped (for audit)
    warnings: list[str]


def parse_checks(file) -> ParseResult:
    """Israeli supplier checks (Checks May14.xls).

    Past-dated checks have already auto-deducted from the bank account, so
    they're already reflected in the user's current bank balance and we skip
    them to avoid double-counting in the forward projection.
    """
    df = _read_excel(file)
    name_col = _find_col(df, ["שם"], "Checks file (supplier name)")
    date_col = _find_col(df, ["תאריך תחזית"], "Checks file (due date)")
    amount_col = _find_col(df, ["TTL Invoice"], "Checks file (amount)")
    bank_col_name = None
    try:
        bank_col_name = _find_col(df, ["בנק"], "Checks file (bank)")
    except ValueError:
        pass

    today = date.today()
    out, dropped, warns = [], [], []
    for _, r in df.iterrows():
        amt = r[amount_col]
        if pd.isna(amt) or float(amt) <= 0:
            continue
        d = _to_date(r[date_col])
        if d is None:
            dropped.append({"reason": "missing date", "row": r.to_dict()})
            continue
        if d < today:
            dropped.append(
                {
                    "reason": "past-dated check (already cleared)",
                    "name": str(r[name_col]).strip(),
                    "amount": float(amt),
                    "date": d,
                }
            )
            continue
        out.append(
            {
                "source": "checks",
                "party": str(r[name_col]).strip(),
                "amount_usd": float(amt),
                "due_date": d,
                "info": str(r[bank_col_name]).strip() if bank_col_name else "",
            }
        )
    return ParseResult(pd.DataFrame(out), pd.DataFrame(dropped), warns)


def parse_suppliers_debt(file) -> ParseResult:
    """Foreign supplier debt (Suppliers Debt Ben.xls).

    The actual header row is row index 0 of the sheet after the file's
    top label row, so the dataframe loaded by pandas already has the right
    headers (xlrd skips empty leading rows).
    """
    df = _read_excel(file)
    # Some files have a leading title row; if the standard columns are missing,
    # retry by skipping the first row.
    if not any(_norm(c) == _norm("שם םפק") for c in df.columns):
        df.columns = df.iloc[0]
        df = df.iloc[1:].reset_index(drop=True)

    name_col = _find_col(df, ["שם םפק", "שם ספק"], "Suppliers Debt (supplier name)")
    date_col = _find_col(df, ["Due Date"], "Suppliers Debt (due date)")
    amount_col = _find_col(df, ["יתרה לתשלום"], "Suppliers Debt (balance to pay)")
    bank_col_name = None
    try:
        bank_col_name = _find_col(df, ["Bank Name"], "Suppliers Debt (bank)")
    except ValueError:
        pass

    today = date.today()
    out, dropped, warns = [], [], []
    for _, r in df.iterrows():
        amt = r[amount_col]
        try:
            amt = float(amt)
        except Exception:
            continue
        if pd.isna(amt) or amt <= 0:
            continue
        d = _to_date(r[date_col])
        if d is None:
            dropped.append({"reason": "missing date", "row": r.to_dict()})
            continue
        if d < today:
            d = today  # overdue -> must pay now
        out.append(
            {
                "source": "suppliers_debt",
                "party": str(r[name_col]).strip(),
                "amount_usd": amt,
                "due_date": d,
                "info": str(r[bank_col_name]).strip() if bank_col_name else "",
            }
        )
    return ParseResult(pd.DataFrame(out), pd.DataFrame(dropped), warns)


def parse_omd_debt(file) -> ParseResult:
    """Customer receivables (OMD DEBT BEN.xls). Filters yellow-list names.

    Keeps `reference_date` (תאריך התיחסות, invoice date) alongside `value_date`
    so callers can compute average customer payment terms.
    """
    df = _read_excel(file)
    name_col = _find_col(df, ["Customer Name"], "OMD Debt (customer name)")
    date_col = _find_col(df, ["תאריך ערך"], "OMD Debt (value date)")
    amount_col = _find_col(df, ["Debit Amount"], "OMD Debt (debit amount)")
    ref_col = None
    try:
        ref_col = _find_col(df, ["תאריך התיחסות"], "OMD Debt (reference date)")
    except ValueError:
        pass

    today = date.today()
    out, dropped, warns = [], [], []
    for _, r in df.iterrows():
        name = str(r[name_col]).strip() if pd.notna(r[name_col]) else ""
        amt = r[amount_col]
        try:
            amt = float(amt)
        except Exception:
            continue
        if pd.isna(amt) or amt <= 0:
            continue
        d = _to_date(r[date_col])
        if d is None:
            dropped.append({"reason": "missing date", "name": name, "amount": amt})
            continue
        ref_d = _to_date(r[ref_col]) if ref_col else None
        if is_excluded(name):
            dropped.append(
                {"reason": "yellow exclude list", "name": name, "amount": amt, "value_date": d}
            )
            continue
        original_value_date = d
        if d < today:
            d = today
        out.append(
            {
                "source": "omd_debt",
                "party": name,
                "amount_usd": amt,
                "value_date": d,
                "reference_date": ref_d,
                "original_value_date": original_value_date,
                "info": "",
            }
        )
    return ParseResult(pd.DataFrame(out), pd.DataFrame(dropped), warns)
