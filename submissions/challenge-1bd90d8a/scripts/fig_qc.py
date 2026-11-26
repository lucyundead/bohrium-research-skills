"""Deterministic visual, export, source-value, and result-contract QC for scientific figures."""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import struct
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import atomic_write_json, atomic_write_text, emit, sha256_file, utc_now_iso

try:
    from PIL import Image

    PIL_ERROR = None
except Exception as exc:
    Image = None
    PIL_ERROR = f"{type(exc).__name__}: {exc}"

try:
    import fitz

    FITZ_ERROR = None
except Exception as exc:
    fitz = None
    FITZ_ERROR = f"{type(exc).__name__}: {exc}"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
TRANSLATE_RE = re.compile(r"translate\s*\(([^)]*)\)", re.IGNORECASE)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _check(name: str, passed: bool, severity: str, measured: Any, threshold: Any, fix_hint: str) -> dict[str, Any]:
    return {
        "check": name,
        "pass": bool(passed),
        "severity": severity,
        "measured": measured,
        "threshold": threshold,
        "fix_hint": fix_hint,
    }


def _xml_failure(name: str, error: str, threshold: Any, fix_hint: str) -> dict[str, Any]:
    return _check(name, False, "error", {"xml_valid": False, "error": error}, threshold, fix_hint)


