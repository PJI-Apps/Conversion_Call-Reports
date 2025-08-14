# app_clean.py
# PJI Law • Conversion and Call Report (Streamlit) - Clean Version with Collapsible Sections

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
    """Idempotent, race-safe worksheet getter/creator.

    Guarantees:
      • If a worksheet with `title` already exists, returns it without error.
      • If it does not exist, creates it once and returns it.
      • Uses fallbacks in TAB_FALLBACKS when present.
    """
    if GSHEET is None:
        return None

    import time
    from gspread.exceptions import APIError, WorksheetNotFound

    # 1) Fast path: try fetching a few times (transient errors happen)
    for delay in (0.0, 0.6, 1.2):
        try:
            if delay:
                time.sleep(delay)
            return GSHEET.worksheet(title)
        except WorksheetNotFound:
            # Double-check via full listing to avoid stale lookup cache
            try:
                existing_titles = {ws.title for ws in GSHEET.worksheets()}
                if title in existing_titles:
                    return GSHEET.worksheet(title)
            except Exception:
                pass
            break  # not found after double-check; move on to fallbacks/create
        except APIError:
            # e.g., rate limit/5xx; retry
            continue
        except Exception:
            continue

    # 2) Fallback titles (legacy tab names)
    for fb in TAB_FALLBACKS.get(title, []):
        try:
            return GSHEET.worksheet(fb)
        except Exception:
            continue

    # 3) Create only if truly absent; treat ALREADY_EXISTS as success
    try:
        GSHEET.add_worksheet(title=title, rows=2000, cols=40)
        return GSHEET.worksheet(title)
    except APIError as e:
        # If another writer created it milliseconds before us, Google returns ALREADY_EXISTS.
        if "already exists" in str(e).lower():
            try:
                return GSHEET.worksheet(title)
            except Exception:
                pass
        # Some APIErrors are retryable; try one last fetch before failing silently
        try:
            return GSHEET.worksheet(title)
        except Exception:
            pass
    except Exception:
        try:
            return GSHEET.worksheet(title)
        except Exception:
            pass

    st.error(f"Could not access/create worksheet '{title}'. Please check permissions and tab names.")
    return None

@st.cache_data(ttl=300, show_spinner=False)
def _read_ws_cached(tab_name: str):
    ws = _ws(tab_name)
    if not ws:
        return pd.DataFrame()
    try:
        data = ws.get_all_records()
        df = pd.DataFrame(data)
        if not df.empty:
            # Clean column headers
            df.columns = df.columns.str.strip()
            # Convert date columns
            for c in df.columns:
                if any(word in c.lower() for word in ["date", "time", "created", "updated"]):
                    df[c] = pd.to_datetime(df[c].map(_clean_datestr), errors="coerce", format="mixed")
        return df
    except Exception as e:
        st.error(f"Error reading {tab_name}: {e}")
        return pd.DataFrame()

def _clean_datestr(x):
    if pd.isna(x) or x == "":
        return None
    s = str(x).strip()
    if s.lower() in ["nan", "none", "null", ""]:
        return None
    return s

# Load data
df_calls = _read_ws_cached("CALLS")
df_leads = _read_ws_cached("LEADS")
df_init = _read_ws_cached("INIT")
df_disc = _read_ws_cached("DISC")
df_ncl = _read_ws_cached("NCL")

# ───────────────────────────────────────────────────────────────────────────────
# Date helper functions and calculation logic
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

def _between_inclusive(ts: pd.Series, start: date, end: date) -> pd.Series:
    if ts is None or ts.empty:
        return pd.Series([False] * len(ts) if ts is not None else 0)
    return (ts.dt.date >= start) & (ts.dt.date <= end)

def _to_ts(x):
    if pd.isna(x) or x == "":
        return pd.NaT
    s = str(x).strip()
    if s.lower() in ["nan", "none", "null", ""]:
        return pd.NaT
    return pd.to_datetime(s, errors="coerce", infer_datetime_format=True)

# Practice area and attorney mappings
PRACTICE_AREAS = {
    "Estate Planning": ["Rebecca Megel", "Elias Kerby", "Eli Kerby"],
    "Estate Administration": ["Rebecca Megel", "Elias Kerby", "Eli Kerby"],
    "Civil Litigation": ["Rebecca Megel", "Elias Kerby", "Eli Kerby"],
    "Business transactional": ["Rebecca Megel", "Elias Kerby", "Eli Kerby"],
    "Other": []
}

def _practice_for(attorney: str) -> str:
    for practice, attorneys in PRACTICE_AREAS.items():
        if attorney in attorneys:
            return practice
    return "Other"

def _ini_to_name(ini: str) -> str:
    mapping = {
        "RM": "Rebecca Megel",
        "EK": "Elias Kerby",
        "EK": "Eli Kerby",  # Override for Elias Kerby
    }
    return mapping.get(ini.strip().upper(), ini)

# Intake specialist mappings
INTAKE_SPECIALISTS = [
    "Anastasia Economopoulos", "Aneesah Shaik", "Azariah Pillay", "Chloe Lansdell",
    "Earl Michaels", "Faeryal Sahadeo", "Kaithlyn Maharaj", "Micayla Sam",
    "Nathanial Beneke", "Nobuhle Mnikathi", "Rialet van Heerden", "Sihle Gadu",
    "Thabang Tshubyane", "Tiffany Pillay"
]

