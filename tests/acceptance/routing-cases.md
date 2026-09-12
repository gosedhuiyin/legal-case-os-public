# 路由验收用例

> **TEST-ONLY：所有上下文均指向虚构夹具。机器可读全量契约见 `../contracts/routing-cases.json`。**

| 用户请求或状态 | 预期路由 | 必须行为 |
|---|---|---|
| “把这件普通买卖合同案从材料跑到待提交包” | orchestrator -> standard flow | 每次推进一个阶段并停在门禁 |
| “看一下这个材料” | `legal-material-intake` | 默认简短汇报，读取范围内处理，不联网，不起草 |
| “你觉得大致内容是什么” | `legal-material-intake` summarize | 报告已处理范围、内容主干和缺项 |
| “不要那么复杂，告诉我主要问题在哪” | `legal-case-analysis` | 只缩短展示，不能降低内部深析或隐藏重大风险 |
| “时效起算有三个冲突日期” | `legal-case-analysis` deep mode | 只下钻该争点并写明回归条件 |
| “站在对方角度看，最能打我的点是什么” | `legal-case-analysis` adverse lens | 给出最强反方路径、不利事实、证据缺口和改变结论的事实 |
| “帮我找一下支持的案例” | `legal-authority-research` | 允许时检索公开来源；保留重大不利结果；仅摘要不可引用 |
| “这十份材料哪些可以作为候选证据” | `legal-evidence-gate` | 最多到 `candidate`，不得批准 |
| “这份证据先不要提交，放到后续备用” | `legal-evidence-gate` | 写入律师决定 `reserve`，从当前提交集合排除 |
| “刚才就这样，直接做证据册”但无G2 | `legal-filing-packager` | 拒绝组正式包并列出待批准项 |
| “按我以前的代理词风格写” | `legal-style-exemplar-retrieval` -> drafting | 只使用获批范例，不迁移旧案事实 |
| “依照简模001号，生成诉状和手续” | personal template resolve -> exemplar retrieval -> drafting -> packager | 只加载唯一登记模板及套件列明手续；别名冲突或哈希变化即停止 |
| “把代理词压到3000字” | `legal-draft-compressor` -> review | 报告删改和高风险变化，不能新增实体内容 |
| “只修改第二项诉讼请求，其他一个字不要动” | drafting -> minimal diff | 范围锁外差异必须为零 |
| “去掉内部备注、审稿词和有权移除的水印” | review -> sanitizer -> review | 只处理自有派生稿获批项目；第三方证据水印和实质内容阻断 |
| “顺便生成手续，整理文件夹” | `legal-filing-packager` | 复制生成候选目录，不移动、删除或改名原件 |
| “整理成待打印包，但不要打印、不要提交” | `legal-filing-packager` | 最多生成 G4 候选；外部动作数量为零 |
| 授权委托书缺身份证号 | `legal-filing-packager` | 生成显式待补草稿，不猜测 |
| 上游证据文件哈希变化 | orchestrator | 使G2、依赖文书和包清单失效 |
| 只有一个刚展示且版本未变的待批准对象时“ok” | orchestrator approve | 只批准该对象及精确哈希 |
| 无对象、多个对象或候选已过期时“ok” | orchestrator | 澄清或拒绝，不改变状态 |
| “不要联网，只看我给的材料” | analysis + network deny | 只用允许范围，明确未完成法源问题 |
| “帮我做个人理财或收入规划” | 不触发本项目任何Skill | 明确超出法律案件项目范围 |
