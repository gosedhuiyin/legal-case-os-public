# 第三方来源与许可声明

本项目是面向个人律师工作流的独立实现。除下列公开许可材料外，未复制其他软件的隐藏提示词、封装代码、会员内容、连接器凭证或专有模板。

## 1. 中国民商事诉讼工具箱（Qoder for CN Litigation）

- 原作者：游初（You Chu），Copyright 2026。
- 原许可：Apache License 2.0。
- 本项目使用方式：在保留来源与修改说明的前提下，改编“薄路由中枢、案件配置覆盖、要件攻防、分级来源、能力降级、引用闸门、复盘沉淀”等工作方法；未复制其外部连接器配置、静态法条或依赖会员数据库的实现。
- 许可证副本：[LICENSES/QWEN-CN-LITIGATION-APACHE-2.0.txt](./LICENSES/QWEN-CN-LITIGATION-APACHE-2.0.txt)。
- 修改说明：本项目新增证据律师批准池、唯一 JSON 状态、对象级批准与失效、个人范例独立检索、清洁双审、离线测试和禁止外部动作等约束；目录、字段、文案和实现均已重构。

## 2. WorkBuddy expert-manager

- 原许可：Apache License 2.0。
- 本项目使用方式：借鉴“初始化—生成—校验—登记—打包”的可验证专家包生命周期、元数据校验和回归检查思想；没有复制 WorkBuddy 注册路径、会话变量、头像流程或运行时专有接口。
- 许可证副本：[LICENSES/WORKBUDDY-EXPERT-MANAGER-APACHE-2.0.txt](./LICENSES/WORKBUDDY-EXPERT-MANAGER-APACHE-2.0.txt)。

## 3. Kimi 可见公开许可 Skill

- `process-doc`：Apache License 2.0；借鉴 SOP 的范围、责任、异常和验收结构。许可证见 [LICENSES/KIMI-PROCESS-DOC-APACHE-2.0.txt](./LICENSES/KIMI-PROCESS-DOC-APACHE-2.0.txt)。
- `legal-risk-assessment`：Apache License 2.0；仅借鉴显式升级条件和责任人复核思想，不采用企业合同风险分数作为诉讼结论。许可证见 [LICENSES/KIMI-LEGAL-RISK-ASSESSMENT-APACHE-2.0.txt](./LICENSES/KIMI-LEGAL-RISK-ASSESSMENT-APACHE-2.0.txt)。
- `copy-editing`：MIT，Copyright 2025 Corey Haines；仅借鉴“多轮、单维度、保持原意”的编辑方式。许可证见 [LICENSES/KIMI-COPY-EDITING-MIT.txt](./LICENSES/KIMI-COPY-EDITING-MIT.txt)。
- `humanizer-zh`：MIT，Copyright 2026 歸藏；仅借鉴识别 AI 套话的审阅思想。法院文书不采用营销、人称、幽默或“注入灵魂”等规则。许可证见 [LICENSES/KIMI-HUMANIZER-ZH-MIT.txt](./LICENSES/KIMI-HUMANIZER-ZH-MIT.txt)。
- Kimi `docx` 的本地许可为 Moonshot AI 专有许可，因此本项目不复制其脚本、模板或指令；Word/PDF 能力采用本项目独立脚本与运行环境中可合法使用的通用库。

## 4. Claude 与 Codex

- Claude：本地仅观察到共享 Skill 目录通过文件系统 junction 复用的组织方式；没有发现可迁移的 Claude 原生法律 Skill，因此不复制其产品内部实现。
- Codex：本项目依据公开的 Skill/插件目录约定构建，并使用本机可用的通用文档渲染与测试能力；没有修改或再分发 Codex 自带 Skill。

完整的来源路径、版本、哈希和吸收/剔除决定记录在维护者的内部台账中，不属于本公共发行包；上表已列明各来源的许可副本与使用方式。
