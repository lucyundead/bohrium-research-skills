# 图表与 SVG 配方

本文件只定义结构和操作，不提供任何案例数值或生物学答案。路径、标签、实体、关系与样式均须从实际任务和来源确定。

## 数据图表：从源值到三格式产物

1. 从 `case.json`、`task.md` 和实际源文件确认图表类型、字段、单位、过滤、聚合与图例要求。
2. 用 `style_inject.py --labels-file` 检查所有实际标签字符；保留字体报告。
3. 绘图脚本调用 `apply_style`，给关键 artist 设置稳定语义 `gid`，再调用 `save_editable(fig, out_stem, dpi=300)`。
4. 在生成图像的同一执行中写出 `plotted_arrays.json`，避免手工抄录绘制值。
5. 每个数值叶节点必须能绑定到源文件和列/JSON 定位；过滤、行选择和聚合要与绘图代码一致。
6. 先跑 `source_ledger.py verify`，再跑 `build` 生成台账和可读映射；所有 mismatch/unbound 都要回到绘图逻辑修复。

绑定示例仅展示结构：

```json
{
  "series_id": "observed",
  "source_file": "/input/ACTUAL.csv",
  "column": "measurement",
  "where": {"group": "ACTUAL_GROUP"},
  "units": "ACTUAL_UNIT",
  "values": [1.25, 2.5]
}
```

数组必须是真实绘制值，不是占位数值。JSON 来源优先用 `locator` 或 `json_path`；表格来源用 `column`/`x_column`/`y_column`。`row`/`rows` 是源记录索引，不是显示行号；仅在绘图确实按这些索引取数时使用。

## 机制图 spec

顶层必填 `width`、`height`，两者为正有限数；`title` 可省略。`compartments`、`entities`、`edges`、`legend` 是数组，省略时按空数组处理。

```json
{
  "width": 800,
  "height": 600,
  "title": "ACTUAL TITLE",
  "compartments": [{
    "id": "compartment_id", "label": "ACTUAL LABEL",
    "x": 20, "y": 60, "w": 300, "h": 400,
    "fill": "#ffffff", "stroke": "#263238", "source_ref": "ACTUAL SOURCE LOCATOR"
  }],
  "entities": [{
    "id": "entity_id", "label": "ACTUAL ENTITY", "compartment": "compartment_id",
    "shape": "rounded", "x": 80, "y": 140, "w": 120, "h": 60,
    "fill": "#ffffff", "stroke": "#263238", "source_ref": "ACTUAL SOURCE LOCATOR"
  }],
  "edges": [{
    "id": "relation_id", "source": "entity_id", "target": "other_entity_id",
    "kind": "activation", "label": "ACTUAL RELATION LABEL", "source_ref": "ACTUAL SOURCE LOCATOR"
  }],
  "legend": [{"id": "legend_item", "label": "ACTUAL LEGEND LABEL", "kind": "activation"}]
}
```

约束：

- 实体形状仅 `rect`、`rounded`、`ellipse`、`hexagon`。
- 关系仅 `activation`、`inhibition`、`binding`、`translocation`；不要自造箭头语义。
- `source`/`target` 必须引用已声明实体，不支持自环和重复的有向实体对。
- `compartment` 必须引用已声明隔室；坐标为画布坐标，不是隔室内相对坐标。
- `source_ref` 应指向真实文件及可定位证据；构建器写入 `data-source`，但不会验证其科学含义。
- 构建报告中的 `collision_resolution.adjustments` 是自动布局变化；仍须检查 `unresolved_collisions` 和真实渲染图。
- 可给图例项设置 `x`、`y`；也可用 `legend_x`、`legend_y` 控制默认起点。图例色块项可设 `fill`、`stroke`。

## 局部 SVG 编辑操作

操作必须精确命中一个元素，优先使用稳定的 `target_id`。常见结构：

```json
{"operations": [
  {"op": "set_text", "target_id": "ACTUAL_ID", "value": "ERRATA TEXT", "source_ref": "ACTUAL EVIDENCE"},
  {"op": "set_attr", "target_id": "ACTUAL_ID", "attribute": "font-size", "value": "14"},
  {"op": "set_style", "target_id": "ACTUAL_ID", "style": {"fill": "#0072B2"}},
  {"op": "recolor", "target_id": "ACTUAL_ID", "old_color": "#OLD", "new_color": "#NEW"},
  {"op": "translate", "target_id": "ACTUAL_ID", "dx": 8, "dy": 0},
  {"op": "set_visibility", "target_id": "ACTUAL_ID", "visible": true}
]}
```

- `set_attr` 的 `value:null` 删除属性；命名空间前缀必须已知。
- `set_style` 也可使用 `property` 加 `value`；`recolor` 可用 `fill`/`stroke` 或 `colors` 映射。
- `translate` 可写 `value:[dx,dy]`，或 `dx`/`dy`；数值必须有限。
- `add_child` 使用 `child:{tag,attributes,id,text}` 或 `xml`，可用 `index` 指定插入位置；拒绝重复 ID 和 `<image>`。
- `remove` 仅在工单明确授权时使用，并添加 `confirm:true`；不能删除 SVG 根节点。
- `edit_id` 可显式指定；`source_ref` 应记录该修改的工单或来源证据。

## 保留证明与渲染

`apply` 未通过保留校验时不会写出编辑 SVG。通过后仍以 `validate` 独立检查：非 allowlist 子树哈希、ID 集合、文本节点和无新增栅格均须合格。allowlist 只包含实际允许变化的目标及后代。

渲染后同时检查 SVG 和 PNG/PDF，不以“命令退出”替代视觉审查。`render_report.json` 为 `PARTIAL` 时，缺失格式必须继续走容器降级链或如实保留未完成状态。
