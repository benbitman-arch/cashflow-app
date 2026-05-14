"""Cash flow calendar — Streamlit entry point."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import exclude_list
from calc import daily_balance_series, max_buy, projected_balance
from calendar_view import render_range
from parsers import parse_checks, parse_omd_debt, parse_suppliers_debt
from persistence import (
    GH_REPO_DEFAULT,
    build_snapshot,
    load_from_github,
    parse_snapshot,
    save_to_github,
)

st.set_page_config(page_title="Cash Flow Calendar", layout="wide", page_icon="💰")


# ---------- password gate with remember-me ----------
def _expected_token(pw: str) -> str:
    return hashlib.sha256(("cashflow:" + pw).encode()).hexdigest()[:32]


def _check_password() -> bool:
    expected = st.secrets.get("app_password", "")
    if not expected:
        st.error("App password not configured. Set `app_password` in Streamlit secrets.")
        return False

    token = _expected_token(expected)
    if st.query_params.get("t") == token:
        st.session_state["auth_ok"] = True

    if st.session_state.get("auth_ok"):
        return True

    st.title("🔒 Cash Flow Calendar")
    st.caption("Enter the password to continue. Tick 'Remember me' and bookmark the URL to skip this next time.")
    pw = st.text_input("Password", type="password", key="_pw_in")
    remember = st.checkbox("Remember me on this device", value=True)
    if st.button("Enter", type="primary") and pw:
        if pw == expected:
            st.session_state["auth_ok"] = True
            if remember:
                st.query_params["t"] = token
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


if not _check_password():
    st.stop()


# ---------- session state + cloud load ----------
ss = st.session_state
ss.setdefault("payments_out_by_source", {})  # {"checks": df, "suppliers_debt": df}
ss.setdefault("payments_in", pd.DataFrame())
ss.setdefault("excluded_omd", pd.DataFrame())
ss.setdefault("loaded", False)
ss.setdefault("current_bank", 0.0)
ss.setdefault("safety_buffer", 0.0)
ss.setdefault("terms_days_str", "0,30,45,60,75")
ss.setdefault("snapshot_saved_at", None)

GH_TOKEN = st.secrets.get("github_token", "")
GH_REPO = st.secrets.get("github_repo", GH_REPO_DEFAULT)

if not ss.loaded:
    snap = load_from_github(repo=GH_REPO, token=GH_TOKEN or None)
    if snap:
        parsed = parse_snapshot(snap)
        ss.payments_out_by_source = parsed["payments_out_by_source"]
        ss.payments_in = parsed["payments_in"]
        ss.excluded_omd = parsed["excluded_omd"]
        ss.current_bank = parsed["current_bank"]
        ss.safety_buffer = parsed["safety_buffer"]
        ss.terms_days_str = ",".join(str(t) for t in parsed["terms_days"])
        ss.snapshot_saved_at = parsed.get("saved_at")
    ss.loaded = True


def _outflows() -> pd.DataFrame:
    parts = [df for df in ss.payments_out_by_source.values() if df is not None and not df.empty]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _push_snapshot(reason: str) -> None:
    if not GH_TOKEN:
        st.warning(
            "Snapshot not pushed: `github_token` is not set in Streamlit secrets. "
            "Data persists only for this session."
        )
        return
    try:
        terms_days = [int(x.strip()) for x in ss.terms_days_str.split(",") if x.strip()]
    except ValueError:
        terms_days = [0, 30, 45, 60, 75]
    snap = build_snapshot(
        payments_out_by_source=ss.payments_out_by_source,
        payments_in=ss.payments_in,
        excluded_omd=ss.excluded_omd,
        current_bank=ss.current_bank,
        safety_buffer=ss.safety_buffer,
        terms_days=terms_days,
    )
    ok, msg = save_to_github(snap, token=GH_TOKEN, repo=GH_REPO)
    if ok:
        ss.snapshot_saved_at = snap["saved_at"]
        st.success(f"☁️ Snapshot saved to cloud ({reason})")
    else:
        st.error(f"Cloud save failed: {msg}")


# ---------- sidebar ----------
with st.sidebar:
    st.header("Uploads")
    f_checks = st.file_uploader("Checks (Israeli suppliers)", type=["xls", "xlsx"], key="f_checks")
    f_sdebt = st.file_uploader("Suppliers Debt (foreign)", type=["xls", "xlsx"], key="f_sdebt")
    f_omd = st.file_uploader("OMD Debt (customers)", type=["xls", "xlsx"], key="f_omd")

    if st.button("Parse uploads", use_container_width=True, type="primary"):
        touched_anything = False
        try:
            if f_checks:
                r = parse_checks(f_checks)
                ss.payments_out_by_source["checks"] = r.payments
                st.success(f"Checks: {len(r.payments)} payments")
                touched_anything = True
            if f_sdebt:
                r = parse_suppliers_debt(f_sdebt)
                ss.payments_out_by_source["suppliers_debt"] = r.payments
                st.success(f"Suppliers debt: {len(r.payments)} payments")
                touched_anything = True
            if f_omd:
                r = parse_omd_debt(f_omd)
                ss.payments_in = r.payments
                ss.excluded_omd = r.excluded
                st.success(
                    f"OMD: {len(r.payments)} receivables, {len(r.excluded)} excluded"
                )
                touched_anything = True
            if touched_anything:
                _push_snapshot("after upload")
            else:
                st.info("No new files selected.")
        except Exception as e:
            st.error(f"Parse failed: {e}")

    st.divider()
    st.header("Settings")
    new_bank = st.number_input(
        "Current bank balance (USD)", value=float(ss.current_bank), step=1000.0, format="%.2f"
    )
    new_buffer = st.number_input(
        "Safety buffer (USD)", value=float(ss.safety_buffer), step=1000.0, format="%.2f"
    )
    new_terms = st.text_input("Terms (days, comma-separated)", value=ss.terms_days_str)
    try:
        terms_days = [int(x.strip()) for x in new_terms.split(",") if x.strip()]
    except ValueError:
        st.error("Terms must be integers separated by commas, e.g. 0,30,45,60,75")
        terms_days = [0, 30, 45, 60, 75]

    settings_changed = (
        new_bank != ss.current_bank
        or new_buffer != ss.safety_buffer
        or new_terms != ss.terms_days_str
    )
    if st.button(
        "💾 Save settings to cloud",
        use_container_width=True,
        disabled=not settings_changed,
    ):
        ss.current_bank = float(new_bank)
        ss.safety_buffer = float(new_buffer)
        ss.terms_days_str = new_terms
        _push_snapshot("settings change")
    else:
        # apply in-session even without save, so calendar reflects current inputs
        ss.current_bank = float(new_bank)
        ss.safety_buffer = float(new_buffer)
        ss.terms_days_str = new_terms

    today = date.today()
    horizon = st.slider("Calendar horizon (days)", 30, 180, 90, step=30)
    start_date = today
    end_date = today + timedelta(days=horizon)

    if ss.snapshot_saved_at:
        st.caption(f"☁️ Last cloud snapshot: {ss.snapshot_saved_at}")

    st.divider()
    st.header("Exclude list")
    with st.expander(f"{len(exclude_list.EXCLUDE_NAMES)} hardcoded names"):
        for n in exclude_list.EXCLUDE_NAMES:
            st.write(f"• {n}")
    new_name = st.text_input("Add customer name to exclude")
    if st.button("Add to exclude list") and new_name:
        if exclude_list.add_name(new_name):
            st.success(f"Added '{new_name}'. Re-parse uploads to apply.")
        else:
            st.info("Already excluded.")

# ---------- main pane ----------
st.title("💰 Cash Flow Calendar")

inflows = ss.payments_in
outflows = _outflows()

if outflows.empty and inflows.empty:
    st.markdown(
        """
        <div style='padding:20px;border-radius:12px;background:#f0f7ff;border:1px solid #c9def0;'>
            <h3 style='margin-top:0'>👋 Welcome</h3>
            <p>To get started, upload the 3 Excel files in the left sidebar:</p>
            <ul>
                <li><b>Checks</b> — Israeli supplier checks (column: <code>TTL Invoice</code>, <code>תאריך תחזית</code>)</li>
                <li><b>Suppliers Debt</b> — Foreign supplier balances (<code>יתרה לתשלום</code>, <code>Due Date</code>)</li>
                <li><b>OMD Debt</b> — Customer receivables (<code>Debit Amount</code>, <code>תאריך ערך</code>)</li>
            </ul>
            <p>Then click <b>Parse uploads</b>. Your data and bank balance are saved to the cloud so other users (and you, next time) see the latest state.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.stop()

