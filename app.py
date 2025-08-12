# app.py  — Buffett-Style Investment Decision Tool (Pro+)
# Adds: yfinance peers, EV/EBIT & FCF yield, correlated MC, PDF export, and fixes scores_table bug

import io, json, math
from datetime import datetime
import numpy as np
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt

# New deps
import yfinance as yf
import numpy_financial as npf
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

st.set_page_config(page_title="Buffett-Style Investment Decision Tool (Pro+)", layout="wide")

# ---------------------------
# Generic Helpers
# ---------------------------
def _fmt_pct(x):
    try: return f"{x:.1%}"
    except: return "—"

def _fmt_num(x, decimals=0):
    try:
        if decimals == 0: return f"{x:,.0f}"
        return f"{x:,.{decimals}f}"
    except: return "—"

def safe_div(a, b):
    try:
        if b in (0, None) or (isinstance(b, float) and np.isnan(b)) or b == 0: return np.nan
        return a / b
    except: return np.nan

def owner_earnings(ebit, tax_rate, d_and_a, capex, wc_change):
    if any(pd.isna([ebit, tax_rate, d_and_a, capex, wc_change])): return np.nan
    return ebit * (1 - tax_rate) + d_and_a - capex - wc_change

def dcf_from_fcf(base_fcf, growth_yrs_1_5, wacc, terminal_growth, years=5):
    if any(pd.isna([base_fcf, growth_yrs_1_5, wacc, terminal_growth])): return np.nan, [], np.nan
    if wacc <= terminal_growth: return np.nan, [], np.nan
    cf_list, pv = [], 0.0
    for t in range(1, years + 1):
        cf_t = base_fcf * ((1 + growth_yrs_1_5) ** t)
        cf_list.append(cf_t)
        pv += cf_t / ((1 + wacc) ** t)
    fcf_6 = base_fcf * ((1 + growth_yrs_1_5) ** (years + 1))
    terminal = fcf_6 * (1 + terminal_growth) / (wacc - terminal_growth)
    total_pv = pv + terminal / ((1 + wacc) ** years)
    return total_pv, cf_list, terminal

def summarize_financials(df):
    cols = {c.lower(): c for c in df.columns}
    def has(x): return x in cols
    if has("depreciation") and has("amortization"):
        df["DandA"] = df[cols["depreciation"]].fillna(0) + df[cols["amortization"]].fillna(0)
    elif has("depreciation"):
        df["DandA"] = df[cols["depreciation"]]
    elif has("amortization"):
        df["DandA"] = df[cols["amortization"]]
    else:
        df["DandA"] = np.nan
    rev = df[cols["revenue"]] if has("revenue") else pd.Series(np.nan, index=df.index)
    ebit = df[cols["ebit"]] if has("ebit") else pd.Series(np.nan, index=df.index)
    capex = df[cols["capex"]] if has("capex") else pd.Series(np.nan, index=df.index)
    df["EBIT_margin"] = ebit / rev
    df["Capex_to_Sales"] = capex / rev
    return df

def stability_score(series: pd.Series):
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < 3: return np.nan
    mean_abs = np.abs(s.mean())
    std = s.std(ddof=1)
    if mean_abs == 0 or np.isnan(std): return np.nan
    ratio = std / mean_abs
    return float(np.clip(1 / (1 + ratio), 0, 1))

def build_excel_download(bytes_io, overview_dict, inputs_dict, dcf_table, scores_table, extra_sheets=None):
    with pd.ExcelWriter(bytes_io, engine="xlsxwriter") as writer:
        pd.DataFrame.from_dict(overview_dict, orient="index", columns=["Value"]).to_excel(writer, sheet_name="Overview")
        pd.DataFrame.from_dict(inputs_dict, orient="index", columns=["Input"]).to_excel(writer, sheet_name="Inputs")
        dcf_table.to_excel(writer, sheet_name="DCF", index=False)
        scores_table.to_excel(writer, sheet_name="Scores", index=False)
        if extra_sheets:
            for name, df in extra_sheets.items():
                df.to_excel(writer, sheet_name=name, index=False)
        for ws_name in writer.sheets.keys():
            ws = writer.sheets[ws_name]
            ws.set_column("A:A", 30)
            ws.set_column("B:Z", 18)

# ---------------------------
# yfinance Peer Helpers
# ---------------------------
def ttm_sum(df):
    """Sum last 4 quarters if quarterly dataframe; else NaN-safe sum of all columns."""
    if df is None or df.empty: return np.nan
    try:
        return float(df.iloc[:, :4].sum(axis=1)[0])
    except Exception:
        try:
            return float(df.sum(axis=1)[0])
        except Exception:
            return np.nan

