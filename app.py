"""Cash flow calendar — Streamlit entry point."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import streamlit.components.v1 as components

import exclude_list
from calc import (
    apply_customer_payment_delay,
    apply_payment_plans,
    compute_avg_customer_terms,
    daily_balance_series,
    max_buy,
    project_fm_deposits,
    project_future_sales,
    project_weekly_expenses,
    projected_balance,
    weekly_summary,
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
ss.setdefault("customer_payment_delay_days", 0)
ss.setdefault("weekly_expenses", 0.0)
ss.setdefault("payment_plans", [])
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
        if parsed.get("customer_payment_delay_days") is not None:
            ss.customer_payment_delay_days = int(parsed["customer_payment_delay_days"])
        if parsed.get("weekly_expenses") is not None:
            ss.weekly_expenses = float(parsed["weekly_expenses"])
        if parsed.get("payment_plans"):
            ss.payment_plans = list(parsed["payment_plans"])
        ss.snapshot_saved_at = parsed.get("saved_at")
    # Mark the just-loaded settings as the baseline so we don't immediately re-save.
    def _plans_signature(plans: list[dict]):
        return tuple(
            sorted(
                (
                    p.get("customer", ""),
                    float(p.get("monthly_amount", 0)),
                    int(p.get("months", 0)),
                    str(p.get("start_date", "")),
                    float(p.get("plan_amount", 0) or 0),
                )
                for p in plans
            )
        )

    ss._plans_signature_fn = _plans_signature
    ss.last_saved_settings = (
        ss.current_bank,
        ss.safety_buffer,
        ss.terms_days_str,
        ss.weekly_sales,
        ss.customer_terms_days,
        ss.customer_payment_delay_days,
        ss.fm_trading_weekly,
        ss.weekly_expenses,
        _plans_signature(ss.payment_plans),
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
        payment_plans=ss.payment_plans,
        customer_payment_delay_days=ss.customer_payment_delay_days,
        weekly_expenses=ss.weekly_expenses,
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
        late_str = st.text_input(
            "Customer late payment delay (days)",
            value=str(int(ss.customer_payment_delay_days)),
            help=(
                "On average, customers pay this many days after the invoice's "
                "value date. Shifts every OMD invoice forward by this many days "
                "in the projection. Set to 0 if customers pay on time."
            ),
        )

        st.markdown("##### 🏢 FM Trading (sister company)")
        fm_weekly_str = st.text_input(
            "FM Trading weekly deposit (USD)",
            value=f"{ss.fm_trading_weekly:,.0f}",
            help="FM Trading deposits weekly into the Israel bank. Added directly to inflows (no terms delay).",
        )

        st.markdown("##### 💸 Weekly company expenses")
        weekly_exp_str = st.text_input(
            "Weekly expenses (USD)",
            value=f"{ss.weekly_expenses:,.0f}",
            help=(
                "Recurring weekly costs going OUT of the company — employee salaries, "
                "office expenses, etc. Generates a synthetic outflow every 7 days starting "
                "next week."
            ),
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
        ss.customer_payment_delay_days = max(
            0, min(180, _parse_int(late_str, ss.customer_payment_delay_days))
        )
        ss.fm_trading_weekly = _parse_money(fm_weekly_str, ss.fm_trading_weekly)
        ss.weekly_expenses = _parse_money(weekly_exp_str, ss.weekly_expenses)

    try:
        terms_days = [int(x.strip()) for x in ss.terms_days_str.split(",") if x.strip()]
    except ValueError:
        st.error("Terms must be integers separated by commas, e.g. 0,30,45,60,75")
        terms_days = [0, 30, 45, 60, 75]

    # Auto-save to cloud whenever a setting changed compared to the last persisted state.
    _plans_signature_fn = ss.get("_plans_signature_fn")
    plans_sig = (
        _plans_signature_fn(ss.payment_plans)
        if _plans_signature_fn
        else tuple()
    )
    current_settings = (
        ss.current_bank,
        ss.safety_buffer,
        ss.terms_days_str,
        ss.weekly_sales,
        ss.customer_terms_days,
        ss.customer_payment_delay_days,
        ss.fm_trading_weekly,
        ss.weekly_expenses,
        plans_sig,
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
    st.header("Customer payment plans")
    st.caption(
        "Spread a customer's **overdue** balance into a monthly schedule. "
        "On-time invoices stay on their original OMD value dates — only the "
        "overdue portion is replaced by the plan."
    )

    # List existing plans with inline edit
    if ss.payment_plans:
        for i, p in enumerate(ss.payment_plans):
            plan_total = float(p['monthly_amount']) * int(p['months'])
            edit_key = f"_edit_plan_{i}"
            if ss.get(edit_key, False):
                # Inline edit form for this plan
                with st.container(border=True):
                    st.markdown(f"**Editing: {p['customer']}**")
                    e_monthly_str = st.text_input(
                        "Monthly amount (USD)",
                        value=f"{float(p['monthly_amount']):,.0f}",
                        key=f"_edit_monthly_{i}",
                    )
                    e_months = st.number_input(
                        "Number of months",
                        min_value=1,
                        max_value=120,
                        value=int(p['months']),
                        step=1,
                        key=f"_edit_months_{i}",
                    )
                    try:
                        e_start = date.fromisoformat(str(p['start_date']))
                    except ValueError:
                        e_start = today + timedelta(days=30)
                    e_start_input = st.date_input(
                        "First payment date",
                        value=e_start,
                        key=f"_edit_start_{i}",
                    )
                    try:
                        e_monthly = float(
                            str(e_monthly_str).replace(",", "").replace(" ", "").replace("$", "")
                        )
                    except ValueError:
                        e_monthly = 0.0

                    cols_e = st.columns(2)
                    if cols_e[0].button("💾 Save", key=f"_save_edit_{i}", type="primary", use_container_width=True):
                        if e_monthly <= 0 or e_months < 1:
                            st.error("Monthly amount and months must be positive.")
                        else:
                            ss.payment_plans[i] = {
                                "customer": p["customer"],
                                "monthly_amount": e_monthly,
                                "months": int(e_months),
                                "start_date": e_start_input.isoformat(),
                                "plan_amount": e_monthly * int(e_months),
                            }
                            ss[edit_key] = False
                            st.rerun()
                    if cols_e[1].button("Cancel", key=f"_cancel_edit_{i}", use_container_width=True):
                        ss[edit_key] = False
                        st.rerun()
            else:
                cols = st.columns([6, 1, 1])
                cols[0].markdown(
                    f"**{p['customer']}**  \n"
                    f"<span style='font-size:12px;color:#666'>"
                    f"${float(p['monthly_amount']):,.0f}/mo × {int(p['months'])} mo "
                    f"from {p['start_date']} = ${plan_total:,.0f} "
                    f"<i>(replaces overdue)</i></span>",
                    unsafe_allow_html=True,
                )
                if cols[1].button("✎", key=f"_edit_btn_{i}", help="Edit this plan"):
                    ss[edit_key] = True
                    st.rerun()
                if cols[2].button("✕", key=f"_remove_plan_{i}", help="Remove this plan"):
                    ss.payment_plans.pop(i)
                    st.rerun()
    else:
        st.caption("_No plans yet._")

    # Add a new plan
    with st.expander("➕ Add payment plan"):
        # Customer dropdown sourced from OMD data
        if not ss.payments_in.empty and "party" in ss.payments_in.columns:
            all_customers = sorted(
                set(str(n) for n in ss.payments_in["party"].dropna() if str(n).strip())
            )
        else:
            all_customers = []

        if not all_customers:
            st.warning("Upload OMD file first to see your customer list.")
        else:
            sel_customer = st.selectbox(
                "Customer (type to search)",
                options=all_customers,
                key="_plan_customer",
            )

            # Build the per-customer breakdown panel
            cust_rows = ss.payments_in[ss.payments_in["party"] == sel_customer]
            sel_owes = float(cust_rows["amount_usd"].sum()) if not cust_rows.empty else 0.0
            n_invoices = len(cust_rows)
            if "is_overdue" in cust_rows.columns and not cust_rows.empty:
                overdue_amt_c = float(
                    cust_rows.loc[cust_rows["is_overdue"], "amount_usd"].sum()
                )
                ontime_amt_c = float(
                    cust_rows.loc[~cust_rows["is_overdue"], "amount_usd"].sum()
                )
            else:
                overdue_amt_c = 0.0
                ontime_amt_c = sel_owes

            date_col = (
                "original_value_date"
                if "original_value_date" in cust_rows.columns
                else "value_date"
            )
            if not cust_rows.empty and date_col in cust_rows.columns:
                dates = pd.to_datetime(cust_rows[date_col], errors="coerce").dropna()
                date_range = (
                    f"{dates.min().date()} → {dates.max().date()}"
                    if not dates.empty
                    else "—"
                )
            else:
                date_range = "—"

            st.markdown(
                f"""
                <div style='padding:12px;border-radius:8px;background:#eef5ff;border:1px solid #b7d4f7;margin:8px 0'>
                    <div style='font-size:12px;color:#1a4d8f;font-weight:600;text-transform:uppercase;letter-spacing:.5px'>Selected customer</div>
                    <div style='font-size:16px;font-weight:600;margin:4px 0 8px 0'>{sel_customer}</div>
                    <div style='display:flex;gap:18px;flex-wrap:wrap'>
                        <div>
                            <div style='font-size:11px;color:#666'>Total owed</div>
                            <div style='font-size:22px;font-weight:600;color:#1a73e8'>${sel_owes:,.0f}</div>
                        </div>
                        <div>
                            <div style='font-size:11px;color:#666'>Invoices</div>
                            <div style='font-size:18px;font-weight:600'>{n_invoices}</div>
                        </div>
                        <div>
                            <div style='font-size:11px;color:#666'>On-time</div>
                            <div style='font-size:14px;color:#0d652d'>${ontime_amt_c:,.0f}</div>
                        </div>
                        <div>
                            <div style='font-size:11px;color:#666'>Overdue</div>
                            <div style='font-size:14px;color:#b71c1c'>${overdue_amt_c:,.0f}</div>
                        </div>
                    </div>
                    <div style='font-size:11px;color:#666;margin-top:8px'>Invoice dates: {date_range}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # Initialize session state defaults once; widgets read from there
            # so on_click callbacks can mutate before the next rerun.
            st.session_state.setdefault("_plan_monthly_str", "0")
            st.session_state.setdefault("_plan_months", 10)

            # Plan basis = overdue portion only. User can override.
            st.session_state.setdefault(
                "_plan_amount_str", f"{overdue_amt_c:,.0f}"
            )
            # Re-default the plan amount when the customer changes
            if st.session_state.get("_plan_amount_for") != sel_customer:
                st.session_state["_plan_amount_str"] = f"{overdue_amt_c:,.0f}"
                st.session_state["_plan_amount_for"] = sel_customer

            plan_amount_str = st.text_input(
                "Plan amount (USD)  — overdue to spread",
                key="_plan_amount_str",
                help="Defaults to this customer's overdue total. Edit if you want to spread a different amount.",
            )
            try:
                plan_amount_val = float(
                    str(plan_amount_str)
                    .replace(",", "")
                    .replace(" ", "")
                    .replace("$", "")
                )
            except ValueError:
                plan_amount_val = 0.0

            st.session_state.setdefault("_plan_monthly_str", "0")
            st.session_state.setdefault("_plan_months", 10)

            col_a, col_b = st.columns(2)
            monthly_str = col_a.text_input(
                "Monthly amount (USD)", key="_plan_monthly_str"
            )
            months_input = col_b.number_input(
                "Number of months",
                min_value=1,
                max_value=120,
                step=1,
                key="_plan_months",
            )
            start_input = st.date_input(
                "First payment date",
                value=today + timedelta(days=30),
                key="_plan_start",
            )

            try:
                monthly_val = float(
                    str(monthly_str).replace(",", "").replace(" ", "").replace("$", "")
                )
            except ValueError:
                monthly_val = 0.0

            # Suggestion shortcuts — must use on_click callbacks so session_state
            # updates run BEFORE widgets re-instantiate on the next rerun.
            def _apply_shortcut(amount: float, months: int | None = None) -> None:
                st.session_state["_plan_monthly_str"] = f"{amount:,.0f}"
                if months is not None:
                    st.session_state["_plan_months"] = months

            basis = plan_amount_val if plan_amount_val > 0 else overdue_amt_c

            if basis > 0:
                st.caption(f"Quick fill (spreads ${basis:,.0f}):")
                st.button(
                    f"Split equally over {int(months_input)} months  →  "
                    f"${basis / int(months_input):,.0f}/mo",
                    key="_sg_split",
                    on_click=_apply_shortcut,
                    args=(basis / int(months_input),),
                    use_container_width=True,
                )
                st.button(
                    f"Pay over 6 months  →  ${basis / 6:,.0f}/mo",
                    key="_sg_6",
                    on_click=_apply_shortcut,
                    args=(basis / 6, 6),
                    use_container_width=True,
                )
                st.button(
                    f"Pay over 12 months  →  ${basis / 12:,.0f}/mo",
                    key="_sg_12",
                    on_click=_apply_shortcut,
                    args=(basis / 12, 12),
                    use_container_width=True,
                )

                # Auto-compute months from the monthly amount
                if monthly_val > 0:
                    import math

                    auto_months = max(1, min(120, math.ceil(basis / monthly_val)))
                    last_overshoot = monthly_val * auto_months - basis
                    last_note = (
                        f" (last payment ${monthly_val - last_overshoot:,.0f})"
                        if abs(last_overshoot) > 0.5
                        else ""
                    )
                    st.button(
                        f"At ${monthly_val:,.0f}/mo  →  set months to {auto_months}{last_note}",
                        key="_sg_auto_months",
                        on_click=_apply_shortcut,
                        args=(monthly_val, auto_months),
                        use_container_width=True,
                    )

            plan_total = monthly_val * int(months_input)
            if monthly_val > 0 and basis > 0:
                diff = plan_total - basis
                if abs(diff) < 1.0:
                    st.success(
                        f"✓ Plan total ${plan_total:,.0f} covers the spread amount."
                    )
                else:
                    st.info(
                        f"Plan total: ${plan_total:,.0f}  |  Spread amount: ${basis:,.0f}  "
                        f"|  Δ ${diff:+,.0f}"
                    )

            if st.button("Add plan", type="primary", key="_add_plan_btn"):
                if monthly_val <= 0 or months_input < 1:
                    st.error("Monthly amount and months must be positive.")
                else:
                    ss.payment_plans.append(
                        {
                            "customer": sel_customer,
                            "monthly_amount": monthly_val,
                            "months": int(months_input),
                            "start_date": start_input.isoformat(),
                            "plan_amount": plan_amount_val,
                        }
                    )
                    st.rerun()

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

