#!/usr/bin/env python3
"""Deterministic, read-only reconnaissance for arbitrary scientific input directories."""
from __future__ import annotations

import argparse
import csv
import importlib
import io
import json
import math
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SKILL_DIR, atomic_write_json, emit, sha256_file, utc_now_iso

try:
    import sqlite3
except Exception as exc:  # pragma: no cover - depends on the Python build
    sqlite3 = None
    SQLITE_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
else:
    SQLITE_IMPORT_ERROR = None

SENTINELS = {"", "?", "na", "n/a", "null", "none", "nan", "-999", "-999.0", "-", r"\n"}
CENSORED_RE = re.compile(
    r"^\s*<\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*$"
)
NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
FILENAME_RE = re.compile(
    r"(?<![\w.-])[\w.@+-]+(?:[ /\\][\w.@+ -]+)*\.(?:csv|tsv|json|sqlite3?|db|txt|md|ya?ml|py|png|pdf|svg)(?![\w.-])",
    re.IGNORECASE,
)
ENV_NAMES = (
    "MPLBACKEND",
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "PYTHONUNBUFFERED",
    "PYTHONHASHSEED",
    "LANG",
    "LC_ALL",
)
FALLBACKS = {
    "numpy": "Use math, statistics, csv, and explicit loops; vectorized acceleration is unavailable.",
    "pandas": "Use csv.DictReader/csv.reader and sqlite3; preserve column names as raw strings.",
    "scipy": "Use math and statistics for supported calculations; report unsupported numerical routines explicitly.",
    "sklearn": "Use deterministic stdlib algorithms, or numpy when available; validate any hand-written model.",
    "matplotlib": "Generate a simple editable SVG with stdlib or report that rendering is unavailable; do not fabricate plots.",
    "sqlite3": "CSV/JSON profiling remains available; SQLite querying requires a Python build with sqlite3 support.",
}


