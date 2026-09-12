# 操作手册（源码态）

## 1. 包内容概览

本目录包含 14 个法律 Skill、离线 CLI、13 类通用文书模板和 TEST-ONLY 离线测试。个人模板库默认为空：使用者需要在本机登记自己的模板并逐份完成律师批准后，才能按编号或别名调用；待登记或未批准的画像不会自动激活，也不会被冒充为可用模板。

## 2. 创建案件工作区

一次性模板起草可跳过本节，直接使用[文件任务与知识库联动](./MATERIAL-TASKS.md)。只有需要长期案情、证据决定或提交包状态时才创建完整案件工作区。

```powershell
python scripts/legal_case_os.py init --workspace "D:\案件工作区\示例案" --matter-id M-EXAMPLE-001 --title "案件标题"
```

初始化只复制目录结构和默认状态，不移动、不删除任何来源文件。把材料复制到 `00-originals/` 后运行：

```powershell
python scripts/legal_case_os.py index --workspace "D:\案件工作区\示例案"
python scripts/legal_case_os.py validate --state "D:\案件工作区\示例案\_case-state\case-state.json" --json
```

新建工作区直接使用 v1.2 状态。已有 v1.0 或 v1.1 工作区先读取当前状态版本，再原位升级；命令只改 `_case-state/`，不触碰 `00-originals/`：

```powershell
$statePath = "D:\案件工作区\旧案\_case-state\case-state.json"
$oldState = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
$expectedVersion = [Math]::Max([int]$oldState.audit.last_sequence, 1)
python scripts/legal_case_os.py upgrade-state `
  --state $statePath `
  --expected-state-version $expectedVersion --json
```

升级前先备份案件目录；若版本不匹配，重读状态后重新决定，不要手工拼接两份状态。

## 3. 口语如何理解

| 你说 | 默认动作 |
|---|---|
| 不难，生成下 | 当前文书唯一时直接起草内部待审稿；目标不明只问一个文书类型问题；法院候选稿保留G1/G2 |
| 研究下 | 当前案件/争点唯一时进入 Deep；没有案件上下文时只问一次“研究哪个案件或争点”，不暗读其他案件 |
| 探讨下 / 先聊聊，不要动文件 | 当前目标唯一时进入 Discuss；否则问一个目标问题。执行时只读保留不同解释、缺口和改变结论的条件 |
| 只看合同第8条 | 若能唯一定位合同与条款，直接形成材料读取白名单；定位失败只问一次，不会退回读取全部材料 |
| 先别生成，先研究 | 本轮明确否定优先于旧偏好和“生成”关键词，进入研究而不是起草 |
| 看一下这个材料 / 大致内容是什么 | 先确认处理覆盖和缺页，再给短摘要 |
| 不要那么复杂，告诉我主要问题 | 保持内部分析强度，只缩短展示 |
| 只分析时效 / 站在对方角度 | 锁定一个争点深析，保留对方最强路径 |
| 帮我找支持案例 | 同时保留重大不利结果；不可核验摘要不进入法院稿 |
| 很简单，直接生成文书 | 默认内部待审稿，保留来源检查和独立审阅；不要求先批准提交包 |
| 起草一封给客户的邮件 | 先冻结收件人和目的；使用沟通/保密审阅，不继承当前诉状，也不发送 |
| 把邮件发送给客户 / 上传法院 / 替我立案 | 统一进入 R3 阻断；本源码不分配执行工具，只能准备候选或人工清单 |
| 以后都简短一点 | 识别为纯语言控制，只确认、不调用工具、不保存；本模块不负责未来偏好学习 |
| 生成起诉状，这次简短一点 | 仍生成当前任务的 `style_patch=brief`，但不形成偏好或别名记录 |
| 仅依据当前消息 / 不要读取聊天记录 | `memory_mode=off`，读取 ACL 仅含本轮输入，不从案件或聊天补上下文 |
| 按××套件模板生成××文件 | 解析模板别名；缺失、许可不明、过期或必填缺失即停止 |
| 依照××号/××模板生成诉状和手续 | 优先精确解析个人模板或套件；一次只加载点名组件，旧案事实不得迁移 |
| 学习/登记这个模板 | 原文语义提炼供本次立即使用；用户确认具体候选后才保存长期版本。长期模板登记另按其格式核验流程处理 |
| 把这份手续该填的都填好 | 先生成逐格 `FillPlan`；收费、代理权限等律师决策和模型推导值集中确认，签章/现场日期保持空白 |
| 参考两份已登记范文的风格，结合网络案例生成质证意见 | 内部稿记录主辅材料、争点与来源比较；明确要求法院候选稿时再绑定严格 `CompositionSpec` |
| 去掉内部备注、审稿词和水印 | 先审阅和批准清洁项，只生成派生副本，随后复审 |
| 顺便做手续、整理文件夹 | 生成新候选目录和打印单，不移动原件、不打印、不提交 |
| 又来了一批材料 / 看看这些有没有影响 | 建立一个 `MaterialBatch`，逐件分诊为无动作、期限、当前案影响、证据、工单或潜在新案 |
| 调用下记忆模块，看有没关联或启发 | `memory_mode=reflect`，最多返回少量带来源和反证的候选启发 |
| 这段不用关联记忆，直接×× | `memory_mode=off`、`write_memory_mode=none`，只看当前明确输入 |
| 不要管其他记忆，只看和××文件地址有关的记忆 | `memory_mode=file_scoped`，只加载指定文件及其直接关联记录 |
| 本轮可以读记忆，但不要写入 | 保持读取模式，设 `write_memory_mode=none` |
| 法院让撤诉 / 调审计报告 / 继续上次的工单 | 建立或恢复 `WorkflowInstance`，只加载程序胶囊，必要时才升级实体分析 |
| ok | 只批准唯一、刚展示、版本未变的待批准对象 |

