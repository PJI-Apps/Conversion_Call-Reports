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
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        
        sa = st.secrets["gcp_service_account"]
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        
        creds = Credentials.from_service_account_info(sa, scopes=scopes)
        gc = gspread.authorize(creds)
        return gc, st.secrets["gsheet_url"]
    except Exception as e:
        st.error(f"Google Sheets connection failed: {e}")
        return None, None

GC, GSHEET = _gsheet_client_cached()

def _ws(tab_name: str):
    if not GC or not GSHEET:
        return None
    try:
        sheet = GC.open_by_url(GSHEET)
        try:
            return sheet.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            # Try fallback names
            fallbacks = TAB_FALLBACKS.get(tab_name, [])
            for fallback in fallbacks:
                try:
                    return sheet.worksheet(fallback)
                except gspread.WorksheetNotFound:
                    continue
            # Create if not found
            try:
                return sheet.add_worksheet(title=tab_name, rows=1000, cols=50)
            except Exception as e:
                st.error(f"Could not access/create worksheet '{tab_name}'. Please check permissions and tab names.")
                return None
    except Exception as e:
        st.error(f"Could not access Google Sheet: {e}")
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
# Data Upload Section
# ───────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.header("📤 Data Upload")

with st.expander("📤 Data Upload", expanded=False):
    st.subheader("Upload New Data")
    
    # File uploaders
    uploaded_files = {}
    uploaded_files["Leads_PNCs"] = st.file_uploader("Leads_PNCs.csv", type=["csv"], key="leads")
    uploaded_files["New Clients List"] = st.file_uploader("New Clients List.xlsx", type=["xlsx"], key="ncl")
    uploaded_files["Initial_Consultation"] = st.file_uploader("Initial_Consultation.csv", type=["csv"], key="init")
    uploaded_files["Discovery_Meeting"] = st.file_uploader("Discovery_Meeting.csv", type=["csv"], key="disc")
    uploaded_files["Call_Report"] = st.file_uploader("Call_Report.csv", type=["csv"], key="calls")
    
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
    
    # Period selection would go here
    period_mode = st.radio(
        "Period",
        ["Month to date", "Full month", "Year to date", "Week of month", "Custom range"],
        horizontal=True,
    )
    
    # Placeholder for conversion report
    st.info("Main conversion report table would be displayed here")

with st.expander("📊 Practice Area", expanded=False):
    with st.expander("🏠 Estate Planning", expanded=False):
        st.subheader("Estate Planning")
        st.info("Estate Planning breakdown would be displayed here")
    
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
        st.info("Combined view of all practice areas would be displayed here")

# ───────────────────────────────────────────────────────────────────────────────
# 📊 Conversion Report: Intake
# ───────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.header("📊 Conversion Report: Intake")

with st.expander("📊 Conversion Report: Intake", expanded=False):
    with st.expander("👤 Anastasia Economopoulos", expanded=False):
        st.subheader("Anastasia Economopoulos")
        st.info("Anastasia's intake report would be displayed here")
    
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
        st.info("Combined view of all intake specialists would be displayed here")

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
    
    # Individual visualization charts
    with st.expander("📈 Retained after meeting attorney trends (%)", expanded=False):
        st.info("Retention rate trend chart would be displayed here")
    
    with st.expander("📈 PNCs scheduled consults (%) trend", expanded=False):
        st.info("Scheduled consultation trend chart would be displayed here")
    
    with st.expander("📈 PNCs showed up trend (%)", expanded=False):
        st.info("Show up rate trend chart would be displayed here")

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
