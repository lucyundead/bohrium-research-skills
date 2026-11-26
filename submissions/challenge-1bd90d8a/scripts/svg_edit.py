"""Namespace-safe, source-preserving local edits for semantic SVG documents."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import atomic_write_json, atomic_write_text, emit, sha256_file, utc_now_iso

NAMESPACES = {
    "": "http://www.w3.org/2000/svg",
    "svg": "http://www.w3.org/2000/svg",
    "xlink": "http://www.w3.org/1999/xlink",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "cc": "http://creativecommons.org/ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "inkscape": "http://www.inkscape.org/namespaces/inkscape",
    "sodipodi": "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd",
}
COLOR_ATTRIBUTES = {"fill", "stroke", "color", "stop-color", "flood-color", "lighting-color"}


def _register_namespaces(path: str | Path | None = None) -> None:
    discovered: list[tuple[str, str]] = []
    if path is not None:
        try:
            with Path(path).open("rb") as handle:
                for _, item in ET.iterparse(handle, events=("start-ns",)):
                    discovered.append(item)
        except (OSError, ET.ParseError):
            pass
    for prefix, uri in discovered + list(NAMESPACES.items()):
        if re.fullmatch(r"ns\d+", prefix):
            continue
        try:
            ET.register_namespace(prefix, uri)
            NAMESPACES[prefix] = uri
        except ValueError:
            pass


def _parse(path: str | Path) -> ET.ElementTree:
    _register_namespaces(path)
    return ET.parse(path)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _qname(name: str) -> str:
    if name.startswith("{") or ":" not in name:
        return name
    prefix, local = name.split(":", 1)
    if prefix not in NAMESPACES:
        raise ValueError(f"Unknown namespace prefix: {prefix}")
    return f"{{{NAMESPACES[prefix]}}}{local}"


def _clean_text(value: str | None) -> str:
    return "" if value is None or not value.strip() else value


def _canonical_bytes(element: ET.Element, skip_ids: set[str] | None = None) -> bytes:
    element_id = element.get("id")
    if skip_ids and element_id in skip_ids:
        return b""
    attributes = sorted((_qname(key), value) for key, value in element.attrib.items())
    parts = [
        b"<", element.tag.encode("utf-8"), b" ",
        json.dumps(attributes, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        b">", _clean_text(element.text).encode("utf-8"),
    ]
    for child in list(element):
        serialized = _canonical_bytes(child, skip_ids)
        if serialized:
            parts.extend([serialized, _clean_text(child.tail).encode("utf-8")])
    parts.extend([b"</", element.tag.encode("utf-8"), b">"])
    return b"".join(parts)


def _subtree_hash(element: ET.Element) -> str:
    return hashlib.sha256(_canonical_bytes(element)).hexdigest()


def _ids(root: ET.Element, warnings: list[dict[str, str]] | None = None, scope: str = "document") -> dict[str, ET.Element]:
    result: dict[str, ET.Element] = {}
    for element in root.iter():
        element_id = element.get("id")
        if element_id:
            if element_id in result:
                if warnings is not None:
                    warnings.append({
                        "scope": scope,
                        "id": element_id,
                        "warning": "Duplicate SVG id; the first occurrence is used for preservation hashing",
                    })
                continue
            result[element_id] = element
    return result


def _parents(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in parent}


def _descendants(element: ET.Element) -> set[str]:
    return {child.get("id") for child in element.iter() if child.get("id")}


def _allowed_closure(root: ET.Element, allow_ids: set[str]) -> set[str]:
    closure = set(allow_ids)
    for element in root.iter():
        if element.get("id") in allow_ids:
            closure.update(_descendants(element))
    return closure


def _has_allowed_ancestor(element: ET.Element, root: ET.Element, allow_ids: set[str]) -> bool:
    parents = _parents(root)
    current: ET.Element | None = element
    while current is not None:
        if current.get("id") in allow_ids:
            return True
        current = parents.get(current)
    return False


def inspect_svg(svg_path: str | Path) -> dict[str, Any]:
    path = Path(svg_path).resolve()
    tree = _parse(path)
    root = tree.getroot()
    parents = _parents(root)
    layers = []
    identified = []
    texts = []
    for index, element in enumerate(root.iter()):
        tag = _local(element.tag)
        element_id = element.get("id")
        parent = parents.get(element)
        if tag == "g" and element_id:
            layers.append({"id": element_id, "child_count": len(element), "attributes": dict(element.attrib)})
        if element_id:
            identified.append({"id": element_id, "tag": tag, "parent_id": parent.get("id") if parent is not None else None})
        if tag == "text":
            texts.append({
                "index": index,
                "id": element_id,
                "parent_id": parent.get("id") if parent is not None else None,
                "text": "".join(element.itertext()),
                "attributes": dict(element.attrib),
            })
    return {
        "schema_version": "1.0",
        "svg": str(path),
        "sha256": sha256_file(path),
        "viewBox": root.get("viewBox"),
        "root": {"tag": _local(root.tag), "id": root.get("id"), "width": root.get("width"), "height": root.get("height")},
        "layers": layers,
        "elements_with_id": identified,
        "texts": texts,
        "summary": {"layers": len(layers), "elements_with_id": len(identified), "text_nodes": len(texts)},
    }


def _target(root: ET.Element, operation: dict[str, Any]) -> ET.Element:
    target_id = operation.get("target_id", operation.get("id"))
    if target_id is not None:
        matches = [element for element in root.iter() if element.get("id") == str(target_id)]
    else:
        xpath = operation.get("xpath")
        if not xpath:
            raise ValueError("Operation needs target_id/id or xpath")
        normalized = str(xpath)
        if normalized.startswith("//"):
            normalized = "." + normalized
        matches = root.findall(normalized)
    if len(matches) != 1:
        raise ValueError(f"Target must match exactly one element; matched {len(matches)}")
    return matches[0]


def _operation_value(operation: dict[str, Any], default: Any = None) -> Any:
    if "value" in operation:
        return operation["value"]
    return operation.get("new_value", default)


def _style_dict(element: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for declaration in element.get("style", "").split(";"):
        if ":" in declaration:
            key, value = declaration.split(":", 1)
            result[key.strip()] = value.strip()
    return result


def _set_style_properties(element: ET.Element, properties: dict[str, Any]) -> None:
    style = _style_dict(element)
    style.update({str(key): str(value) for key, value in properties.items()})
    element.set("style", ";".join(f"{key}:{value}" for key, value in style.items()))


def _set_text(element: ET.Element, value: Any) -> None:
    descendants = list(element.iter())
    element.text = str(value)
    if len(descendants) > 1:
        for descendant in descendants[1:]:
            descendant.text = None
            descendant.tail = None


def _number_text(value: Any) -> str:
    number = float(value)
    if not number == number or abs(number) == float("inf"):
        raise ValueError("Translation values must be finite")
    return str(int(number)) if number.is_integer() else format(number, ".15g")


def _recolor(element: ET.Element, operation: dict[str, Any]) -> dict[str, dict[str, str]]:
    old_color = operation.get("old_color", operation.get("old", operation.get("from_color")))
    new_color = operation.get("new_color", operation.get("new", operation.get("to_color")))
    explicit = {name: operation[name] for name in ("fill", "stroke") if name in operation and operation[name] is not None}
    replacements = operation.get("colors", {})
    if old_color is not None and new_color is not None:
        replacements = {**replacements, str(old_color): str(new_color)}
    normalized_replacements = {str(old).strip().lower(): str(new) for old, new in replacements.items()}
    changes: dict[str, dict[str, str]] = {}
    for node in element.iter():
        for attribute in list(node.attrib):
            local = _local(attribute)
            old = node.get(attribute, "")
            new = None
            if local in explicit:
                new = str(explicit[local])
            elif local in COLOR_ATTRIBUTES and (normalized_replacements or (old_color is None and new_color is not None)):
                if old_color is None and new_color is not None and local in {"fill", "stroke"}:
                    new = str(new_color)
                elif old.strip().lower() in normalized_replacements:
                    new = normalized_replacements[old.strip().lower()]
            if new is not None and new != old:
                node.set(attribute, new)
                changes[f"{node.get('id', _local(node.tag))}:{local}"] = {"old": old, "new": new}
        style = _style_dict(node)
        style_changes: dict[str, str] = {}
        for property_name, old in style.items():
            new = None
            if property_name in explicit:
                new = str(explicit[property_name])
            elif property_name in COLOR_ATTRIBUTES and (normalized_replacements or (old_color is None and new_color is not None)):
                if old_color is None and new_color is not None and property_name in {"fill", "stroke"}:
                    new = str(new_color)
                elif old.strip().lower() in normalized_replacements:
                    new = normalized_replacements[old.strip().lower()]
            if new is not None and new != old:
                style_changes[property_name] = new
                changes[f"{node.get('id', _local(node.tag))}:style.{property_name}"] = {"old": old, "new": new}
        if style_changes:
            _set_style_properties(node, style_changes)
    return changes


def _new_child(operation: dict[str, Any]) -> ET.Element:
    if operation.get("xml"):
        child = ET.fromstring(operation["xml"])
    else:
        spec = operation.get("child")
        if spec is None:
            spec = operation
        if not isinstance(spec, dict):
            raise ValueError("add_child needs a child object or XML string")
        tag = spec.get("tag")
        if not tag:
            raise ValueError("add_child needs child.tag")
        attributes = spec.get("attributes", spec.get("attrs", {}))
        child = ET.Element(_qname(str(tag)), {_qname(str(key)): str(value) for key, value in attributes.items()})
        if spec.get("id") is not None and "id" not in child.attrib:
            child.set("id", str(spec["id"]))
        if spec.get("text") is not None:
            child.text = str(spec["text"])
    if any(_local(node.tag) == "image" for node in child.iter()):
        raise ValueError("add_child refuses to introduce raster <image> elements")
    return child


def _apply_one(root: ET.Element, operation: dict[str, Any], index: int) -> tuple[dict[str, Any], set[str]]:
    op = operation.get("op", operation.get("operation"))
    if not op:
        raise ValueError("Operation is missing op")
    op = str(op)
    target = _target(root, operation)
    target_id = target.get("id")
    affected = _descendants(target)
    if not affected and target_id:
        affected = {target_id}
    edit_id = str(operation.get("edit_id", f"edit_{index:03d}"))
    entry: dict[str, Any] = {
        "edit_id": edit_id,
        "target_id": target_id,
        "op": op,
        "attribute": None,
        "old_value": None,
        "new_value": None,
        "source_ref": operation.get("source_ref"),
    }
    if op == "set_text":
        value = _operation_value(operation, operation.get("text"))
        if value is None:
            raise ValueError("set_text needs value/new_value/text")
        entry.update(attribute="text", old_value="".join(target.itertext()), new_value=str(value))
        _set_text(target, value)
    elif op == "set_attr":
        attribute = operation.get("attribute", operation.get("name"))
        if not attribute:
            raise ValueError("set_attr needs attribute/name")
        value = _operation_value(operation)
        qname = _qname(str(attribute))
        old = target.get(qname)
        if value is None:
            if qname in target.attrib:
                del target.attrib[qname]
        else:
            target.set(qname, str(value))
        entry.update(attribute=str(attribute), old_value=old, new_value=value)
    elif op == "set_style":
        properties = operation.get("style", operation.get("properties"))
        if properties is None:
            property_name = operation.get("property", operation.get("attribute"))
            if not property_name:
                raise ValueError("set_style needs a style object or property")
            properties = {property_name: _operation_value(operation)}
        if not isinstance(properties, dict):
            raise ValueError("set_style properties must be an object")
        before = _style_dict(target)
        _set_style_properties(target, properties)
        entry.update(
            attribute="style",
            old_value={key: before.get(key) for key in properties},
            new_value={key: _style_dict(target).get(key) for key in properties},
        )
    elif op == "recolor":
        changes = _recolor(target, operation)
        entry.update(attribute="color", old_value={key: value["old"] for key, value in changes.items()}, new_value={key: value["new"] for key, value in changes.items()})
    elif op == "translate":
        value = operation.get("value")
        if isinstance(value, (list, tuple)) and len(value) == 2:
            dx, dy = value
        else:
            dx = operation.get("dx", operation.get("x", 0))
            dy = operation.get("dy", operation.get("y", 0))
        translation = f"translate({_number_text(dx)} {_number_text(dy)})"
        old = target.get("transform")
        new = f"{translation} {old}" if old else translation
        target.set("transform", new)
        entry.update(attribute="transform", old_value=old, new_value=new)
    elif op == "set_visibility":
        visible = operation.get("visible", _operation_value(operation, True))
        if isinstance(visible, str):
            visible = visible.lower() not in {"false", "0", "hidden", "none"}
        value = "visible" if bool(visible) else "hidden"
        old = target.get("visibility")
        target.set("visibility", value)
        entry.update(attribute="visibility", old_value=old, new_value=value)
    elif op == "add_child":
        child = _new_child(operation)
        child_ids = _descendants(child)
        duplicates = child_ids.intersection(_ids(root))
        if duplicates:
            raise ValueError(f"add_child would duplicate ids: {sorted(duplicates)}")
        insert_at = operation.get("index")
        if insert_at is None:
            target.append(child)
        else:
            target.insert(int(insert_at), child)
        entry.update(attribute="child", old_value=None, new_value={"tag": _local(child.tag), "id": child.get("id"), "sha256": _subtree_hash(child)})
    elif op == "remove":
        if operation.get("confirm") is not True:
            raise ValueError("remove requires confirm: true")
        if target is root:
            raise ValueError("Refusing to remove the SVG root")
        parent = _parents(root).get(target)
        if parent is None:
            raise ValueError("Target has no parent")
        entry.update(attribute="element", old_value=_subtree_hash(target), new_value=None)
        parent.remove(target)
    else:
        raise ValueError(f"Unsupported operation: {op}")
    return entry, affected


def apply_operations(root: ET.Element, operations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
    changelog = []
    affected: set[str] = set()
    for index, operation in enumerate(operations, 1):
        entry, touched = _apply_one(root, operation, index)
        changelog.append(entry)
        affected.update(touched)
    return changelog, affected


def preservation_validation(original: ET.Element, edited: ET.Element, allow_ids: Iterable[str]) -> dict[str, Any]:
    allow = {str(value) for value in allow_ids if str(value)}
    warnings: list[dict[str, str]] = []
    original_ids = _ids(original, warnings, "original")
    edited_ids = _ids(edited, warnings, "edited")
    original_duplicate_ids = sorted({item["id"] for item in warnings if item["scope"] == "original"})
    edited_duplicate_ids = sorted({item["id"] for item in warnings if item["scope"] == "edited"})
    unexpected_duplicate_ids = sorted(set(edited_duplicate_ids) - set(original_duplicate_ids))
    original_allowed = _allowed_closure(original, allow)
    edited_allowed = _allowed_closure(edited, allow)
    missing = sorted(set(original_ids) - set(edited_ids))
    unexpected_missing = sorted(set(missing) - original_allowed)
    added = sorted(set(edited_ids) - set(original_ids))
    unexpected_added = [element_id for element_id in added if not _has_allowed_ancestor(edited_ids[element_id], edited, allow)]

    untouched_hashes: dict[str, dict[str, Any]] = {}
    mismatches = []
    for element_id, before in original_ids.items():
        if element_id in original_allowed or _has_allowed_ancestor(before, original, allow):
            continue
        after = edited_ids.get(element_id)
        before_hash = _subtree_hash(before)
        after_hash = _subtree_hash(after) if after is not None else None
        preserved = before_hash == after_hash
        untouched_hashes[element_id] = {"before": before_hash, "after": after_hash, "preserved": preserved}
        if not preserved:
            mismatches.append(element_id)
    outside_before = hashlib.sha256(_canonical_bytes(original, allow)).hexdigest()
    outside_after = hashlib.sha256(_canonical_bytes(edited, allow)).hexdigest()
    outside_preserved = outside_before == outside_after

    original_images = sum(_local(element.tag) == "image" for element in original.iter())
    edited_images = sum(_local(element.tag) == "image" for element in edited.iter())
    no_new_images = edited_images <= original_images
    original_text = [element for element in original.iter() if _local(element.tag) == "text"]
    edited_text = [element for element in edited.iter() if _local(element.tag) == "text"]
    removable_text = sum(element.get("id") in original_allowed or _has_allowed_ancestor(element, original, allow) for element in original_text)
    text_nodes_preserved = len(edited_text) >= len(original_text) - removable_text
    for before in original_text:
        element_id = before.get("id")
        if element_id and element_id not in original_allowed:
            after = edited_ids.get(element_id)
            if after is None or _local(after.tag) != "text":
                text_nodes_preserved = False

    id_set_preserved = not unexpected_missing and not unexpected_added
    untouched_preserved = outside_preserved and not mismatches
    valid = id_set_preserved and untouched_preserved and no_new_images and text_nodes_preserved and not unexpected_duplicate_ids
    return {
        "schema_version": "1.0",
        "allow_ids": sorted(allow),
        "warnings": warnings,
        "duplicate_ids": {"original": original_duplicate_ids, "edited": edited_duplicate_ids},
        "unexpected_duplicate_ids": unexpected_duplicate_ids,
        "original_id_count": len(original_ids),
        "edited_id_count": len(edited_ids),
        "id_set_preserved": id_set_preserved,
        "allowed_missing_ids": sorted(set(missing).intersection(original_allowed)),
        "unexpected_missing_ids": unexpected_missing,
        "added_ids": added,
        "unexpected_added_ids": unexpected_added,
        "untouched_subtree_hashes": untouched_hashes,
        "untouched_hash_mismatches": mismatches,
        "outside_allowlist_sha256": {"original": outside_before, "edited": outside_after},
        "untouched_preserved": untouched_preserved,
        "original_image_count": original_images,
        "edited_image_count": edited_images,
        "no_new_images": no_new_images,
        "original_text_count": len(original_text),
        "edited_text_count": len(edited_text),
        "text_nodes_preserved": text_nodes_preserved,
        "valid": valid,
    }


def _operations_payload(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        payload = payload.get("operations", payload.get("ops"))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("Edit operations must be a JSON list or an object containing operations/ops")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="List semantic IDs, layers, text, and viewBox")
    inspect_parser.add_argument("--svg", type=Path, required=True)
    inspect_parser.add_argument("--json-out", type=Path, required=True)
    apply_parser = subparsers.add_parser("apply", help="Apply local operations and emit preservation evidence")
    apply_parser.add_argument("--svg", type=Path, required=True)
    apply_parser.add_argument("--ops", type=Path, required=True)
    apply_parser.add_argument("--out", type=Path, required=True)
    apply_parser.add_argument("--log", type=Path, required=True)
    apply_parser.add_argument("--preservation", type=Path, required=True)
    validate_parser = subparsers.add_parser("validate", help="Validate IDs, untouched hashes, text, and no rasterization")
    validate_parser.add_argument("--orig", type=Path, required=True)
    validate_parser.add_argument("--edited", type=Path, required=True)
    validate_parser.add_argument("--allow-ids", default="")
    validate_parser.add_argument("--json-out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            report = inspect_svg(args.svg)
            emit("ok", report["summary"], report, args.json_out)
            return 0
        if args.command == "apply":
            original_tree = _parse(args.svg)
            edited_tree = _parse(args.svg)
            operations = _operations_payload(args.ops)
            changelog, affected = apply_operations(edited_tree.getroot(), operations)
            report = preservation_validation(original_tree.getroot(), edited_tree.getroot(), affected)
            report.update({
                "created_utc": utc_now_iso(),
                "original_svg": str(args.svg.resolve()),
                "original_sha256": sha256_file(args.svg),
                "edit_count": len(changelog),
            })
            if not report["valid"]:
                atomic_write_json(args.preservation, report)
                print("error: preservation validation failed; edited SVG was not written", file=sys.stderr)
                return 1
            _register_namespaces(args.svg)
            content = ET.tostring(edited_tree.getroot(), encoding="unicode", xml_declaration=False)
            atomic_write_text(args.out, '<?xml version="1.0" encoding="UTF-8"?>\n' + content + "\n")
            report["edited_svg"] = str(args.out.resolve())
            report["edited_sha256"] = sha256_file(args.out)
            log_payload = {
                "schema_version": "1.0",
                "created_utc": utc_now_iso(),
                "original_svg": str(args.svg.resolve()),
                "edited_svg": str(args.out.resolve()),
                "original_sha256": report["original_sha256"],
                "edited_sha256": report["edited_sha256"],
                "edits": changelog,
            }
            atomic_write_json(args.log, log_payload)
            emit("ok" if report["valid"] else "failed", {"edit_count": len(changelog), "untouched_preserved": report["untouched_preserved"], "id_set_preserved": report["id_set_preserved"]}, report, args.preservation)
            return 0 if report["valid"] else 1
        allow_ids = {item.strip() for item in args.allow_ids.split(",") if item.strip()}
        try:
            original = _parse(args.orig).getroot()
            edited = _parse(args.edited).getroot()
            report = preservation_validation(original, edited, allow_ids)
            report["xml_valid"] = True
        except (ET.ParseError, ValueError) as exc:
            report = {"xml_valid": False, "valid": False, "error": f"{type(exc).__name__}: {exc}"}
        report.update({"original_svg": str(args.orig.resolve()), "edited_svg": str(args.edited.resolve())})
        status = "ok" if report.get("valid") else "failed"
        emit(status, {"valid": report.get("valid"), "id_set_preserved": report.get("id_set_preserved"), "untouched_preserved": report.get("untouched_preserved"), "no_new_images": report.get("no_new_images"), "text_nodes_preserved": report.get("text_nodes_preserved")}, report, args.json_out)
        return 0 if report.get("valid") else 1
    except (OSError, ET.ParseError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
