import io
import datetime as dt
from typing import Dict, Tuple

import pandas as pd
import streamlit as st

# Optional local secrets shim (useful for local dev without Streamlit Cloud)
# Put a non-committed ".secrets.yaml" next to app.py if you want local testing.
# This block never runs on Streamlit Cloud and is safe in a public repo.
try:
    import os, pathlib, yaml  # type: ignore
    local_yaml = pathlib.Path(".secrets.yaml")
    if local_yaml.exists():
        with local_yaml.open("r", encoding="utf-8") as f:
            _local_secrets = yaml.safe_load(f) or {}
        class _Shim(dict):
            def get(self, k, default=None):
                return super().get(k, default)
        st.secrets = _Shim(_local_secrets)  # type: ignore
except Exception:
    pass

try:
    import bcrypt
except Exception:
    st.stop()

st.set_page_config(page_title="Call Reports (Zoom)", page_icon="☎️", layout="wide")

# ---------- Security: password check ----------
def _check_password() -> bool:
    if st.session_state.get("auth_ok"):
        return True

    st.title("Sign in")
    with st.form("login", clear_on_submit=False):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")

    if submitted:
        users: Dict[str, str] = st.secrets.get("users", {})  # {"username": "<bcrypt-hash>"}
        hashed = users.get(u)
        if isinstance(hashed, str):
            try:
                ok = bcrypt.checkpw(p.encode("utf-8"), hashed.encode("utf-8"))
            except Exception:
                ok = False
            if ok:
                st.session_state["auth_ok"] = True
                st.rerun()
        st.error("Invalid credentials.")
    st.stop()

if not _check_password():
    st.stop()

# ---------- App UI ----------
st.title("📞 Zoom Call Reports")

st.markdown(
    "Upload the CSV exported from Zoom. Assign a Month/Year to this upload. "
    "Data is filtered to your approved Names list, mapped to Categories, and displayed with convenient filters. "
    "**No files are written to disk.**"
)

# Reporting period picker
period_date = st.date_input(
    "Select the Month/Year this upload represents",
    value=dt.date.today(),
)
period_key = f"{period_date.year}-{period_date.month:02d}"

uploaded = st.file_uploader("Upload Zoom CSV", type=["csv"])

# Whitelist of Names
ALLOWED = [
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
    "Riekie Van Ellinckhuyzen",
    "Shaylin Steyn",
    "Sihle Gadu",
    "Thabang Tshubyane",
    "Tiffany",
]

# Category mapping
CATEGORY = {
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
    "Riekie Van Ellinckhuyzen": "Receptionist",
    "Shaylin Steyn": "Receptionist",
    "Sihle Gadu": "Intake",
    "Thabang Tshubyane": "Intake",
    "Tiffany": "Intake",
}

RENAME_NAME = {"Riekie Van Ellinckhuyzen": "Maria Van Ellinckhuyzen"}

EXPECTED_COLUMNS = [
    "Name", "Email", "Ext.", "Total Calls", "Completed Calls", "Outgoing", "Received",
    "Forwarded to Voicemail", "Answered by Other", "Missed",
    "Avg Call Time", "Total Call Time", "Total Hold Time", "Primary Group"
]

OUT_COLUMNS = [
    "Category", "Name", "Total Calls", "Completed Calls", "Outgoing", "Received",
    "Forwarded to Voicemail", "Answered by Other", "Missed",
    "Avg Call Time", "Total Call Time", "Total Hold Time", "Month-Year"
]

def _to_seconds(s: pd.Series) -> pd.Series:
    # Handles "HH:MM:SS" or "MM:SS"
    td = pd.to_timedelta(s, errors="coerce")
    return td.dt.total_seconds().fillna(0)

def _fmt_hms(seconds: pd.Series) -> pd.Series:
    return seconds.round().astype(int).map(lambda x: str(dt.timedelta(seconds=x)))