def decode_bytes(raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    for encoding in ("utf-8", "cp1252"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def is_number(value: str) -> bool:
    return bool(NUMBER_RE.fullmatch(value.strip()))


def finite_number(value: str) -> float | None:
    if not is_number(value):
        return None
    try:
        number = float(value.strip())
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def detect_format(path: Path, prefix: bytes) -> str:
    suffix = path.suffix.casefold()
    if prefix.startswith(b"SQLite format 3\x00") or suffix in {".sqlite", ".sqlite3", ".db"}:
        return "SQLite"
    if suffix == ".csv":
        return "CSV"
    if suffix == ".tsv":
        return "TSV"
    if suffix == ".json":
        return "JSON"
    if suffix in {".txt", ".md", ".yaml", ".yml", ".py", ".log"}:
        return "TXT"
    stripped = prefix.lstrip()
    if stripped.startswith((b"{", b"[")):
        return "JSON"
    if b"\x00" not in prefix:
        try:
            sample = prefix.decode("utf-8")
        except UnicodeDecodeError:
            return "BINARY"
        first = sample.splitlines()[0] if sample.splitlines() else ""
        if "\t" in first:
            return "TSV"
        if "," in first:
            return "CSV"
        return "TXT"
    return "BINARY"


def header_decision(rows: list[list[str]], sniffer_header: bool | None) -> tuple[bool, dict[str, Any]]:
    first_all_strings = bool(rows and all(cell.strip() and not is_number(cell) for cell in rows[0]))
    numeric_evidence = False
    if first_all_strings and len(rows) > 1:
        for index in range(len(rows[0])):
            if any(index < len(row) and is_number(row[index]) for row in rows[1:6]):
                numeric_evidence = True
                break
    unique_first = bool(rows and len(set(rows[0])) == len(rows[0]))
    single_row_header = len(rows) == 1 and first_all_strings and unique_first
    has_header = bool(first_all_strings and (numeric_evidence or sniffer_header or single_row_header))
    return has_header, {
        "method": "csv.Sniffer plus first-row all-string heuristic",
        "first_row_all_nonnumeric_strings": first_all_strings,
        "later_numeric_evidence": numeric_evidence,
        "sniffer_has_header": sniffer_header,
    }


def whitespace_variants(values: list[str]) -> list[dict[str, Any]]:
    groups: dict[str, Counter[str]] = defaultdict(Counter)
    for value in values:
        groups[" ".join(value.split())][value] += 1
    variants = []
    for normalized, counts in groups.items():
        if len(counts) > 1 or any(value != normalized for value in counts):
            variants.append(
                {
                    "normalized": normalized,
                    "variants": [
                        {"value": value, "count": count}
                        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
                    ],
                }
            )
    return sorted(variants, key=lambda item: item["normalized"])


def profile_delimited(path: Path, file_format: str) -> dict[str, Any]:
    raw = path.read_bytes()
    text, encoding = decode_bytes(raw)
    sample = text[:65536]
    default_delimiter = "\t" if file_format == "TSV" else ","
    delimiter = default_delimiter
    sniffer_header: bool | None = None
    sniffer_error = None
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        delimiter = dialect.delimiter
    except csv.Error as exc:
        sniffer_error = str(exc)
    try:
        sniffer_header = csv.Sniffer().has_header(sample)
    except (csv.Error, ValueError) as exc:
        sniffer_error = str(exc)

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    parsed_rows = list(reader)
    has_header, header_evidence = header_decision(parsed_rows[:6], sniffer_header)
    trailing_empty_rows = 0
    for row in reversed(parsed_rows):
        if not row or all(cell == "" for cell in row):
            trailing_empty_rows += 1
        else:
            break
    body = parsed_rows[1:] if has_header and parsed_rows else parsed_rows
    if trailing_empty_rows:
        body = body[:-trailing_empty_rows]
    empty_rows = sum(1 for row in body if not row or all(cell == "" for cell in row))
    data_rows = [row for row in body if row and not all(cell == "" for cell in row)]
    width = max((len(row) for row in parsed_rows), default=0)
    if has_header and parsed_rows:
        columns = list(parsed_rows[0])
    else:
        columns = [f"column_{index + 1}" for index in range(width)]
    if len(columns) < width:
        columns.extend(f"column_{index + 1}" for index in range(len(columns), width))

    duplicate_rows = len(data_rows) - len({tuple(row) for row in data_rows})
    ragged_rows = sum(1 for row in data_rows if len(row) != width)
    column_profiles = []
    for index, name in enumerate(columns):
        values = [row[index] if index < len(row) else "" for row in data_rows]
        missing = Counter()
        censored = Counter()
        nonfinite = 0
        valid_values = []
        numeric_values = []
        numeric_like_count = 0
        for value in values:
            stripped = value.strip()
            normalized = stripped.casefold()
            if normalized in SENTINELS:
                missing[normalized] += 1
                continue
            if CENSORED_RE.fullmatch(value):
                censored[stripped] += 1
                continue
            valid_values.append(value)
            if is_number(value):
                numeric_like_count += 1
                number = finite_number(value)
                if number is None:
                    nonfinite += 1
                else:
                    numeric_values.append(number)

        if valid_values and numeric_like_count == len(valid_values):
            inferred_type = "numeric"
        elif numeric_like_count:
            inferred_type = "mixed"
        elif valid_values:
            inferred_type = "categorical"
        else:
            inferred_type = "empty"
        profile = {
            "index": index,
            "name": name,
            "inferred_type": inferred_type,
            "value_count": len(values),
            "valid_count": len(valid_values),
            "missing_count": sum(missing.values()),
            "missing_sentinels": dict(sorted(missing.items())),
            "censored_count": sum(censored.values()),
            "censored_values": dict(sorted(censored.items())),
            "nonfinite_numeric_count": nonfinite,
        }
        if inferred_type == "numeric":
            profile["statistics"] = {
                "count": len(numeric_values),
                "min": min(numeric_values) if numeric_values else None,
                "max": max(numeric_values) if numeric_values else None,
                "mean": statistics.fmean(numeric_values) if numeric_values else None,
                "std": statistics.stdev(numeric_values) if len(numeric_values) > 1 else None,
                "std_definition": "sample standard deviation (n-1)",
            }
        else:
            counts = Counter(valid_values)
            profile["top_10"] = [
                {"value": value, "count": count}
                for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]
            ]
            profile["label_whitespace_variants"] = whitespace_variants(valid_values)
        column_profiles.append(profile)

    return {
        "encoding": encoding,
        "delimiter": delimiter,
        "delimiter_repr": repr(delimiter),
        "sniffer_error": sniffer_error,
        "has_header": has_header,
        "header_evidence": header_evidence,
        "row_count": len(data_rows),
        "column_count": width,
        "columns": columns,
        "whitespace_columns": [
            {"index": index, "name": name}
            for index, name in enumerate(columns)
            if name != name.strip()
        ],
        "empty_rows_excluding_trailing": empty_rows,
        "trailing_empty_rows": trailing_empty_rows,
        "duplicate_rows": duplicate_rows,
        "ragged_rows": ragged_rows,
        "column_profiles": column_profiles,
    }


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def profile_sqlite(path: Path) -> dict[str, Any]:
    if sqlite3 is None:
        return {"error": f"sqlite3 unavailable: {SQLITE_IMPORT_ERROR}"}
    p = path.resolve().as_posix()
    connection = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        integrity = [str(row[0]) for row in integrity_rows]
        table_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        tables = []
        for table_name in table_names:
            quoted_table = quote_identifier(table_name)
            row_count = connection.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
            definitions = []
            for cid, name, declared_type, not_null, default, primary_key in connection.execute(
                f"PRAGMA table_info({quoted_table})"
            ).fetchall():
                definitions.append(
                    {
                        "cid": cid,
                        "name": name,
                        "declared_type": declared_type,
                        "not_null": bool(not_null),
                        "default": default,
                        "primary_key": bool(primary_key),
                    }
                )
            column_type_counts = {}
            mixed_type_examples = []
            for definition in definitions:
                column_name = str(definition["name"])
                declared_type = str(definition["declared_type"] or "")
                quoted_column = quote_identifier(column_name)
                sampled = connection.execute(
                    f"SELECT typeof({quoted_column}), {quoted_column} FROM {quoted_table} LIMIT 200"
                ).fetchall()
                storage_types = Counter(str(row[0]) for row in sampled)
                column_type_counts[column_name] = dict(sorted(storage_types.items()))
                text_values = [row[1] for row in sampled if row[0] == "text"]
                numeric_declaration = bool(
                    re.search(r"INT|REAL|FLOA|DOUB|NUM|DEC", declared_type, re.IGNORECASE)
                )
                observed_numeric = bool(storage_types["integer"] or storage_types["real"])
                if text_values and (numeric_declaration or observed_numeric) and len(mixed_type_examples) < 5:
                    mixed_type_examples.append(
                        {
                            "column": column_name,
                            "declared_type": declared_type,
                            "sample_size": len(sampled),
                            "storage_types": dict(sorted(storage_types.items())),
                            "text_examples": [str(value)[:200] for value in text_values[:5]],
                        }
                    )
            tables.append(
                {
                    "name": table_name,
                    "row_count": row_count,
                    "columns": definitions,
                    "typeof_sample_limit": 200,
                    "column_type_counts": column_type_counts,
                    "mixed_type_examples": mixed_type_examples,
                }
            )
        return {
            "opened_read_only": True,
            "uri_mode": "ro",
            "integrity_check": integrity,
            "integrity_ok": integrity == ["ok"],
            "tables": tables,
        }
    finally:
        connection.close()


def profile_json(path: Path) -> dict[str, Any]:
    text, encoding = decode_bytes(path.read_bytes())
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return {"encoding": encoding, "valid": False, "error": str(exc)}
    result: dict[str, Any] = {
        "encoding": encoding,
        "valid": True,
        "top_level_type": type(value).__name__,
    }
    if isinstance(value, dict):
        result["keys"] = [str(key) for key in value.keys()]
        result["key_count"] = len(value)
    elif isinstance(value, list):
        result["item_count"] = len(value)
        result["item_types"] = dict(Counter(type(item).__name__ for item in value))
    return result


def read_case(input_dir: Path) -> dict[str, Any]:
    path = input_dir / "case.json"
    if not path.is_file():
        return {"path": str(path), "found": False}
    try:
        data = json.loads(decode_bytes(path.read_bytes())[0])
    except (OSError, ValueError) as exc:
        return {"path": str(path), "found": True, "error": str(exc)}
    if not isinstance(data, dict):
        return {"path": str(path), "found": True, "error": "case.json top level is not an object"}
    return {
        "path": str(path),
        "found": True,
        "case_id": data.get("case_id"),
        "output_contract": data.get("output_contract"),
        "inputs": data.get("inputs"),
    }


def read_task(input_dir: Path) -> dict[str, Any]:
    path = input_dir / "task.md"
    if not path.is_file():
        return {"path": str(path), "found": False, "heuristic": True}
    text, encoding = decode_bytes(path.read_bytes())
    objectives = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or re.match(r"^[-*\d.)\s]+", stripped) or re.search(
            r"objective|goal|task|analy[sz]e|calculate|compute|目标|任务|分析|计算|输出|要求",
            stripped,
            re.IGNORECASE,
        ):
            objectives.append(stripped[:500])
    stopwords = {
        "the", "and", "for", "with", "from", "that", "this", "into", "are", "you",
        "use", "using", "task", "data", "file", "files", "output", "input", "must", "should",
    }
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,12}", text.casefold())
    frequencies = Counter(token for token in tokens if token not in stopwords)
    keywords = [token for token, _ in sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))[:20]]
    return {
        "path": str(path),
        "found": True,
        "encoding": encoding,
        "heuristic": True,
        "objectives": objectives[:15],
        "keywords": keywords,
        "mentioned_files": sorted({match.group(0) for match in FILENAME_RE.finditer(text)}),
    }


