"""Month-grid calendar rendering for Streamlit."""

from __future__ import annotations

import calendar
from datetime import date

import pandas as pd
import streamlit as st

from calc import max_buy


def _fmt_money(x: float) -> str:
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.1f}K"
    return f"${x:.0f}"


def _cell_html(
    d: date,
    is_today: bool,
    in_past: bool,
    terms: list[int],
    current_bank: float,
    inflows: pd.DataFrame,
    outflows: pd.DataFrame,
    safety_buffer: float,
) -> str:
    if in_past:
        return (
            f"<div style='padding:8px;border-radius:6px;background:#fafafa;color:#bbb;"
            f"font-size:12px;height:120px;'>"
            f"<div style='font-weight:600'>{d.day}</div>"
            f"<div style='opacity:0.5'>past</div></div>"
        )

    border = "2px solid #1a73e8" if is_today else "1px solid #e6e6e6"
    bg = "#fffde7" if is_today else "white"
    badge = " 📍" if is_today else ""

    rows = []
    for t in terms:
        val = max_buy(d, t, current_bank, inflows, outflows, safety_buffer)
        label = "Cash" if t == 0 else f"{t}d"
        if val > 0:
            color = "#0d652d"
            txt = _fmt_money(val)
        else:
            color = "#b71c1c"
            txt = "—"
        rows.append(
            f"<div style='display:flex;justify-content:space-between;font-size:11px;color:{color};line-height:1.4'>"
            f"<span style='opacity:0.75'>{label}</span><span style='font-weight:600'>{txt}</span></div>"
        )

    return (
        f"<div style='padding:8px;border-radius:6px;border:{border};background:{bg};height:140px;overflow:hidden'>"
        f"<div style='font-weight:700;font-size:14px;margin-bottom:4px'>{d.day}{badge}</div>"
        f"{''.join(rows)}"
        f"</div>"
    )


def render_month(
    year: int,
    month: int,
    current_bank: float,
    inflows: pd.DataFrame,
    outflows: pd.DataFrame,
    terms_days: list[int],
    safety_buffer: float,
) -> None:
    cal = calendar.Calendar(firstweekday=6)  # Sunday first
    weeks = cal.monthdayscalendar(year, month)
    today = date.today()
    weekday_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    header_html = "".join(
        f"<div style='text-align:center;font-weight:600;font-size:12px;color:#666;padding:4px'>{w}</div>"
        for w in weekday_names
    )
    grid_cells = []
    for week in weeks:
        for day in week:
            if day == 0:
                grid_cells.append("<div></div>")
                continue
            d = date(year, month, day)
            grid_cells.append(
                _cell_html(
                    d,
                    d == today,
                    d < today,
                    terms_days,
                    current_bank,
                    inflows,
                    outflows,
                    safety_buffer,
                )
            )
    st.markdown(
        "<div style='display:grid;grid-template-columns:repeat(7,1fr);gap:6px'>"
        + header_html
        + "</div>"
        + "<div style='display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin-top:6px'>"
        + "".join(grid_cells)
        + "</div>",
        unsafe_allow_html=True,
    )


def render_range(
    start: date,
    end: date,
    current_bank: float,
    inflows: pd.DataFrame,
    outflows: pd.DataFrame,
    terms_days: list[int],
    safety_buffer: float,
) -> None:
    cur = date(start.year, start.month, 1)
    while cur <= end:
        st.markdown(f"#### {cur.strftime('%B %Y')}")
        render_month(
            cur.year, cur.month, current_bank, inflows, outflows, terms_days, safety_buffer
        )
        st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
