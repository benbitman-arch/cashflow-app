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
