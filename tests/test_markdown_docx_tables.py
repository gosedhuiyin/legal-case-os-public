"""TEST-ONLY coverage of the explicit Markdown-to-DOCX table pipeline."""
from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from legal_case_os_lib.core import LegalCaseError
from legal_case_os_lib.templates import create_docx_from_markdown

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def paragraph_text(element):
    return "".join(item.text or "" for item in element.iter(W + "t"))


class MarkdownDocxTablesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lcos-table-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def render(self, markdown):
        output = self.root / "TEST-ONLY-table.docx"
        create_docx_from_markdown(markdown, output, title="TEST-ONLY")
        with zipfile.ZipFile(output) as package:
            return ET.fromstring(package.read("word/document.xml"))

    def test_real_table_keeps_order_empty_cells_and_alignment(self):
        tree = self.render("# 测试目录\n\n表前文字\n| 序号 | 文件名称 | 页码 |\n| :---: | :--- | ---: |\n| 1 | 测试甲文件 | 1-2 |\n| 2 | 测试乙文件 | |\n\n表后文字")
        table = tree.find(".//" + W + "tbl")
        rows = table.findall(W + "tr")
        self.assertEqual(len(rows), 3)
        self.assertEqual([[paragraph_text(cell) for cell in row.findall(W + "tc")] for row in rows], [["序号", "文件名称", "页码"], ["1", "测试甲文件", "1-2"], ["2", "测试乙文件", ""]])
        self.assertEqual([item.get(W + "val") for item in rows[1].iter(W + "jc")], ["center", "left", "right"])
        self.assertIsNotNone(rows[1].find(".//" + W + "cantSplit"))
        self.assertEqual([paragraph_text(item) for item in tree.find(W + "body") if item.tag == W + "p"], ["测试目录", "", "表前文字", "", "表后文字"])

    def test_header_repeats_and_long_rows_can_expand_and_split(self):
        long_text = "TEST-ONLY长单元格内容。" * 300
        tree = self.render("| 编号 | 内容 |\n| --- | --- |\n| 1 | " + long_text + " |")
        rows = tree.findall(".//" + W + "tr")
        self.assertIsNotNone(rows[0].find(".//" + W + "tblHeader"))
        self.assertIsNotNone(rows[0].find(".//" + W + "cantSplit"))
        self.assertIsNone(rows[1].find(".//" + W + "cantSplit"))
        self.assertIsNone(tree.find(".//" + W + "trHeight"))
        self.assertEqual(paragraph_text(rows[1].findall(W + "tc")[1]), long_text)

    def test_width_fits_existing_page_and_prefers_long_content(self):
        tree = self.render("编号 | 内容说明 | 页码\n--- | --- | ---\n1 | " + "测试长句" * 30 + " | 1-3")
        widths = [int(item.get(W + "w")) for item in tree.findall(".//" + W + "gridCol")]
        self.assertEqual(sum(widths), 9026)
        self.assertGreater(widths[1], widths[0] * 2)
        self.assertEqual(tree.find(".//" + W + "pgSz").get(W + "w"), "11906")
        self.assertEqual(tree.find(".//" + W + "pgMar").get(W + "left"), "1440")

    def test_escaped_pipes_xml_characters_and_explicit_line_breaks(self):
        tree = self.render(r"| 字段 | 值 |" + "\n| --- | --- |\n" + r"| 字符 | A\|B & <值><br>下一行 |")
        cells = tree.findall(".//" + W + "tr")[-1].findall(W + "tc")
        self.assertEqual(paragraph_text(cells[1]), "A|B & <值>下一行")
        self.assertEqual(len(cells[1].findall(".//" + W + "br")), 1)

    def test_multiple_tables_stay_separate(self):
        tree = self.render("| 甲 | 乙 |\n| --- | --- |\n| A | B |\n\n说明\n\n| 丙 | 丁 |\n| --- | --- |\n| C | D |")
        self.assertEqual(len(tree.findall(".//" + W + "tbl")), 2)

    def test_indented_separator_does_not_turn_text_into_table(self):
        for indent in ("    ", "\t", " \t"):
            with self.subTest(indent=repr(indent)):
                tree = self.render("| A | B |\n" + indent + "| --- | --- |\n| C | D |")
                self.assertEqual(tree.findall(".//" + W + "tbl"), [])
                self.assertIn("| --- | --- |", paragraph_text(tree))
                if "\t" in indent:
                    self.assertIsNotNone(tree.find(".//" + W + "tab"))

    def test_new_blocks_end_table_even_when_they_contain_pipes(self):
        following = ["## 独立标题 | 附注", "- 独立条目 | 附注", "> 引用文字 | 附注", "1. 顺序条目 | 附注", "```text | 附注"]
        for line in following:
            with self.subTest(line=line):
                tree = self.render("| A | B |\n| --- | --- |\n| C | D |\n" + line)
                self.assertEqual(len(tree.findall(".//" + W + "tr")), 2)
                paragraphs = [item for item in tree.find(W + "body") if item.tag == W + "p"]
                self.assertIn("附注", paragraph_text(paragraphs[-1]))
                if line.startswith("##"):
                    self.assertEqual(paragraphs[-1].find(".//" + W + "pStyle").get(W + "val"), "Heading1")

    def test_heading_with_pipes_cannot_be_promoted_to_table_header(self):
        tree = self.render("## 独立标题 | 附注\n--- | ---")
        self.assertEqual(tree.findall(".//" + W + "tbl"), [])
        self.assertEqual(tree.find(".//" + W + "pStyle").get(W + "val"), "Heading1")

    def test_fence_with_trailing_text_is_not_a_closing_fence(self):
        for fence in ("```", "~~~"):
            with self.subTest(fence=fence):
                tree = self.render(fence + "text\n" + fence + "still_code\n| A | B |\n| --- | --- |\n| C | D |\n" + fence + "  \n\n| E | F |\n| --- | --- |\n| G | H |")
                tables = tree.findall(".//" + W + "tbl")
                self.assertEqual(len(tables), 1)
                self.assertEqual(paragraph_text(tables[0]), "EFGH")
                self.assertIn("| A | B |", paragraph_text(tree))

    def test_inline_code_keeps_literal_br_while_real_break_still_works(self):
        for code in ("`<br>`", "``<br/>``", "``embedded ` <BR> token``"):
            with self.subTest(code=code):
                tree = self.render("| 字段 | 值 |\n| --- | --- |\n| literal | " + code + "<br>下一行 |")
                cell = tree.findall(".//" + W + "tr")[-1].findall(W + "tc")[1]
                self.assertEqual(paragraph_text(cell), code + "下一行")
                self.assertEqual(len(cell.findall(".//" + W + "br")), 1)

    def test_plain_pipe_lines_and_fenced_or_indented_examples_remain_text(self):
        markdown = "| 原样保留的文字 |\n\n```markdown\n| A | B |\n| --- | --- |\n| C | D |\n```\n\n    | E | F |\n    | --- | --- |\n\n普通段落"
        tree = self.render(markdown)
        self.assertEqual(tree.findall(".//" + W + "tbl"), [])
        self.assertIn("| 原样保留的文字 |", paragraph_text(tree))
        self.assertIn("    | E | F |", paragraph_text(tree))

    def test_malformed_rows_fail_before_output_written(self):
        scenarios = [
            ("| A | B |\n| --- | --- |\n| one | two | three |", "MARKDOWN_TABLE_COLUMN_MISMATCH", 3),
            ("| A | B |\n| --- |", "MARKDOWN_TABLE_COLUMN_MISMATCH", 2),
            ("| A | B |\n| -- | --- |", "MARKDOWN_TABLE_INVALID_DELIMITER", 2),
            ("| A | B |\n| --- | |", "MARKDOWN_TABLE_INVALID_DELIMITER", 2),
        ]
        for markdown, code, line in scenarios:
            with self.subTest(markdown=markdown):
                with self.assertRaises(LegalCaseError) as raised:
                    self.render(markdown)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.details["line"], line)
                self.assertFalse((self.root / "TEST-ONLY-table.docx").exists())

    def test_nine_columns_require_explicit_layout(self):
        with self.assertRaises(LegalCaseError) as raised:
            self.render("| " + " | ".join("ABCDEFGHI") + " |\n| " + " | ".join(["---"] * 9) + " |")
        self.assertEqual(raised.exception.code, "MARKDOWN_TABLE_TOO_WIDE")


if __name__ == "__main__":
    unittest.main()
