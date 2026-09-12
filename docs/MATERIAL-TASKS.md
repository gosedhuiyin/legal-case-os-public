# 文件任务与知识库联动

本入口支持一次性模板起草和来源绑定的学习，不要求先创建 `case-state.json`。本次材料与产物保存在任务目录；正式 `library` 和 `00-originals` 只读。

## 1. 接入与阅读

在源码根目录执行，参数中的文件必须是当前任务明确允许使用的材料：

```powershell
python scripts/legal_case_os.py material-import --file "D:\本次材料\模板.docx" --role template --output-dir "D:\本次任务\materials"
python scripts/legal_case_os.py material-import --file "D:\本次材料\案情.md" --role current_case --output-dir "D:\本次任务\materials"
python scripts/legal_case_os.py material-read --record "实际返回的record_path" --offset 0 --limit 12000
```

`material-import` 返回完整记录和绝对 `record_path`。材料用途由当前任务判断：`template`、`current_case`、`case`、`authority`、`exemplar` 等。`evidence`、`user_statement` 也可用作本案事实来源，但用途标签不等于事实已经证实。读取结果有 `next_offset` 时继续阅读，保留字符范围和提取缺口。

支持 DOCX 段落与表格、Markdown、TXT；PDF文字层提取需要 pypdf。没有文字层或缺后端时明确返回覆盖状态，不自动OCR、不把空白当作完整原文。原文件与任务快照、正文各有哈希；重新提取产生新版本，不覆盖旧记录。

## 2. 接现有只读 MCP

简单模板先用 `find_template` 精确定位；复杂案用 `search_library`/`search_document` 查找，再通过 `read_document`/`read_unit` 读取完整相关原文，使用 `make_citation` 定位。文字结果不足时才考虑独立语义补充。不得把相似度、旧自动角色或审级标签当作法律结论。

需要原DOCX/PDF时，读取 `get_source_info` 的实际能力提示。有 `read_original` 的服务用精确 `document_id` 和 `version_id` 取件，按 `next_offset` 顺序读到 null；把全部响应保存为本任务的JSON数组，再导入：

```powershell
python scripts/legal_case_os.py material-import-original --parts "D:\本次任务\original-parts.json" --role template --output-dir "D:\本次任务\materials"
```

适配器核对各块身份、偏移、长度、块哈希与全件哈希，拒绝缺块/重复块/换版本。知识库的相对 `original_url` 不等于MCP已经取得原件。旧服务未提供该工具时，使用有明确权限的本地原件或上传文件；不能声称原件保真已核验。

已建立原件SHA256对应关系的旧模板画像可复用，但仍需核对当前画像哈希、批准版本和使用方式。知识库“可查询”不等于模板画像“已批准”；新原件版本不继承旧画像批准。

使用 `source-links --sha256 原件哈希 --source-map 映射JSON路径` 查找已核对的对应画像。映射 JSON 由使用者在本机自行保存和维护，不属于本仓库；未匹配不表示全库不存在。该命令只证明当前目录与画像内容对应，不替代本案适用性审阅。

知识库正文与本地DOCX提取器可能产生不同文字序列；即使原件哈希相同，`library_text_sha256` 与任务 `text_sha256` 也可能不同。两套字符偏移不能混用：知识库引用保留知识库定位；任务起草/学习引用以 `material-read` 返回的正文定位。

## 3. 起草并生成真实文件

AI实际阅读上述材料后完成正文和分析；脚本不生成语义，不调用额外模型。把本次写作结果存为proposal JSON：

```json
{
  "title": "申请书",
  "mode": "simple",
  "audience": "internal_review",
  "template_material_id": "实际模板材料ID",
  "template_use": {"applied_patterns": ["请求在前，理由在后"]},
  "sections": [
    {"kind": "heading", "text": "事实与理由"},
    {"kind": "fact", "text": "依据本案材料写出的事实。", "source_bindings": [
      {"id": "本案材料ID", "start": 0, "end": 4, "quote": "精确原文"}
    ]}
  ],
  "exemplar_exclusions": ["不应进入新稿的旧案特有信息"],
  "unresolved_items": ["确实缺少、待律师核对的信息"]
}
```

字符范围为零基左闭右开，必须与 `material-read` 原文相符。section类型可为 `heading/fact/argument/analogy/authority/administrative`。事实、论证需绑定来源或明确 `assumption`；本案事实不得引用旧模板/旧案例作为其事实来源。`analogy/authority` 另写 `verification_status` 为 `source_verified/unverified/test_only`，仅原文核对不等于现行效力核对。

复杂稿设 `mode: complex`，增加 `analysis`：`issues`、`source_comparison`、`strongest_adverse_path`、`application_conditions`、`conclusion_limits` 和精确 `source_bindings`。这是实际的争点、材料比较和结论边界，不能填空泛句子凑齐字段。

```powershell
python scripts/legal_case_os.py task-render --material-record "模板record_path" --material-record "案情record_path" --proposal "D:\本次任务\proposal.json" --output-dir "D:\本次任务\draft-v1"
```

输出 `draft.docx`、`draft.md`、`review.md`、`manifest.json`。已存在的输出目录不覆盖。此路径参考模板结构和表达后重新生成Word，**不承诺保留上传DOCX的原始版式**。格式敏感表单使用已核验的原件填充工具或实际文档编辑能力，再逐页渲染比较；不能假装已获得保真能力。

代码检查只证明来源、引用与明确排除项一致。正文是否准确、事实是否充分、旧案残留是否遗漏、版式是否合格，仍由当前AI实际独立审阅。内部稿尚未获准提交；需要法院候选稿时再进入G1/G2及对应合成核验，需要精确提交包时进入G4。

## 4. 学习并按确认保存

`learning-build` 接受由AI阅读原文后形成的提案：`id/title`，以及选用的 `style_rules/reasoning_patterns/viewpoints`。每条包含 `id/text/applies_when/not_applicable_when/source_bindings`。观点还需 `speaker/speaker_role/original_view/rule_premises/fact_premises/counterarguments_or_limits/verification_status`；核验状态使用 `unverified/source_position_only/verified_at_source_date/disputed/superseded`。

```powershell
python scripts/legal_case_os.py learning-build --material-record "来源record_path" --proposal "D:\本次任务\learning-proposal.json" --output "D:\本次任务\learning-candidate.json"
```

候选立即可用于本任务。向用户展示规则、适用条件和必要来源片段；用户确认具体内容后，确认记录才可写入 `decision: approved`、`object_id`、`candidate_sha256`、`actor`、带时区的 `confirmed_at` 和 `reuse_reviewed: true`。不能把对流程的同意伪造为对具体内容的批准。

```powershell
python scripts/legal_case_os.py learning-save --candidate "D:\本次任务\learning-candidate.json" --library-dir "D:\个人学习资产" --confirmation "D:\本次任务\confirmation.json"
python scripts/legal_case_os.py learning-load --path "实际返回的asset_path"
```

长期资产包含获准内容、版本、最少引文与来源/批准哈希；无需数据库即可读取。新案须重新核对适用条件。若可见来源已变，拒绝把旧资产当作仍然有效；来源暂不可见时明确使用离线快照，不能冒充核对过当前版本。

## 5. 后端故障

MCP或数据库失联时直接继续使用本次上传/已固定文件。无结果、无权限、版本变更与超时分开报告；权限拒绝不允许自动改用旧缓存绕过。只读取本任务实际所需资料，不导出全库、不触发同步或向量建设。恢复后核对实际使用版本，受影响产物进入待复核，已有文件不被覆盖。
