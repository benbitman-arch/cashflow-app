"""Pure cash-flow math over normalized inflow/outflow dataframes."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from dateutil.relativedelta import relativedelta


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
    """Max USD I can commit to buying on `decision_date` with payment in `term_days` days.

    Two checks:
      1. Projected balance at the *decision date* must be >= safety buffer.
         If we're still in a projected deficit on that day, no buys allowed.
      2. Projected balance at the *payment day* (decision + term) determines
         the budget — that's the cushion we have on the day we actually pay.

    Inflows should exclude overdue (uncertain) receivables; the projection
    counts on-time existing OMD receivables, projected weekly sales, and FM
    Trading deposits arriving as scheduled.
    """
    bal_now = projected_balance(decision_date, current_bank, inflows, outflows)
    if bal_now < safety_buffer:
        return 0.0
    payment_day = decision_date + timedelta(days=term_days)
    bal_then = projected_balance(payment_day, current_bank, inflows, outflows)
    return max(0.0, bal_then - safety_buffer)


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


def apply_customer_payment_delay(
    payments_in: pd.DataFrame, delay_days: int
) -> pd.DataFrame:
    """Shift each OMD invoice by `delay_days` from its ORIGINAL value_date.

    Customers pay several days after their invoice's original value_date. We
    shift from the original (pre-clamp) date, then clamp to today so an
    invoice that was due 30 days ago doesn't end up 23 days in the past — it
    arrives today (this week). An invoice due 3 days ago shifts to today+4d.
    An invoice due in the future shifts forward `delay_days`.

    Does NOT touch projected_sales (its customer_terms_days already bakes in
    average lateness), fm_trading deposits, or payment_plan rows.
    """
    if delay_days == 0 or payments_in is None or payments_in.empty:
        return payments_in if payments_in is not None else pd.DataFrame()
    if "source" not in payments_in.columns or "value_date" not in payments_in.columns:
        return payments_in

    df = payments_in.copy()
    mask = df["source"] == "omd_debt"
    if not mask.any():
        return df

    today = date.today()
    delta = timedelta(days=int(delay_days))
    has_orig = "original_value_date" in df.columns

    def _shift_row(row):
        base = None
        if has_orig:
            base = row.get("original_value_date")
            if base is None or (isinstance(base, float) and pd.isna(base)):
                base = None
        if base is None:
            base = row["value_date"]
        if base is None or (isinstance(base, float) and pd.isna(base)):
            return row["value_date"]
        new_date = base + delta
        if new_date < today:
            new_date = today
        return new_date

    df.loc[mask, "value_date"] = df.loc[mask].apply(_shift_row, axis=1)
    return df


def apply_payment_plans(
    payments_in: pd.DataFrame, plans: list[dict]
) -> pd.DataFrame:
    """Override a customer's OVERDUE invoices with a monthly payment schedule.

    For every plan {customer, monthly_amount, months, start_date, ...}:
      1. Drop only the OVERDUE rows for that customer from payments_in
         (rows where is_overdue == True). On-time rows are left untouched and
         continue to use their original OMD value_date.
      2. Generate `months` synthetic rows of $monthly_amount on consecutive
         calendar-month dates starting at start_date.

    Returns a new DataFrame; payments_in is not mutated.
    """
    if not plans:
        return payments_in if payments_in is not None else pd.DataFrame()
    if payments_in is None or payments_in.empty:
        return _plans_to_df(plans)

    df = payments_in.copy()
    if "is_overdue" in df.columns:
        # Drop only this plan-customer's overdue rows
        plan_customers = {p["customer"] for p in plans}
        drop_mask = df["party"].isin(plan_customers) & df["is_overdue"].astype(bool)
        base = df[~drop_mask].copy()
    else:
        # Backward-compat snapshot without is_overdue: drop ALL of that customer's rows
        plan_customers = {p["customer"] for p in plans}
        base = df[~df["party"].isin(plan_customers)].copy()

    synth = _plans_to_df(plans)
    if synth.empty:
        return base
    return pd.concat([base, synth], ignore_index=True)


def _plans_to_df(plans: list[dict]) -> pd.DataFrame:
    rows = []
    for plan in plans:
        try:
            start = date.fromisoformat(str(plan["start_date"]))
        except (ValueError, KeyError, TypeError):
            continue
        monthly = float(plan.get("monthly_amount", 0))
        months = int(plan.get("months", 0))
        customer = str(plan.get("customer", "")).strip()
        if monthly <= 0 or months <= 0 or not customer:
            continue
        for m in range(months):
            d = start + relativedelta(months=m)
            rows.append(
                {
                    "source": "payment_plan",
                    "party": customer,
                    "amount_usd": monthly,
                    "value_date": d,
                    "reference_date": None,
                    "original_value_date": d,
                    "is_overdue": False,
                    "info": f"plan {m + 1}/{months}",
                }
            )
    return pd.DataFrame(rows)


def project_fm_deposits(start: date, end: date, weekly_amount: float) -> pd.DataFrame:
    """Generate synthetic weekly inflows for FM Trading's recurring bank deposits.

    First deposit lands `start + 7 days` (not on `start` itself, since the
    user-entered bank balance already reflects anything that has arrived).
    """
    if weekly_amount <= 0:
        return pd.DataFrame()
    rows = []
    d = start + timedelta(days=7)
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


def project_weekly_expenses(start: date, end: date, weekly_amount: float) -> pd.DataFrame:
    """Generate synthetic weekly OUTFLOWS for recurring company expenses.

    Used for ongoing costs like salaries / office expenses that hit the bank
    weekly. First expense is one week from `start`; same cadence as FM. The
    output schema mirrors the outflows DataFrame (source/party/amount_usd/
    due_date/info) so it can be concatenated with checks + suppliers debt.
    """
    if weekly_amount <= 0:
        return pd.DataFrame()
    rows = []
    d = start + timedelta(days=7)
    while d <= end:
        rows.append(
            {
                "source": "weekly_expenses",
                "party": f"Company expenses ({d.isoformat()})",
                "amount_usd": float(weekly_amount),
                "due_date": d,
                "info": "weekly expenses",
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

    First sale week begins `start + 7 days` (not `start` itself), so we don't
    project new revenue for a week that has already partially passed and
    whose actual sales would already be visible in the OMD file.
    """
    if weekly_sales <= 0:
        return pd.DataFrame()
    rows = []
    sale_date = start + timedelta(days=7)
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


