# app.py
# ---------------------------
# Zoom Call Reports (Streamlit)
# - Auth via streamlit-authenticator, YAML stored in Streamlit Secrets
# - Upload Zoom CSV, assign Month-Year, whitelist Names, rename 1 Name
# - Map Categories, aggregate, weighted Avg Call Time
# - Filters: Month-Year, Category, Name
# - No files written to disk
# ---------------------------

import io
import datetime as dt
from typing import List, Dict

import pandas as pd
import streamlit as st
import yaml
import streamlit_authenticator as stauth


# =========================
# 🔐 AUTHENTICATION (YAML in Secrets)
# =========================
# In Streamlit Cloud → Settings → Secrets (TOML) add:
#
# [auth_config]
# config = """
# credentials:
#   usernames:
#     kelly:
#       name: Kelly Graham
#       email: kgraham@pjilaw.com
#       password: "$2b$12$...."  # bcrypt hash
# cookie:
#   name: referrals_app_cookie
#   key: "LONG_RANDOM_STRING_32+CHARS"
#   expiry_days: 30
# preauthorized:
#   emails:
#     - kgraham@pjilaw.com
# """
#
# (Matches your previous project's pattern.)

st.set_page_config(page_title="Call Reports", page_icon="☎️", layout="wide")

# Load config from secrets and gate the app
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
        st.error("Username/password is incorrect")
        st.stop()
    elif auth_status is None:
        st.warning("Please enter your username and password")
        st.stop()
    else:
        with st.sidebar:
            authenticator.logout("Logout", "sidebar")
            st.caption(f"Signed in as **{name}**")
except Exception as e:
    st.error("Authentication is not configured correctly. Check your **Secrets**.")
    st.exception(e)
    st.stop()


# =========================
# 📋 CONSTANTS & MAPPINGS
# =========================

# Zoom CSV expected columns (as provided)
EXPECTED_COLUMNS: List[str] = [
    "Name", "Email", "Ext.", "Total Calls", "Completed Calls", "Outgoing", "Received",
    "Forwarded to Voicemail", "Answered by Other", "Missed",
    "Avg Call Time", "Total Call Time", "Total Hold Time", "Primary Group"
]

# Whitelisted names (only these rows are relevant)
ALLOWED: List[str] = [
    "Anastasia Economopoulos",
    "Aneesah Shaik",
    "Azariah",
    "Chloe",
    "Donnay",
    "Earl",
    "Faeryal Sahadeo",
    "Kaithlyn",
    "Micayla Sam",
    "Nathanial Beneke",
    "Nobuhle",
    "Rialet",
    "Riekie Van Ellinckhuyzen",  # will be renamed to Maria (display only)
    "Shaylin Steyn",
    "Sihle Gadu",
    "Thabang Tshubyane",
    "Tiffany",
]

# Category mapping (include both original & renamed for safety)
CATEGORY: Dict[str, str] = {
    "Anastasia Economopoulos": "Intake",
    "Aneesah Shaik": "Intake",
    "Azariah": "Intake",
    "Chloe": "Intake IC",
    "Donnay": "Receptionist",
    "Earl": "Intake",
    "Faeryal Sahadeo": "Intake",
    "Kaithlyn": "Intake",
    "Micayla Sam": "Intake",
    "Nathanial Beneke": "Intake",
    "Nobuhle": "Intake IC",
    "Rialet": "Intake",
    "Riekie Van Ellinckhuyzen": "Receptionist",   # original
    "Maria Van Ellinckhuyzen": "Receptionist",    # renamed
    "Shaylin Steyn": "Receptionist",
    "Sihle Gadu": "Intake",
    "Thabang Tshubyane": "Intake",
    "Tiffany": "Intake",
}

# Single rename rule
RENAME_NAME = {"Riekie Van Ellinckhuyzen": "Maria Van Ellinckhuyzen"}

# Output columns order
OUT_COLUMNS = [
    "Category", "Name", "Total Calls", "Completed Calls", "Outgoing", "Received",
    "Forwarded to Voicemail", "Answered by Other", "Missed",
    "Avg Call Time", "Total Call Time", "Total Hold Time", "Month-Year"
]


