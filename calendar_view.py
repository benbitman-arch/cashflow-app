"""Month-grid calendar rendering for Streamlit."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from calc import max_buy


def _fmt_money(x: float) -> str:
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.1f}K"
    return f"${x:.0f}"


def render_month(
    year: int,
    month: int,
    current_bank: float,
    inflows: pd.DataFrame,
    outflows: pd.DataFrame,
    terms_days: list[int],
    safety_buffer: float,
) -> None:
    cal = calendar.Calendar(firstweekday=6)  # Sunday first (Israeli week)
    weeks = cal.monthdayscalendar(year, month)
    today = date.today()

    weekday_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    header_cols = st.columns(7)
    for i, name in enumerate(weekday_names):
        header_cols[i].markdown(f"**{name}**")

    for week in weeks:
        cols = st.columns(7)
        for i, day in enumerate(week):
            with cols[i]:
                if day == 0:
                    st.markdown("&nbsp;", unsafe_allow_html=True)
                    continue
                d = date(year, month, day)
                badge = " 🟦" if d == today else ""
                st.markdown(f"**{day}**{badge}")
                lines = []
                for t in terms_days:
                    val = max_buy(d, t, current_bank, inflows, outflows, safety_buffer)
                    label = "Cash" if t == 0 else f"{t}d"
                    color = "#1a7f37" if val > 0 else "#cf222e"
                    lines.append(
                        f"<div style='font-size:11px;color:{color}'>{label}: {_fmt_money(val)}</div>"
                    )
                st.markdown("".join(lines), unsafe_allow_html=True)


def render_range(
    start: date,
    end: date,
    current_bank: float,
    inflows: pd.DataFrame,
    outflows: pd.DataFrame,
    terms_days: list[int],
    safety_buffer: float,
) -> None:
    """Render every month between `start` and `end` (inclusive)."""
    cur = date(start.year, start.month, 1)
    while cur <= end:
        st.subheader(cur.strftime("%B %Y"))
        render_month(
            cur.year, cur.month, current_bank, inflows, outflows, terms_days, safety_buffer
        )
        # next month
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
