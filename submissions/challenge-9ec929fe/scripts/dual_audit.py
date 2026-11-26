#!/usr/bin/env python3
"""Independent pandas/SQLite numerical audit plus data and artifact QC."""
from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import math
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - depends on the environment
    pd = None
    PANDAS_IMPORT_ERROR = str(exc)
else:
    PANDAS_IMPORT_ERROR = None

from common import emit, sha256_file, utc_now_iso

RTOL = 1e-9
ATOL = 1e-12
SENTINELS = {"", "?", "na", "n/a", "null", "none", "nan", "-999", "-999.0", "-", r"\n"}
CENSORED_RE = re.compile(
    r"^\s*<\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*$"
)
CSV_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
METRICS = ("count", "sum", "mean", "min", "max")
SQL_METRICS = {"count": "COUNT", "sum": "SUM", "mean": "AVG", "min": "MIN", "max": "MAX"}
PANDAS_METRICS = {
    "count": lambda s: int(s.count()),
    "sum": lambda s: float(s.sum()) if s.count() else None,
    "mean": lambda s: float(s.mean()) if s.count() else None,
    "min": lambda s: float(s.min()) if s.count() else None,
    "max": lambda s: float(s.max()) if s.count() else None,
    "median": lambda s: float(s.median()) if s.count() else None,
    "std": lambda s: float(s.std()) if s.count() > 1 else None,
    "nunique": lambda s: int(s.nunique(dropna=True)),
}


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def scalar(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def equivalent(left: Any, right: Any) -> bool:
    left, right = scalar(left), scalar(right)
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=RTOL, abs_tol=ATOL)
    return left == right


def recon_delimited_profile(recon_path: Path | None, source: Path) -> dict[str, Any]:
    if recon_path is None:
        return {}
    payload = json.loads(recon_path.read_text(encoding="utf-8-sig"))
    entries = payload.get("files", []) if isinstance(payload, dict) else []
    matches = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("profile"), dict):
            continue
        raw_path = entry.get("path")
        if raw_path and Path(raw_path).expanduser().resolve() == source.expanduser().resolve():
            return entry["profile"]
        relative = entry.get("relative_path", raw_path)
        if relative and Path(relative).name == source.name:
            matches.append(entry["profile"])
    return matches[0] if len(matches) == 1 else {}


def read_delimited(path: Path, recon_path: Path | None) -> pd.DataFrame:
    profile = recon_delimited_profile(recon_path, path)
    raw = path.read_bytes()
    encodings = list(dict.fromkeys([profile.get("encoding"), *CSV_ENCODINGS]))
    last_error: Exception | None = None
    for encoding in encodings:
        if not encoding:
            continue
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError) as exc:
            last_error = exc
            continue
        delimiter = profile.get("delimiter")
        if not isinstance(delimiter, str) or len(delimiter) != 1:
            delimiter = "\t" if path.suffix.casefold() == ".tsv" else ","
            try:
                delimiter = csv.Sniffer().sniff(text[:65536], delimiters=",\t;|").delimiter
            except csv.Error:
                pass
        frame = pd.read_csv(
            io.StringIO(text, newline=""), sep=delimiter,
            keep_default_na=False, na_values=[""],
        )
        frame.attrs["csv_encoding"] = encoding
        frame.attrs["csv_delimiter"] = delimiter
        return frame
    raise ValueError(f"unable to decode {path}: {last_error}")


