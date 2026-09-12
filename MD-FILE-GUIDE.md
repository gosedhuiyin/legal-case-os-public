# Markdown 文件用途索引

本索引解释公共发行包中每份 Markdown（以及 Markdown 模板）为什么存在。`SKILL.md` 是工位入口，`references/*.md` 是进入特定任务后才加载的细则；`shared/` 是所有工位共同遵守的契约；`tests/fixtures/` 中的文件全部是 `TEST-ONLY` 虚构材料。

## 根目录、说明与建设文件

| 文件 | 用途 |
|---|---|
| [README.md](./README.md) | 公共版首页：项目是什么、能做什么、安装入口和边界。 |
| [CHANGELOG.md](./CHANGELOG.md) | 公开发行版本记录与本次验证摘要。 |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 架构图入口及简单案、复杂争点、清洁、组卷和 G1—G5 的读图说明。 |
| [MD-FILE-GUIDE.md](./MD-FILE-GUIDE.md) | 当前索引，回答“每个 Markdown 是做什么的”。 |
| [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md) | 第三方许可、版权、修改和 clean-room 边界总说明。 |
| [docs/USER-GUIDE.md](./docs/USER-GUIDE.md) | 日常简明说明：用口语示例解释简单起草、复杂模板、缺模板与没有对应流程时如何交互。 |
| [docs/OPERATOR-GUIDE.md](./docs/OPERATOR-GUIDE.md) | 操作手册：案件工作区、口语理解、案件记忆、模板登记与填充、复杂合成、清洁、组卷和 G5 零动作。 |
| [docs/INSTALL.md](./docs/INSTALL.md) | 公共版安装说明：环境要求、安装步骤、口令映射与验收。 |
| [docs/MATERIAL-TASKS.md](./docs/MATERIAL-TASKS.md) | 不建案件状态的一次性文书任务：材料接入、知识库联动、起草、学习与保存。 |
| [automation/automation-map.md](./automation/automation-map.md) | 当前确定性 CLI 子命令的职责、状态版本要求和能力边界。 |

## 个人模板库与案例库

| 文件 | 用途 |
|---|---|
| [library/README.md](./library/README.md) | 解释模板与案例为什么分库，以及为什么二者都不能自动成为当前案事实或法源。 |
| [library/模板库/README.md](./library/模板库/README.md) | 公共版空库起步说明：目录用途、未登记时的行为、如何在本机登记自己的模板。 |

`library/模板库/00-待登记/` 与 `library/模板库/_profiles/` 由使用者在自己的机器上建立，默认被版本控制忽略；`library/模板库/_registry/template-catalog.json` 初始为空表。`library/案例库/` 保持空目录。

## 共享状态、政策、风格与目录

| 文件 | 用途 |
|---|---|
| [shared/schemas/case-state.md](./shared/schemas/case-state.md) | 解释唯一状态源、增量/工单对象、四读三写、案件内存储、跨窗口 `state_version` 和审计日志。 |
| [shared/policies/human-approval.md](./shared/policies/human-approval.md) | 定义 G1—G5、SANITIZE、精确批准对象、`ok` 条件和失效规则。 |
| [shared/policies/source-and-citation.md](./shared/policies/source-and-citation.md) | 区分权威来源、原始材料、用户陈述和模型推断，并设置正式引用门禁。 |
| [shared/policies/version-and-staleness.md](./shared/policies/version-and-staleness.md) | 规定材料、事实、证据、法源或策略变化时如何向下游传播失效。 |
| [shared/policies/evidence-disclosure.md](./shared/policies/evidence-disclosure.md) | 规定 AI 建议、律师决定、当前提交、备用、内部参考、不利/保密和送达义务。 |
| [shared/policies/network-degradation.md](./shared/policies/network-degradation.md) | 规定禁网、无公网、无会员库、无 API 或核验失败时如何降级或停止。 |
| [shared/policies/sanitization-and-originals.md](./shared/policies/sanitization-and-originals.md) | 固定“审阅—逐项批准—派生—报告—复审”，并保护原件、第三方水印和实质内容。 |
| [shared/policies/external-actions-and-scope.md](./shared/policies/external-actions-and-scope.md) | 明确本源码不执行打印、发送、登录、上传、送达或提交，G5 也没有执行器。 |
| [shared/style/personal-style-profile.md](./shared/style/personal-style-profile.md) | 通用的个人办案风格规则样例：短首报、三类稿件、来源密度、反方路径、最小修改和审阅控制页。 |
| [shared/templates/README.md](./shared/templates/README.md) | 解释模板注册、许可、哈希、字段和缺失/过期时的阻断规则。 |
| [shared/templates/matter-folder-layout.md](./shared/templates/matter-folder-layout.md) | 解释单案件目录从原件、派生、分析、工作稿、审阅、批准到候选包的分层。 |