def fetch_peer_metrics(ticker: str):
    """
    Returns dict with Price, MarketCap, EV, EBITDA_TTM, EBIT_TTM, FCF_TTM, NetDebt, ROE, D_E,
    P_E, EV_EBITDA, EV_EBIT, FCF_Yield
    Best-effort (yfinance fields vary by listing).
    """
    try:
        tk = yf.Ticker(ticker.strip())
        info = tk.info if hasattr(tk, "info") else {}
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        mcap = info.get("marketCap")

        # Balance sheet / cashflow / financials (quarterly preferred, fall back to annual)
        q_bs = tk.quarterly_balance_sheet
        a_bs = tk.balance_sheet
        q_cf = tk.quarterly_cashflow
        a_cf = tk.cashflow
        q_fs = tk.quarterly_financials
        a_fs = tk.financials

        # Debt & cash
        total_debt = np.nan
        cash = np.nan
        for bs in [q_bs, a_bs]:
            if bs is None or bs.empty: continue
            # yfinance labels
            for debt_key in ["Total Debt", "Long Term Debt", "Short Long Term Debt"]:
                if debt_key in bs.index:
                    v = float(bs.loc[debt_key].iloc[0])
                    total_debt = (0 if np.isnan(total_debt) else total_debt) + v
            if "Cash And Cash Equivalents" in bs.index:
                cash = float(bs.loc["Cash And Cash Equivalents"].iloc[0])
            elif "Cash" in bs.index:
                cash = float(bs.loc["Cash"].iloc[0])

        net_debt = (total_debt if not np.isnan(total_debt) else 0.0) - (cash if not np.isnan(cash) else 0.0)
        ev = (mcap if mcap else np.nan) + (net_debt if not np.isnan(net_debt) else np.nan)

        # EBIT & EBITDA TTM (Operating Income + D&A)
        ebit_ttm = np.nan
        ebitda_ttm = np.nan
        for fs in [q_fs, a_fs]:
            if fs is None or fs.empty: continue
            if "Operating Income" in fs.index:
                ebit_ttm = ttm_sum(fs.loc[["Operating Income"]])
            if "Gross Profit" in fs.index and "Operating Expenses" in fs.index and np.isnan(ebit_ttm):
                try:
                    ebit_ttm = ttm_sum(fs.loc[["Gross Profit"]]) - ttm_sum(fs.loc[["Operating Expenses"]])
                except Exception:
                    pass
        # D&A from cashflow
        dand_ttm = np.nan
        for cf in [q_cf, a_cf]:
            if cf is None or cf.empty: continue
            for key in ["Depreciation", "Depreciation And Amortization"]:
                if key in cf.index:
                    dand_ttm = ttm_sum(cf.loc[[key]])
        if not np.isnan(ebit_ttm) and not np.isnan(dand_ttm):
            ebitda_ttm = ebit_ttm + dand_ttm

        # FCF TTM
        fcf_ttm = np.nan
        for cf in [q_cf, a_cf]:
            if cf is None or cf.empty: continue
            if "Free Cash Flow" in cf.index:
                fcf_ttm = ttm_sum(cf.loc[["Free Cash Flow"]])
            else:
                cfo = ttm_sum(cf.loc[["Total Cash From Operating Activities"]]) if "Total Cash From Operating Activities" in cf.index else np.nan
                capex = ttm_sum(cf.loc[["Capital Expenditures"]]) if "Capital Expenditures" in cf.index else np.nan
                if not np.isnan(cfo) and not np.isnan(capex):
                    fcf_ttm = cfo + capex  # capex usually negative

        # ROE & D/E from info or compute
        roe = info.get("returnOnEquity")
        de = info.get("debtToEquity")
        eps = info.get("trailingEps")
        pe = price / eps if price and eps and eps != 0 else np.nan

        ev_ebitda = safe_div(ev, ebitda_ttm)
        ev_ebit = safe_div(ev, ebit_ttm)
        fcf_yield = safe_div(fcf_ttm, mcap)  # equity FCF yield

        return {
            "Ticker": ticker,
            "Price": price, "MarketCap": mcap, "EV": ev,
            "EBITDA_TTM": ebitda_ttm, "EBIT_TTM": ebit_ttm, "FCF_TTM": fcf_ttm,
            "NetDebt": net_debt, "ROE": roe, "D_E": de,
            "P_E": pe, "EV_EBITDA": ev_ebitda, "EV_EBIT": ev_ebit, "FCF_Yield": fcf_yield
        }
    except Exception:
        return {"Ticker": ticker}

# ---------------------------
# Sidebar
# ---------------------------
st.sidebar.title("Buffett-Style Tool (Pro+)")
st.sidebar.caption("Quality first • Margin of Safety • Owner mindset")

with st.sidebar.expander("Save / Load Template", expanded=False):
    default_name = f"template_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    template_name = st.text_input("Template file name", value=default_name)
    if "template_state" not in st.session_state:
        st.session_state["template_state"] = {}
    uploaded_tpl = st.file_uploader("Load template (.json)", type=["json"], key="tpl_upl")
    if uploaded_tpl:
        try:
            st.session_state["template_state"] = json.load(uploaded_tpl)
            st.success("Template loaded.")
        except Exception as e:
            st.error(f"Failed to load template: {e}")
    if st.button("Download current inputs as template"):
        b = io.BytesIO()
        b.write(json.dumps(st.session_state.get("template_state", {}), indent=2).encode("utf-8"))
        st.download_button("Save JSON", data=b.getvalue(), file_name=template_name, mime="application/json")

st.title("Buffett-Style Investment Decision Tool — Extended")

# ---------------------------
# Tabs (original 6 + peers + MC + sizing + RE)
# ---------------------------
tabs = st.tabs([
    "1) Business Quality",
    "2) Management",
    "3) Financials",
    "4) Valuation (DCF)",
    "5) Risk & MOS",
    "6) Verdict & Export",
    "7) Peer Benchmarking (Auto via yfinance)",
    "8) Monte Carlo (Correlated)",
    "9) Trade Sizing",
    "10) Real Estate"
])