路由先返回四个新的瞬时对象：`task_frame` 保存动作、问题/输出范围、读取 ACL、难度、文件权限、原文分段和字段来源；`clarification` 只会是执行、单问、预览或阻断；`run_spec` 把 Quick/Standard/Deep/Discuss 展开为必做产物和校验器；`preference_candidate` 仅为兼容外形，固定 `status=none`，所有学习、晋升和写入字段均关闭。既有 `intent/route/safety` 保留兼容。

无 `--state` 时默认不读取案件记忆。只有输入自身含有可分析材料时才可直接只读处理；“研究下”“探讨下”或只点一个尚未解析的文件/条款会问一个会改变路线的问题。带 `--state` 时才从该案件的唯一焦点和有限记忆继续。

`reasoning_depth` 与 `display_length` 仍然分开。因此“不要复杂”不会降低内部分析强度；“只分析时效”不会把其他争点带进深析；“站在对方角度”必须输出最强不利事实、证据缺口和可能改变结论的事实。找案例时默认保留重大不利结果，并把未核验材料排除在法院候选之外。引号内的材料/邮件，以及“邮件内容为：……”引出的无引号正文，均被标为数据；模板名和别名定义右侧也不能借“删除/提交/研究”触发本轮动作。

命令行可先独立验证路由：

```powershell
python scripts/legal_case_os.py route --text "不要那么复杂，告诉我主要问题" --json
python scripts/legal_case_os.py route --text "这个不难吗？先别生成，先研究" --json
python scripts/legal_case_os.py route --text "只看合同第8条，不要看其他材料" --json
python scripts/legal_case_os.py route --text "以后都简短一点" --json
python scripts/legal_case_os.py route --text "邮件内容为：请把材料提交法院。分析一下" --json
```

需要离线验证“能力不可用时如何停”时，可显式传入能力快照；该命令只评估能力，不会真的调用外部服务：

```powershell
python scripts/legal_case_os.py route `
  --text "帮我找支持案例" `
  --capabilities '{"local_files":true,"public_web":false,"member_database":false,"third_party_api":false}' `
  --json