## Skill 入口与按需细则

公共版包含 14 个技能（本地专属的固定手续技能不随包分发）：

| 工位 | 文件 | 用途 |
|---|---|---|
| 总控 | [skills/legal-case-orchestrator/SKILL.md](./skills/legal-case-orchestrator/SKILL.md) | 解析口语、读取唯一状态、每次调用一个工位、校验写回、继续或在门禁处停止。 |
| 总控 | [task-interpretation-and-clarification.md](./skills/legal-case-orchestrator/references/task-interpretation-and-clarification.md) | 定义任务框架、命令/数据分段、否定优先、范围/读取 ACL、单问门控、未知表达，以及关闭学习的边界。 |
| 总控 | [intent-and-focus.md](./skills/legal-case-orchestrator/references/intent-and-focus.md) | 定义意图封装、焦点上下文、代词、`ok`、展示长度、范围锁以及四种读/三种写模式。 |
| 总控 | [routing-rules.md](./skills/legal-case-orchestrator/references/routing-rules.md) | 区分首次入库、增量材料、生命周期工单、标准分析或争点深析。 |
| 总控 | [standard-case-flow.md](./skills/legal-case-orchestrator/references/standard-case-flow.md) | 说明初次生产、后续增量和程序工单分别如何事务式推进并停在门禁。 |
| 总控 | [case-state-contract.md](./skills/legal-case-orchestrator/references/case-state-contract.md) | 规定工位输入、结构化写回、批准快照、状态校验和审计。 |
| 共用 | [execution-recipes.md](./shared/policies/execution-recipes.md) | 把 Quick、Standard、Deep、Discuss 变成必做产物、校验器和停止条件，而不是风格形容词。 |
| 预处理 | [skills/legal-material-preprocessing/SKILL.md](./skills/legal-material-preprocessing/SKILL.md) | OCR、转写、翻译、图片转 PDF 和覆盖报告；不作法律结论或证据评价。 |
| 预处理 | [preprocessing-contract.md](./skills/legal-material-preprocessing/references/preprocessing-contract.md) | 规定原件只读、派生关系、处理范围、失败位置和人工复核项。 |
| 入库 | [skills/legal-material-intake/SKILL.md](./skills/legal-material-intake/SKILL.md) | 哈希、去重、分页、索引和材料版本身份；不决定是否提交。 |
| 入库 | [output-contract.md](./skills/legal-material-intake/references/output-contract.md) | 规定 Source/ProcessingCoverage 的字段和损坏、加密、缺页、OCR状态。 |
| 增量 | [skills/legal-matter-delta-intake/SKILL.md](./skills/legal-matter-delta-intake/SKILL.md) | 对既有案件的一批新增材料做有限记忆关联，形成事件、期限、影响、证据/工单路由或潜在新案候选。 |
| 增量 | [memory-modes-and-triage.md](./skills/legal-matter-delta-intake/references/memory-modes-and-triage.md) | 规定案件内存储、`off/file_scoped/relevant/reflect`、`none/candidate/commit` 和六类分诊结果。 |
| 增量 | [weak-signal-review.md](./skills/legal-matter-delta-intake/references/weak-signal-review.md) | 规定跨批次反思触发、互斥游标、反证和最多三个候选启发。 |
| 生命周期 | [skills/legal-case-lifecycle-manager/SKILL.md](./skills/legal-case-lifecycle-manager/SKILL.md) | 把撤诉、保全、补证、调报告等工作保存为可等待、恢复和留痕的长期工单。 |
| 生命周期 | [workflow-and-procedural-capsule.md](./skills/legal-case-lifecycle-manager/references/workflow-and-procedural-capsule.md) | 区分工单实例、任务和运行记录，规定程序胶囊、等待、恢复和完成凭证。 |
| 分析 | [skills/legal-case-analysis/SKILL.md](./skills/legal-case-analysis/SKILL.md) | 在对象已明确时分析事实、争点、举证责任、反方路径和策略。 |
| 分析 | [standard-analysis.md](./skills/legal-case-analysis/references/standard-analysis.md) | 简单案的一页案情、时间线、主要问题、风险和下一步。 |
| 分析 | [deep-analysis.md](./skills/legal-case-analysis/references/deep-analysis.md) | 单一重大争点的双方最强论证、矛盾、缺口、改变结论条件和退出标准。 |
| 法源 | [skills/legal-authority-research/SKILL.md](./skills/legal-authority-research/SKILL.md) | 双向检索并核验法条、解释、规则和案例，保留重大不利结果。 |
| 法源 | [research-method.md](./skills/legal-authority-research/references/research-method.md) | 问题树、检索—原文—比较—反思循环、停止条件和无网降级。 |
| 法源 | [research-record.md](./skills/legal-authority-research/references/research-record.md) | 规定案号、法院、日期、效力、原文位置、支持/不利和生产可用状态。 |
| 模板 | [skills/legal-template-curator/SKILL.md](./skills/legal-template-curator/SKILL.md) | 只读蒸馏、登记和解析个人模板，生成逐格填充计划或多模板合成规范；画像未经批准不自动激活。 |
| 模板 | [profile-contracts.md](./skills/legal-template-curator/references/profile-contracts.md) | 定义 FormProfile、WritingProfile、FillPlan、CompositionSpec、ClaimBinding 和三种 usage mode 的准入/版本规则。 |
| 模板 | [form-fidelity-and-fill-rules.md](./skills/legal-template-curator/references/form-fidelity-and-fill-rules.md) | 规定八类格子、律师决策字段、旧格式转换、OOXML结构保护和逐页视觉QA。 |
| 模板 | [complex-composition.md](./skills/legal-template-curator/references/complex-composition.md) | 规定版式/结构主模板、最多三份分段辅助范例、逐命题法源绑定、优先级和无网停止。 |
| 范例 | [skills/legal-style-exemplar-retrieval/SKILL.md](./skills/legal-style-exemplar-retrieval/SKILL.md) | 只从律师认可旧稿提取结构、语气和段落功能，禁止迁移旧案事实。 |
| 范例 | [exemplar-record.md](./skills/legal-style-exemplar-retrieval/references/exemplar-record.md) | 规定范例身份、许可、版本、用途、可复用写法和禁止迁移内容。 |
| 证据 | [skills/legal-evidence-gate/SKILL.md](./skills/legal-evidence-gate/SKILL.md) | 分离来源总库、AI候选和律师批准池，控制当前提交与备用。 |
| 证据 | [evidence-decision-model.md](./skills/legal-evidence-gate/references/evidence-decision-model.md) | 规定证明目的、三性、证明力、不利内容、对方利用、时机、替代和披露字段。 |
| 起草 | [skills/legal-document-drafting/SKILL.md](./skills/legal-document-drafting/SKILL.md) | 按已批策略、证据、已核验法源、模板和个人风格生成或局部修改文书。 |
| 起草 | [drafting-contract.md](./skills/legal-document-drafting/references/drafting-contract.md) | 规定 G1/G2、法源、争点、来源映射、稿件受众和输出前置条件。 |
| 起草 | [template-and-artifact.md](./skills/legal-document-drafting/references/template-and-artifact.md) | 规定模板许可/哈希/字段、Artifact版本和缺模板时的停止方式。 |
| 审阅 | [skills/legal-document-review/SKILL.md](./skills/legal-document-review/SKILL.md) | 独立审查事实、法源、证据、自认、数字、逻辑、版本和隐藏残留。 |
| 审阅 | [review-contract.md](./skills/legal-document-review/references/review-contract.md) | 规定阻断项、一页控制页、最值得人工阅读的五处和通过条件。 |
| 审阅 | [sanitization-review.md](./skills/legal-document-review/references/sanitization-review.md) | 规定清洁前后双审、逐项目标、权属、源哈希和再审门禁。 |
| 压缩 | [skills/legal-draft-compressor/SKILL.md](./skills/legal-draft-compressor/SKILL.md) | 在明确篇幅下删重复、并表达，不新增事实或改变策略/证据强度。 |
| 压缩 | [compression-contract.md](./skills/legal-draft-compressor/references/compression-contract.md) | 规定受保护命题、前后长度、删改清单、风险变化和复审条件。 |
| 组卷 | [skills/legal-filing-packager/SKILL.md](./skills/legal-filing-packager/SKILL.md) | 只用合格文书、当前 G2 证据和已核手续生成候选包、书签和打印单。 |
| 组卷 | [filing-package-contract.md](./skills/legal-filing-packager/references/filing-package-contract.md) | 规定输入资格、清洁复审、清单哈希、G4候选和G5边界。 |