# ========== 1) Business Quality ==========
with tabs[0]:
    st.subheader("Economic Moat & Competitive Advantage")
    st.markdown("Score each dimension from **0 (weak)** to **5 (exceptional)**.")
    col1, col2, col3 = st.columns(3)
    moat_brand = col1.slider("Brand Power", 0, 5, st.session_state["template_state"].get("moat_brand", 3))
    moat_cost = col1.slider("Cost Advantage", 0, 5, st.session_state["template_state"].get("moat_cost", 3))
    moat_network = col2.slider("Network Effects", 0, 5, st.session_state["template_state"].get("moat_network", 2))
    moat_switching = col2.slider("Switching Costs", 0, 5, st.session_state["template_state"].get("moat_switching", 3))
    moat_regulatory = col3.slider("Regulatory/Patents", 0, 5, st.session_state["template_state"].get("moat_regulatory", 2))
    circle_comp = col3.selectbox("Circle of Competence", ["Inside", "Borderline", "Outside"],
                                 index=["Inside","Borderline","Outside"].index(st.session_state["template_state"].get("circle_comp","Inside")))
    moat_weight = st.slider("Weight of Business Quality (%)", 10, 50, st.session_state["template_state"].get("moat_weight", 30))
    moat_avg = np.mean([moat_brand, moat_cost, moat_network, moat_switching, moat_regulatory])
    st.metric("Business Quality Score (0-5)", f"{moat_avg:.2f}")
    st.session_state["template_state"].update({
        "moat_brand": moat_brand, "moat_cost": moat_cost, "moat_network": moat_network,
        "moat_switching": moat_switching, "moat_regulatory": moat_regulatory,
        "circle_comp": circle_comp, "moat_weight": moat_weight
    })

# ========== 2) Management ==========
with tabs[1]:
    st.subheader("Management Quality & Integrity")
    col1, col2, col3 = st.columns(3)
    mgmt_capalloc = col1.slider("Capital Allocation", 0, 5, st.session_state["template_state"].get("mgmt_capalloc", 3))
    mgmt_transparency = col2.slider("Transparency & Reporting", 0, 5, st.session_state["template_state"].get("mgmt_transparency", 3))
    mgmt_alignment = col3.slider("Incentive Alignment (Long-term)", 0, 5, st.session_state["template_state"].get("mgmt_alignment", 3))
    mgmt_weight = st.slider("Weight of Management (%)", 5, 30, st.session_state["template_state"].get("mgmt_weight", 20))
    mgmt_avg = np.mean([mgmt_capalloc, mgmt_transparency, mgmt_alignment])
    st.metric("Management Score (0-5)", f"{mgmt_avg:.2f}")
    st.session_state["template_state"].update({
        "mgmt_capalloc": mgmt_capalloc, "mgmt_transparency": mgmt_transparency,
        "mgmt_alignment": mgmt_alignment, "mgmt_weight": mgmt_weight
    })

# ========== 3) Financials ==========
with tabs[2]:
    st.subheader("Financial Strength & Predictability")
    st.caption("Optional upload (CSV/XLSX). Columns (case-insensitive): Year, Revenue, EBIT, Depreciation, Amortization, Capex, WorkingCapitalChange, NetIncome, Shares, NetDebt")
    upl = st.file_uploader("Upload financial history", type=["csv", "xlsx", "xls"], key="fin_upl")
    df_hist = None
    if upl:
        try:
            df_hist = pd.read_csv(upl) if upl.name.lower().endswith(".csv") else pd.read_excel(upl)
            df_hist = summarize_financials(df_hist)
            st.dataframe(df_hist, use_container_width=True)
        except Exception as e:
            st.error(f"Failed to read file: {e}")
    c1, c2, c3 = st.columns(3)
    roe = c1.number_input("ROE (%)", -100.0, 200.0, st.session_state["template_state"].get("roe", 15.0))
    debt_to_equity = c2.number_input("Debt/Equity (x)", 0.0, 10.0, st.session_state["template_state"].get("de", 0.5))
    int_coverage = c3.number_input("Interest Coverage (EBIT/Interest)", 0.0, 1000.0, st.session_state["template_state"].get("int_cov", 8.0))

    ebit_stab = np.nan; fcf_stab = np.nan
    if df_hist is not None:
        try:
            ebit_series = pd.to_numeric(df_hist.get("EBIT", df_hist.get("ebit")), errors="coerce")
            d_and_a = pd.to_numeric(df_hist.get("DandA"), errors="coerce")
            capex = pd.to_numeric(df_hist.get("Capex", df_hist.get("capex")), errors="coerce")
            wc = pd.to_numeric(df_hist.get("WorkingCapitalChange", df_hist.get("workingcapitalchange")), errors="coerce")
            tax_rate_assumed = st.slider("Assumed Tax Rate for Historical OE (%)", 0, 50, st.session_state["template_state"].get("hist_tax", 25))
            hist_oe = ebit_series * (1 - tax_rate_assumed/100.0) + d_and_a - capex - wc
            ebit_stab = stability_score(ebit_series)
            fcf_stab = stability_score(hist_oe)
        except Exception:
            pass

    fin_weight = st.slider("Weight of Financial Strength (%)", 10, 40, st.session_state["template_state"].get("fin_weight", 25))

    def score_roe(x):
        if pd.isna(x): return np.nan
        if x >= 20: return 5
        if x >= 15: return 4.5
        if x >= 12: return 4
        if x >= 10: return 3.5
        if x >= 8: return 3
        if x >= 5: return 2
        return 1
    def score_de(x):
        if pd.isna(x): return np.nan
        if x <= 0.3: return 5
        if x <= 0.5: return 4.5
        if x <= 1.0: return 4
        if x <= 1.5: return 3.5
        if x <= 2.0: return 3
        if x <= 2.5: return 2.5
        return 2
    def score_ic(x):
        if pd.isna(x): return np.nan
        if x >= 15: return 5
        if x >= 10: return 4.5
        if x >= 6: return 4
        if x >= 4: return 3.5
        if x >= 3: return 3
        if x >= 2: return 2.5
        return 1.5
    def score_stab(x):
        if pd.isna(x): return np.nan
        return float(np.clip(x, 0, 1) * 5)

    s_roe, s_de, s_ic = score_roe(roe), score_de(debt_to_equity), score_ic(int_coverage)
    s_ebit_stab = score_stab(ebit_stab) if not pd.isna(ebit_stab) else np.nan
    s_fcf_stab = score_stab(fcf_stab) if not pd.isna(fcf_stab) else np.nan
    subs = [s_roe, s_de, s_ic, s_ebit_stab, s_fcf_stab]
    subs_valid = [x for x in subs if not pd.isna(x)]
    fin_score = float(np.mean(subs_valid)) if subs_valid else 3.0
    st.metric("Financial Strength Score (0-5)", f"{fin_score:.2f}")
    st.session_state["template_state"].update({
        "roe": roe, "de": debt_to_equity, "int_cov": int_coverage,
        "hist_tax": st.session_state["template_state"].get("hist_tax", 25),
        "fin_weight": fin_weight,
        "fin_score_cache": fin_score
    })