def _parse_svg(path: Path) -> tuple[ET.Element | None, str | None]:
    try:
        return ET.parse(path).getroot(), None
    except (ET.ParseError, OSError, UnicodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def check_svg_xml(root: ET.Element | None, error: str | None, edit_mode: bool = False) -> dict[str, Any]:
    threshold = {"xml_valid": True, "viewBox": "present with four finite values", "semantic_groups": ">=1"}
    fix = "Repair the XML, add a valid SVG viewBox, and organize artwork under identified <g id='layer-...'> groups."
    if root is None:
        return _xml_failure("svg_xml_and_viewbox", error or "SVG parsing failed", threshold, fix)
    groups = [node.get("id") for node in root.iter() if _local(node.tag) == "g" and node.get("id")]
    semantic = [value for value in groups if value and (value.startswith("layer-") or "layer" in value.lower())]
    viewbox = root.get("viewBox")
    values = NUMBER_RE.findall(viewbox or "")
    viewbox_valid = bool(viewbox) and len(values) == 4 and all(math.isfinite(float(value)) for value in values)
    if viewbox_valid:
        viewbox_valid = float(values[2]) > 0 and float(values[3]) > 0
    measured = {
        "xml_valid": True,
        "root_tag": _local(root.tag),
        "viewBox": viewbox,
        "viewbox_valid": viewbox_valid,
        "identified_group_count": len(groups),
        "semantic_group_count": len(semantic),
        "semantic_groups": semantic,
        "edit_mode": edit_mode,
        "inherited_semantic_groups_missing": edit_mode and not semantic,
    }
    passed = _local(root.tag) == "svg" and viewbox_valid and (bool(semantic) or edit_mode)
    severity = "error" if not passed else ("warning" if edit_mode and not semantic else "info")
    return _check("svg_xml_and_viewbox", passed, severity, measured, threshold, fix)


def check_svg_text(root: ET.Element | None, error: str | None, edit_mode: bool = False) -> dict[str, Any]:
    threshold = {"text_nodes": ">=1", "embedded_images": 0, "suspected_outlined_text_clusters": 0}
    fix = "Keep labels as native SVG <text>, remove all embedded <image> elements, and replace suspected outlined glyphs with editable text."
    if root is None:
        return _xml_failure("svg_text_editability", error or "SVG parsing failed", threshold, fix)
    texts = [node for node in root.iter() if _local(node.tag) == "text"]
    images = [node for node in root.iter() if _local(node.tag) == "image"]
    paths = [node for node in root.iter() if _local(node.tag) == "path"]
    clusters = []
    for group in (node for node in root.iter() if _local(node.tag) == "g"):
        descendants = list(group.iter())
        path_count = sum(_local(node.tag) == "path" for node in descendants)
        text_count = sum(_local(node.tag) == "text" for node in descendants)
        graphic_count = sum(_local(node.tag) in {"path", "rect", "circle", "ellipse", "polygon", "polyline", "line", "use"} for node in descendants)
        if path_count >= 8 and text_count == 0 and graphic_count and path_count / graphic_count >= 0.8:
            clusters.append({"group_id": group.get("id"), "path_count": path_count, "graphic_count": graphic_count})
    if not texts and len(paths) >= 8 and not clusters:
        clusters.append({"group_id": root.get("id"), "path_count": len(paths), "scope": "document_without_text"})
    measured = {
        "text_nodes": len(texts),
        "embedded_images": len(images),
        "path_nodes": len(paths),
        "suspected_outlined_text_clusters": clusters,
        "edit_mode": edit_mode,
        "inherited_outlined_text_allowed": edit_mode and not images and (not texts or bool(clusters)),
    }
    passed = not images and (bool(texts) or edit_mode)
    severity = "error" if not passed else ("warning" if clusters or (edit_mode and not texts) else "info")
    return _check("svg_text_editability", passed, severity, measured, threshold, fix)


def _style(node: ET.Element) -> dict[str, str]:
    result = {}
    for declaration in node.get("style", "").split(";"):
        if ":" in declaration:
            key, value = declaration.split(":", 1)
            result[key.strip().lower()] = value.strip()
    return result


def _inherited_value(node: ET.Element, name: str, inherited: str | None) -> str | None:
    return _style(node).get(name, node.get(name, inherited))


def _font_size(value: str | None) -> float:
    if not value:
        return 16.0
    match = NUMBER_RE.search(value)
    if not match:
        return 16.0
    size = float(match.group(0))
    lowered = value.lower()
    if "pt" in lowered:
        size *= 96.0 / 72.0
    elif "pc" in lowered:
        size *= 16.0
    elif "mm" in lowered:
        size *= 96.0 / 25.4
    elif "cm" in lowered:
        size *= 96.0 / 2.54
    elif "in" in lowered:
        size *= 96.0
    return size if math.isfinite(size) and size > 0 else 16.0


def _matrix_multiply(first: tuple[float, ...], second: tuple[float, ...]) -> tuple[float, ...]:
    a, b, c, d, e, f = first
    g, h, i, j, k, l = second
    return (
        a * g + c * h, b * g + d * h,
        a * i + c * j, b * i + d * j,
        a * k + c * l + e, b * k + d * l + f,
    )


def _transform_matrix(transform: str | None, warnings: list[str]) -> tuple[float, ...]:
    matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    text = transform or ""
    matches = list(re.finditer(r"([A-Za-z]+)\s*\(([^)]*)\)", text))
    remainder = re.sub(r"[A-Za-z]+\s*\([^)]*\)", "", text)
    if remainder.strip(" ,\t\r\n"):
        warnings.append(f"Unsupported transform syntax: {text!r}")
    for match in matches:
        name = match.group(1).lower()
        values = [float(value) for value in NUMBER_RE.findall(match.group(2))]
        operation = None
        if name == "translate" and len(values) in {1, 2}:
            operation = (1.0, 0.0, 0.0, 1.0, values[0], values[1] if len(values) == 2 else 0.0)
        elif name == "scale" and len(values) in {1, 2}:
            operation = (values[0], 0.0, 0.0, values[1] if len(values) == 2 else values[0], 0.0, 0.0)
        elif name == "rotate" and len(values) in {1, 3}:
            angle = math.radians(values[0])
            cosine, sine = math.cos(angle), math.sin(angle)
            rotation = (cosine, sine, -sine, cosine, 0.0, 0.0)
            if len(values) == 3:
                cx, cy = values[1:]
                operation = _matrix_multiply(
                    _matrix_multiply((1.0, 0.0, 0.0, 1.0, cx, cy), rotation),
                    (1.0, 0.0, 0.0, 1.0, -cx, -cy),
                )
            else:
                operation = rotation
        elif name == "matrix" and len(values) == 6:
            operation = tuple(values)
        if operation is None:
            warnings.append(f"Unsupported or malformed transform: {match.group(0)!r}")
        else:
            matrix = _matrix_multiply(matrix, operation)
    return matrix


def _translation(transform: str | None) -> tuple[float, float]:
    matrix = _transform_matrix(transform, [])
    return matrix[4], matrix[5]


def _coordinate(node: ET.Element, name: str) -> float:
    values = NUMBER_RE.findall(node.get(name, "0"))
    return float(values[0]) if values else 0.0


def _text_width(text: str, size: float) -> float:
    units = 0.0
    for char in text:
        if char.isspace():
            units += 0.33
        elif unicodedata.east_asian_width(char) in {"W", "F"}:
            units += 1.0
        else:
            units += 0.6
    return max(size * 0.2, units * size)


def _text_boxes(root: ET.Element, transform_warnings: list[str] | None = None) -> list[dict[str, Any]]:
    boxes: list[dict[str, Any]] = []
    warnings = transform_warnings if transform_warnings is not None else []

    def visit(node: ET.Element, parent_matrix: tuple[float, ...], font_size: str | None,
              anchor: str | None, baseline: str | None, hidden: bool) -> None:
        style = _style(node)
        hidden = hidden or style.get("display", node.get("display")) == "none" or style.get("visibility", node.get("visibility")) in {"hidden", "collapse"}
        local = _transform_matrix(node.get("transform"), warnings)
        matrix = _matrix_multiply(parent_matrix, local)
        font_size = _inherited_value(node, "font-size", font_size)
        anchor = _inherited_value(node, "text-anchor", anchor)
        baseline = _inherited_value(node, "dominant-baseline", baseline)
        if _local(node.tag) == "text" and not hidden:
            size = _font_size(font_size)

            def add_box(text: str, x: float, y: float, box_node: ET.Element,
                        box_matrix: tuple[float, ...], box_size: float) -> None:
                text = " ".join(text.split())
                if not text:
                    return
                width = _text_width(text, box_size)
                height = box_size * 1.2
                if anchor == "middle":
                    left = x - width / 2
                elif anchor == "end":
                    left = x - width
                else:
                    left = x
                top = y - height / 2 if baseline in {"middle", "central"} else y - box_size * 0.85
                corners = []
                for px, py in ((left, top), (left + width, top), (left, top + height), (left + width, top + height)):
                    a, b, c, d, e, f = box_matrix
                    corners.append((a * px + c * py + e, b * px + d * py + f))
                xs, ys = [point[0] for point in corners], [point[1] for point in corners]
                a, b, c, d, e, f = box_matrix
                boxes.append({
                    "id": box_node.get("id") or node.get("id"),
                    "text": text,
                    "x": min(xs), "y": min(ys),
                    "width": max(xs) - min(xs), "height": max(ys) - min(ys),
                    "font_size": box_size,
                    "anchor_point": [a * x + c * y + e, b * x + d * y + f],
                })

            base_x = _coordinate(node, "x") + _coordinate(node, "dx")
            base_y = _coordinate(node, "y") + _coordinate(node, "dy")
            tspans = [child for child in node.iter() if child is not node and _local(child.tag) == "tspan"]
            if not tspans:
                add_box("".join(node.itertext()), base_x, base_y, node, matrix, size)
            else:
                def emit_fragments(parent: ET.Element, x: float, y: float,
                                   parent_text_matrix: tuple[float, ...], parent_size: float) -> tuple[float, float]:
                    own_text = parent.text or ""
                    add_box(own_text, x, y, parent, parent_text_matrix, parent_size)
                    x += _text_width(" ".join(own_text.split()), parent_size) if own_text.strip() else 0.0
                    for child in list(parent):
                        if _local(child.tag) == "tspan":
                            child_x = _coordinate(child, "x") if child.get("x") is not None else x
                            child_y = _coordinate(child, "y") if child.get("y") is not None else y
                            child_x += _coordinate(child, "dx")
                            child_y += _coordinate(child, "dy")
                            child_matrix = _matrix_multiply(parent_text_matrix, _transform_matrix(child.get("transform"), warnings))
                            child_size = _font_size(_inherited_value(child, "font-size", str(parent_size)))
                            x, y = emit_fragments(child, child_x, child_y, child_matrix, child_size)
                        if child.tail:
                            add_box(child.tail, x, y, parent, parent_text_matrix, parent_size)
                            x += _text_width(" ".join(child.tail.split()), parent_size) if child.tail.strip() else 0.0
                    return x, y

                emit_fragments(node, base_x, base_y, matrix, size)
        for child in list(node):
            if _local(node.tag) == "text" and _local(child.tag) == "tspan":
                continue
            visit(child, matrix, font_size, anchor, baseline, hidden)

    visit(root, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0), None, "start", "auto", False)
    return boxes