## Markdown 模板（`.md.tmpl`）

下列文件是本项目 clean-room 编写的字段骨架；真正可用性由 `template-registry.json` 的许可、版本、SHA-256、必填字段和别名控制，不能把文件存在等同于法院现行格式。

| 文件 | 用途 |
|---|---|
| [civil-complaint.md.tmpl](./shared/templates/forms/civil-complaint.md.tmpl) | 民事起诉状。 |
| [civil-defense.md.tmpl](./shared/templates/forms/civil-defense.md.tmpl) | 民事答辩状。 |
| [civil-appeal.md.tmpl](./shared/templates/forms/civil-appeal.md.tmpl) | 民事上诉状。 |
| [agency-opinion.md.tmpl](./shared/templates/forms/agency-opinion.md.tmpl) | 代理意见。 |
| [cross-examination-opinion.md.tmpl](./shared/templates/forms/cross-examination-opinion.md.tmpl) | 质证意见。 |
| [evidence-catalog.md.tmpl](./shared/templates/forms/evidence-catalog.md.tmpl) | 证据目录。 |
| [authority-research-report.md.tmpl](./shared/templates/forms/authority-research-report.md.tmpl) | 案例/法源研究报告。 |
| [power-of-attorney.md.tmpl](./shared/templates/forms/power-of-attorney.md.tmpl) | 授权委托书。 |
| [law-firm-letter.md.tmpl](./shared/templates/forms/law-firm-letter.md.tmpl) | 律师事务所函。 |
| [legal-representative-certificate.md.tmpl](./shared/templates/forms/legal-representative-certificate.md.tmpl) | 法定代表人身份证明。 |
| [service-information.md.tmpl](./shared/templates/forms/service-information.md.tmpl) | 送达信息。 |
| [print-production-sheet.md.tmpl](./shared/templates/forms/print-production-sheet.md.tmpl) | 打印生产单；不触发打印。 |
| [filing-package-checklist.md.tmpl](./shared/templates/forms/filing-package-checklist.md.tmpl) | 提交包候选清单；不触发上传或提交。 |

