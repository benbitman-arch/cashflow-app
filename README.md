# Cash Flow Calendar

A Streamlit app for deciding how much you can buy each day under different payment terms (cash / 30 / 45 / 60 / 75 days), based on:

- **Outgoing**: Israeli supplier checks (`Checks May14.xls`) + foreign supplier debts (`Suppliers Debt Ben.xls`)
- **Incoming**: customer receivables (`OMD DEBT BEN.xls`), with 34 hardcoded broker/related-entity names auto-excluded

## Run locally

```bash
cd ~/Desktop/cashflow_app
.venv/bin/streamlit run app.py
```

Open http://localhost:8501.

## Deploy to Streamlit Community Cloud (free)

1. `cd ~/Desktop/cashflow_app && git init && git add . && git commit -m "init"`
2. Create a private GitHub repo and `git push -u origin main`.
3. Go to https://share.streamlit.io → **New app** → pick the repo, branch `main`, main file `app.py`.
4. After it deploys, share the URL with your boss.

## Notes on persistence

The app stores the last parsed snapshot in `cashflow.db` (SQLite). On Streamlit Cloud the container filesystem resets on each redeploy — to keep the snapshot across deploys, commit `cashflow.db` to the repo after pressing "Save snapshot" (or replace `persistence.py` with a Postgres backend like Neon free tier).

## Adding new excluded customer names

Type the name in the sidebar's "Exclude list" section and click **Add**. The name is appended to `exclude_list.py` and applies on the next re-parse.

## Column matching

Parsers find columns by *normalized name* (whitespace-collapsed, case-insensitive), so future uploads with slightly shifted headers or column order still work — as long as the column names themselves don't change.
