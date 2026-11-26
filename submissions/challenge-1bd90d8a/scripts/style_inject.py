"""Editable matplotlib defaults, real glyph-coverage checks, and three-format export."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import atomic_write_json, atomic_write_text, emit, sha256_file

try:
    import matplotlib

    matplotlib.use("Agg", force=True)
    from matplotlib import font_manager, ft2font
    from cycler import cycler

    MATPLOTLIB_ERROR = None
except Exception as exc:  # Optional dependency: importing this module remains safe.
    matplotlib = None
    font_manager = None
    ft2font = None
    cycler = None
    MATPLOTLIB_ERROR = f"{type(exc).__name__}: {exc}"

PALETTES: dict[str, list[str]] = {
    "nature": ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#F0E442", "#000000"],
    "science": ["#0072B2", "#D55E00", "#009E73", "#E69F00", "#CC79A7", "#56B4E9", "#F0E442", "#000000"],
    "cell": ["#009E73", "#0072B2", "#E69F00", "#D55E00", "#56B4E9", "#CC79A7", "#F0E442", "#000000"],
    "okabe_ito": ["#000000", "#E69F00", "#56B4E9", "#009E73", "#F0E442", "#0072B2", "#D55E00", "#CC79A7"],
}

EDITABLE_RCPARAMS: dict[str, Any] = {
    "svg.fonttype": "none",
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "axes.unicode_minus": False,
    "savefig.dpi": 300,
    "figure.dpi": 150,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "xtick.direction": "out",
    "ytick.direction": "out",
    "font.family": "sans-serif",
}
FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".otc"}


def _is_cjk(char: str) -> bool:
    value = ord(char)
    return (
        0x3000 <= value <= 0x303F
        or 0x3400 <= value <= 0x4DBF
        or 0x4E00 <= value <= 0x9FFF
        or 0xF900 <= value <= 0xFAFF
        or 0x20000 <= value <= 0x3134F
        or 0x3040 <= value <= 0x30FF
        or 0xAC00 <= value <= 0xD7AF
        or 0xFF00 <= value <= 0xFFEF
    )


def _characters(text: str) -> list[str]:
    return sorted({char for char in text if char.isprintable() and not char.isspace()}, key=ord)


def _font_paths() -> tuple[list[str], dict[str, str]]:
    paths: set[str] = set()
    sources: dict[str, str] = {}
    if font_manager is not None:
        try:
            for path in font_manager.findSystemFonts():
                normalized = str(Path(path).resolve())
                paths.add(normalized)
                sources[normalized] = "matplotlib.findSystemFonts"
        except Exception:
            pass
    system_fonts = Path("/usr/share/fonts")
    if system_fonts.is_dir():
        try:
            for path in system_fonts.rglob("*"):
                if path.is_file() and path.suffix.lower() in FONT_SUFFIXES:
                    normalized = str(path.resolve())
                    paths.add(normalized)
                    sources.setdefault(normalized, "/usr/share/fonts")
        except OSError:
            pass
    executable = shutil.which("fc-list")
    if executable:
        try:
            result = subprocess.run(
                [executable, "--format=%{file}\n"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    path = Path(line.strip())
                    if path.is_file() and path.suffix.lower() in FONT_SUFFIXES:
                        normalized = str(path.resolve())
                        paths.add(normalized)
                        sources.setdefault(normalized, "fc-list")
        except (OSError, subprocess.SubprocessError):
            pass
    return sorted(paths, key=lambda value: value.lower()), sources


def _family_name(path: str) -> str:
    try:
        return str(font_manager.FontProperties(fname=path).get_name())
    except Exception:
        return Path(path).stem


def _coverage(path: str, chars: list[str]) -> dict[str, Any]:
    font = ft2font.FT2Font(path)
    missing = [char for char in chars if font.get_char_index(ord(char)) == 0]
    covered = len(chars) - len(missing)
    return {
        "path": path,
        "family": _family_name(path),
        "coverage": (covered / len(chars)) if chars else 1.0,
        "covered_count": covered,
        "required_count": len(chars),
        "missing_glyphs": missing,
    }


def _best_font(paths: Iterable[str], chars: list[str], preferred: tuple[str, ...]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    for path in paths:
        try:
            item = _coverage(path, chars)
        except Exception:
            continue
        lowered = f"{item['family']} {path}".lower()
        item["preference_rank"] = next((index for index, name in enumerate(preferred) if name in lowered), len(preferred))
        candidates.append(item)
    candidates.sort(key=lambda item: (-item["coverage"], item["preference_rank"], item["family"].lower(), item["path"]))
    best = candidates[0] if candidates else None
    preview = [
        {key: value for key, value in item.items() if key != "preference_rank"}
        for item in candidates[:10]
    ]
    return best, preview


def discover_fonts(labels_text: str = "") -> dict[str, Any]:
    """Discover fonts and verify every requested glyph using the font's actual cmap."""
    chars = _characters(labels_text)
    latin_chars = [char for char in chars if not _is_cjk(char)]
    cjk_chars = [char for char in chars if _is_cjk(char)]
    result: dict[str, Any] = {
        "matplotlib_available": matplotlib is not None,
        "required_characters": chars,
        "latin_characters": latin_chars,
        "cjk_characters": cjk_chars,
        "latin_font": None,
        "cjk_font": None,
        "missing_glyphs": chars.copy(),
        "candidates": {"latin": [], "cjk": []},
        "warnings": [],
    }
    if matplotlib is None:
        result["warnings"].append(f"matplotlib unavailable; cmap validation skipped: {MATPLOTLIB_ERROR}")
        return result
    paths, sources = _font_paths()
    latin_preference = ("arial", "helvetica", "liberation sans", "dejavu sans", "noto sans")
    cjk_preference = ("noto sans cjk", "source han sans", "wenquanyi", "microsoft yahei", "simhei", "pingfang")
    latin, latin_candidates = _best_font(paths, latin_chars, latin_preference)
    cjk, cjk_candidates = _best_font(paths, cjk_chars, cjk_preference)
    if cjk is None and not cjk_chars:
        cjk = None
    result["latin_font"] = latin
    result["cjk_font"] = cjk
    result["candidates"] = {"latin": latin_candidates, "cjk": cjk_candidates}
    result["scanned_font_count"] = len(paths)
    result["font_sources"] = {name: sum(source == name for source in sources.values()) for name in sorted(set(sources.values()))}
    missing = set(latin["missing_glyphs"] if latin else latin_chars)
    if cjk_chars:
        missing.update(cjk["missing_glyphs"] if cjk else cjk_chars)
    result["missing_glyphs"] = [char for char in chars if char in missing]
    if cjk_chars and cjk is None:
        result["warnings"].append("No readable CJK font was discovered; SVG text remains editable and uses a CJK fallback list.")
    if result["missing_glyphs"]:
        result["warnings"].append("Some requested characters have no glyph in the selected fonts; see missing_glyphs.")
    return result


