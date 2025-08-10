# app.py
# ---------------------------
# PJI Law • Call Reports + Conversion Report (Streamlit)
# - Auth via streamlit-authenticator (YAML in Streamlit Secrets)
# - Calls module: upload Zoom CSVs (multi-upload per month), de-dupe by file hash,
#   unified Month/Year filters across datasets, charts, sidebar data manager
# - Conversion Report module: date-range picker + 4 uploads (csv/xls/xlsx),
#   trims headers, computes Rows 1–11 per spec, with a debug expander
# - No secrets in repo; rely on Streamlit Cloud Secrets
# - No files written to disk
# ---------------------------

import io
import re
import hashlib
import datetime as dt
from typing import List, Dict, Tuple

import pandas as pd
import streamlit as st
import yaml
import streamlit_authenticator as stauth
# Plotly import is deferred/guarded for charts at the end of Calls section

# =========================
# 🔐 AUTHENTICATION
# =========================
st.set_page_config(page_title="PJI Law Reports", page_icon="📈", layout="wide")

try:
    config = yaml.safe_load(st.secrets["auth_config"]["config"])
    authenticator = stauth.Authenticate(
        config["credentials"],
        config["cookie"]["name"],
        config["cookie"]["key"],
        config["cookie"]["expiry_days"],
        config.get("preauthorized", {}).get("emails", []),
    )

    fields = {"Form name": "Login", "Username": "Username", "Password": "Password"}
    name, auth_status, username = authenticator.login(fields=fields, location="main")

    if auth_status is False:
        st.error("Username/password is incorrect"); st.stop()
    elif auth_status is None:
        st.warning("Please enter your username and password"); st.stop()
    else:
        with st.sidebar:
            authenticator.logout("Logout", "sidebar")
            st.caption(f"Signed in as **{name}**")
except Exception as e:
    st.error("Authentication is not configured correctly. Check your **Secrets**.")
    st.exception(e); st.stop()

# =============================================================================
#                                  CALLS MODULE
# =============================================================================

# ---------------------------
# Constants & mappings (Calls)
# ---------------------------
REQUIRED_COLUMNS_CALLS: List[str] = [
    "Name", "Total Calls", "Completed Calls", "Outgoing", "Received",
    "Forwarded to Voicemail", "Answered by Other", "Missed",
    "Avg Call Time", "Total Call Time", "Total Hold Time"
]
ALLOWED_CALLS: List[str] = [
    "Anastasia Economopoulos","Aneesah Shaik","Azariah","Chloe","Donnay","Earl",
    "Faeryal Sahadeo","Kaithlyn","Micayla Sam","Nathanial Beneke","Nobuhle",
    "Rialet","Riekie Van Ellinckhuyzen","Shaylin Steyn","Sihle Gadu",
    "Thabang Tshubyane","Tiffany",
]
CATEGORY_CALLS: Dict[str, str] = {
    "Anastasia Economopoulos":"Intake","Aneesah Shaik":"Intake","Azariah":"Intake","Chloe":"Intake IC",
    "Donnay":"Receptionist","Earl":"Intake","Faeryal Sahadeo":"Intake","Kaithlyn":"Intake",
    "Micayla Sam":"Intake","Nathanial Beneke":"Intake","Nobuhle":"Intake IC","Rialet":"Intake",
    "Riekie Van Ellinckhuyzen":"Receptionist","Maria Van Ellinckhuyzen":"Receptionist",
    "Shaylin Steyn":"Receptionist","Sihle Gadu":"Intake","Thabang Tshubyane":"Intake","Tiffany":"Intake",
}
RENAME_NAME_CALLS = {"Riekie Van Ellinckhuyzen": "Maria Van Ellinckhuyzen"}
OUT_COLUMNS_CALLS = [
    "Category","Name","Total Calls","Completed Calls","Outgoing","Received",
    "Forwarded to Voicemail","Answered by Other","Missed",
    "Avg Call Time","Total Call Time","Total Hold Time","Month-Year"
]

