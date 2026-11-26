"""Build, independently verify, and reverse-engineer scientific figure source ledgers."""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import atomic_write_json, atomic_write_text, emit, sha256_file, utc_now_iso

METADATA_KEYS = {
    "id", "series_id", "name", "label", "source", "source_file", "source_ref", "file",
    "locator", "json_path", "column", "field", "x_column", "y_column", "row", "rows",
    "where", "filter", "transform", "statistic", "stat", "aggregation", "description",
    "units", "unit",
}
GRAPHIC_TAGS = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "image", "use"}
GRAPHIC_ATTRS = {
    "d", "x", "y", "x1", "x2", "y1", "y2", "cx", "cy", "r", "rx", "ry", "width", "height",
    "points", "fill", "stroke", "stroke-width", "opacity", "fill-opacity", "stroke-opacity",
    "transform", "style", "class", "visibility", "display", "data-source", "data-value",
}


def _read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _source_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv", ".txt"}:
        return "csv"
    if suffix == ".json":
        return "json"
    with path.open("rb") as handle:
        prefix = handle.read(128).lstrip()
    return "json" if prefix.startswith((b"{", b"[")) else "csv"


def _load_source(path: Path) -> dict[str, Any]:
    kind = _source_kind(path)
    if kind == "json":
        return {"format": "json", "data": _read_json(path)}
    last_error: UnicodeError | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "cp1252", "latin-1"):
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                sample = handle.read(8192)
                handle.seek(0)
                if path.suffix.lower() not in {".csv", ".tsv"}:
                    try:
                        delimiter = csv.Sniffer().sniff(sample, delimiters=",\t;").delimiter
                    except csv.Error:
                        pass
                reader = csv.DictReader(handle, delimiter=delimiter)
                rows = list(reader)
                columns = list(reader.fieldnames or [])
            return {"format": "csv", "encoding": encoding, "delimiter": delimiter, "columns": columns, "rows": rows, "data": rows}
        except UnicodeError as exc:
            last_error = exc
    raise last_error if last_error is not None else UnicodeError(f"Could not decode {path}")


