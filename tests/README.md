# 离线合成验收集

> **TEST-ONLY｜仅供系统测试｜不得用于真实案件、检索结论或法院提交。**

本目录只含虚构案件、虚构法源、路由契约和离线验收代码，不含真实客户事实、个人信息、真实案号或可直接引用的法律结论。

## 目录

| 路径 | 用途 |
|---|---|
| `fixtures/simple-case/` | 虚构普通买卖合同案，验证标准流程、证据门禁和待批准提交包 |
| `fixtures/complex-case/` | 虚构冲突日期案，验证争点局部深析、双向通信和重大风险保留 |
| `fixtures/incremental-case/` | 八批虚构零碎材料，验证案件内记忆、日期候选、客户指令、弱信号、局部影响、长期工单与失败运行 |
| `fixtures/authority-corpus/` | 一条支持、一条重大不利、一条仅摘要不可核验的虚构法源 |
| `fixtures/template-cases/` | 模板存在、缺失、许可不明、字段缺失、版本过期五种情形 |
| `fixtures/template-reference-cases/` | 个人模板编号/别名、禁止迁移范围和“诉状＋手续”套件解析 |
| `contracts/` | 自然语言路由、四种记忆读取、三种写入、并发版本、增量生命周期、`ok`、失效、证据门禁和无 API 降级契约 |
| `acceptance/` | 人类可读的验收标准和路由表 |
| `run_acceptance.py` | 无第三方依赖的静态验收器，并可选调用根目录 CLI |

## 执行

在项目根目录运行：

```powershell
python -B -m unittest discover -s tests -p "test_*.py" -v
python tests/run_acceptance.py --static-only
```

根目录 `scripts/legal_case_cli.py` 可用后运行：

```powershell
python tests/run_acceptance.py
```

生成机器可读报告：

```powershell
python tests/run_acceptance.py --json-report tests/test-report.json
```

`--static-only` 只验证测试夹具自身；默认模式还检查根 CLI 的命令契约。任何 `TEST-ONLY` 法源在生产模式都必须被拒绝，无论其测试状态是否标为“已核验”。

默认模式目前还覆盖：60 条自然语言控制回归和 5 条跨对象篡改测试（TaskFrame、单问门控、Quick/Standard/Deep/Discuss、否定、无上下文记忆关闭、命令/参数/引文/无引号正文隔离、客户邮件、外部动作阻断、学习接口固定关闭）、临时工作区初始化/索引、v1.0/v1.1→v1.2 幂等升级且原件不变、增量批次幂等、失败不推进游标、`off/file_scoped/relevant/reflect` 上下文胶囊、案件内短视图、过期对话冲突、局部失效、程序工单、失败 RunAttempt、只读恢复、DOCX 预检与逐项目标清洁、有状态清洁批准审计、最小修改、模板门禁、G2 精确快照、真实 PDF 组卷，以及始终为零的外部动作。