def read_resources(input_dir: Path) -> dict[str, Any]:
    path = input_dir / "environment" / "resources.yaml"
    if not path.is_file():
        return {"path": str(path), "found": False}
    text, encoding = decode_bytes(path.read_bytes())
    top_level = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z_][\w.-]*):\s*(.*?)\s*$", line)
        if match and not match.group(2).startswith(("|", ">")):
            top_level[match.group(1)] = match.group(2).strip(" '\"")
    return {
        "path": str(path),
        "found": True,
        "encoding": encoding,
        "heuristic": True,
        "top_level_scalars": top_level,
        "raw_text": text,
    }


def environment_report() -> tuple[dict[str, Any], dict[str, Any]]:
    packages = {}
    fallback_matrix = {}
    for name in ("numpy", "pandas", "scipy", "sklearn", "matplotlib", "sqlite3"):
        available = False
        version = None
        error = None
        try:
            module = importlib.import_module(name)
            available = True
            if name == "sqlite3":
                version = getattr(module, "sqlite_version", None)
            else:
                version = getattr(module, "__version__", None)
                if version is None:
                    version = getattr(module, "version", None)
                    version = getattr(version, "version", None)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        packages[name] = {
            "available": available,
            "version": str(version) if version is not None else None,
            "error": error,
        }
        fallback_matrix[name] = {
            "required": False,
            "available": available,
            "fallback": FALLBACKS[name],
        }
    environment = {
        "python": {
            "version": sys.version,
            "executable": sys.executable,
            "implementation": sys.implementation.name,
            "platform": sys.platform,
        },
        "packages": packages,
        "environment_variables": {name: os.environ[name] for name in ENV_NAMES if name in os.environ},
    }
    return environment, fallback_matrix


