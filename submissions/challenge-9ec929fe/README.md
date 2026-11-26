# research-code-executor

面向科研代码生成与执行的可复用 Skill：从真实输入和输出契约出发，完成数据清洗、统计分析、SQLite 查询、可视化、调试、独立数值审计与交付校验。它不携带正式案例答案，也不代替科学判断。

## 组成

- `SKILL.md`：Phase 0→5 执行协议、64 请求/900 秒预算和早落盘规则。
- `scripts/recon.py`：输入画像、原始文件 SHA-256、环境与降级建议。
- `scripts/run_guard.py`：受控执行、超时/错误诊断、日志和产物记录。
- `scripts/dual_audit.py`：pandas ↔ SQLite 对账、统计质量、复现性和 PNG 检查。
- `scripts/contract_gate.py`：契约/JSON/输入完整性校验、partial 恢复、证据清单。
- `references/`：静默错误自查表与故障恢复手册。
- `examples/minimal_demo.py`：临时目录内现场生成 20 行合成数据并跑通四脚本。

## 环境与安装

运行时按 `/skills/weekly-skill` 安装；本地可放在任意目录。脚本通过 `__file__` 定位，不依赖调用时的当前目录。

- Python 3.10+；核心 recon/runner/gate 使用标准库。
- `dual_audit.py` 需要 pandas；传入 PNG 时还需要 Pillow。
- 分析脚本按任务选用 numpy/scipy/scikit-learn/matplotlib；缺包时先读 recon 的 `fallback_matrix`。
- 无显示器环境下必须保存图像而非 `plt.show()`；`run_guard.py` 自动设置 Agg。

无需将输入复制进 Skill。正式运行时输入位于 `/input`，中间文件位于 `/app/work`，交付物位于 `/app/output`。原始输入只读，SQLite 只读打开。

## 快速开始

```bash
export SKILL_DIR=/skills/weekly-skill
python "$SKILL_DIR/scripts/recon.py" \
  --input-dir /input --work-dir /app/work --json-out /app/work/recon.json
python "$SKILL_DIR/scripts/run_guard.py" \
  --script /app/work/analysis.py --work-dir /app/work \
  --out-dir /app/output --timeout 120 --json-out /app/work/run_report.json
python "$SKILL_DIR/scripts/dual_audit.py" \
  --input /input/data.csv --checks /app/work/checks.json \
  --script /app/work/analysis.py --figures /app/output/figure.png \
  --json-out /app/output/crosscheck_report.json
python "$SKILL_DIR/scripts/contract_gate.py" \
  --case /input/case.json --output-dir /app/output --work-dir /app/work \
  --input-dir /input --recon /app/work/recon.json \
  --audit /app/output/crosscheck_report.json --best-effort \
  --json-out /app/output/qc_report.json
```

把 `data.csv`、`analysis.py`、图名等替换为实际对象；SQLite 多表数据库加 `--table`。详细参数和 checks JSON 架构见 `USAGE.md`。

## 每次交付的证据

- `result.json`：全部契约字段、`case_id`、状态、来源、不确定性和变更记录。
- `crosscheck_report.json`：双引擎比较、质量告警和图像检查。
- `qc_report.json`：机器可读通过项、致命项、告警及 `fix_list[]`。
- `VERIFICATION.md`：Judge 可直接阅读的验证摘要。
- `manifest.json`：输出目录产物的相对路径、大小和 SHA-256；排除 manifest 自身。

闸门退出码：0=通过，1=partial/有告警，2=致命缺项。结构通过不等于科学结论正确；仍须审查统计口径与任务要求。

## 本地最小复现

```bash
python examples/minimal_demo.py
```

演示仅使用临时目录中的合成玩具数据，标注 **EXAMPLE ONLY**，结束时自动清理，不读取正式案例、不写入 `/input`。它会执行四个脚本并验证关键证据文件。

## 诚实边界

不补造缺失字段、失败查询、来源或软件状态。未完成字段用 `null` 并在 `uncertainty` 解释；零方差相关系数必须报告为未定义，而不是 0。异常值默认只标记不删除，审查值不自动转为精确数值。

发布前将 `GITHUB_URL.txt` 第一行替换为无需登录即可访问的公开仓库 URL，第二行保留本题目录。