# ========== 4) Valuation (DCF) ==========
with tabs[3]:
    st.subheader("Intrinsic Value via Owner-Earnings DCF")
    c1, c2, c3 = st.columns(3)
    base_fcf = c1.number_input("Base Owner Earnings / FCF", value=st.session_state["template_state"].get("base_fcf", 100_000_000.0), step=1e6, format="%.0f")
    growth_yrs_1_5 = c2.number_input("Growth Years 1–5 (%)", value=st.session_state["template_state"].get("g_1_5", 8.0))
    wacc = c3.number_input("WACC / Discount Rate (%)", value=st.session_state["template_state"].get("wacc", 12.0))
    c4, c5, c6 = st.columns(3)
    terminal_growth = c4.number_input("Terminal Growth (%)", value=st.session_state["template_state"].get("g_term", 3.0))
    net_debt = c5.number_input("Net Debt", value=st.session_state["template_state"].get("net_debt", 0.0), step=1e6, format="%.0f")
    shares_out = c6.number_input("Shares Outstanding", value=st.session_state["template_state"].get("shares", 100_000_000.0), min_value=1.0, step=1e6, format="%.0f")
    c7, c8 = st.columns(2)
    market_price = c7.number_input("Market Price / Share", value=st.session_state["template_state"].get("mkt_px", 0.0), step=0.01, format="%.2f")
    currency = c8.text_input("Currency", value=st.session_state["template_state"].get("ccy", "USD"))
    pv, cf_list, terminal = dcf_from_fcf(base_fcf, growth_yrs_1_5/100.0, wacc/100.0, terminal_growth/100.0, years=5)
    equity_value = pv - net_debt if not pd.isna(pv) else np.nan
    intrinsic_ps = equity_value / shares_out if not pd.isna(equity_value) else np.nan
    st.metric("Enterprise Value (PV)", f"{_fmt_num(pv)} {currency}" if not pd.isna(pv) else "—")
    st.metric("Equity Value", f"{_fmt_num(equity_value)} {currency}" if not pd.isna(equity_value) else "—")
    st.metric("Intrinsic Value / Share", f"{intrinsic_ps:,.2f} {currency}" if not pd.isna(intrinsic_ps) else "—")
    if cf_list:
        years = [f"Year {i}" for i in range(1, len(cf_list)+1)]
        disc = [(cf / ((1 + wacc/100.0) ** i)) for i, cf in enumerate(cf_list, start=1)]
        dcf_table = pd.DataFrame({"Year": years, "FCF": cf_list, "Discounted FCF": disc})
        st.dataframe(dcf_table, use_container_width=True)
    st.session_state["template_state"].update({
        "base_fcf": base_fcf, "g_1_5": growth_yrs_1_5, "wacc": wacc,
        "g_term": terminal_growth, "net_debt": net_debt, "shares": shares_out,
        "mkt_px": market_price, "ccy": currency, "intrinsic_ps_cache": intrinsic_ps
    })