_omd_delayed = apply_customer_payment_delay(ss.payments_in, ss.customer_payment_delay_days)
existing_inflows = apply_payment_plans(_omd_delayed, ss.payment_plans)
projected = project_future_sales(today, end_date, ss.weekly_sales, ss.customer_terms_days)
fm_deposits = project_fm_deposits(today, end_date, ss.fm_trading_weekly)
inflow_parts = [df for df in [existing_inflows, projected, fm_deposits] if not df.empty]
inflows = pd.concat(inflow_parts, ignore_index=True) if inflow_parts else pd.DataFrame()
_base_outflows = _outflows()
_weekly_exp_df = project_weekly_expenses(today, end_date, ss.weekly_expenses)
outflow_parts = [df for df in [_base_outflows, _weekly_exp_df] if not df.empty]
outflows = pd.concat(outflow_parts, ignore_index=True) if outflow_parts else pd.DataFrame()

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
if not existing_inflows.empty and "is_overdue" in existing_inflows.columns:
    # New snapshot format: overdue is flagged inside payments_in itself
    real_in_overdue = float(
        existing_inflows.loc[existing_inflows["is_overdue"], "amount_usd"].sum()
    )
    real_in_ontime = float(
        existing_inflows.loc[~existing_inflows["is_overdue"], "amount_usd"].sum()
    )
