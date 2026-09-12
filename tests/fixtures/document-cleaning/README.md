# TEST-ONLY 清洁文书夹具

`TEST-ONLY-cleaning-source.docx` 是完全虚构的脏文书，只用于验证预检阻断。它故意包含：

- Word 批注；
- 跟踪修订；
- `DRAFT-TEST-ONLY` 页眉水印；
- `INT-TEST-CASE-001` 内部编号；
- `[[PENDING:确认送达地址]]` 未决占位符；
- 隐藏文字；
- 内部作者、最后修改者、关键词和备注元数据。

预期规则：只有在文书属于本项目生成稿、律师批准具体清洁项时，才可生成派生副本；未决占位符和实质内容必须阻断法院候选，第三方证据水印不得自动移除。原始夹具哈希必须始终保持不变。

`TEST-ONLY-cleanable-source.docx` 省去未决占位符和隐藏文字，但保留批注、修订、草稿水印、内部编号、内部备注和元数据，用于验证获批清洁流程。`TEST-ONLY-cleaned-derived.docx` 必须由 CLI 作为派生副本生成，并附清洁报告和再次审阅状态。

重建脚本位于 [../../tools/build_cleaning_fixture.py](../../tools/build_cleaning_fixture.py)，需要合法可用的通用 DOCX 运行库和文档 Skill 辅助脚本；仓库内已保存生成后的固定夹具，日常测试无需重建。
