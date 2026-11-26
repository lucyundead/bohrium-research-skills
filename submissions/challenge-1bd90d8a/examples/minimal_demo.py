"""EXAMPLE ONLY: run the complete scientific-figure toolchain on synthetic toy data."""
from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_DIR / "scripts"


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def write_toy_png(path: Path) -> None:
    width, height = 400, 240
    pixels = bytearray([255] * (width * height * 3))

    def rectangle(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        for y in range(y0, y1):
            for x in range(x0, x1):
                offset = (y * width + x) * 3
                pixels[offset:offset + 3] = bytes(color)

    rectangle(30, 35, 370, 39, (38, 50, 56))
    rectangle(65, 90, 165, 150, (35, 100, 170))
    rectangle(235, 90, 335, 150, (0, 158, 115))
    rectangle(164, 116, 236, 122, (38, 50, 56))
    rectangle(65, 185, 335, 189, (38, 50, 56))
    rows = b"".join(b"\x00" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ppm = round(300 / 0.0254)
    content = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr) + png_chunk(b"pHYs", struct.pack(">IIB", ppm, ppm, 1)) + png_chunk(b"IDAT", zlib.compress(rows, 9)) + png_chunk(b"IEND", b"")
    path.write_bytes(content)


def write_toy_pdf(path: Path) -> None:
    stream = b"BT /F1 18 Tf 72 330 Td (EXAMPLE ONLY - synthetic toy figure) Tj 0 -32 Td /F1 11 Tf (Editable SVG and source ledger accompany this preview.) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 400] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    content = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(content))
        content.extend(f"{index} 0 obj\n".encode("ascii"))
        content.extend(obj)
        content.extend(b"\nendobj\n")
    xref = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    content.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    path.write_bytes(content)


def run(script: str, arguments: list[str]) -> None:
    command = [sys.executable, str(SCRIPTS / script), *arguments]
    completed = subprocess.run(command, cwd=SKILL_DIR, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"{script} returned {completed.returncode}; expected 0\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fig_qc_example_") as temporary:
        root = Path(temporary)
        inputs = root / "inputs"
        output = root / "output"
        inputs.mkdir()
        output.mkdir()
        source = inputs / "raw.csv"
        source.write_text("series,value\nEXAMPLE ONLY,1.0\nEXAMPLE ONLY,2.0\nEXAMPLE ONLY,4.0\n", encoding="utf-8")
        labels = inputs / "labels.txt"
        labels.write_text("EXAMPLE ONLY synthetic toy labels\n", encoding="utf-8")
        case = inputs / "case.json"
        case_data = {
            "case_id": "EXAMPLE_ONLY_TOY",
            "output_contract": {
                "case_id": "str",
                "status": "str",
                "artifacts": "dict",
                "qc_summary": "dict",
                "sources": "list",
                "uncertainty": "list",
                "change_log": "list",
            },
        }
        write_json(case, case_data)
        plotted = inputs / "plotted.json"
        write_json(plotted, {
            "series_id": "EXAMPLE_ONLY_TOY",
            "source_file": "raw.csv",
            "column": "value",
            "values": [1.0, 2.0, 4.0],
        })
        spec = inputs / "mechanism.json"
        write_json(spec, {
            "title": "EXAMPLE ONLY — synthetic mechanism",
            "width": 640,
            "height": 400,
            "compartments": [{"id": "toy-area", "label": "TOY AREA", "x": 20, "y": 60, "w": 600, "h": 280}],
            "entities": [
                {"id": "node-a", "label": "NODE A", "x": 80, "y": 150, "w": 140, "h": 70, "shape": "rounded", "source_ref": "EXAMPLE ONLY"},
                {"id": "node-b", "label": "NODE B", "x": 400, "y": 150, "w": 140, "h": 70, "shape": "ellipse", "source_ref": "EXAMPLE ONLY"},
            ],
            "edges": [{"source": "node-a", "target": "node-b", "kind": "activation", "source_ref": "EXAMPLE ONLY"}],
            "legend": [{"id": "toy-relation", "label": "TOY RELATION", "kind": "activation", "x": 30, "y": 370}],
        })
        operations = inputs / "operations.json"
        write_json(operations, [{"op": "set_attr", "target_id": "layer-entities", "attribute": "data-example", "value": "EXAMPLE ONLY", "source_ref": "EXAMPLE ONLY"}])
        result = output / "result.json"
        write_json(result, {
            "case_id": "EXAMPLE_ONLY_TOY",
            "status": "EXAMPLE ONLY - PASS",
            "artifacts": {"svg": "edited.svg", "png": "figure.png", "pdf": "figure.pdf"},
            "qc_summary": {},
            "sources": ["raw.csv (synthetic EXAMPLE ONLY)"],
            "uncertainty": [],
            "change_log": ["Added data-example=EXAMPLE ONLY to the toy entity layer"],
        })

        run("style_inject.py", ["--journal", "nature", "--labels-file", str(labels), "--mplstyle-out", str(output / "figure.mplstyle"), "--json-out", str(output / "style_report.json")])
        run("source_ledger.py", ["build", "--case", str(case), "--sources", str(source), "--plotted", str(plotted), "--json-out", str(output / "source_mapping.json"), "--md-out", str(output / "SOURCE_MAP.md")])
        run("mechanism_svg.py", ["build", "--spec", str(spec), "--out", str(output / "mechanism.svg"), "--json-out", str(output / "mechanism_report.json")])
        run("svg_edit.py", ["apply", "--svg", str(output / "mechanism.svg"), "--ops", str(operations), "--out", str(output / "edited.svg"), "--log", str(output / "edit_log.json"), "--preservation", str(output / "preservation.json")])
        write_toy_png(output / "figure.png")
        write_toy_pdf(output / "figure.pdf")
        run("fig_qc.py", [
            "--svg", str(output / "edited.svg"),
            "--png", str(output / "figure.png"),
            "--pdf", str(output / "figure.pdf"),
            "--ledger", str(output / "source_mapping.json"),
            "--plotted", str(plotted),
            "--source", str(source),
            "--case", str(case),
            "--result", str(result),
            "--output-dir", str(output),
            "--min-dpi", "300",
            "--json-out", str(output / "qc_report.json"),
        ])
        report = json.loads((output / "qc_report.json").read_text(encoding="utf-8"))
        if report["status"] != "PASS" or len(report["checks"]) != 7 or not all(item["pass"] for item in report["checks"]):
            raise RuntimeError("fig_qc.py did not produce a seven-check PASS report")
        for required in ("manifest.json", "VERIFICATION.md"):
            if not (output / required).is_file():
                raise RuntimeError(f"Missing QC evidence: {required}")
    print("EXAMPLE ONLY: all five scripts exited 0; seven QC checks passed; temporary artifacts cleaned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