def check_text_overlap(root: ET.Element | None, error: str | None) -> dict[str, Any]:
    threshold = {"overlap_pairs": 0, "method": "estimated text bboxes with affine transforms and tspan offsets"}
    fix = "Move or resize the reported text labels, shorten their wording, or increase panel spacing; rerun overlap QC."
    if root is None:
        return _xml_failure("svg_text_overlap", error or "SVG parsing failed", threshold, fix)
    transform_warnings: list[str] = []
    boxes = _text_boxes(root, transform_warnings)
    pairs = []
    for index, first in enumerate(boxes):
        for second in boxes[index + 1:]:
            overlap_x = min(first["x"] + first["width"], second["x"] + second["width"]) - max(first["x"], second["x"])
            overlap_y = min(first["y"] + first["height"], second["y"] + second["height"]) - max(first["y"], second["y"])
            if overlap_x > 0.01 and overlap_y > 0.01:
                pairs.append({
                    "text_a": first["text"],
                    "text_b": second["text"],
                    "id_a": first["id"],
                    "id_b": second["id"],
                    "bbox_a": {key: round(first[key], 3) for key in ("x", "y", "width", "height")},
                    "bbox_b": {key: round(second[key], 3) for key in ("x", "y", "width", "height")},
                    "overlap_width": round(overlap_x, 3),
                    "overlap_height": round(overlap_y, 3),
                    "overlap_area": round(overlap_x * overlap_y, 3),
                    "fix_hint": f"Separate {first['text']!r} from {second['text']!r} or reduce their font size.",
                })
    measured = {"text_boxes": len(boxes), "boxes": boxes, "overlap_pair_count": len(pairs), "overlap_pairs": pairs, "transform_warnings": transform_warnings}
    if pairs:
        fix = pairs[0]["fix_hint"] + " See measured.overlap_pairs for every collision."
    if transform_warnings:
        fix = "Some transforms could not be parsed exactly; inspect the labels manually. " + fix
    passed = not pairs or bool(transform_warnings)
    severity = "warning" if transform_warnings else ("info" if not pairs else "error")
    return _check("svg_text_overlap", passed, severity, measured, threshold, fix)


