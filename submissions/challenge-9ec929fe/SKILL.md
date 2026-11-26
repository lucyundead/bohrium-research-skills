---
name: research-code-executor
description: Generate, execute, debug, and verify scientific Python for data cleaning, statistics, SQLite queries, visualization, and reproducible research results.
---

# Research Code Executor

## Rules
- Work in `/app/work`; deliver to `/app/output`; never modify `/input`.
- Read actual `case.json`, `task.md`, and `environment/resources.yaml`; labels are not evidence.
- Copy `case_id` and every `output_contract` key into a checklist; map required metrics, filters, units, and artifacts.
- Preserve raw values; record cleaning rules, commands, failures, assumptions, and uncertainty.
- Resolve `SKILL_DIR` from this loaded file; runtime default is `/skills/weekly-skill`.
- Compatible setup: `SKILL_DIR="${SKILL_DIR:-/skills/weekly-skill}"`; scripts self-locate `common.py`.
- Use the current Python executable. Install nothing unless a dependency is absent and installation is allowed.
- Efficiency hard limits: run each stage's script once unless an error requires a focused rerun; never repeat a clean audit.
- Target <=18 rounds / <=450s; always deliver within <=20 rounds / <=600s.

## Phase 0 — Contract and recon (rounds 1–2 / 0–60s)
- Run recon once; open SQLite read-only and inspect schema/storage classes before querying.
```bash
python "$SKILL_DIR/scripts/recon.py" \
  --input-dir /input --work-dir /app/work \
  --json-out /app/work/recon.json
```
- Record hashes, encodings, delimiters, headers, tables, sentinels, censored values, and fallbacks.
- Preserve labels and whitespace until an explicit, recorded mapping is justified.

## Phase 1 — Minimal analysis and checkpoint (rounds 3–8 / 60–240s)
- Write `/app/work/analysis.py` with explicit paths, cleaning rules, and strict JSON output.
- Set `matplotlib.use("Agg")` before pyplot; call `savefig(..., dpi=300)`, never `plt.show()`.
- use stdout summary + targeted python slices (never dump whole recon.json).
- Fix random seeds and `random_state`; distinguish missing values, sentinels, duplicates, outliers, and censored values.
- Write minimal `/app/output/result.json` immediately: completed values plus honest `null` pending values.
- Include `case_id`, `status:"partial"`, `sources`, `uncertainty`, and `change_log`; copy it to `/app/work/checkpoint.json`.
- Execute once with deterministic settings and tracked logs:
```bash
python "$SKILL_DIR/scripts/run_guard.py" \
  --script /app/work/analysis.py --work-dir /app/work \
  --out-dir /app/output --input-dir /input --timeout 120 \
  --json-out /app/work/run_report.json
```
- Script arguments follow standalone `--`; on failure inspect `diagnosis[]`, fix one cause, then rerun.

## Phase 2 — Independent audit (rounds 9–11 / 240–330s)
- Do not read `USAGE.md` or `scripts/*.py` source unless a command reports an error.
- Pair task-critical SQL with declarative pandas metrics; replace `x` and aliases below with real names.
- Minimal `/app/work/checks.json` (four lines):
```json
{"checks":[{
  "id":"summary","sql":"SELECT COUNT(x) AS n, AVG(x) AS mean FROM data",
  "pandas":{"n":{"column":"x","metric":"count"},"mean":{"column":"x","metric":"mean"}}
}]}
```
- Filters support comparisons, `isna`/`notna`, `in`, `between`, and composite `and`/`or` rule lists.
- Run once (omit unused options); `--min-dpi` defaults to 200 while 300-DPI compliance is also recorded:
```bash
python "$SKILL_DIR/scripts/dual_audit.py" \
  --input /input/ACTUAL.csv --recon /app/work/recon.json \
  --checks /app/work/checks.json --script /app/work/analysis.py \
  --figures /app/output/figure.png --min-dpi 200 \
  --json-out /app/output/crosscheck_report.json
```
- For SQLite use `/input/ACTUAL.db --table ACTUAL_TABLE` instead of CSV input.
- Review every `mismatches[]` and `skipped_columns[]`; dirty object columns with >=50% numeric values receive sentinel-aware baseline checks.
- `<num` values are censored, not missing sentinels; IQR/Z-score outliers are flags only.
- Exit codes: `recon:0/1; run_guard:0/1/124/125; dual_audit:0/1; contract_gate:0/1/2`.
- Informational warnings or a partial report are not reasons to rerun blindly; failed queries remain explicit errors.

## Phase 3 — Improve only when needed (rounds 12–14 / 330–390s)
- Diagnose mismatches via row sets, NULL semantics, filters, division, and precision.
- Check statistical assumptions and sample sizes; do not force unsupported p-values.
- After a real code/numeric change, rerun only the affected executor/audit and update checkpoints atomically.
- Keep scripts, figures, logs, checks, and reports inspectable; append rather than erase failure evidence.
- If the minimal result and audit are already clean, skip this phase.

## Phase 4 — Gate and repair (rounds 15–17 / 390–480s)
- Run the gate once:
```bash
python "$SKILL_DIR/scripts/contract_gate.py" \
  --case /input/case.json --output-dir /app/output \
  --work-dir /app/work --input-dir /input \
  --recon /app/work/recon.json \
  --audit /app/output/crosscheck_report.json \
  --best-effort --json-out /app/output/qc_report.json
```
- Repair `fix_list[]` by priority: exit 2 is fatal, exit 1 needs warning review, exit 0 is clean.
- Fatal issues include missing fields, wrong types/case_id, NaN/Infinity, and altered input hashes.
- If result is absent, `--best-effort` assembles an honest partial; null means pending, never zero or complete.
- After a repair, rerun the gate once; never claim success on exit 2.

## Phase 5 — Verify and stop (rounds 18–20 / 480–600s)
- Inspect `VERIFICATION.md`, targeted `qc_report.json` fields, and `manifest.json`.
- Confirm requested artifacts are nonempty, hashes are listed, numbers match outputs, and figures match sources/units.
- Confirm sources are real and uncertainty covers unresolved limits; provide an evidence-linked response.
- Stop after a clean pass; do not expand scope or repeat audits.

## Capability matrix
| Capability | Script | Evidence |
|---|---|---|
| Input/environment recon | `recon.py` | `recon.json` |
| Tracked execution/debug | `run_guard.py` | `run_report.json` |
| pandas ↔ SQLite + quality QC | `dual_audit.py` | `crosscheck_report.json` |
| Contract/integrity gate | `contract_gate.py` | `qc_report.json`, `VERIFICATION.md` |
| SHA-256 inventory | `contract_gate.py` | `manifest.json` |

## Honesty red lines
- Never invent fields, values, sources, query results, logs, software state, or reproducibility.
- Never hide failed/empty queries; preserve SQL, outcome, and downstream limits.
- Zero-variance correlation is undefined: return `null` and explain.
- Never convert censored/sentinel values to zero or exact values without authorization.
- Never fabricate complete status for partial work.
