# 安装说明（公共版）

本页供两类读者：准备使用本包的人，以及替用户执行安装的 AI。安装完成后回到 [README](../README.md) 开始使用。

## 1. 需要什么环境

| 需求 | 说明 |
|---|---|
| AI 软件 | 能读取技能目录（`skills/`）、需要时能执行本地 Python 程序的 AI 软件。只聊天、不能读本地文件的软件无法完整使用。 |
| Python | 3.10+。核心状态、路由、审计、模板命令只用标准库。 |
| 可选依赖 | Word/PDF 相关命令按需检查 `pypdf`、`PyMuPDF`、`python-docx`、`reportlab` 等；缺失时对应命令会明确停止，不会生成伪文件。生成 PDF 需本机 LibreOffice 或 Word。 |

## 2. 安装步骤

1. **取得本包**：`git clone` 本仓库，或下载 Release 中的 ZIP 并解压到一个本地目录（下称“安装目录”）。
2. **验证 CLI 可用**（在安装目录运行）：

   ```powershell
   python scripts/legal_case_os.py --help
   python scripts/smoke_test.py
   ```

3. **让 AI 软件读取技能**：在 AI 软件中加载/指向安装目录的 `skills/` 目录。总控入口是 `skills/legal-case-orchestrator/SKILL.md`，其余技能由它按需调用。
4. **确认技能已识别**：对 AI 说“调用法律套件”，AI 应当读取 `skills/legal-case-orchestrator/SKILL.md` 并按其中的流程响应；如果 AI 不知道这个口令对应什么，说明技能目录没有被加载。

**口令映射**：安装请求和日常使用中的“调用法律套件”= 读取并遵循 `skills/legal-case-orchestrator/SKILL.md`。这不是一个需要单独安装的程序。

## 3. 给执行安装的 AI

- 只把安装目录的 `skills/`、`scripts/`、`shared/`、`matter-workspace-template/`、`library/` 指给目标软件；不要把安装目录复制进任何项目的工作目录，也不要把本包的说明当作项目规则安装。
- 保留用户已有的规则、技能和 MCP 配置；本包不要求任何 MCP 配置。
- 安装完成后用中文向用户报告：改了哪里、技能入口是否已可读取、CLI 帮助是否可用、是否需要重新打开会话。

## 4. 安装后验收（可选，但建议）

在安装目录运行：

```powershell
python -B -m unittest discover -s tests -p "test_*.py" -v
python tests/run_acceptance.py --json-report tests/test-report.json
python scripts/smoke_test.py
```

全部夹具均为 `TEST-ONLY` 虚构数据。本次发行的实际测试结果记录在 [CHANGELOG.md](../CHANGELOG.md)。

再逐项确认：

- `library/模板库/_registry/template-catalog.json` 有效且为空表；按任意模板编号查询都如实返回“未找到”；
- 生成一份测试稿到临时目录（用 `tests/fixtures/simple-case/` 的虚构材料），确认 Word/PDF 后端表现符合预期或如实降级。

## 5. 边界说明

- **个人模板库默认为空**：登记自己的模板见 [library/模板库/README.md](../library/模板库/README.md)。
- **固定手续模板不随包分发**：如户籍查询双表等固定手续能力依赖使用者自行配置的本地扩展；本包不含其模板与脚本，也没有下载链接。
- **数据库未随包发布**：需要知识库时，由用户自行配置只读 MCP 等接口；未配置时基础功能仍可用。
- **没有外部动作执行器**：本包不会打印、发送、登录、上传、送达或提交；这些动作始终由律师在系统外执行。

## 6. 更新与移除

- **更新**：重新下载新版本覆盖安装目录，或 `git pull`；用户数据（案件工作区、个人模板库）保存在安装目录之外或被忽略的目录中，不受影响。
- **移除**：删除安装目录，并在 AI 软件中移除对 `skills/` 的加载即可；本包不写入全局配置。
