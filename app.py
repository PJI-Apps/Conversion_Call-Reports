# app.py
# PJI Law • Conversion and Call Report (Streamlit)

import io
import re
import json
import hashlib
import datetime as dt
from datetime import date, timedelta
from calendar import monthrange
from typing import List, Dict, Tuple, Optional

import pandas as pd
import streamlit as st
import yaml
import streamlit_authenticator as stauth

# ───────────────────────────────────────────────────────────────────────────────
# Auth (version-tolerant) + page setup
# ───────────────────────────────────────────────────────────────────────────────
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

    def _login_compat(authenticator_obj):
        # New API (0.4.x+)
        try:
            res = authenticator_obj.login(
                fields={"Form name": "Login", "Username": "Username", "Password": "Password"},
                location="main",
            )
            if isinstance(res, tuple) and len(res) == 3:
                return res
            if isinstance(res, dict):
                return res.get("name"), res.get("authentication_status"), res.get("username")
            if res is not None:
                return res
        except TypeError:
            pass
        except Exception:
            pass
        # Old API (0.3.2)
        try:
            return authenticator_obj.login("Login", "main")
        except TypeError:
            return authenticator_obj.login(form_name="Login", location="main")

    name, auth_status, username = _login_compat(authenticator)

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

st.title("📊 Conversion and Call Report")

# ───────────────────────────────────────────────────────────────────────────────
# Quiet log collector
# ───────────────────────────────────────────────────────────────────────────────
if "logs" not in st.session_state:
    st.session_state["logs"] = []
def log(msg: str): st.session_state["logs"].append(msg)

# ───────────────────────────────────────────────────────────────────────────────
# Google Sheets master + caching
# ───────────────────────────────────────────────────────────────────────────────
TAB_NAMES = {
    "CALLS": "Call_Report_Master",
    "LEADS": "Leads_PNCs_Master",
    "INIT":  "Initial_Consultation_Master",
    "DISC":  "Discovery_Meeting_Master",
    "NCL":   "New_Client_List_Master",
}
TAB_FALLBACKS = {
    "CALLS": ["Zoom_Calls"],
    "LEADS": ["Leads_PNCs"],
    "INIT":  ["Initial_Consultation"],
    "DISC":  ["Discovery_Meeting"],
    "NCL":   ["New_Clients", "New Client List"],
}

if "gs_ver" not in st.session_state:
    st.session_state["gs_ver"] = 0

