# app.py
# ---------------------------
# PJI Law • Call Reports + Conversion Report (Streamlit)
# - Auth via streamlit-authenticator (0.3.2 API; 'form_name' style)
# - Calls module: upload Zoom CSVs, de-dupe, master store (Zoom_Calls), filters, charts (minimized)
# - Conversion Report: uploads via date range (Reporter-only expander),
#   KPIs filtered by Year/Month pick list (like Calls), master store for 4 datasets
# - Optional Google Sheets master store (private) for persistent accumulation & dedupe
# - No secrets in repo; secrets via Streamlit Secrets
# ---------------------------

import io
import re
import hashlib
import datetime as dt
from typing import List, Dict, Tuple, Optional

import pandas as pd
import streamlit as st
import yaml
import streamlit_authenticator as stauth

# =========================
# 🔐 AUTHENTICATION (0.3.2)
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
    # 0.3.2 API uses ('form_name', 'location')
    name, auth_status, username = authenticator.login('Login', 'main')

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
#                         OPTIONAL GOOGLE SHEETS MASTER STORE
# =============================================================================

def _gsheet_client():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        sa = st.secrets.get("gcp_service_account", None)
        ms = st.secrets.get("master_store", None)
        if not sa or not ms or "sheet_url" not in ms:
            return None, None
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(sa, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_url(ms["sheet_url"])
        return gc, sh
    except Exception as e:
        st.warning(f"Master store unavailable: {e}")
        return None, None

GC, GSHEET = _gsheet_client()

def _ws(name: str):
    if GSHEET is None: return None
    try:
        return GSHEET.worksheet(name)
    except Exception:
        try:
            GSHEET.add_worksheet(title=name, rows=1000, cols=26)
            return GSHEET.worksheet(name)
        except Exception as e:
            st.error(f"Could not access/create worksheet '{name}': {e}")
            return None

def _read_ws(ws) -> pd.DataFrame:
    if ws is None: return pd.DataFrame()
    try:
        import gspread_dataframe as gd
        df = gd.get_as_dataframe(ws, evaluate_formulas=True, dtype=str)
        df = df.loc[:, ~df.columns.str.contains("^Unnamed")]
        # best-effort datetime parsing
        for c in df.columns:
            cl = c.lower()
            if "date" in cl or "with pji law" in cl:
                df[c] = pd.to_datetime(df[c], errors="coerce")
        return df.dropna(how="all").fillna("")
    except Exception as e:
        st.error(f"Read failed for '{ws.title}': {e}")
        return pd.DataFrame()

def _append_df(ws, df: pd.DataFrame):
    if ws is None or df is None or df.empty: return
    import gspread_dataframe as gd
    try:
        data = ws.get_all_values()
        if len(data) == 0:
            gd.set_with_dataframe(ws, df.reset_index(drop=True), include_index=False, include_column_header=True)
        else:
            start_row = len(data) + 1
            gd.set_with_dataframe(ws, df.reset_index(drop=True), row=start_row,
                                  include_index=False, include_column_header=False)
    except Exception as e:
        st.error(f"Append failed for '{ws.title}': {e}")

def _unique_key(df: pd.DataFrame, dataset: str) -> pd.Series:
    # Stable dedupe keys per dataset
    if dataset == "Leads_PNCs":
        parts = [df.get("Email",""), df.get("Matter ID",""),
                 df.get("Stage",""),
                 df.get("Initial Consultation With Pji Law",""),
                 df.get("Discovery Meeting With Pji Law","")]
    elif dataset == "New_Clients":
        parts = [df.get("Client Name",""), df.get("Matter Number/Link",""),
                 df.get("Date we had BOTH the signed CLA and full payment",""),
                 df.get("Retained With Consult (Y/N)", df.get("Retained with Consult (Y/N)", ""))]
    elif dataset == "Initial_Consultation":
        parts = [df.get("Email",""), df.get("Matter ID",""),
                 df.get("Initial Consultation With Pji Law",""),
                 df.get("Sub Status","")]
    elif dataset == "Discovery_Meeting":
        parts = [df.get("Email",""), df.get("Matter ID",""),
                 df.get("Discovery Meeting With Pji Law",""),
                 df.get("Sub Status","")]
    elif dataset == "Zoom_Calls":
        parts = [df.get("Month-Year",""), df.get("Name",""), df.get("Category","")]
        concat = (pd.Series(parts[0]).astype(str).str.strip() + "|" +
                  pd.Series(parts[1]).astype(str).str.strip() + "|" +
                  pd.Series(parts[2]).astype(str).str.strip())
        return pd.util.hashing.hash_pandas_object(concat, index=False).astype(str)
    else:
        parts = [df.astype(str).agg("-".join, axis=1)]
        return pd.util.hashing.hash_pandas_object(pd.DataFrame({"k": parts[0]}), index=False).astype(str)

    concat = (pd.Series(parts[0]).astype(str).str.strip() + "|" +
              pd.Series(parts[1]).astype(str).str.strip() + "|" +
              pd.Series(parts[2]).astype(str) + "|" +
              pd.Series(parts[3]).astype(str) + "|" +
              pd.Series(parts[4]).astype(str))
    return pd.util.hashing.hash_pandas_object(concat, index=False).astype(str)

def _upsert_master(dataset_name: str, df_new: pd.DataFrame, keycol: str = "__key__", month_scope: Optional[Tuple[pd.Timestamp,pd.Timestamp]]=None):
    ws = _ws(dataset_name)
    if ws is None:
        st.warning(f"Master store not configured; running in session-only mode.")
        return False

    df_new = df_new.copy()
    df_new.columns = [str(c).strip() for c in df_new.columns]
    if dataset_name == "New_Clients" and "Retained with Consult (Y/N)" in df_new.columns and "Retained With Consult (Y/N)" not in df_new.columns:
        df_new = df_new.rename(columns={"Retained with Consult (Y/N)":"Retained With Consult (Y/N)"})

    df_master = _read_ws(ws)

    df_new[keycol] = _unique_key(df_new, dataset_name)
    if not df_master.empty and keycol not in df_master.columns:
        df_master[keycol] = _unique_key(df_master, dataset_name)

    if month_scope is not None and not df_master.empty:
        start, end = month_scope
        date_col = None
        if dataset_name == "New_Clients":
            date_col = "Date we had BOTH the signed CLA and full payment"
        elif dataset_name == "Initial_Consultation":
            date_col = "Initial Consultation With Pji Law"
        elif dataset_name == "Discovery_Meeting":
            date_col = "Discovery Meeting With Pji Law"
        elif dataset_name == "Zoom_Calls":
            date_col = None  # aggregated by Month-Year already

        if date_col and date_col in df_master.columns:
            m = pd.to_datetime(df_master[date_col], errors="coerce").between(start, end)
            df_master = df_master.loc[~m].copy()

    combined = pd.concat([df_master, df_new], ignore_index=True)
    combined = combined.drop_duplicates(subset=[keycol], keep="last")

    try:
        import gspread_dataframe as gd
        ws.clear()
        gd.set_with_dataframe(ws, combined.drop(columns=[keycol], errors="ignore"),
                              include_index=False, include_column_header=True)
        return True
    except Exception as e:
        st.error(f"Upsert failed for '{dataset_name}': {e}")
        return False

def _maybe_master(tab_name: str, fallback_df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    ws = _ws(tab_name)
    if ws is None:
        return fallback_df
    dfm = _read_ws(ws)
    return (dfm if not dfm.empty else fallback_df)

# =============================================================================
#                                  CALLS MODULE
# =============================================================================

# Constants & mappings (Calls)
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
    pos = uploaded_file.tell()
    uploaded_file.seek(0)
    data = uploaded_file.read()
    uploaded_file.seek(pos if pos is not None else 0)
    return hashlib.md5(data).hexdigest()

def process_calls_csv(raw: pd.DataFrame, period_key: str) -> pd.DataFrame:
    def norm(s: str) -> str:
        s = s.strip().lower()
        s = re.sub(r"[\s_]+"," ",s); s = re.sub(r"[^a-z0-9 ]","",s)
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
        "Answered by Other":["answered by other","answered by others","answered by other member","answered by other user","answered by other extension"],
        "Missed":["missed","missed calls","abandoned","ring no answer"],
        "Avg Call Time":["avg call time","average call time","avg call duration","average call duration","avg talk time","average talk time"],
        "Total Call Time":["total call time","total call duration","total talk time"],
        "Total Hold Time":["total hold time","hold time total","total on hold"],
    }
    rename_map, used = {}, set()
    for canonical, alts in synonyms.items():
        for actual, n in col_norm.items():
            if actual in used: continue
            if n in alts:
                rename_map[actual] = canonical; used.add(actual); break
    df = raw.rename(columns=rename_map).copy()

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
    sum_if_present(incoming, "Received"); sum_if_present(outgoing, "Outgoing")

    missing = [c for c in REQUIRED_COLUMNS_CALLS if c not in df.columns]
    if missing:
        st.error("Calls CSV headers detected: " + ", ".join(list(raw.columns)))
        raise ValueError(f"Calls CSV is missing columns after normalization: {missing}")

    df = df[df["Name"].isin(ALLOWED_CALLS)].copy()
    df["Name"] = df["Name"].replace(RENAME_NAME_CALLS)
    df["Category"] = df["Name"].map(lambda n: CATEGORY_CALLS.get(n, "Other"))

    for c in ["Total Calls","Completed Calls","Outgoing","Received","Forwarded to Voicemail","Answered by Other","Missed"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)

    df["_avg_sec"] = pd.to_timedelta(df["Avg Call Time"], errors="coerce").dt.total_seconds().fillna(0.0)
    df["_total_sec"] = pd.to_timedelta(df["Total Call Time"], errors="coerce").dt.total_seconds().fillna(0.0)
    df["_hold_sec"]  = pd.to_timedelta(df["Total Hold Time"], errors="coerce").dt.total_seconds().fillna(0.0)
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

st.title("📞 Zoom Call Reports")

def upload_section(section_id: str, title: str) -> Tuple[str, object]:
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

# Session state for Calls
if "batches_calls" not in st.session_state:
    st.session_state["batches_calls"] = []
if "hashes_calls" not in st.session_state:
    st.session_state["hashes_calls"] = set()

# Reporter access control for uploaders
is_reporter = True
try:
    allowed = set(st.secrets.get("report_access", {}).get("reporters", []))
    is_reporter = (username in allowed) or (name in allowed) if allowed else True
except Exception:
    pass

if is_reporter:
    with st.expander("🛠️ Reporter: Upload data (Calls & Conversion)", expanded=False):
        # Calls uploader
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

                    # Append Calls to master (optional)
                    if st.checkbox("Append Calls upload to Master (deduped)", value=False, key="append_calls_now"):
                        if GSHEET is None:
                            st.warning("Master store not configured; cannot append Calls.")
                        else:
                            y, m = calls_period_key.split("-")
                            start = pd.Timestamp(int(y), int(m), 1)
                            end = (start + pd.offsets.MonthEnd(1))
                            ok_calls = _upsert_master("Zoom_Calls", processed, month_scope=(start, end))
                            st.success("Calls master updated.") if ok_calls else st.warning("Calls master update failed.")
            except Exception as e:
                st.error("Could not parse Calls CSV."); st.exception(e)
else:
    st.info("Viewer mode: upload controls are hidden (Reporter-only).")

# Combine Calls from session
if st.session_state["batches_calls"]:
    df_calls = pd.concat(st.session_state["batches_calls"], ignore_index=True)
else:
    df_calls = pd.DataFrame(columns=OUT_COLUMNS_CALLS + ["__avg_sec","__total_sec","__hold_sec"])

# Prefer master Zoom_Calls for accumulation
df_calls = _maybe_master("Zoom_Calls", df_calls)
if df_calls is None:
    df_calls = pd.DataFrame(columns=OUT_COLUMNS_CALLS + ["__avg_sec","__total_sec","__hold_sec"])

# ---------------------------
# Calls Filters (Year/Month, Category, Name)
# ---------------------------
st.subheader("Filters — Calls")
def with_all(options): return ["All"] + sorted(options)
months_map = {"01":"January","02":"February","03":"March","04":"April","05":"May","06":"June",
              "07":"July","08":"August","09":"September","10":"October","11":"November","12":"December"}
def month_num_to_name(mnum): return months_map.get(mnum, mnum)

def union_months_from(df):
    if isinstance(df, pd.DataFrame) and not df.empty and "Month-Year" in df.columns:
        return sorted(set(df["Month-Year"].dropna().astype(str)))
    return []

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

def calls_period_mask(df: pd.DataFrame) -> pd.Series:
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

filtered_calls = df_calls.loc[calls_period_mask(df_calls)].copy()
mask_calls_extra = pd.Series(True, index=filtered_calls.index)
if sel_cat != "All":  mask_calls_extra &= filtered_calls["Category"] == sel_cat
if sel_name != "All": mask_calls_extra &= filtered_calls["Name"] == sel_name
view_calls = filtered_calls.loc[mask_calls_extra].copy()

# Calls results + download
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

# Calls — Visualizations (default minimized)
st.subheader("Calls — Visualizations")
try:
    import plotly.express as px
    plotly_ok = True
except Exception:
    plotly_ok = False
    st.info("Charts are unavailable because Plotly isn’t installed. Add `plotly>=5.22` to requirements.txt.")

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
    with st.expander("📈 Call volume trend over time", expanded=False):
        fig1 = px.line(vol_long, x="_ym", y="Count", color="Metric", markers=True,
                       labels={"_ym":"Month","Count":"Calls"})
        fig1.update_layout(xaxis=dict(tickformat="%b %Y"))
        st.plotly_chart(fig1, use_container_width=True)

    comp = (view_calls.groupby("Name", as_index=False)[["Completed Calls","Total Calls"]].sum())
    comp["Completion Rate (%)"] = comp.apply(
        lambda r: (r["Completed Calls"]/r["Total Calls"]*100.0) if r["Total Calls"]>0 else 0.0, axis=1)
    comp = comp.sort_values("Completion Rate (%)", ascending=False)
    with st.expander("✅ Completion rate by staff", expanded=False):
        fig2 = px.bar(comp, x="Name", y="Completion Rate (%)",
                      labels={"Name":"Staff","Completion Rate (%)":"Completion Rate (%)"})
        fig2.update_layout(xaxis={'categoryorder':'array','categoryarray':comp["Name"].tolist()})
        st.plotly_chart(fig2, use_container_width=True)

    by_name = view_calls.groupby("Name", as_index=False).apply(
        lambda g: pd.Series({"Avg Seconds (weighted)": (g["__avg_sec"]*g["Total Calls"]).sum()/max(g["Total Calls"].sum(),1)})
    )
    by_name["Avg Minutes"] = by_name["Avg Seconds (weighted)"]/60.0
    by_name = by_name.sort_values("Avg Minutes", ascending=False)
    with st.expander("⏱️ Average call duration by staff (minutes)", expanded=False):
        fig3 = px.bar(by_name, x="Avg Minutes", y="Name", orientation="h",
                      labels={"Avg Minutes":"Minutes","Name":"Staff"})
        st.plotly_chart(fig3, use_container_width=True)

# =============================================================================
#                           CONVERSION REPORT (RESULT FILTERS BY MONTH/YEAR)
# =============================================================================

st.markdown("---")
st.header("Conversion Report")

# ----- Uploaders for Conversion (Reporter expander; still date-range for uploads) -----
def _read_any(upload):
    if upload is None: return None
    name = (upload.name or "").lower()
    if name.endswith(".csv"):
        try:
            df = pd.read_csv(upload)
        except Exception:
            upload.seek(0); df = pd.read_csv(upload, engine="python")
        df.columns = [str(c).strip() for c in df.columns]; return df
    try:
        if name.endswith(".xlsx"):
            df = pd.read_excel(upload, engine="openpyxl")
        elif name.endswith(".xls"):
            df = pd.read_excel(upload, engine="xlrd")
        else:
            df = pd.read_excel(upload)
    except ImportError as e:
        need = "openpyxl" if name.endswith(".xlsx") else "xlrd"
        st.error(f"Excel support not available for `{upload.name}`. Add `{need}` to requirements.txt or upload CSV.")
        raise
    except Exception:
        upload.seek(0)
        df = pd.read_excel(upload)
    df.columns = [str(c).strip() for c in df.columns]
    return df

if is_reporter:
    with st.expander("🛠️ Reporter: Upload Conversion files (date range for uploads)", expanded=False):
        c1, c2 = st.columns(2)
        upload_start = c1.date_input("Upload start date", value=dt.date.today().replace(day=1), key="conv_upload_start")
        upload_end   = c2.date_input("Upload end date",   value=dt.date.today(), key="conv_upload_end")
        if upload_start > upload_end:
            st.error("Upload start must be on or before end."); st.stop()

        up_leads = st.file_uploader("Upload **Leads_PNCs**", type=["csv","xls","xlsx"], key="up_leads_pncs")
        up_ncl   = st.file_uploader("Upload **New Clients List**", type=["csv","xls","xlsx"], key="up_ncl")
        up_init  = st.file_uploader("Upload **Initial_Consultation**", type=["csv","xls","xlsx"], key="up_initial")
        up_disc  = st.file_uploader("Upload **Discovery_Meeting**", type=["csv","xls","xlsx"], key="up_discovery")

        df_leads_up = _read_any(up_leads)
        df_ncl_up   = _read_any(up_ncl)
        df_init_up  = _read_any(up_init)
        df_disc_up  = _read_any(up_disc)

        # Append to master if requested
        append_now = st.checkbox("Append these uploads to the Master Store (deduped)", value=False)
        if append_now:
            # Define month scope as the month of upload_start (purge/replace month for date-bearing sheets)
            month_scope = (pd.Timestamp(upload_start).replace(day=1),
                           (pd.Timestamp(upload_start).replace(day=28) + pd.Timedelta(days=4)).replace(day=1) - pd.Timedelta(days=1))
            ok = True
            if df_leads_up is not None: ok &= _upsert_master("Leads_PNCs", df_leads_up)
            if df_ncl_up   is not None: ok &= _upsert_master("New_Clients", df_ncl_up, month_scope=month_scope)
            if df_init_up  is not None: ok &= _upsert_master("Initial_Consultation", df_init_up, month_scope=month_scope)
            if df_disc_up  is not None: ok &= _upsert_master("Discovery_Meeting",   df_disc_up, month_scope=month_scope)
            st.success("Masters updated (deduped).") if ok else st.warning("Some master updates failed — see messages above.")

# ----- Read effective data sources for Conversion (prefer master; fallback to last uploads if available) -----
df_leads = _maybe_master("Leads_PNCs", None)
df_ncl   = _maybe_master("New_Clients", None)
df_init  = _maybe_master("Initial_Consultation", None)
df_disc  = _maybe_master("Discovery_Meeting", None)

# if master isn't configured or empty, try to use the last uploaded (if any)
if df_leads is None: df_leads = 'df_leads_up' in locals() and isinstance(df_leads_up, pd.DataFrame) and df_leads_up or None
if df_ncl   is None: df_ncl   = 'df_ncl_up'   in locals() and isinstance(df_ncl_up,   pd.DataFrame) and df_ncl_up   or None
if df_init  is None: df_init  = 'df_init_up'  in locals() and isinstance(df_init_up,  pd.DataFrame) and df_init_up  or None
if df_disc  is None: df_disc  = 'df_disc_up'  in locals() and isinstance(df_disc_up,  pd.DataFrame) and df_disc_up  or None

if not all([isinstance(df, pd.DataFrame) for df in [df_leads, df_ncl, df_init, df_disc]]):
    st.info("No Conversion data available yet. Reporter must upload files or configure the Master Store.")
    st.stop()

# ----- Conversion Filters — Year/Month (for results) -----
months_map = {"01":"January","02":"February","03":"March","04":"April","05":"May","06":"June",
              "07":"July","08":"August","09":"September","10":"October","11":"November","12":"December"}

def _extract_months_from(df: pd.DataFrame, date_cols: list[str]) -> set[str]:
    if df is None or df.empty: return set()
    all_months = set()
    for col in date_cols:
        if col in df.columns:
            s = pd.to_datetime(df[col], errors="coerce")
            all_months |= set(s.dt.strftime("%Y-%m").dropna())
    return all_months

conv_months = set()
conv_months |= _extract_months_from(df_ncl,  ["Date we had BOTH the signed CLA and full payment"])
conv_months |= _extract_months_from(df_init, ["Initial Consultation With Pji Law"])
conv_months |= _extract_months_from(df_disc, ["Discovery Meeting With Pji Law"])
conv_months = sorted(m for m in conv_months if isinstance(m, str))

if conv_months:
    latest_my = max(conv_months)
    years = sorted({m.split("-")[0] for m in conv_months})
    cyr, cmo = st.columns(2)
    sel_year_conv = cyr.selectbox("Year (Conversion)", ["All"] + years,
                                  index=(years.index(latest_my.split("-")[0])+1 if latest_my.split("-")[0] in years else 0))
    def month_num_to_name(n): return months_map.get(n, n)
    def months_for_year(y):
        return sorted({m.split("-")[1] for m in conv_months if y == "All" or m.startswith(y)})

    mnums = months_for_year(sel_year_conv)
    mnames = ["All"] + [month_num_to_name(m) for m in mnums]
    default_mname = month_num_to_name(latest_my.split("-")[1])
    sel_mname_conv = cmo.selectbox("Month (Conversion)", mnames,
                                   index=(mnames.index(default_mname) if default_mname in mnames else 0))
else:
    sel_year_conv, sel_mname_conv = "All", "All"
    st.info("No month metadata detected yet in Conversion datasets.")

def _conv_mask_by_month(df: pd.DataFrame, date_col: str) -> pd.Series:
    if df is None or df.empty or date_col not in df.columns:
        return pd.Series([False]* (0 if df is None else len(df)))
    s = pd.to_datetime(df[date_col], errors="coerce")
    if sel_year_conv == "All" and sel_mname_conv == "All":
        return s.notna()
    mask = pd.Series(True, index=df.index)
    if sel_year_conv != "All":
        mask &= s.dt.year.astype("Int64") == int(sel_year_conv)
    if sel_mname_conv != "All":
        month_num = next((k for k, v in months_map.items() if v == sel_mname_conv), None)
        if month_num:
            mask &= s.dt.month.astype("Int64") == int(month_num)
    return mask & s.notna()

# =========================
# Compute Conversion KPIs (using Year/Month filter)
# =========================

# Validate shared columns
for df, label in [(df_leads,"Leads_PNCs"), (df_init,"Initial_Consultation"), (df_disc,"Discovery_Meeting")]:
    needed = [
        "First Name","Stage","Sub Status",
        "Initial Consultation With Pji Law","Discovery Meeting With Pji Law"
    ]
    for col in needed:
        if col not in df.columns:
            st.error(f"[{label}] missing expected column `{col}`."); st.stop()

# NCL flag column (tolerant)
ncl_flag_col = None
for candidate in ["Retained With Consult (Y/N)", "Retained with Consult (Y/N)"]:
    if candidate in df_ncl.columns:
        ncl_flag_col = candidate; break
if "Date we had BOTH the signed CLA and full payment" not in df_ncl.columns:
    st.error("[New Clients List] missing `Date we had BOTH the signed CLA and full payment`."); st.stop()

# Row 1 — # of Leads (exclude Non-Lead)
row1 = int(df_leads.loc[
    df_leads["Stage"].astype(str).str.strip() != "Marketing/Scam/Spam (Non-Lead)",
    "First Name"
].shape[0])

# Row 2 — # of PNCs (exclude the big set)
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

# In-range subsets by Year/Month filter
init_mask = _conv_mask_by_month(df_init, "Initial Consultation With Pji Law")
disc_mask = _conv_mask_by_month(df_disc, "Discovery Meeting With Pji Law")
ncl_mask  = _conv_mask_by_month(df_ncl,  "Date we had BOTH the signed CLA and full payment")

init_in = df_init.loc[init_mask].copy()
disc_in = df_disc.loc[disc_mask].copy()
ncl_in  = df_ncl.loc[ncl_mask].copy()

# Row 3 — retained without consult (flag == N)
# Row 8 — retained after consult (anything not N)
if ncl_flag_col:
    flag_in = ncl_in[ncl_flag_col].astype(str).str.strip().str.upper()
    row3 = int((flag_in == "N").sum())
    row8 = int((flag_in != "N").sum())
else:
    row3 = 0
    row8 = int(ncl_in.shape[0])

# Row 10 — total retained
row10 = int(ncl_in.shape[0])

# Row 4 — scheduled consults (Init + Discovery) — do NOT exclude "Follow Up"
row4 = int(init_in.shape[0] + disc_in.shape[0])

# Row 6 — showed up for consultation (Sub Status == "Pnc")
row6 = int(
    (init_in["Sub Status"].astype(str).str.strip() == "Pnc").sum()
    + (disc_in["Sub Status"].astype(str).str.strip() == "Pnc").sum()
)

# Percentages
def _pct(numer, denom):
    if denom is None or denom == 0: return 0
    return round((numer / denom) * 100)

row5  = _pct(row4, (row2 - row3))   # % remaining PNCs who scheduled consult
row7  = _pct(row6, row4)            # % scheduled who showed
row9  = _pct(row8, row4)            # % retained after consult
row11 = _pct(row10, row2)           # % total retained of PNCs

# Display Conversion Report
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

with st.expander("Debug details (for reconciliation)", expanded=False):
    st.write("Leads_PNCs — Stage value counts", df_leads["Stage"].value_counts(dropna=False))
    st.write("Initial_Consultation — Sub Status (in month filter)", init_in["Sub Status"].value_counts(dropna=False))
    st.write("Discovery_Meeting — Sub Status (in month filter)",   disc_in["Sub Status"].value_counts(dropna=False))
    if ncl_flag_col:
        st.write("New Clients List — Retained split (in month filter)", ncl_in[ncl_flag_col].value_counts(dropna=False))
    st.write(
        f"Computed: Leads={row1}, PNCs={row2}, "
        f"Retained w/out consult={row3}, Scheduled={row4} ({row5}%), "
        f"Showed={row6} ({row7}%), Retained after consult={row8} ({row9}%), "
        f"Total retained={row10} ({row11}%)"
    )

# --------- Sidebar Master controls (optional) ----------
with st.sidebar.expander("📦 Master Data (Google Sheets)", expanded=False):
    if GSHEET is None:
        st.caption("Not configured. Add `[gcp_service_account]` and `[master_store]` to Secrets.")
    else:
        st.success("Connected to Master Store (Google Sheets).")
    st.caption("These actions update persistent master datasets. Deduping is automatic.")

    today = dt.date.today()
    mstart = st.date_input("Month scope start", value=today.replace(day=1), key="ms_start")
    mend   = st.date_input("Month scope end",   value=(today.replace(day=28) + dt.timedelta(days=4)), key="ms_end")
    mstart_ts, mend_ts = pd.Timestamp(mstart), pd.Timestamp(mend)

    colA, colB = st.columns(2)
    if colA.button("Purge month in NCL (master)"):
        if _upsert_master("New_Clients", pd.DataFrame(), month_scope=(mstart_ts, mend_ts)):
            st.success("Purged selected month in New_Clients.")
    if colB.button("Wipe ALL masters"):
        if GSHEET is not None:
            for tab in ["Leads_PNCs", "New_Clients", "Initial_Consultation", "Discovery_Meeting", "Zoom_Calls"]:
                ws = _ws(tab)
                if ws: ws.clear()
            st.success("All master tabs cleared.")
