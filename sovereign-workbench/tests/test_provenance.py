"""The four evidence classes, the number rule, and provenance propagation."""
from __future__ import annotations

import pytest

from sovereign import db
from sovereign.evidence import provenance as pv

SOURCE = [{"doc_title": "IR-2026-0731", "page_no": 1, "chunk_id": "c1",
           "text": "Design Pressure : 16 bar g  Operating Pressure : 12 bar g  "
                   "Minimum Required Thick : 7.5 mm  Nominal Thickness : 12.0 mm"}]


def ctx(calcs=None) -> pv.EvidenceContext:
    return pv.EvidenceContext(passages=SOURCE, calculations=calcs or [])


def classes(text, calcs=None) -> dict[str, str]:
    rep = pv.classify_text(text, ctx(calcs), persist=False)
    return {v.mention.raw: v.ev_class for v in rep.verdicts}


class TestClassification:
    def test_sourced_values_are_class_a(self):
        c = classes("The design pressure is 16 bar and operating is 12 bar.")
        assert c["16 bar"] == "A" and c["12 bar"] == "A"

    def test_calculated_values_are_class_b(self):
        calcs = [{"id": "CALC-1", "expression": "16 - 12", "result": 4.0,
                  "steps": {"inputs_verified": True}}]
        assert classes("The margin is 4 bar.", calcs)["4 bar"] == "B"

    def test_hedged_values_are_class_c(self):
        c = classes("The findings may indicate a remaining life of about 6.1 years.")
        assert c["6.1 years"] == "C"

    def test_unsupported_values_are_class_d(self):
        assert classes("The corrosion allowance is 3.7 mm.")["3.7 mm"] == "D"

    @pytest.mark.parametrize("text", [
        "Refer to clause 4.4 of the procedure.",
        "SOP-MECH-014 Rev 4 applies.",
        "Dated 31 July 2026.",
        "See page 12.",
        "Report No. IR-2026-0731.",
    ])
    def test_structural_references_are_not_treated_as_claims(self, text):
        """Flagging clause and document numbers would make the checker cry wolf
        until an operator switched it off."""
        assert pv.classify_text(text, ctx(), persist=False).verdicts == []

    def test_a_value_split_by_a_sentence_stop_is_still_found(self):
        # "4 bar." — the trailing full stop must not swallow the unit.
        calcs = [{"id": "C", "expression": "16-12", "result": 4.0,
                  "steps": {"inputs_verified": True}}]
        assert classes("The margin is 4 bar.", calcs)["4 bar"] == "B"


class TestPropagation:
    def test_a_calculation_over_an_unverified_input_does_not_confer_class_b(self):
        """Otherwise the calculator launders an invented number into a trusted one."""
        calcs = [{"id": "CALC-BAD", "expression": "(12.4 - 10.4) / 3",
                  "result": 0.666667,
                  "steps": {"inputs_verified": False,
                            "unverified_inputs": ["literal[0]=12.4"]}}]
        c = classes("The corrosion rate is 0.666667 mm/yr.", calcs)
        assert c["0.666667 mm"] == "D"

    def test_the_rationale_names_the_offending_input(self):
        calcs = [{"id": "CALC-BAD", "expression": "x", "result": 0.666667,
                  "steps": {"inputs_verified": False,
                            "unverified_inputs": ["literal[0]=12.4"]}}]
        rep = pv.classify_text("Rate 0.666667 mm/yr.", ctx(calcs), persist=False)
        assert "12.4" in rep.verdicts[0].rationale


class TestEnforcement:
    def test_redaction_replaces_the_value_with_a_refusal(self):
        text = "The corrosion allowance is 3.7 mm."
        rep = pv.classify_text(text, ctx(), persist=False)
        out = pv.redact_unsupported(text, rep)
        assert "CANNOT DETERMINE" in out and "3.7" not in out

    def test_a_clean_report_is_left_untouched(self):
        text = "The design pressure is 16 bar."
        rep = pv.classify_text(text, ctx(), persist=False)
        assert pv.redact_unsupported(text, rep) == text

    def test_annotation_marks_each_value_with_its_class(self):
        rep = pv.classify_text("Design pressure 16 bar.", ctx(), persist=False)
        assert "[A]" in pv.annotate("Design pressure 16 bar.", rep)


def test_claims_are_persisted_with_their_class(task_id):
    pv.classify_text("The corrosion allowance is 3.7 mm.", ctx(), task_id=task_id)
    rows = db.query("SELECT ev_class FROM claims WHERE task_id=?", (task_id,))
    assert any(r["ev_class"] == "D" for r in rows)
