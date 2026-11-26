# Bohrium Research Skills (`challenge-9ec929fe` & `challenge-1bd90d8a`)

Two production-grade, Harbor-compliant Agent Skills engineered for the **DeepModeling / Bohrium AI Product Weekly Contest** (`play.bohrium.com`):

| Track | Challenge ID | Skill Name | Files / Size | Core Deterministic Tooling |
|---|---|---|---|---|
| **科研代码生成与执行** | `challenge-9ec929fe` | `research-code-executor` | 12 files / 124.5 KB | `recon.py`, `run_guard.py`, `dual_audit.py`, `contract_gate.py` |
| **科研数据图表生成与编辑** | `challenge-1bd90d8a` | `sciviz-editable-studio` | 14 files / 183.7 KB | `style_inject.py`, `source_ledger.py`, `svg_edit.py`, `mechanism_svg.py`, `fig_qc.py` |

## Design Philosophy

Prose-only skills add minimal measurable delta over modern harnesses. Both skills follow a **deterministic script + compact procedural `SKILL.md` + machine-verifiable evidence** architecture:

1. **`research-code-executor` (`submissions/challenge-9ec929fe/`)**
   - **Data & Environment Recon (`recon.py`)**: Read-only SQLite URI (`mode=ro`), delimiter/encoding/header probing, sentinel (`?`, `NA`, `-999`) vs censored (`<0.01`) separation, compact `typeof()` summaries, fallback matrix.
   - **Deterministic Runner (`run_guard.py`)**: Pins `MPLBACKEND=Agg`, `OPENBLAS_NUM_THREADS=1`, `OMP_NUM_THREADS=1`, `PYTHONHASHSEED=0`; provides fuzzy column suggestions against `/input` on `KeyError`.
   - **Dual-Engine Cross-Audit (`dual_audit.py`)**: Independent Pandas ↔ SQLite numerical verification (`rtol=1e-9`), sentinel-aware object-column checks, compound filters (`and`/`or`/`in`/`between`), figure nonblank/DPI QC.
   - **Contract & Integrity Gate (`contract_gate.py`)**: Early-checkpoint delivery, strict `output_contract` + finite JSON validation, input SHA-256 immutability verification, `manifest.json`, and `VERIFICATION.md`.

2. **`sciviz-editable-studio` (`submissions/challenge-1bd90d8a/`)**
   - **Editable Typography (`style_inject.py`)**: Forces `svg.fonttype="none"`, `pdf.fonttype=42`, `ps.fonttype=42`; real FT2Font cmap glyph coverage checks; colorblind-safe journal palettes (`nature`, `science`, `cell`, `okabe_ito`).
   - **Source Provenance Ledger (`source_ledger.py`)**: Independent source-to-plot value recomputation (with elementwise transforms `log10`/`ln`/`sqrt`/`scale`/`offset` + aggregations), reverse SVG extraction, `source_mapping.json` & `SOURCE_MAP.md`.
   - **Source-Preserving Local SVG Editor (`svg_edit.py`)**: Namespace-safe local edits (`set_text`, `set_attr`, `recolor`, `translate`, `add_child`, `remove`), exact before/after `edit_changelog.json`, canonical hash verification of all untouched subtrees.
   - **Semantic Mechanism Engine & Render Chain (`mechanism_svg.py`)**: Layered `<g id>` + `<marker>` + editable `<text>` SVG builder with collision resolution and multi-backend renderer (`cairosvg` → `rsvg-convert` → `inkscape` → `PyMuPDF` → headless browser / Playwright).
   - **7-Gate Automated Visual QC (`fig_qc.py`)**: XML/viewBox, editable text/no raster `<image>`, transform-aware text overlap, PNG DPI/size, downsampled nonblank/clipping, PDF validity, source/contract gate (`--edit-mode` aware).

## Empirical Kimi (`kimi-code/k3.1-pro`) A/B Ablation Summary

Evaluated across 6 isolated local Harbor-style cases (`R1`–`R3`, `V1`–`V3`; 18 real Kimi runs total, ground truth isolated from sandboxes):

| Case | Baseline | Skill v1 | Skill v2 | Key Verified Gain |
|---|---|---|---|---|
| `R1_concrete_dirty` | 90.0 (15r / 311.9s) | 100.0 (34r / 1043.9s) | **100.0 (16r / 342.3s)** | +10.0 pts, 3.05× faster v1→v2, full dual-audit + QC + manifest |
| `R2_iris_bezdek_sqlite` | 70.0 (13r / 279.6s) | 80.0 (19r / 790.2s) | **80.0 (21r / 289.0s)** | +10.0 pts, 2.73× faster v1→v2, dual-engine reconciliation |
| `R3_wine_censored` | 100.0 (13r / 532.6s) | 100.0 (31r / 915.8s) | **100.0 (30r / 299.0s)** | 1.78× faster than Baseline, 3.06× faster v1→v2, honest `partial` |
| `V1_source_value_chart` | 100.0 (13r / 331.7s) | 100.0 (30r / 452.2s) | **100.0 (33r / 585.2s)** | 16 vs 7 evidence files, 7/7 QC gates (0 warnings), SVG/PDF/300-DPI PNG |
| `V2_pathway_figure03` | 100.0 (18r / 591.3s) | 100.0 (26r / 715.3s) | **100.0 (28r / 510.4s)** | 80.9s faster than Baseline; delivers PDF + `SOURCE_MAP.md` + `VERIFICATION.md` |
| `V3_svg_edit_workorder` | 100.0 (12r / 296.5s) | 100.0 (30r / 367.1s) | **100.0 (43r / 600.0s)** | Delivers rendered PNG+PDF previews (absent in Baseline) + preservation hash proof; early checkpoint achieved 100.0 even under 600s cutoff |

## Quick Self-Test

```bash
python submissions/challenge-9ec929fe/examples/minimal_demo.py
python submissions/challenge-1bd90d8a/examples/minimal_demo.py
```

## License

MIT License (see `LICENSE`).