# =========================
# 🧮 UTILITIES
# =========================

def _to_seconds(series: pd.Series) -> pd.Series:
    """Parse HH:MM:SS or MM:SS strings to seconds (float)."""
    td = pd.to_timedelta(series, errors="coerce")
    return td.dt.total_seconds().fillna(0.0)


def _fmt_hms(seconds: pd.Series) -> pd.Series:
    """Format seconds (float) as H:MM:SS strings."""
    return seconds.round().astype(int).map(lambda s: str(dt.timedelta(seconds=s)))


def process_zoom_csv(raw: pd.DataFrame, period_key: str) -> pd.DataFrame:
    """Clean, filter, map, and aggregate a single Zoom CSV batch for the selected Month-Year."""
    # --- Header normalization: trim leading/trailing spaces (fixes "Completed Calls  ")
    raw.columns = [c.strip() for c in raw.columns]

    # Validate columns
    missing = [c for c in EXPECTED_COLUMNS if c not in raw.columns]
    if missing:
        raise ValueError(f"CSV is missing columns: {missing}")

    df = raw.copy()

    # Keep only allowed Names
    df = df[df["Name"].isin(ALLOWED)].copy()

    # Rename single Name as requested
    df["Name"] = df["Name"].replace(RENAME_NAME)

    # Map Category (fallback to 'Other' if ever needed)
    df["Category"] = df["Name"].map(lambda n: CATEGORY.get(n, "Other"))

    # Drop Email, Ext. (not needed downstream)
    df = df.drop(columns=["Email", "Ext."], errors="ignore")

    # Coerce numeric count columns to int
    count_cols = [
        "Total Calls", "Completed Calls", "Outgoing", "Received",
        "Forwarded to Voicemail", "Answered by Other", "Missed"
    ]
    for c in count_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)

    # Parse durations to seconds
    df["_avg_sec"] = _to_seconds(df["Avg Call Time"])
    df["_total_sec"] = _to_seconds(df["Total Call Time"])
    df["_hold_sec"] = _to_seconds(df["Total Hold Time"])

    # Assign Month-Year (applies to entire upload)
    df["Month-Year"] = period_key

    # Aggregate to one row per Name per Month-Year
    grouped = df.groupby(["Month-Year", "Category", "Name"], as_index=False).agg(
        {
            "Total Calls": "sum",
            "Completed Calls": "sum",
            "Outgoing": "sum",
            "Received": "sum",
            "Forwarded to Voicemail": "sum",
            "Answered by Other": "sum",
            "Missed": "sum",
            "_total_sec": "sum",
            "_hold_sec": "sum",
        }
    )

    # Weighted average of Avg Call Time by Total Calls
    totals = df.groupby(["Month-Year", "Category", "Name"], as_index=False).agg(
        total_calls_sum=("Total Calls", "sum"),
        avg_weighted_sum=("_avg_sec", lambda s: (s * df.loc[s.index, "Total Calls"]).sum()),
    )
    # Avoid division by zero
    totals["avg_sec_weighted"] = totals.apply(
        lambda r: (r["avg_weighted_sum"] / r["total_calls_sum"]) if r["total_calls_sum"] > 0 else 0.0,
        axis=1,
    )

    out = grouped.merge(
        totals[["Month-Year", "Category", "Name", "avg_sec_weighted"]],
        on=["Month-Year", "Category", "Name"],
        how="left",
    )

    # Format durations back to H:MM:SS
    out["Avg Call Time"] = _fmt_hms(out["avg_sec_weighted"])
    out["Total Call Time"] = _fmt_hms(out["_total_sec"])
    out["Total Hold Time"] = _fmt_hms(out["_hold_sec"])

    # Reorder & tidy
    out = out[[
        "Category", "Name",
        "Total Calls", "Completed Calls", "Outgoing", "Received",
        "Forwarded to Voicemail", "Answered by Other", "Missed",
        "Avg Call Time", "Total Call Time", "Total Hold Time", "Month-Year"
    ]].sort_values(["Category", "Name"]).reset_index(drop=True)

    return out


# =========================
# 🖥️ UI
# =========================