@st.cache_resource(show_spinner=False)
def _gsheet_client_cached():
    import gspread
    from google.oauth2.service_account import Credentials
    sa = st.secrets.get("gcp_service_account", None)
    if not sa:
        raw = st.secrets.get("gcp_service_account_json", None)
        if raw: sa = json.loads(raw)
    ms = st.secrets.get("master_store", None)
    if not sa or not ms or "sheet_url" not in ms:
        return None, None
    if "client_email" not in sa:
        raise ValueError("Service account object missing 'client_email'")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(sa, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(ms["sheet_url"])
    return gc, sh

def _gsheet_client():
    try:
        return _gsheet_client_cached()
    except Exception as e:
        st.warning(f"Master store unavailable: {e}")
        return None, None

GC, GSHEET = _gsheet_client()

def _ws(title: str):
    """Safe worksheet getter/creator."""
    if GSHEET is None: return None
    import gspread
    from gspread.exceptions import APIError, WorksheetNotFound
    for delay in (0.0, 0.8, 1.6):
        try:
            if delay: import time; time.sleep(delay)
            return GSHEET.worksheet(title)
        except WorksheetNotFound:
            break
        except APIError:
            continue
        except Exception:
            continue
    for fb in TAB_FALLBACKS.get(title, []):
        try:
            return GSHEET.worksheet(fb)
        except Exception:
            continue
    try:
        GSHEET.add_worksheet(title=title, rows=2000, cols=40)
        return GSHEET.worksheet(title)
    except Exception as e:
        try:
            return GSHEET.worksheet(title)
        except Exception:
            st.error(f"Could not access/create worksheet '{title}': {e}")
            return None

def _clean_datestr(x):
    if pd.isna(x): return x
    s = str(x).strip()
    s = re.sub(r"\s+at\s+", " ", s, flags=re.I)
    s = re.sub(r"\s+(ET|EDT|EST|CT|CDT|CST|MT|MDT|MST|PT|PDT)$", "", s)
    return s

@st.cache_data(ttl=300, show_spinner=False)
def _read_ws_cached(sheet_url: str, tab_title: str, ver: int) -> pd.DataFrame:
    import gspread_dataframe as gd
    gc, sh = _gsheet_client_cached()
    ws = sh.worksheet(tab_title)
    last_exc = None
    for delay in (0.0, 1.0, 2.0):
        try:
            if delay: import time; time.sleep(delay)
            df = gd.get_as_dataframe(ws, evaluate_formulas=True, dtype=str)
            last_exc = None; break
        except Exception as e:
            last_exc = e
    if last_exc is not None: raise last_exc
    df = df.loc[:, ~df.columns.str.contains("^Unnamed")]
    for c in df.columns:
        cl = c.lower()
        if "date" in cl or "with pji law" in cl or "batch" in cl:
            df[c] = pd.to_datetime(df[c].map(_clean_datestr), errors="coerce")
    return df.dropna(how="all").fillna("")

def _read_ws_by_name(logical_key: str) -> pd.DataFrame:
    if GSHEET is None: return pd.DataFrame()
    ws = _ws(TAB_NAMES[logical_key])
    if ws is None: return pd.DataFrame()
    try:
        sheet_url = st.secrets["master_store"]["sheet_url"]
        return _read_ws_cached(sheet_url, ws.title, st.session_state["gs_ver"])
    except Exception as e:
        log(f"Read failed for '{ws.title}': {e}")
        return pd.DataFrame()

def _write_ws_by_name(logical_key: str, df: pd.DataFrame):
    ws = _ws(TAB_NAMES[logical_key])
    if ws is None or df is None: return False
    try:
        import gspread_dataframe as gd
        ws.clear()
        gd.set_with_dataframe(ws, df.reset_index(drop=True), include_index=False, include_column_header=True)
        st.session_state["gs_ver"] += 1
        return True
    except Exception as e:
        st.error(f"Write failed for '{TAB_NAMES[logical_key]}': {e}")
        return False

# ───────────────────────────────────────────────────────────────────────────────
# Render Admin Sidebar early so it always shows
# ───────────────────────────────────────────────────────────────────────────────
def render_admin_sidebar():
    with st.sidebar.expander("📦 Master Data (Google Sheets) — Admin", expanded=False):
        if GSHEET is None:
            st.warning("Not connected to the master store.")
            st.caption("Add `[gcp_service_account]` and `[master_store]` to Secrets.")
            if st.button("🧹 Master Reset (session & caches)", use_container_width=True):
                for k in ["hashes_calls","hashes_conv","exp_upload_open","logs"]:
                    st.session_state.pop(k, None)
                try: st.cache_data.clear()
                except: pass
                try: st.cache_resource.clear()
                except: pass
                st.success("Reset complete. Reloading…"); st.rerun()
            return

        st.success("Connected to Master Store (Google Sheets).")
        st.caption("Tabs used: " + ", ".join(TAB_NAMES.values()))

        if st.button("🔄 Refresh data now", use_container_width=True):
            st.session_state["gs_ver"] += 1
            st.rerun()

        sheets = {
            "Calls": "CALLS",
            "Leads/PNCs": "LEADS",
            "Initial Consultation": "INIT",
            "Discovery Meeting": "DISC",
            "New Client List": "NCL",
        }
        sel_label = st.selectbox("Select sheet", list(sheets.keys()))
        key = sheets[sel_label]

        colY, colM = st.columns(2)
        yr = colY.number_input("Year", min_value=2000, max_value=2100,
                               value=date.today().year, step=1)
        mo = colM.number_input("Month", min_value=1, max_value=12,
                               value=date.today().month, step=1)

        def _date_col_for(logical_key: str) -> Optional[str]:
            if logical_key == "NCL":  return "Date we had BOTH the signed CLA and full payment"
            if logical_key == "INIT": return "Initial Consultation With Pji Law"
            if logical_key == "DISC": return "Discovery Meeting With Pji Law"
            return None  # LEADS has no single canonical date; CALLS uses Month-Year

        def _purge_month(logical_key: str, year: int, month: int) -> tuple[bool, int]:
            df = _read_ws_by_name(logical_key)
            if df.empty: return True, 0
            if logical_key == "CALLS":
                mkey = f"{year}-{month:02d}"
                before = len(df)
                df2 = df.loc[df["Month-Year"].astype(str).str.strip() != mkey].copy()
                ok = _write_ws_by_name(logical_key, df2)
                return ok, before - len(df2)
            date_col = _date_col_for(logical_key)
            if not date_col or date_col not in df.columns: return False, 0
            s = pd.to_datetime(df[date_col], errors="coerce")
            mask_drop = (s.dt.year == int(year)) & (s.dt.month == int(month))
            removed = int(mask_drop.sum())
            df2 = df.loc[~mask_drop].copy()
            ok = _write_ws_by_name(logical_key, df2)
            return ok, removed

        def _wipe_all(logical_key: str) -> bool:
            return _write_ws_by_name(logical_key, pd.DataFrame())

        def _dedupe_sheet(logical_key: str) -> tuple[bool, int]:
            df = _read_ws_by_name(logical_key)
            if df.empty: return True, 0
            if logical_key == "LEADS":
                k = (df.get("Email","").astype(str).str.strip() + "|" +
                     df.get("Matter ID","").astype(str).str.strip() + "|" +
                     df.get("Stage","").astype(str).str.strip() + "|" +
                     df.get("Initial Consultation With Pji Law","").astype(str) + "|" +
                     df.get("Discovery Meeting With Pji Law","").astype(str))
            elif logical_key == "INIT":
                k = (df.get("Email","").astype(str).str.strip() + "|" +
                     df.get("Matter ID","").astype(str).str.strip() + "|" +
                     df.get("Initial Consultation With Pji Law","").astype(str) + "|" +
                     df.get("Sub Status","").astype(str).str.strip())
            elif logical_key == "DISC":
                k = (df.get("Email","").astype(str).str.strip() + "|" +
                     df.get("Matter ID","").astype(str).str.strip() + "|" +
                     df.get("Discovery Meeting With Pji Law","").astype(str) + "|" +
                     df.get("Sub Status","").astype(str).str.strip())
            elif logical_key == "NCL":
                flag_col = "Retained With Consult (Y/N)"
                if flag_col not in df.columns and "Retained with Consult (Y/N)" in df.columns:
                    df = df.rename(columns={"Retained with Consult (Y/N)": flag_col})
                k = (df.get("Client Name","").astype(str).str.strip() + "|" +
                     df.get("Matter Number/Link","").astype(str).str.strip() + "|" +
                     df.get("Date we had BOTH the signed CLA and full payment","").astype(str) + "|" +
                     df.get(flag_col,"").astype(str).str.strip())
            else:  # CALLS
                k = (df.get("Month-Year","").astype(str).str.strip() + "|" +
                     df.get("Name","").astype(str).str.strip() + "|" +
                     df.get("Category","").astype(str).str.strip())
            before = len(df)
            df2 = df.loc[~k.duplicated(keep="last")].copy()
            ok = _write_ws_by_name(logical_key, df2)
            return ok, before - len(df2)

        st.divider()
        st.subheader("Maintenance")
        st.caption("Safely manage master data. Actions are immediate.")

        with st.container(border=True):
            st.markdown("**Purge a month**")
            st.caption("Remove all rows for the selected sheet and month (above).")
            if st.button("Purge Month", use_container_width=True):
                ok, removed = _purge_month(key, int(yr), int(mo))
                if ok:
                    st.success(f"Purged {removed} row(s) for {int(yr)}-{int(mo):02d} in '{sel_label}'.")
                    if key == "CALLS": st.session_state.get("hashes_calls", set()).clear()
                    else:              st.session_state.get("hashes_conv", set()).clear()
                    st.session_state["gs_ver"] += 1; st.rerun()
                else:
                    st.warning("Nothing purged (missing date column or unsupported for this sheet).")

        with st.container(border=True):
            st.markdown("**Re-dedupe sheet**")
            st.caption("Rebuilds unique rows using the same keys as the uploader.")
            if st.button("Re-dedupe sheet", use_container_width=True):
                ok, removed = _dedupe_sheet(key)
                st.success(f"Removed {removed} duplicate row(s).") if ok else st.error("Re-dedupe failed.")
                if ok: st.session_state["gs_ver"] += 1; st.rerun()

        with st.container(border=True):
            st.markdown("**Wipe ALL rows**")
            st.caption("Deletes every row in the selected sheet. Use with care.")
            confirm_wipe = st.checkbox("I understand this cannot be undone.", key="confirm_wipe")
            if st.button("Wipe ALL rows", disabled=not confirm_wipe, use_container_width=True):
                ok = _wipe_all(key)
                st.success(f"All rows wiped in '{sel_label}'.") if ok else st.error("Wipe failed.")
                if ok:
                    if key == "CALLS": st.session_state.get("hashes_calls", set()).clear()
                    else:              st.session_state.get("hashes_conv", set()).clear()
                    st.session_state["gs_ver"] += 1; st.rerun()

# Render it now
render_admin_sidebar()

# ───────────────────────────────────────────────────────────────────────────────
# Calls processing utilities
# ───────────────────────────────────────────────────────────────────────────────
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

def _fmt_hms(seconds: pd.Series) -> pd.Series:
    return seconds.round().astype(int).map(lambda s: str(dt.timedelta(seconds=s)))

def file_md5(uploaded_file) -> str:
    pos = uploaded_file.tell()
    uploaded_file.seek(0); data = uploaded_file.read()
    uploaded_file.seek(pos if pos is not None else 0)
    return hashlib.md5(data).hexdigest()

def month_key_from_range(start: dt.date, end: dt.date) -> str:
    return f"{start.year}-{start.month:02d}"

def validate_single_month_range(start: dt.date, end: dt.date) -> Tuple[bool, str]:
    if start > end:
        return False, "Start date must be on or before End date."
    if (start.year, start.month) != (end.year, end.month):
        return False, "Please select a range within a single calendar month."
    return True, ""

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

    # Combine split incoming/outgoing if present
    def norm2(s: str) -> str:
        return re.sub(r"[^a-z0-9 ]","",re.sub(r"[\s_]+"," ",s.strip().lower()))
    incoming = [c for c in raw.columns if norm2(c) in {"incoming internal","incoming external","incoming"}]
    outgoing = [c for c in raw.columns if norm2(c) in {"outgoing internal","outgoing external","outgoing"}]
    if incoming:
        base = pd.to_numeric(df.get("Received", 0), errors="coerce").fillna(0)
        for c in incoming: base += pd.to_numeric(df.get(c, 0), errors="coerce").fillna(0)
        df["Received"] = base
    if outgoing:
        base = pd.to_numeric(df.get("Outgoing", 0), errors="coerce").fillna(0)
        for c in outgoing: base += pd.to_numeric(df.get(c, 0), errors="coerce").fillna(0)
        df["Outgoing"] = base

    missing = [c for c in REQUIRED_COLUMNS_CALLS if c not in df.columns]
    if missing:
        st.error("Calls CSV headers detected: " + ", ".join(list(raw.columns)))
        raise ValueError(f"Calls CSV is missing columns after normalization: {missing}")

    df = df[df["Name"].isin(ALLOWED_CALLS)].copy()
    df["Name"] = df["Name"].replace(RENAME_NAME_CALLS)
    df["Category"] = df["Name"].map(lambda n: CATEGORY_CALLS.get(n, "Other"))

    for c in ["Total Calls","Completed Calls","Outgoing","Received","Forwarded to Voicemail","Answered by Other","Missed"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)

    df["_avg_sec"]   = pd.to_timedelta(df["Avg Call Time"], errors="coerce").dt.total_seconds().fillna(0.0)
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
               "__avg_sec","__hold_sec","__total_sec"]].sort_values(["Category","Name"]).reset_index(drop=True)
    return out

