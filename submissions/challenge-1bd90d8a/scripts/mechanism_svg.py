"""Spec-driven editable mechanism/flowchart SVG builder and honest render fallback chain."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from common import atomic_write_json, atomic_write_text, emit, utc_now_iso

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)
SHAPES = {"rect", "rounded", "ellipse", "hexagon"}
KINDS = {"activation", "inhibition", "binding", "translocation"}
FALLBACK_GUIDANCE = (
    "No local renderer completed the requested export. Call the container "
    "browser-automation tool to open the SVG and export a PNG/PDF, or call "
    "office-renderer where supported; preserve editable text, use >=300 DPI "
    "for PNG, then run scripts/fig_qc.py on the real outputs."
)


class InputError(ValueError):
    pass


def qname(tag: str) -> str:
    return f"{{{SVG_NS}}}{tag}"


def fmt(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise InputError(f"{name} must be {'positive and ' if positive else ''}finite")
    return result


def safe_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{name} must be a non-empty string")
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "-", value.strip()).strip("-")
    if not cleaned:
        raise InputError(f"{name} has no usable XML id characters")
    if not re.match(r"[A-Za-z_:]", cleaned):
        cleaned = f"n-{cleaned}"
    return cleaned


def source_attr(element: ET.Element, source_ref: Any) -> None:
    if source_ref is not None:
        element.set("data-source", str(source_ref))


def rect(item: dict[str, Any], prefix: str) -> dict[str, Any]:
    result = dict(item)
    result["id"] = safe_id(item.get("id"), f"{prefix}.id")
    result["label"] = str(item.get("label", item["id"]))
    for key in ("x", "y"):
        result[key] = number(item.get(key), f"{prefix}.{key}")
    for key in ("w", "h"):
        result[key] = number(item.get(key), f"{prefix}.{key}", positive=True)
    result["fill"] = str(item.get("fill", "#ffffff"))
    result["stroke"] = str(item.get("stroke", "#263238"))
    return result


def load_spec(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot read spec {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InputError("spec root must be an object")
    data["width"] = number(data.get("width"), "width", positive=True)
    data["height"] = number(data.get("height"), "height", positive=True)
    data["title"] = str(data.get("title", ""))
    for collection in ("compartments", "entities", "edges", "legend"):
        value = data.get(collection, [])
        if not isinstance(value, list):
            raise InputError(f"{collection} must be an array")
        data[collection] = value
    for collection in ("compartments", "entities"):
        normalized = []
        for i, item in enumerate(data[collection]):
            if not isinstance(item, dict):
                raise InputError(f"{collection}[{i}] must be an object")
            normalized.append(rect(item, f"{collection}[{i}]"))
        data[collection] = normalized
    for i, entity in enumerate(data["entities"]):
        shape = entity.get("shape", "rect")
        if shape not in SHAPES:
            raise InputError(f"entities[{i}].shape must be one of {sorted(SHAPES)}")
        entity["shape"] = shape
        compartment = entity.get("compartment")
        if compartment is not None and not isinstance(compartment, str):
            raise InputError(f"entities[{i}].compartment must be a string")
    ids = [v["id"] for v in data["compartments"] + data["entities"]]
    if len(ids) != len(set(ids)):
        raise InputError("compartment/entity ids must be unique after XML-safe normalization")
    entity_ids = {v["id"] for v in data["entities"]}
    compartment_ids = {v["id"] for v in data["compartments"]}
    original_to_safe = {
        safe_id(v["id"], "entity.id"): v["id"] for v in data["entities"]
    }
    for i, entity in enumerate(data["entities"]):
        compartment = entity.get("compartment")
        if compartment is not None and compartment not in compartment_ids:
            matches = [v["id"] for v in data["compartments"] if str(v.get("label", "")) == compartment]
            if safe_id(compartment, f"entities[{i}].compartment") not in compartment_ids and not matches:
                raise InputError(f"entities[{i}].compartment references unknown compartment {compartment!r}")
    normalized_edges = []
    seen_pairs = set()
    for i, raw in enumerate(data["edges"]):
        if not isinstance(raw, dict):
            raise InputError(f"edges[{i}] must be an object")
        source = safe_id(raw.get("source"), f"edges[{i}].source")
        target = safe_id(raw.get("target"), f"edges[{i}].target")
        if source not in entity_ids or target not in entity_ids:
            raise InputError(f"edges[{i}] references unknown source/target entity")
        if source == target:
            raise InputError(f"edges[{i}] self-edges are not supported")
        if (source, target) in seen_pairs:
            raise InputError(f"duplicate edge pair {source}->{target} would duplicate SVG id")
        seen_pairs.add((source, target))
        kind = raw.get("kind")
        if kind not in KINDS:
            raise InputError(f"edges[{i}].kind must be one of {sorted(KINDS)}")
        normalized = dict(raw, source=source, target=target, kind=kind)
        normalized["id"] = safe_id(raw.get("id", f"{source}-{target}"), f"edges[{i}].id")
        normalized["label"] = str(raw.get("label", ""))
        normalized_edges.append(normalized)
    data["edges"] = normalized_edges
    data["_original_entity_ids"] = original_to_safe
    return data


def overlaps(a: dict[str, Any], b: dict[str, Any], margin: float = 10.0) -> tuple[float, float]:
    ox = min(a["x"] + a["w"], b["x"] + b["w"]) + margin - max(a["x"], b["x"])
    oy = min(a["y"] + a["h"], b["y"] + b["h"]) + margin - max(a["y"], b["y"])
    return (ox, oy) if ox > 0 and oy > 0 else (0.0, 0.0)


def collision_pairs(entities: list[dict[str, Any]], margin: float = 10.0) -> list[dict[str, Any]]:
    found = []
    for i, a in enumerate(entities):
        for b in entities[i + 1 :]:
            ox, oy = overlaps(a, b, margin)
            if ox and oy:
                found.append({"a": a["id"], "b": b["id"], "overlap_x": ox, "overlap_y": oy})
    return found


def resolve_collisions(spec: dict[str, Any]) -> dict[str, Any]:
    entities = sorted(spec["entities"], key=lambda item: item["id"])
    initial = collision_pairs(entities)
    originals = {v["id"]: (v["x"], v["y"]) for v in entities}
    width, height = spec["width"], spec["height"]
    for _ in range(160):
        changed = False
        for i, a in enumerate(entities):
            for b in entities[i + 1 :]:
                ox, oy = overlaps(a, b)
                if not ox:
                    continue
                changed = True
                if ox <= oy:
                    direction = 1.0 if a["x"] + a["w"] / 2 >= b["x"] + b["w"] / 2 else -1.0
                    if a["x"] == b["x"]:
                        direction = 1.0 if a["id"] > b["id"] else -1.0
                    a["x"] += direction * (ox / 2 + 0.05)
                    b["x"] -= direction * (ox / 2 + 0.05)
                else:
                    direction = 1.0 if a["y"] + a["h"] / 2 >= b["y"] + b["h"] / 2 else -1.0
                    if a["y"] == b["y"]:
                        direction = 1.0 if a["id"] > b["id"] else -1.0
                    a["y"] += direction * (oy / 2 + 0.05)
                    b["y"] -= direction * (oy / 2 + 0.05)
                for node in (a, b):
                    node["x"] = min(max(0.0, node["x"]), max(0.0, width - node["w"]))
                    node["y"] = min(max(0.0, node["y"]), max(0.0, height - node["h"]))
        if not changed:
            break
    unresolved = collision_pairs(entities)
    adjustments = [
        {
            "id": node["id"],
            "from": {"x": originals[node["id"]][0], "y": originals[node["id"]][1]},
            "to": {"x": node["x"], "y": node["y"]},
        }
        for node in entities
        if originals[node["id"]] != (node["x"], node["y"])
    ]
    return {"initial_collisions": initial, "adjustments": adjustments, "unresolved_collisions": unresolved}


def add_text(parent: ET.Element, text: str, x: float, y: float, *, size: float = 14,
             anchor: str = "middle", css_class: str = "label") -> ET.Element:
    node = ET.SubElement(parent, qname("text"), {
        "x": fmt(x), "y": fmt(y), "text-anchor": anchor,
        "dominant-baseline": "middle", "font-size": fmt(size), "class": css_class,
    })
    node.text = text
    return node


def add_markers(defs: ET.Element) -> None:
    activation = ET.SubElement(defs, qname("marker"), {
        "id": "marker-activation", "viewBox": "0 0 12 12", "refX": "10", "refY": "6",
        "markerWidth": "12", "markerHeight": "12", "markerUnits": "userSpaceOnUse", "orient": "auto",
    })
    ET.SubElement(activation, qname("path"), {"d": "M 1 1 L 11 6 L 1 11 Z", "fill": "#2364aa"})
    inhibition = ET.SubElement(defs, qname("marker"), {
        "id": "marker-inhibition", "viewBox": "-2 -8 8 16", "refX": "0", "refY": "0",
        "markerWidth": "12", "markerHeight": "18", "markerUnits": "userSpaceOnUse", "orient": "auto",
    })
    ET.SubElement(inhibition, qname("path"), {
        "d": "M 0 -7 L 0 7", "fill": "none", "stroke": "#c3423f", "stroke-width": "2.5",
    })
    translocation = ET.SubElement(defs, qname("marker"), {
        "id": "marker-translocation", "viewBox": "0 0 12 12", "refX": "10", "refY": "6",
        "markerWidth": "12", "markerHeight": "12", "markerUnits": "userSpaceOnUse", "orient": "auto",
    })
    ET.SubElement(translocation, qname("path"), {
        "d": "M 1 1 L 11 6 L 1 11 Z", "fill": "#6a4c93",
    })


def shape_node(parent: ET.Element, entity: dict[str, Any]) -> None:
    common = {"fill": entity["fill"], "stroke": entity["stroke"], "stroke-width": "1.6"}
    w, h, shape = entity["w"], entity["h"], entity["shape"]
    if shape == "ellipse":
        ET.SubElement(parent, qname("ellipse"), dict(common, cx=fmt(w / 2), cy=fmt(h / 2), rx=fmt(w / 2), ry=fmt(h / 2)))
    elif shape == "hexagon":
        points = [(w * 0.12, 0), (w * 0.88, 0), (w, h / 2), (w * 0.88, h), (w * 0.12, h), (0, h / 2)]
        ET.SubElement(parent, qname("polygon"), dict(common, points=" ".join(f"{fmt(x)},{fmt(y)}" for x, y in points)))
    else:
        attrs = dict(common, x="0", y="0", width=fmt(w), height=fmt(h))
        if shape == "rounded":
            attrs.update(rx=fmt(min(12, h / 4)), ry=fmt(min(12, h / 4)))
        ET.SubElement(parent, qname("rect"), attrs)


def boundary(source: dict[str, Any], target: dict[str, Any]) -> tuple[float, float, float, float]:
    sx, sy = source["x"] + source["w"] / 2, source["y"] + source["h"] / 2
    tx, ty = target["x"] + target["w"] / 2, target["y"] + target["h"] / 2
    dx, dy = tx - sx, ty - sy
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        raise InputError(f"coincident centers for {source['id']} and {target['id']}")
    source_scale = 1.0 / max(abs(dx) / (source["w"] / 2), abs(dy) / (source["h"] / 2))
    target_scale = 1.0 / max(abs(dx) / (target["w"] / 2), abs(dy) / (target["h"] / 2))
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    return (
        sx + dx * source_scale + ux * 2,
        sy + dy * source_scale + uy * 2,
        tx - dx * target_scale - ux * 5,
        ty - dy * target_scale - uy * 5,
    )


def build_svg(spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    collision = resolve_collisions(spec)
    root = ET.Element(qname("svg"), {
        "width": fmt(spec["width"]), "height": fmt(spec["height"]),
        "viewBox": f"0 0 {fmt(spec['width'])} {fmt(spec['height'])}",
        "role": "img", "aria-labelledby": "figure-title figure-description",
    })
    ET.SubElement(root, qname("title"), {"id": "figure-title"}).text = spec["title"]
    ET.SubElement(root, qname("desc"), {"id": "figure-description"}).text = (
        "Editable spec-driven mechanism or flowchart diagram; scientific content is supplied by the input spec."
    )
    defs = ET.SubElement(root, qname("defs"))
    add_markers(defs)
    ET.SubElement(root, qname("rect"), {
        "id": "canvas-background", "x": "0", "y": "0", "width": fmt(spec["width"]),
        "height": fmt(spec["height"]), "fill": str(spec.get("background", "#ffffff")),
    })
    title_layer = ET.SubElement(root, qname("g"), {"id": "layer-title"})
    add_text(title_layer, spec["title"], 24, 30, size=22, anchor="start", css_class="figure-title")
    compartments = ET.SubElement(root, qname("g"), {"id": "layer-compartments"})
    for item in spec["compartments"]:
        group = ET.SubElement(compartments, qname("g"), {
            "id": f"compartment-{item['id']}", "transform": f"translate({fmt(item['x'])} {fmt(item['y'])})",
        })
        source_attr(group, item.get("source_ref"))
        ET.SubElement(group, qname("rect"), {
            "x": "0", "y": "0", "width": fmt(item["w"]), "height": fmt(item["h"]),
            "rx": fmt(float(item.get("rx", 12))), "fill": item["fill"], "stroke": item["stroke"],
            "stroke-width": "1.4", "stroke-dasharray": str(item.get("dasharray", "6 4")),
        })
        add_text(group, item["label"], 12, 18, size=13, anchor="start", css_class="compartment-label")
    entities_layer = ET.SubElement(root, qname("g"), {"id": "layer-entities"})
    entities = {item["id"]: item for item in spec["entities"]}
    for item in sorted(spec["entities"], key=lambda value: value["id"]):
        group = ET.SubElement(entities_layer, qname("g"), {
            "id": f"entity-{item['id']}", "transform": f"translate({fmt(item['x'])} {fmt(item['y'])})",
            "data-shape": item["shape"], "data-compartment": str(item.get("compartment", "")),
        })
        group.set("data-source", str(item.get("source_ref", "")))
        shape_node(group, item)
        add_text(group, item["label"], item["w"] / 2, item["h"] / 2, size=float(item.get("font_size", 14)))
    edges_layer = ET.SubElement(root, qname("g"), {"id": "layer-edges"})
    edge_geometry = []
    for edge in spec["edges"]:
        try:
            x1, y1, x2, y2 = boundary(entities[edge["source"]], entities[edge["target"]])
        except InputError as exc:
            collision["unresolved_collisions"].append({
                "a": edge["source"], "b": edge["target"], "edge_id": edge["id"], "reason": str(exc),
            })
            edge_geometry.append({"id": f"edge-{edge['source']}-{edge['target']}", "status": "skipped", "reason": str(exc)})
            continue
        group = ET.SubElement(edges_layer, qname("g"), {
            "id": f"edge-{edge['source']}-{edge['target']}", "data-kind": edge["kind"],
            "data-edge-id": edge["id"],
        })
        source_attr(group, edge.get("source_ref"))
        color = {"activation": "#2364aa", "inhibition": "#c3423f", "binding": "#3a7d44", "translocation": "#6a4c93"}[edge["kind"]]
        attrs = {"x1": fmt(x1), "y1": fmt(y1), "x2": fmt(x2), "y2": fmt(y2), "fill": "none", "stroke": color, "stroke-width": "2"}
        if edge["kind"] in {"activation", "translocation"}:
            attrs["marker-end"] = f"url(#marker-{edge['kind']})"
        elif edge["kind"] == "inhibition":
            attrs["marker-end"] = "url(#marker-inhibition)"
        if edge["kind"] == "translocation":
            attrs["stroke-dasharray"] = "7 5"
        ET.SubElement(group, qname("line"), attrs)
        if edge["label"]:
            label = add_text(group, edge["label"], (x1 + x2) / 2, (y1 + y2) / 2 - 10, size=12, css_class="edge-label")
            label.set("paint-order", "stroke")
            label.set("stroke", "#ffffff")
            label.set("stroke-width", "4")
            label.set("stroke-linejoin", "round")
        edge_geometry.append({"id": group.get("id"), "x1": x1, "y1": y1, "x2": x2, "y2": y2})
    legend_layer = ET.SubElement(root, qname("g"), {"id": "layer-legend"})
    legend_x = number(spec.get("legend_x", 24), "legend_x")
    default_legend_y = spec["height"] - 28 - max(0, len(spec["legend"]) - 1) * 24
    legend_y = number(spec.get("legend_y", default_legend_y), "legend_y")
    for index, raw in enumerate(spec["legend"]):
        if isinstance(raw, str):
            item = {"label": raw}
        elif isinstance(raw, dict):
            item = raw
        else:
            raise InputError(f"legend[{index}] must be a string or object")
        x = number(item.get("x", legend_x), f"legend[{index}].x")
        y = number(item.get("y", legend_y + index * 24), f"legend[{index}].y")
        group = ET.SubElement(legend_layer, qname("g"), {"id": f"legend-{safe_id(str(item.get('id', f'legend-{index}')), 'legend.id')}"})
        source_attr(group, item.get("source_ref"))
        kind = item.get("kind")
        if kind in KINDS:
            color = {"activation": "#2364aa", "inhibition": "#c3423f", "binding": "#3a7d44", "translocation": "#6a4c93"}[kind]
            attrs = {"x1": fmt(x), "y1": fmt(y), "x2": fmt(x + 32), "y2": fmt(y), "stroke": color, "stroke-width": "2"}
            if kind in {"activation", "translocation", "inhibition"}:
                attrs["marker-end"] = f"url(#marker-{kind})"
            if kind == "translocation":
                attrs["stroke-dasharray"] = "7 5"
            ET.SubElement(group, qname("line"), attrs)
            text_x = x + 42
        else:
            ET.SubElement(group, qname("rect"), {
                "x": fmt(x), "y": fmt(y - 8), "width": "20", "height": "16",
                "fill": str(item.get("fill", "#ffffff")), "stroke": str(item.get("stroke", "#263238")),
            })
            text_x = x + 30
        add_text(group, str(item.get("label", "")), text_x, y, size=12, anchor="start", css_class="legend-label")
    text = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    report = {
        "width": spec["width"], "height": spec["height"],
        "counts": {"compartments": len(spec["compartments"]), "entities": len(spec["entities"]),
                   "edges": len(spec["edges"]), "legend": len(spec["legend"])},
        "semantic_layers": ["layer-title", "layer-compartments", "layer-entities", "layer-edges", "layer-legend"],
        "markers": ["marker-activation", "marker-inhibition", "marker-translocation"],
        "collision_resolution": collision, "edge_geometry": edge_geometry,
        "editable_text": True, "embedded_raster_images": 0,
    }
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + text + "\n", report


def command_build(args: argparse.Namespace) -> int:
    try:
        spec = load_spec(Path(args.spec))
        svg, details = build_svg(spec)
        out = Path(args.out)
        atomic_write_text(out, svg)
        unresolved = details["collision_resolution"]["unresolved_collisions"]
        status = "PARTIAL" if unresolved else "PASS"
        report = {"status": status, "created_at": utc_now_iso(), "spec": str(Path(args.spec).resolve()),
                  "svg": str(out.resolve()), "svg_bytes": out.stat().st_size, **details}
        if unresolved:
            report["fix_hint"] = "Increase canvas dimensions or separate the listed entities; collision avoidance reached its safe limit."
        emit(status, {"svg": str(out), "entities": details["counts"]["entities"],
                      "unresolved_collisions": len(unresolved)}, report, args.json_out)
        return 1 if unresolved else 0
    except InputError as exc:
        report = {"status": "INPUT_ERROR", "error": str(exc), "fix_hint": "Correct the JSON spec and rerun build."}
        emit("INPUT_ERROR", {"error": str(exc)}, report, args.json_out)
        return 2


def svg_dimensions(svg: Path, dpi: int) -> tuple[int, int]:
    root = ET.parse(svg).getroot()
    viewbox = root.get("viewBox", "").replace(",", " ").split()
    if len(viewbox) == 4:
        width, height = float(viewbox[2]), float(viewbox[3])
    else:
        def css_px(value: str | None, default: float) -> float:
            if not value:
                return default
            match = re.match(r"^[0-9.+-]+", value)
            return float(match.group(0)) if match else default
        width, height = css_px(root.get("width"), 800), css_px(root.get("height"), 600)
    if width <= 0 or height <= 0 or not math.isfinite(width + height):
        raise InputError("SVG width/height or viewBox must be positive and finite")
    return max(1, round(width * dpi / 96)), max(1, round(height * dpi / 96))


def run_command(command: list[str], timeout: int = 90) -> tuple[bool, str]:
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, check=False)
        message = (proc.stderr or proc.stdout).strip()[-1200:]
        return proc.returncode == 0, f"exit={proc.returncode} {message}".strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def set_png_dpi(path: Path, dpi: int) -> None:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        raise ValueError("renderer output is not a PNG with an IHDR chunk")
    ppm = max(1, round(dpi / 0.0254))
    payload = struct.pack(">IIB", ppm, ppm, 1)
    chunk = struct.pack(">I", len(payload)) + b"pHYs" + payload
    chunk += struct.pack(">I", zlib.crc32(b"pHYs" + payload) & 0xFFFFFFFF)
    without_old = bytearray(data[:8])
    offset = 8
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("truncated PNG chunk")
        kind = data[offset + 4:offset + 8]
        if kind != b"pHYs":
            without_old.extend(data[offset:end])
        offset = end
        if kind == b"IHDR":
            without_old.extend(chunk)
        if kind == b"IEND":
            break
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(bytes(without_old))
    os.replace(tmp, path)


def produced(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def browser_executable() -> str | None:
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome", "msedge", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def render_with_pymupdf(svg: Path, output: Path, kind: str, dpi: int) -> tuple[bool, str]:
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
    except (ImportError, ModuleNotFoundError) as exc:
        return False, f"pymupdf unavailable: {exc}"
    try:
        with fitz.open(str(svg)) as doc:
            pdf_bytes = doc.convert_to_pdf()
        if kind == "pdf":
            output.write_bytes(pdf_bytes)
        else:
            with fitz.open("pdf", pdf_bytes) as pdf_doc:
                pix = pdf_doc[0].get_pixmap(dpi=dpi, alpha=False)
                pix.save(str(output))
        return True, "pymupdf completed"
    except Exception as exc:
        return False, f"pymupdf failed: {type(exc).__name__}: {exc}"[:1200]


def render_with_browser_cli(executable: str, svg: Path, output: Path, kind: str,
                            dimensions: tuple[int, int], dpi: int) -> tuple[bool, str]:
    width, height = dimensions
    base = [executable, "--headless", "--disable-gpu", "--no-sandbox", "--allow-file-access-from-files"]
    if kind == "png":
        command = base + [f"--screenshot={output.resolve()}", f"--window-size={max(1, round(width * 96 / dpi))},{max(1, round(height * 96 / dpi))}",
                          f"--force-device-scale-factor={dpi / 96}", svg.resolve().as_uri()]
    else:
        command = base + [f"--print-to-pdf={output.resolve()}", "--no-pdf-header-footer", svg.resolve().as_uri()]
    return run_command(command)


def render_with_playwright(svg: Path, output: Path, kind: str, dimensions: tuple[int, int], dpi: int) -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except (ImportError, ModuleNotFoundError) as exc:
        return False, f"playwright unavailable: {exc}"
    try:
        width, height = dimensions
        with sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(
                viewport={"width": max(1, round(width * 96 / dpi)), "height": max(1, round(height * 96 / dpi))},
                device_scale_factor=dpi / 96,
            )
            page = context.new_page()
            page.goto(svg.resolve().as_uri(), wait_until="load")
            if kind == "png":
                page.screenshot(path=str(output), full_page=True)
            else:
                page.pdf(path=str(output), width=f"{width / dpi}in", height=f"{height / dpi}in", print_background=True)
            browser.close()
        return True, "playwright chromium completed"
    except Exception as exc:
        return False, f"playwright failed: {type(exc).__name__}: {exc}"[:1200]


def command_render(args: argparse.Namespace) -> int:
    svg = Path(args.svg)
    outputs = {"png": Path(args.png_out) if args.png_out else None, "pdf": Path(args.pdf_out) if args.pdf_out else None}
    report: dict[str, Any] = {"status": "PARTIAL", "created_at": utc_now_iso(), "svg": str(svg),
                              "dpi": args.dpi, "outputs": {}, "attempts": [], "fallback_guidance": FALLBACK_GUIDANCE}
    try:
        if not svg.is_file():
            raise InputError(f"SVG does not exist: {svg}")
        if not any(outputs.values()):
            raise InputError("render requires --png-out and/or --pdf-out")
        if not 1 <= args.dpi <= 2400:
            raise InputError("--dpi must be between 1 and 2400")
        ET.parse(svg)
        dimensions = svg_dimensions(svg, args.dpi)
        report["png_pixel_dimensions"] = dimensions
        for path in outputs.values():
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
        requested = [kind for kind, path in outputs.items() if path]
        pending = set(requested)
        try:
            import cairosvg
        except (ImportError, ModuleNotFoundError, OSError) as exc:
            report["attempts"].append({"backend": "cairosvg", "status": "unavailable", "detail": str(exc)[:800]})
        else:
            for kind in list(pending):
                output = outputs[kind]
                try:
                    if kind == "png":
                        cairosvg.svg2png(url=str(svg), write_to=str(output), output_width=dimensions[0], output_height=dimensions[1])
                        set_png_dpi(output, args.dpi)
                    else:
                        cairosvg.svg2pdf(url=str(svg), write_to=str(output))
                    if not produced(output):
                        raise RuntimeError("renderer returned without a non-empty output")
                    report["outputs"][kind] = {"path": str(output.resolve()), "backend": "cairosvg", "bytes": output.stat().st_size}
                    pending.remove(kind)
                    report["attempts"].append({"backend": "cairosvg", "format": kind, "status": "success"})
                except Exception as exc:
                    report["attempts"].append({"backend": "cairosvg", "format": kind, "status": "failed", "detail": str(exc)[:800]})
        rsvg = shutil.which("rsvg-convert")
        inkscape = shutil.which("inkscape")
        for backend, executable in (("rsvg-convert", rsvg), ("inkscape", inkscape)):
            if not executable:
                report["attempts"].append({"backend": backend, "status": "unavailable"})
                continue
            for kind in list(pending):
                output = outputs[kind]
                try:
                    if backend == "rsvg-convert":
                        command = [executable, "-f", kind, "-d", str(args.dpi), "-p", str(args.dpi), "-o", str(output), str(svg)]
                    else:
                        command = [executable, str(svg), f"--export-type={kind}", f"--export-filename={output}", f"--export-dpi={args.dpi}"]
                    ok, detail = run_command(command)
                    if ok and kind == "png" and produced(output):
                        set_png_dpi(output, args.dpi)
                    ok = ok and produced(output)
                except Exception as exc:
                    ok, detail = False, f"{type(exc).__name__}: {exc}"[:1200]
                report["attempts"].append({"backend": backend, "format": kind, "status": "success" if ok else "failed", "detail": detail})
                if ok:
                    report["outputs"][kind] = {"path": str(output.resolve()), "backend": backend, "bytes": output.stat().st_size}
                    pending.remove(kind)
        for kind in list(pending):
            output = outputs[kind]
            try:
                ok, detail = render_with_pymupdf(svg, output, kind, args.dpi)
                if ok and kind == "png" and produced(output):
                    set_png_dpi(output, args.dpi)
                ok = ok and produced(output)
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"[:1200]
            report["attempts"].append({"backend": "pymupdf", "format": kind, "status": "success" if ok else "failed", "detail": detail})
            if ok:
                report["outputs"][kind] = {"path": str(output.resolve()), "backend": "pymupdf", "bytes": output.stat().st_size}
                pending.remove(kind)
        browser = browser_executable()
        if browser:
            for kind in list(pending):
                output = outputs[kind]
                try:
                    ok, detail = render_with_browser_cli(browser, svg, output, kind, dimensions, args.dpi)
                    if ok and kind == "png" and produced(output):
                        set_png_dpi(output, args.dpi)
                    ok = ok and produced(output)
                except Exception as exc:
                    ok, detail = False, f"{type(exc).__name__}: {exc}"[:1200]
                report["attempts"].append({"backend": "headless-browser-cli", "executable": browser, "format": kind,
                                           "status": "success" if ok else "failed", "detail": detail})
                if ok:
                    report["outputs"][kind] = {"path": str(output.resolve()), "backend": "headless-browser-cli", "bytes": output.stat().st_size}
                    pending.remove(kind)
        else:
            report["attempts"].append({"backend": "headless-browser-cli", "status": "unavailable"})
        for kind in list(pending):
            output = outputs[kind]
            try:
                ok, detail = render_with_playwright(svg, output, kind, dimensions, args.dpi)
                if ok and kind == "png" and produced(output):
                    set_png_dpi(output, args.dpi)
                ok = ok and produced(output)
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"[:1200]
            report["attempts"].append({"backend": "playwright", "format": kind, "status": "success" if ok else "failed", "detail": detail})
            if ok:
                report["outputs"][kind] = {"path": str(output.resolve()), "backend": "playwright", "bytes": output.stat().st_size}
                pending.remove(kind)
        status = "PASS" if not pending else "PARTIAL"
        report["status"] = status
        report["missing_outputs"] = sorted(pending)
        emit(status, {"outputs": sorted(report["outputs"]), "missing_outputs": sorted(pending),
                      "next": "run fig_qc.py" if not pending else "use fallback_guidance"}, report, args.json_out)
        return 0 if not pending else 1
    except (InputError, ET.ParseError, OSError, ValueError) as exc:
        report["status"] = "INPUT_ERROR"
        report["error"] = str(exc)
        emit("INPUT_ERROR", {"error": str(exc)}, report, args.json_out)
        return 2


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Build editable semantic SVG from a JSON spec")
    build.add_argument("--spec", required=True)
    build.add_argument("--out", required=True)
    build.add_argument("--json-out", required=True)
    build.set_defaults(func=command_build)
    render = sub.add_parser("render", help="Render SVG with explicit, recorded fallback")
    render.add_argument("--svg", required=True)
    render.add_argument("--png-out")
    render.add_argument("--pdf-out")
    render.add_argument("--dpi", type=int, default=300)
    render.add_argument("--json-out", required=True)
    render.set_defaults(func=command_render)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