## 单案件工作区模板

| 文件 | 用途 |
|---|---|
| [matter-workspace-template/_case-state/README.md](./matter-workspace-template/_case-state/README.md) | 说明 `case-state.json` 唯一状态、`memory/` 派生目录、四读三写、跨窗口版本冲突和升级/恢复。 |
| [matter-workspace-template/00-originals/ORIGINALS-ARE-READ-ONLY.md](./matter-workspace-template/00-originals/ORIGINALS-ARE-READ-ONLY.md) | 明示原件禁止覆盖、清洁、合并、遮盖或改名。 |

## 测试说明、矩阵与报告

| 文件 | 用途 |
|---|---|
| [tests/README.md](./tests/README.md) | 测试集入口、TEST-ONLY 边界、目录说明和运行方式。 |
| [tests/TEST-MATRIX.md](./tests/TEST-MATRIX.md) | 把用户口语、门禁、复杂争点、模板、失效、清洁和组卷映射到测试编号。 |
| [tests/acceptance/acceptance-criteria.md](./tests/acceptance/acceptance-criteria.md) | 可机器核验的通过标准，包括未批准证据、未核验法源、原件哈希和G5为零。 |
| [tests/acceptance/routing-cases.md](./tests/acceptance/routing-cases.md) | 常用口语到工位/动作/停止条件的人工可读对照。 |
| [tests/fixtures/document-cleaning/README.md](./tests/fixtures/document-cleaning/README.md) | 两份虚构 DOCX 的残留项、允许清洁项和必须阻断项说明。 |
| [tests/artifacts/README.md](./tests/artifacts/README.md) | 说明当前只保留验收器直接依赖的固定 PDF 输入；历史生成样件和逐页渲染图可按需重建。 |

## TEST-ONLY 虚构案与模拟法源 Markdown

