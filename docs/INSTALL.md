# 安装说明（公共版）

本页供两类读者：准备使用本包的人，以及替用户执行安装的 AI。安装完成后回到 [README](../README.md) 开始使用。

只安装本套件按本页执行；要一次安装并连接三个项目（本套件、[Agent Workstyle](https://github.com/gosedhuiyin/agent-workstyle)、[Law Library MCP](https://github.com/gosedhuiyin/law-library-mcp)）时，把 [组合安装方法](../INSTALL-COMBINED.md) 交给你的 AI 执行，并按其中的分层核验逐项确认。

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

- **个人模板库默认为空**：模板资料（原件、画像、登记表）应放在程序目录之外的自建资料目录，登记方法见 [library/模板库/README.md](../library/模板库/README.md)；程序目录内的登记表只是全新安装的空表起点。
- **固定手续模板不随包分发**：如户籍查询双表等固定手续能力依赖使用者自行配置的本地扩展；本包不含其模板与脚本，也没有下载链接。
- **数据库未随包发布**：需要知识库时，由用户自行配置只读 MCP 等接口；未配置时基础功能仍可用。
- **没有外部动作执行器**：本包不会打印、发送、登录、上传、送达或提交；这些动作始终由律师在系统外执行。

## 6. 更新与移除

先分清两类位置：

- **程序目录**：本包解压/克隆得到的目录，只包含随发行包分发的文件。不要把个人资料放进来。
- **用户数据位置**：案件工作区（你执行 `init` 时指定的目录）和模板库资料目录（见 [library/模板库/README.md](../library/模板库/README.md)，含原件 `00-待登记/`、画像 `_profiles/` 和登记表 `_registry/template-catalog.json`）。这两处都由你自建，独立于程序目录保存。

**更新（推荐：新目录并行，不覆盖旧目录）**

1. 下载新版本 ZIP，解压到**新的程序目录**（例如把 `legal-case-os-1.2.6/` 解压在旧目录旁边）；原程序目录此时原样保留。
2. 在 AI 软件中把技能加载从旧目录改指向新目录。
3. 需要使用个人模板登记表的命令继续按各自 `--help` 指向原登记表（通常用 `--catalog`，`validate` 用 `--personal-catalog`）；案件工作区路径不变。资料目录和案件工作区不做任何改动。
4. 用一两个虚构任务确认新版本可用后，旧程序目录才可以归档或删除；删除前确认其中没有你手动放入的文件（按本约定，你的资料本来就不在里面）。
5. 解压或复制新包时，如果目标位置已存在同名文件，先停下来逐项比对，不要直接覆盖；尤其不要把发行包内的空登记表复制或解压到已登记过模板的位置——空表只用于全新安装，不能重置已有登记。

直接在原程序目录 `git pull` 只适合跟随开发的场景：本地有修改或冲突时 Git 会停止，此时按提示逐项处理，不要强行覆盖。

**移除**

1. 默认步骤：在 AI 软件中解除对本包 `skills/` 的加载。到此卸载已完成；案件工作区和模板库资料目录**完整保留**，不受卸载影响。
2. 之后如果不再需要程序目录，先确认其中没有你手动放入的文件，再删除程序目录本身。
3. **删除个人资料（案件工作区或模板库资料目录）是另一项需要你明确提出的请求**；本包的说明和程序都不会默认执行。如需迁移，把原件、画像和登记表作为一组整体搬迁，并保留各自的相对结构。