else:
    # Old snapshot format: overdue was split into excluded_omd
    real_in_ontime = (
        float(existing_inflows["amount_usd"].sum()) if not existing_inflows.empty else 0.0
    )
    real_in_overdue = (
        float(
            ss.excluded_omd.loc[
                ss.excluded_omd["reason"] == "overdue (excluded from projection)",
                "amount",
            ].sum()
        )
        if not ss.excluded_omd.empty and "reason" in ss.excluded_omd.columns
        else 0.0
    )
real_in = real_in_ontime + real_in_overdue
projected_in = (
    (float(projected["amount_usd"].sum()) if not projected.empty else 0.0)
    + (float(fm_deposits["amount_usd"].sum()) if not fm_deposits.empty else 0.0)
)
total_out = float(outflows["amount_usd"].sum()) if not outflows.empty else 0.0
# Split total debts: real obligations (checks + supplier debt) vs synthetic
# recurring weekly expenses (salaries, office). Show full total + breakdown.
total_debts_real = (
    float(_base_outflows["amount_usd"].sum()) if not _base_outflows.empty else 0.0
)
total_debts_weekly = (
    float(_weekly_exp_df["amount_usd"].sum()) if not _weekly_exp_df.empty else 0.0
)
overdue_amt = (
    float(outflows.loc[outflows["due_date"] <= today, "amount_usd"].sum())
    if not outflows.empty
    else 0.0
)
cash_now = ss.current_bank - overdue_amt
net_horizon = projected_balance(end_date, ss.current_bank, inflows, outflows)


