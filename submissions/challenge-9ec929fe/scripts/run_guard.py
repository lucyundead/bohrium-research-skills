#!/usr/bin/env python3
"""Run a Python analysis script deterministically and persist a compact execution report."""
from __future__ import annotations

import argparse
import csv
import difflib
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import sqlite3
except ImportError:  # pragma: no cover - depends on the Python build
    sqlite3 = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SKILL_DIR, atomic_write_json, emit, sha256_file, utc_now_iso

ENV_OVERRIDES = {
    "MPLBACKEND": "Agg",
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONHASHSEED": os.environ.get("PYTHONHASHSEED", "0"),
}
TAIL_CHARS = 2000


def decode_tail(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = value
    return text[-TAIL_CHARS:]


def snapshot(directory: Path) -> dict[str, dict[str, Any]]:
    result = {}
    if not directory.exists():
        return result
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        relative = str(path.relative_to(directory))
        try:
            stat = path.stat()
            result[relative] = {
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": sha256_file(path),
            }
        except OSError as exc:
            result[relative] = {"error": f"{type(exc).__name__}: {exc}"}
    return result


def changed_artifacts(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    artifacts = []
    for relative, current in sorted(after.items()):
        previous = before.get(relative)
        if previous is not None and previous == current:
            continue
        artifact = {
            "path": relative,
            "status": "new" if previous is None else "updated",
            **current,
        }
        artifacts.append(artifact)
    return artifacts


def read_first_delimited_row(path: Path) -> list[str]:
    raw = path.read_bytes()[:65536]
    if raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
    delimiter = "\t" if path.suffix.casefold() == ".tsv" else ","
    try:
        delimiter = csv.Sniffer().sniff(text, delimiters=",\t;|").delimiter
    except csv.Error:
        pass
    return next(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter), [])


def candidate_data_paths(
    work_dir: Path, script_args: list[str], input_dir: Path | None = None
) -> list[Path]:
    paths = []
    for value in script_args:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = work_dir / candidate
        if candidate.is_file():
            paths.append(candidate)
    roots = [input_dir, Path("/input"), work_dir.parent / "input", work_dir]
    for root in roots:
        if root is None or not root.is_dir():
            continue
        for candidate in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
            if candidate.is_file() and candidate.suffix.casefold() in {
                ".csv", ".tsv", ".json", ".db", ".sqlite", ".sqlite3"
            }:
                paths.append(candidate)
            if len(paths) >= 40:
                break
        if len(paths) >= 40:
            break
    unique = []
    seen = set()
    for path in paths:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique[:40]


def available_columns(
    work_dir: Path, script_args: list[str], input_dir: Path | None = None
) -> list[dict[str, str]]:
    columns = []
    seen = set()
    for path in candidate_data_paths(work_dir, script_args, input_dir):
        try:
            if path.stat().st_size > 16 * 1024 * 1024:
                continue
            names: list[str] = []
            suffix = path.suffix.casefold()
            if suffix in {".csv", ".tsv"}:
                names = read_first_delimited_row(path)
            elif suffix == ".json":
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    names = [str(key) for key in data.keys()]
                elif isinstance(data, list) and data and isinstance(data[0], dict):
                    names = [str(key) for key in data[0].keys()]
            elif suffix in {".db", ".sqlite", ".sqlite3"} and sqlite3 is not None:
                uri_path = path.resolve().as_posix()
                connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
                try:
                    tables = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    ).fetchall()
                    for (table,) in tables[:20]:
                        quoted = '"' + str(table).replace('"', '""') + '"'
                        for definition in connection.execute(f"PRAGMA table_info({quoted})").fetchall():
                            names.append(str(definition[1]))
                finally:
                    connection.close()
            for name in names:
                key = (name, str(path))
                if key not in seen:
                    seen.add(key)
                    columns.append({"name": name, "source": str(path)})
        except Exception:
            continue
    return columns[:500]


def fuzzy_columns(missing: str, candidates: list[dict[str, str]]) -> list[dict[str, Any]]:
    if not candidates:
        return []
    normalized = re.sub(r"\s+", " ", missing.strip()).casefold()
    exact = [
        candidate
        for candidate in candidates
        if re.sub(r"\s+", " ", candidate["name"].strip()).casefold() == normalized
    ]
    names = list(dict.fromkeys(candidate["name"] for candidate in candidates))
    close = difflib.get_close_matches(missing, names, n=5, cutoff=0.45)
    ordered_names = list(dict.fromkeys([candidate["name"] for candidate in exact] + close))
    suggestions = []
    for name in ordered_names:
        source = next(candidate["source"] for candidate in candidates if candidate["name"] == name)
        suggestions.append({"column": name, "source": source})
    return suggestions[:5]


