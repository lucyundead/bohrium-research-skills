---
name: sciviz-editable-studio
description: Generate scientific charts and mechanism diagrams, preserve local SVG edits, export editable SVG/PDF and 300-DPI PNG, and run visual QC.
---

# SciViz Editable Studio

## Rules and efficiency
- Resolve `SKILL_DIR="${SKILL_DIR:-/skills/weekly-skill}"` from the loaded skill root.
- Set `W=/app/work O=/app/output S="$SKILL_DIR/scripts"`; never modify `/input`.
- Read `/input/case.json`, `/input/task.md`, and `/input/environment/resources.yaml` if present.
- Checklist `case_id` and every `output_contract` field before drawing.
- **DO NOT read `USAGE.md`, `references/`, or `scripts/*.py` unless a command fails**; target <=16 requests / <=450s.
- Use the schemas/commands below; replace only `ACTUAL_*` placeholders.
- Keep text editable, values source-bound, and sources unchanged.

## Phase 0 — Contract and route (requests 1–3)
- Inventory sources, errata, entities, relations, labels, and exports.
- Route A: data chart/`source_value_chart`; B: mechanism/flowchart; C: existing-SVG edit.
- Combined tasks retain evidence; never replace C with a redraw.
- Read targeted JSON slices, not large dumps.

## Phase 1 — Build (requests 4–8)
### Route A — Source-valued chart
- Write actual figure labels to `$W/labels.txt`, then probe glyphs:
```bash
python "$S/style_inject.py" --journal nature --labels-file $W/labels.txt \
  --mplstyle-out $W/figure.mplstyle --json-out $W/font_report.json
```
- Import `style_inject`; call `apply_style(..., labels_text=ACTUAL_LABELS)`, then `save_editable(fig, "/app/output/figure", dpi=300)`.
- Set semantic `gid`s; no `plt.show()`. Minimal `$O/plotted_arrays.json` anchor:
```json
{"series_id":"s1","source_file":"raw.csv","x_column":"x","y_column":"y","x":[1,2],"y":[3,4],"transform":"none","aggregation":null,"units":{}}
```
- Bind with `column`/`locator`, `where`, `rows`; apply `transform` before `aggregation`.
```bash
python "$S/source_ledger.py" build --case /input/case.json \
  --sources /input/ACTUAL_SOURCE --plotted $O/plotted_arrays.json \
  --svg $O/figure.svg --json-out $O/source_mapping.json --md-out $O/SOURCE_MAP.md
python "$S/source_ledger.py" verify --sources /input/ACTUAL_SOURCE \
  --plotted $O/plotted_arrays.json --rtol 1e-6 --json-out $O/value_verification.json
```
- Put all sources after `--sources`; repair mismatch/unbound items, never source data.

### Route B — Mechanism/flowchart
- Extract content only from input evidence. Minimal `$W/mechanism_spec.json` anchor:
```json
{"title":"Title","width":640,"height":400,"compartments":[{"id":"c1","label":"C1","x":20,"y":60,"w":600,"h":280}],"entities":[{"id":"a","label":"A","x":80,"y":150,"w":140,"h":70,"shape":"rounded","compartment":"c1","source_ref":"raw.csv"},{"id":"b","label":"B","x":400,"y":150,"w":140,"h":70,"shape":"ellipse"}],"edges":[{"id":"a-b","source":"a","target":"b","kind":"activation","label":""}],"legend":[{"id":"l1","label":"Activation","kind":"activation"}]}
```
- Entity `shape`: `rect|rounded|ellipse|hexagon`; edge `kind`: `activation|inhibition|binding|translocation`.
```bash
python "$S/mechanism_svg.py" build --spec $W/mechanism_spec.json \
  --out $O/figure.svg --json-out $O/mechanism_build.json
python "$S/mechanism_svg.py" render --svg $O/figure.svg \
  --png-out $O/figure.png --pdf-out $O/figure.pdf --dpi 300 --json-out $O/render_report.json
python "$S/source_ledger.py" build --case /input/case.json \
  --sources /input/ACTUAL_SOURCE --svg $O/figure.svg \
  --json-out $O/source_mapping.json --md-out $O/SOURCE_MAP.md
```
- Resolve `unresolved_collisions`; record `PARTIAL` rendering honestly and retain fallback guidance.

