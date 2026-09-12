#!/usr/bin/env python3
"""Offline smoke test for deterministic CLI/library operations; uses only TEST-ONLY data."""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI = PROJECT_ROOT / "scripts" / "legal_case_os.py"


def run_cli(*arguments: str, expect: int = 0) -> dict:
    completed = subprocess.run(
        [shutil.which("python") or sys.executable, str(CLI), *arguments, "--json"],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode != expect:
        raise AssertionError(f"CLI exit {completed.returncode}, expected {expect}: {completed.stdout}\n{completed.stderr}")
    return json.loads(completed.stdout)


def main() -> int:
    checks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="legal-case-os-smoke-") as temporary:
        root = Path(temporary)
        workspace = root / "TEST-ONLY-matter"
        initialized = run_cli(
            "init",
            "--workspace",
            str(workspace),
            "--matter-id",
            "M-TEST-SMOKE",
            "--title",
            "TEST-ONLY 虚构案件",
            "--environment",
            "test",
        )
        assert initialized["ok"] and initialized["external_actions_executed"] == 0
        checks.append("init")

        originals = PROJECT_ROOT / "tests" / "fixtures" / "simple-case" / "00-originals"
        for source in originals.iterdir():
            shutil.copy2(source, workspace / "00-originals" / source.name)
        indexed = run_cli("index", "--workspace", str(workspace))
        assert indexed["ok"] and indexed["file_count"] == 4
        checks.append("index")

        validated = run_cli("validate", "--state", str(workspace / "_case-state" / "case-state.json"))
        assert validated["ok"]
        checks.append("validate")

        routed = run_cli("route", "--text", "不要复杂，只说主要问题")
        assert routed["route"]["action"] == "analyze" and routed["safety"]["presentation_only_reduction"]
        checks.append("route")

        data = {
            "plaintiff": "TEST-ONLY 原告（虚构）",
            "defendant": "TEST-ONLY 被告（虚构）",
            "cause_of_action": "TEST-ONLY 买卖合同纠纷",
            "claims": "TEST-ONLY 请求，不得提交。",
            "facts_and_reasons": "TEST-ONLY 虚构事实。",
            "evidence_summary": "TEST-ONLY 虚构证据。",
            "court_name": "TEST-ONLY 法院（虚构）",
            "signature": "TEST-ONLY",
            "filing_date": "2099年1月1日",
        }
        generated_docx = root / "TEST-ONLY-template-output.docx"
        filled = run_cli(
            "template-fill",
            "--template-id",
            "起诉状",
            "--data",
            json.dumps(data, ensure_ascii=False),
            "--output",
            str(generated_docx),
        )
        assert filled["ok"] and zipfile.is_zipfile(generated_docx)
        checks.append("template-fill-docx")

        office = (
            shutil.which("soffice.com")
            or shutil.which("soffice")
            or shutil.which("libreoffice")
            or next(
                (
                    str(candidate)
                    for candidate in (
                        Path(r"C:\Program Files\LibreOffice\program\soffice.com"),
                        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
                    )
                    if candidate.is_file()
                ),
                None,
            )
        )
        generated_pdf = root / "TEST-ONLY-template-output.pdf"
        filled_pdf = run_cli(
            "template-fill",
            "--template-id",
            "起诉状",
            "--data",
            json.dumps(data, ensure_ascii=False),
            "--output",
            str(generated_pdf),
            expect=0 if office else 2,
        )
        if office:
            assert filled_pdf["ok"] and generated_pdf.read_bytes().startswith(b"%PDF-")
            assert filled_pdf["page_count"] >= 1 and filled_pdf["visual_qa_required"]
            checks.append("template-fill-pdf")
        else:
            assert filled_pdf["code"] == "PDF_RENDERER_UNAVAILABLE" and not generated_pdf.exists()
            checks.append("template-fill-pdf-degraded:no-local-office")

        clean_source = PROJECT_ROOT / "tests" / "fixtures" / "document-cleaning" / "TEST-ONLY-cleanable-source.docx"
        approval = root / "TEST-ONLY-sanitization-approval.json"
        with zipfile.ZipFile(clean_source) as source_package:
            part_hash = {
                name: hashlib.sha256(source_package.read(name)).hexdigest()
                for name in ("word/comments.xml", "word/document.xml", "word/header1.xml", "docProps/core.xml")
            }
            document_xml = source_package.read("word/document.xml").decode("utf-8")
        note_text = re.search(r"\[内部备注[:：][^\]]+\]", document_xml).group(0)
        approved_targets = [
            {"item_id": "SAN-TEST-001", "kind": "comments", "location": "word/comments.xml", "source_part_sha256": part_hash["word/comments.xml"], "expected_count": 1},
            {"item_id": "SAN-TEST-002", "kind": "tracked_changes", "location": "word/document.xml", "source_part_sha256": part_hash["word/document.xml"], "expected_count": 2},
            {"item_id": "SAN-TEST-003", "kind": "draft_watermark", "location": "word/header1.xml", "source_part_sha256": part_hash["word/header1.xml"], "value": "DRAFT-TEST-ONLY"},
            {"item_id": "SAN-TEST-004", "kind": "authorized_metadata", "location": "docProps/core.xml", "source_part_sha256": part_hash["docProps/core.xml"], "expected_count": 6},
            {"item_id": "SAN-TEST-005", "kind": "internal_ids", "location": "word/document.xml", "source_part_sha256": part_hash["word/document.xml"], "value": "INT-TEST-CASE-001"},
            {"item_id": "SAN-TEST-006", "kind": "internal_notes", "location": "word/document.xml", "source_part_sha256": part_hash["word/document.xml"], "original_text_sha256": hashlib.sha256(note_text.encode("utf-8")).hexdigest()},
        ]
        for target in approved_targets:
            target["ownership"] = "self_generated"
            target["authority_basis"] = "TEST-ONLY：自有生成稿逐项清洁授权"
        approval_set_sha256 = hashlib.sha256(
            json.dumps(approved_targets, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        approval.write_text(
            json.dumps(
                {
                    "test_only": True,
                    "ownership": "self_generated",
                    "approval_id": "P-TEST-SANITIZE",
                    "approved_by": "TEST-ONLY 律师",
                    "approved_at": "2099-01-01T00:00:00Z",
                    "independent_review_id": "R-TEST-PRE-REVIEW",
                    "pre_review_status": "passed",
                    "approved_items": [
                        "comments",
                        "tracked_changes",
                        "draft_watermark",
                        "authorized_metadata",
                        "internal_ids",
                        "internal_notes",
                    ],
                    "approved_targets": approved_targets,
                    "approval_set_sha256": approval_set_sha256,
                    "authorized_watermarks": ["DRAFT-TEST-ONLY", "DRAFT"],
                    "approved_internal_ids": ["INT-TEST-CASE-001"],
                    "source_artifact_id": "R-TEST-SANITIZE-SOURCE",
                    "derived_artifact_id": "R-TEST-SANITIZE-DERIVED",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        clean_output = root / "TEST-ONLY-cleaned.docx"
        sanitized = run_cli(
            "sanitize-docx",
            "--source",
            str(clean_source),
            "--approved-items",
            str(approval),
            "--output",
            str(clean_output),
        )
        assert sanitized["ok"] and sanitized["original_hash_unchanged"] and zipfile.is_zipfile(clean_output)
        assert sanitized["rereview_status"] == "required"
        postflight = run_cli("preflight", "--path", str(clean_output), expect=0 if not sanitized["blocked_items"] else 2)
        assert not {"comments", "tracked_changes", "internal_id", "internal_note"} & {
            finding["kind"] for finding in postflight["findings"]
        }
        checks.append("sanitize-docx")

        if office:
            render_dir = root / "rendered"
            render_dir.mkdir()
            office_profile = (root / "libreoffice-profile").as_posix()
            rendered = subprocess.run(
                [
                    office,
                    f"-env:UserInstallation=file:///{office_profile}",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(render_dir),
                    str(clean_output),
                ],
                cwd=root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            rendered_pdf = render_dir / f"{clean_output.stem}.pdf"
            if rendered.returncode != 0 or not rendered_pdf.is_file() or not rendered_pdf.read_bytes().startswith(b"%PDF"):
                raise AssertionError(f"Sanitized DOCX render failed: {rendered.stdout}\n{rendered.stderr}")
            checks.append("sanitize-docx-render")
        else:
            checks.append("sanitize-docx-render-degraded:no-local-office")

    print(json.dumps({"ok": True, "checks": checks, "external_actions_executed": 0}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
