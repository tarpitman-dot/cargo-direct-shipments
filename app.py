from __future__ import annotations

import io
import os
import re
import zipfile
from datetime import date
from pathlib import Path
from typing import Iterable

import pandas as pd
import streamlit as st

from storage_backend import ReferenceStorage

APP_TITLE = "Direct Shipments"
DATA_DIR = Path("data")

RELEASE_REQUIRED = ["Article Number", "Title", "Artist Name", "Format", "Article Status"]
STORE_REQUIRED = ["Ship-To Number", "Ship-To Name", "Address Line 1", "Post Code"]
STORE_OPTIONAL = [
    "Sold-To Name",
    "Customer Store Number",
    "Address Line 2",
    "Address Line 3",
    "Address Line 4",
    "Credit Status",
]


def clean_scalar(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if re.fullmatch(r"-?\d+\.0", text):
        text = text[:-2]
    return re.sub(r"\s+", " ", text)


def _header_index(raw: pd.DataFrame, required: Iterable[str]) -> int:
    required_norm = {x.strip().casefold() for x in required}
    for idx, row in raw.iterrows():
        values = {clean_scalar(v).casefold() for v in row.tolist() if clean_scalar(v)}
        if required_norm.issubset(values):
            return int(idx)
    raise ValueError(f"Could not find a header row containing: {', '.join(required)}")


def _read_tabular_bytes(data: bytes, filename: str, required_header_names: Iterable[str]) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        last_exc = None
        for encoding in ("utf-8-sig", "cp1252", "latin1"):
            try:
                raw = pd.read_csv(io.BytesIO(data), header=None, dtype=str, encoding=encoding, keep_default_na=False)
                break
            except Exception as exc:  # pragma: no cover - fallback path
                last_exc = exc
        else:
            raise ValueError(f"Could not read CSV: {last_exc}")
    elif suffix in {".xlsx", ".xlsm", ".xls"}:
        raw = pd.read_excel(io.BytesIO(data), header=None, dtype=str, keep_default_na=False)
    else:
        raise ValueError(f"Unsupported file type: {suffix}")

    header_idx = _header_index(raw, required_header_names)
    headers = [clean_scalar(v) for v in raw.iloc[header_idx].tolist()]
    body = raw.iloc[header_idx + 1 :].copy()
    body.columns = headers
    body = body.loc[:, [c for c in body.columns if c]]
    return body.reset_index(drop=True)


def parse_store_file(data: bytes, filename: str) -> pd.DataFrame:
    df = _read_tabular_bytes(data, filename, STORE_REQUIRED)
    missing = [c for c in STORE_REQUIRED if c not in df.columns]
    if missing:
        raise ValueError("Store file is missing: " + ", ".join(missing))

    cols = STORE_REQUIRED + [c for c in STORE_OPTIONAL if c in df.columns]
    out = df[cols].copy()
    for c in out.columns:
        out[c] = out[c].map(clean_scalar)

    out = out[out["Ship-To Number"] != ""].drop_duplicates(subset=["Ship-To Number"], keep="first")
    out["_address"] = out.apply(format_store_address, axis=1)
    out["_search"] = out.apply(
        lambda r: " ".join(
            clean_scalar(r.get(c, ""))
            for c in [
                "Ship-To Number",
                "Ship-To Name",
                "Sold-To Name",
                "Customer Store Number",
                "Address Line 1",
                "Address Line 2",
                "Address Line 3",
                "Address Line 4",
                "Post Code",
            ]
        ).casefold(),
        axis=1,
    )
    return out.reset_index(drop=True)


def format_store_address(row: pd.Series) -> str:
    parts = [
        clean_scalar(row.get("Address Line 1", "")),
        clean_scalar(row.get("Address Line 2", "")),
        clean_scalar(row.get("Address Line 3", "")),
        clean_scalar(row.get("Address Line 4", "")),
        clean_scalar(row.get("Post Code", "")),
    ]
    return ", ".join([p for p in parts if p])


def parse_stock_zip(data: bytes) -> tuple[pd.DataFrame, str]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("That file is not a valid ZIP archive.") from exc

    candidates = [
        name for name in zf.namelist()
        if not name.endswith("/") and Path(name).suffix.lower() in {".csv", ".xlsx", ".xlsm", ".xls"}
    ]
    candidates.sort(key=lambda n: ("stock" not in n.casefold(), n.casefold()))
    errors: list[str] = []
    for name in candidates:
        try:
            payload = zf.read(name)
            df = _read_tabular_bytes(payload, name, RELEASE_REQUIRED)
            if all(c in df.columns for c in RELEASE_REQUIRED):
                out = df[RELEASE_REQUIRED].copy()
                for c in out.columns:
                    out[c] = out[c].map(clean_scalar)
                out = out[out["Article Number"] != ""].drop_duplicates(subset=["Article Number"], keep="first")
                # Deliberately retain no stock-level columns.
                out["_search"] = out.apply(
                    lambda r: " ".join(
                        [r["Article Number"], r["Artist Name"], r["Title"], r["Format"]]
                    ).casefold(),
                    axis=1,
                )
                return out.reset_index(drop=True), name
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    detail = "; ".join(errors[:3])
    raise ValueError(
        "No usable DPW stock report was found inside the ZIP. "
        "Expected a CSV/Excel file with Article Number, Title, Artist Name, Format and Article Status."
        + (f" ({detail})" if detail else "")
    )


def search_rows(df: pd.DataFrame, query: str, limit: int = 50) -> pd.DataFrame:
    terms = [t.casefold() for t in query.split() if t.strip()]
    if not terms:
        return df.iloc[0:0]
    mask = df["_search"].map(lambda text: all(term in text for term in terms))
    return df[mask].head(limit)


def status_short(text: str) -> str:
    text = clean_scalar(text)
    if not text:
        return ""
    if " - " in text:
        return text.split(" - ", 1)[1]
    return text


def release_label(row: pd.Series) -> str:
    status = status_short(row["Article Status"])
    base = f'{row["Article Number"]} — {row["Artist Name"]} — {row["Title"]} — {row["Format"]}'
    return f"{base} [{status}]" if status else base


def store_label(row: pd.Series) -> str:
    store_no = clean_scalar(row.get("Customer Store Number", ""))
    extra = f" | Store {store_no}" if store_no else ""
    return f'{row["Ship-To Name"]} | Ship-To {row["Ship-To Number"]}{extra} | {row["_address"]}'


def load_reference_data(storage: ReferenceStorage) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    releases = storage.load_dataframe("stock_lookup.csv")
    stores = storage.load_dataframe("stores_lookup.csv")
    for df in (releases, stores):
        if df is not None:
            for col in df.columns:
                df[col] = df[col].fillna("").map(clean_scalar)
    return releases, stores


def _secret(name: str) -> str:
    try:
        return str(st.secrets.get(name, os.getenv(name, "")) or "")
    except Exception:
        return str(os.getenv(name, "") or "")


def require_login() -> None:
    user_password = _secret("USER_PASSWORD")
    admin_password = _secret("ADMIN_PASSWORD")

    if "role" not in st.session_state:
        st.session_state.role = None

    if st.session_state.role:
        return

    st.title(APP_TITLE)
    st.caption("Cargo direct-shipment CSV builder")
    with st.form("login"):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", use_container_width=True)
    if submitted:
        if admin_password and password == admin_password:
            st.session_state.role = "admin"
            st.rerun()
        if user_password and password == user_password:
            st.session_state.role = "user"
            st.rerun()
        st.error("Incorrect password.")
    if not user_password or not admin_password:
        st.info("Set USER_PASSWORD and ADMIN_PASSWORD in Streamlit secrets before deploying.")
    st.stop()


def render_admin(storage: ReferenceStorage) -> None:
    st.subheader("Reference data")
    st.caption("Stock quantities are discarded during import and are never shown in this app.")

    stock_zip = st.file_uploader("Upload latest DPW stock ZIP", type=["zip"], key="stock_zip")
    if stock_zip is not None:
        try:
            stock_df, source_name = parse_stock_zip(stock_zip.getvalue())
            st.success(f"Valid stock report found: {source_name} — {len(stock_df):,} catalogue items.")
            preview = stock_df[["Article Number", "Artist Name", "Title", "Format", "Article Status"]].head(10)
            st.dataframe(preview, use_container_width=True, hide_index=True)
            if st.button("Replace current stock lookup", type="primary"):
                storage.save_dataframe("stock_lookup.csv", stock_df)
                storage.save_bytes("latest_stock.zip", stock_zip.getvalue(), content_type="application/zip")
                storage.save_text("stock_updated.txt", pd.Timestamp.utcnow().isoformat())
                st.success(f"Stock lookup updated: {len(stock_df):,} catalogue items loaded.")
                st.rerun()
        except Exception as exc:
            st.error(str(exc))

    st.divider()
    store_file = st.file_uploader("Upload/replace store list", type=["csv", "xlsx", "xls"], key="store_file")
    if store_file is not None:
        try:
            stores_df = parse_store_file(store_file.getvalue(), store_file.name)
            st.success(f"Valid store list — {len(stores_df):,} Ship-To locations.")
            st.dataframe(
                stores_df[["Ship-To Number", "Ship-To Name", "_address"]].head(10),
                use_container_width=True,
                hide_index=True,
            )
            if st.button("Replace current store lookup", type="primary"):
                storage.save_dataframe("stores_lookup.csv", stores_df)
                storage.save_bytes(f"latest_stores{Path(store_file.name).suffix.lower()}", store_file.getvalue())
                storage.save_text("stores_updated.txt", pd.Timestamp.utcnow().isoformat())
                st.success(f"Store lookup updated: {len(stores_df):,} Ship-To locations loaded.")
                st.rerun()
        except Exception as exc:
            st.error(str(exc))

    st.divider()
    stock_updated = storage.load_text("stock_updated.txt")
    stores_updated = storage.load_text("stores_updated.txt")
    if stock_updated:
        st.caption(f"Stock lookup last updated: {stock_updated}")
    if stores_updated:
        st.caption(f"Store lookup last updated: {stores_updated}")


def render_builder(releases: pd.DataFrame, stores: pd.DataFrame) -> None:
    if "shipments" not in st.session_state:
        st.session_state.shipments = []

    st.subheader("New direct shipment")

    release_query = st.text_input(
        "Release",
        placeholder="Start typing catalogue number, artist or title…",
        key="release_query",
    )
    release_matches = search_rows(releases, release_query) if release_query else releases.iloc[0:0]
    selected_release = None
    if release_query:
        if release_matches.empty:
            st.warning("No matching catalogue item found in the current DPW stock list.")
        else:
            options = list(release_matches.index)
            selected_idx = st.selectbox(
                "Confirm release",
                options=options,
                format_func=lambda i: release_label(release_matches.loc[i]),
                index=None,
                placeholder="Choose the correct release…",
            )
            if selected_idx is not None:
                selected_release = release_matches.loc[selected_idx]
                status = selected_release["Article Status"]
                if status.startswith("92") or "delete" in status.casefold():
                    st.warning(f"DPW status: {status}. You can still use this article.")
                elif status:
                    st.caption(f"DPW status: {status}")

    store_query = st.text_input(
        "Ship to",
        placeholder="Start typing store name, town, postcode or Ship-To number…",
        key="store_query",
    )
    store_matches = search_rows(stores, store_query) if store_query else stores.iloc[0:0]
    selected_store = None
    if store_query:
        if store_matches.empty:
            st.warning("No matching Ship-To location found.")
        else:
            store_options = list(store_matches.index)
            selected_store_idx = st.selectbox(
                "Confirm store / branch",
                options=store_options,
                format_func=lambda i: store_label(store_matches.loc[i]),
                index=None,
                placeholder="Choose the correct Ship-To location…",
            )
            if selected_store_idx is not None:
                selected_store = store_matches.loc[selected_store_idx]
                st.info(
                    f'**{selected_store["Ship-To Name"]}**  \n'
                    f'Ship-To **{selected_store["Ship-To Number"]}**  \n'
                    f'{selected_store["_address"]}'
                )
                credit = clean_scalar(selected_store.get("Credit Status", ""))
                if credit and not credit.startswith("A"):
                    st.warning(f"Customer status: {credit}. You can still use this Ship-To location.")

    c1, c2 = st.columns(2)
    with c1:
        qty = st.number_input("Quantity shipped", min_value=1, step=1, value=1)
    with c2:
        shipped_date = st.date_input("Date shipped", value=date.today())

    ready = selected_release is not None and selected_store is not None and int(qty) > 0
    if st.button("Add shipment", type="primary", disabled=not ready, use_container_width=True):
        st.session_state.shipments.append(
            {
                "catalogue_number": selected_release["Article Number"],
                "release": f'{selected_release["Artist Name"]} — {selected_release["Title"]} — {selected_release["Format"]}',
                "quantity": int(qty),
                "account_number": selected_store["Ship-To Number"],
                "ship_to": selected_store["Ship-To Name"],
                "address": selected_store["_address"],
                "date_shipped": shipped_date.isoformat(),
                "update_stock": "Y",
            }
        )
        st.session_state.release_query = ""
        st.session_state.store_query = ""
        st.rerun()

    if st.session_state.shipments:
        st.divider()
        st.subheader("Shipment rows")
        edit_df = pd.DataFrame(st.session_state.shipments)
        edit_df.insert(0, "remove", False)
        edited = st.data_editor(
            edit_df,
            use_container_width=True,
            hide_index=True,
            disabled=["catalogue_number", "release", "account_number", "ship_to", "address", "update_stock"],
            column_config={
                "remove": st.column_config.CheckboxColumn("Remove"),
                "quantity": st.column_config.NumberColumn("Quantity", min_value=1, step=1),
                "date_shipped": st.column_config.TextColumn("Date shipped", help="YYYY-MM-DD"),
            },
            key="shipment_editor",
        )

        e1, e2 = st.columns(2)
        with e1:
            if st.button("Apply changes / remove checked", use_container_width=True):
                edited = edited[~edited["remove"].fillna(False)].drop(columns=["remove"])
                # Validate editable values before committing.
                try:
                    edited["quantity"] = pd.to_numeric(edited["quantity"], errors="raise").astype(int)
                    if (edited["quantity"] <= 0).any():
                        raise ValueError("Quantity must be at least 1.")
                    parsed_dates = pd.to_datetime(edited["date_shipped"], format="%Y-%m-%d", errors="raise")
                    edited["date_shipped"] = parsed_dates.dt.strftime("%Y-%m-%d")
                    edited["update_stock"] = "Y"
                    st.session_state.shipments = edited.to_dict("records")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not apply changes: {exc}")
        with e2:
            if st.button("Clear all", use_container_width=True):
                st.session_state.shipments = []
                st.rerun()

        export_df = pd.DataFrame(st.session_state.shipments)[
            ["catalogue_number", "quantity", "account_number", "date_shipped", "update_stock"]
        ]
        csv_bytes = export_df.to_csv(index=False, lineterminator="\n").encode("utf-8")
        st.download_button(
            "Download DPW CSV",
            data=csv_bytes,
            file_name=f"direct_shipments_{date.today().isoformat()}.csv",
            mime="text/csv",
            type="primary",
            use_container_width=True,
        )


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="📦", layout="wide")
    require_login()

    storage = ReferenceStorage(DATA_DIR)
    releases, stores = load_reference_data(storage)

    with st.sidebar:
        st.title(APP_TITLE)
        st.caption(f"Signed in as {st.session_state.role}")
        if st.button("Sign out"):
            st.session_state.clear()
            st.rerun()

    tabs = ["Create shipment"]
    if st.session_state.role == "admin":
        tabs.append("Admin")
    selected = st.radio("Section", tabs, horizontal=True, label_visibility="collapsed")

    if selected == "Admin":
        render_admin(storage)
        return

    if releases is None or stores is None:
        st.warning("Reference data has not been loaded yet. An admin needs to upload the DPW stock ZIP and store list.")
        st.stop()

    render_builder(releases, stores)


if __name__ == "__main__":
    main()
