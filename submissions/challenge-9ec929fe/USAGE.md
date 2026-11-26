# 使用与接口说明

以下命令中的 `SKILL_DIR` 是 Skill 根目录，正式运行通常为 `/skills/weekly-skill`。所有 `--json-out` 均原子写入严格 JSON；控制台只保留短摘要。

## 1. 输入侦察

```bash
python "$SKILL_DIR/scripts/recon.py" \
  --input-dir <dir> --work-dir <dir> --json-out <recon.json>
```

读取 `case.json`、`task.md`、可选 `environment/resources.yaml`，扫描输入文件并记录 SHA-256、格式、表结构、数据质量和运行环境。先根据摘要定位风险，再读取需要的 JSON 字段。

## 2. 受控执行

```bash
python "$SKILL_DIR/scripts/run_guard.py" \
  --script <analysis.py> --work-dir <dir> --out-dir <dir> \
  --timeout 120 --json-out <run_report.json> [-- script_args...]
```

使用当前 Python 执行脚本，设置无界面绘图和单线程数值后端，记录退出码、耗时、stdout/stderr 尾部和产物变化。退出码：0 成功，1 脚本失败，124 超时，125 runner 错误。不要根据“没有报错”推断数值正确。

## 3. 双引擎与质量审计

```bash
python "$SKILL_DIR/scripts/dual_audit.py" \
  --input <csv_or_db> [--table <name>] [--checks <checks.json>] \
  [--script <analysis.py>] [--figures <png1> <png2> ...] \
  --json-out <crosscheck_report.json>
```

支持 CSV、TSV、JSON、JSONL 和 SQLite。表格输入导入内存 SQLite；数据库输入反向读入 pandas。多表 SQLite 必须指定 `--table`。所有数值列比较 `count/sum/mean/min/max`，相对容差 `1e-9`，绝对容差 `1e-12`。

### 自定义 checks JSON

顶层可以是检查列表，也可以是 `{"checks": [...]}`。每项包含：

- `id`：可选标识。
- `sql`：只返回一行的 SELECT。多指标用列别名。
- `pandas`：单个指标对象，或“SQL 别名 → 指标对象”的映射。
- 指标对象：`column` + `metric`；可选 `filter: {column, operator, value}`。
- 指标：`count/sum/mean/min/max/median/std/nunique`；`rows` 不需要 `column`。
- filter 操作符：`==, !=, >, >=, <, <=, isna, notna`。

单值示例：

```json
{
  "checks": [
    {
      "id": "filtered_mean",
      "sql": "SELECT AVG(measurement) AS value FROM data WHERE group_name = 'A'",
      "pandas": {
        "column": "measurement", "metric": "mean",
        "filter": {"column": "group_name", "operator": "==", "value": "A"}
      }
    }
  ]
}
```

多指标示例：

```json
{
  "checks": [{
    "id": "summary",
    "sql": "SELECT COUNT(measurement) AS n, SUM(measurement) AS total FROM data",
    "pandas": {
      "n": {"column": "measurement", "metric": "count"},
      "total": {"column": "measurement", "metric": "sum"}
    }
  }]
}
```

SQL 与 pandas 必须表达同一过滤和 NULL 口径。自定义 SQL 必须只返回一行；执行失败会进入 `mismatches[]`，不会用 pandas 值冒充 SQL 成功。

### 审计输出

- `engine_consistency`：所有已执行双引擎比较是否一致。
- `baseline_comparisons[]`、`custom_comparisons[]`：双方原值与通过状态。
- `mismatches[]`：不一致或执行失败；此时脚本退出码为 1。
- `statistical_quality`：缺失、`?`/`NA`/`<0.01`、重复行、IQR/Z-score、零方差。
- `reproducibility`：seed/random_state、可能写入 `/input`、`plt.show()`。
- `figures[]`：非空白像素方差、非白比例、DPI 元数据。

离群值只标记，行不会被删除。质量告警不阻断报告，退出码为 0；输入、配置或双引擎失败为 1。

## 4. 交付闸门

```bash
python "$SKILL_DIR/scripts/contract_gate.py" \
  --case <case.json> --output-dir <dir> --work-dir <dir> \
  [--input-dir <dir>] [--recon <recon.json>] [--audit <crosscheck.json>] \
  [--best-effort] --json-out <qc_report.json>
```

`output_dir/result.json` 必须包含所有 `output_contract` 字段以及：

```json
{
  "case_id": "来自 case.json",
  "status": "complete | partial | failed",
  "sources": [],
  "uncertainty": [],
  "change_log": []
}
```

契约支持字段名列表、`{"fields": [...]}`、`{"required_fields": [...]}`、`{"properties": {...}}` 或直接字段映射。字段描述可含 `name` 和 `type`；常见类型会校验。`null` 是诚实的 pending，但产生告警，不代表完成。

### partial 恢复

只有 `result.json` 缺失且指定 `--best-effort` 时才恢复。闸门依次读取：

1. `work_dir/checkpoint.json`
2. `work_dir/result_checkpoint.json`
3. `work_dir/checkpoints/*.json`（按修改时间）

checkpoint 可以是结果对象，或包含 `result` 对象。闸门合并已有真值，强制 `case_id` 与 `status="partial"`，附加不确定性及变更记录，再原子写入。没有 checkpoint 不会编造结果。

### 输入与产物校验

同时提供 `--input-dir` 和 `--recon` 时，recon 中的文件路径及 SHA-256 会与当前原始文件比较。哈希变化或原文件缺失是致命错误；未纳入 recon 的文件会告警。只提供其中一个参数不会执行完整性校验。

闸门还完成：

- 递归拒绝 `NaN`、`Infinity`、`-Infinity`。
- 校验 `case_id` 和常见字段类型。
- 纳入审计状态；双引擎不一致进入修复清单。
- 写 `qc_report.json` 与 `VERIFICATION.md`。
- 扫描输出目录所有文件并写 `manifest.json`；manifest 排除自身，避免自哈希悖论。

退出码：0=通过；1=partial、pending 或有告警；2=结果缺失、契约致命缺项、非法数值、输入篡改或闸门错误。修复 `fix_list[]` 后必须重跑，以刷新全部哈希。

## 最小演示

```bash
python examples/minimal_demo.py
```

**EXAMPLE ONLY**：演示现场创建 20 行合成数据，不引用任何正式数据或答案。它运行 recon、受控分析、双引擎审计和最终闸门，并断言报告、图像与清单存在。临时目录自动删除。