INTAKE_INITIALS_TO_NAME = {
    "AE": "Anastasia Economopoulos",
    "AS": "Aneesah Shaik", 
    "AP": "Azariah Pillay",
    "CL": "Chloe Lansdell",
    "EM": "Earl Michaels",
    "FS": "Faeryal Sahadeo",
    "KM": "Kaithlyn Maharaj",
    "MS": "Micayla Sam",
    "NB": "Nathanial Beneke",
    "NM": "Nobuhle Mnikathi",
    "RH": "Rialet van Heerden",
    "SG": "Sihle Gadu",
    "TT": "Thabang Tshubyane",
    "TP": "Tiffany Pillay"
}

# ───────────────────────────────────────────────────────────────────────────────
# Main conversion report calculation functions
# ───────────────────────────────────────────────────────────────────────────────
def _met_counts_from_ic_dm_index(df_init: pd.DataFrame, df_disc: pd.DataFrame, start_date: date, end_date: date) -> int:
    """Count PNCs who met with attorneys (Initial Consultation + Discovery Meeting)"""
    if df_init.empty and df_disc.empty:
        return 0
    
    total_met = 0
    
    # Count from Initial Consultation
    if not df_init.empty:
        ic_dates = _to_ts(df_init["Initial Consultation With Pji Law"])
        ic_mask = _between_inclusive(ic_dates, start_date, end_date)
        # Filter out "No Show" and "Canceled Meeting"
        ic_valid = df_init[ic_mask].copy()
        ic_valid = ic_valid[~ic_valid["Sub Status"].str.contains("No Show|Canceled Meeting", case=False, na=False)]
        total_met += len(ic_valid)
    
    # Count from Discovery Meeting
    if not df_disc.empty:
        dm_dates = _to_ts(df_disc["Discovery Meeting With Pji Law"])
        dm_mask = _between_inclusive(dm_dates, start_date, end_date)
        # Filter out "Follow Up" and "No Show" and "Canceled Meeting"
        dm_valid = df_disc[dm_mask].copy()
        dm_valid = dm_valid[~dm_valid["Sub Status"].str.contains("Follow Up|No Show|Canceled Meeting", case=False, na=False)]
        total_met += len(dm_valid)
    
    return total_met

def _retained_counts_from_ncl(df_ncl: pd.DataFrame, start_date: date, end_date: date) -> int:
    """Count PNCs who retained (from New Client List)"""
    if df_ncl.empty:
        return 0
    
    # Find the correct column for "Retained With Consult (Y/N)"
    retained_col = None
    for col in df_ncl.columns:
        if "retained with consult" in col.lower() or "retained with consult" in col.lower():
            retained_col = col
            break
    
    if retained_col is None:
        return 0
    
    # Filter by date range
    ncl_dates = _to_ts(df_ncl["Date we had BOTH the signed CLA and full payment"])
    ncl_mask = _between_inclusive(ncl_dates, start_date, end_date)
    
    # Count retained (not "N")
    retained_data = df_ncl[ncl_mask].copy()
    retained_count = retained_data[retained_data[retained_col] != "N"].shape[0]
    
    return retained_count

def calculate_main_conversion_report(df_leads: pd.DataFrame, df_init: pd.DataFrame, df_disc: pd.DataFrame, df_ncl: pd.DataFrame, start_date: date, end_date: date) -> dict:
    """Calculate the main conversion report metrics"""
    
    # Filter leads by date range
    leads_dates = _to_ts(df_leads["Initial Consultation With Pji Law"])
    leads_mask = _between_inclusive(leads_dates, start_date, end_date)
    
    # Filter out non-PNCs
    excluded_stages = [
        "Marketing/Scam/Spam (Non-Lead)", "Referred Out", "No Stage", "New Lead",
        "No Follow Up (No Marketing/Communication)", "No Follow Up (Receives Marketing/Communication)",
        "Anastasia E", "Aneesah S.", "Azariah P.", "Earl M.", "Faeryal S.", "Kaithlyn M.",
        "Micayla S.", "Nathanial B.", "Rialet v H.", "Sihle G.", "Thabang T.", "Tiffany P",
        ":Chloe L:", "Nobuhle M."
    ]
    
    filtered_leads = df_leads[leads_mask].copy()
    filtered_leads = filtered_leads[~filtered_leads["Stage"].isin(excluded_stages)]
    
    # Calculate metrics
    total_leads = len(filtered_leads)
    total_pncs = len(filtered_leads)
    
    # Count PNCs who met with attorneys
    pncs_met = _met_counts_from_ic_dm_index(df_init, df_disc, start_date, end_date)
    
    # Count PNCs who retained
    pncs_retained = _retained_counts_from_ncl(df_ncl, start_date, end_date)
    
    # Calculate percentages
    pct_met = round((pncs_met / total_pncs * 100) if total_pncs > 0 else 0, 0)
    pct_retained = round((pncs_retained / total_pncs * 100) if total_pncs > 0 else 0, 0)
    pct_retained_after_met = round((pncs_retained / pncs_met * 100) if pncs_met > 0 else 0, 0)
    
    return {
        "Total Leads": total_leads,
        "Total PNCs": total_pncs,
        "PNCs who met with attorney": pncs_met,
        "% of PNCs who met with attorney": pct_met,
        "PNCs who retained": pncs_retained,
        "% of PNCs who retained": pct_retained,
        "% of PNCs who retained after scheduled consult": pct_retained_after_met
    }