def _short_money(x: float) -> str:
    """Compact money: $1.23M / $123.4K / $123, with sign."""
    sign = "-" if x < 0 else ""
    a = abs(x)
    if a >= 1_000_000:
        return f"{sign}${a/1_000_000:.2f}M"
    if a >= 10_000:
        return f"{sign}${a/1_000:.0f}K"
    if a >= 1_000:
        return f"{sign}${a/1_000:.1f}K"
    return f"{sign}${a:.0f}"


c1, c2, c3, c4, c5, c6 = st.columns(6)


def _card(col, label: str, value: str, sub: str = "", color: str = "#1a1a1a"):
    """Compact metric card with adaptive font size — never overflows."""
    sub_html = (
        f"<div style='font-size:12px;color:#666;margin-top:4px;white-space:nowrap'>{sub}</div>"
        if sub
        else ""
    )
    col.markdown(
        f"<div style='font-size:13px;color:#666;white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>{label}</div>"
        f"<div style='font-size:clamp(18px, 2.2vw, 28px);font-weight:600;color:{color};line-height:1.25;white-space:nowrap;overflow:hidden'>{value}</div>"
        f"{sub_html}",
        unsafe_allow_html=True,
    )


_card(c1, "Current bank", _short_money(ss.current_bank))

# Cash now (red when negative)
cash_color = "#b71c1c" if cash_now < 0 else ("#0d652d" if cash_now > 0 else "#1a1a1a")
_card(
    c2,
    "Cash now (after overdue)",
    _short_money(cash_now),
    sub=(f"⚠️ {_short_money(overdue_amt)} overdue" if overdue_amt > 0 else ""),
    color=cash_color,
)

