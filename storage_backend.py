from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


PERSISTED_NAMES = {
    "stock_lookup.csv",
    "stores_lookup.csv",
    "stock_updated.txt",
    "stores_updated.txt",
}


class ReferenceStorage:
    """Local-first reference storage with optional Supabase persistence.

    Normal app reads stay local and fast. If Supabase Storage is configured,
    the slim lookup files are copied there whenever an admin replaces them.
    After a Streamlit reboot, missing local lookup files are restored once from
    Supabase and then used from the local cache again.
    """

    def __init__(self, local_dir: Path):
        self.local_dir = Path(local_dir)
        self.local_dir.mkdir(parents=True, exist_ok=True)

        self.supabase_url = self._setting("SUPABASE_URL").rstrip("/")
        self.supabase_key = self._setting("SUPABASE_SERVICE_ROLE_KEY")
        self.bucket = self._setting("SUPABASE_BUCKET") or "direct-shipments"
        self.remote_enabled = bool(self.supabase_url and self.supabase_key)

        if self.remote_enabled:
            self._ensure_bucket()
            self._hydrate_missing_reference_files()

    @staticmethod
    def _setting(name: str) -> str:
        try:
            import streamlit as st

            value = st.secrets.get(name, os.getenv(name, ""))
        except Exception:
            value = os.getenv(name, "")
        return str(value or "").strip()

    def _path(self, name: str) -> Path:
        return self.local_dir / name

    def _headers(self, *, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.supabase_key}",
            "apikey": self.supabase_key,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _ensure_bucket(self) -> None:
        """Create the private bucket if it does not already exist."""
        url = f"{self.supabase_url}/storage/v1/bucket"
        payload = json.dumps({"id": self.bucket, "name": self.bucket, "public": False}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers=self._headers(content_type="application/json"),
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=8).read()
        except urllib.error.HTTPError as exc:
            # 400/409 normally means the bucket already exists. Reads/uploads
            # below will surface any genuine configuration problem.
            if exc.code not in {400, 409}:
                raise

    def _remote_url(self, name: str) -> str:
        safe_name = urllib.parse.quote(name, safe="")
        safe_bucket = urllib.parse.quote(self.bucket, safe="")
        return f"{self.supabase_url}/storage/v1/object/{safe_bucket}/{safe_name}"

    def _download_remote(self, name: str) -> bytes | None:
        request = urllib.request.Request(self._remote_url(name), headers=self._headers(), method="GET")
        try:
            return urllib.request.urlopen(request, timeout=12).read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def _upload_remote(self, name: str, data: bytes, content_type: str) -> None:
        headers = self._headers(content_type=content_type)
        headers["x-upsert"] = "true"
        request = urllib.request.Request(
            self._remote_url(name),
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=15).read()
        except urllib.error.HTTPError as exc:
            # Some Storage versions require PUT for an upsert of an existing object.
            if exc.code not in {400, 409}:
                raise
            request = urllib.request.Request(
                self._remote_url(name),
                data=data,
                headers=headers,
                method="PUT",
            )
            urllib.request.urlopen(request, timeout=15).read()

    def _hydrate_missing_reference_files(self) -> None:
        for name in PERSISTED_NAMES:
            path = self._path(name)
            if path.exists():
                continue
            try:
                payload = self._download_remote(name)
            except Exception:
                # Persistence should never stop the app opening. If Supabase is
                # temporarily unavailable, the admin can still use local data.
                continue
            if payload is not None:
                path.write_bytes(payload)

    def _persist_if_needed(self, name: str, data: bytes, content_type: str) -> None:
        if not self.remote_enabled or name not in PERSISTED_NAMES:
            return
        self._upload_remote(name, data, content_type)

    def save_bytes(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._path(name).write_bytes(data)
        self._persist_if_needed(name, data, content_type)

    def save_dataframe(self, name: str, df) -> None:
        data = df.to_csv(index=False).encode("utf-8")
        self._path(name).write_bytes(data)
        self._persist_if_needed(name, data, "text/csv")

    def load_dataframe(self, name: str):
        path = self._path(name)
        if not path.exists():
            return None
        import pandas as pd

        return pd.read_csv(path, dtype=str, keep_default_na=False)

    def save_text(self, name: str, text: str) -> None:
        data = text.encode("utf-8")
        self._path(name).write_bytes(data)
        self._persist_if_needed(name, data, "text/plain; charset=utf-8")

    def load_text(self, name: str) -> str | None:
        path = self._path(name)
        return path.read_text(encoding="utf-8") if path.exists() else None