def weekly_summary(
    start: date,
    end: date,
    current_bank: float,
    inflows: pd.DataFrame,
    outflows: pd.DataFrame,
) -> pd.DataFrame:
    """Per-week rollup from `start` through `end`, Sunday-anchored.

    Columns: week_start, week_end, week_label, in_usd, out_usd, net_usd,
    closing_balance. The first row clips its start to `start` (so partial
    first weeks are honored); subsequent weeks are full Sun→Sat ranges.
    Closing balance for week W is projected_balance(W.week_end).
    """
    if start > end:
        return pd.DataFrame()

    rows = []
    # First week starts at `start`; subsequent weeks at the next Sunday.
    # Sunday = weekday() 6.
    cur_start = start
    while cur_start <= end:
        # Saturday of cur_start's week
        days_to_sat = (5 - cur_start.weekday()) % 7  # Mon=0..Sun=6 -> next Sat
        # weekday(): Mon=0, Sat=5, Sun=6. Days to next Saturday:
        # Mon(0)->5, Tue(1)->4, Wed(2)->3, Thu(3)->2, Fri(4)->1, Sat(5)->0, Sun(6)->6
        cur_end = cur_start + timedelta(days=days_to_sat)
        if cur_end > end:
            cur_end = end

        if not inflows.empty:
            in_usd = float(
                inflows.loc[
                    (inflows["value_date"] >= cur_start)
                    & (inflows["value_date"] <= cur_end),
                    "amount_usd",
                ].sum()
            )
        else:
            in_usd = 0.0
        if not outflows.empty:
            out_usd = float(
                outflows.loc[
                    (outflows["due_date"] >= cur_start)
                    & (outflows["due_date"] <= cur_end),
                    "amount_usd",
                ].sum()
            )
        else:
            out_usd = 0.0
        closing = projected_balance(cur_end, current_bank, inflows, outflows)
        opening = projected_balance(
            cur_start - timedelta(days=1), current_bank, inflows, outflows
        )

        same_day = cur_start == cur_end
        if same_day:
            label = cur_start.strftime("%a %b %d")
        else:
            label = f"{cur_start.strftime('%a %b %d')} – {cur_end.strftime('%a %b %d')}"

        rows.append(
            {
                "week_start": cur_start,
                "week_end": cur_end,
                "week_label": label,
                "opening_balance": opening,
                "in_usd": in_usd,
                "out_usd": out_usd,
                "net_usd": in_usd - out_usd,
                "closing_balance": closing,
            }
        )

        # Advance to the next Sunday after cur_end
        cur_start = cur_end + timedelta(days=1)
        # If we landed mid-week (after end), break
        if cur_start > end:
            break

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