# Total debts = real obligations + projected weekly expenses, with breakdown.
_card(
    c3,
    "Total debts",
    _short_money(total_debts_real + total_debts_weekly),
    sub=(
        f"↓ incl. {_short_money(total_debts_weekly)} weekly expenses"
        if total_debts_weekly > 0
        else "checks + supplier debt"
    ),
    color="#b71c1c",
)

# Real receivables
_card(
    c4,
    "Real receivables",
    _short_money(real_in),
    sub=(f"↑ incl. {_short_money(real_in_overdue)} overdue" if real_in_overdue > 0 else ""),
)
_card(c5, "Projected receivables", _short_money(projected_in), sub=f"sales+FM over {horizon}d")
_card(
    c6,
    f"Projected at +{horizon}d",
    _short_money(net_horizon),
    color=("#b71c1c" if net_horizon < 0 else "#0d652d" if net_horizon > 0 else "#1a1a1a"),
)

if overdue_amt > 0 and cash_now < 0:
    st.error(
        f"⚠️ Outstanding payments due by today (${overdue_amt:,.0f}) exceed your current "
        f"bank balance (${ss.current_bank:,.0f}) by ${-cash_now:,.0f}. "
        f"The calendar projects when you'll close this gap (assuming projected sales, "
        f"FM Trading deposits, overdue customer collections, and on-time customer "
        f"payments all arrive)."
    )

