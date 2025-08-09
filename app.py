# app.py
# ---------------------------
# Conversion Reports (Streamlit)
# - Auth via streamlit-authenticator, YAML in Streamlit Secrets
# - Three uploads (single-month date ranges, weekly uploads roll up monthly):
#   1) Zoom Calls (robust header mapping)
#   2) Leads/PNCs (bucket by intake specialist; PNC logic)
#   3) New Client List (retained Y/N)
# - Top Block KPIs (Leads/PNCs + NCL)
# - Calls table + 3 interactive charts (bottom; Plotly import guarded)
# - No files written to disk
# ---------------------------

import io
import re
import datetime as dt
from typing import List, Dict, Tuple

import pandas as pd
import streamlit as st
import yaml
import streamlit_authenticator as stauth
# plotly import is deferred + guarded later

# =========================
# 🔐 AUTHENTICATION
# =========================
st.set_page_config(page_title="Conversion Reports", page_icon="📈", layout="wide")

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

# =========================
# 📋 CONSTANTS & MAPPINGS (CALLS)
# =========================
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

# =========================
# 📋 CONSTANTS (LEADS/PNCs)
# =========================
EXPECTED_COLUMNS_PNC: List[str] = [
    "First Name","Last Name","Email","Stage","Assigned Intake Specialist","Status",
    "Sub Status","Matter ID","Reason for Rescheduling","No Follow Up (Reason)",
    "Refer Out?","Lead Attorney","Initial Consultation With Pji Law",
    "Initial Consultation Rescheduled With Pji Law","Discovery Meeting Rescheduled With Pji Law",
    "Discovery Meeting With Pji Law","Practice Area"
]
INTAKE_TEAM = ["Anastasia","Aneesah","Azariah","Earl","Faeryal","Kaithlyn",
               "Micayla","Nathanial","Nobuhle","Rialet","Sihle","Thabang","Tiffany"]
OTHER_BUCKET = "Jessie & Everyone else’s intake"
SPECIALIST_NORMALIZE = {"Kaithyln":"Kaithlyn","Kaithlyn":"Kaithlyn"}
EXCLUDED_PNC_STAGES = {
    "Marketing/Scam/Spam (Non-Lead)","Referred Out","No Stage","New Lead",
    "No Follow Up (No Marketing/Communication)","No Follow Up (Receives Marketing/Communication)",
    "Anastasia E","Aneesah S.","Azariah P.","Earl M.","Faeryal S.","Kaithlyn M.",
    "Micayla S.","Nathanial B.","Rialet v H.","Sihle G.","Thabang T.","Tiffany P",
    ":Chloe L:","Nobuhle M."
}

# =========================
# 📋 CONSTANTS (NEW CLIENT LIST)
# =========================
EXPECTED_COLUMNS_NCL: List[str] = [
    "Client Name","Practice Area","Matter Number/Link","Responsible Attorney",
    "Retained With Consult (Y/N)","Date we had BOTH the signed CLA and full payment",
    "Substantially related to existing matter?","Qualifies for bonus?",
    "Primary Intake?","File open fee"
]

# =========================
# 🧮 UTILITIES
# =========================
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

# =========================
# 🧩 PROCESSORS
# =========================
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

    # sum split inbound/outbound if present
    def sum_if_present(cols, target):
        present = [c for c in cols if c in df.columns]
        if present:
            base = pd.to_numeric(df.get(target, 0), errors="coerce").fillna(0)
            for c in present:
                base = base + pd.to_numeric(df[c], errors="coerce").fillna(0)
            df[target] = base
    incoming = [c for c in raw.columns if norm(c) in {"incoming internal","incoming external","incoming"}]
    outgoing = [c for c in raw.columns if norm(c) in {"outgoing internal","outgoing external","outgoing"}]
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