def _font_families(font_report: dict[str, Any], cjk_font: str | None = None) -> list[str]:
    latin: list[str] = []
    if font_report.get("latin_font"):
        latin.append(font_report["latin_font"]["family"])
    latin.extend(["DejaVu Sans", "Arial", "sans-serif"])
    cjk: list[str] = []
    if cjk_font:
        cjk_path = Path(cjk_font)
        cjk.append(_family_name(str(cjk_path)) if cjk_path.is_file() and font_manager is not None else cjk_font)
    if font_report.get("cjk_font"):
        cjk.append(font_report["cjk_font"]["family"])
    cjk.extend(["Noto Sans CJK SC", "WenQuanYi Zen Hei", "Microsoft YaHei", "SimHei"])
    has_cjk = bool(font_report.get("cjk_characters")) or any(
        _is_cjk(char) for char in font_report.get("required_characters", [])
    )
    families = (cjk + latin) if has_cjk else (latin + cjk)
    return list(dict.fromkeys(families))


def apply_style(journal: str = "nature", cjk_font: str | None = None, labels_text: str = "") -> dict[str, Any]:
    """Apply editable, colorblind-safe journal styling without converting text to paths."""
    if matplotlib is None:
        raise RuntimeError(f"matplotlib is required to apply a style: {MATPLOTLIB_ERROR}")
    journal = journal.lower()
    if journal not in PALETTES:
        raise ValueError(f"Unknown journal {journal!r}; choose one of {', '.join(sorted(PALETTES))}")
    font_report = discover_fonts(labels_text)
    families = _font_families(font_report, cjk_font)
    settings = dict(EDITABLE_RCPARAMS)
    settings["font.sans-serif"] = families
    matplotlib.rcParams.update(settings)
    matplotlib.rcParams["axes.prop_cycle"] = cycler(color=PALETTES[journal])
    return {
        "journal": journal,
        "palette": PALETTES[journal].copy(),
        "font_families": families,
        "missing_glyphs": font_report["missing_glyphs"],
        "backend": matplotlib.get_backend(),
        "rcParams": {key: matplotlib.rcParams[key] for key in settings},
    }