def diagnosis_item(code: str, evidence: str, recommendation: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "evidence": evidence[-TAIL_CHARS:], "recommendation": recommendation, **extra}


def diagnose(
    returncode: int | None,
    timed_out: bool,
    stdout_tail: str,
    stderr_tail: str,
    work_dir: Path,
    script_args: list[str],
    input_dir: Path | None = None,
) -> list[dict[str, Any]]:
    if returncode == 0 and not timed_out:
        return []
    combined = "\n".join(part for part in (stderr_tail, stdout_tail) if part)
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    final = lines[-1] if lines else ""
    diagnoses = []
    if timed_out:
        diagnoses.append(
            diagnosis_item(
                "TimeoutExpired",
                final or "The child process exceeded the configured timeout.",
                "Reduce workload, add chunking/checkpoints, or rerun with a justified larger timeout.",
            )
        )
    module_match = re.search(r"ModuleNotFoundError:\s*No module named ['\"]([^'\"]+)['\"]", combined)
    if module_match:
        module = module_match.group(1)
        diagnoses.append(
            diagnosis_item(
                "ModuleNotFoundError",
                module_match.group(0),
                "Use an available stdlib/scientific-library fallback; do not assume network package installation is possible.",
                missing_module=module,
            )
        )
    file_match = re.search(
        r"FileNotFoundError:.*?(?:No such file or directory|cannot find the file specified).*?['\"]([^'\"]+)['\"]",
        combined,
        re.IGNORECASE,
    )
    if "FileNotFoundError" in combined:
        diagnoses.append(
            diagnosis_item(
                "FileNotFoundError",
                final,
                "Resolve paths from actual case inputs and verify existence before opening; do not write into raw input directories.",
                missing_path=file_match.group(1) if file_match else None,
            )
        )
    key_match = re.search(r"KeyError:\s*(['\"])(.*?)\1\s*$", final)
    if "KeyError" in combined:
        missing = key_match.group(2) if key_match else None
        candidates = available_columns(work_dir, script_args, input_dir) if missing else []
        suggestions = fuzzy_columns(missing, candidates) if missing else []
        recommendation = "Check the exact mapping key and preserve source labels."
        if candidates:
            recommendation = "Compare the key with actual source columns, including case and leading/trailing whitespace."
        diagnoses.append(
            diagnosis_item(
                "KeyError",
                final,
                recommendation,
                missing_key=missing,
                fuzzy_column_matches=suggestions,
                columns_inspected=len(candidates),
            )
        )
    if "sqlite3.OperationalError" in combined or re.search(r"OperationalError:.*(?:sqlite|table|column|database)", final, re.IGNORECASE):
        diagnoses.append(
            diagnosis_item(
                "sqlite3.OperationalError",
                final,
                "Inspect sqlite_master and PRAGMA table_info, quote identifiers, and open source databases with URI mode=ro.",
            )
        )
    if re.search(r"(?:^|\n)\s*(?:ValueError|TypeError):", combined) or final.startswith(("ValueError:", "TypeError:")):
        diagnoses.append(
            diagnosis_item(
                "ValueError/TypeError",
                final,
                "Check input shape, numeric conversion, missing sentinels, and function argument types; retain the original exception evidence.",
            )
        )
    if "MemoryError" in combined:
        diagnoses.append(
            diagnosis_item(
                "MemoryError",
                final,
                "Stream records, reduce copied arrays/dataframes, process chunks, and save intermediate checkpoints.",
            )
        )
    if "SyntaxError" in combined or "IndentationError" in combined:
        location = re.search(r'File\s+["\']([^"\']+)["\'],\s*line\s+(\d+)', combined)
        diagnoses.append(
            diagnosis_item(
                "SyntaxError",
                final,
                "Run py_compile, inspect the reported line and quoting/indentation, then rerun the real analysis command.",
                file=location.group(1) if location else None,
                line=int(location.group(2)) if location else None,
            )
        )
    if not diagnoses:
        diagnoses.append(
            diagnosis_item(
                "ScriptFailed",
                final or f"Child exited with return code {returncode}.",
                "Inspect the persisted stdout/stderr tails and reproduce with the recorded command and working directory.",
            )
        )
    return diagnoses


