# 更新记录

## 组合安装说明更新

- 安装时就登记工作风格、法律SOP、资料库三个可发现的独立入口，并填入真实本机依赖和工具映射。
- DSH等法律预设从第一条回复起使用Fable/Astra工作风格；SOP和资料库按法律任务需要使用。独立技能支持从会话第一句分别调用。
- 区分法律问答只读、明确制作指令及普通商务改写，补充误触发负例和入口验收记录。
- 本次只改说明和示例收据，运行代码与1.2.6发行包未改变；已有安装须按新说明更新入口才能取得新行为。

本文件记录公开发行的版本。1.2.1 及更早的版本为内部开发版本，未公开发布。

## 1.2.6（2026-09-13）

新增四组本地文书工具、学习应用核对，以及三组件组合安装入口。均为本机离线操作，不调用模型、不执行外部动作；程序检查不等于语义核验，版式仍须逐页人工复核。

- **原 DOCX 指定位置局部修改**（新命令 `docx-inspect`、`docx-patch`，新模块 `scripts/legal_case_os_lib/docx_edit.py`）：先检出正文段落和顶层表格单元格的精确坐标，再按 `source_sha256` 绑定原件做字节级局部替换；目标外内容和原件保留，独立输出修改说明。仅支持普通统一格式文字；含字段、修订、超链接、合并单元格、签名 DOCX 等复杂目标明确拒绝。
- **按明确物理页段加页码并回填原清单**（新命令 `evidence-pages`，新模块 `scripts/legal_case_os_lib/evidence_pages.py`）：核对原 PDF/清单哈希后，按显式页段生成带页脚页码和书签的派生 PDF，只回填原证据清单指定格子，共用同一 `page_map`；缺失/越界/重复页段、加密或含图层显隐的 PDF 等复杂输入明确拒绝。原 PDF 与清单只读。
- **成果版本登记与恢复**（新命令 `delivery-current`、`delivery-publish`、`delivery-restore`，新模块 `scripts/legal_case_os_lib/delivery.py`）：对任务目录的 `manifest.json` 核验实际文件哈希后登记原字节版本；`current.json` 唯一当前指针，`--expected-current` 传 `revision` 令牌防并发覆盖，恢复只切换指针不重渲染。登记不表示正式定稿或提交批准。
- **新建稿标准 Markdown 表格生成真实 Word 表格**（`templates.py`）：支持列对齐、空单元格、转义竖线、重复表头和长格跨页；列数不符报具体行号，超过八列要求改用明确版式路径。只影响新建稿，不扩大 `docx-patch` 修改范围。
- **学习条目应用核对**（`learning.py`、新命令 `learning-check`，`task-render` 支持 `learning_applications`）：学习条目可补 `application_guide`（步骤/正反例/差异）；应用 JSON 按"来源→条件→输出位置"核对，事实前提须绑定本次材料，未满足/未知条件保留为待核。脚本核验结构与字面一致，不判定语义正确。
- **三组件组合安装入口**（新增根目录 `INSTALL-COMBINED.md` 与 `docs/combined-install-receipt.example.json`）：把本套件、Agent Workstyle、Law Library MCP 的组合安装步骤、分层核验（代码到位/规则加载/技能发现/CLI 可运行/MCP 真连接/合成任务）和记录方法集中成一个入口；三个上游仓库保持独立，README 与 `docs/INSTALL.md` 增加相对链接。
- **文档与政策同步**（`docs/MATERIAL-TASKS.md`、`docs/USER-GUIDE.md`、`shared/policies/execution-recipes.md`、`shared/policies/material-access-and-learning.md`）：补充上述工具的自然语言说法、计划示例和边界；正文与内部审阅记录保持分开，"仅用户主动调用法律套件"的启用约束不变。

### 本次发行的验证记录

全部在本仓库工作树独立完成，Windows + Python 3.11.9（python-docx、pypdf、PyMuPDF、reportlab 用于相应测试与渲染核对），全部夹具为 TEST-ONLY 合成数据：