def _png_chunks(path: Path) -> tuple[list[tuple[bytes, bytes]], dict[str, Any]]:
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("Missing PNG signature")
    chunks = []
    offset = len(PNG_SIGNATURE)
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("Truncated PNG chunk")
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length:end])[0]
        actual_crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise ValueError(f"PNG CRC mismatch in {kind.decode('ascii', errors='replace')}")
        chunks.append((kind, payload))
        offset = end
        if kind == b"IEND":
            break
    ihdr = next((payload for kind, payload in chunks if kind == b"IHDR"), None)
    if ihdr is None or len(ihdr) != 13:
        raise ValueError("PNG has no valid IHDR chunk")
    width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", ihdr)
    info = {
        "width": width,
        "height": height,
        "bit_depth": bit_depth,
        "color_type": color_type,
        "compression": compression,
        "filter": filter_method,
        "interlace": interlace,
    }
    return chunks, info


def check_png_dpi(path: Path, min_dpi: float) -> dict[str, Any]:
    threshold = {"dpi": f">={min_dpi:g} on every available metadata source", "pixel_dimensions": "positive and consistent", "metadata_disagreement_dpi": "<=1"}
    fix = f"Export a non-empty PNG at >= {min_dpi:g} DPI and embed matching pHYs/DPI metadata; do not merely upscale a low-resolution raster."
    measured: dict[str, Any] = {"pil_available": Image is not None, "pil_error": PIL_ERROR}
    try:
        chunks, info = _png_chunks(path)
        measured.update(info)
        physical = next((payload for kind, payload in chunks if kind == b"pHYs"), None)
        phys_dpi = None
        if physical is not None and len(physical) == 9:
            x_ppm, y_ppm, unit = struct.unpack(">IIB", physical)
            measured["pHYs"] = {"x_pixels_per_unit": x_ppm, "y_pixels_per_unit": y_ppm, "unit": unit}
            if unit == 1 and x_ppm and y_ppm:
                phys_dpi = [x_ppm * 0.0254, y_ppm * 0.0254]
        measured["pHYs_dpi"] = phys_dpi
        pil_dpi = None
        pil_size = None
        pil_open_error = None
        if Image is not None:
            try:
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    raw_dpi = image.info.get("dpi")
                    if raw_dpi is not None and len(raw_dpi) >= 2:
                        pil_dpi = [float(raw_dpi[0]), float(raw_dpi[1])]
                    pil_size = list(image.size)
            except Exception as exc:
                pil_open_error = f"{type(exc).__name__}: {exc}"
        measured["pil_dpi"] = pil_dpi
        measured["pil_pixel_dimensions"] = pil_size
        measured["pil_open_error"] = pil_open_error
        dpi_values = [value for source in (phys_dpi, pil_dpi) if source for value in source if math.isfinite(value)]
        disagreement = 0.0
        if phys_dpi and pil_dpi:
            disagreement = max(abs(a - b) for a, b in zip(phys_dpi, pil_dpi))
        dimensions_match = pil_size is None or pil_size == [info["width"], info["height"]]
        passed = (
            info["width"] > 0 and info["height"] > 0 and dimensions_match and pil_open_error is None
            and bool(dpi_values) and min(dpi_values) >= min_dpi - 0.01 and disagreement <= 1.0
        )
        measured["metadata_disagreement_dpi"] = disagreement
        measured["minimum_detected_dpi"] = min(dpi_values) if dpi_values else None
        severity = "error" if not passed else ("warning" if Image is None else "info")
        return _check("png_dpi_and_resolution", passed, severity, measured, threshold, fix)
    except (OSError, ValueError, struct.error, zlib.error) as exc:
        measured["error"] = f"{type(exc).__name__}: {exc}"
        return _check("png_dpi_and_resolution", False, "error", measured, threshold, fix)


