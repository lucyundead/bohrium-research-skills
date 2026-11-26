# 故障恢复手册

原则：保留现场，按证据定位，一次修一个原因，再运行同一受控命令。不要换一段未验证代码掩盖失败。

| 症状 | 先查 | 恢复动作 | 必留证据 |
|---|---|---|---|
| `ModuleNotFoundError` | recon 的库版本与 `fallback_matrix` | 用已装库/标准库降级；仅获准时安装 | 包名、版本、降级路径 |
| `FileNotFoundError` | case inputs、实际目录、相对路径 | 从 case/recon 解析绝对路径；不写 `/input` | 解析后路径、输入哈希 |
| `KeyError`/缺列 | schema、列名空白、映射表 | 按数据字典修正映射；模糊匹配仅作提示 | 实际列名、采用映射 |
| CSV 解析异常 | 编码、分隔符、引号、坏行 | 用 recon 检测结果显式设置；不静默跳行 | 参数、坏行位置、处置 |
| SQLite 只读/权限错误 | URI 是否 `mode=ro`、写入目标 | 原库只读；派生表写 work/output | 连接方式、目标路径 |
| `sqlite3.OperationalError` | 表/列、引号、SQL 语法、函数 | 先运行最小 SELECT，再恢复完整条件 | SQL、完整错误、schema |
| 查询返回空行 | 过滤值、NULL、连接条件、空白 | 分步计数定位空集来源；保留空结果 | 各步计数、最终 SQL |
| 双引擎不一致 | 行集、NULL、除法、聚合顺序、别名 | 先对齐语义，再比较数值；禁止手改报告 | 双方值、容差、根因 |
| NaN/Infinity | 空组、除零、溢出、未定义统计量 | 改为 `null`，解释原因；检查分母和输入 | JSON 路径、触发计算 |
| 零方差相关 | 列唯一值、过滤后样本 | 报告未定义；不得输出 0 或伪造扰动 | 方差、样本量、null |
| 离群值过多 | 单位、哨兵、批次、IQR/Z 规则 | 只标记；按任务授权规则单独敏感性分析 | 规则、行索引、影响 |
| `MemoryError` | 输入体积、全量复制、并行度 | 分块/投影列/降低并行；保留聚合口径 | 降级范围、未完成部分 |
| `TimeoutExpired` | 实际耗时、最后日志、中间 checkpoint | 缩小范围或优化；从 checkpoint 恢复 | 超时时长、已完成阶段 |
| `SyntaxError` | 行号、最近编辑、Python 版本 | 修复语法后运行 py_compile 和同一脚本 | 错误行、修复记录 |
| 无界面绘图崩溃 | backend 设置位置 | pyplot 前设 Agg；通过 run_guard 执行 | backend、运行日志 |
| 空白/低 DPI 图 | 非白方差、保存参数、数据是否为空 | 修复数据和绘制；保存时设置 DPI 后重检 | 审计项、修复后图 |
| 图像标签错误 | 源字段、单位、过滤和图例 | 从源数据重新绑定标签；重渲染重审计 | 源定位、前后变更 |
| 随机结果漂移 | seed、随机状态、线程、输入顺序 | 固定种子/状态与单线程；重跑比较 | 种子、版本、两次结果 |
| result.json 缺失 | checkpoint、剩余预算 | 用 `--best-effort` 组装 partial；无 checkpoint 不编造 | 使用 checkpoint、限制 |
| 契约字段缺失 | `qc_report.json` 的 `fix_list[]` | 填真实结果；未完成填 null + uncertainty | 字段级修复记录 |
| 输入哈希变化 | 当前路径、预期/实际 SHA-256 | 停止交付，恢复只读原始来源，重跑分析 | 两个哈希、影响范围 |
| 闸门退出 1 | warnings、partial、pending、审计告警 | 可修复则修复；无法解决则保留限制 | 最终告警、诚实状态 |
| 闸门退出 2 | fatal_errors、缺失/非法/篡改 | 禁止宣称完成；逐项修复后重跑 | 致命项与修复证据 |

## 预算耗尽

1. 不再扩展分析；确保 work 中有最新 checkpoint。
2. 若 result 缺失，以 `--best-effort` 运行闸门。
3. 保留 fatal/warning/fix_list，不删除失败证据。
4. 最终说明仅陈述已完成部分、验证范围和未决限制。

## 恢复后闭环

代码变化后重跑 `run_guard.py`；数值变化后重跑 `dual_audit.py`；任何最终产物变化后重跑 `contract_gate.py`。只有这样才能确保日志、数值和 manifest 哈希对应同一版本。
