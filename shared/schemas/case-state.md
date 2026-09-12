# 统一案件状态契约

## 唯一真值源

每个案件只有 `_case-state/case-state.json` 可以表达“现在是什么状态”。案件记忆跟随案件保存在 `_case-state/memory/`，但其中的短当前视图、主题卡、索引、上下文胶囊和运行日志都是可重建派生物。聊天记忆、分析Markdown、派生索引、文件夹名称、审计日志和打印清单都不能替代规范状态。

聊天窗口只是一次工作界面：它可以按许可范围读取状态切片并提出变更，但不能拥有独立案件记忆。规范更新必须先读取当前文件和 `focus.state_version`，在内存中校验，再通过事务命令原子替换写回并追加审计事件。

机器契约见 [case-state.schema.json](./case-state.schema.json)，审计事件见 [audit-event.schema.json](./audit-event.schema.json)。标准库校验入口：

```powershell
python scripts/legal_case_os.py validate --state "案件目录\_case-state\case-state.json" --json
```

## 关键对象

| 对象 | 稳定前缀 | 作用 |
|---|---|---|
| `Matter` | `M-` | 案件身份、阶段、环境和目标 |
| `Source` | `S-` | 原始文件哈希、版本、页数、OCR和保密状态 |
| `Fact` | `F-` | 确认、争议、未知或推断事实及页码/行号来源 |
| `Issue` | `I-` | 争点、重要性、标准/深析模式和状态 |
| `Theory` | `T-` | 请求、抗辩或程序理论 |
| `Evidence` | `E-` | AI建议与律师决定分离的证据策略对象 |
| `Authority` | `A-` | 法源等级、核验状态、是否可用于生产和不利属性 |
| `Decision` | `D-` | 候选方案与律师决定 |
| `Approval` | `P-` | 绑定门禁、对象版本和哈希的批准 |
| `Artifact` | `R-` | 探讨稿、内部稿或法院候选及输入快照 |
| `Template` | `TPL-` | 模板来源、许可、哈希、版本和字段契约 |
| `ProcessingCoverage` | `PC-` | OCR、转写或翻译的覆盖范围与失败位置 |
| `SanitizationReport` | `SR-` | 清洁前后哈希、逐项授权、阻断项和复审状态 |
| `PackageManifest` | `B-` | 提交候选包的精确对象、版本、路径、顺序和哈希 |
| `ExternalAction` | `X-` | 与G4包哈希绑定的外部动作记录 |
| `Task` | `K-` | 工单下的步骤、责任人、依赖、期限、等待条件、输出和完成事件 |
| `MaterialBatch` | `MB-` | 一次边界明确的新增材料到件，按内容保持幂等并记录游标 |
| `CaseEvent` | `CE-` | 来源绑定的案件事件，分开记录发生、收到和入账时间 |
| `MaterialTriageCard` | `TC-` | 单份新增材料的相关性、主路由、直接关联对象和候选/提交状态 |
| `DeadlineRecord` | `DL-` | 从原文抽取并经核验、驳回、完成或取代的期限记录 |
| `ImpactAssessment` | `IA-` | 新材料影响哪些对象、哪些对象失效、反证和是否需要深析 |
| `ProspectiveMatterSeed` | `SEED-` | 可能衍生新案的弱信号、来源、反证、人审决定和来源案链接 |
| `WorkflowInstance` | `WF-` | 跨聊天持续存在的撤诉、补证、保全、调报告等法律业务工单 |
| `RunAttempt` | `RUN-` | 一次可失败、终止和重试的AI/脚本执行，不等于业务完成 |
| `MemoryState` | — | 本案语义游标、短视图版本、反思游标和待处理候选指针 |

## 案件记忆不是长聊天

`MaterialBatch → CaseEvent/MaterialTriageCard → DeadlineRecord/ImpactAssessment/ProspectiveMatterSeed/WorkflowInstance` 是后续零碎材料的增量链。原始文件仍由 `Source` 指向 `00-originals/`；状态只保存哈希、路径、定位、关系、决定和必要短摘要，不复制原件全文或整段聊天。

`CurrentCaseView`、`ContextCapsule` 和 `ProceduralCapsule` 应带输入对象/版本与 `as_of_event_seq`。它们可以丢弃后重建，不能反向覆盖 `Fact`、`Evidence`、`Decision`、`DeadlineRecord` 或 `WorkflowInstance`。

`WorkflowInstance` 与 `RunAttempt` 必须分离：工单可以跨几个月和多个窗口处于等待状态；一次运行失败、被杀死或没有提交输出，只改变运行记录，不完成工单。完成工单须有与当前版本匹配的输出或 `CaseEvent` 作为凭证。

