# 使用与接口说明

以下命令中的 `SKILL_DIR` 是 Skill 根目录，正式运行通常为 `/skills/weekly-skill`。所有 `ACTUAL_*` 都是占位符，必须换成真实输入；不得据此猜测案例文件名。工作文件放 `/app/work`，交付文件放 `/app/output`。

## 1. 样式、字体与三格式导出

```bash
python "$SKILL_DIR/scripts/style_inject.py" \
  --journal nature --labels-file /input/task.md \
  --mplstyle-out /app/work/figure.mplstyle --json-out /app/work/font_report.json
```

参数：

- `--journal`：`nature`、`science`、`cell` 或 `okabe_ito`，默认 `nature`。
- `--labels-file`：可选 UTF-8 文本，用真实字符集检查字体 cmap 覆盖。
- `--cjk-font`：可选 CJK 字体族名或字体文件覆盖。
- `--mplstyle-out`、`--json-out`：分别为可选样式文件和字体报告。

绘图脚本应在导入 pyplot 前使用 Agg，并调用：

```python
import sys
sys.path.insert(0, str(SKILL_DIR / "scripts"))
import style_inject
style_inject.apply_style(journal="nature")
exports = style_inject.save_editable(fig, "/app/output/figure", dpi=300)
```

`apply_style` 设置 `svg.fonttype="none"`、`pdf.fonttype=42`、`ps.fonttype=42` 和 300 DPI 保存默认值。`save_editable` 返回 SVG/PDF/PNG 路径与 SHA-256；`dpi<300` 会拒绝导出。报告中出现 `missing_glyphs` 时必须保留警告并更换有足够覆盖的字体，不得转曲规避。

## 2. 来源台账与独立数值复算

### build

```bash
python "$SKILL_DIR/scripts/source_ledger.py" build \
  --case /input/case.json --sources /input/ACTUAL_SOURCE \
  --plotted /app/output/plotted_arrays.json --svg /app/output/figure.svg \
  --rtol 1e-6 --json-out /app/output/source_mapping.json \
  --md-out /app/output/SOURCE_MAP.md
```

`--case`、`--sources`、`--json-out`、`--md-out` 必填；`--sources` 接收一个或多个实际 CSV/TSV/JSON 文件。`--plotted` 和 `--svg` 可按管线省略。机制图没有数值数组时不要伪造 `--plotted`。

### verify

```bash
python "$SKILL_DIR/scripts/source_ledger.py" verify \
  --sources /input/ACTUAL_SOURCE --plotted /app/output/plotted_arrays.json \
  --rtol 1e-6 --json-out /app/output/value_verification.json
```

只有全部比较匹配时 `all_match=true` 且退出码为 0；不匹配、未绑定或无法独立复算都必须处理。不要用 `build` 的退出码代替对报告内容的检查。

### reverse

```bash
python "$SKILL_DIR/scripts/source_ledger.py" reverse \
  --svg /input/ACTUAL.svg --json-out /app/work/reverse_ledger.json
```

该命令提取图层、文本和图元，生成的是待绑定清单，不会自动推断来源或证明数值正确。

### plotted_arrays.json 绑定方式

顶层可为系列对象或数组。数值叶节点继承父对象中的元数据；推荐显式写出：

```json
{
  "series_id": "series_a",
  "source_file": "/input/ACTUAL.csv",
  "x_column": "x",
  "y_column": "y",
  "units": "task-defined unit",
  "x": [1, 2],
  "y": [3, 4]
}
```

也可使用 `column`、`field`、`locator`/`json_path`、`where`/`filter`、`row`/`rows` 和 `statistic`/`stat`/`aggregation`。聚合支持 `mean`、`sum`、`min`、`max`、`median`、样本/总体标准差与方差、`count`。只有输入中实际执行的过滤或变换才可写入；多个来源时显式 `source_file` 可避免歧义。

## 3. 机制图构建与渲染

