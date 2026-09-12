# 确定性自动化地图

统一入口为 `scripts/legal_case_os.py`；`scripts/legal_case_cli.py` 是兼容包装。当前 `--help` 共列出 39 个子命令。所有脚本默认离线，只读原件，不执行打印、发送、登录、上传、送达或提交；所有返回中的实际 G5 外部动作数量必须为零。

| 子命令 | 确定性职责 | 明确边界 |
|---|---|---|
| `init` | 建立案件目录、唯一状态和首条审计事件 | 不读取真实案情，不安装配置 |
| `upgrade-state` | 在给定 `expected-state-version` 后把 v1.0 或 v1.1 规范状态迁移为 v1.2，并创建案件内记忆、模板合成集合和焦点字段 | 不触碰 `00-originals`，不替代升级前备份，不合并并发版本；重复迁移保持幂等 |
| `index` | 文件哈希、去重、页数、OCR状态、派生索引 | 不评价证据，不写原件 |
| `ingest-batch` | 只扫描案件工作区内点名的新增材料目录，按内容生成幂等 `MaterialBatch` 和收件 `CaseEvent`，合并而不删除旧来源 | 不持续扫盘，不把来源升级为证据，不把本批之外来源标成删除 |
| `context-build` | 按 `off/file_scoped/relevant/reflect` 和精确范围生成有限 `ContextCapsule`，绑定当前规范投影版本；可写入本案 `_case-state/memory/` | 不改规范状态；窄范围不能支持完整结论时如实标明，不静默扩张；不得把审计序号冒充状态版本 |
| `memory-view` | 在版本匹配后重建本案短 `CurrentCaseView` 并登记派生 Artifact | 不是第二真值源，不复制聊天或原件，不写到案件目录外 |
| `triage-commit` | `candidate` 只校验并返回来源绑定的 `MemoryDeltaCandidate`，不改状态/审计/游标；`commit` 才原子写入合格的分诊、事件、期限、影响和潜在案件记录 | 不把候选直接变成事实、证据、策略、已核期限或新案件 |
| `deadline-verify` | 根据原文、时区和计算依据核验或驳回一个 `DeadlineRecord` | 不从AI抽取直接确认期限，不删除被驳回或被取代的历史 |
| `impact-apply` | 批准、免审或驳回一个有明确受影响对象的 `ImpactAssessment`，只传播声明的失效 | 不重跑全案，不让模型推断自行改写批准对象 |
| `seed-promote` | 记录律师对 `ProspectiveMatterSeed` 的批准或驳回，并保留来源案/事件链接 | 不自动创建新案件目录，不把弱信号冒充已定策略 |
| `workflow-start` | 创建撤诉、保全、调报告、补证等持久 `WorkflowInstance` | 不以聊天会话或一次文书生成代替业务工单 |
| `workflow-update` | 让工单等待、恢复、完成或取消，并绑定等待条件、输出和完成事件 | 没有完成凭证时不宣布完成，不执行对外动作 |
| `run-start` | 在工单下登记一次有输入快照、任务和日志位置的 `RunAttempt` | 运行状态不是业务状态，日志必须留在本案记忆目录 |
| `run-finish` | 结束、失败、中止或丢弃一次运行，并分别记录候选/已提交结果和游标 | 失败或中断运行不得完成工单；重试不得覆盖旧运行历史 |
| `reconcile-matter` | 只读检查原始来源哈希、批次/事件游标、当前规范投影版本、当前视图、候选期限、工单和中断运行 | 不自动修复或改状态，不把缺失解释为已完成；报告版本不得退回审计事件序号 |
| `validate` | JSON Schema、语义门禁、审计哈希链/末状态、内置模板注册表及个人模板目录校验；每个 active 批准必须有唯一且对象哈希、`scope_hash` 相符的 `approval_granted` 收据 | 不自动修复；审计不是第二状态源 |
| `route` | 把常用口语解析为意图、工位、记忆读写模式和停止条件；可用 `--capabilities` 离线评估公网、会员库、API和OCR缺口 | 关键词路由不是法律判断；能力评估不执行外部调用 |
| `approve-ok` | 校验唯一、刚展示、未失效且三方版本/哈希一致的批准，并追加绑定对象哈希与 `scope_hash` 的批准收据 | 不接受含糊、跨阶段或范围扩大授权 |
| `invalidate` | 来源或对象变化后的显式下游失效传播 | 不删除旧成果 |
| `preflight` | DOCX/PDF批注、修订、隐藏文字、内部ID、占位符、元数据、水印候选检查 | PDF能力不足时列出缺口 |
| `sanitize-docx` | 逐目标核验部件哈希、权属、授权依据和数量/精确值；有状态运行还复核完整状态语义、三对象范围及追加式批准收据，再生成DOCX派生副本和清洁报告 | 阻止第三方证据、实质内容、隐藏文字、占位符及无审计收据的有状态清洁 |
| `min-diff` | 检查修改比例及授权行范围 | 不判断修改内容的法律正确性 |
| `template-fill` | 验证模板许可/哈希/字段并生成MD/TXT/真实DOCX；本地LibreOffice可用时生成真PDF | 后端缺失/失败时拒绝，绝不伪造；DOCX/PDF均需视觉QA |
| `template-distill` | 只读解析一个DOCX并生成draft `FormProfile` 或 `WritingProfile` | 不批准、不登记、不激活，不复制旧案正文到画像 |
| `template-register` | 在律师显式批准后 create/upgrade/retire 版本化个人模板和画像，并校验目录 | 不覆盖旧版；retire不删除文件；无视觉基线或未决画像时拒绝 |
| `template-fill-plan` | 从active FormProfile、案件状态和精确来源生成逐格拟填值、留白理由及一次性风险确认单 | 不接受模型猜测替代来源；收费、金额、审级和权限必须律师决定 |
| `template-fill-docx` | 复制原DOCX并只修改已批准稳定文字节点，执行包结构差异和预检 | 原件不变；输出仍待逐页视觉QA；结构增行/正文块改由专用TEST-ONLY命令验证 |
| `template-repeat-plan` | 为虚构FormProfile建立受登记标准行的0/1/多行克隆计划 | 仅`--test-mode`；真实画像、法院候选和提交资格均阻断 |
| `template-hybrid-plan` | 为虚构WritingProfile建立机械字段与整块正文替换计划 | 仅`--test-mode`；只接收结构化纯文本段落，不接受OOXML/Markdown注入 |
| `template-structural-apply` | 按精确源/子树/锚点哈希执行标准行或整块正文结构变换 | 仅`--test-mode`；危险关系对象、纵向合并、旧案残留或结构漂移即停止 |
| `composition-build` | 冻结文书目标、G1/G2快照、主辅模板资格、危险争点、来源/法源和章节级ClaimBinding | 不起草、不联网、不把未批准模板或TEST-ONLY法源升级为生产来源 |
| `composition-validate` | 对CompositionSpec执行模板资格、精确定位、法源、重大不利结果、章节绑定和失效门禁 | 网络/法源不足时阻断正式候选，不伪装完成 |
| `citation-audit` | 扫描文书中的案号、法条和引文并与已核验目录逐项对账 | 未登记、仅摘要、缩写无法唯一解析或内容不一致时阻断 |
| `exemplar-leak-check` | 用旧案特征文本哈希清单检查新稿串案 | 不保存旧案正文；空指纹清单不能作为已检查通过 |
| `template-ref-validate` | 校验独立个人模板目录的ID/别名、授权、版本、哈希、禁止迁移范围和套件组件 | 不登记新模板，不读取全库，不生成文书 |
| `template-ref-resolve` | 按编号、名称或别名唯一解析一个个人参考模板或套件，并列出精确路径/组件 | 只选择，不复制旧案事实，不生成、不提交 |
| `name` | 生成Windows安全、带案件/类型/版本的文件名 | 不重命名现有文件 |
| `bundle` | 自行复核完整状态语义与递归来源，按清单合并真实PDF、写书签，并可叠加/文本核验逐页 `n / total` 页码 | 缺后端或页码能力时结构化降级；需另做逐页视觉QA |
| `print-sheet` | 复用包资格校验后生成打印生产清单 | 不触发打印 |

## 版本与窗口边界

语义提交、短视图、期限、影响、潜在案件、工单、运行和持久CompositionSpec等 v1.2 写命令要求 `--expected-state-version`。每个聊天窗口只能提交其读取快照对应的版本；不一致时命令停止，调用方必须重读 `case-state.json`、重建胶囊并重新核对差异，不能采用“最后写入者胜出”。`ingest-batch` 另以内容 `arrival_key` 保证同批重复调用幂等。

`context-build` 的四种读取模式与 `IntentEnvelope.memory_mode` 一致；`triage-commit` 的 `candidate/commit` 对应写入候选和正式事务，`none` 表示不调用任何语义写入命令。`commit` 仍不具有律师批准或 G5 权限。

法律分析、证据决定、清洁批准和最终批准继续由专业Skill与律师完成。脚本只执行可复核机械动作；本项目没有 `print/send/upload/serve/file` 执行子命令。