# ========== 5) Risk & MOS ==========
with tabs[4]:
    st.subheader("Risk, Margin of Safety, Opportunity Cost")
    c1, c2, c3 = st.columns(3)
    worst_case_rev = c1.number_input("Worst-case revenue change (%)", value=st.session_state["template_state"].get("wc_rev",-20.0))
    disruption_risk = c2.selectbox("Business Disruption Risk", ["Low", "Medium", "High"],
                                   index={"Low":0,"Medium":1,"High":2}[st.session_state["template_state"].get("disrupt","Medium")])
    alt_opportunity = c3.number_input("Opportunity Cost Hurdle IRR (%)", value=st.session_state["template_state"].get("opp_irr", 12.0))
    mos = st.slider("Margin of Safety (%)", 10, 50, st.session_state["template_state"].get("mos", 30))
    moat_weight = st.session_state["template_state"]["moat_weight"]
    mgmt_weight = st.session_state["template_state"]["mgmt_weight"]
    fin_weight = st.session_state["template_state"]["fin_weight"]
    other_weight = max(0, 100 - (moat_weight + mgmt_weight + fin_weight))
    moat_avg = np.mean([st.session_state["template_state"]["moat_brand"],
                        st.session_state["template_state"]["moat_cost"],
                        st.session_state["template_state"]["moat_network"],
                        st.session_state["template_state"]["moat_switching"],
                        st.session_state["template_state"]["moat_regulatory"]])
    mgmt_avg = np.mean([st.session_state["template_state"]["mgmt_capalloc"],
                        st.session_state["template_state"]["mgmt_transparency"],
                        st.session_state["template_state"]["mgmt_alignment"]])
    fin_score = st.session_state["template_state"].get("fin_score_cache", 3.0)
    risk_deduction = {"Low":0.1, "Medium":0.4, "High":0.8}[disruption_risk]
    risk_component = max(0.0, 5.0 - 5.0 * risk_deduction)
    total_score = (moat_avg * (moat_weight/100.0) +
                   mgmt_avg * (mgmt_weight/100.0) +
                   (fin_score) * (fin_weight/100.0) +
                   risk_component * (other_weight/100.0))
    st.metric("Composite Quality Score (0-5)", f"{total_score:.2f}")
    st.session_state["template_state"].update({
        "wc_rev": worst_case_rev, "disrupt": disruption_risk, "opp_irr": alt_opportunity,
        "mos": mos, "composite_score_cache": total_score
    })

# ========== 6) Verdict & Export ==========
with tabs[5]:
    st.subheader("Decision & Export")
    intrinsic_ps = st.session_state["template_state"].get("intrinsic_ps_cache")
    base_fcf = st.session_state["template_state"]["base_fcf"]
    pv, cf_list, terminal = dcf_from_fcf(base_fcf,
                                         st.session_state["template_state"]["g_1_5"]/100.0,
                                         st.session_state["template_state"]["wacc"]/100.0,
                                         st.session_state["template_state"]["g_term"]/100.0, 5)
    equity_value = pv - st.session_state["template_state"]["net_debt"] if not pd.isna(pv) else np.nan
    intrinsic_ps = equity_value / st.session_state["template_state"]["shares"] if not pd.isna(equity_value) else np.nan
    currency = st.session_state["template_state"]["ccy"]; market_price = st.session_state["template_state"]["mkt_px"]
    mos = st.session_state["template_state"]["mos"] / 100.0
    st.metric("Intrinsic Value / Share", f"{intrinsic_ps:,.2f} {currency}" if not pd.isna(intrinsic_ps) else "—")
    target_buy_price = intrinsic_ps * (1 - mos) if not pd.isna(intrinsic_ps) else np.nan
    st.metric("Target Buy Price (MOS)", f"{target_buy_price:,.2f} {currency}" if not pd.isna(target_buy_price) else "—")
    if market_price and market_price > 0 and not pd.isna(target_buy_price):
        discount = (target_buy_price - market_price) / market_price
        st.write(f"Current discount to target: **{_fmt_pct(discount)}**")

    circle_comp = st.session_state["template_state"]["circle_comp"]
    decision, reason = "WAIT", []
    if circle_comp == "Outside":
        decision = "PASS"; reason.append("Outside circle of competence.")
    elif not pd.isna(intrinsic_ps) and market_price > 0:
        if market_price <= target_buy_price:
            decision = "BUY"; reason.append("Price at/below MOS-adjusted intrinsic.")
        elif market_price <= intrinsic_ps:
            decision = "WAIT"; reason.append("Below intrinsic but above MOS threshold; wait.")
        else:
            decision = "PASS"; reason.append("Price above intrinsic; insufficient MOS.")
    else:
        reason.append("Provide market price or complete valuation inputs.")

    st.markdown(f"### **Verdict: {decision}**")
    st.write("- " + "\n- ".join(reason))

    # ---- Scores table (FIXED bug) ----
    moat_avg = np.mean([st.session_state["template_state"]["moat_brand"],
                        st.session_state["template_state"]["moat_cost"],
                        st.session_state["template_state"]["moat_network"],
                        st.session_state["template_state"]["moat_switching"],
                        st.session_state["template_state"]["moat_regulatory"]])
    mgmt_avg = np.mean([st.session_state["template_state"]["mgmt_capalloc"],
                        st.session_state["template_state"]["mgmt_transparency"],
                        st.session_state["template_state"]["mgmt_alignment"]])
    scores_table = pd.DataFrame([
        {"Dimension":"Business Quality (Moat)", "Score (0-5)": round(moat_avg,2),
         "Weight %": st.session_state["template_state"]["moat_weight"]},
        {"Dimension":"Management", "Score (0-5)": round(mgmt_avg,2),
         "Weight %": st.session_state["template_state"]["mgmt_weight"]},
        {"Dimension":"Financial Strength", "Score (0-5)": round(st.session_state["template_state"].get("fin_score_cache", np.nan),2),
         "Weight %": st.session_state["template_state"]["fin_weight"]},
        {"Dimension":"Risk Component", "Score (0-5)": "Derived",
         "Weight %": max(0, 100 - (
             st.session_state["template_state"]["moat_weight"] +
             st.session_state["template_state"]["mgmt_weight"] +
             st.session_state["template_state"]["fin_weight"]
         ))},
        {"Dimension":"Composite", "Score (0-5)": round(st.session_state["template_state"].get("composite_score_cache", np.nan),2),
         "Weight %": 100}
    ])

    overview = {
        "Decision": decision,
        "Intrinsic / Share": f"{intrinsic_ps:,.2f} {currency}" if not pd.isna(intrinsic_ps) else "—",
        "Target Buy Price (MOS)": f"{target_buy_price:,.2f} {currency}" if not pd.isna(target_buy_price) else "—",
        "Market Price": f"{market_price:,.2f} {currency}" if market_price else "—",
        "Margin of Safety": f"{st.session_state['template_state']['mos']}%",
        "Circle of Competence": circle_comp,
        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    inputs = {
        "Base FCF / OE": base_fcf,
        "Growth (Y1-5 %)": st.session_state["template_state"]["g_1_5"],
        "WACC %": st.session_state["template_state"]["wacc"],
        "Terminal Growth %": st.session_state["template_state"]["g_term"],
        "Net Debt": st.session_state["template_state"]["net_debt"],
        "Shares": st.session_state["template_state"]["shares"],
        "Market Price": market_price,
        "Currency": currency
    }
    if cf_list:
        years = [f"Year {i}" for i in range(1, len(cf_list)+1)]
        disc = [(cf / ((1 + st.session_state['template_state']['wacc']/100.0) ** i)) for i, cf in enumerate(cf_list, start=1)]
        dcf_table = pd.DataFrame({"Year": years, "FCF": cf_list, "Discounted FCF": disc})
    else:
        dcf_table = pd.DataFrame(columns=["Year","FCF","Discounted FCF"])

    # Excel export
    b = io.BytesIO()
    build_excel_download(b, overview, inputs, dcf_table, scores_table)
    st.download_button("Download Excel Summary", data=b.getvalue(),
                       file_name=f"Buffett_Decision_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # ---- Board-ready PDF export ----
    st.markdown("#### Export Board-Ready PDF")
    pdf_buf = io.BytesIO()
    doc = SimpleDocTemplate(pdf_buf, pagesize=A4, topMargin=24, bottomMargin=24, leftMargin=24, rightMargin=24)
    styles = getSampleStyleSheet()
    elems = []
    elems.append(Paragraph("<b>Investment Decision Summary</b>", styles["Title"]))
    elems.append(Spacer(1, 8))
    elems.append(Paragraph(f"Verdict: <b>{decision}</b>", styles["Heading2"]))
    elems.append(Paragraph(f"Intrinsic/Share: <b>{_fmt_num(intrinsic_ps,2)} {currency}</b>  •  Target (MOS): <b>{_fmt_num(target_buy_price,2)} {currency}</b>", styles["Normal"]))
    elems.append(Paragraph(f"Composite Quality Score: <b>{_fmt_num(st.session_state['template_state'].get('composite_score_cache', np.nan),2)}</b>", styles["Normal"]))
    elems.append(Spacer(1, 8))
    # Scores Table
    tbl_data = [["Dimension","Score (0-5)","Weight %"]] + scores_table.values.tolist()
    t = Table(tbl_data)
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0), colors.black),
        ("TEXTCOLOR",(0,0),(-1,0), colors.white),
        ("ALIGN",(0,0),(-1,-1),"CENTER"),
        ("GRID",(0,0),(-1,-1),0.25, colors.grey),
        ("BACKGROUND",(0,1),(-1,-1), colors.whitesmoke),
    ]))
    elems.append(t)
    elems.append(Spacer(1, 8))
    # Rationale
    elems.append(Paragraph("<b>Rationale</b>", styles["Heading2"]))
    elems.append(Paragraph("<br/>".join([f"• {r}" for r in reason]) if reason else "—", styles["Normal"]))
    doc.build(elems)
    st.download_button("Download PDF Summary", data=pdf_buf.getvalue(),
                       file_name=f"Investment_Summary_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
                       mime="application/pdf")