def _resolve_source(reference: Any, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if reference is None:
        return sources
    text = str(reference)
    normalized = text.replace("\\", "/").lower()
    exact = [source for source in sources if str(source["path"]).replace("\\", "/").lower() == normalized]
    if exact:
        return exact
    return [source for source in sources if source["path"].name.lower() == Path(text).name.lower()]


def _json_pointer(data: Any, pointer: str) -> Any:
    if pointer in {"", "/"}:
        return data
    parts = pointer.lstrip("/").split("/") if pointer.startswith("/") else pointer.split(".")
    current = data
    for raw_part in parts:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(pointer)
    return current


def _records_column(data: Any, column: str) -> list[Any] | None:
    if isinstance(data, list) and all(isinstance(row, dict) for row in data):
        if data and not any(column in row for row in data):
            return None
        return [row.get(column) for row in data]
    if isinstance(data, dict):
        value = data.get(column)
        if isinstance(value, list):
            return value
        for key in ("rows", "records", "data"):
            if key in data:
                found = _records_column(data[key], column)
                if found is not None:
                    return found
    return None


def _select_rows(values: list[Any], metadata: dict[str, Any]) -> list[Any]:
    selected = values
    where = metadata.get("where", metadata.get("filter"))
    # Filtering needs original records and is handled in _source_values when available.
    row_spec = metadata.get("rows", metadata.get("row"))
    if row_spec is None:
        return list(selected)
    indices = [row_spec] if isinstance(row_spec, int) else list(row_spec)
    return [selected[index] for index in indices]


def _aggregate(values: list[Any], operation: str) -> Any:
    operation = operation.lower().strip()
    if operation == "count":
        return len(values)
    numeric = [_number(value) for value in values]
    if operation in {"mean", "avg", "average"}:
        return statistics.fmean(numeric)
    if operation == "sum":
        return math.fsum(numeric)
    if operation == "min":
        return min(numeric)
    if operation == "max":
        return max(numeric)
    if operation == "median":
        return statistics.median(numeric)
    if operation in {"std", "stdev", "sample_std"}:
        return statistics.stdev(numeric)
    if operation == "sem":
        return statistics.stdev(numeric) / math.sqrt(len(numeric))
    if operation in {"std_pop", "pstdev", "population_std"}:
        return statistics.pstdev(numeric)
    if operation in {"var", "variance", "sample_var"}:
        return statistics.variance(numeric)
    if operation in {"var_pop", "pvariance"}:
        return statistics.pvariance(numeric)
    if operation == "count":
        return len(values)
    raise ValueError(f"Unsupported aggregation: {operation}")


def _transform_values(values: Any, transform: str) -> Any:
    operation = transform.lower().strip()
    if operation in {"", "none", "identity", "raw"}:
        return values

    def apply(value: Any) -> float:
        numeric = _number(value)
        if operation == "log10":
            return math.log10(numeric)
        if operation == "ln":
            return math.log(numeric)
        if operation == "log2":
            return math.log2(numeric)
        if operation == "sqrt":
            return math.sqrt(numeric)
        if operation == "abs":
            return abs(numeric)
        if operation.startswith("scale:"):
            return numeric * _number(operation.split(":", 1)[1])
        if operation.startswith("offset:"):
            return numeric + _number(operation.split(":", 1)[1])
        raise ValueError(f"Unsupported transform: {operation}")

    return [apply(value) for value in values] if isinstance(values, list) else apply(values)


def _source_values(source: dict[str, Any], leaf: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    metadata = leaf["metadata"]
    loaded = source["loaded"]
    data = loaded["data"]
    leaf_key = leaf["path"][-1]
    column = metadata.get("column", metadata.get("field"))
    if column is None and leaf_key in {"x", "y"}:
        column = metadata.get(f"{leaf_key}_column")
    if column is None and leaf_key in {"value", "values", "plotted_values"}:
        column = metadata.get("column")
    locator = metadata.get("locator", metadata.get("json_path"))
    if isinstance(locator, str) and locator.lower().startswith(("column:", "col:")):
        column = locator.split(":", 1)[1]
        locator = None
    if locator is not None:
        values = _json_pointer(data, str(locator))
        binding = {"locator": locator}
    else:
        direct_json = False
        if column is None and loaded["format"] == "json":
            try:
                values = _json_pointer(data, leaf["pointer"])
                binding = {"locator": leaf["pointer"]}
                direct_json = True
            except (KeyError, IndexError, ValueError, TypeError):
                pass
        if not direct_json:
            if column is None:
                column = leaf_key
            where = metadata.get("where", metadata.get("filter"))
            if isinstance(where, dict) and isinstance(data, list):
                rows = [row for row in data if isinstance(row, dict) and all(str(row.get(key)) == str(value) for key, value in where.items())]
                values = [row.get(str(column)) for row in rows]
            elif isinstance(where, dict) and loaded["format"] == "csv":
                rows = [row for row in loaded["rows"] if all(str(row.get(key)) == str(value) for key, value in where.items())]
                values = [row.get(str(column)) for row in rows]
            else:
                values = _records_column(data, str(column))
                if values is None:
                    raise KeyError(f"Column {column!r} was not found")
            values = _select_rows(values, metadata)
            binding = {"column": str(column)}
            if where:
                binding["where"] = where
            if metadata.get("rows", metadata.get("row")) is not None:
                binding["rows"] = metadata.get("rows", metadata.get("row"))
    transform = metadata.get("transform")
    if isinstance(transform, str) and transform.lower().strip() not in {"", "none", "identity", "raw"}:
        values = _transform_values(values, transform)
        binding["transform"] = transform
    operation = metadata.get("statistic", metadata.get("stat", metadata.get("aggregation")))
    if operation:
        values = _aggregate(list(values) if isinstance(values, list) else [values], str(operation))
        binding["aggregation"] = str(operation)
    return values, binding


def _number(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Not numeric: {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Not finite: {value!r}")
    return result


def _is_number(value: Any) -> bool:
    try:
        _number(value)
        return True
    except (TypeError, ValueError):
        return False


def _is_numeric_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_is_number(item) for item in value)


def _walk_plotted(node: Any, path: tuple[str, ...] = (), metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    metadata = dict(metadata or {})
    leaves: list[dict[str, Any]] = []

    def add(value: Any, leaf_path: tuple[str, ...]) -> None:
        pointer = "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in leaf_path)
        leaves.append({
            "path": leaf_path,
            "pointer": pointer,
            "value": value,
            "metadata": dict(metadata),
            "series_id": metadata.get("series_id", metadata.get("id", metadata.get("name", "/".join(leaf_path[:-1]) or leaf_path[-1]))),
        })

    if _is_number(node):
        add(node, path)
    elif _is_numeric_array(node):
        add(node, path)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            leaves.extend(_walk_plotted(item, path + (str(index),), metadata))
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in METADATA_KEYS:
                metadata[key] = value
        for key, value in node.items():
            if key not in METADATA_KEYS:
                leaves.extend(_walk_plotted(value, path + (key,), metadata))
    return leaves


def _compare(plotted: Any, expected: Any, rtol: float) -> dict[str, Any]:
    plotted_values = plotted if isinstance(plotted, list) else [plotted]
    expected_values = expected if isinstance(expected, list) else [expected]
    result: dict[str, Any] = {
        "match": False,
        "plotted_count": len(plotted_values),
        "source_count": len(expected_values),
        "difference_count": 0,
        "differences": [],
        "max_absolute_error": None,
        "max_relative_error": None,
    }
    if len(plotted_values) != len(expected_values):
        result["error"] = "Length mismatch"
        return result
    absolute_errors: list[float] = []
    relative_errors: list[float] = []
    for index, (actual, source) in enumerate(zip(plotted_values, expected_values)):
        try:
            actual_number = _number(actual)
            source_number = _number(source)
        except (TypeError, ValueError) as exc:
            result["difference_count"] += 1
            if len(result["differences"]) < 100:
                result["differences"].append({"index": index, "plotted": actual, "source": source, "error": str(exc)})
            continue
        absolute = abs(actual_number - source_number)
        relative = absolute / abs(source_number) if source_number else (0.0 if absolute == 0 else math.inf)
        absolute_errors.append(absolute)
        relative_errors.append(relative)
        if not math.isclose(actual_number, source_number, rel_tol=rtol, abs_tol=0.0):
            result["difference_count"] += 1
            if len(result["differences"]) < 100:
                result["differences"].append({
                    "index": index,
                    "plotted": actual_number,
                    "source": source_number,
                    "absolute_error": absolute,
                    "relative_error": relative,
                })
    result["max_absolute_error"] = max(absolute_errors) if absolute_errors else None
    result["max_relative_error"] = max(relative_errors) if relative_errors else None
    result["match"] = result["difference_count"] == 0
    return result


def verify_plotted(sources: list[dict[str, Any]], plotted: Any, rtol: float = 1e-6) -> dict[str, Any]:
    comparisons: list[dict[str, Any]] = []
    for leaf in _walk_plotted(plotted):
        metadata = leaf["metadata"]
        reference = metadata.get("source_file", metadata.get("source", metadata.get("file")))
        candidates = _resolve_source(reference, sources)
        base: dict[str, Any] = {
            "series_id": leaf["series_id"],
            "plotted_path": leaf["pointer"],
            "plotted_value": leaf["value"],
            "source_file": None,
            "source_sha256": None,
            "source_binding": None,
            "source_value": None,
            "status": "unbound",
            "match": False,
            "rtol": rtol,
        }
        if not candidates:
            base["error"] = f"Source not found: {reference}"
            comparisons.append(base)
            continue
        if len(candidates) > 1:
            if reference is not None:
                base["error"] = f"Ambiguous source reference: {reference}"
                comparisons.append(base)
                continue
            # Automatic binding is accepted only when exactly one source has the requested column.
            matched_sources = []
            for source in candidates:
                try:
                    _source_values(source, leaf)
                    matched_sources.append(source)
                except (KeyError, IndexError, TypeError, ValueError):
                    pass
            candidates = matched_sources
            if len(candidates) != 1:
                base["error"] = "No unique source binding; add source_file and column/locator metadata" if not candidates else "Column is ambiguous across multiple sources"
                comparisons.append(base)
                continue
        source = candidates[0]
        base["source_file"] = str(source["path"])
        base["source_sha256"] = source["sha256"]
        try:
            expected, binding = _source_values(source, leaf)
            base["source_binding"] = binding
            base["source_value"] = expected
            comparison = _compare(leaf["value"], expected, rtol)
            base.update(comparison)
            base["status"] = "match" if comparison["match"] else "mismatch"
            if comparison.get("error"):
                base["error"] = comparison["error"]
        except (KeyError, IndexError, TypeError, ValueError, statistics.StatisticsError) as exc:
            base["error"] = f"Could not independently recompute source value: {exc}"
        comparisons.append(base)
    matched = sum(item["status"] == "match" for item in comparisons)
    mismatched = sum(item["status"] == "mismatch" for item in comparisons)
    unbound = len(comparisons) - matched - mismatched
    return {
        "schema_version": "1.0",
        "created_utc": utc_now_iso(),
        "rtol": rtol,
        "comparisons": comparisons,
        "summary": {"total": len(comparisons), "matched": matched, "mismatched": mismatched, "unbound": unbound},
        "all_match": bool(comparisons) and matched == len(comparisons),
    }


def extract_svg_elements(svg_path: str | Path) -> dict[str, Any]:
    path = Path(svg_path).resolve()
    root = ET.parse(path).getroot()
    local = lambda tag: tag.rsplit("}", 1)[-1]
    elements: list[dict[str, Any]] = []
    groups: list[str] = []
    for index, element in enumerate(root.iter()):
        tag = local(element.tag)
        if tag not in GRAPHIC_TAGS and tag not in {"text", "g"}:
            continue
        element_id = element.get("id")
        if tag == "g" and not element_id:
            continue
        attributes = {local(key): value for key, value in element.attrib.items() if key in GRAPHIC_ATTRS or key.startswith("data-")}
        item: dict[str, Any] = {
            "index": index,
            "tag": tag,
            "id": element_id,
            "attributes": attributes,
            "source_ref": element.get("data-source"),
            "binding_status": "pending",
        }
        if tag == "text":
            item["text"] = "".join(element.itertext())
        if tag == "g":
            groups.append(element_id)
        elements.append(item)
    return {
        "schema_version": "1.0",
        "svg": str(path),
        "sha256": sha256_file(path),
        "viewBox": root.get("viewBox"),
        "groups": groups,
        "elements": elements,
        "summary": {"elements": len(elements), "text_nodes": sum(item["tag"] == "text" for item in elements), "groups": len(groups)},
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = ["# SOURCE_MAP", "", f"Created (UTC): {report['created_utc']}", ""]
    case = report.get("case")
    if case:
        lines.extend(["## Case", "", f"- Path: `{case['path']}`", f"- SHA-256: `{case['sha256']}`", f"- Case ID: `{case.get('case_id', '')}`", ""])
    lines.extend(["## Source files", "", "| Source | SHA-256 | Format |", "|---|---|---|"])
    for source in report["sources"]:
        lines.append(f"| `{source['path']}` | `{source['sha256']}` | {source['format']} |")
    lines.extend(["", "## Plotted-value verification", "", "| Series | Plotted path | Source | Binding | Status | Differences |", "|---|---|---|---|---|---:|"])
    for item in report["verification"]["comparisons"]:
        binding = json.dumps(item.get("source_binding"), ensure_ascii=False) if item.get("source_binding") else "unbound"
        source = item.get("source_file") or "—"
        lines.append(f"| {item['series_id']} | `{item['plotted_path']}` | `{source}` | `{binding}` | {item['status']} | {item.get('difference_count', '—')} |")
        if item.get("error"):
            lines.append(f"|  |  |  | {item['error']} |  |  |")
    lines.extend(["", f"**All values independently matched:** {report['verification']['all_match']}", ""])
    if report.get("svg"):
        lines.extend(["## Existing SVG elements awaiting source binding", "", f"- SVG: `{report['svg']['svg']}`", f"- SHA-256: `{report['svg']['sha256']}`", f"- Elements: {report['svg']['summary']['elements']}", f"- Text nodes: {report['svg']['summary']['text_nodes']}", ""])
        for item in report["svg"]["elements"]:
            label = item.get("text", "")
            lines.append(f"- `{item['tag']}` id=`{item.get('id') or ''}` {label} — binding: **{item['binding_status']}**")
    return "\n".join(lines)


def _load_sources(paths: list[str | Path]) -> list[dict[str, Any]]:
    sources = []
    for raw_path in paths:
        path = Path(raw_path).resolve()
        sources.append({"path": path, "sha256": sha256_file(path), "format": _source_kind(path), "loaded": _load_source(path)})
    return sources


def _public_source(source: dict[str, Any]) -> dict[str, Any]:
    return {"path": str(source["path"]), "sha256": source["sha256"], "format": source["format"]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="Build a source mapping ledger")
    build.add_argument("--case", type=Path, required=True)
    build.add_argument("--sources", nargs="+", type=Path, required=True)
    build.add_argument("--plotted", type=Path)
    build.add_argument("--svg", type=Path)
    build.add_argument("--rtol", type=float, default=1e-6)
    build.add_argument("--json-out", type=Path, required=True)
    build.add_argument("--md-out", type=Path, required=True)
    verify = subparsers.add_parser("verify", help="Independently compare plotted values with sources")
    verify.add_argument("--sources", nargs="+", type=Path, required=True)
    verify.add_argument("--plotted", type=Path, required=True)
    verify.add_argument("--rtol", type=float, default=1e-6)
    verify.add_argument("--json-out", type=Path, required=True)
    reverse = subparsers.add_parser("reverse", help="Extract an unbound ledger from an existing SVG")
    reverse.add_argument("--svg", type=Path, required=True)
    reverse.add_argument("--json-out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "reverse":
            report = extract_svg_elements(args.svg)
            emit("ok", report["summary"], report, args.json_out)
            return 0
        sources = _load_sources(args.sources)
        if args.command == "verify":
            plotted = _read_json(args.plotted)
            report = verify_plotted(sources, plotted, args.rtol)
            report["sources"] = [_public_source(source) for source in sources]
            report["plotted_arrays"] = {"path": str(args.plotted.resolve()), "sha256": sha256_file(args.plotted)}
            status = "ok" if report["all_match"] else "failed"
            emit(status, report["summary"], report, args.json_out)
            return 0 if report["all_match"] else 1
        case_data = _read_json(args.case)
        plotted = _read_json(args.plotted) if args.plotted else {}
        verification = verify_plotted(sources, plotted, args.rtol)
        report = {
            "schema_version": "1.0",
            "created_utc": utc_now_iso(),
            "case": {"path": str(args.case.resolve()), "sha256": sha256_file(args.case), "case_id": case_data.get("case_id") if isinstance(case_data, dict) else None},
            "sources": [_public_source(source) for source in sources],
            "plotted_arrays": ({"path": str(args.plotted.resolve()), "sha256": sha256_file(args.plotted)} if args.plotted else None),
            "svg": extract_svg_elements(args.svg) if args.svg else None,
            "verification": verification,
        }
        atomic_write_text(args.md_out, _markdown(report))
        status = "ok" if verification["all_match"] or not verification["comparisons"] else "warning"
        emit(status, verification["summary"], report, args.json_out)
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, ET.ParseError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