# ───────────────────────────────────────────────────────────────────────────────
# Sticky-open expander helpers
# ───────────────────────────────────────────────────────────────────────────────
def _keep_open_flag(flag_key: str):
    st.session_state[flag_key] = True

if "exp_upload_open" not in st.session_state:
    st.session_state["exp_upload_open"] = False

# ───────────────────────────────────────────────────────────────────────────────
# Custom firm week logic and masks
# ───────────────────────────────────────────────────────────────────────────────
def _month_bounds(year: int, month: int):
    last_day = monthrange(year, month)[1]
    start = date(year, month, 1)
    end   = date(year, month, last_day)
    return start, end

def _clamp_to_today(end_date: date) -> date:
    today = date.today()
    return min(end_date, today)

def custom_weeks_for_month(year: int, month: int):
    """Week 1: 1st→first Sunday; Weeks 2..N: Mon–Sun; final week ends month-end."""
    last_day = monthrange(year, month)[1]
    start_month = date(year, month, 1)
    end_month = date(year, month, last_day)

    first_sunday = start_month + timedelta(days=(6 - start_month.weekday()))
    w1_end = min(first_sunday, end_month)
    weeks = [{"label": "Week 1", "start": start_month, "end": w1_end}]

    start = w1_end + timedelta(days=1)
    w = 2
    while start <= end_month:
        this_end = min(start + timedelta(days=6), end_month)
        weeks.append({"label": f"Week {w}", "start": start, "end": this_end})
        start = this_end + timedelta(days=1); w += 1
    return weeks

def _mask_by_range_dates(df: pd.DataFrame, date_col: str, start: date, end: date) -> pd.Series:
    if df is None or df.empty or date_col not in df.columns:
        return pd.Series([False] * (0 if df is None else len(df)))
    s = pd.to_datetime(df[date_col], errors="coerce").dt.date
    return (s >= start) & (s <= end)

