"""
CIapp -- Corporate Issuance Monitor

Two modes:
  Live Monitor  -- polls EDGAR's new-filings feed, filters to universe.
                   Runs in seconds. Set auto-refresh for hands-free monitoring.
  Historical Scan -- queries each company's full submission history for a
                     date range. Thorough but takes 2-3 minutes.

Run with:
    streamlit run CIapp.py
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from universe import get_universe
from monitor import check_realtime, check_filings

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

st.set_page_config(page_title="CIapp", layout="wide", page_icon="📋")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_amount(v, currency: str = "USD") -> str:
    if pd.isna(v) or v is None:
        return ""
    symbol = {
        "USD": "$",   "CAD": "C$",  "EUR": "€",   "GBP": "£",
        "JPY": "¥",   "AUD": "A$",  "NZD": "NZ$", "CHF": "CHF ",
        "HKD": "HK$", "SGD": "S$",
    }.get(currency, "$")
    if v >= 1e9:
        return f"{symbol}{v / 1e9:,.2f}bln"
    return f"{symbol}{v / 1e6:,.2f}mln"


def _render_table(results: list[dict], dl_filename: str, dl_key: str):
    if not results:
        return
    df = pd.DataFrame(results).sort_values("filed_at", ascending=False).reset_index(drop=True)
    m1, m2, m3 = st.columns(3)
    m1.metric("Filings", len(df))
    m2.metric("Debt only", int((df["classification"] == "Debt").sum()))
    m3.metric("Debt + Equity", int((df["classification"] == "Debt + Equity").sum()))

    display = df.copy()
    display["debt_size"]   = display.apply(lambda r: _fmt_amount(r["debt_size"],   r.get("currency", "USD")), axis=1)
    display["equity_size"] = display.apply(lambda r: _fmt_amount(r["equity_size"], r.get("currency", "USD")), axis=1)

    st.dataframe(
        display,
        width="stretch",
        column_config={
            "filed_at":       st.column_config.TextColumn("Filed"),
            "ticker":         st.column_config.TextColumn("Ticker"),
            "company":        st.column_config.TextColumn("Company"),
            "form":           st.column_config.TextColumn("Form"),
            "classification": st.column_config.TextColumn("Type"),
            "currency":       st.column_config.TextColumn("Currency"),
            "debt_size":      st.column_config.TextColumn("Debt Size"),
            "equity_size":    st.column_config.TextColumn("Equity Size"),
            "maturities":     st.column_config.TextColumn("Maturities"),
            "structure":      st.column_config.TextColumn("Structure", help="Number of tranches and whether a floating rate note (FRN) is included"),
            "link":           st.column_config.LinkColumn("Filing", display_text="Open ↗"),
        },
        hide_index=True,
    )
    col_dl, col_clr = st.columns([1, 5])
    with col_dl:
        st.download_button("⬇ CSV", display.to_csv(index=False).encode(), dl_filename, "text/csv", key=dl_key)


# ---------------------------------------------------------------------------
# Sidebar -- universe + shared filters
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("📋 CIapp")
    st.caption("S&P 500 + Nasdaq 100 | SEC EDGAR")
    st.divider()

    st.subheader("Universe")
    if st.button("🔄 Refresh universe"):
        universe = get_universe(force_refresh=True)
    else:
        universe = get_universe()

    if universe.empty:
        st.error("Failed to load universe. Check your internet connection.")
        st.stop()

    st.success(f"{len(universe)} companies loaded")
    with st.expander("Show universe"):
        st.dataframe(universe[["ticker", "name", "index"]], hide_index=True, width="stretch")

    st.divider()
    st.subheader("Filters")
    st.caption("Applied to both modes.")
    exclude_structured = st.checkbox("Exclude structured/retail notes", value=True)
    min_size_m = st.number_input("Min deal size ($M)", min_value=0, value=100, step=50)
    min_amount = (min_size_m * 1e6) if min_size_m > 0 else None


# ---------------------------------------------------------------------------
# Main -- two tabs
# ---------------------------------------------------------------------------

st.title("CIapp — Corporate Issuance Monitor")

tab_live, tab_hist = st.tabs(["🔴 Live Monitor", "📅 Historical Scan"])


# ── Tab 1: Live monitor ────────────────────────────────────────────────────

with tab_live:
    st.caption(
        "Polls EDGAR's new-filings feed for each form type and filters to your "
        "universe. Only fetches text for actual matches — typically **5–20 seconds** "
        "regardless of universe size. Enable auto-refresh for continuous monitoring."
    )

    col_a, col_b, col_c = st.columns([1, 1, 2])
    with col_a:
        auto = st.checkbox("Auto-refresh")
    with col_b:
        interval = st.selectbox("Every", [5, 10, 15, 30], index=1, disabled=not auto)
        st.caption("minutes")
    with col_c:
        run_live = st.button("🔍 Check now", type="primary", width="stretch", key="btn_live")

    if auto and HAS_AUTOREFRESH:
        st_autorefresh(interval=interval * 60 * 1000, key="live_refresh")
    elif auto:
        st.warning("Install `streamlit-autorefresh` for auto-refresh.")

    if "live_results" not in st.session_state:
        st.session_state["live_results"] = []
    if "live_seen" not in st.session_state:
        st.session_state["live_seen"] = set()

    should_run = run_live or (auto and HAS_AUTOREFRESH)

    if should_run:
        with st.spinner("Polling EDGAR new-filings feed..."):
            new_hits, updated_seen = check_realtime(
                universe,
                seen_accessions=st.session_state["live_seen"].copy(),
                exclude_structured=exclude_structured,
                min_debt_amount=min_amount,
            )
        st.session_state["live_seen"] = updated_seen
        if new_hits:
            st.session_state["live_results"] = new_hits + st.session_state["live_results"]
            st.toast(f"{len(new_hits)} new filing(s) found", icon="📄")
        else:
            st.toast("No new filings since last check", icon="✅")

    st.divider()

    if st.session_state["live_results"]:
        _render_table(st.session_state["live_results"], "live_filings.csv", "dl_live")
        if st.button("🗑 Clear live results"):
            st.session_state["live_results"] = []
            st.session_state["live_seen"]    = set()
            st.rerun()
    else:
        st.info("No results yet — click **Check now** or enable auto-refresh.")


# ── Tab 2: Historical scan ─────────────────────────────────────────────────

with tab_hist:
    st.caption(
        "Queries each company's full submission history for a specific date range. "
        "Complete and reliable, but slow (~2-3 minutes for the full universe). "
        "Use this to catch up on a past period or verify the live monitor."
    )

    col_d, col_e, col_f = st.columns([1, 1, 2])
    with col_d:
        since_date = st.date_input("Since", value=date.today() - timedelta(days=1), max_value=date.today())
    with col_e:
        workers = st.number_input("Workers", min_value=5, max_value=30, value=20, step=5)
    with col_f:
        st.write("")
        run_hist = st.button("🔎 Scan history", type="primary", width="stretch", key="btn_hist")

    if "hist_results" not in st.session_state:
        st.session_state["hist_results"] = []

    if run_hist:
        bar = st.progress(0.0, text="Starting historical scan...")
        def _progress(done, total):
            bar.progress(done / total, text=f"Scanned {done}/{total} companies...")
        with st.spinner(""):
            hist_hits = check_filings(
                universe, since_date,
                exclude_structured=exclude_structured,
                min_debt_amount=min_amount,
                max_workers=int(workers),
                progress_callback=_progress,
            )
        bar.empty()
        if hist_hits:
            existing = {r["link"] for r in st.session_state["hist_results"]}
            fresh    = [r for r in hist_hits if r["link"] not in existing]
            st.session_state["hist_results"] = fresh + st.session_state["hist_results"]
            st.toast(f"Found {len(hist_hits)} filing(s)", icon="📄")
        else:
            st.toast("No matching filings in this date range", icon="✅")

    st.divider()

    if st.session_state["hist_results"]:
        _render_table(st.session_state["hist_results"], "hist_filings.csv", "dl_hist")
        if st.button("🗑 Clear history results"):
            st.session_state["hist_results"] = []
            st.rerun()
    else:
        st.info("No results yet — set a date range and click **Scan history**.")


if __name__ == "__main__":
    pass