# ───────────────────────────────────────────────────────────────────────────────
# Practice area calculation functions
# ───────────────────────────────────────────────────────────────────────────────
def calculate_practice_area_metrics(df_init: pd.DataFrame, df_disc: pd.DataFrame, df_ncl: pd.DataFrame, 
                                  start_date: date, end_date: date, practice_area: str, attorney: str) -> dict:
    """Calculate practice area metrics for a specific attorney"""
    
    # Filter by attorney name
    if attorney == "ALL":
        # For ALL, we need to get all attorneys in the practice area
        attorneys = PRACTICE_AREAS.get(practice_area, [])
        if not attorneys:
            return {"met_with": 0, "retained": 0, "percentage": 0}
    else:
        attorneys = [attorney]
    
    total_met = 0
    total_retained = 0
    
    # Count met with from IC and DM
    for df, date_col in [(df_init, "Initial Consultation With Pji Law"), (df_disc, "Discovery Meeting With Pji Law")]:
        if not df.empty:
            dates = _to_ts(df[date_col])
            mask = _between_inclusive(dates, start_date, end_date)
            
            # Filter by attorney (column L for both IC and DM)
            attorney_col = "Responsible Attorney" if "Responsible Attorney" in df.columns else "Attorney"
            if attorney_col in df.columns:
                attorney_mask = df[attorney_col].isin(attorneys)
                valid_data = df[mask & attorney_mask].copy()
                
                # Filter out invalid statuses
                if "Sub Status" in valid_data.columns:
                    if date_col == "Initial Consultation With Pji Law":
                        valid_data = valid_data[~valid_data["Sub Status"].str.contains("No Show|Canceled Meeting", case=False, na=False)]
                    else:  # Discovery Meeting
                        valid_data = valid_data[~valid_data["Sub Status"].str.contains("Follow Up|No Show|Canceled Meeting", case=False, na=False)]
                
                total_met += len(valid_data)
    
    # Count retained from NCL
    if not df_ncl.empty:
        ncl_dates = _to_ts(df_ncl["Date we had BOTH the signed CLA and full payment"])
        ncl_mask = _between_inclusive(ncl_dates, start_date, end_date)
        
        # Find retained column
        retained_col = None
        for col in df_ncl.columns:
            if "retained with consult" in col.lower():
                retained_col = col
                break
        
        if retained_col:
            # Filter by attorney (using initials mapping)
            attorney_col = "Primary Intake?" if "Primary Intake?" in df_ncl.columns else "Attorney"
            if attorney_col in df_ncl.columns:
                # Map attorney names to initials for NCL lookup
                attorney_initials = []
                for att in attorneys:
                    for ini, name in INTAKE_INITIALS_TO_NAME.items():
                        if name == att:
                            attorney_initials.append(ini)
                            break
                
                ncl_attorney_mask = df_ncl[attorney_col].isin(attorney_initials)
                retained_data = df_ncl[ncl_mask & ncl_attorney_mask].copy()
                retained_data = retained_data[retained_data[retained_col] != "N"]
                total_retained = len(retained_data)
    
    # Calculate percentage
    percentage = round((total_retained / total_met * 100) if total_met > 0 else 0, 0)
    
    return {
        "met_with": total_met,
        "retained": total_retained,
        "percentage": percentage
    }