def resolve_script(script: str, work_dir: Path) -> Path:
    path = Path(script).expanduser()
    if path.is_absolute():
        return path.resolve()
    from_cwd = (Path.cwd() / path).resolve()
    if from_cwd.is_file():
        return from_cwd
    return (work_dir / path).resolve()


def execute_guard(args: argparse.Namespace, script_args: list[str]) -> dict[str, Any]:
    started_at = utc_now_iso()
    started = time.perf_counter()
    work_dir = args.work_dir.expanduser().resolve()
    out_dir = args.out_dir.expanduser().resolve()
    input_dir = args.input_dir.expanduser().resolve() if args.input_dir else None
    work_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.timeout <= 0:
        raise ValueError("--timeout must be greater than zero")
    script = resolve_script(args.script, work_dir)
    if not script.is_file():
        raise FileNotFoundError(f"Target script does not exist: {script}")
    before = snapshot(out_dir)
    environment = os.environ.copy()
    environment.update(ENV_OVERRIDES)
    command = [sys.executable, str(script), *script_args]
    timed_out = False
    returncode = None
    stdout_tail = ""
    stderr_tail = ""
    try:
        completed = subprocess.run(
            command,
            cwd=str(work_dir),
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=args.timeout,
            check=False,
        )
        returncode = completed.returncode
        stdout_tail = decode_tail(completed.stdout)
        stderr_tail = decode_tail(completed.stderr)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_tail = decode_tail(exc.stdout)
        stderr_tail = decode_tail(exc.stderr)
    finished_at = utc_now_iso()
    duration = time.perf_counter() - started
    after = snapshot(out_dir)
    artifacts = changed_artifacts(before, after)
    diagnoses = diagnose(returncode, timed_out, stdout_tail, stderr_tail, work_dir, script_args, input_dir)
    if timed_out:
        status = "timeout"
        runner_exit_code = 124
    elif returncode == 0:
        status = "ok"
        runner_exit_code = 0
    else:
        status = "script_failed"
        runner_exit_code = 1
    return {
        "schema_version": 1,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration,
        "skill_dir": str(SKILL_DIR),
        "runner": {"python": sys.executable, "pid": os.getpid()},
        "command": command,
        "script": str(script),
        "script_args": script_args,
        "work_dir": str(work_dir),
        "out_dir": str(out_dir),
        "input_dir": str(input_dir) if input_dir else None,
        "timeout_seconds": args.timeout,
        "timed_out": timed_out,
        "environment_overrides": ENV_OVERRIDES,
        "returncode": returncode,
        "runner_exit_code": runner_exit_code,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "tail_char_limit": TAIL_CHARS,
        "artifacts": artifacts,
        "diagnosis": diagnoses,
    }


def split_script_args(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" not in argv:
        return argv, []
    index = argv.index("--")
    return argv[:index], argv[index + 1:]


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    guard_args, script_args = split_script_args(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", required=True)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--input-dir", type=Path, help="optional read-only raw input directory for diagnostics")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--json-out", required=True, type=Path)
    args = parser.parse_args(guard_args)
    return args, script_args


def main() -> int:
    args, script_args = parse_args(sys.argv[1:])
    try:
        report = execute_guard(args, script_args)
    except Exception as exc:
        report = {
            "schema_version": 1,
            "status": "runner_error",
            "generated_at": utc_now_iso(),
            "skill_dir": str(SKILL_DIR),
            "script": args.script,
            "script_args": script_args,
            "work_dir": str(args.work_dir),
            "out_dir": str(args.out_dir),
            "input_dir": str(args.input_dir) if args.input_dir else None,
            "timeout_seconds": args.timeout,
            "runner_exit_code": 125,
            "error": f"{type(exc).__name__}: {exc}",
            "diagnosis": [
                diagnosis_item(
                    "RunnerError",
                    str(exc),
                    "Check the target script, directories, timeout, and JSON output path; no child success is claimed.",
                )
            ],
        }
    try:
        emit(
            report["status"],
            {
                "runner_exit_code": report["runner_exit_code"],
                "returncode": report.get("returncode"),
                "duration_seconds": report.get("duration_seconds"),
                "artifacts": len(report.get("artifacts", [])),
                "diagnosis": [item["code"] for item in report.get("diagnosis", [])],
            },
            report,
            args.json_out,
        )
    except Exception as exc:
        print(f"run_guard could not write its JSON report: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 125
    return int(report["runner_exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