# summary cards
total_in = float(inflows["amount_usd"].sum()) if not inflows.empty else 0.0
total_out = float(outflows["amount_usd"].sum()) if not outflows.empty else 0.0
net_horizon = projected_balance(end_date, ss.current_bank, inflows, outflows)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Current bank", f"${ss.current_bank:,.0f}")
c2.metric("Receivables (in)", f"${total_in:,.0f}")
c3.metric("Payments (out)", f"${total_out:,.0f}")
c4.metric(f"Projected at +{horizon}d", f"${net_horizon:,.0f}")

# Today's recommendation hero card
st.markdown("### 🎯 Today's buy budget")
today_terms_html = []
for t in terms_days:
    val = max_buy(today, t, ss.current_bank, inflows, outflows, ss.safety_buffer)
    label = "Cash today" if t == 0 else f"{t} days"
    if val > 0:
        amt_str = f"${val/1_000_000:.2f}M" if val >= 1_000_000 else f"${val/1_000:.1f}K" if val >= 1_000 else f"${val:.0f}"
        bg, fg = "#e6f4ea", "#137333"
        body = f"<div style='font-size:13px;opacity:0.7'>{label}</div><div style='font-size:22px;font-weight:600'>{amt_str}</div>"
    else:
        bg, fg = "#fce8e6", "#a50e0e"
        body = f"<div style='font-size:13px;opacity:0.7'>{label}</div><div style='font-size:22px;font-weight:600'>—</div>"
    today_terms_html.append(
        f"<div style='flex:1;padding:14px;border-radius:10px;background:{bg};color:{fg};min-width:120px;text-align:center'>{body}</div>"
    )