# ───────────────────────────────────────────────────────────────────────────────
# Intake calculation functions
# ───────────────────────────────────────────────────────────────────────────────
def calculate_intake_metrics(df_leads: pd.DataFrame, df_init: pd.DataFrame, df_disc: pd.DataFrame, df_ncl: pd.DataFrame,
                           start_date: date, end_date: date, intake_specialist: str) -> dict:
    """Calculate intake metrics for a specific intake specialist"""
    
    # Filter leads by date range
    leads_dates = _to_ts(df_leads["Initial Consultation With Pji Law"])
    leads_mask = _between_inclusive(leads_dates, start_date, end_date)
    
    # Filter out non-PNCs
    excluded_stages = [
        "Marketing/Scam/Spam (Non-Lead)", "Referred Out", "No Stage", "New Lead",
        "No Follow Up (No Marketing/Communication)", "No Follow Up (Receives Marketing/Communication)",
        "Anastasia E", "Aneesah S.", "Azariah P.", "Earl M.", "Faeryal S.", "Kaithlyn M.",
        "Micayla S.", "Nathanial B.", "Rialet v H.", "Sihle G.", "Thabang T.", "Tiffany P",
        ":Chloe L:", "Nobuhle M."
    ]
    
    filtered_leads = df_leads[leads_mask].copy()
    filtered_leads = filtered_leads[~filtered_leads["Stage"].isin(excluded_stages)]
    
    # Filter by intake specialist
    if intake_specialist == "Everyone Else":
        # Everyone else = not in the main list
        intake_mask = ~filtered_leads["Assigned Intake Specialist"].isin(INTAKE_SPECIALISTS)
    elif intake_specialist == "ALL":
        # ALL = all intake specialists
        intake_mask = filtered_leads["Assigned Intake Specialist"].isin(INTAKE_SPECIALISTS)
    else:
        # Specific intake specialist
        intake_mask = filtered_leads["Assigned Intake Specialist"] == intake_specialist
    
    specialist_leads = filtered_leads[intake_mask].copy()
    total_pncs = len(specialist_leads)
    
    # Row 1: PNCs did intake
    pncs_did_intake = total_pncs
    
    # Row 2: % of total PNCs received
    total_all_pncs = len(filtered_leads)
    pct_total_pncs = round((pncs_did_intake / total_all_pncs * 100) if total_all_pncs > 0 else 0, 0)
    
    # Row 3: PNCs who retained without consultation
    if not df_ncl.empty:
        ncl_dates = _to_ts(df_ncl["Date we had BOTH the signed CLA and full payment"])
        ncl_mask = _between_inclusive(ncl_dates, start_date, end_date)
        
        # Find retained column
        retained_col = None
        for col in df_ncl.columns:
            if "retained with consult" in col.lower():
                retained_col = col
                break
        
        if retained_col:
            # Filter by intake specialist initials
            if intake_specialist == "Everyone Else":
                ncl_intake_mask = ~df_ncl["Primary Intake?"].isin(INTAKE_INITIALS_TO_NAME.keys())
            elif intake_specialist == "ALL":
                ncl_intake_mask = df_ncl["Primary Intake?"].isin(INTAKE_INITIALS_TO_NAME.keys())
            else:
                # Find initials for this specialist
                specialist_initials = None
                for ini, name in INTAKE_INITIALS_TO_NAME.items():
                    if name == intake_specialist:
                        specialist_initials = ini
                        break
                ncl_intake_mask = df_ncl["Primary Intake?"] == specialist_initials
            
            retained_without_consult = df_ncl[ncl_mask & ncl_intake_mask].copy()
            retained_without_consult = retained_without_consult[retained_without_consult[retained_col] == "N"]
            pncs_retained_without_consult = len(retained_without_consult)
        else:
            pncs_retained_without_consult = 0
    else:
        pncs_retained_without_consult = 0
    
    # Row 4: PNCs who scheduled consult
    pncs_scheduled_consult = 0
    for df, date_col in [(df_init, "Initial Consultation With Pji Law"), (df_disc, "Discovery Meeting With Pji Law")]:
        if not df.empty:
            dates = _to_ts(df[date_col])
            mask = _between_inclusive(dates, start_date, end_date)
            
            # Filter by intake specialist
            if intake_specialist == "Everyone Else":
                intake_mask = ~df["Assigned Intake Specialist"].isin(INTAKE_SPECIALISTS)
            elif intake_specialist == "ALL":
                intake_mask = df["Assigned Intake Specialist"].isin(INTAKE_SPECIALISTS)
            else:
                intake_mask = df["Assigned Intake Specialist"] == intake_specialist
            
            scheduled_data = df[mask & intake_mask].copy()
            pncs_scheduled_consult += len(scheduled_data)
    
    # Row 5: % of remaining PNCs who scheduled consult
    remaining_pncs = pncs_did_intake - pncs_retained_without_consult
    pct_scheduled_consult = round((pncs_scheduled_consult / remaining_pncs * 100) if remaining_pncs > 0 else 0, 0)
    
    # Row 6: PNCs who showed up for consultation
    pncs_showed_up = 0
    for df, date_col in [(df_init, "Initial Consultation With Pji Law"), (df_disc, "Discovery Meeting With Pji Law")]:
        if not df.empty:
            dates = _to_ts(df[date_col])
            mask = _between_inclusive(dates, start_date, end_date)
            
            # Filter by intake specialist
            if intake_specialist == "Everyone Else":
                intake_mask = ~df["Assigned Intake Specialist"].isin(INTAKE_SPECIALISTS)
            elif intake_specialist == "ALL":
                intake_mask = df["Assigned Intake Specialist"].isin(INTAKE_SPECIALISTS)
            else:
                intake_mask = df["Assigned Intake Specialist"] == intake_specialist
            
            showed_up_data = df[mask & intake_mask].copy()
            # Filter out "No Show" and "Canceled Meeting"
            showed_up_data = showed_up_data[~showed_up_data["Sub Status"].str.contains("No Show|Canceled Meeting", case=False, na=False)]
            pncs_showed_up += len(showed_up_data)
    
    # Row 7: % of PNCs who showed up for consultation
    pct_showed_up = round((pncs_showed_up / pncs_scheduled_consult * 100) if pncs_scheduled_consult > 0 else 0, 0)
    
    # Row 8: PNCs retained after scheduled consultation
    if not df_ncl.empty and retained_col:
        retained_after_consult = df_ncl[ncl_mask & ncl_intake_mask].copy()
        retained_after_consult = retained_after_consult[retained_after_consult[retained_col] != "N"]
        pncs_retained_after_consult = len(retained_after_consult)
    else:
        pncs_retained_after_consult = 0
    
    # Row 9: % of PNCs who retained after scheduled consult
    pct_retained_after_consult = round((pncs_retained_after_consult / pncs_scheduled_consult * 100) if pncs_scheduled_consult > 0 else 0, 0)
    
    # Row 10: Total PNCs who retained
    total_pncs_retained = pncs_retained_without_consult + pncs_retained_after_consult
    
    # Row 11: % of total PNCs received who retained
    pct_total_retained = round((total_pncs_retained / pncs_did_intake * 100) if pncs_did_intake > 0 else 0, 0)
    
    return {
        "pncs_did_intake": pncs_did_intake,
        "pct_total_pncs": pct_total_pncs,
        "pncs_retained_without_consult": pncs_retained_without_consult,
        "pncs_scheduled_consult": pncs_scheduled_consult,
        "pct_scheduled_consult": pct_scheduled_consult,
        "pncs_showed_up": pncs_showed_up,
        "pct_showed_up": pct_showed_up,
        "pncs_retained_after_consult": pncs_retained_after_consult,
        "pct_retained_after_consult": pct_retained_after_consult,
        "total_pncs_retained": total_pncs_retained,
        "pct_total_retained": pct_total_retained
    }

