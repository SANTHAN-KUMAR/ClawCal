"""Workspace confinement, and the quality of its refusals.

Confining agents to a per-task workspace is a security requirement. Refusing
*informatively* is a usability requirement, and the two are easy to get wrong
together: an agent asked for `/home/user/Downloads` once received
`ok=true, "workspace is empty"` and concluded the user's file did not exist.
The refusal has to be a refusal, and it has to say where the documents are.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sovereign import db
from sovereign.config import WORKSPACE_DIR
from sovereign.tools.base import ToolContext
from sovereign.tools.files import WorkspaceEscape, resolve
from sovereign.tools import register_all


@pytest.fixture()
def ctx(task_id):
    ws = WORKSPACE_DIR / task_id
    ws.mkdir(parents=True, exist_ok=True)
    return ToolContext(task_id=task_id, workspace=ws)


@pytest.fixture(scope="module")
def tools():
    return register_all()


class TestPathConfinement:
    @pytest.mark.parametrize("bad", [
        "/etc/passwd", "/home/someone/Downloads/", "/", "~/Downloads",
        "../../etc/shadow", "sub/../../../etc/hosts",
    ])
    def test_paths_outside_the_workspace_are_refused(self, bad, tmp_path):
        with pytest.raises(WorkspaceEscape):
            resolve(tmp_path, bad)

    def test_an_absolute_path_is_not_silently_reinterpreted(self, tmp_path):
        """Stripping the leading slash turns /home/user/Downloads into
        <workspace>/home/user/Downloads, which then simply does not exist — an
        empty result where an answerable error belonged."""
        with pytest.raises(WorkspaceEscape) as exc:
            resolve(tmp_path, "/home/user/Downloads/")
        assert "absolute host path" in str(exc.value)

    @pytest.mark.parametrize("good", ["notes.txt", "sub/dir/file.py", "."])
    def test_relative_paths_inside_the_workspace_resolve(self, good, tmp_path):
        p = resolve(tmp_path, good)
        assert tmp_path.resolve() == p or tmp_path.resolve() in p.parents

    def test_a_symlink_out_of_the_workspace_is_refused(self, tmp_path):
        (tmp_path / "escape").symlink_to("/etc")
        with pytest.raises(WorkspaceEscape):
            resolve(tmp_path, "escape/passwd")


class TestRefusalsAreActionable:
    def test_listing_a_host_path_fails_and_names_the_alternative(self, tools, ctx):
        r = tools.invoke("list_files", {"subdir": "/home/santhankumar/Downloads/"},
                         ctx)
        assert not r.ok, "a host path returned success"
        for hint in ("list_documents", "search_knowledge", "cannot read the host"):
            assert hint in r.error, f"the refusal does not mention {hint!r}"

    def test_an_empty_workspace_points_at_the_document_index(self, tools, ctx):
        r = tools.invoke("list_files", {}, ctx)
        assert r.ok
        body = r.for_model()
        assert "empty" in body
        assert "list_documents" in body, (
            "an empty workspace told the agent nothing about where documents are")

    def test_reading_a_missing_file_names_the_alternative(self, tools, ctx):
        r = tools.invoke("read_file", {"path": "nope.txt"}, ctx)
        assert not r.ok and "list_documents" in r.error

    def test_a_written_file_is_listed_and_readable(self, tools, ctx):
        w = tools.invoke("write_file", {"path": "a/b.txt", "content": "hello"}, ctx)
        assert w.ok, w.error
        listing = tools.invoke("list_files", {}, ctx)
        assert any(e["path"] == "a/b.txt" for e in listing.content)
        r = tools.invoke("read_file", {"path": "a/b.txt"}, ctx)
        assert r.ok and r.content == "hello"

    def test_writing_outside_the_workspace_is_refused(self, tools, ctx):
        r = tools.invoke("write_file", {"path": "/tmp/escape.txt", "content": "x"},
                         ctx)
        assert not r.ok
        assert not Path("/tmp/escape.txt").exists()


class TestAgentGuidance:
    def test_the_system_prompt_explains_how_documents_are_reached(self):
        """The model has to know this before it reaches for a path, not after."""
        from sovereign.agent.prompts import system_prompt
        p = system_prompt("summarisation")
        assert "list_documents" in p and "cannot read the host filesystem" in p
        assert "do not report that" in p.lower() or "very likely" in p.lower()


class TestOfficeIngestion:
    """Spreadsheets, documents and decks carry their text natively. Rejecting
    them by extension made "spreadsheet work" a file-type error."""

    def _xlsx(self, tmp_path):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Register"
        ws.append(["Tag", "Design P (bar g)", "Service"])
        ws.append(["V-204", 16, "Reflux accumulator"])
        ws.append(["E-301", 14, "Overhead condenser"])
        p = tmp_path / "register.xlsx"
        wb.save(p)
        return p

    def test_a_workbook_is_read_with_labelled_cells(self, tmp_path):
        from sovereign.knowledge import ocr
        pages = ocr.extract_xlsx(self._xlsx(tmp_path))
        assert pages and pages[0].extractor == "xlsx"
        text = pages[0].text
        assert "SHEET: Register" in text
        # Labelling each value with its column is what lets the value extractor
        # and a human reader tell which number is which.
        assert "Tag: V-204" in text and "Design P (bar g): 16" in text

    def test_a_word_document_is_read(self, tmp_path):
        from docx import Document
        from sovereign.knowledge import ocr
        d = Document()
        d.add_paragraph("Approval note for V-204.")
        t = d.add_table(rows=2, cols=2)
        t.cell(0, 0).text = "Parameter"; t.cell(0, 1).text = "Value"
        t.cell(1, 0).text = "Design pressure"; t.cell(1, 1).text = "16 bar g"
        p = tmp_path / "note.docx"
        d.save(p)
        pages = ocr.extract_docx(p)
        assert pages and "Approval note for V-204." in pages[0].text
        assert "Design pressure: 16 bar g" in pages[0].text

    def test_a_deck_is_read_one_page_per_slide(self, tmp_path):
        from pptx import Presentation
        from pptx.util import Inches
        from sovereign.knowledge import ocr
        prs = Presentation()
        for heading in ("Findings", "Recommendation"):
            s = prs.slides.add_slide(prs.slide_layouts[6])
            box = s.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
            box.text_frame.text = heading
        p = tmp_path / "deck.pptx"
        prs.save(p)
        pages = ocr.extract_pptx(p)
        assert len(pages) == 2
        assert "Findings" in pages[0].text and "Recommendation" in pages[1].text

    def test_an_unsupported_type_names_what_is_supported(self, tmp_path):
        from sovereign.knowledge import ingest
        bad = tmp_path / "legacy.xls"
        bad.write_bytes(b"not really an xls")
        with pytest.raises(ValueError) as exc:
            ingest.ingest_file(bad)
        msg = str(exc.value)
        assert ".xlsx" in msg and "libreoffice" in msg.lower()

    def test_a_failed_ingest_does_not_block_a_retry(self, tmp_path):
        """A failed attempt used to leave a row whose content hash matched the
        file forever, so the same file could never be ingested again."""
        from sovereign.knowledge import ingest
        f = tmp_path / "note.txt"
        f.write_text("Design Pressure : 16 bar g")
        digest = ingest.sha256_file(f)
        db.insert("documents", {
            "id": db.new_id("doc"), "title": "poisoned", "kind": "txt",
            "doc_class": "other", "path": str(f), "sha256": digest,
            "pages": 0, "status": "FAILED", "created_at": db.now()})
        r = ingest.ingest_file(f, title="retry")
        assert not r.get("reused"), "the failed row blocked the retry"
        assert r["status"] == "READY" and r["chunks"] >= 1


class TestNoAttachmentHandling:
    def test_a_request_for_an_attachment_that_is_absent_is_detected(self):
        """With nothing attached, a model will otherwise answer about the most
        plausible indexed document — observed summarising an inspection report
        the user never mentioned, with every value correctly cited."""
        from sovereign.agent.harness import AgentHarness
        for prompt in ("summarize the attached doc", "summarize this doc bro",
                       "what does the uploaded pdf say"):
            assert AgentHarness._DEICTIC.search(prompt), prompt

    def test_an_ordinary_request_is_not_flagged(self):
        from sovereign.agent.harness import AgentHarness
        for prompt in ("What is the operating margin of V-204?",
                       "Prepare an approval note for V-204 from IR-2026-0731"):
            assert not AgentHarness._DEICTIC.search(prompt), prompt


class TestAttachmentIsUsed:
    """An attached document must be opened, not looked for.

    Told only to "work on these documents", the agent called `list_files` and
    then `list_documents` and answered with the document *catalogue* — a fluent,
    correct-looking reply that never opened the file the operator attached.
    """

    def _harness(self, attachments, prompt="summarize what's there in this file"):
        from sovereign.agent.harness import AgentHarness
        from sovereign.runtime.control import TaskControl
        task = {"id": db.new_id("t"), "prompt": prompt, "task_type": "summarisation",
                "workflow": "general", "attachments": db.jdump(attachments),
                "selected_model": "qwen3-8b", "context_budget": 8192}
        return AgentHarness(task, TaskControl(task_id=task["id"], priority="LOW"))

    def test_the_opening_message_names_the_exact_call_to_make(self):
        h = self._harness([{"doc_id": "doc-abc123", "title": "register.xlsx",
                            "pages": 2, "kind": "document"}])
        msg = h._opening_message()
        assert "doc-abc123" in msg
        assert "read_document_page" in msg
        assert "do not call list_files" in msg.lower()

    def test_a_drawing_attachment_points_at_the_drawing_tool(self):
        h = self._harness([{"doc_id": "doc-d", "title": "PID-204-01",
                            "pages": 1, "kind": "drawing"}])
        assert "analyse_drawing" in h._opening_message()

    def test_workspace_browsing_is_withdrawn_when_a_document_is_attached(self):
        """Leaving the workspace browser in the tool set invites the model to go
        looking for a document it has already been handed."""
        from sovereign.workflows.runners import TOOLSETS
        granted = list(TOOLSETS["general"])
        assert "list_files" in granted, "precondition: general grants list_files"

        task = {"workflow": "general",
                "attachments": db.jdump([{"doc_id": "doc-x", "title": "t"}])}
        tools = [t for t in TOOLSETS["general"] if t not in ("list_files", "read_file")] \
            if db.jload(task["attachments"], []) else granted
        assert "list_files" not in tools and "read_document_page" in tools
