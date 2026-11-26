"""Shared deterministic utilities for Bohrium Harbor Skills (zero third-party dependencies).

Provides:
- Portable SKILL_DIR resolution (works from /skills/weekly-skill or any temp path)
- Atomic NaN/Inf-safe JSON writer (`atomic_write_json`)
- File SHA-256 fingerprinting (`sha256_file`)
- Compact stdout summary protocol (`emit`: <= 2 KB stdout, full JSON persisted to disk)
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sanitize_json(obj: Any, _path: str = "$", _sanitized: list[str] | None = None) -> tuple[Any, list[str]]:
    """Recursively convert numpy/Path/set/NaN/Inf into strict RFC-8259 JSON-safe values."""
    if _sanitized is None:
        _sanitized = []
    if obj is None or isinstance(obj, (bool, str, int)):
        return obj, _sanitized
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            _sanitized.append(f"{_path}={obj}")
            return None, _sanitized
        return obj, _sanitized
    if isinstance(obj, Path):
        return str(obj), _sanitized
    mod = type(obj).__module__
    if mod.startswith("numpy"):
        if hasattr(obj, "item") and getattr(obj, "ndim", 0) == 0:
            return sanitize_json(obj.item(), _path, _sanitized)
        if hasattr(obj, "tolist"):
            return sanitize_json(obj.tolist(), _path, _sanitized)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = str(k)
            val, _ = sanitize_json(v, f"{_path}.{ks}", _sanitized)
            out[ks] = val
        return out, _sanitized
    if isinstance(obj, (list, tuple, set)):
        out_list = []
        for idx, item in enumerate(obj):
            val, _ = sanitize_json(item, f"{_path}[{idx}]", _sanitized)
            out_list.append(val)
        return out_list, _sanitized
    return str(obj), _sanitized


def atomic_write_json(path: str | Path, payload: Any, record_sanitized: bool = True) -> list[str]:
    """Write strict JSON (`allow_nan=False`) atomically via temp file + `os.replace`."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    clean, sanitized = sanitize_json(payload)
    if record_sanitized and sanitized and isinstance(clean, dict):
        clean.setdefault("_sanitized_nonfinite_fields", sanitized)
    text = json.dumps(clean, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return sanitized


def atomic_write_text(path: str | Path, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


def emit(status: str, summary: dict[str, Any], full_payload: dict[str, Any], json_out: str | Path | None = None) -> None:
    """Persist full payload to `json_out` and print a compact <=2KB summary to stdout."""
    out_str = ""
    if json_out is not None:
        atomic_write_json(json_out, full_payload)
        out_str = str(json_out)
    brief = {"status": status, "json_out": out_str, **summary}
    clean_brief, _ = sanitize_json(brief)
    line = json.dumps(clean_brief, ensure_ascii=False, allow_nan=False)
    if len(line) > 1900:
        line = line[:1880] + '..."truncated"}'
    print(f"{status} {out_str}".strip())
    print(line)
    sys.stdout.flush()