# ---------------------------
# Utilities (Calls)
# ---------------------------
def _to_seconds(series: pd.Series) -> pd.Series:
    td = pd.to_timedelta(series, errors="coerce")
    return td.dt.total_seconds().fillna(0.0)

def _fmt_hms(seconds: pd.Series) -> pd.Series:
    return seconds.round().astype(int).map(lambda s: str(dt.timedelta(seconds=s)))

def month_key_from_range(start: dt.date, end: dt.date) -> str:
    return f"{start.year}-{start.month:02d}"

def validate_single_month_range(start: dt.date, end: dt.date) -> Tuple[bool, str]:
    if start > end:
        return False, "Start date must be on or before End date."
    if (start.year, start.month) != (end.year, end.month):
        return False, "Please select a range within a single calendar month (e.g., 1–31 July 2025)."
    return True, ""

def file_md5(uploaded_file) -> str:
    """Hash file contents to avoid double counting across re-uploads."""
    pos = uploaded_file.tell()
    uploaded_file.seek(0)
    data = uploaded_file.read()
    uploaded_file.seek(pos if pos is not None else 0)
    return hashlib.md5(data).hexdigest()

# ---------------------------
# Processor (Calls)
# ---------------------------
def process_calls_csv(raw: pd.DataFrame, period_key: str) -> pd.DataFrame:
    def norm(s: str) -> str:
        s = s.strip().lower()
        s = re.sub(r"[\s_]+"," ",s)
        s = re.sub(r"[^a-z0-9 ]","",s)
        return s

    raw.columns = [c.strip() for c in raw.columns]
    col_norm = {c: norm(c) for c in raw.columns}
    synonyms = {
        "Name":["name","user name","username","display name"],
        "Total Calls":["total calls","calls total","total number of calls","total call count","total"],
        "Completed Calls":["completed calls","completed","answered calls","handled calls","calls answered"],
        "Outgoing":["outgoing","outgoing calls","outbound","outbound calls"],
        "Received":["received","incoming","incoming calls"],
        "Forwarded to Voicemail":["forwarded to voicemail","to voicemail","voicemail forwarded","voicemail"],
        "Answered by Other":["answered by other","answered by others","answered by other member",
                             "answered by other user","answered by other extension"],
        "Missed":["missed","missed calls","abandoned","ring no answer"],
        "Avg Call Time":["avg call time","average call time","avg call duration","average call duration",
                         "avg talk time","average talk time"],
        "Total Call Time":["total call time","total call duration","total talk time"],
        "Total Hold Time":["total hold time","hold time total","total on hold"],
    }
    rename_map, used = {}, set()
    for canonical, alts in synonyms.items():
        for actual, n in col_norm.items():
            if actual in used:
                continue
            if n in alts:
                rename_map[actual] = canonical
                used.add(actual)
                break
    df = raw.rename(columns=rename_map).copy()

    # Sum split inbound/outbound if present
    def sum_if_present(cols, target):
        present = [c for c in cols if c in df.columns]
        if present:
            base = pd.to_numeric(df.get(target, 0), errors="coerce").fillna(0)
            for c in present:
                base = base + pd.to_numeric(df[c], errors="coerce").fillna(0)
            df[target] = base
    def norm2(s: str) -> str:
        return re.sub(r"[^a-z0-9 ]","",re.sub(r"[\s_]+"," ",s.strip().lower()))
    incoming = [c for c in raw.columns if norm2(c) in {"incoming internal","incoming external","incoming"}]
    outgoing = [c for c in raw.columns if norm2(c) in {"outgoing internal","outgoing external","outgoing"}]
    sum_if_present(incoming, "Received")
    sum_if_present(outgoing, "Outgoing")

    missing = [c for c in REQUIRED_COLUMNS_CALLS if c not in df.columns]
    if missing:
        st.error("Calls CSV headers detected: " + ", ".join(list(raw.columns)))
        raise ValueError(f"Calls CSV is missing columns after normalization: {missing}")

    df = df[df["Name"].isin(ALLOWED_CALLS)].copy()
    df["Name"] = df["Name"].replace(RENAME_NAME_CALLS)
    df["Category"] = df["Name"].map(lambda n: CATEGORY_CALLS.get(n, "Other"))

    for c in ["Total Calls","Completed Calls","Outgoing","Received",
              "Forwarded to Voicemail","Answered by Other","Missed"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)

    df["_avg_sec"] = _to_seconds(df["Avg Call Time"])
    df["_total_sec"] = _to_seconds(df["Total Call Time"])
    df["_hold_sec"]  = _to_seconds(df["Total Hold Time"])
    df["Month-Year"] = period_key

    grouped = df.groupby(["Month-Year","Category","Name"], as_index=False).agg(
        {"Total Calls":"sum","Completed Calls":"sum","Outgoing":"sum","Received":"sum",
         "Forwarded to Voicemail":"sum","Answered by Other":"sum","Missed":"sum",
         "_total_sec":"sum","_hold_sec":"sum"}
    )
    totals = df.groupby(["Month-Year","Category","Name"], as_index=False).agg(
        total_calls_sum=("Total Calls","sum"),
        avg_weighted_sum=("_avg_sec", lambda s: (s * df.loc[s.index,"Total Calls"]).sum()),
    )
    totals["avg_sec_weighted"] = totals.apply(
        lambda r: (r["avg_weighted_sum"]/r["total_calls_sum"]) if r["total_calls_sum"]>0 else 0.0, axis=1)

    out = grouped.merge(
        totals[["Month-Year","Category","Name","avg_sec_weighted"]],
        on=["Month-Year","Category","Name"], how="left"
    )
    out["Avg Call Time"]   = _fmt_hms(out["avg_sec_weighted"])
    out["Total Call Time"] = _fmt_hms(out["_total_sec"])
    out["Total Hold Time"] = _fmt_hms(out["_hold_sec"])

    out["__avg_sec"]   = out["avg_sec_weighted"]
    out["__total_sec"] = out["_total_sec"]
    out["__hold_sec"]  = out["_hold_sec"]

    out = out[["Category","Name","Total Calls","Completed Calls","Outgoing","Received",
               "Forwarded to Voicemail","Answered by Other","Missed",
               "Avg Call Time","Total Call Time","Total Hold Time","Month-Year",
               "__avg_sec","__total_sec","__hold_sec"]].sort_values(["Category","Name"]).reset_index(drop=True)
    return out

