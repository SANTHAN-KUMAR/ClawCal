"""A weaker model writes tool calls as text; the harness must still act on them."""
from __future__ import annotations

from sovereign.agent.harness import recover_prose_call
from sovereign.tools import register_all

TOOLS = register_all()
ALLOWED = {"spreadsheet_read", "extract_document_values", "read_document_page",
           "search_knowledge", "calculator"}


def test_a_named_call_written_as_a_plan_is_recovered():
    # Verbatim shape of a live qwen3-4b reply that ended the task with a plan.
    text = ('1. `spreadsheet_read with {"action": "sheets", "workbook": "doc-bf902f7d5332"}`\n'
            '2. `extract_document_values` with `{"doc_id": "doc-bf902f7d5332"}`')
    got = recover_prose_call(text, TOOLS, ALLOWED)
    assert got == {"name": "spreadsheet_read",
                   "arguments": {"action": "sheets", "workbook": "doc-bf902f7d5332"}}


def test_bare_arguments_that_fit_one_schema_are_recovered():
    text = ('1. Read the first page of the inspection report:\n\n```json\n'
            '{\n  "doc_id": "doc-315348c1757d",\n  "page_no": 1\n}\n```')
    got = recover_prose_call(text, TOOLS, ALLOWED)
    assert got and got["name"] == "read_document_page"


def test_ambiguous_or_absent_calls_are_not_guessed():
    assert recover_prose_call("The margin is 4 bar.", TOOLS, ALLOWED) is None
    assert recover_prose_call('Result: {"value": 4}', TOOLS, ALLOWED) is None
    # a tool the task was not granted is never recovered
    assert recover_prose_call('run_code {"code": "print(1)"}', TOOLS, ALLOWED) is None