## IntentEnvelope 与 FocusContext

自然语言首先产生三个**瞬时控制对象**，分别由
[task-frame.schema.json](./task-frame.schema.json)、
[clarification-decision.schema.json](./clarification-decision.schema.json) 和
[run-spec.schema.json](./run-spec.schema.json) 约束。它们用于冻结本轮含义、
判断是否只问一个问题、以及把 Quick/Standard/Deep/Discuss 展开为可验收
工作，不写入案件事实集合。兼容接口另由
[preference-candidate.schema.json](./preference-candidate.schema.json) 约束，
其状态恒为 `none`，候选内容恒为空；本语言模块不提取、提议、晋升或保存偏好和语言别名。

`TaskFrame` 将问题/输出范围、读取 ACL、证据来源、记忆/网络策略、文件
修改权限、用户难度提示与系统评估难度分开，并保存命令/引文分段和字段
来源。`RunSpec.verification_result` 在编译时为空；只有执行并校验后才可写
运行记录，不能用“已经编译”冒充“已经检索/生成/审阅”。

`IntentEnvelope` 同时记录内部推理深度和对外展示长度。“不要复杂”只把 `display_length` 调为 `brief`，不能把已识别的深析争点降为标准分析。联网范围、读取范围、模板别名和可修改范围都必须显式保留。分析任务还必须回写 `scope_lock`；“站在对方角度”等请求以 `analysis_lens=strongest_adverse_path` 记录，不能只靠模型临时记住。

案件记忆另有三个字段：

- `memory_mode=off | file_scoped | relevant | reflect`：分别表示关闭实体记忆、只看点名文件/对象、加载任务相关切片、触发有反证的跨批次联想；
- `read_memory_scope[]`：保存允许读取的具体来源、对象、争点或工单范围；
- `write_memory_mode=none | candidate | commit`：分别表示不写、只产候选、提交已验证事务。`commit` 不能替代期限核验、G1/G2/G4或任何律师批准。

`FocusContext` 保存当前案件、批次、事件、来源、争点、文书、工单、任务、模板、记忆模式和本轮刚展示的精确候选，并绑定 `state_version` 和状态快照。`ok` 只在以下条件全部满足时有效：候选唯一；`just_displayed=true`；候选所在轮次等于 `active_turn_id`；候选未失效；候选、待批准记录和当前对象的版本与哈希三方一致。

## 跨窗口版本冲突

`focus.state_version` 是规范投影版本。窗口A和窗口B即使属于同一案件，也只能提交自己读取时的版本。语义提交、短视图、期限、影响、潜在案件、工单和运行命令会核对 `--expected-state-version`：若窗口A已先提交，窗口B的旧版本写入必须拒绝。窗口B随后应重读状态、重建所需胶囊、检查输入是否失效，再形成新候选；禁止静默覆盖或自动拼接。

批次游标、反思游标和运行游标只在对应事务成功后前进。失败、中断、状态版本冲突或输出校验失败时保留原游标，以便安全重试。重复批次按 `arrival_key` 幂等，不靠文件名猜测是否相同。

## 审计日志不是第二状态源

`audit-log.jsonl` 每行是一条 JSON 事件，含变更前后状态内容哈希、上一事件哈希和本事件哈希。它只用于追责和复核。每个 active 批准都必须恰有一条 `approval_granted` 收据，收据绑定门禁、对象哈希和规范化 `scope_hash`；日志尾部 `after_state_hash` 必须等于当前状态内容哈希。崩溃、手工改写或拼接范围导致状态游标、批准收据与日志尾部不一致时，`validate` 必须报告错误，不得根据日志猜测当前状态。

这是一条完整性与可复核边界，不是带秘密密钥的数字签名。v1 能发现绕过事务循环的状态改写和普通拼接；能够同时重写状态与整条审计链的高权限攻击者不在本阶段威胁模型内。

## 兼容与迁移

旧的 `case-status.yaml` 已删除。任何发现同目录下存在旧状态文件的实现都应停止并先迁移，不能同步维护两种状态。

Schema v1.0 案件先运行：

```powershell
python scripts/legal_case_os.py upgrade-state `
  --state "案件目录\_case-state\case-state.json" `
  --expected-state-version <读取时的当前版本> --json
```

升级增加 v1.1 的增量材料、案件记忆和生命周期对象，不读取或改写原件。升级后重新 `validate`，并确认 `_case-state/memory/` 只包含本案派生索引、胶囊、工单和运行日志。

本状态可以记录未来可能授权的 `ExternalAction`，但本源码没有 G5 执行器；任何 CLI 运行产生的实际打印、发送、上传、送达或提交动作均必须为零。