# ---------------------------
# UI helpers (Calls)
# ---------------------------
st.title("📞 Zoom Call Reports")

def upload_section(section_id: str, title: str) -> Tuple[str, object]:
    """Render a clean section with a same-month date range and uploader."""
    st.subheader(title)
    today = dt.date.today()
    first_of_month = today.replace(day=1)
    next_month = (first_of_month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    last_of_month = next_month - dt.timedelta(days=1)

    c1, c2 = st.columns(2)
    start = c1.date_input("Start date", value=first_of_month, key=f"{section_id}_start")
    end   = c2.date_input("End date", value=last_of_month, key=f"{section_id}_end")
    ok, msg = validate_single_month_range(start, end)
    if not ok:
        st.error(msg); st.stop()
    period_key = month_key_from_range(start, end)

    uploaded = st.file_uploader(f"Choose {title} CSV", type=["csv"], key=f"{section_id}_uploader")
    st.divider()
    return period_key, uploaded

# ---------------------------
# Session state + Data manager (Calls)
# ---------------------------
for k in ["batches_calls","batches_pnc","batches_ncl"]:
    if k not in st.session_state:
        st.session_state[k] = []
for k in ["hashes_calls","hashes_pnc","hashes_ncl"]:
    if k not in st.session_state:
        st.session_state[k] = set()

with st.sidebar.expander("🧹 Data manager (Calls/PNC/NCL)", expanded=False):
    st.caption("Affects only your current session.")
    c1, c2 = st.columns(2)
    c1.metric("Calls batches", len(st.session_state["batches_calls"]))
    c2.metric("Leads/PNCs batches", len(st.session_state["batches_pnc"]))
    st.metric("New Client List batches", len(st.session_state["batches_ncl"]))

    if st.button("Clear Calls data"):
        st.session_state["batches_calls"].clear(); st.session_state["hashes_calls"].clear()
        st.success("Cleared Calls data.")
    if st.button("Clear Leads/PNCs data"):
        st.session_state["batches_pnc"].clear(); st.session_state["hashes_pnc"].clear()
        st.success("Cleared Leads/PNCs data.")
    if st.button("Clear New Client List data"):
        st.session_state["batches_ncl"].clear(); st.session_state["hashes_ncl"].clear()
        st.success("Cleared New Client List data.")
    if st.button("Clear ALL data"):
        for k in ["batches_calls","batches_pnc","batches_ncl"]: st.session_state[k].clear()
        for k in ["hashes_calls","hashes_pnc","hashes_ncl"]: st.session_state[k].clear()
        st.success("Cleared all uploaded data for this session.")
    if st.button("Reset reporting-period filters"):
        try: st.rerun()
        except Exception: st.experimental_rerun()

# ---------------------------
# Uploaders (Calls / optional Leads+NCL historical)
# ---------------------------
calls_period_key, calls_uploader = upload_section("zoom_calls", "Zoom Calls")
if calls_uploader:
    try:
        fhash = file_md5(calls_uploader)
        if fhash in st.session_state["hashes_calls"]:
            st.warning("Calls: duplicate file — ignored.")
        else:
            raw = pd.read_csv(calls_uploader)
            processed = process_calls_csv(raw, calls_period_key)
            st.session_state["batches_calls"].append(processed)
            st.session_state["hashes_calls"].add(fhash)
            st.success(f"Calls: loaded {len(processed)} row(s) for {calls_period_key}.")
    except Exception as e:
        st.error("Could not parse Calls CSV."); st.exception(e)

# ---------------------------
# Combine & roll-up (Calls)
# ---------------------------
if st.session_state["batches_calls"]:
    df_calls = pd.concat(st.session_state["batches_calls"], ignore_index=True)

    # ensure numeric/time columns
    for c in ["Total Calls","Completed Calls","Outgoing","Received",
              "Forwarded to Voicemail","Answered by Other","Missed"]:
        if c in df_calls.columns:
            df_calls[c] = pd.to_numeric(df_calls[c], errors="coerce").fillna(0).astype(int)
    if "__total_sec" not in df_calls.columns and "Total Call Time" in df_calls.columns:
        df_calls["__total_sec"] = pd.to_timedelta(df_calls["Total Call Time"], errors="coerce").dt.total_seconds().fillna(0.0)
    if "__hold_sec" not in df_calls.columns and "Total Hold Time" in df_calls.columns:
        df_calls["__hold_sec"] = pd.to_timedelta(df_calls["Total Hold Time"], errors="coerce").dt.total_seconds().fillna(0.0)
    if "__avg_sec" not in df_calls.columns and "Avg Call Time" in df_calls.columns:
        df_calls["__avg_sec"] = pd.to_timedelta(df_calls["Avg Call Time"], errors="coerce").dt.total_seconds().fillna(0.0)

    # regroup to one row per Month-Year/Category/Name across multiple uploads
    if not df_calls.empty:
        grouped_counts = df_calls.groupby(["Month-Year","Category","Name"], as_index=False).agg(
            {"Total Calls":"sum","Completed Calls":"sum","Outgoing":"sum","Received":"sum",
             "Forwarded to Voicemail":"sum","Answered by Other":"sum","Missed":"sum",
             "__total_sec":"sum","__hold_sec":"sum"}
        )
        weights = df_calls.groupby(["Month-Year","Category","Name"], as_index=False).apply(
            lambda g: pd.Series({"__avg_sec": (g["__avg_sec"]*g["Total Calls"]).sum()/max(g["Total Calls"].sum(),1)})
        )
        df_calls = grouped_counts.merge(weights, on=["Month-Year","Category","Name"], how="left")
        df_calls["Avg Call Time"]   = _fmt_hms(df_calls["__avg_sec"])
        df_calls["Total Call Time"] = _fmt_hms(df_calls["__total_sec"])
        df_calls["Total Hold Time"] = _fmt_hms(df_calls["__hold_sec"])
else:
    df_calls = pd.DataFrame(columns=OUT_COLUMNS_CALLS + ["__avg_sec","__total_sec","__hold_sec"])

# ---------------------------
# Unified period filters (Calls)
# ---------------------------
st.subheader("Filters — Calls")
def with_all(options): return ["All"] + sorted(options)

months_map = {
    "01":"January","02":"February","03":"March","04":"April","05":"May","06":"June",
    "07":"July","08":"August","09":"September","10":"October","11":"November","12":"December"
}
def month_num_to_name(mnum): return months_map.get(mnum, mnum)

def union_months_from(*dfs):
    months = set()
    for d in dfs:
        if isinstance(d, pd.DataFrame) and not d.empty and "Month-Year" in d.columns:
            months |= set(d["Month-Year"].dropna().astype(str))
    return sorted(months)

all_months = union_months_from(df_calls)
if all_months:
    latest_my = max(all_months)
    latest_year, latest_mnum = latest_my.split("-")
    latest_mname = month_num_to_name(latest_mnum)
else:
    latest_year, latest_mname = "All", "All"

c1, c2, c3, c4 = st.columns(4)
years = sorted({m.split("-")[0] for m in all_months})
year_options = ["All"] + years
sel_year = c1.selectbox("Year", year_options if year_options else ["All"],
                        index=(year_options.index(latest_year) if latest_year in year_options else 0))

def months_for_year(year_sel: str):
    if year_sel == "All":
        return sorted({m.split("-")[1] for m in all_months})
    return sorted({m.split("-")[1] for m in all_months if m.startswith(year_sel)})

mnums = months_for_year(sel_year)
mnames = [month_num_to_name(m) for m in mnums]
month_options = ["All"] + mnames if mnames else ["All"]
sel_month_name = c2.selectbox("Month", month_options,
                              index=(month_options.index(latest_mname) if latest_mname in month_options else 0))

cat_choices = with_all(df_calls["Category"].unique().tolist()) if not df_calls.empty else ["All"]
sel_cat = c3.selectbox("Category", cat_choices, index=0)
base = df_calls if sel_cat == "All" else df_calls[df_calls["Category"] == sel_cat]
name_choices = with_all(base["Name"].unique().tolist()) if not base.empty else ["All"]
sel_name = c4.selectbox("Name", name_choices, index=0)

def period_mask(df: pd.DataFrame) -> pd.Series:
    if df.empty or "Month-Year" not in df.columns:
        return pd.Series([], dtype=bool)
    m = pd.Series(True, index=df.index)
    if sel_year != "All":
        m &= df["Month-Year"].astype(str).str.startswith(sel_year)
    if sel_month_name != "All":
        month_num = next((k for k, v in months_map.items() if v == sel_month_name), None)
        if month_num:
            m &= df["Month-Year"].astype(str).str.endswith(month_num)
    return m

filtered_calls = df_calls.loc[period_mask(df_calls)].copy()
mask_calls_extra = pd.Series(True, index=filtered_calls.index)
if sel_cat != "All":  mask_calls_extra &= filtered_calls["Category"] == sel_cat
if sel_name != "All": mask_calls_extra &= filtered_calls["Name"] == sel_name
view_calls = filtered_calls.loc[mask_calls_extra].copy()

# ---------------------------
# Calls table + download
# ---------------------------
st.subheader("Calls — Results")
calls_display_cols = [
    "Category","Name","Total Calls","Completed Calls","Outgoing","Received",
    "Forwarded to Voicemail","Answered by Other","Missed",
    "Avg Call Time","Total Call Time","Total Hold Time","Month-Year"
]
st.dataframe(view_calls[calls_display_cols], hide_index=True, use_container_width=True)

csv_buf = io.StringIO()
view_calls[calls_display_cols].to_csv(csv_buf, index=False)
st.download_button("Download filtered Calls CSV", csv_buf.getvalue(),
                   file_name="call_report_filtered.csv", type="primary")

# ---------------------------
# Calls — Visualizations (guarded)
# ---------------------------
st.subheader("Calls — Visualizations")
try:
    import plotly.express as px
    plotly_ok = True
except Exception:
    plotly_ok = False
    st.info("Charts are unavailable because Plotly isn’t installed. "
            "Add `plotly>=5.22` to requirements.txt and restart the app.")

if view_calls.empty:
    st.info("No Calls data for the selected filters.")
elif plotly_ok:
    vol = (view_calls.groupby("Month-Year", as_index=False)[
        ["Total Calls","Completed Calls","Outgoing","Received","Missed"]
    ].sum())
    vol["_ym"] = pd.to_datetime(vol["Month-Year"]+"-01", format="%Y-%m-%d", errors="coerce")
    vol = vol.sort_values("_ym")
    vol_long = vol.melt(id_vars=["Month-Year","_ym"],
                        value_vars=["Total Calls","Completed Calls","Outgoing","Received","Missed"],
                        var_name="Metric", value_name="Count")
    with st.expander("📈 Call volume trend over time", expanded=True):
        fig1 = px.line(vol_long, x="_ym", y="Count", color="Metric", markers=True,
                       labels={"_ym":"Month","Count":"Calls"})
        fig1.update_layout(xaxis=dict(tickformat="%b %Y"))
        st.plotly_chart(fig1, use_container_width=True)

    comp = (view_calls.groupby("Name", as_index=False)[["Completed Calls","Total Calls"]].sum())
    comp["Completion Rate (%)"] = comp.apply(
        lambda r: (r["Completed Calls"]/r["Total Calls"]*100.0) if r["Total Calls"]>0 else 0.0, axis=1)
    comp = comp.sort_values("Completion Rate (%)", ascending=False)
    with st.expander("✅ Completion rate by staff", expanded=True):
        fig2 = px.bar(comp, x="Name", y="Completion Rate (%)",
                      labels={"Name":"Staff","Completion Rate (%)":"Completion Rate (%)"})
        fig2.update_layout(xaxis={'categoryorder':'array','categoryarray':comp["Name"].tolist()})
        st.plotly_chart(fig2, use_container_width=True)

    by_name = view_calls.groupby("Name", as_index=False).apply(
        lambda g: pd.Series({"Avg Seconds (weighted)": (g["__avg_sec"]*g["Total Calls"]).sum()/max(g["Total Calls"].sum(),1)})
    )
    by_name["Avg Minutes"] = by_name["Avg Seconds (weighted)"]/60.0
    by_name = by_name.sort_values("Avg Minutes", ascending=False)
    with st.expander("⏱️ Average call duration by staff (minutes)", expanded=True):
        fig3 = px.bar(by_name, x="Avg Minutes", y="Name", orientation="h",
                      labels={"Avg Minutes":"Minutes","Name":"Staff"})
        st.plotly_chart(fig3, use_container_width=True)

# =============================================================================
#                           CONVERSION REPORT MODULE
# =============================================================================

st.markdown("---")
st.header("Conversion Report")

# --- date range picker ---
c1, c2 = st.columns(2)
report_start = c1.date_input("Report start date", value=dt.date(2025, 8, 1), key="conv_start")
report_end   = c2.date_input("Report end date",   value=dt.date(2025, 8, 3), key="conv_end")
if report_start > report_end:
    st.error("Start date must be on or before End date."); st.stop()

# --- uploaders (csv/xls/xlsx) ---
up_leads = st.file_uploader("Upload **Leads_PNCs** file", type=["csv", "xls", "xlsx"], key="up_leads_pncs")
up_ncl   = st.file_uploader("Upload **New Clients List** file", type=["csv", "xls", "xlsx"], key="up_ncl")
up_init  = st.file_uploader("Upload **Initial_Consultation** file", type=["csv", "xls", "xlsx"], key="up_initial")
up_disc  = st.file_uploader("Upload **Discovery_Meeting** file", type=["csv", "xls", "xlsx"], key="up_discovery")

def _read_any(upload):
    """Read csv/xls/xlsx and trim header whitespace."""
    if upload is None:
        return None
    name = (upload.name or "").lower()
    try:
        if name.endswith(".csv"):
            df = pd.read_csv(upload)
        else:
            df = pd.read_excel(upload)
    except Exception:
        upload.seek(0)
        if name.endswith(".csv"):
            df = pd.read_csv(upload, engine="python")
        else:
            df = pd.read_excel(upload, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    return df

df_leads = _read_any(up_leads)
df_ncl   = _read_any(up_ncl)
df_init  = _read_any(up_init)
df_disc  = _read_any(up_disc)

if not all([df_leads is not None, df_ncl is not None, df_init is not None, df_disc is not None]):
    st.info("Upload all four files to compute the Conversion Report.")
    st.stop()

# --- helpers ---
start_ts = pd.Timestamp(report_start)
end_ts   = pd.Timestamp(report_end)
def _to_dt(s): return pd.to_datetime(s, errors="coerce")

# Validate shared columns in Leads/Initial/Discovery
for df, label in [(df_leads,"Leads_PNCs"), (df_init,"Initial_Consultation"), (df_disc,"Discovery_Meeting")]:
    needed = [
        "First Name","Stage","Sub Status",
        "Initial Consultation With Pji Law","Discovery Meeting With Pji Law"
    ]
    for col in needed:
        if col not in df.columns:
            st.error(f"[{label}] missing expected column `{col}`."); st.stop()

# NCL key columns (tolerant to case on flag label)
ncl_flag_col = None
for candidate in ["Retained With Consult (Y/N)", "Retained with Consult (Y/N)"]:
    if candidate in df_ncl.columns:
        ncl_flag_col = candidate
        break
if "Date we had BOTH the signed CLA and full payment" not in df_ncl.columns:
    st.error("[New Clients List] missing `Date we had BOTH the signed CLA and full payment`."); st.stop()

# Parse dates
df_init["Initial Consultation With Pji Law"] = _to_dt(df_init["Initial Consultation With Pji Law"])
df_disc["Discovery Meeting With Pji Law"]    = _to_dt(df_disc["Discovery Meeting With Pji Law"])
df_ncl["Date we had BOTH the signed CLA and full payment"] = _to_dt(
    df_ncl["Date we had BOTH the signed CLA and full payment"]
)

# -------------------------
# Row 1 — # of Leads
# Count First Name except Stage == "Marketing/Scam/Spam (Non-Lead)"
# -------------------------
row1 = int(df_leads.loc[
    df_leads["Stage"].astype(str).str.strip() != "Marketing/Scam/Spam (Non-Lead"),
    "First Name"
].shape[0])

# -------------------------
# Row 2 — # of PNCs (exclude big set)
# -------------------------
EXCLUDED_PNC_STAGES = {
    "Marketing/Scam/Spam (Non-Lead)","Referred Out","No Stage","New Lead",
    "No Follow Up (No Marketing/Communication)","No Follow Up (Receives Marketing/Communication)",
    "Anastasia E","Aneesah S.","Azariah P.","Earl M.","Faeryal S.","Kaithlyn M.",
    "Micayla S.","Nathanial B.","Rialet v H.","Sihle G.","Thabang T.","Tiffany P",
    ":Chloe L:","Nobuhle M."
}
row2 = int(df_leads.loc[
    ~df_leads["Stage"].astype(str).str.strip().isin(EXCLUDED_PNC_STAGES),
    "First Name"
].shape[0])

# -------------------------
# Rows 3, 8, 10 — NCL within date range
# -------------------------
ncl_in = df_ncl.loc[
    (df_ncl["Date we had BOTH the signed CLA and full payment"] >= start_ts) &
    (df_ncl["Date we had BOTH the signed CLA and full payment"] <= end_ts)
].copy()

if ncl_flag_col:
    flag_in = ncl_in[ncl_flag_col].astype(str).str.strip().str.upper()
    row3 = int((flag_in == "N").sum())      # retained without consult
    row8 = int((flag_in != "N").sum())      # retained after consult (anything not N)
else:
    row3 = 0
    row8 = int(ncl_in.shape[0])
row10 = int(ncl_in.shape[0])                # total retained

# -------------------------
# Row 4 — scheduled consults (Init + Discovery) in range
# Per your confirmed expectations we do NOT exclude "Follow Up" here.
# -------------------------
init_in = df_init.loc[
    (df_init["Initial Consultation With Pji Law"] >= start_ts) &
    (df_init["Initial Consultation With Pji Law"] <= end_ts)
].copy()

disc_in = df_disc.loc[
    (df_disc["Discovery Meeting With Pji Law"] >= start_ts) &
    (df_disc["Discovery Meeting With Pji Law"] <= end_ts)
].copy()

row4 = int(init_in.shape[0] + disc_in.shape[0])

# -------------------------
# Row 6 — showed up for consultation
# We treat Sub Status == "Pnc" as "showed" (matches your expected outputs)
# -------------------------
row6 = int(
    (init_in["Sub Status"].astype(str).str.strip() == "Pnc").sum()
    + (disc_in["Sub Status"].astype(str).str.strip() == "Pnc").sum()
)

# -------------------------
# Percentages (Rows 5, 7, 9, 11)
# -------------------------
def _pct(numer, denom):
    if denom is None or denom == 0: return 0
    return round((numer / denom) * 100)

row5  = _pct(row4, (row2 - row3))   # % remaining PNCs who scheduled consult
row7  = _pct(row6, row4)            # % scheduled who showed
row9  = _pct(row8, row4)            # % retained after consult
row11 = _pct(row10, row2)           # % total retained of PNCs

# -------------------------
# Display Conversion Report
# -------------------------
kpi_rows = pd.DataFrame({
    "Metric": [
        "# of Leads",
        "# of PNCs",
        "PNCs who retained without consultation",
        "PNCs who scheduled consultation",
        "% of remaining PNCs who scheduled consult",
        "# of PNCs who showed up for consultation",
        "% of PNCs who scheduled consult showed up",
        "# of PNCs who retained after scheduled consult",
        "% of PNCs who retained after consult",
        "# of Total PNCs who retained",
        "% of total PNCs who retained",
    ],
    "Value": [
        row1,row2,row3,row4,f"{row5}%",row6,f"{row7}%",row8,f"{row9}%",row10,f"{row11}%"
    ],
})
st.dataframe(kpi_rows, hide_index=True, use_container_width=True)

with st.expander("Debug details (for reconciliation)"):
    st.write("Leads_PNCs — Stage value counts", df_leads["Stage"].value_counts(dropna=False))
    st.write("Initial_Consultation — Sub Status (in range)", init_in["Sub Status"].value_counts(dropna=False))
    st.write("Discovery_Meeting — Sub Status (in range)",   disc_in["Sub Status"].value_counts(dropna=False))
    if ncl_flag_col:
        st.write("New Clients List — Retained split (in range)", ncl_in[ncl_flag_col].value_counts(dropna=False))
    st.write(
        f"Computed: Leads={row1}, PNCs={row2}, "
        f"Retained w/out consult={row3}, Scheduled={row4} ({row5}%), "
        f"Showed={row6} ({row7}%), Retained after consult={row8} ({row9}%), "
        f"Total retained={row10} ({row11}%)"
    )
