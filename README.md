# Cargo Direct Shipments

Version 2 adds explicit **User** and **Admin** roles for deployment as a public Streamlit app protected by the app's own login.

Standalone Streamlit app for creating DPW direct-shipment CSV uploads.

## What it does

- Password-protected standalone app.
- Admin uploads the **daily DPW stock ZIP directly**; no manual unzipping.
- The stock importer keeps only: Article Number, Title, Artist Name, Format and Article Status. **Stock quantities are discarded and never displayed.**
- Users search releases by catalogue number, artist or title and must select a valid DPW article.
- Users search destinations by Ship-To number, store name, address, town or postcode.
- Full store address is shown before a Ship-To location is confirmed.
- Deleted/held articles and held customer locations are warned about but not blocked.
- Users can build multiple rows, adjust quantity/date, remove rows, and download a DPW-ready CSV.

Export columns are exactly:

```text
catalogue_number,quantity,account_number,date_shipped,update_stock
```

`account_number` is the **Ship-To Number**, and `update_stock` is always `Y`.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p .streamlit
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
streamlit run app.py
```

Set `USER_PASSWORD` and `ADMIN_PASSWORD` in `.streamlit/secrets.toml` first.

On first run, sign in with the admin password and upload:

1. the latest DPW stock ZIP;
2. the store listing CSV/XLSX.

## Deployment / persistence

The app works with local files for development. Streamlit Community Cloud's writable filesystem should not be treated as durable storage, so for a deployed app configure a **private Supabase Storage bucket** using the three optional Supabase secrets in the example file.

When Supabase is configured, the normalized lookups and latest source files are stored in that private bucket. The Supabase service-role key remains server-side in Streamlit secrets and is never sent to the browser.

The app deliberately stores a normalized stock lookup **without any stock-level columns**.

## DPW input assumptions

### Stock ZIP
The ZIP may contain CSV or Excel files. The app scans for a file whose header contains:

- Article Number
- Title
- Artist Name
- Format
- Article Status

It tolerates the CINRAM report title/preamble rows above the true header.

### Store listing
The store file may be CSV or Excel. It tolerates CINRAM report title/preamble rows and expects at least:

- Ship-To Number
- Ship-To Name
- Address Line 1
- Post Code

It also uses Sold-To Name, Customer Store Number, Address Lines 2–4 and Credit Status when present.

## Authentication

The app uses two separate role passwords:

- `USER_PASSWORD`: create/export shipments
- `ADMIN_PASSWORD`: all user actions plus reference-data uploads

A User cannot access the Admin section. Only Admin can upload/replace the DPW stock ZIP or store list. This keeps the standalone app small while allowing it to be deployed without using a Streamlit private-app slot. Individual named accounts can be added later if required.