def add_risk(risks: list[dict[str, Any]], code: str, message: str, path: Path | None = None) -> None:
    risk = {"code": code, "message": message}
    if path is not None:
        risk["path"] = str(path)
    risks.append(risk)


def build_report(input_dir: Path, work_dir: Path) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    environment, fallback_matrix = environment_report()
    case = read_case(input_dir)
    task = read_task(input_dir)
    resources = read_resources(input_dir)
    files = []
    risks: list[dict[str, Any]] = []
    recommendations = [
        "Treat task extraction and resource scalar parsing as heuristics; confirm semantics against the original text.",
        "Preserve raw inputs and write all generated artifacts only to the designated work/output directories.",
        "Exclude missing sentinels from numeric calculations and report censored <num values separately.",
    ]
    if not case.get("found"):
        add_risk(risks, "case_missing", "case.json was not found in the input directory.")
    elif case.get("error"):
        add_risk(risks, "case_invalid", case["error"])
    if not task.get("found"):
        add_risk(risks, "task_missing", "task.md was not found in the input directory.")
    if not resources.get("found"):
        add_risk(risks, "resources_missing", "environment/resources.yaml was not found.")

    for path in sorted(input_dir.rglob("*"), key=lambda item: str(item).casefold()):
        if not path.is_file():
            continue
        entry: dict[str, Any] = {
            "path": str(path),
            "relative_path": str(path.relative_to(input_dir)),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        try:
            with path.open("rb") as handle:
                prefix = handle.read(65536)
            file_format = detect_format(path, prefix)
            entry["format"] = file_format
            if file_format in {"CSV", "TSV"}:
                entry["profile"] = profile_delimited(path, file_format)
                profile = entry["profile"]
                if profile["whitespace_columns"]:
                    add_risk(risks, "whitespace_columns", "Column names contain leading/trailing whitespace.", path)
                if profile["duplicate_rows"]:
                    add_risk(risks, "duplicate_rows", f"{profile['duplicate_rows']} duplicate data rows detected.", path)
                if profile["ragged_rows"]:
                    add_risk(risks, "ragged_rows", f"{profile['ragged_rows']} rows have inconsistent widths.", path)
                if any(column["missing_count"] for column in profile["column_profiles"]):
                    add_risk(risks, "missing_sentinels", "Missing-value sentinels detected; do not coerce them silently.", path)
                if any(column["censored_count"] for column in profile["column_profiles"]):
                    add_risk(risks, "censored_values", "Censored <num values require an explicit analysis rule.", path)
                if any(column["inferred_type"] == "mixed" for column in profile["column_profiles"]):
                    add_risk(risks, "mixed_types", "Columns mix numeric and nonnumeric values.", path)
                if any(column["label_whitespace_variants"] for column in profile["column_profiles"] if column["inferred_type"] != "numeric"):
                    add_risk(risks, "label_whitespace", "Category labels have whitespace variants; preserve raw labels before grouping.", path)
            elif file_format == "SQLite":
                entry["profile"] = profile_sqlite(path)
                profile = entry["profile"]
                if profile.get("error"):
                    add_risk(risks, "sqlite_error", profile["error"], path)
                else:
                    if not profile["integrity_ok"]:
                        add_risk(risks, "sqlite_integrity", "PRAGMA integrity_check did not return exactly 'ok'.", path)
                    if any(table["mixed_type_examples"] for table in profile["tables"]):
                        add_risk(risks, "sqlite_numeric_text", "TEXT values occur in numeric SQLite columns (first 200 rows).", path)
            elif file_format == "JSON":
                entry["profile"] = profile_json(path)
                if not entry["profile"].get("valid", False):
                    add_risk(risks, "json_invalid", entry["profile"].get("error", "Invalid JSON"), path)
            elif file_format == "TXT":
                text, encoding = decode_bytes(path.read_bytes())
                entry["profile"] = {"encoding": encoding, "line_count": len(text.splitlines())}
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            add_risk(risks, "file_profile_error", entry["error"], path)
        files.append(entry)

    for package, info in environment["packages"].items():
        if not info["available"]:
            add_risk(risks, "optional_package_unavailable", f"{package}: {info['error']}")
            recommendations.append(f"{package}: {FALLBACKS[package]}")
    recommendations = list(dict.fromkeys(recommendations))
    status = "partial" if any(risk["code"] in {"case_invalid", "file_profile_error", "sqlite_error"} for risk in risks) else "ok"
    return {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "status": status,
        "skill_dir": str(SKILL_DIR),
        "input_dir": str(input_dir),
        "work_dir": str(work_dir),
        "case": case,
        "task": task,
        "resources": resources,
        "environment": environment,
        "fallback_matrix": fallback_matrix,
        "files": files,
        "risks": risks,
        "recommendations": recommendations,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--json-out", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        input_dir = args.input_dir.expanduser().resolve()
        if not input_dir.is_dir():
            raise NotADirectoryError(f"Input directory does not exist: {input_dir}")
        report = build_report(input_dir, args.work_dir.expanduser().resolve())
        emit(
            report["status"],
            {
                "files": len(report["files"]),
                "risks": len(report["risks"]),
                "case_id": report["case"].get("case_id"),
                "next": "Use targeted JSON slices and honor the output_contract before analysis.",
            },
            report,
            args.json_out,
        )
        return 0 if report["status"] == "ok" else 1
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "generated_at": utc_now_iso(),
            "status": "error",
            "skill_dir": str(SKILL_DIR),
            "error": f"{type(exc).__name__}: {exc}",
            "fallback_matrix": {
                name: {"required": False, "available": None, "fallback": fallback}
                for name, fallback in FALLBACKS.items()
            },
            "risks": [{"code": "recon_error", "message": str(exc)}],
            "recommendations": ["Check the input/work/json output paths and rerun recon."],
        }
        emit("error", {"error": str(exc)[:500]}, failure, args.json_out)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