def process_pnc_csv(raw: pd.DataFrame, period_key: str) -> pd.DataFrame:
    raw.columns = [c.strip() for c in raw.columns]
    missing = [c for c in EXPECTED_COLUMNS_PNC if c not in raw.columns]
    if missing:
        raise ValueError(f"Leads/PNCs CSV is missing columns: {missing}")
    df = raw.copy()

    spec = df["Assigned Intake Specialist"].astype(str).str.strip().replace(SPECIALIST_NORMALIZE)
    spec_first = spec.str.split().str[0].fillna("").str.replace(r"[^A-Za-z]","", regex=True).str.title()
    df["Intake Bucket"] = spec_first.apply(lambda s: s if s in INTAKE_TEAM else OTHER_BUCKET)

    df["Lead"] = 1
    stage = df["Stage"].astype(str).str.strip()
    df["PNC"] = (~stage.isin(EXCLUDED_PNC_STAGES)).astype(int)
    df["Month-Year"] = period_key

    agg = df.groupby(["Month-Year","Intake Bucket"], as_index=False).agg(Leads=("Lead","sum"), PNCs=("PNC","sum"))
    return agg

def process_ncl_csv(raw: pd.DataFrame, period_key: str) -> pd.DataFrame:
    raw.columns = [c.strip() for c in raw.columns]
    missing = [c for c in EXPECTED_COLUMNS_NCL if c not in raw.columns]
    if missing:
        raise ValueError(f"New Client List CSV is missing columns: {missing}")
    df = raw.copy()
    df["Month-Year"] = period_key
    agg = df.groupby("Month-Year", as_index=False).agg(
        Retained_Total=("Retained With Consult (Y/N)", lambda s: s.notna().sum()),
        Retained_With_Consult=("Retained With Consult (Y/N)", lambda s: (s.astype(str).str.upper()=="Y").sum()),
        Retained_Without_Consult=("Retained With Consult (Y/N)", lambda s: (s.astype(str).str.upper()=="N").sum()),
    )
    return agg

# =========================
# 🖥️ UPLOAD UI (CLEAN LAYOUT)
# =========================
st.title("Conversion Reports")

def upload_section(section_id: str, title: str) -> Tuple[str, object]:
    """Render a clean section with a same-month date range and uploader.
       Returns (period_key, uploaded_file or None). Keys are safe & unique."""
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

# session state holders
for k in ["batches_calls","batches_pnc","batches_ncl"]:
    if k not in st.session_state:
        st.session_state[k] = []

# Sections
calls_period_key, calls_uploader = upload_section("zoom_calls", "Zoom Calls")
if calls_uploader:
    try:
        raw = pd.read_csv(calls_uploader)
        processed = process_calls_csv(raw, calls_period_key)
        st.session_state["batches_calls"].append(processed)
        st.success(f"Calls: loaded {len(processed)} row(s) for {calls_period_key}.")
    except Exception as e:
        st.error("Could not parse Calls CSV."); st.exception(e)

pnc_period_key, pnc_uploader = upload_section("leads_pncs", "Leads / PNCs")
if pnc_uploader:
    try:
        raw = pd.read_csv(pnc_uploader)
        processed = process_pnc_csv(raw, pnc_period_key)
        st.session_state["batches_pnc"].append(processed)
        st.success(f"Leads/PNCs: loaded {processed['Leads'].sum()} leads, {processed['PNCs'].sum()} PNCs for {pnc_period_key}.")
    except Exception as e:
        st.error("Could not parse Leads/PNCs CSV."); st.exception(e)

ncl_period_key, ncl_uploader = upload_section("new_client_list", "New Client List")
if ncl_uploader:
    try:
        raw = pd.read_csv(ncl_uploader)
        processed = process_ncl_csv(raw, ncl_period_key)
        st.session_state["batches_ncl"].append(processed)
        st.success(f"New Client List: loaded retained totals for {ncl_period_key}.")
    except Exception as e:
        st.error("Could not parse New Client List CSV."); st.exception(e)