# ========== 7) Peer Benchmarking (Auto via yfinance) ==========
with tabs[6]:
    st.subheader("Peer Benchmarking — Auto Fetch with yfinance")
    tickers = st.text_input("Enter comma-separated tickers (e.g., AAPL,MSFT,NVDA)", value=st.session_state["template_state"].get("peer_tickers",""))
    if st.button("Fetch Peers"):
        rows = []
        for tk in [t.strip() for t in tickers.split(",") if t.strip()]:
            rows.append(fetch_peer_metrics(tk))
        if rows:
            peers = pd.DataFrame(rows)
            # Display core metrics
            show = ["Ticker","Price","P_E","EV_EBITDA","EV_EBIT","ROE","D_E","FCF_Yield","MarketCap","EV","EBITDA_TTM","EBIT_TTM","FCF_TTM","NetDebt"]
            show = [c for c in show if c in peers.columns]
            st.dataframe(peers[show], use_container_width=True)
            # Boxplots
            for metric in ["P_E","EV_EBITDA","EV_EBIT","ROE","D_E","FCF_Yield"]:
                if metric in peers.columns and peers[metric].notna().sum() > 1:
                    fig = plt.figure()
                    plt.boxplot(peers[metric].replace([np.inf,-np.inf], np.nan).dropna())
                    plt.title(f"{metric} — Distribution")
                    st.pyplot(fig)
            # Save to Excel for export page
            st.session_state["peers_df"] = peers
            st.success("Peers fetched.")

