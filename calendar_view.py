"""Month-grid calendar rendering for Streamlit, mobile-friendly via CSS @media."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from calc import max_buy, projected_balance

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
.cf-week-summary {
    display: flex;
    gap: 12px;
    padding: 8px 12px;
    margin: 4px 0 10px 0;
    background: #f5f7fa;
    border-radius: 6px;
    font-size: 12px;
    color: #444;
    flex-wrap: wrap;
}
.cf-week-summary .label {
    font-weight: 600;
    margin-right: 4px;
}
.cf-week-summary .out { color: #b71c1c; }
.cf-week-summary .in { color: #0d652d; }
.cf-week-summary .net { color: #1a73e8; }
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


def _week_summary_html(
    week_start: date,
    week_end: date,
    current_bank: float,
    inflows: pd.DataFrame,
    outflows: pd.DataFrame,
    safety_buffer: float = 0.0,
) -> str:
    out_w = (
        float(
            outflows.loc[
                (outflows["due_date"] >= week_start) & (outflows["due_date"] <= week_end),
                "amount_usd",
            ].sum()
        )
        if not outflows.empty
        else 0.0
    )
    in_w = (
        float(
            inflows.loc[
                (inflows["value_date"] >= week_start) & (inflows["value_date"] <= week_end),
                "amount_usd",
            ].sum()
        )
        if not inflows.empty
        else 0.0
    )
    net_w = in_w - out_w
    closing = projected_balance(week_end, current_bank, inflows, outflows)
    net_class = "in" if net_w >= 0 else "out"
    net_sign = "+" if net_w >= 0 else "−"
    closing_class = "in" if closing >= safety_buffer else "out"
    closing_sign = "" if closing >= 0 else "−"
    label = f"Week of {week_start.strftime('%b %d')}"
    return (
        f"<div class='cf-week-summary'>"
        f"<span class='label'>{label}</span>"
        f"<span class='out'>↓ Pay: {_fmt_money(out_w)}</span>"
        f"<span class='in'>↑ Receive: {_fmt_money(in_w)}</span>"
        f"<span class='{net_class}'>Net: {net_sign}{_fmt_money(abs(net_w))}</span>"
        f"<span class='{closing_class}'>Closing: {closing_sign}{_fmt_money(abs(closing))}</span>"
        f"</div>"
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
    """Render the calendar as continuous Sun→Sat weeks, regardless of month boundary.

    A month header is emitted whenever the midweek date crosses into a new
    month, so users see Jun/Jul transitions inline without splitting any week.
    """
    st.markdown(_CSS, unsafe_allow_html=True)
    today = date.today()
    weekday_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    header_html = (
        "<div class='cf-header'>"
        + "".join(f"<div class='cf-weekday'>{w}</div>" for w in weekday_names)
        + "</div>"
    )

    # Walk back to the Sunday of `start`'s week. weekday(): Mon=0, Sun=6.
    # Days back to last Sunday: 0 if Sun, else weekday + 1
    days_back = 0 if start.weekday() == 6 else start.weekday() + 1
    cur_sunday = start - timedelta(days=days_back)

    last_month_shown = None
    header_rendered = False

    while cur_sunday <= end:
        # Month label flips based on the midweek (Wednesday) date — so a Sun→Sat
        # week that straddles a month is labelled by whichever month "owns" most
        # of it.
        midweek = cur_sunday + timedelta(days=3)
        month_key = (midweek.year, midweek.month)
        if month_key != last_month_shown:
            st.markdown(f"#### {midweek.strftime('%B %Y')}")
            st.markdown(header_html, unsafe_allow_html=True)
            last_month_shown = month_key
            header_rendered = True
        elif not header_rendered:
            st.markdown(header_html, unsafe_allow_html=True)
            header_rendered = True

        # Build the 7 cells for the week
        cells = []
        for i in range(7):
            d = cur_sunday + timedelta(days=i)
            if d > end:
                cells.append("<div></div>")
                continue
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
        # Week summary spans the FULL Sunday→Saturday, regardless of month/horizon
        summary = _week_summary_html(
            cur_sunday,
            cur_sunday + timedelta(days=6),
            current_bank,
            inflows,
            outflows,
            safety_buffer,
        )
        st.markdown(grid_html + summary, unsafe_allow_html=True)

        cur_sunday += timedelta(days=7)