- 单元测试：`python -B -m unittest discover -s tests -p "test_*.py"` —— **215 通过 / 0 失败 / 1 跳过**（跳过项为本机无法创建符号链接的环境限制测试）。含新增 `tests/test_docx_edit.py`、`test_evidence_pages.py`、`test_delivery.py`、`test_workflow_micro.py`、`test_markdown_docx_tables.py`、`test_final_polish.py` 及更新的 `test_learning.py`。
- 离线验收：`python tests/run_acceptance.py` —— **36 PASS / 0 FAIL / 0 SKIP**。
- 冒烟检查：`python scripts/smoke_test.py` —— **8 / 8 通过**，`external_actions_executed = 0`。
- CLI 帮助核对：`docx-inspect`、`docx-patch`、`evidence-pages`、`delivery-current`、`delivery-publish`、`delivery-restore`、`learning-check` 均已注册且帮助可见。
- 合成样例视觉核对：60 行 Markdown 表格生成真实 Word 表格，经 LibreOffice 渲染逐页查看，表头在第 2、3 页重复、收尾段落衔接正常；合成 3 页 PDF 取物理第 2–3 页生成 2 页带页脚页码（"1 / 2"）的派生 PDF，原清单指定格子精确回填"1-2"，其余格子未动。
- 已知限制：列宽为按内容估计，复杂版式仍须逐页复核；`evidence-pages` 页脚区为固定 28pt；自动安装链面向的宿主组合见 `INSTALL-COMBINED.md`，其他宿主需按其文档核对。组合安装方法本次未在其他宿主实测。

## 1.2.5（2026-09-12）

补充模板命令的参数说明：只有支持外部登记表参数的命令才添加 `--catalog`；`validate` 使用 `--personal-catalog`，其他命令以各自 `--help` 为准，避免把同一参数套给全部模板命令。包含 1.2.4 的路径说明修正，本次统一发布下载包。代码未变。

## 1.2.4（2026-09-12）

修正模板资料目录说明中的相对路径起点，避免按文档填写后找不到模板。

- `path`、`profile_path`、`fingerprint_manifest_path` 的相对路径从登记表所在目录（通常为 `_registry/`）解析；模板库根目录是允许访问的边界，不是相对路径的起点。补充 `../00-待登记/示例.docx` 等示例。
- 明确整体迁移时保持有效的是相对路径；绝对路径需要另行核对。外部登记表也需在相关命令中正确指定，未指定时仍使用程序内的默认登记表。
- 本次仅修改说明和版本元数据，路径解析代码及办案流程未变。已对现有三个路径解析函数做虚构路径核验；AI 客户端实际安装、更新与卸载仍未实测。

## 1.2.3（2026-09-12）

补丁发布：修正更新/卸载时的用户资料保留说明，并同步知识库连接说明的通用化修正。办案流程、状态合同与门禁语义不变。

- **修正更新与卸载说明**（`docs/INSTALL.md`）：v1.2.2 曾建议用新版本覆盖安装目录，并称"被忽略目录中的用户数据不受影响"，随后又建议删除整个安装目录来卸载。Git 忽略规则不能防止文件被覆盖或删除，而模板资料可能就放在安装目录内。现改为**程序目录与用户数据分离**：更新时把新版本解压到新的程序目录并行使用，原程序目录与用户数据完整保留；卸载的默认步骤是解除 AI 软件对本包的加载，用户资料保留；删除个人资料是另一项需要用户明确提出的请求。
- **模板资料目录模式**（`library/模板库/README.md`、`docs/OPERATOR-GUIDE.md`、`README.md`、`MD-FILE-GUIDE.md`）：推荐把原件 `00-待登记/`、画像 `_profiles/` 和登记表 `_registry/template-catalog.json` 整体放在程序目录之外的自建资料目录，用现有 `--catalog` / `--personal-catalog` 参数指向资料目录，不新增配置项；登记表内的相对路径按登记表所在位置解析，资料目录整体迁移后登记仍然有效。程序目录内的空登记表只用于全新安装，任何情况下不得用空表覆盖已登记的登记表；复制或解压遇同名文件先停止、逐项比对。
- **知识库连接说明通用化**（`shared/policies/network-degradation.md`）：同步上游已完成的修正——知识库连接由当前客户端或用户配置提供，不依赖固定的开发机目录或 MCP 服务名称；仅在本次任务已有相应资料访问授权时使用实际可发现、已认证且符合读取范围的只读 MCP 工具，读取本规则本身不产生新的资料访问授权；"公开互联网"段改为"新的会员库、第三方付费服务或额外凭据不随既有知识库访问授权自动开放"。移除了残留的原开发机数据库路径。
- 版本统一 1.2.3（插件元数据、文档、tag、ZIP 名称）。

### 修复演练记录（全部使用虚构 TEST-ONLY 资料，不涉及真实模板与案件）