def load_source(
    path: Path, table: str | None, recon_path: Path | None = None
) -> tuple[pd.DataFrame, sqlite3.Connection, str, str]:
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv", ".txt"}:
        frame = read_delimited(path, recon_path)
        name = table or "data"
        connection = sqlite3.connect(":memory:")
        frame.to_sql(name, connection, index=False, if_exists="fail")
        source_kind = "csv_to_memory_sqlite"
    elif suffix in {".json", ".jsonl"}:
        frame = pd.read_json(path, lines=suffix == ".jsonl")
        name = table or "data"
        connection = sqlite3.connect(":memory:")
        frame.to_sql(name, connection, index=False, if_exists="fail")
        source_kind = "json_to_memory_sqlite"
    else:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        names = [r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        if table:
            if table not in names:
                raise ValueError(f"table {table!r} not found; available={names}")
            name = table
        elif len(names) == 1:
            name = names[0]
        else:
            raise ValueError("--table is required; available tables={names}")
        frame = pd.read_sql_query(f"SELECT * FROM {quote(name)}", connection)
        source_kind = "sqlite_to_pandas"
    if frame.columns.duplicated().any():
        raise ValueError("duplicate column names prevent unambiguous SQL comparison")
    return frame, connection, name, source_kind


def sentinel_aware_numeric(series: pd.Series) -> tuple[pd.Series | None, dict[str, Any]]:
    strings = series.astype("string").str.strip()
    sentinel_mask = (series.isna() | strings.str.casefold().isin(SENTINELS)).fillna(False)
    censored_mask = strings.str.fullmatch(CENSORED_RE).fillna(False)
    excluded = sentinel_mask | censored_mask
    candidates = series.loc[~excluded]
    converted = pd.to_numeric(candidates, errors="coerce")
    eligible_count = int(converted.notna().sum())
    denominator = int(len(candidates))
    fraction = eligible_count / denominator if denominator else 0.0
    detail = {
        "non_sentinel_count": denominator,
        "numeric_convertible_count": eligible_count,
        "numeric_convertible_fraction": fraction,
        "sentinel_count": int(sentinel_mask.sum()),
        "censored_count": int(censored_mask.sum()),
    }
    if not denominator or fraction < 0.5:
        return None, detail
    cleaned = pd.to_numeric(series.mask(excluded), errors="coerce")
    return cleaned, detail


def baseline_crosscheck(
    frame: pd.DataFrame, connection: sqlite3.Connection, table: str
) -> tuple[list[dict], list[dict], list[dict]]:
    comparisons: list[dict] = []
    mismatches: list[dict] = []
    skipped_columns: list[dict] = []
    converted_columns = []
    for column in frame.columns:
        series = frame[column]
        if pd.api.types.is_numeric_dtype(series):
            continue
        if not pd.api.types.is_object_dtype(series) and not pd.api.types.is_string_dtype(series):
            skipped_columns.append({
                "column": str(column), "dtype": str(series.dtype),
                "reason": "unsupported_non_numeric_dtype",
            })
            continue
        cleaned, detail = sentinel_aware_numeric(series)
        if cleaned is None:
            reason = "no_non_sentinel_values" if not detail["non_sentinel_count"] else "numeric_convertible_fraction_below_0.5"
            skipped_columns.append({"column": str(column), "dtype": str(series.dtype), "reason": reason, **detail})
        else:
            converted_columns.append((column, cleaned, detail))

    if converted_columns:
        connection.execute(
            'CREATE TEMP TABLE "_dual_audit_numeric" ("column_index" INTEGER, "value" REAL)'
        )
        for index, (_, series, _) in enumerate(converted_columns):
            connection.executemany(
                'INSERT INTO "_dual_audit_numeric" VALUES (?, ?)',
                [(index, None if pd.isna(value) else float(value)) for value in series.tolist()],
            )

    def compare(column: Any, series: pd.Series, sql_from: str, params: tuple = (), **extra: Any) -> None:
        for metric in METRICS:
            sql_value = connection.execute(
                f"SELECT {SQL_METRICS[metric]}(value) FROM {sql_from}", params
            ).fetchone()[0]
            pandas_value = PANDAS_METRICS[metric](series)
            passed = equivalent(pandas_value, sql_value)
            item = {
                "check_id": f"column:{column}:{metric}", "column": str(column),
                "metric": metric, "pandas_value": scalar(pandas_value),
                "sql_value": scalar(sql_value), "rtol": RTOL, "pass": passed, **extra,
            }
            comparisons.append(item)
            if not passed:
                mismatches.append({**item, "kind": "baseline_engine_mismatch"})

    for column in frame.select_dtypes(include="number").columns:
        compare(column, frame[column], f"(SELECT {quote(str(column))} AS value FROM {quote(table)})")
    for index, (column, series, detail) in enumerate(converted_columns):
        compare(
            column, series, '"_dual_audit_numeric" WHERE column_index = ?', (index,),
            sentinel_aware=True, conversion=detail,
        )
    return comparisons, mismatches, skipped_columns


def filter_mask(frame: pd.DataFrame, rule: dict[str, Any]) -> pd.Series:
    for operation in ("and", "or"):
        if operation in rule:
            rules = rule[operation]
            if not isinstance(rules, list) or not rules:
                raise ValueError(f"{operation} filter requires a nonempty rule list")
            masks = [filter_mask(frame, item) for item in rules]
            combined = masks[0]
            for mask in masks[1:]:
                combined = combined & mask if operation == "and" else combined | mask
            return combined
    operation = rule.get("operator", "==")
    if operation in {"and", "or"}:
        rules = rule.get("rules", rule.get("filters", rule.get("conditions", rule.get("value"))))
        return filter_mask(frame, {operation: rules})
    series = frame[rule["column"]]
    value = rule.get("value")
    if operation == "==":
        return series == value
    if operation == "!=":
        return series != value
    if operation == ">":
        return series > value
    if operation == ">=":
        return series >= value
    if operation == "<":
        return series < value
    if operation == "<=":
        return series <= value
    if operation == "isna":
        return series.isna()
    if operation == "notna":
        return series.notna()
    if operation == "in":
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("in filter requires a list, tuple, or set value")
        return series.isin(value)
    if operation == "between":
        inclusive = rule.get("inclusive", "both")
        if isinstance(value, dict):
            lower = value.get("min", value.get("lower"))
            upper = value.get("max", value.get("upper"))
            inclusive = value.get("inclusive", inclusive)
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            lower, upper = value
        else:
            lower = rule.get("min", rule.get("lower"))
            upper = rule.get("max", rule.get("upper"))
        if lower is None or upper is None or inclusive not in {"both", "left", "right", "neither"}:
            raise ValueError("between filter requires lower/upper bounds and valid inclusive mode")
        lower_mask = series >= lower if inclusive in {"both", "left"} else series > lower
        upper_mask = series <= upper if inclusive in {"both", "right"} else series < upper
        return lower_mask & upper_mask
    raise ValueError(f"unsupported filter operator {operation!r}")


def apply_filter(frame: pd.DataFrame, rule: dict[str, Any]) -> pd.DataFrame:
    return frame.loc[filter_mask(frame, rule)]


def pandas_metric(frame: pd.DataFrame, spec: dict[str, Any]) -> Any:
    selected = apply_filter(frame, spec["filter"]) if "filter" in spec else frame
    metric = spec["metric"]
    if metric == "rows":
        return int(len(selected))
    if metric not in PANDAS_METRICS:
        raise ValueError(f"unsupported pandas metric {metric!r}")
    return PANDAS_METRICS[metric](selected[spec["column"]])


def custom_crosschecks(frame: pd.DataFrame, connection: sqlite3.Connection, path: Path) -> tuple[list[dict], list[dict]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    checks = payload.get("checks", payload) if isinstance(payload, dict) else payload
    if not isinstance(checks, list):
        raise ValueError("checks JSON must be a list or an object containing a checks list")
    results: list[dict] = []
    mismatches: list[dict] = []
    for index, check in enumerate(checks):
        check_id = str(check.get("id", check.get("check_id", f"check_{index + 1}")))
        try:
            cursor = connection.execute(check["sql"])
            rows = cursor.fetchall()
            description = [d[0] for d in cursor.description]
            if len(rows) != 1:
                raise ValueError(f"SQL must return exactly one row, got {len(rows)}")
            sql_row = dict(zip(description, rows[0]))
            pandas_spec = check.get("pandas", check.get("expected_pandas"))
            if not isinstance(pandas_spec, dict):
                raise ValueError("missing pandas metric object")
            mapping = pandas_spec if "metric" not in pandas_spec else {"value": pandas_spec}
            if len(sql_row) == 1 and len(mapping) == 1:
                sql_values = {"value": next(iter(sql_row.values()))}
            else:
                sql_values = sql_row
            for key, spec in mapping.items():
                sql_key = "value" if len(sql_values) == 1 and len(mapping) == 1 else key
                if sql_key not in sql_values:
                    raise ValueError(f"SQL output lacks alias {sql_key!r}")
                pandas_value = pandas_metric(frame, spec)
                sql_value = sql_values[sql_key]
                passed = equivalent(pandas_value, sql_value)
                item = {
                    "check_id": f"{check_id}:{key}", "sql": check["sql"],
                    "pandas_spec": spec, "pandas_value": scalar(pandas_value),
                    "sql_value": scalar(sql_value), "rtol": RTOL, "pass": passed,
                }
                results.append(item)
                if not passed:
                    mismatches.append({**item, "kind": "custom_engine_mismatch"})
        except Exception as exc:
            item = {"check_id": check_id, "pass": False, "error": f"{type(exc).__name__}: {exc}"}
            results.append(item)
            mismatches.append({**item, "kind": "check_execution_failed"})
    return results, mismatches


def quality_audit(frame: pd.DataFrame) -> tuple[dict, list[str]]:
    columns: dict[str, Any] = {}
    warnings: list[str] = []
    duplicate_mask = frame.duplicated(keep=False)
    for column in frame.columns:
        series = frame[column]
        strings = series.astype("string").str.strip()
        sentinel_mask = strings.str.casefold().isin(SENTINELS).fillna(False)
        censored_mask = strings.str.fullmatch(CENSORED_RE).fillna(False)
        entry: dict[str, Any] = {
            "dtype": str(series.dtype), "missing": int(series.isna().sum()),
            "sentinel_count": int(sentinel_mask.sum()),
            "sentinel_examples": [str(x) for x in series[sentinel_mask].head(5).tolist()],
            "censored_count": int(censored_mask.sum()),
            "censored_examples": [str(x) for x in series[censored_mask].head(5).tolist()],
        }
        if pd.api.types.is_numeric_dtype(series):
            numeric = pd.to_numeric(series, errors="coerce").dropna()
            numeric = numeric[numeric.map(math.isfinite)]
            entry["zero_variance"] = bool(numeric.nunique() <= 1)
            if numeric.empty:
                iqr_mask = pd.Series(False, index=series.index)
                z_mask = pd.Series(False, index=series.index)
            else:
                q1, q3 = numeric.quantile([0.25, 0.75])
                iqr = q3 - q1
                iqr_mask = (series < q1 - 1.5 * iqr) | (series > q3 + 1.5 * iqr)
                std = numeric.std(ddof=0)
                z_mask = ((series - numeric.mean()) / std).abs() > 3 if std > 0 else series == "__never__"
            iqr_indices = [int(i) for i in series.index[iqr_mask.fillna(False)][:50]]
            z_indices = [int(i) for i in series.index[z_mask.fillna(False)][:50]]
            entry["outliers"] = {
                "iqr_count": int(len(series.index[iqr_mask.fillna(False)])),
                "iqr_row_indices_first50": iqr_indices,
                "zscore_count": int(len(series.index[z_mask.fillna(False)])),
                "zscore_row_indices_first50": z_indices,
                "rule": "IQR 1.5x; |population z| > 3; flag only",
            }
            if entry["zero_variance"]:
                warnings.append(f"zero-variance or empty numeric column: {column}")
        if entry["missing"]:
            warnings.append(f"missing values in {column}: {entry['missing']}")
        if entry["sentinel_count"]:
            warnings.append(f"sentinel tokens in {column}: {entry['sentinel_count']}")
        if entry["censored_count"]:
            warnings.append(f"censored <num values in {column}: {entry['censored_count']}")
        columns[str(column)] = entry
    if duplicate_mask.any():
        warnings.append(f"duplicate rows involved: {int(duplicate_mask.sum())}")
    return {
        "rows": len(frame), "columns": len(frame.columns),
        "duplicate_extra_rows": int(frame.duplicated().sum()),
        "duplicate_row_indices_first50": [int(i) for i in frame.index[duplicate_mask][:50]],
        "per_column": columns, "outlier_policy": "flag_only_no_rows_removed",
    }, warnings


def detects_input_write(tree: ast.AST | None, text: str) -> bool:
    if tree is None:
        return bool(re.search(r"\.(?:write_text|write_bytes|to_csv|to_json|to_sql|savefig)\s*\([^\n]*['\"]/input/", text))
    write_methods = {"write_text", "write_bytes", "to_csv", "to_json", "to_sql", "savefig", "save", "mkdir", "unlink"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if name in write_methods and "/input" in (ast.get_source_segment(text, node) or ""):
            return True
        if name == "open" and node.args:
            target = node.args[0]
            if isinstance(target, ast.Constant) and isinstance(target.value, str) and target.value.startswith("/input"):
                mode_node = node.args[1] if len(node.args) > 1 else next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
                mode = mode_node.value if isinstance(mode_node, ast.Constant) else "r"
                if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
                    return True
    return False


def reproducibility_scan(path: Path) -> tuple[dict, list[str]]:
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
        syntax_error = None
    except SyntaxError as exc:
        tree = None
        syntax_error = str(exc)
    seed_calls = re.findall(r"\b[\w.]*seed\s*\([^\n)]*\)", text)
    random_state = re.findall(r"\brandom_state\s*=\s*[^,\n)]+", text)
    random_calls = re.findall(r"\b(?:np\.)?random\.(?:[A-Za-z_]+)\s*\(", text)
    show = bool(re.search(r"\bplt\.show\s*\(", text))
    input_write = detects_input_write(tree, text)
    findings = {
        "script": str(path), "sha256": sha256_file(path), "syntax_error": syntax_error,
        "seed_calls": seed_calls, "random_state_settings": random_state,
        "random_calls": random_calls, "plt_show": show, "possible_input_write": input_write,
    }
    warnings: list[str] = []
    if random_calls and not seed_calls and not random_state:
        warnings.append("random operations detected without a fixed seed/random_state")
    if input_write:
        warnings.append("possible write to read-only /input")
    if show:
        warnings.append("plt.show() is unsafe in headless execution; save the figure instead")
    if syntax_error:
        warnings.append(f"analysis script has invalid Python syntax: {syntax_error}")
    return findings, warnings


def figure_audit(paths: list[str], min_dpi: float = 200.0) -> tuple[list[dict], list[str]]:
    reports: list[dict] = []
    warnings: list[str] = []
    try:
        from PIL import Image
    except ImportError:
        return [], ["Pillow unavailable; figure QC not executed"]
    for raw in paths:
        path = Path(raw)
        try:
            with Image.open(path) as image:
                gray = image.convert("L")
                histogram = gray.histogram()
                ink_count = sum(histogram[:250])
                ink_mean = sum(value * histogram[value] for value in range(250)) / ink_count if ink_count else 0.0
                variance = sum(histogram[value] * (value - ink_mean) ** 2 for value in range(250)) / ink_count if ink_count else 0.0
                pixel_count = image.width * image.height
                colors = image.convert("RGB").getcolors(maxcolors=pixel_count)
                unique_colors = len(colors) if colors is not None else pixel_count
                dpi = image.info.get("dpi")
                dpi_values = [float(v) for v in dpi] if dpi else []
                report = {
                    "path": str(path), "format": image.format, "width": image.width,
                    "height": image.height, "nonwhite_pixels": ink_count,
                    "nonwhite_fraction": ink_count / pixel_count if pixel_count else 0,
                    "nonwhite_variance": variance, "nonwhite_std": math.sqrt(variance),
                    "unique_colors": unique_colors, "dpi": dpi_values or None,
                    "nonblank_pass": ink_count > 0 and (math.sqrt(variance) > 1.0 or unique_colors > 1),
                    "min_dpi": min_dpi,
                    "dpi_pass": bool(dpi_values) and min(dpi_values) >= min_dpi - 0.01,
                    "dpi_300_pass": bool(dpi_values) and min(dpi_values) >= 299.0,
                }
                if not report["nonblank_pass"]:
                    warnings.append(f"blank or near-blank figure: {path}")
                if not report["dpi_pass"]:
                    warnings.append(f"missing or below-{min_dpi:g}-DPI metadata: {path}")
                reports.append(report)
        except Exception as exc:
            reports.append({"path": str(path), "nonblank_pass": False, "error": str(exc)})
            warnings.append(f"figure unreadable: {path}: {exc}")
    return reports, warnings


def build_report(args: argparse.Namespace) -> tuple[str, dict]:
    source = Path(args.input)
    recon_path = Path(args.recon) if args.recon else None
    frame, connection, table, source_kind = load_source(source, args.table, recon_path)
    warnings: list[str] = []
    try:
        baseline, mismatches, skipped_columns = baseline_crosscheck(frame, connection, table)
        custom: list[dict] = []
        if args.checks:
            custom, custom_mismatches = custom_crosschecks(frame, connection, Path(args.checks))
            mismatches.extend(custom_mismatches)
        quality, quality_warnings = quality_audit(frame)
        warnings.extend(quality_warnings)
        reproducibility = None
        if args.script:
            reproducibility, scan_warnings = reproducibility_scan(Path(args.script))
            warnings.extend(scan_warnings)
        figures, figure_warnings = figure_audit(args.figures or [], args.min_dpi)
        warnings.extend(figure_warnings)
        status = "fail" if mismatches else ("warn" if warnings else "pass")
        return status, {
            "schema_version": "1.0", "generated_at": utc_now_iso(), "status": status,
            "input": {
                "path": str(source), "sha256": sha256_file(source), "kind": source_kind,
                "table": table, "encoding": frame.attrs.get("csv_encoding"),
                "delimiter": frame.attrs.get("csv_delimiter"),
            },
            "engine_consistency": not mismatches, "comparison_tolerance": {"rtol": RTOL, "atol": ATOL},
            "baseline_comparisons": baseline, "custom_comparisons": custom,
            "skipped_columns": skipped_columns, "mismatches": mismatches, "statistical_quality": quality,
            "reproducibility": reproducibility, "figures": figures, "warnings": warnings,
            "limitations": [
                "Sentinel/censored exclusions apply only to reported sentinel-aware baseline comparisons; raw values remain in custom checks.",
                "Outliers are flagged, never removed.",
            ],
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="CSV/TSV/JSON/JSONL/SQLite input")
    parser.add_argument("--table", help="SQLite table (or imported table name)")
    parser.add_argument("--recon", help="recon.json supplying CSV encoding and delimiter")
    parser.add_argument("--checks", help="JSON list of paired SQL and declarative pandas checks")
    parser.add_argument("--script", help="analysis.py to scan for reproducibility hazards")
    parser.add_argument("--figures", nargs="*", default=[], help="PNG files for nonblank/DPI QC")
    parser.add_argument("--min-dpi", type=float, default=200.0, help="minimum figure DPI (default: 200)")
    parser.add_argument("--json-out", required=True)
    args = parser.parse_args()
    if pd is None:
        status = "skipped_missing_pandas"
        report = {
            "schema_version": "1.0", "generated_at": utc_now_iso(), "status": status,
            "input": {"path": args.input}, "engine_consistency": None,
            "baseline_comparisons": [], "custom_comparisons": [], "skipped_columns": [],
            "mismatches": [], "warnings": [f"pandas unavailable: {PANDAS_IMPORT_ERROR}"],
            "fatal_error": None,
        }
        emit(status, {"engine_consistency": None, "mismatches": 0, "warnings": 1}, report, args.json_out)
        return 1
    try:
        status, report = build_report(args)
    except Exception as exc:
        status = "fail"
        report = {
            "schema_version": "1.0", "generated_at": utc_now_iso(), "status": status,
            "engine_consistency": False, "mismatches": [],
            "fatal_error": f"{type(exc).__name__}: {exc}", "warnings": [],
        }
    emit(status, {
        "engine_consistency": report.get("engine_consistency"),
        "mismatches": len(report.get("mismatches", [])),
        "warnings": len(report.get("warnings", [])),
    }, report, args.json_out)
    return 0 if status in {"pass", "warn"} else 1


if __name__ == "__main__":
    sys.exit(main())