# ========== 8) Monte Carlo (Correlated) ==========
with tabs[7]:
    st.subheader("Monte Carlo on Growth & WACC — Correlated Draws")
    c1, c2, c3 = st.columns(3)
    mc_n = int(c1.number_input("Simulations", 1000, 2_000_000, 50_000, step=5000))
    g_mean = c2.number_input("Growth mean Y1–5 (%)", value=st.session_state["template_state"].get("g_mean", 8.0))
    g_sd = c3.number_input("Growth std dev (pp)", value=st.session_state["template_state"].get("g_sd", 3.0))
    c4, c5, c6 = st.columns(3)
    wacc_mean = c4.number_input("WACC mean (%)", value=st.session_state["template_state"].get("wacc_mean", 12.0))
    wacc_sd = c5.number_input("WACC std dev (pp)", value=st.session_state["template_state"].get("wacc_sd", 2.0))
    rho = c6.number_input("Correlation (growth, WACC) [-1..1]", value=-0.5, min_value=-1.0, max_value=1.0, step=0.05)
    base_fcf_mc = st.number_input("Base FCF / OE", value=st.session_state["template_state"].get("base_fcf_mc", 100_000_000.0), step=1e6, format="%.0f")
    net_debt_mc = st.number_input("Net Debt", value=st.session_state["template_state"].get("net_debt_mc", 0.0), step=1e6, format="%.0f")
    shares_mc = st.number_input("Shares Outstanding", value=st.session_state["template_state"].get("shares_mc", 100_000_000.0), step=1e6, format="%.0f")
    market_px_mc = st.number_input("Market Price / Share (optional)", value=st.session_state["template_state"].get("mkt_px_mc", 0.0), step=0.01, format="%.2f")
    g_term_mc = st.number_input("Terminal growth (%)", value=st.session_state["template_state"].get("g_term_mc", 3.0))

    run = st.button("Run Monte Carlo (Correlated)")
    if run:
        rng = np.random.default_rng(42)
        # Correlated normals via Cholesky
        cov = np.array([[ (g_sd/100.0)**2, rho*(g_sd/100.0)*(wacc_sd/100.0)],
                        [ rho*(g_sd/100.0)*(wacc_sd/100.0), (wacc_sd/100.0)**2 ]])
        L = np.linalg.cholesky(cov) if np.all(np.linalg.eigvals(cov) > 0) else np.linalg.cholesky(cov + 1e-12*np.eye(2))
        Z = rng.normal(size=(2, mc_n))
        draws = (L @ Z).T
        g_draws = g_mean/100.0 + draws[:,0]
        wacc_draws = wacc_mean/100.0 + draws[:,1]
        g_term_v = g_term_mc/100.0

        ev = np.empty(mc_n); ev[:] = np.nan
        for i in range(mc_n):
            if wacc_draws[i] <= g_term_v:  # discard invalid
                continue
            pv, _, _ = dcf_from_fcf(base_fcf_mc, g_draws[i], wacc_draws[i], g_term_v, years=5)
            ev[i] = pv
        eq = ev - net_debt_mc
        ps = eq / shares_mc
        ps = pd.Series(ps).replace([np.inf,-np.inf], np.nan).dropna()
        if len(ps) == 0:
            st.error("No valid simulations (likely WACC <= terminal growth too often). Adjust inputs.")
        else:
            p5, p50, p95 = ps.quantile([0.05,0.50,0.95])
            st.metric("P5 / P50 / P95 Intrinsic Value per Share", f"{p5:,.2f} / {p50:,.2f} / {p95:,.2f}")
            if market_px_mc > 0:
                st.write(f"Probability intrinsic ≥ market: **{_fmt_pct((ps >= market_px_mc).mean())}**")
            fig = plt.figure()
            plt.hist(ps, bins=60)
            plt.title("Intrinsic Value / Share — Monte Carlo (Correlated)")
            plt.xlabel("Value")
            plt.ylabel("Frequency")
            st.pyplot(fig)
            out = io.BytesIO()
            pd.DataFrame({"intrinsic_ps": ps}).to_csv(out, index=False)
            st.download_button("Download Simulation CSV", out.getvalue(), file_name="mc_intrinsic_values.csv", mime="text/csv")

# ========== 9) Trade Sizing ==========
with tabs[8]:
    st.subheader("Position Sizing — Kelly & Volatility Target")
    st.caption("Two approaches: (1) Kelly fraction using excess return & variance (proxy), (2) Target portfolio volatility.")
    c1, c2, c3 = st.columns(3)
    exp_ret = c1.number_input("Expected annual return (μ, %)", value=15.0)
    ann_vol = c2.number_input("Annualized volatility (σ, %)", value=25.0, min_value=0.01)
    rf = c3.number_input("Risk-free rate (%, for excess)", value=4.0)
    kelly_raw = (exp_ret/100.0 - rf/100.0) / ((ann_vol/100.0)**2)
    kelly = float(np.clip(kelly_raw, 0, 1))
    st.metric("Kelly fraction (bounded 0..1)", f"{kelly:.2f}")
    st.caption("Rule of thumb: deploy half-Kelly to reduce path risk.")
    tgt_vol = st.number_input("Target portfolio volatility (%, optional)", value=10.0)
    vol_pos = float(np.clip((tgt_vol/max(ann_vol, 1e-9)), 0, 1))
    c4, c5, c6 = st.columns(3)
    max_weight = c4.number_input("Max position weight", 0.0, 1.0, 0.15)
    dd_stop = c5.number_input("Drawdown stop-loss (%, optional)", 0.0, 100.0, 25.0)
    cash_buffer = c6.number_input("Cash buffer (%)", 0.0, 50.0, 5.0)
    suggested = min(kelly*0.5, vol_pos, max_weight)
    st.markdown(f"**Suggested position weight:** {suggested:.2f} (min of ½-Kelly, vol-target, max)")