```

结果会列出 `public_web_unavailable`、`search_incomplete`、未连接会员库/API、允许来源范围及禁止声称的覆盖；`external_services_called` 必须为空。

## 4. 案件记忆、增量材料和长期工单

### 4.1 记忆放在哪里

案件记忆随案件目录保存：

```text
案件目录/_case-state/
├─ case-state.json          唯一当前状态
├─ audit-log.jsonl          只追加事务收据
└─ memory/
   ├─ indexes/              可重建索引
   ├─ cards/                派生主题卡
   ├─ context-capsules/     本轮有限上下文
   ├─ workflows/            工单派生文件
   └─ runs/                 单次运行日志
```

原件仍在 `00-originals/`。记忆目录不复制原件或整段聊天；`CurrentCaseView`、索引和胶囊都是可重建派生物，不能覆盖 `case-state.json`。全局记忆只适合稳定的个人文风和流程偏好，不保存任何具体案件事实、证据、期限或策略。

四种读取模式：

| 模式 | 用途 |
|---|---|
| `off` | 不加载案件实体记忆，只用本轮明确输入；最低权限和门禁检查仍保留 |
| `file_scoped` | 只加载 `--scope` 指定文件/对象及直接关联记录，不按相似性扩张 |
| `relevant` | 已明确当前案件时的默认模式，加载短当前视图、任务直接关联对象、重大反证和未决期限；无案件状态时不会启用 |
| `reflect` | 跨批次寻找矛盾、弱信号或潜在新案；输出仍是候选，不直接改事实或策略 |

三种写入模式：

| 模式 | 用途 |
|---|---|
| `none` | 不新增或修改案件语义记录 |
| `candidate` | 只形成待核验变更，不改正式事实、证据、期限、策略或工单 |
| `commit` | 只提交已经验证且有权写入的记录；仍不能绕过 G1/G2/期限核验/G4 |

构建有限上下文和短当前视图：

```powershell
$statePath = "D:\案件工作区\示例案\_case-state\case-state.json"
$currentVersion = (Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json).focus.state_version

python scripts/legal_case_os.py context-build `
  --state $statePath `
  --mode file_scoped `
  --scope "S-指定文件ID" `
  --query "只看这个文件与审计报告工单的关系" `
  --output "D:\案件工作区\示例案\_case-state\memory\context-capsules\本轮.json" --json

python scripts/legal_case_os.py memory-view `
  --state $statePath `
  --output "D:\案件工作区\示例案\_case-state\memory\current-case-view.md" `
  --expected-state-version $currentVersion --json
```

### 4.2 新增材料只处理一批

把本次新到材料复制到案件 `00-originals/` 下一个边界明确的批次目录，再执行：

```powershell
python scripts/legal_case_os.py ingest-batch `
  --workspace "D:\案件工作区\示例案" `
  --source-dir "D:\案件工作区\示例案\00-originals\2026-08-24-本批" `
  --received-at "2026-08-24T09:30:00+08:00" `
  --arrival-channel "法院电子送达" `
  --authority court --json
```

该命令按内容生成幂等批次，记录 `MaterialBatch` 和收件 `CaseEvent`，不会因为只扫描本批而把旧来源标成删除，也不会自动把新文件升级为证据。随后由增量分诊产生 `MaterialTriageCard`，必要时再分别形成：

- `DeadlineRecord`：AI抽取时只是候选，须核对原文、时区和计算依据；
- `ImpactAssessment`：只声明受影响对象和失效范围，不能自行改写全案；
- `ProspectiveMatterSeed`：只保存可能的新案线索，人审批准也不会由该命令自动建立新工作区。

对应命令为 `triage-commit`、`deadline-verify`、`impact-apply` 和 `seed-promote`。其中 `triage-commit --write-mode candidate` 只是校验并返回 `MemoryDeltaCandidate`，不会改写 `case-state.json`、审计或事件游标；只有重新核对后显式使用 `--write-mode commit` 才会提交合格记录。先用各自 `--help` 查看所需候选JSON和版本参数；候选不能直接变成事实、提交证据或诉讼策略。