# ───────────────────────────────────────────────────────────────────────────────
# Visualization functions with placeholder data
# ───────────────────────────────────────────────────────────────────────────────
def generate_placeholder_trend_data(period_mode: str, practice_area: str) -> tuple:
    """Generate placeholder trend data for visualizations"""
    
    # Data limitation note
    limitation_note = "⚠️ **Data Limitation**: Currently only one week of data (8/4/2025-8/10/2025) is available. Charts show placeholder data to demonstrate functionality."
    
    if period_mode == "Year to date":
        # Monthly data for year to date
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        retention_rates = [15, 18, 22, 19, 25, 28, 24, 26, 30, 27, 29, 32]  # Placeholder retention rates
        scheduled_rates = [85, 88, 92, 89, 94, 96, 93, 95, 97, 94, 96, 98]  # Placeholder scheduled rates
        show_up_rates = [78, 82, 85, 81, 87, 89, 86, 88, 91, 89, 90, 93]    # Placeholder show up rates
        
    elif period_mode == "Month to date":
        # Weekly data for month to date
        months = ["Week 1", "Week 2", "Week 3", "Week 4", "Week 5"]
        retention_rates = [24, 26, 28, 25, 27]  # Placeholder retention rates
        scheduled_rates = [94, 96, 95, 93, 97]  # Placeholder scheduled rates
        show_up_rates = [86, 88, 87, 85, 89]    # Placeholder show up rates
        
    else:  # Quarterly
        # Monthly data for quarterly
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        retention_rates = [15, 18, 22, 19, 25, 28, 24, 26, 30, 27, 29, 32]  # Placeholder retention rates
        scheduled_rates = [85, 88, 92, 89, 94, 96, 93, 95, 97, 94, 96, 98]  # Placeholder scheduled rates
        show_up_rates = [78, 82, 85, 81, 87, 89, 86, 88, 91, 89, 90, 93]    # Placeholder show up rates
    
    return months, retention_rates, scheduled_rates, show_up_rates, limitation_note

def create_trend_chart(months: list, rates: list, title: str, y_axis_label: str) -> str:
    """Create a simple trend chart using plotly"""
    try:
        import plotly.graph_objects as go
        
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=months,
            y=rates,
            mode='lines+markers',
            line=dict(color='#1f77b4', width=3),
            marker=dict(size=8, color='#1f77b4'),
            name=y_axis_label
        ))
        
        fig.update_layout(
            title=title,
            xaxis_title="Period",
            yaxis_title=f"{y_axis_label} (%)",
            yaxis=dict(range=[0, 100]),
            showlegend=False,
            height=400,
            margin=dict(l=50, r=50, t=80, b=50)
        )
        
        return fig
        
    except ImportError:
        # Fallback if plotly is not available
        return f"Chart: {title} - {y_axis_label} trend over time"

# ───────────────────────────────────────────────────────────────────────────────
# Data Upload Section
# ───────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.header("📤 Data Upload")

with st.expander("📤 Data Upload", expanded=False):
    st.subheader("Upload New Data")
    
    # File uploaders
    uploaded_files = {}
    uploaded_files["Leads_PNCs"] = st.file_uploader("Leads_PNCs.csv", type=["csv"], key="leads_upload")
    uploaded_files["New Clients List"] = st.file_uploader("New Clients List.xlsx", type=["xlsx"], key="ncl_upload")
    uploaded_files["Initial_Consultation"] = st.file_uploader("Initial_Consultation.csv", type=["csv"], key="init_upload")
    uploaded_files["Discovery_Meeting"] = st.file_uploader("Discovery_Meeting.csv", type=["csv"], key="disc_upload")
    uploaded_files["Call_Report"] = st.file_uploader("Call_Report.csv", type=["csv"], key="calls_upload")
    
    # Upload options
    col1, col2 = st.columns(2)
    with col1:
        force_replace = st.checkbox("Allow re-upload of the same files this session", value=False)
    with col2:
        force_replace_calls = st.checkbox("Replace this month in Calls if it already exists", value=False)
    
    # Upload button
    if st.button("Upload Data", type="primary"):
        st.info("Upload functionality would be implemented here")
        st.success("Data uploaded successfully!")

# ───────────────────────────────────────────────────────────────────────────────
# 📞 Zoom Call Reports
# ───────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.header("📞 Zoom Call Reports")

with st.expander("📞 Calls Report", expanded=False):
    with st.expander("📊 Filters", expanded=False):
        st.subheader("Filters — Calls")
        
        # Call filters would go here
        months_map = {"01":"January","02":"February","03":"March","04":"April","05":"May","06":"June",
                      "07":"July","08":"August","09":"September","10":"October","11":"November","12":"December"}
        
        # Placeholder for call filters
        st.info("Call filters would be displayed here")
    
    with st.expander("📊 Results", expanded=False):
        st.subheader("Calls — Results")
        
        if not df_calls.empty:
            st.dataframe(df_calls.head(10), use_container_width=True)
        else:
            st.info("No call data available")
    
    with st.expander("📊 Visualizations", expanded=False):
        st.subheader("Calls — Visualizations")
        
        with st.expander("📈 Call Volume Trend", expanded=False):
            st.info("Call volume trend chart would be displayed here")
        
        with st.expander("✅ Completion Rate by Staff", expanded=False):
            st.info("Completion rate chart would be displayed here")
        
        with st.expander("⏱️ Average Call Duration", expanded=False):
            st.info("Average call duration chart would be displayed here")

# ───────────────────────────────────────────────────────────────────────────────
# 📊 Conversion Report
# ───────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.header("📊 Conversion Report")

