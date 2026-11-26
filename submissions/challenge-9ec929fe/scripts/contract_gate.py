#!/usr/bin/env python3
"""Validate result contracts, preserve partial delivery, and seal evidence artifacts."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

from common import atomic_write_json, atomic_write_text, emit, sha256_file, utc_now_iso

REQUIRED_METADATA = ("case_id", "status", "sources", "uncertainty", "change_log")
TYPE_MAP = {
    "object": dict, "dict": dict, "array": list, "list": list, "string": str,
    "str": str, "boolean": bool, "bool": bool, "number": (int, float),
    "float": (int, float), "integer": int, "int": int,
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def contract_fields(case: dict[str, Any]) -> list[dict[str, Any]]:
    contract = case.get("output_contract", [])
    if isinstance(contract, dict) and any(k in contract for k in ("fields", "required_fields", "properties")):
        contract = contract.get("fields", contract.get("required_fields", contract.get("properties")))
    fields: list[dict[str, Any]] = []
    if isinstance(contract, list):
        for item in contract:
            if isinstance(item, str):
                fields.append({"name": item})
            elif isinstance(item, dict):
                name = item.get("name", item.get("field", item.get("key")))
                if name:
                    fields.append({**item, "name": str(name)})
    elif isinstance(contract, dict):
        for name, spec in contract.items():
            fields.append({**(spec if isinstance(spec, dict) else {"type": spec}), "name": str(name)})
    return fields


def nonfinite_paths(value: Any, path: str = "$") -> list[str]:
    if isinstance(value, float) and not math.isfinite(value):
        return [f"{path}={value}"]
    if isinstance(value, dict):
        return [issue for key, item in value.items() for issue in nonfinite_paths(item, f"{path}.{key}")]
    if isinstance(value, list):
        return [issue for index, item in enumerate(value) for issue in nonfinite_paths(item, f"{path}[{index}]")]
    return []


def type_matches(value: Any, expected: Any) -> bool | None:
    if isinstance(expected, list):
        return any(type_matches(value, item) for item in expected)
    if not isinstance(expected, str):
        return None
    expected = expected.lower().strip()
    if expected in {"any", "unknown", "null"}:
        return value is None if expected == "null" else None
    python_type = TYPE_MAP.get(expected)
    if python_type is None:
        return None
    if expected in {"number", "float", "integer", "int"} and isinstance(value, bool):
        return False
    return isinstance(value, python_type)


def merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def assemble_partial(case: dict[str, Any], fields: list[dict[str, Any]], work_dir: Path, output_dir: Path) -> tuple[dict, list[str], list[str]]:
    candidates = []
    checkpoint_dir = work_dir / "checkpoints"
    if checkpoint_dir.is_dir():
        candidates.extend(
            sorted(checkpoint_dir.glob("*.json"), key=lambda p: (p.stat().st_mtime, str(p)))
        )
    candidates.extend([work_dir / "result_checkpoint.json", work_dir / "checkpoint.json"])
    existing = [p for p in candidates if p.is_file()]
    if not existing:
        raise FileNotFoundError(f"no checkpoint.json, result_checkpoint.json, or checkpoints/*.json under {work_dir}")
    partial: dict[str, Any] = {field["name"]: None for field in fields}
    notes = [f"Loaded checkpoint {p}" for p in existing]
    for path in existing:
        payload = load_json(path)
        if not isinstance(payload, dict):
            notes.append(f"Ignored non-object checkpoint {path}")
            continue
        result_payload = payload.get("result")
        partial = merge(partial, result_payload if isinstance(result_payload, dict) else payload)
    partial["case_id"] = case["case_id"]
    partial["status"] = "partial"
    partial.setdefault("sources", [])
    uncertainty = partial.get("uncertainty")
    message = "Best-effort partial assembly; null contract fields are pending and must not be interpreted as results."
    if isinstance(uncertainty, list):
        uncertainty.append(message)
    elif uncertainty:
        partial["uncertainty"] = [str(uncertainty), message]
    else:
        partial["uncertainty"] = [message]
    change_log = partial.get("change_log")
    if not isinstance(change_log, list):
        change_log = []
    change_log.append({"at": utc_now_iso(), "event": "best_effort_partial_assembled", "checkpoints": [str(p) for p in existing]})
    partial["change_log"] = change_log
    destination = output_dir / "result.json"
    sanitized = atomic_write_json(destination, partial)
    if sanitized:
        notes.append(f"Nonfinite checkpoint values converted to null: {sanitized}")
        partial = load_json(destination)
    return partial, notes, [str(p) for p in existing]


def recon_entries(payload: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for key in ("files", "inputs", "input_files", "artifacts"):
            value = payload.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and any(k in item for k in ("sha256", "sha256_hex")):
                        entries.append(item)
        hashes = payload.get("sha256")
        if isinstance(hashes, dict):
            entries.extend({"path": path, "sha256": digest} for path, digest in hashes.items())
        if not entries and any(k in payload for k in ("path", "file", "name")) and "sha256" in payload:
            entries.append(payload)
    return entries


def resolve_input(input_dir: Path, entry: dict[str, Any]) -> tuple[Path, str]:
    raw = str(entry.get("path", entry.get("file", entry.get("relative_path", entry.get("name", "")))))
    path = Path(raw)
    if path.is_absolute():
        parts = list(path.parts)
        lowered = [part.lower() for part in parts]
        if "input" in lowered:
            relative = Path(*parts[len(lowered) - 1 - lowered[::-1].index("input") + 1:])
        else:
            relative = Path(path.name)
    else:
        relative = path
    resolved = (input_dir / relative).resolve()
    root = input_dir.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"recon path escapes --input-dir: {raw}")
    return resolved, relative.as_posix()


def verify_inputs(input_dir: Path, recon_path: Path) -> tuple[dict, list[dict], list[dict]]:
    fatal: list[dict] = []
    warnings: list[dict] = []
    payload = load_json(recon_path)
    entries = recon_entries(payload)
    if not entries:
        warnings.append({"code": "recon_no_hashes", "message": "no file SHA-256 entries found in recon JSON"})
    checked: list[dict] = []
    recorded: set[Path] = set()
    for entry in entries:
        try:
            path, relative = resolve_input(input_dir, entry)
            recorded.add(path)
            expected = str(entry.get("sha256", entry.get("sha256_hex"))).lower()
            if not path.is_file():
                fatal.append({"code": "input_missing", "path": relative, "message": "recorded raw input is missing"})
                continue
            actual = sha256_file(path)
            passed = actual == expected
            checked.append({"path": relative, "expected_sha256": expected, "actual_sha256": actual, "pass": passed})
            if not passed:
                fatal.append({"code": "input_hash_mismatch", "path": relative, "message": "raw input was modified"})
        except Exception as exc:
            fatal.append({"code": "input_check_error", "message": str(exc)})
    for path in sorted(input_dir.rglob("*")):
        if path.is_file() and path.resolve() not in recorded:
            warnings.append({"code": "input_not_recorded", "path": path.relative_to(input_dir).as_posix()})
    return {"recon": str(recon_path), "checked": checked, "unchanged": not fatal}, fatal, warnings


def validate_result(result: Any, case: dict[str, Any], fields: list[dict[str, Any]]) -> tuple[list[dict], list[dict], list[dict]]:
    checks: list[dict] = []
    fatal: list[dict] = []
    warnings: list[dict] = []
    if not isinstance(result, dict):
        return checks, [{"code": "result_not_object", "message": "result.json must contain a JSON object"}], warnings
    names = [field["name"] for field in fields]
    for name in dict.fromkeys(names + list(REQUIRED_METADATA)):
        present = name in result
        checks.append({"check": f"field:{name}", "pass": present})
        if not present:
            fatal.append({"code": "missing_field", "field": name, "fix": f"add {name!r}; use null plus uncertainty if genuinely pending"})
        elif result[name] is None:
            warnings.append({"code": "pending_field", "field": name, "fix": "replace null with a computed value, or retain null and explain the limitation"})
    if "case_id" in result and result["case_id"] != case.get("case_id"):
        fatal.append({"code": "case_id_mismatch", "expected": case.get("case_id"), "actual": result["case_id"]})
    for field in fields:
        name = field["name"]
        if name in result and result[name] is not None:
            matches = type_matches(result[name], field.get("type"))
            if matches is False:
                fatal.append({"code": "wrong_field_type", "field": name, "expected": field.get("type"), "actual": type(result[name]).__name__})
    for issue in nonfinite_paths(result):
        fatal.append({"code": "nonfinite_number", "path": issue, "fix": "replace NaN/Infinity with null and document why"})
    if result.get("status") not in {"complete", "partial", "failed"}:
        warnings.append({"code": "unconventional_status", "actual": result.get("status"), "fix": "use complete, partial, or failed"})
    for name in ("sources", "change_log"):
        if isinstance(result.get(name), list) and not result[name]:
            warnings.append({"code": "empty_provenance", "field": name, "fix": "record actual source paths and processing changes"})
    return checks, fatal, warnings


def read_optional_audit(path: str | None) -> tuple[Any, list[dict]]:
    if not path:
        return None, []
    try:
        audit = load_json(Path(path))
        warnings = []
        if audit.get("engine_consistency") is False:
            warnings.append({"code": "engine_mismatch", "message": "dual_audit reported mismatch or audit failure", "fix": "inspect mismatches[] in the audit JSON"})
        warnings.extend({"code": "audit_warning", "message": str(item)} for item in audit.get("warnings", []))
        return audit, warnings
    except Exception as exc:
        return None, [{"code": "audit_unreadable", "message": str(exc)}]


def artifact_inventory(output_dir: Path) -> list[dict[str, Any]]:
    inventory = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or path.name == "manifest.json" or path.name.startswith("."):
            continue
        inventory.append({
            "path": path.relative_to(output_dir).as_posix(), "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return inventory


def verification_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Verification", "", f"- Case: `{report['case_id']}`",
        f"- Gate status: **{report['status'].upper()}** (exit code {report['exit_code']})",
        f"- Result: `{report['result_path']}`", f"- Generated: {report['generated_at']}",
        "- Hash inventory: `manifest.json` (the manifest excludes itself).", "",
        "## Checks", "", "| Area | Result | Evidence |", "|---|---|---|",
    ]
    for area, key in (("Output contract", "contract"), ("Raw input integrity", "input_integrity"), ("Pandas ↔ SQLite", "dual_engine")):
        detail = report[key]
        lines.append(f"| {area} | {'PASS' if detail['pass'] else 'REVIEW'} | {detail['evidence']} |")
    lines.extend(["", "## Output artifacts", "", "See `manifest.json` for SHA-256 and byte sizes.", ""])
    if report["fix_list"]:
        lines.extend(["## Fix list", ""])
        lines.extend(f"- **{item['code']}**: {item.get('fix', item.get('message', json.dumps(item, ensure_ascii=False)))}" for item in report["fix_list"])
    else:
        lines.append("No fixes required.")
    lines.extend(["", "## Honesty boundary", "", "A passing gate verifies structure and integrity, not scientific truth. Pending/null fields, failed queries, outliers, and uncertainty remain explicit; no value is imputed by this gate.", ""])
    return "\n".join(lines)


def run_gate(args: argparse.Namespace) -> tuple[dict, int]:
    case_path, output_dir, work_dir = Path(args.case), Path(args.output_dir), Path(args.work_dir)
    case = load_json(case_path)
    if not isinstance(case, dict) or "case_id" not in case or "output_contract" not in case:
        raise ValueError("case.json must contain case_id and output_contract")
    fields = contract_fields(case)
    output_dir.mkdir(parents=True, exist_ok=True)
    fatal: list[dict] = []
    warnings: list[dict] = []
    checkpoints: list[str] = []
    result_path = output_dir / "result.json"
    if not result_path.exists() and args.best_effort:
        try:
            _, notes, checkpoints = assemble_partial(case, fields, work_dir, output_dir)
            warnings.extend({"code": "best_effort", "message": note} for note in notes)
        except Exception as exc:
            fatal.append({"code": "partial_assembly_failed", "message": str(exc), "fix": "create a real checkpoint before the budget cutoff"})
    if not result_path.is_file():
        fatal.append({"code": "result_missing", "path": str(result_path), "fix": "write result.json atomically before finalization"})
        result = None
    else:
        try:
            result = load_json(result_path)
        except Exception as exc:
            result = None
            fatal.append({"code": "result_invalid_json", "message": str(exc), "fix": "write strict RFC-8259 JSON"})
    checks, result_fatal, result_warnings = validate_result(result, case, fields)
    fatal.extend(result_fatal)
    warnings.extend(result_warnings)
    input_integrity = {"pass": True, "evidence": "not requested (requires --input-dir and --recon)"}
    if args.input_dir and args.recon:
        try:
            detail, input_fatal, input_warnings = verify_inputs(Path(args.input_dir), Path(args.recon))
            input_integrity = {"pass": not input_fatal, "evidence": str(args.recon), **detail}
            fatal.extend(input_fatal)
            warnings.extend(input_warnings)
        except Exception as exc:
            input_integrity = {"pass": False, "evidence": str(args.recon)}
            fatal.append({"code": "input_integrity_failed", "message": str(exc)})
    elif args.input_dir or args.recon:
        warnings.append({"code": "integrity_not_run", "message": "both --input-dir and --recon are required for hash verification"})
    audit, audit_warnings = read_optional_audit(args.audit)
    warnings.extend(audit_warnings)
    dual_engine = {
        "pass": bool(audit and audit.get("engine_consistency") is True),
        "evidence": args.audit or "not supplied",
        "mismatches": len(audit.get("mismatches", [])) if isinstance(audit, dict) else None,
    }
    if not args.audit:
        warnings.append({"code": "audit_not_supplied", "message": "run dual_audit.py and pass --audit for independent numerical evidence"})
    if isinstance(result, dict) and result.get("status") == "partial" and not any(w["code"] == "best_effort" for w in warnings):
        warnings.append({"code": "partial_result", "message": "result declares partial status"})
    exit_code = 2 if fatal else (1 if warnings else 0)
    status = "fail" if fatal else ("partial" if warnings else "pass")
    fix_list = fatal + warnings
    return {
        "schema_version": "1.0", "generated_at": utc_now_iso(), "case_id": case["case_id"],
        "status": status, "exit_code": exit_code, "result_path": str(result_path),
        "contract": {"pass": not result_fatal, "evidence": str(result_path), "fields_checked": checks},
        "input_integrity": input_integrity, "dual_engine": dual_engine,
        "checkpoints_used": checkpoints, "fatal_errors": fatal, "warnings": warnings,
        "fix_list": fix_list,
    }, exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--input-dir")
    parser.add_argument("--recon")
    parser.add_argument("--audit")
    parser.add_argument("--best-effort", action="store_true")
    parser.add_argument("--json-out", required=True)
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        report, exit_code = run_gate(args)
    except Exception as exc:
        exit_code = 2
        report = {
            "schema_version": "1.0", "generated_at": utc_now_iso(), "case_id": "unknown",
            "status": "fail", "exit_code": exit_code, "result_path": str(output_dir / "result.json"),
            "contract": {"pass": False, "evidence": "gate initialization failed"},
            "input_integrity": {"pass": False, "evidence": "not run"},
            "dual_engine": {"pass": False, "evidence": "not run"},
            "fatal_errors": [{"code": "gate_error", "message": f"{type(exc).__name__}: {exc}"}],
            "warnings": [], "fix_list": [{"code": "gate_error", "message": str(exc), "fix": "inspect case.json and CLI paths"}],
        }
    qc_path = output_dir / "qc_report.json"
    atomic_write_json(qc_path, report)
    json_out = Path(args.json_out)
    if json_out.resolve() != qc_path.resolve():
        atomic_write_json(json_out, report)
    atomic_write_text(output_dir / "VERIFICATION.md", verification_markdown(report))
    manifest = {
        "schema_version": "1.0", "generated_at": utc_now_iso(), "case_id": report["case_id"],
        "status": report["status"], "hash_algorithm": "SHA-256",
        "self_excluded": "manifest.json", "files": artifact_inventory(output_dir),
    }
    atomic_write_json(output_dir / "manifest.json", manifest)
    emit(report["status"], {
        "exit_code": exit_code, "fatal": len(report.get("fatal_errors", [])),
        "warnings": len(report.get("warnings", [])), "fixes": len(report.get("fix_list", [])),
    }, report)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
