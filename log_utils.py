# log_utils.py
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from config import LOGS_DIR


def save_cleaning_json(result: dict[str, Any], file_id: str) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(file_id))
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOGS_DIR / f"cleaning_{safe}_{ts}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, default=str)
    return path


def list_logs() -> list[dict]:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(LOGS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:50]:
        out.append({
            "file_name":   p.name,
            "size_kb":     round(p.stat().st_size / 1024, 1),
            "modified_at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
        })
    return out


def get_log_path(file_name: str) -> Path:
    path = LOGS_DIR / Path(file_name).name
    if not path.exists() or path.suffix.lower() != ".json":
        raise FileNotFoundError(file_name)
    return path
