from __future__ import annotations

from pathlib import Path


class ReferenceStorage:
    """Simple local storage for the app's reference data."""

    def __init__(self, local_dir: Path):
        self.local_dir = Path(local_dir)
        self.local_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.local_dir / name

    def save_bytes(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        del content_type  # kept for call compatibility
        self._path(name).write_bytes(data)

    def save_dataframe(self, name: str, df) -> None:
        df.to_csv(self._path(name), index=False)

    def load_dataframe(self, name: str):
        path = self._path(name)
        if not path.exists():
            return None
        import pandas as pd

        return pd.read_csv(path, dtype=str, keep_default_na=False)

    def save_text(self, name: str, text: str) -> None:
        self._path(name).write_text(text, encoding="utf-8")

    def load_text(self, name: str) -> str | None:
        path = self._path(name)
        return path.read_text(encoding="utf-8") if path.exists() else None
