from __future__ import annotations

import io
import os
import re
import zipfile
from datetime import date
from pathlib import Path
from typing import Iterable

import streamlit as st

# Deliberately import pandas and the storage module only after login. This keeps
# the first screen as light as possible when Streamlit wakes the app.
pd = None

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


def _header_index(raw, required: Iterable[str]) -> int:
    required_norm = {x.strip().casefold() for x in required}
    for idx, row in raw.iterrows():
        values = {clean_scalar(v).casefold() for v in row.tolist() if clean_scalar(v)}
        if required_norm.issubset(values):
            return int(idx)
    raise ValueError(f"Could not find a header row containing: {', '.join(required)}")


def _read_tabular_bytes(data: bytes, filename: str, required_header_names: Iterable[str]):
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        last_exc = None
        for encoding in ("utf-8-sig", "cp1252", "latin1"):
            try:
                raw = pd.read_csv(io.BytesIO(data), header=None, dtype=str, encoding=encoding, keep_default_na=False)
                break
            except Exception as exc:
                last_exc = exc
        else:
            raise ValueError(f"Could not read CSV: {last_exc}")
    elif suffix in {".xlsx", ".xlsm"}:
        raw = pd.read_excel(io.BytesIO(data), header=None, dtype=str, keep_default_na=False)
    else:
        raise ValueError(f"Unsupported file type: {suffix}")

    header_idx = _header_index(raw, required_header_names)
    headers = [clean_scalar(v) for v in raw.iloc[header_idx].tolist()]
    body = raw.iloc[header_idx + 1 :].copy()
    body.columns = headers
    body = body.loc[:, [c for c in body.columns if c]]
    return body.reset_index(drop=True)


def parse_store_file(data: bytes, filename: str):
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


def format_store_address(row) -> str:
    parts = [
        clean_scalar(row.get("Address Line 1", "")),
        clean_scalar(row.get("Address Line 2", "")),
        clean_scalar(row.get("Address Line 3", "")),
        clean_scalar(row.get("Address Line 4", "")),
        clean_scalar(row.get("Post Code", "")),
    ]
    return ", ".join([p for p in parts if p])


def parse_stock_zip(data: bytes):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("That file is not a valid ZIP archive.") from exc

    candidates = [
        name for name in zf.namelist()
        if not name.endswith("/") and Path(name).suffix.lower() in {".csv", ".xlsx", ".xlsm"}
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
                out["_search"] = (
                    out["Article Number"].astype(str)
                    + " " + out["Artist Name"].astype(str)
                    + " " + out["Title"].astype(str)
                    + " " + out["Format"].astype(str)
                ).str.casefold()
                return out.reset_index(drop=True), name
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    detail = "; ".join(errors[:3])
    raise ValueError(
        "No usable DPW stock report was found inside the ZIP. "
        "Expected a CSV/Excel file with Article Number, Title, Artist Name, Format and Article Status."
        + (f" ({detail})" if detail else "")
    )


def search_rows(df, query: str, limit: int = 50):
    """Fast vectorised search with likely matches first."""
    terms = [t.casefold() for t in query.split() if t.strip()]
    if not terms:
        return df.iloc[0:0]

    search_text = df["_search"]
    mask = pd.Series(True, index=df.index)
    for term in terms:
        mask &= search_text.str.contains(term, regex=False, na=False)

    matches = df.loc[mask].copy()
    if matches.empty:
        return matches

    needle = query.strip().casefold()
    if "Article Number" in matches.columns:
        primary = matches["Article Number"].str.casefold()
    elif "Ship-To Number" in matches.columns:
        ship_to = matches["Ship-To Number"].str.casefold()
        name = matches["Ship-To Name"].str.casefold()
        primary = ship_to.where(ship_to.str.startswith(needle), name)
    else:
        primary = matches["_search"]

    matches["_rank"] = 2
    matches.loc[primary.str.startswith(needle, na=False), "_rank"] = 1
    matches.loc[primary.eq(needle), "_rank"] = 0
    return matches.sort_values("_rank", kind="stable").drop(columns=["_rank"]).head(limit)


def status_short(text: str) -> str:
    text = clean_scalar(text)
    if not text:
        return ""
    if " - " in text:
        return text.split(" - ", 1)[1]
    return text


def release_label(row) -> str:
    status = status_short(row["Article Status"])
    base = f'{row["Article Number"]} — {row["Artist Name"]} — {row["Title"]} — {row["Format"]}'
    return f"{base} [{status}]" if status else base


def store_label(row) -> str:
    store_no = clean_scalar(row.get("Customer Store Number", ""))
    extra = f" | Store {store_no}" if store_no else ""
    return f'{row["Ship-To Name"]} | Ship-To {row["Ship-To Number"]}{extra} | {row["_address"]}'


@st.cache_data(show_spinner=False)
def _read_lookup_csv(path_text: str, mtime_ns: int):
    del mtime_ns
    path = Path(path_text)
    if not path.exists():
        return None
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def load_reference_data(storage):
    frames = []
    for name in ("stock_lookup.csv", "stores_lookup.csv"):
        path = storage.local_dir / name
        mtime = path.stat().st_mtime_ns if path.exists() else -1
        frames.append(_read_lookup_csv(str(path), mtime))

    releases, stores = frames
    for df in (releases, stores):
        if df is not None:
            for col in df.columns:
                df[col] = df[col].fillna("").map(clean_scalar)
    return releases, stores


def get_reference_data(storage):
    if "_reference_data" not in st.session_state:
        st.session_state["_reference_data"] = load_reference_data(storage)
    return st.session_state["_reference_data"]


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


def render_admin(storage) -> None:
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
                st.session_state.pop("_reference_data", None)
                st.success(f"Stock lookup updated: {len(stock_df):,} catalogue items loaded.")
                st.rerun()
        except Exception as exc:
            st.error(str(exc))

    st.divider()
    store_file = st.file_uploader("Upload/replace store list", type=["csv", "xlsx"], key="store_file")
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
                st.session_state.pop("_reference_data", None)
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


def render_builder(releases, stores) -> None:
    if "shipments" not in st.session_state:
        st.session_state.shipments = []

    if st.session_state.pop("clear_builder_form", False):
        st.session_state["release_query"] = ""
        st.session_state["store_query"] = ""

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
                format_func=lambda i: store_label(store_matches