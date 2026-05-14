"""Month-grid calendar rendering for Streamlit, mobile-friendly via CSS @media."""

from __future__ import annotations

import calendar
from datetime import date

import pandas as pd
import streamlit as st

from calc import max_buy

_CSS = """
<style>
.cf-header {
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 6px;
    margin-bottom: 6px;
}
.cf-grid {
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 6px;
}
.cf-weekday {
    text-align: center;
    font-weight: 600;
    font-size: 12px;
    color: #666;
    padding: 4px;
}
.cf-cell {
    padding: 8px;
    border-radius: 8px;
    border: 1px solid #e6e6e6;
    background: white;
    display: flex;
    flex-direction: column;
}
.cf-cell.past { color: #bbb; background: #fafafa; }
.cf-cell.today { border: 2px solid #1a73e8; background: #fffde7; }
.cf-day { font-weight: 700; font-size: 14px; margin-bottom: 4px; }
.cf-weekday-inline { display: none; opacity: 0.6; font-weight: 400; font-size: 12px; margin-left: 6px; }
.cf-row {
    display: flex;
    justify-content: space-between;
    font-size: 11px;
    line-height: 1.5;
}
.cf-row .lbl { opacity: 0.75; }
.cf-row .val { font-weight: 600; white-space: nowrap; }
.cf-positive { color: #0d652d; }
.cf-negative { color: #b71c1c; }
@media (max-width: 720px) {
    .cf-grid { grid-template-columns: repeat(2, 1fr) !important; gap: 8px !important; }
    .cf-header { display: none !important; }
    .cf-cell.past { display: none !important; }
    .cf-cell { padding: 12px !important; }
    .cf-day { font-size: 18px !important; margin-bottom: 8px !important; }
    .cf-row { font-size: 14px !important; line-height: 1.7 !important; }
    .cf-weekday-inline { display: inline !important; }
}
</style>
"""


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
    weekday: str,
    terms: list[int],
    current_bank: float,
    inflows: pd.DataFrame,
    outflows: pd.DataFrame,
    safety_buffer: float,
) -> str:
    if in_past:
        return f"<div class='cf-cell past'><div class='cf-day'>{d.day}</div><div style='opacity:0.5;font-size:11px'>past</div></div>"

    klass = "cf-cell today" if is_today else "cf-cell"
    badge = " 📍" if is_today else ""
    rows = []
    for t in terms:
        val = max_buy(d, t, current_bank, inflows, outflows, safety_buffer)
        label = "Cash" if t == 0 else f"{t}d"
        if val > 0:
            color_class = "cf-positive"
            txt = _fmt_money(val)
        else:
            color_class = "cf-negative"
            txt = "—"
        rows.append(
            f"<div class='cf-row {color_class}'><span class='lbl'>{label}</span><span class='val'>{txt}</span></div>"
        )
    return (
        f"<div class='{klass}'>"
        f"<div class='cf-day'>{d.day}{badge}<span class='cf-weekday-inline'>{weekday}</span></div>"
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

    header_html = "<div class='cf-header'>" + "".join(
        f"<div class='cf-weekday'>{w}</div>" for w in weekday_names
    ) + "</div>"

    cells = []
    for week in weeks:
        for i, day in enumerate(week):
            if day == 0:
                cells.append("<div></div>")
                continue
            d = date(year, month, day)
            cells.append(
                _cell_html(
                    d,
                    d == today,
                    d < today,
                    weekday_names[i],
                    terms_days,
                    current_bank,
                    inflows,
                    outflows,
                    safety_buffer,
                )
            )
    grid_html = "<div class='cf-grid'>" + "".join(cells) + "</div>"
    st.markdown(header_html + grid_html, unsafe_allow_html=True)


def render_range(
    start: date,
    end: date,
    current_bank: float,
    inflows: pd.DataFrame,
    outflows: pd.DataFrame,
    terms_days: list[int],
    safety_buffer: float,
) -> None:
    st.markdown(_CSS, unsafe_allow_html=True)
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