# =========================
# 🧮 COMBINE + ROLL-UP
# =========================
# Calls
if st.session_state["batches_calls"]:
    df_calls = pd.concat(st.session_state["batches_calls"], ignore_index=True)
    def _to_sec_col(s: pd.Series) -> pd.Series:
        return pd.to_timedelta(s, errors="coerce").dt.total_seconds().fillna(0.0)
    for c in ["Total Calls","Completed Calls","Outgoing","Received",
              "Forwarded to Voicemail","Answered by Other","Missed"]:
        if c in df_calls.columns:
            df_calls[c] = pd.to_numeric(df_calls[c], errors="coerce").fillna(0).astype(int)
    if "__total_sec" not in df_calls.columns and "Total Call Time" in df_calls.columns:
        df_calls["__total_sec"] = _to_sec_col(df_calls["Total Call Time"])
    if "__hold_sec" not in df_calls.columns and "Total Hold Time" in df_calls.columns:
        df_calls["__hold_sec"] = _to_sec_col(df_calls["Total Hold Time"])
    if "__avg_sec" not in df_calls.columns and "Avg Call Time" in df_calls.columns:
        df_calls["__avg_sec"] = _to_sec_col(df_calls["Avg Call Time"])
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

# Leads/PNCs
df_pnc = pd.concat(st.session_state["batches_pnc"], ignore_index=True) if st.session_state["batches_pnc"] else pd.DataFrame(columns=["Month-Year","Intake Bucket","Leads","PNCs"])
# New Client List
df_ncl = pd.concat(st.session_state["batches_ncl"], ignore_index=True) if st.session_state["batches_ncl"] else pd.DataFrame(columns=["Month-Year","Retained_Total","Retained_With_Consult","Retained_Without_Consult"])

# =========================
# 🔎 FILTERS (CALLS ONLY)
# =========================
st.subheader("Filters (Calls)")
def with_all(options): return ["All"] + sorted(options)

c1, c2, c3, c4 = st.columns(4)
years = sorted({int(m.split("-")[0]) for m in df_calls["Month-Year"].unique()}) if not df_calls.empty else []
sel_year = c1.selectbox("Year", with_all(years) if years else ["All"], index=0)

months_map = {"01":"January","02":"February","03":"March","04":"April","05":"May","06":"June",
              "07":"July","08":"August","09":"September","10":"October","11":"November","12":"December"}
months_available = sorted({m.split("-")[1] for m in df_calls["Month-Year"].unique()}) if not df_calls.empty else []
month_names_available = [months_map[m] for m in months_available] if months_available else []
sel_month_name = c2.selectbox("Month", ["All"] + month_names_available, index=0)

cat_choices = with_all(df_calls["Category"].unique().tolist()) if not df_calls.empty else ["All"]
sel_cat = c3.selectbox("Category", cat_choices, index=0)

base = df_calls if sel_cat == "All" else df_calls[df_calls["Category"] == sel_cat]
name_choices = with_all(base["Name"].unique().tolist()) if not base.empty else ["All"]
sel_name = c4.selectbox("Name", name_choices, index=0)

mask_calls = pd.Series(True, index=df_calls.index)
if sel_year != "All": mask_calls &= df_calls["Month-Year"].str.startswith(str(sel_year))
if sel_month_name != "All":
    month_num = [k for k,v in months_map.items() if v == sel_month_name][0]
    mask_calls &= df_calls["Month-Year"].str.endswith(month_num)
if sel_cat != "All": mask_calls &= df_calls["Category"] == sel_cat
if sel_name != "All": mask_calls &= df_calls["Name"] == sel_name

view_calls = df_calls.loc[mask_calls].copy()

# =========================
# 🧮 TOP BLOCK (Leads/PNCs + NCL)
# =========================
st.markdown("---")
st.subheader("Top Block — Conversion KPIs")

def month_mask(df: pd.DataFrame) -> pd.Series:
    if df.empty: return pd.Series([], dtype=bool)
    m = pd.Series(True, index=df.index)
    if sel_year != "All": m &= df["Month-Year"].str.startswith(str(sel_year))
    if sel_month_name != "All":
        mnum = [k for k,v in months_map.items() if v == sel_month_name][0]
        m &= df["Month-Year"].str.endswith(mnum)
    return m