```bash
python "$SKILL_DIR/scripts/mechanism_svg.py" build \
  --spec /app/work/mechanism_spec.json --out /app/output/figure.svg \
  --json-out /app/output/mechanism_build.json
python "$SKILL_DIR/scripts/mechanism_svg.py" render \
  --svg /app/output/figure.svg --png-out /app/output/figure.png \
  --pdf-out /app/output/figure.pdf --dpi 300 --json-out /app/output/render_report.json
```

`render` 至少要求 `--png-out` 或 `--pdf-out` 之一，`--dpi` 默认 300，允许 1–2400；本任务交付不得用低于 300 的 PNG 冒充合格结果。本地后端全部失败时返回 `PARTIAL`，按 `fallback_guidance` 调用容器 `browser-automation` 或 `office-renderer`，再对真实产物运行 QC。

spec 的完整字段与最小结构见 `references/chart_and_svg_recipes.md`。构建器只支持输入声明的实体和关系，不会补充生物学知识。`unresolved_collisions` 非空时必须扩大画布或调整布局后重建。

## 4. 保源局部 SVG 编辑

```bash
python "$SKILL_DIR/scripts/svg_edit.py" inspect \
  --svg /input/ACTUAL.svg --json-out /app/work/svg_tree.json
python "$SKILL_DIR/scripts/svg_edit.py" apply \
  --svg /input/ACTUAL.svg --ops /app/work/edit_ops.json \
  --out /app/output/figure.svg --log /app/output/edit_changelog.json \
  --preservation /app/output/preservation_report.json
python "$SKILL_DIR/scripts/svg_edit.py" validate \
  --orig /input/ACTUAL.svg --edited /app/output/figure.svg \
  --allow-ids target_id,edited_descendant_id \
  --json-out /app/output/edit_validation.json
```

操作文件可为操作数组，或 `{"operations": [...]}` / `{"ops": [...]}`。每条操作用 `target_id`/`id` 或唯一匹配的 `xpath` 定位。支持 `set_text`、`set_attr`、`set_style`、`recolor`、`translate`、`set_visibility`、`add_child`、`remove`；`remove` 必须显式 `confirm:true`，新增子树不得包含 `<image>`。

`--allow-ids` 仅列出工单允许改变的目标及其后代；默认空字符串。`apply` 会依据实际操作生成保留报告，`validate` 用显式 allowlist 独立复核。两者都必须通过；失败时不得交付编辑结果，更不能扩大 allowlist 掩盖误改。

## 5. 统一 QC 与交付闸门

```bash
python "$SKILL_DIR/scripts/fig_qc.py" --svg /app/output/figure.svg \
  --png /app/output/figure.png --pdf /app/output/figure.pdf \
  --ledger /app/output/source_mapping.json \
  --plotted /app/output/plotted_arrays.json --source /input/ACTUAL_SOURCE \
  --case /input/case.json --result /app/output/result.json \
  --output-dir /app/output --min-dpi 300 --json-out /app/output/qc_report.json
```

`--svg` 和 `--json-out` 是主输入/报告；其余按实际产物与管线提供。`--source` 用于来源复算，多源数值已由 `source_ledger.py verify` 全量核对时仍须保留该报告。提供 `--output-dir` 后生成 `manifest.json` 和 `VERIFICATION.md`。

每项检查包含 `check`、`pass`、`severity`、`measured`、`threshold`、`fix_hint`。退出码：0=全部通过，1=存在失败项，2=致命输入错误。按 `fix_hint` 修复并完整重跑，最多三轮；不能通过时保持 `partial` 并写明限制。

`result.json` 至少包含真实 `case_id`、`status`、`sources`、`uncertainty`、`change_log` 和案例 `output_contract` 的每个字段。首版可以是 `partial`，但第 25 请求前必须写入 `/app/output/result.json` 和 `/app/work/checkpoint.json`；pending 用 `null`，禁止 NaN/Infinity 或编造值。
