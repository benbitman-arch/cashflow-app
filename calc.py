"""Pure cash-flow math over normalized inflow/outflow dataframes."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd


def projected_balance(
    on_date: date,
    current_bank: float,
    inflows: pd.DataFrame,
    outflows: pd.DataFrame,
) -> float:
    """Bank balance projected to land at end-of-day `on_date`.

    Assumes every inflow with value_date <= on_date has cleared, and every
    outflow with due_date <= on_date has cleared.
    """
    bal = float(current_bank)
    if not inflows.empty:
        bal += float(inflows.loc[inflows["value_date"] <= on_date, "amount_usd"].sum())
    if not outflows.empty:
        bal -= float(outflows.loc[outflows["due_date"] <= on_date, "amount_usd"].sum())
    return bal


def max_buy(
    decision_date: date,
    term_days: int,
    current_bank: float,
    inflows: pd.DataFrame,
    outflows: pd.DataFrame,
    safety_buffer: float = 0.0,
) -> float:
    """Max USD I can commit to buying on `decision_date` with payment in `term_days` days."""
    payment_day = decision_date + timedelta(days=term_days)
    bal = projected_balance(payment_day, current_bank, inflows, outflows)
    return max(0.0, bal - safety_buffer)


def compute_avg_customer_terms(inflows: pd.DataFrame) -> float | None:
    """Amount-weighted average days between invoice (reference_date) and payment (value_date).

    Returns None if the input lacks reference_date or has no usable rows.
    """
    if inflows is None or inflows.empty:
        return None
    if "reference_date" not in inflows.columns:
        return None
    val_col = "original_value_date" if "original_value_date" in inflows.columns else "value_date"
    df = inflows[["reference_date", val_col, "amount_usd"]].copy()
    df = df.dropna(subset=["reference_date", val_col])
    if df.empty:
        return None
    df["gap"] = (pd.to_datetime(df[val_col]) - pd.to_datetime(df["reference_date"])).dt.days
    df = df[(df["gap"] >= 0) & (df["gap"] <= 365) & (df["amount_usd"] > 0)]
    if df.empty:
        return None
    return float((df["gap"] * df["amount_usd"]).sum() / df["amount_usd"].sum())


def project_fm_deposits(start: date, end: date, weekly_amount: float) -> pd.DataFrame:
    """Generate synthetic weekly inflows for FM Trading's recurring bank deposits.

    Deposits land same-day (no terms gap), every 7 days from `start`.
    """
    if weekly_amount <= 0:
        return pd.DataFrame()
    rows = []
    d = start
    while d <= end:
        rows.append(
            {
                "source": "fm_trading",
                "party": f"FM Trading ({d.isoformat()})",
                "amount_usd": float(weekly_amount),
                "value_date": d,
                "info": "weekly deposit",
            }
        )
        d += timedelta(days=7)
    return pd.DataFrame(rows)


def project_future_sales(
    start: date,
    end: date,
    weekly_sales: float,
    customer_terms_days: int,
) -> pd.DataFrame:
    """Generate synthetic weekly receivables for projected future sales.

    Each week starting `start` we book `weekly_sales` of revenue that arrives
    `customer_terms_days` later. Used as additive inflow on top of existing OMD.
    """
    if weekly_sales <= 0:
        return pd.DataFrame()
    rows = []
    sale_date = start
    while sale_date <= end:
        rows.append(
            {
                "source": "projected_sales",
                "party": f"projected ({sale_date.isoformat()})",
                "amount_usd": float(weekly_sales),
                "value_date": sale_date + timedelta(days=int(customer_terms_days)),
                "info": "",
            }
        )
        sale_date += timedelta(days=7)
    return pd.DataFrame(rows)


def daily_balance_series(
    start: date,
    end: date,
    current_bank: float,
    inflows: pd.DataFrame,
    outflows: pd.DataFrame,
) -> pd.DataFrame:
    """End-of-day balance for every day in [start, end]."""
    days = pd.date_range(start, end, freq="D").date
    return pd.DataFrame(
        {
            "date": days,
            "balance": [
                projected_balance(d, current_bank, inflows, outflows) for d in days
            ],
        }
    )