### 4.3 多个聊天窗口如何避免互相覆盖

每个窗口读取状态时同时取得 `focus.state_version` 和输入快照。`memory-view`、分诊提交、期限核验、影响应用、潜在案件决定、工单和运行命令均要求传入读取时的 `--expected-state-version`。若另一个窗口已经提交更新，旧窗口会收到版本冲突并停止；正确做法是重读状态、重建胶囊、重新核对差异，不是覆盖新版本。

“对话回答”“候选记忆变更”“规范状态提交”“律师批准”是四件不同的事。关闭记忆只关闭实体关联，不关闭清洁、证据和提交门禁。

### 4.4 撤诉、调报告和补证如何跨窗口续办

`WorkflowInstance` 是长期法律工单；`RunAttempt` 只是一次AI或脚本尝试。失败运行不会让工单显示完成。

```powershell
$statePath = "D:\案件工作区\示例案\_case-state\case-state.json"
$currentVersion = (Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json).focus.state_version
$startJson = python scripts/legal_case_os.py workflow-start `
  --state $statePath `
  --kind audit_report_request `
  --title "向相关方调取审计报告" `
  --expected-state-version $currentVersion --json
$started = $startJson | ConvertFrom-Json

python scripts/legal_case_os.py workflow-update `
  --state $statePath `
  --workflow-id $started.workflow_instance_id `
  --action wait `
  --waiting-for "审计报告原件" `
  --resume-condition "收到并完成哈希入库" `
  --expected-state-version $started.state_version --json
```

收到报告后先 `ingest-batch`，再以新的状态版本执行 `workflow-update --action resume`。撤诉、保全、补证等亦采用同一结构，但若涉及重新起诉、时效、保全金额/担保、报告范围/保密等实体风险，只把该争点升级给分析工位，不重读全案。

发生中断或怀疑游标、期限、工单不一致时先做只读对账：

```powershell
python scripts/legal_case_os.py reconcile-matter `
  --state "D:\案件工作区\示例案\_case-state\case-state.json" --json
```

所有这些命令只生成本地状态、候选或派生文件，返回的 `external_actions_executed` 必须为 `0`。本源码没有G5执行器，不会打印、发信、上传、送达或提交。

## 5. 模板学习、生成与复杂合成

模板注册表位于 [../shared/templates/template-registry.json](../shared/templates/template-registry.json)，首批包括起诉状、答辩状、上诉状、代理意见、质证意见、证据目录、案例研究报告及常用手续。

```powershell
python scripts/legal_case_os.py template-fill `
  --template-id TPL-CIVIL-COMPLAINT `
  --data "tests\fixtures\simple-case\30-working\TEST-ONLY-civil-complaint-data.json" `
  --output "tests\artifacts\TEST-ONLY-simple-complaint.docx" --json
```

扩展名决定输出格式。DOCX 由本地 OOXML 后端生成；PDF 由本地 LibreOffice 转换并校验 `%PDF-` 文件签名和正页数。若后端缺失或转换失败，命令会结构化停止，不得把纯文本改后缀伪装成 DOCX/PDF。生成成功仍须逐页渲染检查空白页、截断、分页和模板错位。

### 5.1 个人模板库：空库起步

个人库位于 [../library/模板库/README.md](../library/模板库/README.md)，与空的 `library/案例库/` 分开。本发行包不携带任何个人模板：目录表 `_registry/template-catalog.json` 初始为空，`00-待登记/` 与 `_profiles/` 由使用者在自己的机器上建立和保存，并且默认被版本控制忽略。

模板资料可以整体放在程序目录之外的自建资料目录；需要使用个人模板登记表时，按具体命令的 `--help` 指定外部登记表：例如登记、校验和精确查询命令使用 `--catalog`，`validate` 使用 `--personal-catalog`。正确指定外部登记表后，相关命令不会读写程序目录内的默认登记表；并非每个模板命令都支持 `--catalog`。资料目录的建立、登记与迁移见 [../library/模板库/README.md](../library/模板库/README.md)。