def _paeth(a: int, b: int, c: int) -> int:
    estimate = a + b - c
    distances = (abs(estimate - a), abs(estimate - b), abs(estimate - c))
    return (a, b, c)[distances.index(min(distances))]


def _stdlib_png_pixels(path: Path) -> tuple[int, int, list[tuple[int, int, int]]]:
    chunks, info = _png_chunks(path)
    width, height = info["width"], info["height"]
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(info["color_type"])
    if info["bit_depth"] != 8 or channels is None or info["interlace"] != 0:
        raise ValueError("Stdlib PNG fallback supports only non-interlaced 8-bit grayscale/RGB/palette/alpha PNGs")
    stride = width * channels
    compressed = b"".join(payload for kind, payload in chunks if kind == b"IDAT")
    raw = zlib.decompress(compressed)
    if len(raw) != height * (stride + 1):
        raise ValueError("Unexpected PNG scanline length")
    rows = []
    previous = bytearray(stride)
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        source = raw[offset + 1:offset + 1 + stride]
        offset += stride + 1
        row = bytearray(stride)
        for index, value in enumerate(source):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                predictor = _paeth(left, above, upper_left)
            else:
                raise ValueError(f"Unsupported PNG filter {filter_type}")
            row[index] = (value + predictor) & 255
        rows.append(row)
        previous = row
    palette_chunk = next((payload for kind, payload in chunks if kind == b"PLTE"), b"")
    palette = [tuple(palette_chunk[index:index + 3]) for index in range(0, len(palette_chunk), 3)]
    pixels = []
    for row in rows:
        for x in range(width):
            values = row[x * channels:(x + 1) * channels]
            if info["color_type"] == 0:
                rgb = (values[0],) * 3
            elif info["color_type"] == 2:
                rgb = tuple(values)
            elif info["color_type"] == 3:
                if values[0] >= len(palette) or len(palette[values[0]]) != 3:
                    raise ValueError("Invalid PNG palette index")
                rgb = palette[values[0]]
            elif info["color_type"] == 4:
                alpha = values[1] / 255
                rgb = tuple(round(values[0] * alpha + 255 * (1 - alpha)),) * 3
            else:
                alpha = values[3] / 255
                rgb = tuple(round(values[channel] * alpha + 255 * (1 - alpha)) for channel in range(3))
            pixels.append(rgb)
    return width, height, pixels


def _load_pixels(path: Path) -> tuple[tuple[int, int], int, int, list[tuple[int, int, int]]]:
    if Image is None:
        width, height, pixels = _stdlib_png_pixels(path)
        return (width, height), width, height, pixels
    with Image.open(path) as image:
        original_size = image.size
        image.thumbnail((512, 512))
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        composite = Image.alpha_composite(background, rgba).convert("RGB")
        return original_size, composite.width, composite.height, list(composite.getdata())


