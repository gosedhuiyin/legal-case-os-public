# 模板库（程序目录内默认为空）

本目录是**程序自带的空模板库**：公共发行包不携带任何个人模板，这里只有目录说明和一张空的登记表，供全新安装起步。

**推荐做法：把你的模板资料放在程序目录之外的自建资料目录**，例如 `D:\办案资料\模板库\`。这样更新或移除本包程序时，你的原件、画像和登记表完全不参与，也不需要迁移。

## 资料目录里放什么

在资料目录中保持这套结构（登记表可从本目录复制空表起步）：

- `00-待登记/`：候选模板原件。
- `_profiles/`：模板画像、渲染基线与批准记录。
- `_registry/template-catalog.json`：登记表。程序按登记表精确定位原件和画像；**原件、画像、渲染/批准记录和登记表是一组相互关联的数据**，迁移或备份时要整体搬动，不能只保留其中一部分。

登记表里的 `path`、`profile_path`、`fingerprint_manifest_path` 等相对路径，以**登记表所在目录**（通常为 `_registry/`）为起点。例如，原件可写作 `../00-待登记/示例.docx`，画像可写作 `../_profiles/示例.json`；解析后的路径必须位于模板库根目录内。保持这套相对结构时，资料目录整体迁移无需改写相对路径；已有的绝对路径不会随目录迁移自动更新，需要另行核对。

## 未添加模板时会怎样

登记表为空时，任何模板编号、名称或别名都会如实返回"未找到"；系统不会把候选文件冒充为已登记模板，也不会自动激活任何画像。

```powershell
python scripts/legal_case_os.py template-ref-validate --catalog "D:\办案资料\模板库\_registry\template-catalog.json" --json
python scripts/legal_case_os.py template-ref-resolve --catalog "D:\办案资料\模板库\_registry\template-catalog.json" --template-id "你的模板编号" --json
```

需要使用个人模板登记表时，按该命令的 `--help` 指定外部登记表。例如，`template-register`、`template-ref-validate`、`template-ref-resolve` 均支持 `--catalog`；`validate` 使用 `--personal-catalog`。这些命令未指定该参数时，会使用程序目录内的默认登记表。不同模板命令的参数并不相同，不要给不支持该参数的命令统一添加 `--catalog`。

## 如何登记自己的模板

1. 建立资料目录结构，把候选模板原件放入 `00-待登记/`。
2. 用 `python scripts/legal_case_os.py template-distill --help` 查看蒸馏命令，从模板生成待批准画像草稿。
3. 逐份完成校准与视觉复核后，经律师明确批准，再用 `python scripts/legal_case_os.py template-register --help` 登记：登记时用 `--catalog` 指向资料目录中的登记表，用 `--destination-dir` 指定画像等派生文件的存放位置。
4. 登记后即可按编号、名称或别名精确调用；未登记或未批准的文件不能按编号调用。

模板是表达和版式参照，不是案件事实或法律依据。只有被你按编号、名称或别名精确点名时才允许读取；系统不会默认把全库装入上下文。

## 不要用发行包覆盖已有登记表

本程序目录中的 `_registry/template-catalog.json` 只是**全新安装**的起点，它为空是正常状态。在相关命令正确指定外部登记表时，程序不会读写程序目录内的这份文件。

任何情况下都**不要把发行包内的空登记表复制或解压到已经登记过模板的位置**——那会用空表覆盖你的登记信息。解压新版本到新的程序目录、复制文件到新位置之前，如果目标已存在同名文件（尤其是 `template-catalog.json`），先停止，逐项比对内容后再决定；已登记的登记表永远以资料目录中的那份为准。
