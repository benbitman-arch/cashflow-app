"""Cash flow calendar — Streamlit entry point."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

import exclude_list
from calc import daily_balance_series, projected_balance
from calendar_view import render_range
from parsers import parse_checks, parse_omd_debt, parse_suppliers_debt
from persistence import load_snapshot, save_snapshot

st.set_page_config(page_title="Cash Flow Calendar", layout="wide")


# ---------- password gate ----------
def _check_password() -> bool:
    expected = st.secrets.get("app_password", "")
    if not expected:
        st.error("App password not configured. Set `app_password` in Streamlit secrets.")
        return False
    if st.session_state.get("auth_ok"):
        return True
    st.title("🔒 Cash Flow Calendar")
    pw = st.text_input("Password", type="password", key="_pw_in")
    if st.button("Enter") and pw:
        if pw == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


if not _check_password():
    st.stop()


# ---------- session state ----------
ss = st.session_state
ss.setdefault("payments_out", pd.DataFrame())
ss.setdefault("payments_in", pd.DataFrame())
ss.setdefault("excluded_omd", pd.DataFrame())
ss.setdefault("loaded_from_snapshot", False)

if not ss.loaded_from_snapshot:
    snap = load_snapshot()
    if snap:
        ss.payments_out = snap["payments_out"]
        ss.payments_in = snap["payments_in"]
        ss.current_bank = snap["current_bank"]
        ss.safety_buffer = snap["safety_buffer"]
        ss.terms_days_str = ",".join(str(t) for t in snap["terms_days"])
        ss.snapshot_saved_at = snap.get("saved_at")
    ss.loaded_from_snapshot = True

ss.setdefault("current_bank", 0.0)
ss.setdefault("safety_buffer", 0.0)
ss.setdefault("terms_days_str", "0,30,45,60,75")
ss.setdefault("snapshot_saved_at", None)

# ---------- sidebar ----------
with st.sidebar:
    st.header("Uploads")
    f_checks = st.file_uploader("Checks (Israeli suppliers)", type=["xls", "xlsx"], key="f_checks")
    f_sdebt = st.file_uploader("Suppliers Debt (foreign)", type=["xls", "xlsx"], key="f_sdebt")
    f_omd = st.file_uploader("OMD Debt (customers)", type=["xls", "xlsx"], key="f_omd")

    if st.button("Parse uploads", use_container_width=True, type="primary"):
        outflows = []
        excluded_omd = pd.DataFrame()
        try:
            if f_checks:
                r = parse_checks(f_checks)
                outflows.append(r.payments)
                st.success(f"Checks: {len(r.payments)} payments")
            if f_sdebt:
                r = parse_suppliers_debt(f_sdebt)
                outflows.append(r.payments)
                st.success(f"Suppliers debt: {len(r.payments)} payments")
            if outflows:
                # unify date column to due_date
                ss.payments_out = pd.concat(outflows, ignore_index=True)
            if f_omd:
                r = parse_omd_debt(f_omd)
                ss.payments_in = r.payments
                ss.excluded_omd = r.excluded
                st.success(
                    f"OMD: {len(r.payments)} receivables, {len(r.excluded)} excluded"
                )
        except Exception as e:
            st.error(f"Parse failed: {e}")

    st.divider()
    st.header("Settings")
    ss.current_bank = st.number_input(
        "Current bank balance (USD)", value=float(ss.current_bank), step=1000.0, format="%.2f"
    )
    ss.safety_buffer = st.number_input(
        "Safety buffer (USD)", value=float(ss.safety_buffer), step=1000.0, format="%.2f"
    )
    ss.terms_days_str = st.text_input("Terms (days, comma-separated)", value=ss.terms_days_str)
    try:
        terms_days = [int(x.strip()) for x in ss.terms_days_str.split(",") if x.strip()]
    except ValueError:
        st.error("Terms must be integers separated by commas, e.g. 0,30,45,60,75")
        terms_days = [0, 30, 45, 60, 75]

    today = date.today()
    horizon = st.slider("Calendar horizon (days)", 30, 180, 90, step=30)
    start_date = today
    end_date = today + timedelta(days=horizon)

    st.divider()
    if st.button("💾 Save snapshot", use_container_width=True):
        save_snapshot(
            ss.payments_out, ss.payments_in, ss.current_bank, ss.safety_buffer, terms_days
        )
        st.success("Saved.")
    if ss.snapshot_saved_at:
        st.caption(f"Last saved: {ss.snapshot_saved_at}")

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
outflows = ss.payments_out

if outflows.empty and inflows.empty:
    st.info("Upload your 3 Excel files in the sidebar and click **Parse uploads** to start.")
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