- **并行更新保留数据**：在含已登记 TEST-ONLY 模板的资料目录旁，把新版程序解压为新目录；原件、画像、登记表的前后 SHA-256 全部不变，登记状态与非空登记表保持原样，且从新程序目录用同一 `--catalog` 解析模板仍然成功（相对引用有效）。
- **空表不覆盖已登记表**：整个更新流程不向资料目录写入任何文件；新程序目录内的空登记表与资料目录中的已登记表互不影响。
- **卸载保留资料**：按修正后的卸载步骤（仅解除加载）后，资料目录与登记表仍存在且可正常校验、解析。
- **全新空库安装**：新版冒烟检查 8/8 通过；对整个发行包扫描，无固定开发机路径或 MCP 服务名残留；未配置知识库时基础命令与限制说明保持可用。

## 1.2.2（2026-09-12）

首次公开发布。基于内部 v1.2.1 源码做公开分发整理，核心流程、状态合同与门禁语义不变。

**本次做的是公开分发整理、文案与安装修正**，不是新的功能版本：

- 从内部源码按明确文件白名单导出公共发行包；不携带个人模板库（`00-待登记/`、`_profiles/`）、个人模板原件与登记记录、验收与报告目录、迁移研究底稿，以及任何真实案件材料。
- 不包含本地专属的固定手续技能（如户籍查询双表）及其 DOCX 模板与填充脚本；相应能力说明改为“依赖使用者自行配置的本地扩展”。
- 个人模板库以空表发行：目录表初始为空，未登记模板时按编号查询如实返回“未找到”，并提供空库起步与登记说明。
- 新增公共版 `README.md` 与 `docs/INSTALL.md`，重写插件元数据与首页文案，使其面向第一次接触项目的人；安装口令“调用法律套件”与总控技能入口的映射在文档中写明。
- 修正公开版入口与相对链接：技能总清单、架构图（重新生成 SVG）、文档索引、测试说明与验收命令与实际发行内容一致。
- 公开版 `.gitignore` 明确忽略个人工作目录（`library/模板库/00-待登记/`、`_profiles/`、`_registry/*.lock`）与本地测试输出；不依赖任何本机排除规则。
- 修正三个环境兼容问题：`.gitattributes` 要求整个仓库按字节精确检出（`* -text`），否则 Windows 上 CRLF 检出会使模板与夹具哈希门禁误报；`tools/build_template_boundary_suite.py` 在非 UTF-8 控制台打印中文会崩溃（加 UTF-8 输出保护）；固定渲染夹具 `tests/artifacts/render-cleaning-source/render.pdf` 的 TEST-ONLY 标语原文“不得提交”会被生产预检词表如实拦截，改用不冲突的措辞重新渲染该夹具。两者都不改变任何生产代码、lint 词表或检查逻辑；夹具 DOCX 未改动。
- 版本号统一为 1.2.2（插件元数据、文档、tag）。
- 实际包含的公开技能数量为 14 个（内部版为 15 个；差异即上述本地专属技能）。

### 本次发行的验证记录

验证在独立临时安装目录中进行（不含原私人模板库与原开发源码路径），环境为 Windows + Python 3.12，装有 pypdf 6.18.1、PyMuPDF 1.28.2、python-docx、reportlab 与 LibreOffice：

- 单元测试：`python -B -m unittest discover -s tests -p "test_*.py"` —— **139 通过 / 0 失败 / 0 跳过**。
- 离线验收：`python tests/run_acceptance.py` —— **36 PASS / 0 FAIL / 0 SKIP**。
- 冒烟检查：`python scripts/smoke_test.py` —— **8 / 8 通过**，`external_actions_executed = 0`。
- 最小任务抽查：以一段合成材料执行 `material-import` → `material-read` → `route`，均成功；空模板目录下 `template-ref-validate` 返回 0 条记录且无错误，`template-ref-resolve` 对未登记编号如实返回“未找到”。
- 边界渲染夹具的确定性重建测试通过，表明随包分发的 DOCX/PDF/PNG 测试夹具是构建工具从 TEST-ONLY 数据生成的确定性产物。
- 全部测试使用 `TEST-ONLY` 虚构夹具；测试通过只证明流程与门禁按设计工作，不构成对真实案件法律结论的验证。
- 客户端兼容性：未经实测的 AI 软件一律视为未验证，本包不承诺“自动适配所有客户端”。

## 1.2.1

内部开发版本（未公开分发）：在 v1.2 的“首次生产＋长期工单＋个人模板学习与复杂文书合成”核心上，新增已独立验证的固定双表叶子技能，不改变案件状态、通用填充、审批或外部动作边界。
