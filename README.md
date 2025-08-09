# Zoom Call Reports — Streamlit

## What this app does
- Login-gated (bcrypt) using Streamlit **Secrets** (no secrets file in repo).
- Upload the Zoom CSV export.
- Assign a Month/Year to the upload.
- Whitelists specific Names, remaps one Name, maps each Name to a Category.
- Parses call durations; sums totals; computes a **weighted** average for Avg Call Time.
- Filter by Month/Year, Category, and Name; download filtered CSV.
- **No files written to disk**.

## Deploy (Streamlit Community Cloud)
1) Push this repo to GitHub (public).
2) In Streamlit Cloud:
   - Create app pointing to `app.py`.
   - Add **Secrets** (Settings → Secrets). Example:

      [users]
      kelly = "$2b$12$REPLACE_WITH_BCRYPT_HASH"
      admin = "$2b$12$ANOTHER_HASH"

3) Run the app and sign in.

## Generate bcrypt password hashes
Run locally (anywhere):

```python
import bcrypt
print(bcrypt.hashpw("YOUR_PASSWORD".encode(), bcrypt.gensalt()).decode())