def process(df: pd.DataFrame, period_key: str) -> pd.DataFrame:
    # Validate & align columns
    missing = [c for c in EXPECTED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing columns: {missing}")

    # Keep only allowed Names
    df = df[df["Name"].isin(ALLOWED)].copy()

    # Rename the single Name as requested
    df["Name"] = df["Name"].replace(RENAME_NAME)

    # Map to Category
    df["Category"] = df["Name"].map(lambda n: CATEGORY.get(n, "Other"))

    # Drop Email, Ext.
    df = df.drop(columns=["Email", "Ext."], errors="ignore")

    # Coerce numeric columns
    numeric_cols = [
        "Total Calls", "Completed Calls", "Outgoing", "Received",
        "Forwarded to Voicemail", "Answered by Other", "Missed"
    ]
    for c in numeric_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)

    # Durations
    df["_avg_sec"] = _to_seconds(df["Avg Call Time"])
    df["_total_sec"] = _to_seconds(df["Total Call Time"])
    df["_hold_sec"] = _to_seconds(df["Total Hold Time"])

    # Assign Month-Year
    df["Month-Year"] = period_key

    # Aggregate to one row per Name per Month-Year (summing counts, summing totals, weighted avg for Avg)
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
    w = (
        df.assign(_w=df["Total Calls"])
          .groupby(["Month-Year", "Category", "Name"], as_index=False)
          .apply(lambda g: pd.Series({
              "_avg_sec": (g["_avg_sec"] * g["_w"]).sum() / max(g["_w"].sum(), 1)
          }))
    )
    out = grouped.merge(w, on=["Month-Year", "Category", "Name"], how="left")

    # Format durations
    out["Avg Call Time"]   = _fmt_hms(out["_avg_sec"])
    out["Total Call Time"] = _fmt_hms(out["_total_sec"])
    out["Total Hold Time"] = _fmt_hms(out["_hold_sec"])

    # Order columns
    out = out[[
        "Category", "Name", "Total Calls", "Completed Calls", "Outgoing", "Received",
        "Forwarded to Voicemail", "Answered by Other", "Missed",
        "Avg Call Time", "Total Call Time", "Total Hold Time", "Month-Year"
    ]]

    return out.sort_values(["Category", "Name"]).reset_index(drop=True)

# ---------- Process upload ----------
all_data = []
if uploaded:
    try:
        raw = pd.read_csv(uploaded)
        if set(EXPECTED_COLUMNS) - set(raw.columns):
            # Sometimes Zoom includes BOM / different header casing; try second pass with engine python
            uploaded.seek(0)
            raw = pd.read_csv(uploaded, engine="python")
        processed = process(raw, period_key)
        st.success(f"Loaded {len(processed)} rows for {period_key}.")
        # Store this upload in session only
        st.session_state.setdefault("batches", [])
        st.session_state["batches"].append(processed)
    except Exception as e:
        st.error(f"Could not parse CSV: {e}")

batches = st.session_state.get("batches", [])
if not batches:
    st.info("Upload a CSV to begin.")
    st.stop()

df_all = pd.concat(batches, ignore_index=True)

# ---------- Filters ----------
st.subheader("Filters")
col1, col2, col3 = st.columns(3)

with col1:
    month_choices = sorted(df_all["Month-Year"].unique().tolist())
    month_sel = st.multiselect("Month-Year", month_choices, default=month_choices)

with col2:
    cat_choices = sorted(df_all["Category"].unique().tolist())
    cat_sel = st.multiselect("Category", cat_choices, default=cat_choices)

with col3:
    name_choices = sorted(df_all["Name"].unique().tolist())
    name_sel = st.multiselect("Name", name_choices, default=name_choices)

mask = (
    df_all["Month-Year"].isin(month_sel)
    & df_all["Category"].isin(cat_sel)
    & df_all["Name"].isin(name_sel)
)
view = df_all.loc[mask].copy()

st.subheader("Results")
st.dataframe(
    view[
        [
            "Category", "Name", "Total Calls", "Completed Calls", "Outgoing", "Received",
            "Forwarded to Voicemail", "Answered by Other", "Missed",
            "Avg Call Time", "Total Call Time", "Total Hold Time", "Month-Year"
        ]
    ],
    use_container_width=True,
    hide_index=True,
)

# Download (memory only)
csv_buf = io.StringIO()
view.to_csv(csv_buf, index=False)
st.download_button("Download filtered CSV", csv_buf.getvalue(), file_name="call_report_filtered.csv", type="primary")