| 文件 | 用途 |
|---|---|
| [模板学习夹具说明](./tests/fixtures/template-curation-v12/README.md) | 说明FormProfile、FillPlan、八类字段及旧案串入阻断的TEST-ONLY夹具。 |
| [增量案-普通邮件](./tests/fixtures/incremental-case/materials/TEST-ONLY-01-ordinary-email.md) | 不应触发重大深析的普通增量材料。 |
| [增量案-法院通知](./tests/fixtures/incremental-case/materials/TEST-ONLY-02-court-notice.md) | 测试程序事件、期限与后续工单的虚构通知。 |
| [增量案-客户坚持](./tests/fixtures/incremental-case/materials/TEST-ONLY-03-client-insists.md) | 测试客户指令、风险记录和律师决定边界。 |
| [增量案-弱信号1](./tests/fixtures/incremental-case/materials/TEST-ONLY-04-weak-signal-1.md) | 单独不足以另案升级的弱信号。 |
| [增量案-弱信号2](./tests/fixtures/incremental-case/materials/TEST-ONLY-05-weak-signal-2.md) | 与其他材料合并后才可形成候选启发的弱信号。 |
| [增量案-弱信号3](./tests/fixtures/incremental-case/materials/TEST-ONLY-06-weak-signal-3.md) | 测试多批次弱信号聚合但不自动立案。 |
| [增量案-影响性补充](./tests/fixtures/incremental-case/materials/TEST-ONLY-07-impacting-supplement.md) | 测试局部失效传播和受影响成果重算。 |
| [增量案-程序请求](./tests/fixtures/incremental-case/materials/TEST-ONLY-08-procedural-request.md) | 测试可等待、失败恢复和完成凭证的程序工单。 |
| [简单案-买卖合同](./tests/fixtures/simple-case/00-originals/TEST-ONLY-01-买卖合同.md) | 虚构合同原始材料。 |
| [简单案-交付验收单](./tests/fixtures/simple-case/00-originals/TEST-ONLY-02-交付验收单.md) | 虚构交付原始材料。 |
| [简单案-付款记录](./tests/fixtures/simple-case/00-originals/TEST-ONLY-03-付款记录.md) | 虚构付款原始材料。 |
| [简单案-催款通信](./tests/fixtures/simple-case/00-originals/TEST-ONLY-04-催款通信.md) | 虚构催款原始材料。 |
| [简单案-案情摘要](./tests/fixtures/simple-case/20-analysis/TEST-ONLY-案情摘要.md) | 标准轨的短分析期望。 |
| [简单案-起诉状候选](./tests/fixtures/simple-case/40-review/TEST-ONLY-起诉状候选.md) | 已审法院候选的文本夹具。 |
| [简单案-证据目录候选](./tests/fixtures/simple-case/40-review/TEST-ONLY-证据目录候选.md) | 只列 G2 当前证据的目录夹具。 |
| [简单案-G4清单](./tests/fixtures/simple-case/60-filing/TEST-ONLY-G4候选清单.md) | G4 候选且 G5 动作为零的包清单夹具。 |
| [复杂案-框架协议](./tests/fixtures/complex-case/00-originals/TEST-ONLY-01-框架协议.md) | 虚构日期一及合同框架材料。 |
| [复杂案-仓储登记](./tests/fixtures/complex-case/00-originals/TEST-ONLY-02-仓储登记.md) | 虚构日期二及履行材料。 |
| [复杂案-双向通信](./tests/fixtures/complex-case/00-originals/TEST-ONLY-03-双向通信.md) | 同时有利和重大不利的通信材料。 |
| [复杂案-催告记录](./tests/fixtures/complex-case/00-originals/TEST-ONLY-04-催告记录.md) | 虚构日期三及程序风险材料。 |
| [复杂案-深析范围](./tests/fixtures/complex-case/20-analysis/TEST-ONLY-深析范围.md) | 只允许时效争点进入深析的范围锁。 |
| [模拟支持法源](./tests/fixtures/authority-corpus/TEST-ONLY-SUPPORT-001.md) | 可读但仍禁止生产引用的支持性模拟法源。 |
| [模拟不利法源](./tests/fixtures/authority-corpus/TEST-ONLY-ADVERSE-001.md) | 必须保留的重大不利模拟法源。 |
| [模板-存在](./tests/fixtures/template-cases/templates/TEST-ONLY-起诉状模板.md) | 模板存在且字段齐全情形。 |
| [模板-许可不明](./tests/fixtures/template-cases/templates/TEST-ONLY-许可不明模板.md) | 许可不明必须阻断情形。 |
| [模板-过期](./tests/fixtures/template-cases/templates/TEST-ONLY-过期模板.md) | 版本过期必须阻断情形。 |
| [个人模板参照](./tests/fixtures/template-reference-cases/templates/TEST-ONLY-simple-reference.md) | 虚构的个人标准文书参照，用于编号、别名、哈希和套件解析测试；不含真实旧案事实。 |

机器契约的 JSON、Schema、`agents/openai.yaml`、许可证文本和 Python 脚本不属于 Markdown；它们分别由根目录 [README.md](./README.md)、[automation/automation-map.md](./automation/automation-map.md) 和 [shared/schemas/case-state.md](./shared/schemas/case-state.md) 说明。
