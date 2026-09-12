# 模板库（公共版默认为空）

本目录存放律师认可的简单标准文书、复杂经典文书和手续版式。**本发行包不携带任何个人模板**：这里只有目录结构和一张空的目录表，供你在本机登记自己的模板。

- `00-待登记/`：候选模板原件。默认被版本控制忽略，只保存在你的机器上。
- `_profiles/`：模板画像、渲染基线与批准记录。默认被版本控制忽略，只保存在你的机器上。
- `_registry/template-catalog.json`：机器目录表。初始为空；登记模板后由 CLI 在本机维护，`*.lock` 同样不入库。
- `案例库/`（位于上一级目录）：保持空目录，用于以后另行确定存放裁判案例或复盘资料的规则。

## 未添加模板时会怎样

目录表为空时，任何模板编号、名称或别名都会如实返回"未找到"；系统不会把候选文件冒充为已登记模板，也不会自动激活任何画像。

```powershell
python scripts/legal_case_os.py template-ref-validate --json
python scripts/legal_case_os.py template-ref-resolve --template-id "你的模板编号" --json
```

## 如何登记自己的模板

1. 把候选模板原件放入本目录的 `00-待登记/`（本机，不入库）。
2. 用 `python scripts/legal_case_os.py template-distill --help` 查看蒸馏命令，从模板生成待批准画像草稿。
3. 逐份完成校准与视觉复核后，经律师明确批准，再用 `python scripts/legal_case_os.py template-register --help` 登记。
4. 登记后即可按编号、名称或别名精确调用；未登记或未批准的文件不能按编号调用。

模板是表达和版式参照，不是案件事实或法律依据。只有被你按编号、名称或别名精确点名时才允许读取；系统不会默认把全库装入上下文。