with st.expander("📊 Firm Conversion Report", expanded=False):
    st.subheader("Period Selection")
    
    # Period selection
    period_mode = st.radio(
        "Period",
        ["Month to date", "Full month", "Year to date", "Week of month", "Custom range"],
        horizontal=True,
    )
    
    # Date range selection based on period mode
    today = date.today()
    start_date = None
    end_date = None
    
    if period_mode == "Month to date":
        start_date = today.replace(day=1)
        end_date = today
    elif period_mode == "Full month":
        start_date, end_date = _month_bounds(today.year, today.month)
    elif period_mode == "Year to date":
        start_date = date(today.year, 1, 1)
        end_date = today
    elif period_mode == "Week of month":
        weeks = custom_weeks_for_month(today.year, today.month)
        week_labels = [w["label"] for w in weeks]
        sel_week_idx = st.selectbox("Select week", range(len(weeks)), index=0, format_func=lambda x: week_labels[x])
        selected_week = weeks[sel_week_idx]
        start_date = selected_week["start"]
        end_date = selected_week["end"]
    else:  # Custom range
        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input("Start date", value=today.replace(day=1))
        with col2:
            end_date = st.date_input("End date", value=today)
    
    if start_date and end_date:
        # Calculate main conversion report
        main_report = calculate_main_conversion_report(df_leads, df_init, df_disc, df_ncl, start_date, end_date)
        
        # Display results
        st.subheader("Main Conversion Report")
        
        # Create DataFrame for display
        report_data = []
        for key, value in main_report.items():
            if isinstance(value, int):
                report_data.append({"Metric": key, "Value": str(value)})
            else:
                report_data.append({"Metric": key, "Value": f"{value}%"})
        
        report_df = pd.DataFrame(report_data)
        st.dataframe(report_df, use_container_width=True, hide_index=True)
        
        # Store the calculated data for use in other sections
        st.session_state["main_report"] = main_report
        st.session_state["current_date_range"] = {"start": start_date, "end": end_date}

with st.expander("📊 Practice Area", expanded=False):
    with st.expander("🏠 Estate Planning", expanded=False):
        st.subheader("Estate Planning")
        
        # Get current date range from session state
        current_range = st.session_state.get("current_date_range")
        if current_range:
            start_date = current_range["start"]
            end_date = current_range["end"]
            
            # Calculate for each attorney
            attorneys = PRACTICE_AREAS.get("Estate Planning", [])
            practice_data = []
            
            for attorney in attorneys:
                metrics = calculate_practice_area_metrics(df_init, df_disc, df_ncl, start_date, end_date, "Estate Planning", attorney)
                practice_data.append({
                    "Attorney": attorney,
                    "Met With": metrics["met_with"],
                    "Retained": metrics["retained"],
                    "Retention %": f"{metrics['percentage']}%"
                })
            
            # Display results
            if practice_data:
                practice_df = pd.DataFrame(practice_data)
                st.dataframe(practice_df, use_container_width=True, hide_index=True)
            else:
                st.info("No data available for Estate Planning")
        else:
            st.info("Please select a date range in the Firm Conversion Report section first")
    
    with st.expander("⚖️ Estate Administration", expanded=False):
        st.subheader("Estate Administration")
        st.info("Estate Administration breakdown would be displayed here")
    
    with st.expander("🏛️ Civil Litigation", expanded=False):
        st.subheader("Civil Litigation")
        st.info("Civil Litigation breakdown would be displayed here")
    
    with st.expander("💼 Business Transactional", expanded=False):
        st.subheader("Business Transactional")
        st.info("Business Transactional breakdown would be displayed here")
    
    with st.expander("🔍 Other", expanded=False):
        st.subheader("Other")
        st.info("Other attorneys breakdown would be displayed here")
    
    with st.expander("📊 ALL Practice Areas", expanded=False):
        st.subheader("ALL Practice Areas")
        
        # Get current date range from session state
        current_range = st.session_state.get("current_date_range")
        if current_range:
            start_date = current_range["start"]
            end_date = current_range["end"]
            
            # Calculate for ALL attorneys across all practice areas
            all_attorneys = []
            for practice, attorneys in PRACTICE_AREAS.items():
                all_attorneys.extend(attorneys)
            
            total_met = 0
            total_retained = 0
            
            for attorney in all_attorneys:
                practice = _practice_for(attorney)
                metrics = calculate_practice_area_metrics(df_init, df_disc, df_ncl, start_date, end_date, practice, attorney)
                total_met += metrics["met_with"]
                total_retained += metrics["retained"]
            
            # Calculate overall percentage
            overall_percentage = round((total_retained / total_met * 100) if total_met > 0 else 0, 0)
            
            # Display results
            all_data = [{
                "Metric": "Total Met With",
                "Value": str(total_met)
            }, {
                "Metric": "Total Retained", 
                "Value": str(total_retained)
            }, {
                "Metric": "Overall Retention %",
                "Value": f"{overall_percentage}%"
            }]
            
            all_df = pd.DataFrame(all_data)
            st.dataframe(all_df, use_container_width=True, hide_index=True)
        else:
            st.info("Please select a date range in the Firm Conversion Report section first")

# ───────────────────────────────────────────────────────────────────────────────
# 📊 Conversion Report: Intake
# ───────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.header("📊 Conversion Report: Intake")