# ========== 10) Real Estate ==========
with tabs[9]:
    st.subheader("Real Estate — Income Asset & Development")
    sub = st.radio("Mode", ["Income Property (NOI/Cap/DSCR)","Development (Curves & IRR)"])
    if sub == "Income Property (NOI/Cap/DSCR)":
        st.markdown("### Income Property Valuation")
        c1,c2,c3 = st.columns(3)
        noi_y1 = c1.number_input("NOI (Year 1)", value=10_000_000.0, step=1e5, format="%.0f")
        cap_rate = c2.number_input("Entry Cap Rate (%)", value=8.0)
        exit_cap = c3.number_input("Exit Cap Rate (%)", value=9.0)
        hold = st.number_input("Hold Period (years)", value=5, step=1)
        noi_growth = st.number_input("NOI Growth (%)", value=3.0)
        disc = st.number_input("Discount Rate (%)", value=12.0)
        value_now = noi_y1 / (cap_rate/100.0)
        cfs = []
        for t in range(1, hold+1):
            noi_t = noi_y1 * ((1+noi_growth/100.0)**(t-1))
            cfs.append(noi_t)
        noi_next = noi_y1 * ((1+noi_growth/100.0)**hold)
        terminal = noi_next / (exit_cap/100.0)
        pv = sum(cf / ((1+disc/100.0)**t) for t, cf in enumerate(cfs, start=1))
        dcf_value = pv + terminal / ((1+disc/100.0)**hold)
        st.metric("Cap Value (NOI/Cap)", f"{_fmt_num(value_now)}")
        st.metric("DCF Value", f"{_fmt_num(dcf_value)}")
        st.markdown("### Debt & DSCR")
        c4,c5,c6 = st.columns(3)
        ltv = c4.number_input("LTV (%)", value=60.0)
        loan_rate = c5.number_input("Loan rate (%)", value=7.0)
        amort_years = c6.number_input("Amortization (years)", value=20, step=1)
        loan_amt = value_now * (ltv/100.0)
        r = loan_rate/100.0
        ann_pay = loan_amt * (r*(1+r)**amort_years)/(((1+r)**amort_years)-1) if r>0 else loan_amt / amort_years
        dscr_y1 = noi_y1 / ann_pay if ann_pay>0 else np.nan
        st.metric("Loan Amount", f"{_fmt_num(loan_amt)}")
        st.metric("Annual Debt Service", f"{_fmt_num(ann_pay)}")
        st.metric("DSCR (Year 1)", f"{dscr_y1:.2f}")
    else:
        st.markdown("### Development Project (Simplified)")
        st.caption("S-curve for costs, linear sales after launch. Computes cash flows, NPV & IRR.")
        c1,c2,c3 = st.columns(3)
        total_cost = c1.number_input("Total Development Cost", value=200_000_000.0, step=1e6, format="%.0f")
        build_years = c2.number_input("Build period (years)", value=3, step=1)
        sell_years = c3.number_input("Sales period (years)", value=3, step=1)
        price_total = st.number_input("Total Sales Proceeds (nominal)", value=300_000_000.0, step=1e6, format="%.0f")
        disc = st.number_input("Discount Rate (%)", value=12.0)
        loan_rate = st.number_input("Construction Loan Rate (%)", value=9.0)
        ltc = st.number_input("LTC (%)", value=60.0)
        # S-curve weights
        t_cost = max(1, int(build_years))
        x = np.linspace(0, 1, t_cost)
        s_curve = 3*x**2 - 2*x**3
        w = np.diff(np.hstack([[0], s_curve])); w = w/ w.sum()
        costs = total_cost * w
        sales_each = price_total / sell_years
        years = list(range(1, build_years + sell_years + 1))
        cf = []
        loan_drawn = 0.0
        equity_in = 0.0
        for t in years:
            if t <= build_years:
                out = -costs[t-1]
                loan_cap = total_cost * (ltc/100.0)
                draw = min(-out, max(0.0, loan_cap - loan_drawn)) if out<0 else 0.0
                loan_drawn += draw
                equity = -out - draw
                equity_in += max(0, equity)
                interest = -loan_drawn * (loan_rate/100.0)
                cf.append(out + interest)
            else:
                inflow = sales_each
                interest = -loan_drawn * (loan_rate/100.0) if loan_drawn>0 else 0.0
                repay = min(inflow, loan_drawn + (-interest))
                loan_drawn = max(0.0, loan_drawn - repay)
                net = inflow + interest - repay
                cf.append(net)
        npv = sum([cf[t-1]/((1+disc/100.0)**t) for t in years])
        try:
            irr = npf.irr([0.0] + cf)
        except Exception:
            irr = np.nan
        st.metric("Equity Invested (approx.)", f"{_fmt_num(equity_in)}")
        st.metric("NPV (to time 0)", f"{_fmt_num(npv)}")
        st.metric("Project IRR (approx.)", f"{irr*100:.2f}%")
        fig = plt.figure()
        plt.bar(years, cf)
        plt.axhline(0, linewidth=0.8)
        plt.title("Development Cash Flows by Year")
        plt.xlabel("Year"); plt.ylabel("Cash Flow")
        st.pyplot(fig)

# Final safe cache
try:
    st.session_state["template_state"]["intrinsic_ps_cache"] = intrinsic_ps
except Exception:
    pass
