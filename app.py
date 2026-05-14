"""Cash flow calendar — Streamlit entry point."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import streamlit.components.v1 as components

import exclude_list
from calc import (
    compute_avg_customer_terms,
    daily_balance_series,
    max_buy,
    project_fm_deposits,
    project_future_sales,
    projected_balance,
)
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


# ---------- live comma formatting for money fields ----------
_MONEY_FORMATTER_JS = """
<script>
(function () {
  const doc = window.parent ? window.parent.document : document;
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value'
  ).set;

  function format(value) {
    if (!value) return '';
    let v = ('' + value).replace(/[^\\d.-]/g, '');
    const dot = v.indexOf('.');
    if (dot >= 0) v = v.slice(0, dot + 1) + v.slice(dot + 1).replace(/\\./g, '');
    let [intPart, fracPart] = v.split('.');
    intPart = (intPart || '').replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',');
    return fracPart !== undefined ? intPart + '.' + fracPart : intPart;
  }

  function isMoney(input) {
    const block = input.closest('[data-testid="stTextInput"]');
    if (!block) return false;
    const labelEl = block.querySelector('label');
    const txt = (labelEl && labelEl.textContent) || input.getAttribute('aria-label') || '';
    return /USD|balance|sales|deposit|buffer/i.test(txt);
  }

  function attach(input) {
    if (input._mfAttached) return;
    input._mfAttached = true;
    input.addEventListener('input', function (e) {
      const oldVal = e.target.value;
      const cursor = e.target.selectionStart;
      const newVal = format(oldVal);
      if (newVal === oldVal) return;
      setter.call(e.target, newVal);
      const delta = newVal.length - oldVal.length;
      try { e.target.setSelectionRange(cursor + delta, cursor + delta); } catch (_) {}
      e.target.dispatchEvent(new Event('input', { bubbles: true }));
    });
  }

  function scan() {
    doc.querySelectorAll('input[type="text"]').forEach(function (inp) {
      if (isMoney(inp)) attach(inp);
    });
  }

  scan();
  if (!doc._mfObserver) {
    doc._mfObserver = new MutationObserver(scan);
    doc._mfObserver.observe(doc.body, { childList: true, subtree: true });
  }
})();
</script>
"""


def _inject_money_formatter():
    components.html(_MONEY_FORMATTER_JS, height=0)


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
ss.setdefault("weekly_sales", 0.0)
ss.setdefault("customer_terms_days", 30)
ss.setdefault("fm_trading_weekly", 0.0)
ss.setdefault("snapshot_saved_at", None)
ss.setdefault("last_saved_settings", None)

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
        if parsed.get("weekly_sales") is not None:
            ss.weekly_sales = float(parsed["weekly_sales"])
        if parsed.get("customer_terms_days") is not None:
            ss.customer_terms_days = int(parsed["customer_terms_days"])
        if parsed.get("fm_trading_weekly") is not None:
            ss.fm_trading_weekly = float(parsed["fm_trading_weekly"])
        ss.snapshot_saved_at = parsed.get("saved_at")
    # Mark the just-loaded settings as the baseline so we don't immediately re-save.
    ss.last_saved_settings = (
        ss.current_bank,
        ss.safety_buffer,
        ss.terms_days_str,
        ss.weekly_sales,
        ss.customer_terms_days,
        ss.fm_trading_weekly,
    )
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
        weekly_sales=ss.weekly_sales,
        customer_terms_days=ss.customer_terms_days,
        fm_trading_weekly=ss.fm_trading_weekly,
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

    def _parse_money(s: str, fallback: float) -> float:
        try:
            return float(str(s).replace(",", "").replace(" ", "").replace("$", ""))
        except (ValueError, TypeError):
            return fallback

    def _parse_int(s: str, fallback: int) -> int:
        try:
            return int(float(str(s).replace(",", "").replace(" ", "")))
        except (ValueError, TypeError):
            return fallback

    detected_avg = compute_avg_customer_terms(ss.payments_in)
    default_cust_terms = (
        int(round(detected_avg)) if detected_avg is not None else int(ss.customer_terms_days)
    )

    with st.form("settings_form", border=False):
        bank_str = st.text_input(
            "Current bank balance (USD)", value=f"{ss.current_bank:,.0f}"
        )
        buffer_str = st.text_input(
            "Safety buffer (USD)", value=f"{ss.safety_buffer:,.0f}"
        )
        new_terms = st.text_input(
            "Buy terms (days, comma-separated)", value=ss.terms_days_str
        )

        st.markdown("##### 📈 Projected sales")
        if detected_avg is not None:
            st.caption(
                f"Detected average customer term from OMD data: **{detected_avg:.0f} days** "
                f"(weighted by amount)"
            )
        weekly_sales_str = st.text_input(
            "Weekly projected sales (USD)",
            value=f"{ss.weekly_sales:,.0f}",
            help="Average new sales we book each week. Projected into future receivables.",
        )
        cust_terms_str = st.text_input(
            "Customer terms (days until we get paid)",
            value=str(int(ss.customer_terms_days or default_cust_terms)),
            help="Days from sale to cash. Defaults to the detected average.",
        )

        st.markdown("##### 🏢 FM Trading (sister company)")
        fm_weekly_str = st.text_input(
            "FM Trading weekly deposit (USD)",
            value=f"{ss.fm_trading_weekly:,.0f}",
            help="FM Trading deposits weekly into the Israel bank. Added directly to inflows (no terms delay).",
        )

        submitted = st.form_submit_button(
            "✅ Apply", use_container_width=True, type="primary"
        )

    _inject_money_formatter()

    if submitted:
        ss.current_bank = _parse_money(bank_str, ss.current_bank)
        ss.safety_buffer = _parse_money(buffer_str, ss.safety_buffer)
        ss.terms_days_str = new_terms
        ss.weekly_sales = _parse_money(weekly_sales_str, ss.weekly_sales)
        ss.customer_terms_days = max(
            0, min(365, _parse_int(cust_terms_str, ss.customer_terms_days))
        )
        ss.fm_trading_weekly = _parse_money(fm_weekly_str, ss.fm_trading_weekly)

    try:
        terms_days = [int(x.strip()) for x in ss.terms_days_str.split(",") if x.strip()]
    except ValueError:
        st.error("Terms must be integers separated by commas, e.g. 0,30,45,60,75")
        terms_days = [0, 30, 45, 60, 75]

    # Auto-save to cloud whenever a setting changed compared to the last persisted state.
    current_settings = (
        ss.current_bank,
        ss.safety_buffer,
        ss.terms_days_str,
        ss.weekly_sales,
        ss.customer_terms_days,
        ss.fm_trading_weekly,
    )
    if ss.last_saved_settings != current_settings:
        ss.last_saved_settings = current_settings
        _push_snapshot("settings auto-save")
    else:
        st.caption("☁️ Click Apply to commit changes — they auto-save to cloud")

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

existing_inflows = ss.payments_in
projected = project_future_sales(today, end_date, ss.weekly_sales, ss.customer_terms_days)
fm_deposits = project_fm_deposits(today, end_date, ss.fm_trading_weekly)
inflow_parts = [df for df in [existing_inflows, projected, fm_deposits] if not df.empty]
inflows = pd.concat(inflow_parts, ignore_index=True) if inflow_parts else pd.DataFrame()
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
overdue_amt = (
    float(outflows.loc[outflows["due_date"] <= today, "amount_usd"].sum())
    if not outflows.empty
    else 0.0
)
cash_now = ss.current_bank - overdue_amt
net_horizon = projected_balance(end_date, ss.current_bank, inflows, outflows)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Current bank", f"${ss.current_bank:,.0f}")

# Custom 'Cash now' metric so we can color the value red when negative
cash_color = "#b71c1c" if cash_now < 0 else ("#0d652d" if cash_now > 0 else "#444")
sign = "-$" if cash_now < 0 else "$"
cash_value_str = f"{sign}{abs(cash_now):,.0f}"
overdue_html = (
    f"<div style='font-size:13px;color:#b71c1c;margin-top:4px'>"
    f"⚠️ ${overdue_amt:,.0f} overdue</div>"
    if overdue_amt > 0
    else ""
)
c2.markdown(
    f"<div style='font-size:14px;color:#666'>Cash now (after overdue)</div>"
    f"<div style='font-size:32px;font-weight:600;color:{cash_color};line-height:1.3'>{cash_value_str}</div>"
    f"{overdue_html}",
    unsafe_allow_html=True,
)

c3.metric("Receivables outstanding", f"${total_in:,.0f}")
c4.metric(f"Projected at +{horizon}d", f"${net_horizon:,.0f}")

if overdue_amt > 0 and cash_now < 0:
    st.error(
        f"⚠️ Outstanding payments due by today (${overdue_amt:,.0f}) exceed your current "
        f"bank balance (${ss.current_bank:,.0f}) by ${-cash_now:,.0f}. "
        f"You're cash-negative right now (before counting any incoming receivables)."
    )

# Today's recommendation hero card — shows actual balance even when negative
st.markdown("### 🎯 Today's buy budget")


def _fmt_money_signed(x: float) -> str:
    sign = "-" if x < 0 else ""
    a = abs(x)
    if a >= 1_000_000:
        return f"{sign}${a/1_000_000:.2f}M"
    if a >= 1_000:
        return f"{sign}${a/1_000:.1f}K"
    return f"{sign}${a:.0f}"


today_terms_html = []
for t in terms_days:
    raw = projected_balance(
        today + timedelta(days=t), ss.current_bank, inflows, outflows
    ) - ss.safety_buffer
    label = "Cash today" if t == 0 else f"{t} days"
    if raw > 0:
        bg, fg = "#e6f4ea", "#137333"
        sub = "can buy"
    elif raw == 0:
        bg, fg = "#fff4e5", "#b06000"
        sub = "exactly $0"
    else:
        bg, fg = "#fce8e6", "#a50e0e"
        sub = "deficit"
    amt_str = _fmt_money_signed(raw)
    body = (
        f"<div style='font-size:13px;opacity:0.7'>{label}</div>"
        f"<div style='font-size:22px;font-weight:600'>{amt_str}</div>"
        f"<div style='font-size:11px;opacity:0.7'>{sub}</div>"
    )
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