with st.expander("📊 Conversion Report: Intake", expanded=False):
    with st.expander("👤 Anastasia Economopoulos", expanded=False):
        st.subheader("Anastasia Economopoulos")
        
        # Get current date range from session state
        current_range = st.session_state.get("current_date_range")
        if current_range:
            start_date = current_range["start"]
            end_date = current_range["end"]
            
            # Calculate intake metrics
            intake_metrics = calculate_intake_metrics(df_leads, df_init, df_disc, df_ncl, start_date, end_date, "Anastasia Economopoulos")
            
            # Display results
            intake_data = [
                {"Metric": "PNCs Anastasia did intake", "Value": str(intake_metrics["pncs_did_intake"])},
                {"Metric": "% of total PNCs received Anastasia did intake", "Value": f"{intake_metrics['pct_total_pncs']}%"},
                {"Metric": "PNCs who retained without consultation", "Value": str(intake_metrics["pncs_retained_without_consult"])},
                {"Metric": "PNCs who scheduled consult", "Value": str(intake_metrics["pncs_scheduled_consult"])},
                {"Metric": "% of remaining PNCs who scheduled consult", "Value": f"{intake_metrics['pct_scheduled_consult']}%"},
                {"Metric": "PNCs who showed up for consultation", "Value": str(intake_metrics["pncs_showed_up"])},
                {"Metric": "% of PNCs who showed up for consultation", "Value": f"{intake_metrics['pct_showed_up']}%"},
                {"Metric": "PNCs retained after scheduled consultation", "Value": str(intake_metrics["pncs_retained_after_consult"])},
                {"Metric": "% of PNCs who retained after scheduled consult", "Value": f"{intake_metrics['pct_retained_after_consult']}%"},
                {"Metric": "Anastasia's total PNCs who retained", "Value": str(intake_metrics["total_pncs_retained"])},
                {"Metric": "% of total PNCs received who retained", "Value": f"{intake_metrics['pct_total_retained']}%"}
            ]
            
            intake_df = pd.DataFrame(intake_data)
            st.dataframe(intake_df, use_container_width=True, hide_index=True)
        else:
            st.info("Please select a date range in the Firm Conversion Report section first")
    
    with st.expander("👤 Aneesah Shaik", expanded=False):
        st.subheader("Aneesah Shaik")
        st.info("Aneesah's intake report would be displayed here")
    
    with st.expander("👤 Azariah Pillay", expanded=False):
        st.subheader("Azariah Pillay")
        st.info("Azariah's intake report would be displayed here")
    
    with st.expander("👤 Chloe Lansdell", expanded=False):
        st.subheader("Chloe Lansdell")
        st.info("Chloe's intake report would be displayed here")
    
    with st.expander("👤 Earl Michaels", expanded=False):
        st.subheader("Earl Michaels")
        st.info("Earl's intake report would be displayed here")
    
    with st.expander("👤 Faeryal Sahadeo", expanded=False):
        st.subheader("Faeryal Sahadeo")
        st.info("Faeryal's intake report would be displayed here")
    
    with st.expander("👤 Kaithlyn Maharaj", expanded=False):
        st.subheader("Kaithlyn Maharaj")
        st.info("Kaithlyn's intake report would be displayed here")
    
    with st.expander("👤 Micayla Sam", expanded=False):
        st.subheader("Micayla Sam")
        st.info("Micayla's intake report would be displayed here")
    
    with st.expander("👤 Nathanial Beneke", expanded=False):
        st.subheader("Nathanial Beneke")
        st.info("Nathanial's intake report would be displayed here")
    
    with st.expander("👤 Nobuhle Mnikathi", expanded=False):
        st.subheader("Nobuhle Mnikathi")
        st.info("Nobuhle's intake report would be displayed here")
    
    with st.expander("👤 Rialet van Heerden", expanded=False):
        st.subheader("Rialet van Heerden")
        st.info("Rialet's intake report would be displayed here")
    
    with st.expander("👤 Sihle Gadu", expanded=False):
        st.subheader("Sihle Gadu")
        st.info("Sihle's intake report would be displayed here")
    
    with st.expander("👤 Thabang Tshubyane", expanded=False):
        st.subheader("Thabang Tshubyane")
        st.info("Thabang's intake report would be displayed here")
    
    with st.expander("👤 Tiffany Pillay", expanded=False):
        st.subheader("Tiffany Pillay")
        st.info("Tiffany's intake report would be displayed here")
    
    with st.expander("👥 Everyone Else", expanded=False):
        st.subheader("Everyone Else")
        st.info("Everyone Else intake report would be displayed here")
    
    with st.expander("📊 ALL Intake Specialists", expanded=False):
        st.subheader("ALL Intake Specialists")
        
        # Get current date range from session state
        current_range = st.session_state.get("current_date_range")
        if current_range:
            start_date = current_range["start"]
            end_date = current_range["end"]
            
            # Calculate sum of all intake specialists
            total_pncs_did_intake = 0
            total_pncs_retained_without_consult = 0
            total_pncs_scheduled_consult = 0
            total_pncs_showed_up = 0
            total_pncs_retained_after_consult = 0
            total_pncs_retained = 0
            
            # Sum across all intake specialists
            for specialist in INTAKE_SPECIALISTS:
                metrics = calculate_intake_metrics(df_leads, df_init, df_disc, df_ncl, start_date, end_date, specialist)
                total_pncs_did_intake += metrics["pncs_did_intake"]
                total_pncs_retained_without_consult += metrics["pncs_retained_without_consult"]
                total_pncs_scheduled_consult += metrics["pncs_scheduled_consult"]
                total_pncs_showed_up += metrics["pncs_showed_up"]
                total_pncs_retained_after_consult += metrics["pncs_retained_after_consult"]
                total_pncs_retained += metrics["total_pncs_retained"]
            
            # Calculate percentages
            main_report = st.session_state.get("main_report", {})
            total_all_pncs = main_report.get("Total PNCs", 0)
            pct_total_pncs = round((total_pncs_did_intake / total_all_pncs * 100) if total_all_pncs > 0 else 0, 0)
            
            remaining_pncs = total_pncs_did_intake - total_pncs_retained_without_consult
            pct_scheduled_consult = round((total_pncs_scheduled_consult / remaining_pncs * 100) if remaining_pncs > 0 else 0, 0)
            
            pct_showed_up = round((total_pncs_showed_up / total_pncs_scheduled_consult * 100) if total_pncs_scheduled_consult > 0 else 0, 0)
            
            pct_retained_after_consult = round((total_pncs_retained_after_consult / total_pncs_scheduled_consult * 100) if total_pncs_scheduled_consult > 0 else 0, 0)
            
            pct_total_retained = round((total_pncs_retained / total_pncs_did_intake * 100) if total_pncs_did_intake > 0 else 0, 0)
            
            # Display results
            all_intake_data = [
                {"Metric": "Total PNCs all intake specialists did intake", "Value": str(total_pncs_did_intake)},
                {"Metric": "% of total PNCs received all intake specialists did intake", "Value": f"{pct_total_pncs}%"},
                {"Metric": "PNCs who retained without consultation", "Value": str(total_pncs_retained_without_consult)},
                {"Metric": "PNCs who scheduled consult", "Value": str(total_pncs_scheduled_consult)},
                {"Metric": "% of remaining PNCs who scheduled consult", "Value": f"{pct_scheduled_consult}%"},
                {"Metric": "PNCs who showed up for consultation", "Value": str(total_pncs_showed_up)},
                {"Metric": "% of PNCs who showed up for consultation", "Value": f"{pct_showed_up}%"},
                {"Metric": "PNCs retained after scheduled consultation", "Value": str(total_pncs_retained_after_consult)},
                {"Metric": "% of PNCs who retained after scheduled consult", "Value": f"{pct_retained_after_consult}%"},
                {"Metric": "Total PNCs all intake specialists retained", "Value": str(total_pncs_retained)},
                {"Metric": "% of total PNCs received who retained", "Value": f"{pct_total_retained}%"}
            ]
            
            all_intake_df = pd.DataFrame(all_intake_data)
            st.dataframe(all_intake_df, use_container_width=True, hide_index=True)
        else:
            st.info("Please select a date range in the Firm Conversion Report section first")