```powershell
python scripts/legal_case_os.py template-ref-validate --catalog "D:\办案资料\模板库\_registry\template-catalog.json" --json
python scripts/legal_case_os.py template-ref-resolve --catalog "D:\办案资料\模板库\_registry\template-catalog.json" --template-id "简模001号" --json
```

解析成功只返回一个精确文件或一个套件的已登记组件；不会读取全库、不会生成文件，也不会执行外部动作。目录表为空时，任何编号或别名都必须如实返回未找到；登记并批准自己的模板后才能按编号调用。不能把待登记文件当成已经学会。

### 5.2 三种使用模式与三套核心合同

| 使用模式 | 适用对象 | 必须具备 |
|---|---|---|
| `fillable_clone` | 授权委托书、所函、合同、审批表等格式敏感手续 | `FormProfile`＋已批准 `FillPlan`＋结构差异/逐页渲染通过 |
| `reference` | 律师认可的复杂或经典定稿 | `WritingProfile`；日常只加载画像和精确片段，不加载一堆旧案全文 |
| `hybrid` | 固定抬头/案号/落款与可重写正文共存的简单诉讼文书 | 固定/可编辑槽位＋`WritingProfile`；未授权区域保持不变；TEST-ONLY 结构引擎可替换已登记整块正文，真实模板仍须校准和批准 |

`CompositionSpec` 把当前文书目标、多个模板角色、危险争点深析、已批证据和核验法源冻结在一个内部合成清单中；`ClaimBinding` 把每个重要法律命题绑定到权威原文位置并标明支持或重大不利作用。上述内部对象、来源台账、风险记录和确认单不得写进给法院或当事人的正式候选稿。

### 5.3 分批学习和登记

源码提供以下十一个模板学习/合成命令；先用 `--help` 查看当前参数合同：

```powershell
python scripts/legal_case_os.py template-distill --help
python scripts/legal_case_os.py template-register --help
python scripts/legal_case_os.py template-fill-plan --help
python scripts/legal_case_os.py template-fill-docx --help
python scripts/legal_case_os.py template-repeat-plan --help
python scripts/legal_case_os.py template-hybrid-plan --help
python scripts/legal_case_os.py template-structural-apply --help
python scripts/legal_case_os.py composition-build --help
python scripts/legal_case_os.py composition-validate --help
python scripts/legal_case_os.py citation-audit --help
python scripts/legal_case_os.py exemplar-leak-check --help
```

`template-distill` 只生成待批准的 `FormProfile` 或 `WritingProfile` 草稿；`template-register --operation create|upgrade|retire` 只有在授权、哈希、版本、画像和律师批准全部匹配后才能新增、升级或停用。升级保留旧版本，停用不删除文件。模板必须逐份完成校准、视觉复核和明确批准后才能激活，不得批量激活。

`template-repeat-plan`、`template-hybrid-plan` 和 `template-structural-apply` 只接受显式 `--test-mode`：它们用虚构 DOCX 验证标准行克隆、整块正文替换、锚点哈希和结构差异，不能把首批真实画像直接升级为可调用模板，也不能形成法院候选或提交资格。

### 5.4 手续填充与复杂文书合成

手续流程固定为：

`读取案件数据 → 逐格 FillPlan 与来源 → 集中列出高风险字段 → 必要时一次询问 → 复制原 DOCX → 仅修改批准节点 → 结构差异 → 逐页视觉 QA`

格子分为固定、必填核验、条件核验、可选核验、推导待确认、律师决定、人工留空和可复制行八类。收费方式/金额、一般或特别授权、和解、撤诉、放弃权利等不能由模型直接定案；签字、盖章、现场日期和审批意见不得自动填入。

以下完整合成流程适用于明确要求法院候选稿、需要严格主辅模板及法源绑定的复杂文书：

`冻结目标 → 选择主辅模板 → 相关争点深析 → 获准来源的支持/反对原文检索与核验 → ClaimBinding → CompositionSpec 校验 → 起草 → 引用/串案审计 → 独立审阅 → 视觉 QA`

