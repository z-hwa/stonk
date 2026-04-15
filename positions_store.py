"""
持倉儲存抽象層
  POSITIONS_BACKEND=local  → 本機檔案 cache/_positions.json (預設,本機開發用)
  POSITIONS_BACKEND=gcs    → Google Cloud Storage (Cloud Run / 跨機共用)
"""
import os
import json
from typing import Dict


class PositionsStore:
    def load(self) -> Dict[str, dict]:
        raise NotImplementedError

    def save(self, positions: Dict[str, dict]) -> None:
        raise NotImplementedError

    # 通用便利方法
    def add(self, symbol: str, entry_price: float, entry_date: str) -> Dict[str, dict]:
        positions = self.load()
        positions[symbol.upper()] = {
            "entry_price": float(entry_price),
            "entry_date": entry_date,
        }
        self.save(positions)
        return positions

    def remove(self, symbol: str) -> Dict[str, dict]:
        positions = self.load()
        positions.pop(symbol.upper(), None)
        self.save(positions)
        return positions


class LocalPositionsStore(PositionsStore):
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def load(self) -> Dict[str, dict]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, positions: Dict[str, dict]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(positions, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)


class GCSPositionsStore(PositionsStore):
    def __init__(self, bucket: str, blob_name: str = "_positions.json"):
        # 延遲 import,本機開發不必裝 google-cloud-storage
        from google.cloud import storage  # type: ignore
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket)
        self._blob_name = blob_name

    def load(self) -> Dict[str, dict]:
        blob = self._bucket.blob(self._blob_name)
        if not blob.exists():
            return {}
        try:
            return json.loads(blob.download_as_text())
        except json.JSONDecodeError:
            return {}

    def save(self, positions: Dict[str, dict]) -> None:
        blob = self._bucket.blob(self._blob_name)
        blob.upload_from_string(
            json.dumps(positions, indent=2, ensure_ascii=False),
            content_type="application/json",
        )


def get_store() -> PositionsStore:
    """根據環境變數選擇後端"""
    backend = os.getenv("POSITIONS_BACKEND", "local").lower()
    if backend == "gcs":
        bucket = os.getenv("POSITIONS_GCS_BUCKET")
        if not bucket:
            raise RuntimeError("POSITIONS_BACKEND=gcs 但未設定 POSITIONS_GCS_BUCKET")
        blob = os.getenv("POSITIONS_GCS_BLOB", "_positions.json")
        return GCSPositionsStore(bucket=bucket, blob_name=blob)

    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
    return LocalPositionsStore(os.path.join(cache_dir, "_positions.json"))