### Route C — Source-preserving local SVG edit
```bash
python "$S/svg_edit.py" inspect --svg /input/ACTUAL.svg --json-out $W/svg_tree.json
python "$S/source_ledger.py" reverse --svg /input/ACTUAL.svg --json-out $W/reverse_ledger.json
```
- Match workorder/errata to exact IDs. Minimal `$W/edit_ops.json` anchor:
```json
{"operations":[{"op":"set_text","target_id":"label1","value":"New label"},{"op":"translate","target_id":"node1","dx":10,"dy":0},{"op":"set_attr","target_id":"node1","attribute":"fill","value":"#0072B2"}]}
```
- Ops: `set_text|set_attr|set_style|recolor|translate|set_visibility|add_child|remove`.
```bash
python "$S/svg_edit.py" apply --svg /input/ACTUAL.svg --ops $W/edit_ops.json \
  --out $O/figure.svg --log $O/edit_changelog.json --preservation $O/preservation_report.json
python "$S/svg_edit.py" validate --orig /input/ACTUAL.svg --edited $O/figure.svg \
  --allow-ids ACTUAL_TARGET_IDS --json-out $O/edit_validation.json
python "$S/mechanism_svg.py" render --svg $O/figure.svg \
  --png-out $O/figure.png --pdf-out $O/figure.pdf --dpi 300 --json-out $O/render_report.json
```
- Allow only edited target/descendant IDs; every other subtree hash must match.

## Phase 2 — First delivery and QC (requests 9–16)
- By request 12, atomically write `$O/result.json` and copy it to `$W/checkpoint.json`.
- Include real `case_id`, `status:"partial"`, `sources`, `uncertainty`, `change_log`, and all contract fields.
- Pending fields may be `null`; `complete` forbids null required fields and requires all gates.
- Route A exact gate:
```bash
python "$S/fig_qc.py" --svg $O/figure.svg --png $O/figure.png --pdf $O/figure.pdf \
  --ledger $O/source_mapping.json --plotted $O/plotted_arrays.json --source /input/ACTUAL_SOURCE \
  --case /input/case.json --result $O/result.json --output-dir /app/output \
  --min-dpi 300 --json-out $O/qc_report.json
```
- Route B exact gate (no numeric provenance options):
```bash
python "$S/fig_qc.py" --svg $O/figure.svg --png $O/figure.png --pdf $O/figure.pdf \
  --case /input/case.json --result $O/result.json --output-dir /app/output \
  --min-dpi 300 --json-out $O/qc_report.json
```
- Route C exact gate (`--edit-mode` is mandatory):
```bash
python "$S/fig_qc.py" --svg $O/figure.svg --png $O/figure.png --pdf $O/figure.pdf --edit-mode \
  --case /input/case.json --result $O/result.json --output-dir /app/output \
  --min-dpi 300 --json-out $O/qc_report.json
```
- Seven gates: XML/viewBox; text/no raster; overlap; PNG DPI/size; content/clipping; PDF; source/contract.
- Inspect the PNG; geometry alone is insufficient.

## Repair and stop
- Follow `fix_hint`; fix one cause, regenerate exports, rerun the full gate; at most three QC rounds.
- Route C stays local and reruns `apply`/`validate`; inherited layers/outlined text may warn, but images and invalid XML fail.
- Refresh result, `change_log`, ledgers, `manifest.json`, and `VERIFICATION.md`; retain failure reports.

| Requests / time | Action | Evidence |
|---|---|---|
| 1–3 / 0–60s | Contract and route | checklist, inventory |
| 4–8 / 60–240s | First routed build | figure/spec/edit reports |
| 9–12 / 180–450s | First delivery and QC | result, ledger, `qc_report.json` |
| <=16 / <=450s target | Finish one repair if needed | regenerated artifacts |
| <=750s | Stop expanding scope | best honest checkpoint |
| 750–900s | Final verification only | manifest, verification |

## Honesty red lines
- Never use `svg.fonttype='path'`, text curves, or raster `<image>` as vector output.
- Never rewrite edit-workorder SVGs or broaden allowlists to hide changes.
- Never invent content, sources, render/QC success, or contract fields.
- Never omit glyph, collision, renderer, unbound-value, duplicate-ID, or partial warnings.
- `complete` requires all gates; otherwise report `partial`/`failed` and concrete limits.