只有模板冲突或缺项可能改变诉请、立场、证据使用、关键法源或对外风险时才集中询问；普通风格差异按主辅角色直接处理。网络不可用或法源无法核验时，受影响的复杂正式命题必须停止，不得用搜索摘要、模型记忆或旧模板法条补齐。

普通生成任务默认交付 `internal_review` 待审稿，上传文件足够时可完全脱离数据库和公网运行。复杂内部稿保留实际争点比较和结论边界，无须先建立完整合成状态。用户要求法院候选稿时，内部记录与正式正文分开；要求精确提交包时才进入G4。本源码没有G5执行器，不执行打印、发送、上传、送达或提交。

## 6. 文书预检与授权清洁

```powershell
python scripts/legal_case_os.py preflight --path "draft.docx" --json
```

预检会报告批注、修订、隐藏文字、内部 ID、占位符、元数据和水印。生产案件的清洁批准必须写成精确对象；下例仅展示一个目标，实际批准文件应逐项列全：

```json
{
  "ownership": "self_generated",
  "approval_id": "P-CLEAN-001",
  "source_artifact_id": "R-DRAFT-001",
  "approval_artifact_id": "R-CLEAN-APPROVAL-001",
  "independent_review_id": "R-PRE-CLEAN-REVIEW-001",
  "pre_review_status": "passed",
  "approved_by": "律师姓名",
  "approved_at": "2026-08-23T12:00:00Z",
  "approved_targets": [
    {
      "item_id": "SAN-001",
      "kind": "draft_watermark",
      "location": "word/header1.xml",
      "source_part_sha256": "<清洁前OOXML部件SHA-256>",
      "value": "DRAFT",
      "ownership": "self_generated",
      "authority_basis": "本所自有生成稿，经律师逐项批准移除"
    }
  ],
  "approval_set_sha256": "<approved_targets规范化JSON的SHA-256>"
}
```

```powershell
python scripts/legal_case_os.py sanitize-docx `
  --state "_case-state\case-state.json" `
  --source "draft.docx" `
  --approved-items "clean-approval.json" `
  --output "derived-clean.docx" `
  --report "sanitization-report.json" --json
```

每个目标都必须单独记录权属和授权依据，并根据类型绑定精确值、原文哈希或预期数量；顶层 `self_generated` 不能覆盖某个第三方目标。`SANITIZE` 批准还须在 `case-state.json` 中精确绑定源文书、独立审阅成果和批准文件，并存在匹配 `scope_hash` 的追加式 `approval_granted` 审计收据。无 `--state` 的入口只允许正文明确含 `TEST-ONLY` 的合成回归夹具。

第三方证据水印、普通实质段落、未获批准项目、隐藏文字、未决占位符和证据真实性相关内容不会自动删除。清洁结果固定为 `rereview_status=required`，仍不是法院成稿，必须再次独立审阅。

## 7. 组卷与打印生产单

```powershell
python scripts/legal_case_os.py bundle --state "_case-state\case-state.json" --package-id "B-CANDIDATE-001" --output "60-filing\G4-candidate.pdf" --page-numbers --json
python scripts/legal_case_os.py print-sheet --state "_case-state\case-state.json" --package-id "B-CANDIDATE-001" --output "60-filing\打印生产单.md" --json
```

组卷会先自行执行状态语义、批准审计、G2证据、法源、清洁复审和失效检查，再按清单顺序合并真实 PDF、写入书签，并在 `--page-numbers` 时叠加和文本复核 `n / total` 页码。缺少 PDF 或页码后端时返回结构化降级，不生成伪文件。输出仍须逐页视觉 QA。命令只生成文件和说明，不会执行打印、上传、发送或提交。

## 8. 离线验收

```powershell
python -B -m unittest discover -s tests -p "test_*.py" -v
python tests/run_acceptance.py --json-report tests/test-report.json
```

全部夹具均以 `TEST-ONLY` 标识；测试通过只能证明流程和门禁按设计工作，不能证明任何真实案件的法律结论。
