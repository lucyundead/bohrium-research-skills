# sciviz-editable-studio

面向科研数据图表、生物机制示意图和既有 SVG 局部编辑的可复用 Skill。核心交付是原生可编辑矢量文件、300 DPI PNG、PDF、源数据台账和自动 QC 证据；不包含正式案例答案，也不代替科学判断。

## 组成

- `SKILL.md`：三条管线的 Phase 0–5 路由、64 请求/900 秒预算、早落盘协议和诚实红线。
- `scripts/style_inject.py`：真实字形覆盖检查、可编辑 matplotlib 样式、SVG/PDF/PNG 三格式导出。
- `scripts/source_ledger.py`：源文件哈希、绘制值独立复算、SVG 元素反向清单。
- `scripts/mechanism_svg.py`：spec 驱动的语义分层机制图 SVG，以及带降级记录的 PNG/PDF 渲染。
- `scripts/svg_edit.py`：限定目标的局部 SVG 编辑、变更日志、未命中子树保留校验。
- `scripts/fig_qc.py`：7 项视觉/格式/来源/契约检查，生成 `manifest.json` 与 `VERIFICATION.md`。
- `USAGE.md` 与 `references/`：精确 CLI、JSON 结构、修复配方和交付检查表。

## 运行目录

正式运行通常安装到 `/skills/weekly-skill`，本地可放在任意目录：

```bash
export SKILL_DIR="${SKILL_DIR:-/skills/weekly-skill}"
```

输入位于 `/input`，中间文件写入 `/app/work`，最终产物写入 `/app/output`。不要修改输入；所有示例路径都必须替换为案例中实际存在的文件。

Python 脚本通过自身位置加载辅助模块，不依赖调用时的当前目录。`style_inject.py` 的绘图功能需要 matplotlib；机制图构建与局部编辑使用标准库。渲染和 QC 会探测可用的可选后端，缺失时必须保留降级或警告记录。

## 三条管线

1. **Route A：数据图表**  
   字体探测与样式注入 → 绘图并调用 `save_editable` → 写出 `plotted_arrays.json` → `source_ledger.py build/verify` → 统一 QC。
2. **Route B：机制图/流程图**  
   从实际输入提取 spec → `mechanism_svg.py build` → `render` → 来源台账 → 统一 QC。渲染器不可用时按报告调用容器工具，不伪造导出。
3. **Route C：保源局部编辑**  
   `inspect` 与 `reverse` → 按工单生成局部操作 → `apply` → `validate` → `render` → 统一 QC。禁止整图重绘。

完整命令见 `SKILL.md` 和 `USAGE.md`。

## 每次交付的证据

- 可编辑 SVG、PDF、至少 300 DPI 的 PNG，以及任务要求的其他产物。
- `source_mapping.json` / `SOURCE_MAP.md`：来源路径、SHA-256、数值绑定与比较结果。
- `plotted_arrays.json`（数据图表）：实际绘制值及来源定位元数据。
- `edit_changelog.json` / `preservation_report.json`（局部编辑）：逐条前后值与保留证明。
- `qc_report.json`：7 项检查的实测值、阈值、严重级别和 `fix_hint`。
- `manifest.json` / `VERIFICATION.md`：产物清单、哈希和 Judge 可读摘要。
- `result.json`：`case_id`、全部 `output_contract` 字段、状态、来源、不确定性和变更记录；第 25 请求前必须已有诚实首版。

## 诚实边界

不把文字转路径，不用内嵌栅格图冒充矢量，不编造数据或生物关系，不隐藏 missing glyph、碰撞、渲染失败或来源未绑定。结构检查通过不等于科学内容正确；仍须对照任务和真实渲染图审查。

发布前将 `GITHUB_URL.txt` 第一行的用户名替换为实际公开仓库所有者，第二行保留本题目录。