pnc_view = df_pnc.loc[month_mask(df_pnc)].copy()
ncl_view = df_ncl.loc[month_mask(df_ncl)].copy()

leads_total = int(pnc_view["Leads"].sum()) if not pnc_view.empty else 0
pncs_total  = int(pnc_view["PNCs"].sum()) if not pnc_view.empty else 0
retained_total = int(ncl_view["Retained_Total"].sum()) if not ncl_view.empty else 0
retained_with_consult = int(ncl_view["Retained_With_Consult"].sum()) if not ncl_view.empty else 0
retained_without_consult = int(ncl_view["Retained_Without_Consult"].sum()) if not ncl_view.empty else 0

k1,k2,k3 = st.columns(3)
k1.metric("#Leads", f"{leads_total:,}")
k2.metric("#PNCs", f"{pncs_total:,}")
k3.metric("PNCs who retained without consult", f"{retained_without_consult:,}")

k4,k5,k6 = st.columns(3)
k4.metric("PNCs who scheduled consult", "—")
k5.metric("% of remaining PNCs who scheduled consult", "—")
k6.metric("# of PNCs who showed up for consultation", "—")

k7,k8,k9 = st.columns(3)
k7.metric("# of PNCs who scheduled consult showed up", "—")
k8.metric("# of PNCs who retained after scheduled consult", f"{retained_with_consult:,}")
pct_after_consult = (retained_with_consult/(retained_with_consult+retained_without_consult)*100.0) if (retained_with_consult+retained_without_consult)>0 else 0.0
k9.metric("% of PNCs who retained after consult", f"{pct_after_consult:.1f}%")

k10,k11 = st.columns(2)
k10.metric("# of Total PNCs who retained", f"{retained_total:,}")
pct_total_retained = (retained_total/pncs_total*100.0) if pncs_total>0 else 0.0
k11.metric("% of Total PNCs who retained", f"{pct_total_retained:.1f}%")

with st.expander("How these are calculated"):
    st.markdown(
        """
- **#Leads**: count of rows in the Leads/PNCs file (all rows in Column A = leads).
- **#PNCs**: leads **excluding** rows whose **Stage** is in your excluded list.
- **PNCs who retained without consult**: from *New Client List*, count where **Retained With Consult (Y/N) = N**.
- **# of Total PNCs who retained**: from *New Client List*, count of Y+N in **Retained With Consult (Y/N)**.
- **% of Total PNCs who retained**: `(# total retained / #PNCs) × 100`.
- The consult-scheduling/show-up metrics are placeholders until we connect the consult report.
"""
    )

# =========================
# 📊 CALLS — RESULTS TABLE
# =========================
st.markdown("---")
st.subheader("Calls — Results")
calls_display_cols = [
    "Category","Name","Total Calls","Completed Calls","Outgoing","Received",
    "Forwarded to Voicemail","Answered by Other","Missed",
    "Avg Call Time","Total Call Time","Total Hold Time","Month-Year"
]
st.dataframe(view_calls[calls_display_cols], hide_index=True, use_container_width=True)

csv_buf = io.StringIO()
view_calls[calls_display_cols].to_csv(csv_buf, index=False)
st.download_button("Download filtered Calls CSV", csv_buf.getvalue(), file_name="call_report_filtered.csv", type="primary")

# =========================
# 📈 CALLS — VISUALIZATIONS (GUARDED PLOTLY)
# =========================
st.markdown("---")
st.subheader("Calls — Visualizations")

try:
    import plotly.express as px
    plotly_ok = True
except Exception:
    plotly_ok = False
    st.info("Charts are unavailable because Plotly isn’t installed. "
            "Add `plotly>=5.22` to requirements.txt and restart the app.")

if view_calls.empty:
    st.info("No data for the selected Calls filters.")
elif not plotly_ok:
    st.stop()
else:
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
    comp["Completion Rate (%)"] = comp.apply(lambda r: (r["Completed Calls"]/r["Total Calls"]*100.0)
                                             if r["Total Calls"]>0 else 0.0, axis=1)
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