def check_png_content(path: Path) -> dict[str, Any]:
    threshold = {"grayscale_population_stddev": ">3.0", "unique_colors": ">=2", "each_2px_edge_nonbackground_ratio": "<=0.02"}
    fix = "Render actual figure content, verify it is not blank, and add enough outer margin that no non-background ink touches the four 2-pixel borders."
    measured: dict[str, Any] = {"pil_available": Image is not None, "pil_error": PIL_ERROR}
    try:
        original_size, width, height, pixels = _load_pixels(path)
    except Exception as exc:
        if Image is None:
            measured.update({"status": "skipped", "reason": f"PIL unavailable and stdlib decoder could not analyze this PNG: {type(exc).__name__}: {exc}"})
            return _check("png_nonblank_and_clipping", True, "warning", measured, threshold, fix)
        measured["error"] = f"{type(exc).__name__}: {exc}"
        return _check("png_nonblank_and_clipping", False, "error", measured, threshold, fix)
    if not pixels or width < 2 or height < 2:
        measured.update({"width": width, "height": height})
        return _check("png_nonblank_and_clipping", False, "error", measured, threshold, fix)
    sample_stride = max(1, len(pixels) // 250000)
    sampled = pixels[::sample_stride]
    gray = [0.2126 * red + 0.7152 * green + 0.0722 * blue for red, green, blue in sampled]
    stddev = statistics.pstdev(gray) if len(gray) > 1 else 0.0
    unique_colors = set()
    for pixel in sampled:
        unique_colors.add(pixel)
        if len(unique_colors) >= 10000:
            break
    corners = [pixels[0], pixels[width - 1], pixels[(height - 1) * width], pixels[-1]]
    background = max(corners, key=corners.count)

    def is_ink(pixel: tuple[int, int, int]) -> bool:
        return max(abs(pixel[channel] - background[channel]) for channel in range(3)) > 24

    regions = {
        "top": (pixels[y * width + x] for y in range(2) for x in range(width)),
        "bottom": (pixels[y * width + x] for y in range(height - 2, height) for x in range(width)),
        "left": (pixels[y * width + x] for y in range(height) for x in range(2)),
        "right": (pixels[y * width + x] for y in range(height) for x in range(width - 2, width)),
    }
    edge_ratios = {}
    for name, values in regions.items():
        selected = list(values)
        edge_ratios[name] = sum(is_ink(pixel) for pixel in selected) / len(selected)
    measured.update({
        "width": original_size[0],
        "height": original_size[1],
        "analysis_width": width,
        "analysis_height": height,
        "sample_stride": sample_stride,
        "sampled_pixels": len(sampled),
        "grayscale_population_stddev": stddev,
        "unique_color_count": len(unique_colors),
        "unique_color_count_capped_at": 10000,
        "background_rgb_estimated_from_corners": background,
        "edge_nonbackground_ratios": edge_ratios,
        "maximum_edge_nonbackground_ratio": max(edge_ratios.values()),
    })
    passed = stddev > 3.0 and len(unique_colors) >= 2 and max(edge_ratios.values()) <= 0.02
    return _check("png_nonblank_and_clipping", passed, "info" if passed else "error", measured, threshold, fix)


def check_pdf(path: Path) -> dict[str, Any]:
    threshold = {"magic": "%PDF", "bytes": ">0", "pages_if_fitz_available": ">=1", "extractable_text_characters_if_fitz_available": ">=1"}
    fix = "Export a real non-empty vector PDF with at least one page and selectable text; inspect the renderer logs if export failed."
    size = path.stat().st_size
    with path.open("rb") as handle:
        magic = handle.read(4)
    measured: dict[str, Any] = {"bytes": size, "magic_valid": magic == b"%PDF", "fitz_available": fitz is not None, "fitz_error": FITZ_ERROR}
    passed = size > 0 and magic == b"%PDF"
    if fitz is not None and passed:
        try:
            document = fitz.open(path)
            page_count = document.page_count
            text_characters = sum(len(page.get_text()) for page in document)
            document.close()
            measured.update({"page_count": page_count, "extractable_text_characters": text_characters})
            passed = page_count >= 1 and text_characters >= 1
        except Exception as exc:
            measured["fitz_open_error"] = f"{type(exc).__name__}: {exc}"
            passed = False
    elif fitz is None:
        measured["page_count"] = None
        measured["extractable_text_characters"] = None
    severity = "error" if not passed else ("warning" if fitz is None else "info")
    return _check("pdf_validity", passed, severity, measured, threshold, fix)


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _nonfinite_paths(value: Any, path: str = "$") -> list[str]:
    found = []
    if isinstance(value, float) and not math.isfinite(value):
        found.append(path)
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_nonfinite_paths(item, f"{path}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_nonfinite_paths(item, f"{path}[{index}]"))
    return found


def _ledger_verification(ledger: Any) -> dict[str, Any]:
    if not isinstance(ledger, dict):
        return {"available": False, "all_match": False, "error": "Ledger root is not an object"}
    verification = ledger.get("verification", ledger)
    if not isinstance(verification, dict) or not isinstance(verification.get("comparisons"), list):
        return {"available": False, "all_match": False, "error": "Ledger has no verification.comparisons array"}
    comparisons = verification["comparisons"]
    mismatches = [item.get("plotted_path") if isinstance(item, dict) else "<non-object comparison>" for item in comparisons if not isinstance(item, dict) or item.get("status") != "match" or not item.get("match")]
    return {
        "available": True,
        "comparison_count": len(comparisons),
        "all_match": bool(comparisons) and not mismatches and verification.get("all_match") is True,
        "mismatch_or_unbound_paths": mismatches,
        "reported_summary": verification.get("summary"),
    }


def _source_contract_check(args: argparse.Namespace) -> dict[str, Any]:
    threshold = {
        "plotted_values": "all independently recomputed comparisons match (rtol=1e-6)",
        "case_id": "exact match",
        "output_contract": "every required field present; no NaN/Infinity anywhere",
    }
    fix = "Bind plotted arrays to raw source columns, correct every mismatch, match case_id, supply all output_contract fields, and replace non-finite values with documented valid results."
    measured: dict[str, Any] = {"numeric_status": "not_requested", "contract_status": "not_requested", "errors": []}
    numeric_requested = any((args.ledger, args.plotted, args.source))
    numeric_passed = True
    if numeric_requested:
        measured["numeric_status"] = "checked"
        ledger = _read_json(args.ledger) if args.ledger else None
        ledger_result = _ledger_verification(ledger) if ledger is not None else {"available": False}
        measured["ledger"] = ledger_result
        independent = None
        if args.plotted and args.source:
            from source_ledger import _load_sources, verify_plotted

            sources = _load_sources([args.source])
            plotted = _read_json(args.plotted)
            verification = verify_plotted(sources, plotted, rtol=1e-6)
            independent = {
                "all_match": verification["all_match"],
                "summary": verification["summary"],
                "comparisons": verification["comparisons"],
                "source_sha256": sources[0]["sha256"],
            }
            numeric_passed = numeric_passed and verification["all_match"]
        measured["independent_verification"] = independent
        if args.ledger:
            numeric_passed = numeric_passed and bool(ledger_result.get("all_match"))
        elif independent is None:
            numeric_passed = False
            measured["errors"].append("Numeric verification needs a ledger or both --plotted and --source")
        if args.source and args.ledger and isinstance(ledger, dict):
            actual_hash = sha256_file(args.source)
            ledger_sources = ledger.get("sources", [])
            matching = [item for item in ledger_sources if isinstance(item, dict) and Path(str(item.get("path", ""))).name == args.source.name]
            hash_match = bool(matching) and all(item.get("sha256") == actual_hash for item in matching)
            measured["ledger_source_hash_match"] = hash_match
            numeric_passed = numeric_passed and hash_match
    contract_requested = bool(args.case or args.result)
    contract_passed = True
    if contract_requested:
        measured["contract_status"] = "checked"
        if not args.case or not args.result:
            contract_passed = False
            measured["errors"].append("Contract validation requires both --case and --result")
        else:
            case = _read_json(args.case)
            result = _read_json(args.result)
            contract = case.get("output_contract", {}) if isinstance(case, dict) else {}
            required = list(contract) if isinstance(contract, dict) else []
            if "case_id" not in required:
                required.append("case_id")
            missing = [key for key in required if not isinstance(result, dict) or key not in result]
            complete = isinstance(result, dict) and result.get("status") == "complete"
            null_required = [
                key for key in required
                if complete and isinstance(result, dict) and key in result and result[key] is None
            ]
            if null_required:
                measured["errors"].append(f"Complete results cannot have null required fields: {null_required}")
            case_id = case.get("case_id") if isinstance(case, dict) else None
            result_case_id = result.get("case_id") if isinstance(result, dict) else None
            nonfinite = _nonfinite_paths(result)
            measured["contract"] = {
                "expected_case_id": case_id,
                "result_case_id": result_case_id,
                "case_id_match": case_id is not None and case_id == result_case_id,
                "required_fields": required,
                "missing_fields": missing,
                "null_required_fields_when_complete": null_required,
                "nonfinite_paths": nonfinite,
            }
            contract_passed = isinstance(contract, dict) and not missing and not null_required and case_id is not None and case_id == result_case_id and not nonfinite
    passed = numeric_passed and contract_passed
    return _check("source_value_and_contract", passed, "info" if passed else "error", measured, threshold, fix)


def _verification_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Figure QC verification",
        "",
        f"- Overall status: **{report['status']}**",
        f"- Created (UTC): {report['created_utc']}",
        f"- Checks: {report['summary']['passed']} passed, {report['summary']['failed']} failed, {report['summary']['warnings']} warnings",
        "- Exit semantics: 0 = no failed checks; 1 = at least one failed check; 2 = fatal argument/file error",
        "",
        "| Check | Result | Severity | Fix hint |",
        "|---|---|---|---|",
    ]
    for item in report["checks"]:
        result = "PASS" if item["pass"] else "FAIL"
        hint = str(item["fix_hint"]).replace("|", "\\|")
        lines.append(f"| `{item['check']}` | {result} | {item['severity']} | {hint} |")
    lines.extend(["", "## Measured details", ""])
    for item in report["checks"]:
        details = json.dumps(item["measured"], ensure_ascii=False, allow_nan=False)
        if len(details) > 1600:
            details = details[:1590] + "… (see qc_report.json)"
        lines.extend([f"### {item['check']}", "", f"```json\n{details}\n```", ""])
    lines.extend(["## Artifact integrity", "", "`manifest.json` lists SHA-256 and byte size for every output artifact except the manifest itself.", ""])
    return "\n".join(lines)


def _write_output_evidence(output_dir: Path, report: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    verification = output_dir / "VERIFICATION.md"
    manifest = output_dir / "manifest.json"
    atomic_write_text(verification, _verification_markdown(report))
    files = []
    for path in sorted(output_dir.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.resolve() == manifest.resolve():
            continue
        files.append({"path": path.relative_to(output_dir).as_posix(), "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    atomic_write_json(manifest, {"schema_version": "1.0", "created_utc": report["created_utc"], "file_count": len(files), "files": files})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--svg", type=Path, required=True)
    parser.add_argument("--png", type=Path)
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--plotted", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--case", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--edit-mode", action="store_true", help="Allow inherited missing layers and outlined text during existing-SVG edits")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--min-dpi", type=float, default=300.0)
    parser.add_argument("--json-out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        supplied = {name: value for name, value in vars(args).items() if name not in {"output_dir", "min_dpi", "json_out", "edit_mode"} and value is not None}
        for name, path in supplied.items():
            if not path.is_file():
                raise FileNotFoundError(f"--{name.replace('_', '-')} file does not exist: {path}")
        if not math.isfinite(args.min_dpi) or args.min_dpi <= 0:
            raise ValueError("--min-dpi must be positive and finite")
        if args.output_dir and args.output_dir.exists() and not args.output_dir.is_dir():
            raise NotADirectoryError(f"--output-dir is not a directory: {args.output_dir}")
        root, xml_error = _parse_svg(args.svg)
        checks = [
            check_svg_xml(root, xml_error, args.edit_mode),
            check_svg_text(root, xml_error, args.edit_mode),
            check_text_overlap(root, xml_error),
        ]
        if args.png:
            checks.extend([check_png_dpi(args.png, args.min_dpi), check_png_content(args.png)])
        else:
            skipped = {"status": "skipped", "reason": "--png not provided"}
            checks.append(_check("png_dpi_and_resolution", True, "info", skipped, "only when --png is provided", "Provide --png to audit raster export."))
            checks.append(_check("png_nonblank_and_clipping", True, "info", skipped, "only when --png is provided", "Provide --png to audit raster content."))
        if args.pdf:
            checks.append(check_pdf(args.pdf))
        else:
            checks.append(_check("pdf_validity", True, "info", {"status": "skipped", "reason": "--pdf not provided"}, "only when --pdf is provided", "Provide --pdf to audit PDF export."))
        checks.append(_source_contract_check(args))
        failed = sum(not item["pass"] for item in checks)
        warnings = sum(item["pass"] and item["severity"] == "warning" for item in checks)
        report = {
            "schema_version": "1.0",
            "created_utc": utc_now_iso(),
            "status": "PASS" if failed == 0 else "FAIL",
            "summary": {"total": len(checks), "passed": len(checks) - failed, "failed": failed, "warnings": warnings},
            "inputs": {name: str(path.resolve()) for name, path in supplied.items()},
            "min_dpi": args.min_dpi,
            "edit_mode": args.edit_mode,
            "optional_dependencies": {"PIL": Image is not None, "fitz": fitz is not None, "PIL_error": PIL_ERROR, "fitz_error": FITZ_ERROR},
            "checks": checks,
        }
        if args.output_dir:
            report["output_evidence"] = {
                "manifest": str((args.output_dir / "manifest.json").resolve()),
                "verification": str((args.output_dir / "VERIFICATION.md").resolve()),
            }
            atomic_write_json(args.json_out, report)
            _write_output_evidence(args.output_dir, report)
        emit("ok" if failed == 0 else "failed", report["summary"], report, args.json_out)
        return 0 if failed == 0 else 1
    except Exception as exc:
        report = {"schema_version": "1.0", "created_utc": utc_now_iso(), "status": "INPUT_ERROR", "error": f"{type(exc).__name__}: {exc}"}
        emit("INPUT_ERROR", {"error": str(exc)}, report, args.json_out)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