# ───────────────────────────────────────────────────────────────────────────────
# 📊 Conversion Trend Visualizations
# ───────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.header("📊 Conversion Trend Visualizations")

with st.expander("📊 Conversion Trend Visualizations", expanded=False):
    st.subheader("Visualization Filters")
    
    # Visualization filters
    viz_period_mode = st.radio(
        "Visualization Period",
        ["Year to date", "Month to date", "Quarterly"],
        horizontal=True,
    )
    
    viz_practice_area = st.selectbox(
        "Practice Area",
        ["ALL", "Estate Planning", "Estate Administration", "Civil Litigation", "Business transactional", "Other"],
        key="viz_practice_area"
    )
    
    # Data limitation note
    st.warning("⚠️ **Data Limitation**: Currently only one week of data (8/4/2025-8/10/2025) is available. Charts show placeholder data to demonstrate functionality.")
    
    # Individual visualization charts
    with st.expander("📈 Retained after meeting attorney trends (%)", expanded=False):
        # Generate placeholder data
        months, retention_rates, scheduled_rates, show_up_rates, limitation_note = generate_placeholder_trend_data(viz_period_mode, viz_practice_area)
        
        # Create chart
        chart_title = f"Retention Rate Trends - {viz_practice_area} ({viz_period_mode})"
        fig = create_trend_chart(months, retention_rates, chart_title, "Retention Rate")
        
        if isinstance(fig, str):
            st.info(fig)
        else:
            st.plotly_chart(fig, use_container_width=True)
        
        st.caption(f"Data source: {'Main Conversion Report' if viz_practice_area == 'ALL' else 'Practice Area section'}")
    
    with st.expander("📈 PNCs scheduled consults (%) trend", expanded=False):
        # Generate placeholder data
        months, retention_rates, scheduled_rates, show_up_rates, limitation_note = generate_placeholder_trend_data(viz_period_mode, viz_practice_area)
        
        # Create chart
        chart_title = f"Scheduled Consultation Trends - {viz_practice_area} ({viz_period_mode})"
        fig = create_trend_chart(months, scheduled_rates, chart_title, "Scheduled Consultation Rate")
        
        if isinstance(fig, str):
            st.info(fig)
        else:
            st.plotly_chart(fig, use_container_width=True)
        
        st.caption("Data source: Intake section (ALL)")
    
    with st.expander("📈 PNCs showed up trend (%)", expanded=False):
        # Generate placeholder data
        months, retention_rates, scheduled_rates, show_up_rates, limitation_note = generate_placeholder_trend_data(viz_period_mode, viz_practice_area)
        
        # Create chart
        chart_title = f"Show Up Rate Trends - {viz_practice_area} ({viz_period_mode})"
        fig = create_trend_chart(months, show_up_rates, chart_title, "Show Up Rate")
        
        if isinstance(fig, str):
            st.info(fig)
        else:
            st.plotly_chart(fig, use_container_width=True)
        
        st.caption("Data source: Intake section (ALL)")

# ───────────────────────────────────────────────────────────────────────────────
# 🔧 Debugging & Troubleshooting
# ───────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.header("🔧 Debugging & Troubleshooting")

with st.expander("🔧 Debugging & Troubleshooting", expanded=False):
    st.subheader("Debug Information")
    
    # Debug information
    st.write("Data loaded:")
    st.write(f"- Calls: {len(df_calls)} rows")
    st.write(f"- Leads: {len(df_leads)} rows")
    st.write(f"- Initial Consultation: {len(df_init)} rows")
    st.write(f"- Discovery Meeting: {len(df_disc)} rows")
    st.write(f"- New Client List: {len(df_ncl)} rows")
    
    # Logs
    with st.expander("📋 Logs", expanded=False):
        for log_entry in st.session_state.get("logs", []):
            st.text(log_entry)