# ───────────────────────────────────────────────────────────────────────────────
# Data Upload (Calls & Conversion)
# ───────────────────────────────────────────────────────────────────────────────
def upload_section(section_id: str, title: str, expander_flag: str) -> Tuple[str, object]:
    st.subheader(title)
    today = date.today()
    first_of_month = today.replace(day=1)
    next_month = (first_of_month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    last_of_month = next_month - dt.timedelta(days=1)

    c1, c2 = st.columns(2)
    start = c1.date_input("Start date", value=first_of_month,
                          key=f"{section_id}_start",
                          on_change=_keep_open_flag, args=(expander_flag,))
    end = c2.date_input("End date", value=last_of_month,
                        key=f"{section_id}_end",
                        on_change=_keep_open_flag, args=(expander_flag,))
    ok, msg = validate_single_month_range(start, end)
    if not ok: st.error(msg); st.stop()
    period_key = month_key_from_range(start, end)

    uploaded = st.file_uploader(f"Upload {title} CSV", type=["csv"],
                                key=f"{section_id}_uploader",
                                on_change=_keep_open_flag, args=(expander_flag,))
    st.divider()
    return period_key, uploaded

if "hashes_calls" not in st.session_state: st.session_state["hashes_calls"] = set()
if "hashes_conv"  not in st.session_state: st.session_state["hashes_conv"]  = set()

with st.expander("🧾 Data Upload (Calls & Conversion)", expanded=st.session_state.get("exp_upload_open", False)):
    if st.button("Allow re-upload of the same files this session"):
        st.session_state.get("hashes_calls", set()).clear()
        st.session_state.get("hashes_conv", set()).clear()
        st.caption("Ready — you can re-upload the same files now.")

    calls_period_key, calls_uploader = upload_section("zoom_calls", "Zoom Calls", "exp_upload_open")
    force_replace_calls = st.checkbox("Replace this month in Calls if it already exists",
                                      key="force_calls_replace")

    c1, c2 = st.columns(2)
    upload_start = c1.date_input("Conversion upload start date",
                                 value=date.today().replace(day=1),
                                 key="conv_upload_start",
                                 on_change=_keep_open_flag, args=("exp_upload_open",))
    upload_end   = c2.date_input("Conversion upload end date",
                                 value=date.today(),
                                 key="conv_upload_end",
                                 on_change=_keep_open_flag, args=("exp_upload_open",))
    if upload_start > upload_end: st.error("Upload start must be on or before end.")

    up_leads = st.file_uploader("Upload **Leads_PNCs**", type=["csv","xls","xlsx"],
                                key="up_leads_pncs", on_change=_keep_open_flag, args=("exp_upload_open",))
    replace_leads = st.checkbox("Replace matching records in Leads (Email+Matter ID+Stage+IC Date+DM Date)",
                                key="rep_leads")

    up_init  = st.file_uploader("Upload **Initial_Consultation**", type=["csv","xls","xlsx"],
                                key="up_initial", on_change=_keep_open_flag, args=("exp_upload_open",))
    replace_init = st.checkbox("Replace this date range in Initial_Consultation", key="rep_init")

    up_disc  = st.file_uploader("Upload **Discovery_Meeting**", type=["csv","xls","xlsx"],
                                key="up_discovery", on_change=_keep_open_flag, args=("exp_upload_open",))
    replace_disc = st.checkbox("Replace this date range in Discovery_Meeting", key="rep_disc")

    up_ncl   = st.file_uploader("Upload **New Client List**", type=["csv","xls","xlsx"],
                                key="up_ncl", on_change=_keep_open_flag, args=("exp_upload_open",))
    replace_ncl = st.checkbox("Replace this date range in New Client List", key="rep_ncl")

    def _read_any(upload):
        if upload is None: return None
        name = (upload.name or "").lower()
        if name.endswith(".csv"):
            try: df = pd.read_csv(upload)
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
        except Exception:
            upload.seek(0); df = pd.read_excel(upload)
        df.columns = [str(c).strip() for c in df.columns]
        return df

    # Calls processing
    if calls_uploader:
        CALLS_MASTER_COLS = [
            "Category","Name","Total Calls","Completed Calls","Outgoing","Received",
            "Forwarded to Voicemail","Answered by Other","Missed",
            "Avg Call Time","Total Call Time","Total Hold Time","Month-Year"
        ]
        try:
            fhash = file_md5(calls_uploader)
            month_exists = False
            try:
                existing = _read_ws_by_name("CALLS")
                if isinstance(existing, pd.DataFrame) and not existing.empty:
                    month_exists = existing["Month-Year"].astype(str).eq(calls_period_key).any()
            except Exception as e:
                log(f"Month existence check failed: {e}")

            if fhash in st.session_state["hashes_calls"] and month_exists and not force_replace_calls:
                st.caption("Calls: same file and month already present — upload skipped.")
                log("Calls upload skipped by session dedupe guard.")
            else:
                raw = pd.read_csv(calls_uploader)
                processed = process_calls_csv(raw, calls_period_key)
                processed_clean = processed[CALLS_MASTER_COLS].copy()

                if GSHEET is None:
                    st.warning("Master store not configured; Calls will not persist.")
                    df_calls_master = processed_clean.copy()
                else:
                    current = _read_ws_by_name("CALLS")
                    if force_replace_calls and month_exists and not current.empty:
                        current = current.loc[current["Month-Year"].astype(str).str.strip() != calls_period_key].copy()
                    combined = (pd.concat([current, processed_clean], ignore_index=True)
                                if not current.empty else processed_clean.copy())
                    key = (combined["Month-Year"].astype(str).str.strip() + "|" +
                           combined["Name"].astype(str).str.strip() + "|" +
                           combined["Category"].astype(str).str.strip())
                    combined = combined.loc[~key.duplicated(keep="last")].copy()
                    _write_ws_by_name("CALLS", combined)
                    st.success(f"Calls: upserted {len(processed_clean)} row(s).")
                    df_calls_master = combined.copy()
                st.session_state["hashes_calls"].add(fhash)
        except Exception as e:
            st.error("Could not parse Calls CSV."); st.exception(e)

    # Conversion processing uploads
    uploads = {"LEADS": (up_leads, replace_leads),
               "INIT":  (up_init,  replace_init),
               "DISC":  (up_disc,  replace_disc),
               "NCL":   (up_ncl,   replace_ncl)}

    def _mask_by_range(df: pd.DataFrame, col: str) -> pd.Series:
        s = pd.to_datetime(df[col], errors="coerce")
        return (s.dt.date >= upload_start) & (s.dt.date <= upload_end) & s.notna()

    for key_name, (upl, want_replace) in uploads.items():
        if not upl: continue
        try:
            fhash = file_md5(upl)
            if fhash in st.session_state["hashes_conv"]:
                st.caption(f"{key_name}: duplicate file — ignored.")
                log(f"{key_name} skipped by session dedupe guard.")
                continue

            df_up = _read_any(upl)
            if df_up is None or df_up.empty:
                st.caption(f"{key_name}: file appears empty."); continue

            if key_name == "LEADS":
                df_up["__batch_start"] = pd.to_datetime(upload_start)
                df_up["__batch_end"]   = pd.to_datetime(upload_end)

            if key_name == "NCL" and "Retained with Consult (Y/N)" in df_up.columns \
               and "Retained With Consult (Y/N)" not in df_up.columns:
                df_up = df_up.rename(columns={"Retained with Consult (Y/N)":"Retained With Consult (Y/N)"})

            current = _read_ws_by_name(key_name)

            if want_replace and not current.empty:
                if key_name == "LEADS":
                    key_in = (
                        df_up.get("Email","").astype(str).str.strip() + "|" +
                        df_up.get("Matter ID","").astype(str).str.strip() + "|" +
                        df_up.get("Stage","").astype(str).str.strip() + "|" +
                        df_up.get("Initial Consultation With Pji Law","").astype(str) + "|" +
                        df_up.get("Discovery Meeting With Pji Law","").astype(str)
                    )
                    incoming_keys = set(key_in.tolist())
                    key_cur = (
                        current.get("Email","").astype(str).str.strip() + "|" +
                        current.get("Matter ID","").astype(str).str.strip() + "|" +
                        current.get("Stage","").astype(str).str.strip() + "|" +
                        current.get("Initial Consultation With Pji Law","").astype(str) + "|" +
                        current.get("Discovery Meeting With Pji Law","").astype(str)
                    )
                    mask_keep = ~key_cur.isin(incoming_keys)
                    current = current.loc[mask_keep].copy()
                elif key_name == "INIT":
                    col = "Initial Consultation With Pji Law"
                    if col in current.columns:
                        drop_mask = _mask_by_range(current, col)
                        current = current.loc[~drop_mask].copy()
                elif key_name == "DISC":
                    col = "Discovery Meeting With Pji Law"
                    if col in current.columns:
                        drop_mask = _mask_by_range(current, col)
                        current = current.loc[~drop_mask].copy()
                elif key_name == "NCL":
                    col = "Date we had BOTH the signed CLA and full payment"
                    if col in current.columns:
                        drop_mask = _mask_by_range(current, col)
                        current = current.loc[~drop_mask].copy()

            combined = pd.concat([current, df_up], ignore_index=True) if not current.empty else df_up.copy()

            # Dedupe by dataset keys
            if key_name == "LEADS":
                k = (combined.get("Email","").astype(str).str.strip() + "|" +
                     combined.get("Matter ID","").astype(str).str.strip() + "|" +
                     combined.get("Stage","").astype(str).str.strip() + "|" +
                     combined.get("Initial Consultation With Pji Law","").astype(str) + "|" +
                     combined.get("Discovery Meeting With Pji Law","").astype(str))
            elif key_name == "INIT":
                k = (combined.get("Email","").astype(str).str.strip() + "|" +
                     combined.get("Matter ID","").astype(str).str.strip() + "|" +
                     combined.get("Initial Consultation With Pji Law","").astype(str) + "|" +
                     combined.get("Sub Status","").astype(str).str.strip())
            elif key_name == "DISC":
                k = (combined.get("Email","").astype(str).str.strip() + "|" +
                     combined.get("Matter ID","").astype(str).str.strip() + "|" +
                     combined.get("Discovery Meeting With Pji Law","").astype(str) + "|" +
                     combined.get("Sub Status","").astype(str).str.strip())
            else:  # NCL
                k = (combined.get("Client Name","").astype(str).str.strip() + "|" +
                     combined.get("Matter Number/Link","").astype(str).str.strip() + "|" +
                     combined.get("Date we had BOTH the signed CLA and full payment","").astype(str) + "|" +
                     combined.get("Retained With Consult (Y/N)","").astype(str).str.strip())

            combined = combined.loc[~k.duplicated(keep="last")].copy()
            _write_ws_by_name(key_name, combined)
            st.success(f"{key_name}: upserted {len(df_up)} row(s).")
            st.session_state["hashes_conv"].add(fhash)
        except Exception as e:
            st.error(f"{key_name}: upload failed."); st.exception(e)

# Load masters
df_calls = _read_ws_by_name("CALLS") if GSHEET is not None else pd.DataFrame()
df_leads = _read_ws_by_name("LEADS") if GSHEET is not None else pd.DataFrame()
df_init  = _read_ws_by_name("INIT")  if GSHEET is not None else pd.DataFrame()
df_disc  = _read_ws_by_name("DISC")  if GSHEET is not None else pd.DataFrame()
df_ncl   = _read_ws_by_name("NCL")   if GSHEET is not None else pd.DataFrame()

if not df_calls.empty:
    df_calls["__avg_sec"]   = pd.to_timedelta(df_calls["Avg Call Time"], errors="coerce").dt.total_seconds().fillna(0.0)
    df_calls["__total_sec"] = pd.to_timedelta(df_calls["Total Call Time"], errors="coerce").dt.total_seconds().fillna(0.0)
    df_calls["__hold_sec"]  = pd.to_timedelta(df_calls["Total Hold Time"], errors="coerce").dt.total_seconds().fillna(0.0)

# ───────────────────────────────────────────────────────────────────────────────
# Zoom Call Reports
# ───────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.header("Zoom Call Reports")

st.subheader("Filters — Calls")
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
year_options = ["All"] + years if years else ["All"]
sel_year = c1.selectbox("Year", year_options, index=(year_options.index(latest_year) if latest_year in year_options else 0))

def months_for_year(year_sel: str):
    if year_sel == "All": return sorted({m.split("-")[1] for m in all_months})
    return sorted({m.split("-")[1] for m in all_months if m.startswith(year_sel)})

mnums = months_for_year(sel_year)
mnames = [month_num_to_name(m) for m in mnums]
month_options = ["All"] + mnames if mnames else ["All"]
sel_month_name = c2.selectbox("Month", month_options,
                              index=(month_options.index(latest_mname) if latest_mname in month_options else 0))

cat_choices = ["All"] + (sorted(df_calls["Category"].unique().tolist()) if not df_calls.empty else [])
sel_cat = c3.selectbox("Category", cat_choices, index=0)
base = df_calls if sel_cat == "All" else df_calls[df_calls["Category"] == sel_cat]
name_choices = ["All"] + (sorted(base["Name"].unique().tolist()) if not base.empty else [])
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

st.subheader("Calls — Results")
calls_display_cols = [
    "Category","Name","Total Calls","Completed Calls","Outgoing","Received",
    "Forwarded to Voicemail","Answered by Other","Missed",
    "Avg Call Time","Total Call Time","Total Hold Time","Month-Year"
]
if not view_calls.empty:
    st.dataframe(view_calls[calls_display_cols], hide_index=True, use_container_width=True)
    csv_buf = io.StringIO()
    view_calls[calls_display_cols].to_csv(csv_buf, index=False)
    st.download_button("Download filtered Calls CSV", csv_buf.getvalue(),
                       file_name="call_report_filtered.csv", type="primary")
else:
    st.info("No rows match the current Calls filters.")

# ───────────────────────────────────────────────────────────────────────────────
# Conversion Report
# ───────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.header("Conversion Report")

row = st.columns([2, 1, 1])  # Period (wide), Year, Month

months_map_names = {
    1:"January",2:"February",3:"March",4:"April",5:"May",6:"June",
    7:"July",8:"August",9:"September",10:"October",11:"November",12:"December"
}
month_nums = list(months_map_names.keys())

def _years_from(*dfs_cols):
    ys = set()
    for df, col in dfs_cols:
        if df is not None and not df.empty and col in df.columns:
            ys |= set(pd.to_datetime(df[col], errors="coerce").dt.year.dropna().astype(int))
    return ys

years_detected = _years_from(
    (df_ncl,  "Date we had BOTH the signed CLA and full payment"),
    (df_init, "Initial Consultation With Pji Law"),
    (df_disc, "Discovery Meeting With Pji Law"),
)
years_conv = sorted(years_detected) if years_detected else [date.today().year]

with row[0]:
    period_mode = st.radio(
        "Period",
        ["Month to date", "Full month", "Year to date", "Week of month", "Custom range"],
        horizontal=True,
    )
with row[1]:
    sel_year_conv = st.selectbox("Year", years_conv, index=len(years_conv)-1)
with row[2]:
    sel_month_num = st.selectbox(
        "Month",
        month_nums,
        index=date.today().month-1,
        format_func=lambda m: months_map_names[m]
    )

week_defs = None
sel_week_idx = 0
if period_mode == "Week of month":
    week_defs = custom_weeks_for_month(sel_year_conv, sel_month_num)
    def _wk_label(i):
        wk = week_defs[i]; sd, ed = wk["start"], wk["end"]
        return f'{wk["label"]} ({sd.day}–{ed.day} {ed.strftime("%b")})'
    sel_week_idx = st.selectbox("Week of month",
                                options=list(range(len(week_defs))),
                                index=0, format_func=_wk_label)

cust_cols = st.columns(2)
custom_start = custom_end = None
if period_mode == "Custom range":
    custom_start = cust_cols[0].date_input("Start date", value=date.today().replace(day=1))
    custom_end   = cust_cols[1].date_input("End date",   value=date.today())
    if custom_start > custom_end:
        st.error("Start date must be on or before End date."); st.stop()

# Resolve period → (start_date, end_date)
if period_mode == "Month to date":
    mstart, mend = _month_bounds(sel_year_conv, sel_month_num)
    if date.today().month == sel_month_num and date.today().year == sel_year_conv:
        start_date, end_date = mstart, _clamp_to_today(mend)
    else:
        start_date, end_date = mstart, mend
elif period_mode == "Full month":
    start_date, end_date = _month_bounds(sel_year_conv, sel_month_num)
elif period_mode == "Year to date":
    y_start = date(sel_year_conv, 1, 1)
    y_end   = _clamp_to_today(date(sel_year_conv, 12, 31)) if sel_year_conv == date.today().year else date(sel_year_conv, 12, 31)
    start_date, end_date = y_start, y_end
elif period_mode == "Week of month":
    wk = week_defs[sel_week_idx]
    start_date, end_date = wk["start"], wk["end"]
else:
    start_date, end_date = custom_start, custom_end

st.caption(f"Showing Conversion metrics for **{start_date:%-d %b %Y} → {end_date:%-d %b %Y}**")

# Filtered slices (date-in-range only; column names are fixed by your files)
init_mask = _mask_by_range_dates(df_init, "Initial Consultation With Pji Law", start_date, end_date)
disc_mask = _mask_by_range_dates(df_disc, "Discovery Meeting With Pji Law", start_date, end_date)
ncl_mask  = _mask_by_range_dates(df_ncl,  "Date we had BOTH the signed CLA and full payment", start_date, end_date)

init_in = df_init.loc[init_mask].copy() if not df_init.empty else pd.DataFrame()
disc_in = df_disc.loc[disc_mask].copy() if not df_disc.empty else pd.DataFrame()
ncl_in  = df_ncl.loc[ncl_mask].copy()  if not df_ncl.empty  else pd.DataFrame()

# Leads & PNCs — batch period overlap (unchanged)
if not df_leads.empty and {"__batch_start","__batch_end"} <= set(df_leads.columns):
    bs = pd.to_datetime(df_leads["__batch_start"], errors="coerce")
    be = pd.to_datetime(df_leads["__batch_end"],   errors="coerce")
    start_ts, end_ts = pd.Timestamp(start_date), pd.Timestamp(end_date)
    leads_in_range = (bs <= end_ts) & (be >= start_ts)
else:
    leads_in_range = pd.Series(False, index=df_leads.index)

EXCLUDED_PNC_STAGES = {
    "Marketing/Scam/Spam (Non-Lead)","Referred Out","No Stage","New Lead",
    "No Follow Up (No Marketing/Communication)","No Follow Up (Receives Marketing/Communication)",
    "Anastasia E","Aneesah S.","Azariah P.","Earl M.","Faeryal S.","Kaithlyn M.",
    "Micayla S.","Nathanial B.","Rialet v H.","Sihle G.","Thabang T.","Tiffany P",
    ":Chloe L:","Nobuhle M."
}

row1 = int(
    df_leads.loc[
        leads_in_range &
        (df_leads["Stage"].astype(str).str.strip() != "Marketing/Scam/Spam (Non-Lead)")
    ].shape[0]
) if not df_leads.empty and "Stage" in df_leads.columns else 0

row2 = int(
    df_leads.loc[
        leads_in_range &
        (~df_leads["Stage"].astype(str).str.strip().isin(EXCLUDED_PNC_STAGES))
    ].shape[0]
) if not df_leads.empty and "Stage" in df_leads.columns else 0

# Helper to find a column by name (case-insensitive)
def _find_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    if df is None or df.empty: return None
    cols = {c.lower().strip(): c for c in df.columns}
    for cand in candidates:
        k = cand.lower().strip()
        if k in cols: return cols[k]
    return None

# === SCHEDULED & MET (exact to your rules) ===
def _scheduled_and_met(df: pd.DataFrame) -> Tuple[int, int]:
    """
    Scheduled = rows in-range (we already passed an in-range slice) MINUS only Sub Status == 'Follow Up'
    Met       = Scheduled MINUS rows where Column I (Reason for Rescheduling) is non-blank
    """
    if df is None or df.empty:
        return 0, 0

    # Exclude Follow Up (Column G = 'Sub Status')
    sub_col = _find_col(df, ["Sub Status"])
    in_scope = df.copy()
    if sub_col and sub_col in in_scope.columns:
        in_scope = in_scope.loc[~in_scope[sub_col].astype(str).str.strip().str.lower().eq("follow up")].copy()

    scheduled = int(len(in_scope))

    # Column I (Reason for Rescheduling) — treat real blanks, NaN, and whitespace as blank
    reason_col = _find_col(in_scope, ["Reason for Rescheduling"]) or (in_scope.columns[8] if in_scope.shape[1] >= 9 else None)
    if reason_col:
        vals = in_scope[reason_col]
        non_blank = vals.notna() & vals.astype(str).str.strip().ne("")
    else:
        non_blank = pd.Series(False, index=in_scope.index)

    met = int((~non_blank).sum())
    return scheduled, met

# Compute scheduled/met for IC and DM
ic_sched, ic_met = _scheduled_and_met(init_in)
dm_sched, dm_met = _scheduled_and_met(disc_in)

# NCL retained split within range (unchanged)
ncl_flag_col = None
for candidate in ["Retained With Consult (Y/N)", "Retained with Consult (Y/N)"]:
    if candidate in ncl_in.columns:
        ncl_flag_col = candidate; break

if ncl_flag_col:
    flag_in = ncl_in[ncl_flag_col].astype(str).str.strip().str.upper()
    row3 = int((flag_in == "N").sum())           # retained without consult
    row8 = int((flag_in != "N").sum())           # retained after consult
else:
    row3 = 0
    row8 = int(ncl_in.shape[0])

row10 = int(ncl_in.shape[0])                     # total retained
row4  = int(ic_sched + dm_sched)                 # scheduled consultations
row6  = int(ic_met   + dm_met)                   # met (showed) consultations

def _pct(numer, denom): return 0 if (denom is None or denom == 0) else round((numer/denom)*100)

row5  = _pct(row4, (row2 - row3))
row7  = _pct(row6, row4)
row9  = _pct(row8, row4)
row11 = _pct(row10, row2)

# Static HTML KPI table
def _html_escape(s: str) -> str:
    return (str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"))

kpi_rows = [
    ("# of Leads", row1),
    ("# of PNCs", row2),
    ("PNCs who retained without consultation", row3),
    ("PNCs who scheduled consultation", row4),
    ("% of remaining PNCs who scheduled consult", f"{row5}%"),
    ("# of PNCs who showed up for consultation", row6),
    ("% of PNCs who scheduled consult showed up", f"{row7}%"),
    ("PNCs who retained after scheduled consult", row8),
    ("% of PNCs who retained after consult", f"{row9}%"),
    ("# of Total PNCs who retained", row10),
    ("% of total PNCs who retained", f"{row11}%"),
]
table_rows = "\n".join(
    f"<tr><td>{_html_escape(k)}</td><td style='text-align:right'>{_html_escape(v)}</td></tr>"
    for k, v in kpi_rows
)
html_table = """
<style>
.kpi-table { width: 100%; border-collapse: collapse; font-size: 0.95rem; }
.kpi-table th, .kpi-table td { border: 1px solid #eee; padding: 10px 12px; }
.kpi-table th { background: #fafafa; text-align: left; font-weight: 600; }
</style>
<table class="kpi-table">
  <thead><tr><th>Metric</th><th>Value</th></tr></thead>
  <tbody>
    """ + table_rows + """
  </tbody>
</table>
"""
st.markdown(html_table, unsafe_allow_html=True)

# ───────────────────────────────────────────────────────────────────────────────
# Practice Area
# ───────────────────────────────────────────────────────────────────────────────
st.subheader("Practice Area")

PRACTICE_AREAS = {
    "Estate Planning": ["Connor Watkins", "Jennifer Fox", "Rebecca Megel"],
    "Estate Administration": [
        "Adam Hill", "Elias Kerby", "Elizabeth Ross", "Garrett Kizer",
        "Kyle Grabulis", "Sarah Kravetz",
        # NEW hires:
        "Jamie Kliem", "Carter McClain",
    ],
    "Civil Litigation": ["Andrew Suddarth", "William Bang", "Bret Giaimo", "Hannah Supernor", "Laura Kouremetis", "Lukios Stefan", "William Gogoel"],
    "Business transactional": ["Kevin Jaros"],
}

OTHER_ATTORNEYS = ["Robert Brown", "Justine Sennott", "Paul Abraham"]

# ───────────────────────────────────────────────────────────────────────────────
# Practice Area (robust counting + safe report build)
# ───────────────────────────────────────────────────────────────────────────────

# Friendly display overrides (define if missing)
if "DISPLAY_NAME_OVERRIDES" not in globals():
    DISPLAY_NAME_OVERRIDES = {
        "Elias Kerby": "Eli Kerby",
        "William Bang": "Billy Bang",
        "William Gogoel": "Will Gogoel",
        "Andrew Suddarth": "Andy Suddarth",
    }

def _display_safe(n: str) -> str:
    return DISPLAY_NAME_OVERRIDES.get(n, n)

# Canonical roster in a stable order
CANON = list(dict.fromkeys(sum(PRACTICE_AREAS.values(), []) + OTHER_ATTORNEYS))

# ---------- Fuzzy column matching + resilient datetime parsing ----------
import re

def _col_fuzzy(df: pd.DataFrame, patterns: list[str]) -> str | None:
    if df is None or df.empty:
        return None
    def norm(s: str) -> str:
        s = str(s).lower().strip()
        s = re.sub(r"[\s_]+", " ", s)
        s = re.sub(r"[^a-z0-9 ]", "", s)
        return s
    tgt = [norm(p) for p in patterns if p and str(p).strip()]
    cols_norm = {c: norm(c) for c in df.columns}
    for c, n in cols_norm.items():
        if all(tok in n for tok in tgt):
            return c
    return None

_TZ_RE = re.compile(r"\s+(ET|EDT|EST|CT|CDT|CST|MT|MDT|MST|PT|PDT)\b", flags=re.I)
def _clean_dt_text(x: str) -> str:
    if x is None: return ""
    s = str(x).strip()
    s = re.sub(r"\s+at\s+", " ", s, flags=re.I)  # " at " -> space
    s = _TZ_RE.sub("", s)                        # drop timezone token
    return s

def _to_ts(series: pd.Series) -> pd.Series:
    s = series.astype(str).map(_clean_dt_text)
    x = pd.to_datetime(s, errors="coerce", infer_datetime_format=True)
    try: x = x.dt.tz_localize(None)
    except Exception: pass
    return x

def _between_inclusive(series: pd.Series, sd, ed) -> pd.Series:
    x = _to_ts(series)
    return (x >= pd.Timestamp(sd)) & (x <= pd.Timestamp(ed))

def _is_blank(series: pd.Series) -> pd.Series:
    s = series.astype(str)
    return series.isna() | (s.str.strip() == "") | (s.str.lower().isin(["nan","none","na","null"]))

# NCL initials → full names (includes JK/CM and Other)
INITIALS_TO_ATTORNEY = {
    "CW":"Connor Watkins","JF":"Jennifer Fox","RM":"Rebecca Megel",
    "AH":"Adam Hill","EK":"Elias Kerby","ER":"Elizabeth Ross",
    "GK":"Garrett Kizer","KG":"Kyle Grabulis","SK":"Sarah Kravetz",
    "AS":"Andrew Suddarth","WB":"William Bang","BG":"Bret Giaimo",
    "HS":"Hannah Supernor","LK":"Laura Kouremetis","LS":"Lukios Stefan",
    "WG":"William Gogoel","KJ":"Kevin Jaros",
    "JK":"Jamie Kliem","CM":"Carter McClain",
    "RB":"Robert Brown","JS":"Justine Sennott","PA":"Paul Abraham",
}

# ---------- Robust counters (operate on RAW sheets; do their own date filtering) ----------
def _met_counts_from_ic_dm_fuzzy(ic_df: pd.DataFrame, dm_df: pd.DataFrame, sd, ed) -> dict:
    """PNCs who met with {Attorney} from IC+DM: fuzzy headers, include in-range; exclude Follow Up + non-blank Reason."""
    buckets = []

    # IC
    if isinstance(ic_df, pd.DataFrame) and not ic_df.empty:
        att  = _col_fuzzy(ic_df, ["lead","attorney"])
        date = _col_fuzzy(ic_df, ["initial","consultation"])
        sub  = _col_fuzzy(ic_df, ["sub","status"])
        rsn  = _col_fuzzy(ic_df, ["reason","resched"])
        if att and date:
            t = ic_df.copy()
            m = _between_inclusive(t[date], sd, ed)
            if sub is not None: m &= ~t[sub].astype(str).str.strip().str.lower().eq("follow up")
            if rsn is not None: m &= _is_blank(t[rsn])
            buckets.append(t.loc[m, att].astype(str).str.strip())

    # DM
    if isinstance(dm_df, pd.DataFrame) and not dm_df.empty:
        att  = _col_fuzzy(dm_df, ["lead","attorney"])
        date = _col_fuzzy(dm_df, ["discovery","meeting"])
        sub  = _col_fuzzy(dm_df, ["sub","status"])
        rsn  = _col_fuzzy(dm_df, ["reason","resched"])
        if att and date:
            t = dm_df.copy()
            m = _between_inclusive(t[date], sd, ed)
            if sub is not None: m &= ~t[sub].astype(str).str.strip().str.lower().eq("follow up")
            if rsn is not None: m &= _is_blank(t[rsn])
            buckets.append(t.loc[m, att].astype(str).str.strip())

    if not buckets:
        return {}
    met_series = pd.concat(buckets, ignore_index=True)
    met_series = met_series[met_series.ne("")]
    return met_series.value_counts(dropna=False).to_dict()

def _retained_counts_from_ncl_fuzzy(ncl_df: pd.DataFrame, sd, ed) -> dict:
    """PNCs who met & retained from NCL: fuzzy headers; F != 'N'; G in range; initials → full names (unknown→Other)."""
    if not isinstance(ncl_df, pd.DataFrame) or ncl_df.empty:
        return {}
    init_col = _col_fuzzy(ncl_df, ["responsible","attorney"])
    flag_col = _col_fuzzy(ncl_df, ["retained","consult"])
    date_col = _col_fuzzy(ncl_df, ["date","signed","payment"])
    if not (init_col and flag_col and date_col):
        return {}

    t = ncl_df.copy()
    m = _between_inclusive(t[date_col], sd, ed)
    m &= t[flag_col].astype(str).str.strip().str.upper().ne("N")

    mapped = (
        t.loc[m, init_col]
         .astype(str).str.upper()
         .map(lambda s: INITIALS_TO_ATTORNEY.get(re.sub(r"[^A-Z]", "", s), "Other"))
    )
    return mapped.value_counts(dropna=False).to_dict()

# ---------- Build counts + report safely ----------
try:
    _met_counts = _met_counts_from_ic_dm_fuzzy(df_init, df_disc, start_date, end_date)
    _ret_counts = _retained_counts_from_ncl_fuzzy(df_ncl, start_date, end_date)

    met_by_attorney      = {name: int(_met_counts.get(name, 0)) for name in CANON}
    retained_by_attorney = {name: int(_ret_counts.get(name, 0)) for name in CANON}

    report = pd.DataFrame({"Attorney": CANON})
    report["PNCs who met"] = report["Attorney"].map(lambda a: met_by_attorney.get(a, 0))
    report["PNCs who met and retained"] = report["Attorney"].map(lambda a: retained_by_attorney.get(a, 0))
    report["Practice Area"] = report["Attorney"].map(_practice_for)
    report["Attorney_Display"] = report["Attorney"].map(_display_safe)

except Exception as e:
    st.error("Practice Area: failed to build counts/report.")
    st.exception(e)
    # ensure 'report' exists to avoid NameError downstream
    report = pd.DataFrame({"Attorney": CANON, "PNCs who met": 0, "PNCs who met and retained": 0,
                           "Practice Area": [ _practice_for(a) for a in CANON ],
                           "Attorney_Display": [ _display_safe(a) for a in CANON ]})

# ---------- Render (percent = retained / met) ----------
def _render_three_row_card(title_name: str, met: int, kept: int):
    met_i = int(met); kept_i = int(kept)
    pct = 0.0 if met_i == 0 else round((kept_i / met_i) * 100.0, 2)
    rows = [
        (f"PNCs who met with {title_name}", f"{met_i}"),
        (f"PNCs who met with {title_name} and retained", f"{kept_i}"),
        (f"% of PNCs who met with {title_name} and retained", f"{pct:.2f}%"),
    ]
    trs = "\n".join(
        f"<tr><td>{_html_escape(k)}</td><td style='text-align:right'>{_html_escape(v)}</td></tr>"
        for k, v in rows
    )
    html = """
<style>
.mini-kpi { width: 100%; border-collapse: collapse; font-size: 0.95rem; }
.mini-kpi th, .mini-kpi td { border: 1px solid #eee; padding: 8px 10px; }
.mini-kpi th { background: #fafafa; text-align: left; font-weight: 600; }
</style>
<table class="mini-kpi">
  <thead><tr><th>Metric</th><th>Value</th></tr></thead>
  <tbody>""" + trs + """</tbody>
</table>
"""
    st.markdown(html, unsafe_allow_html=True)

for pa in ["Estate Planning","Estate Administration","Civil Litigation","Business transactional","Other"]:
    sub = report.loc[report["Practice Area"] == pa].copy()
    met_sum  = int(sub["PNCs who met"].sum())
    kept_sum = int(sub["PNCs who met and retained"].sum())

    with st.expander(pa, expanded=False):
        attys = ["ALL"] + sub["Attorney_Display"].tolist()
        pick = st.selectbox(f"{pa} — choose attorney", attys, key=f"pa_pick_{pa.replace(' ','_')}")
        if pick == "ALL":
            _render_three_row_card("ALL", met_sum, kept_sum)
        else:
            rowx = sub.loc[sub["Attorney_Display"] == pick].iloc[0]
            _render_three_row_card(
                pick,
                int(rowx["PNCs who met"]),
                int(rowx["PNCs who met and retained"]),
            )


# ───────────────────────────────────────────────────────────────────────────────
# Debug details
# ───────────────────────────────────────────────────────────────────────────────
with st.expander("Debug details (for reconciliation)", expanded=False):
    if not df_leads.empty and "Stage" in df_leads.columns and len(leads_in_range) == len(df_leads):
        st.write("Leads_PNCs — Stage (in selected period)",
                 df_leads.loc[leads_in_range, "Stage"].value_counts(dropna=False))
    if not init_in.empty:
        st.write("Initial_Consultation — in range", init_in.shape)
    if not disc_in.empty:
        st.write("Discovery_Meeting — in range", disc_in.shape)
    if ncl_flag_col:
        st.write("New Client List — Retained split (in range)", ncl_in[ncl_flag_col].value_counts(dropna=False))
    st.write(
        f"Computed: Leads={row1}, PNCs={row2}, "
        f"Retained w/out consult={row3}, Scheduled={row4} ({row5}%), "
        f"Showed={row6} ({row7}%), Retained after consult={row8} ({row9}%), "
        f"Total retained={row10} ({row11}%)"
    )

# ───────────────────────────────────────────────────────────────────────────────
# Quiet logs (optional)
# ───────────────────────────────────────────────────────────────────────────────
with st.expander("ℹ️ Logs (tech details)", expanded=False):
    if st.session_state["logs"]:
        for line in st.session_state["logs"]:
            st.code(line)
    else:
        st.caption("No technical logs this session.")
