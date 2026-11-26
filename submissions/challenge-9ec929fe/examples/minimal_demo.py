#!/usr/bin/env python3
"""EXAMPLE ONLY: run all four tools on temporary synthetic toy data."""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_DIR / "scripts"

ANALYSIS = r'''# EXAMPLE ONLY: synthetic analysis, not a scientific result.
import csv
import json
import os
import random
import sys
from pathlib import Path
from PIL import Image, ImageDraw

input_path, output_dir, work_dir = map(Path, sys.argv[1:4])
output_dir.mkdir(parents=True, exist_ok=True)
work_dir.mkdir(parents=True, exist_ok=True)
random.seed(7)
with input_path.open(newline="", encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))
values = [float(row["measurement"]) for row in rows]
result = {
    "case_id": "synthetic-demo",
    "status": "complete",
    "row_count": len(rows),
    "mean_value": sum(values) / len(values),
    "plot": "figure.png",
    "sources": [str(input_path)],
    "uncertainty": [],
    "change_log": ["Computed the arithmetic mean without cleaning or row removal."],
}
image = Image.new("RGB", (640, 400), "white")
draw = ImageDraw.Draw(image)
draw.rectangle((60, 30, 600, 350), outline="black", width=2)
low, high = min(values), max(values)
points = []
for index, value in enumerate(values):
    x = 75 + index * 500 // (len(values) - 1)
    y = 335 - int((value - low) / (high - low) * 280)
    points.append((x, y))
draw.line(points, fill="navy", width=3)
for point in points:
    draw.ellipse((point[0] - 3, point[1] - 3, point[0] + 3, point[1] + 3), fill="orange")
draw.text((65, 10), "EXAMPLE ONLY - synthetic measurements", fill="black")
image.save(output_dir / "figure.png", dpi=(300, 300))
for target in (output_dir / "result.json", work_dir / "checkpoint.json"):
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, target)
'''


def run(command: list[str]) -> None:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    print(completed.stdout.strip())
    if completed.returncode:
        print(completed.stderr.strip(), file=sys.stderr)
        raise RuntimeError(f"command exited {completed.returncode}: {' '.join(command)}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="research-skill-example-") as temporary:
        root = Path(temporary)
        input_dir, work_dir, output_dir = root / "input", root / "work", root / "output"
        input_dir.mkdir()
        work_dir.mkdir()
        output_dir.mkdir()
        case = {
            "case_id": "synthetic-demo",
            "output_contract": {"fields": [
                {"name": "row_count", "type": "integer"},
                {"name": "mean_value", "type": "number"},
                {"name": "plot", "type": "string"},
            ]},
        }
        (input_dir / "case.json").write_text(json.dumps(case), encoding="utf-8")
        (input_dir / "task.md").write_text(
            "EXAMPLE ONLY: compute the row count and mean measurement; save a PNG.\n",
            encoding="utf-8",
        )
        with (input_dir / "toy.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["sample_id", "group_name", "measurement"])
            writer.writeheader()
            for index in range(20):
                writer.writerow({
                    "sample_id": f"toy-{index + 1:02d}",
                    "group_name": "A" if index % 2 == 0 else "B",
                    "measurement": 4.0 + index * 0.1 + (index % 5) * 0.25,
                })
        analysis = work_dir / "analysis.py"
        analysis.write_text(ANALYSIS, encoding="utf-8")
        checks = {"checks": [{
            "id": "toy_summary",
            "sql": "SELECT COUNT(measurement) AS n, AVG(measurement) AS mean_value FROM data",
            "pandas": {
                "n": {"column": "measurement", "metric": "count"},
                "mean_value": {"column": "measurement", "metric": "mean"},
            },
        }]}
        (work_dir / "checks.json").write_text(json.dumps(checks), encoding="utf-8")
        python = sys.executable
        run([python, str(SCRIPTS / "recon.py"), "--input-dir", str(input_dir),
             "--work-dir", str(work_dir), "--json-out", str(work_dir / "recon.json")])
        run([python, str(SCRIPTS / "run_guard.py"), "--script", str(analysis),
             "--work-dir", str(work_dir), "--out-dir", str(output_dir), "--timeout", "120",
             "--json-out", str(work_dir / "run_report.json"), "--",
             str(input_dir / "toy.csv"), str(output_dir), str(work_dir)])
        run([python, str(SCRIPTS / "dual_audit.py"), "--input", str(input_dir / "toy.csv"),
             "--checks", str(work_dir / "checks.json"), "--script", str(analysis),
             "--figures", str(output_dir / "figure.png"),
             "--json-out", str(output_dir / "crosscheck_report.json")])
        run([python, str(SCRIPTS / "contract_gate.py"), "--case", str(input_dir / "case.json"),
             "--output-dir", str(output_dir), "--work-dir", str(work_dir),
             "--input-dir", str(input_dir), "--recon", str(work_dir / "recon.json"),
             "--audit", str(output_dir / "crosscheck_report.json"), "--best-effort",
             "--json-out", str(output_dir / "qc_report.json")])
        audit = json.loads((output_dir / "crosscheck_report.json").read_text(encoding="utf-8"))
        gate = json.loads((output_dir / "qc_report.json").read_text(encoding="utf-8"))
        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
        assert audit["engine_consistency"] and not audit["mismatches"]
        assert audit["statistical_quality"]["rows"] == 20
        assert audit["figures"][0]["nonblank_pass"] and audit["figures"][0]["dpi_300_pass"]
        assert gate["status"] == "pass" and gate["exit_code"] == 0
        names = {item["path"] for item in manifest["files"]}
        expected = {"result.json", "figure.png", "crosscheck_report.json", "qc_report.json", "VERIFICATION.md"}
        assert expected <= names
        print("EXAMPLE ONLY: all four scripts passed; synthetic artifacts and hashes verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
