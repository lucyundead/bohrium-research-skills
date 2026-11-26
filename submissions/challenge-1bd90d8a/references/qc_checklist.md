# QC 与交付检查表

先修复科学内容和来源绑定，再修复排版；每次修改后重新导出并重跑完整 `fig_qc.py`，不要只检查被改的一项。

## 7 项自动闸门

| 检查 | 通过重点 | 失败后的动作 |
|---|---|---|
| `svg_xml_and_viewbox` | XML 可解析、`viewBox` 存在、语义 `<g id>` 图层可识别 | 修复 XML/画布和图层 ID；Route C 不得整图替换 |
| `svg_text_editability` | 存在原生 `<text>`，无内嵌 `<image>`，无可疑文字路径簇 | 恢复可编辑文字；删除伪装矢量的栅格；不要转曲 |
| `svg_text_overlap` | 估算文本 bbox 不重叠，祖先平移已计入 | 调整位置、字号、换行或画布；重新渲染复核 |
| `png_dpi_and_resolution` | DPI 元数据或 pHYs 换算达到 `--min-dpi`，像素尺寸合理 | 用至少 300 DPI 真正重渲，不只修改元数据 |
| `png_nonblank_and_clipping` | 灰度标准差大于 3.0，图像非空白，四边无异常墨迹裁切 | 检查数据/轴范围、图例与标签边距，再真实重渲 |
| `pdf_validity` | `%PDF`、非空、页数有效；后端可用时文字可提取 | 重新导出矢量 PDF；记录后端不可用或文字提取限制 |
| `source_value_and_contract` | 来源值匹配，`case_id` 和全部契约字段存在，无 NaN/Infinity | 修复绘图/绑定/result；不得改源数据或编造缺项 |

每项都审查 `measured`、`threshold`、`severity` 和 `fix_hint`。退出码 0 才能视为自动闸门通过；1 表示有失败项，2 表示输入或执行错误。三轮修复仍失败时保留报告和诚实 `partial` 状态。

## 6 个评分检查点的人工复核

- **科学实体**：名称、数量、类型与输入一致；没有靠常识补入未声明实体。
- **空间关系**：隔室归属、方向、激活/抑制/结合/转位语义与来源一致；连线不压字。
- **数据编码**：x/y、柱高、颜色、分组、单位、过滤与聚合均可追溯到 `plotted_arrays.json` 和源文件；`verify` 全匹配。
- **图例标注**：标题、轴、刻度、单位、图例、上下标和特殊字符完整；字体报告的 missing glyph 已解决或明确披露。
- **可编辑源文件**：SVG 保留 `<text>` 和语义图层；Route C 的 changelog 完整，非 allowlist 子树 100% 保留。
- **导出格式**：任务要求的 SVG/PDF/PNG 都真实存在且非空；PNG 至少 300 DPI；最终 PNG 经过人工视觉检查。

## 结果与证据闭环

- [ ] `/app/output/result.json` 含真实 `case_id` 和每个 `output_contract` 字段。
- [ ] `status` 只反映真实完成度；pending 为 `null`，所有限制写入 `uncertainty`。
- [ ] `sources` 指向真实文件；来源哈希、映射和独立复算报告一致。
- [ ] `change_log` 记录生成、编辑、QC 修复和降级，不删除失败证据。
- [ ] 第 25 请求前已保存 result 与 `/app/work/checkpoint.json`，之后每阶段更新。
- [ ] `qc_report.json` 对应当前最终产物，不引用旧图或旧 result。
- [ ] `manifest.json` 覆盖交付目录实际产物，大小和 SHA-256 已刷新。
- [ ] `VERIFICATION.md` 能让 Judge 找到每项结论的证据，不包含虚构通过声明。
- [ ] Route A：`SOURCE_MAP.md`、`source_mapping.json`、`plotted_arrays.json` 和值验证报告齐全。
- [ ] Route B：spec、构建报告、渲染报告齐全；自动避让和关系语义已人工复核。
- [ ] Route C：`edit_changelog.json`、`preservation_report.json`、`edit_validation.json` 齐全且有效。
- [ ] 渲染降级、缺字、未解决碰撞、未绑定值和缺失产物均未被隐藏。

## 最后停止条件

请求超过 54、墙钟超过 750 秒或剩余不足 10 请求时停止扩展范围，优先修复致命契约缺项、刷新 QC/清单并完成诚实交付。自动 QC 通过不证明科学判断正确；任何未验证内容都必须明确说明。