st.title("📞 Zoom Call Reports")

st.markdown(
    """
Upload the CSV exported from Zoom, select the Month/Year that the file represents,
and view/filter the processed results. **No files are written to disk** — uploads live only in memory.
"""
)

# Month/Year picker (applies to the current upload)
default_date = dt.date.today().replace(day=1)
period_date = st.date_input(
    "Select the Month/Year this upload represents",
    value=default_date,
)
period_key = f"{period_date.year}-{period_date.month:02d}"

uploaded = st.file_uploader("Upload Zoom CSV", type=["csv"])

# Hold processed batches in memory for this session
if "batches" not in st.session_state:
    st.session_state["batches"] = []

# If a CSV is uploaded, process it immediately
if uploaded:
    try:
        # First pass
        raw = pd.read_csv(uploaded)
        # If headers are odd (BOM/casing), try python engine as fallback
        if set(EXPECTED_COLUMNS) - set([c.strip() for c in raw.columns]):
            uploaded.seek(0)
            raw = pd.read_csv(uploaded, engine="python")

        processed = process_zoom_csv(raw, period_key)
        st.session_state["batches"].append(processed)
        st.success(f"Loaded {len(processed)} row(s) for {period_key}.")
    except Exception as e:
        st.error("Could not parse CSV. Please check the column headers and try again.")
        st.exception(e)

batches = st.session_state["batches"]
if not batches:
    st.info("Upload a CSV to begin.")
    st.stop()

# Combine all uploads (multi-month analysis supported by multiple uploads)
df_all = pd.concat(batches, ignore_index=True)

# Filters
st.subheader("Filters")

def with_all(options):
    """Return sorted list with 'All' prepended."""
    return ["All"] + sorted(options)

col1, col2, col3, col4 = st.columns(4)

# --- Year picklist ---
years = sorted({int(m.split("-")[0]) for m in df_all["Month-Year"].unique()})
sel_year = col1.selectbox("Year", with_all(years), index=0)

# --- Month picklist ---
months_map = {
    "01": "January", "02": "February", "03": "March", "04": "April",
    "05": "May", "06": "June", "07": "July", "08": "August",
    "09": "September", "10": "October", "11": "November", "12": "December"
}
months_available = sorted({m.split("-")[1] for m in df_all["Month-Year"].unique()})
month_names_available = [months_map[m] for m in months_available]
sel_month_name = col2.selectbox("Month", ["All"] + month_names_available, index=0)

# --- Category picklist ---
cat_choices = with_all(df_all["Category"].unique().tolist())
sel_cat = col3.selectbox("Category", cat_choices, index=0)

# --- Name picklist (depends on category) ---
if sel_cat == "All":
    base = df_all
else:
    base = df_all[df_all["Category"] == sel_cat]
name_choices = with_all(base["Name"].unique().tolist())
sel_name = col4.selectbox("Name", name_choices, index=0)

# Build mask
mask = pd.Series(True, index=df_all.index)

if sel_year != "All":
    mask &= df_all["Month-Year"].str.startswith(str(sel_year))

if sel_month_name != "All":
    month_num = [k for k, v in months_map.items() if v == sel_month_name][0]
    mask &= df_all["Month-Year"].str.endswith(month_num)

if sel_cat != "All":
    mask &= df_all["Category"] == sel_cat

if sel_name != "All":
    mask &= df_all["Name"] == sel_name

view = df_all.loc[mask].copy()



# Download filtered CSV (memory only)
csv_buf = io.StringIO()
view.to_csv(csv_buf, index=False)
st.download_button(
    "Download filtered CSV",
    csv_buf.getvalue(),
    file_name="call_report_filtered.csv",
    type="primary"
)

with st.expander("Notes"):
    st.markdown(
        """
- **Whitelisted Names only** are included in the report.
- “**Riekie Van Ellinckhuyzen**” is displayed as “**Maria Van Ellinckhuyzen**”.
- **Avg Call Time** is a **weighted average** by *Total Calls* across the upload.
- **Month-Year** applies to the whole uploaded file; upload additional months to compare.
- Nothing is saved to disk; data remains in session memory only.
"""
    )