if real_in_overdue > 0:
    st.info(
        f"ℹ️ ${real_in_overdue:,.0f} in overdue customer receivables are counted in "
        f"the projection as arriving **today** (clamped). If your late customers "
        f"won't pay that fast, the projection is optimistic."
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

tab_cal, tab_weekly, tab_chart, tab_data, tab_day = st.tabs(
    ["📅 Calendar", "📊 Weekly summary", "📈 Balance chart", "🗂 Raw data", "🔍 Daily detail"]
)

with tab_cal:
    st.caption(
        "Each cell shows the max amount you can commit to buying on that day, by payment term. "
        "Green = OK, red = projected balance is zero/negative (cannot buy)."
    )
    render_range(
        start_date, end_date, ss.current_bank, inflows, outflows, terms_days, ss.safety_buffer
    )

with tab_weekly:
    st.caption(
        "Week-by-week roll-up. **Opening** = bank balance at the start of the week. "
        "**In** / **Out** = receivables / payments landing in that week. "
        "**Net** = In − Out. **Closing** = projected bank balance at end of week."
    )
    ws = weekly_summary(start_date, end_date, ss.current_bank, inflows, outflows)
    if ws.empty:
        st.info("No weeks to display.")
    else:
        display_df = ws[["week_label", "opening_balance", "in_usd", "out_usd", "net_usd", "closing_balance"]].rename(
            columns={
                "week_label": "Week",
                "opening_balance": "Opening",
                "in_usd": "↑ In",
                "out_usd": "↓ Out",
                "net_usd": "Net",
                "closing_balance": "Closing",
            }
        )

        buffer = ss.safety_buffer

        def _style(row):
            styles = [""] * len(row)
            net_idx = row.index.get_loc("Net")
            close_idx = row.index.get_loc("Closing")
            open_idx = row.index.get_loc("Opening")
            if row["Net"] > 0:
                styles[net_idx] = "color: #0d652d; font-weight: 600"
            elif row["Net"] < 0:
                styles[net_idx] = "color: #b71c1c; font-weight: 600"
            if row["Closing"] < buffer:
                styles[close_idx] = "color: #b71c1c; font-weight: 600"
            else:
                styles[close_idx] = "color: #0d652d; font-weight: 600"
            if row["Opening"] < buffer:
                styles[open_idx] = "color: #b71c1c"
            return styles

        styled = display_df.style.apply(_style, axis=1).format(
            {
                "Opening": "${:,.0f}",
                "↑ In": "${:,.0f}",
                "↓ Out": "${:,.0f}",
                "Net": "${:,.0f}",
                "Closing": "${:,.0f}",
            }
        )
        st.dataframe(styled, use_container_width=True, hide_index=True, height=520)

        below = ws[ws["closing_balance"] < buffer]
        if not below.empty:
            first_bad = below.iloc[0]
            st.warning(
                f"⚠️ Closing balance below buffer (${buffer:,.0f}) for {len(below)} week(s). "
                f"First: week of {first_bad['week_label']} → ${first_bad['closing_balance']:,.0f}."
            )
        else:
            st.success("✓ Every week's closing balance stays above the safety buffer.")

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