st.markdown(
    f"<div style='display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px'>{''.join(today_terms_html)}</div>",
    unsafe_allow_html=True,
)

tab_cal, tab_chart, tab_data, tab_day = st.tabs(
    ["📅 Calendar", "📈 Balance chart", "🗂 Raw data", "🔍 Daily detail"]
)

with tab_cal:
    st.caption(
        "Each cell shows the max amount you can commit to buying on that day, by payment term. "
        "Green = OK, red = projected balance is zero/negative (cannot buy)."
    )
    render_range(
        start_date, end_date, ss.current_bank, inflows, outflows, terms_days, ss.safety_buffer
    )

with tab_chart:
    series = daily_balance_series(start_date, end_date, ss.current_bank, inflows, outflows)
    series_indexed = series.set_index("date")
    st.line_chart(series_indexed)
    below = series[series["balance"] < ss.safety_buffer]
    if not below.empty:
        st.warning(
            f"Balance drops below buffer (${ss.safety_buffer:,.0f}) on "
            f"{len(below)} day(s); first: {below.iloc[0]['date']}"
        )

with tab_data:
    st.subheader("Payments out (combined)")
    st.dataframe(outflows, use_container_width=True, height=300)
    st.subheader("Receivables")
    st.dataframe(inflows, use_container_width=True, height=300)
    st.subheader(f"Excluded from OMD ({len(ss.excluded_omd)})")
    st.dataframe(ss.excluded_omd, use_container_width=True, height=200)

with tab_day:
    pick = st.date_input("Pick a decision day", value=today, min_value=start_date, max_value=end_date)
    for t in terms_days:
        pay_day = pick + timedelta(days=t)
        bal = projected_balance(pay_day, ss.current_bank, inflows, outflows)
        st.markdown(f"### Term {t}d — pay on {pay_day} — projected balance ${bal:,.0f}")
        oc = outflows[outflows["due_date"] <= pay_day] if not outflows.empty else pd.DataFrame()
        ic = inflows[inflows["value_date"] <= pay_day] if not inflows.empty else pd.DataFrame()
        col_a, col_b = st.columns(2)
        col_a.caption(f"Payments due by then: ${oc['amount_usd'].sum():,.0f}" if not oc.empty else "No payments")
        col_b.caption(f"Receivables by then: ${ic['amount_usd'].sum():,.0f}" if not ic.empty else "No receivables")