def save_editable(fig: Any, out_stem: str | Path, dpi: int = 300) -> dict[str, Any]:
    """Save SVG, PDF and PNG and return their absolute paths and SHA-256 hashes."""
    if matplotlib is None:
        raise RuntimeError(f"matplotlib is required to save figures: {MATPLOTLIB_ERROR}")
    if dpi < 300:
        raise ValueError("PNG export must be at least 300 DPI")
    stem = Path(out_stem).expanduser().resolve()
    if stem.suffix.lower() in {".svg", ".pdf", ".png"}:
        stem = stem.with_suffix("")
    stem.parent.mkdir(parents=True, exist_ok=True)
    paths = {extension: stem.with_suffix(f".{extension}") for extension in ("svg", "pdf", "png")}
    fonttypes = {"svg.fonttype": "none", "pdf.fonttype": 42, "ps.fonttype": 42}
    original_fonttypes = {key: matplotlib.rcParams[key] for key in fonttypes}
    try:
        matplotlib.rcParams.update(fonttypes)
        fig.savefig(paths["svg"], format="svg", dpi=dpi)
        fig.savefig(paths["pdf"], format="pdf", dpi=dpi)
        fig.savefig(paths["png"], format="png", dpi=dpi)
    finally:
        matplotlib.rcParams.update(original_fonttypes)
    return {
        **{extension: str(path) for extension, path in paths.items()},
        "sha256": {extension: sha256_file(path) for extension, path in paths.items()},
    }


def _mplstyle_text(journal: str, families: list[str]) -> str:
    colors = ", ".join(repr(color) for color in PALETTES[journal])
    lines = [f"{key}: {value}" for key, value in EDITABLE_RCPARAMS.items() if key != "font.family"]
    lines.extend([
        "font.family: sans-serif",
        "font.sans-serif: " + ", ".join(families),
        f"axes.prop_cycle: cycler('color', [{colors}])",
        "",
    ])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", choices=sorted(PALETTES), default="nature")
    parser.add_argument("--labels-file", type=Path, help="UTF-8 task/label text whose actual glyphs will be checked")
    parser.add_argument("--cjk-font", help="Optional CJK family name or font file override")
    parser.add_argument("--mplstyle-out", type=Path)
    parser.add_argument("--json-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        labels = args.labels_file.read_text(encoding="utf-8", errors="replace") if args.labels_file else ""
        report = discover_fonts(labels)
        report["journal"] = args.journal
        report["palette"] = PALETTES[args.journal]
        report["font_families"] = _font_families(report, args.cjk_font)
        report["editable_rcParams"] = EDITABLE_RCPARAMS
        report["matplotlib_error"] = MATPLOTLIB_ERROR
        if args.mplstyle_out:
            atomic_write_text(args.mplstyle_out, _mplstyle_text(args.journal, report["font_families"]))
            report["mplstyle_out"] = str(args.mplstyle_out.resolve())
        status = "ok" if not report["missing_glyphs"] else "warning"
        emit(status, {
            "journal": args.journal,
            "latin_font": (report["latin_font"] or {}).get("family"),
            "cjk_font": (report["cjk_font"] or {}).get("family"),
            "missing_glyph_count": len(report["missing_glyphs"]),
            "scanned_font_count": report.get("scanned_font_count", 0),
        }, report, args.json_out)
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
