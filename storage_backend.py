from __future__ import annotations

import io
import os
from pathlib import Path

import pandas as pd
import streamlit as st

try:
    from supabase import create_client
except Exception:  # pragma: no cover
    create_client = None


class ReferenceStorage:
    """Persist reference data locally, or in Supabase Storage when configured."""

    def __init__(self, local_dir: Path):
        self.local_dir = Path(local_dir)
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.bucket = self._secret("SUPABASE_BUCKET")
        self.url = self._secret("SUPABASE_URL")
        self.key = self._secret("SUPABASE_SERVICE_ROLE_KEY")
        self.remote = bool(self.bucket and self.url and self.key and create_client)
        self.client = create_client(self.url, self.key) if self.remote else None

    @staticmethod
    def _secret(name: str) -> str:
        try:
            return str(st.secrets.get(name, os.getenv(name, "")) or "")
        except Exception:
            return str(os.getenv(name, "") or "")

    def _download(self, name: str) -> bytes | None:
        if self.remote:
            try:
                return self.client.storage.from_(self.bucket).download(name)
            except Exception:
                return None
        path = self.local_dir / name
        return path.read_bytes() if path.exists() else None

    def save_bytes(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        if self.remote:
            self.client.storage.from_(self.bucket).upload(
                path=name,
                file=data,
                file_options={"content-type": content_type, "upsert": "true"},
            )
            return
        (self.local_dir / name).write_bytes(data)

    def save_dataframe(self, name: str, df: pd.DataFrame) -> None:
        payload = df.to_csv(index=False).encode("utf-8")
        self.save_bytes(name, payload, content_type="text/csv")

    def load_dataframe(self, name: str) -> pd.DataFrame | None:
        payload = self._download(name)
        if payload is None:
            return None
        return pd.read_csv(io.BytesIO(payload), dtype=str, keep_default_na=False)

    def save_text(self, name: str, text: str) -> None:
        self.save_bytes(name, text.encode("utf-8"), content_type="text/plain")

    def load_text(self, name: str) -> str | None:
        payload = self._download(name)
        return payload.decode("utf-8") if payload else None
