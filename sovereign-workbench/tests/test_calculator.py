"""The calculator: restricted evaluation, argument tolerance, input verification."""
from __future__ import annotations

import pytest

from sovereign import db
from sovereign.tools.calculator import coerce_inputs, evaluate, verify_inputs


class TestEvaluation:
    def test_arithmetic_with_named_inputs(self):
        r = evaluate("design - operating", {"design": 16, "operating": 12},
                     unit="bar")
        assert r["ok"] and r["result"] == 4.0
        assert r["substituted"] == "16 - 12"
        assert r["steps"] == ["16 - 12 = 4"]

    def test_float_noise_is_rounded_away(self):
        # 1.2/3 is 0.40000000000000036 in binary floating point, which would then
        # fail to match the "0.4" a document states.
        r = evaluate("(a - b) / c", {"a": 10.4, "b": 9.2, "c": 3})
        assert r["result"] == 0.4

    @pytest.mark.parametrize("expr", [
        '__import__("os").system("id")',
        "open('/etc/passwd').read()",
        "eval('1+1')",
        "[].__class__.__mro__",
        "lambda: 1",
    ])
    def test_non_arithmetic_expressions_are_refused(self, expr):
        assert not evaluate(expr, {})["ok"]

    def test_division_by_zero_is_reported_not_raised(self):
        r = evaluate("1/0", {})
        assert not r["ok"] and "zero" in r["error"].lower()

    def test_unbound_symbols_are_reported(self):
        r = evaluate("a + b", {"a": 1})
        assert not r["ok"] and "b" in r["error"]

    def test_whitelisted_maths_functions_work(self):
        assert evaluate("sqrt(16)", {})["result"] == 4.0
        assert evaluate("max(3, 7)", {})["result"] == 7.0

    def test_a_trace_row_is_always_written(self, task_id):
        evaluate("2 + 2", {}, task_id=task_id, label="t")
        assert db.query("SELECT * FROM calculations WHERE task_id=?", (task_id,))


class TestArgumentTolerance:
    """Models routinely put provenance into `inputs`. Hard-failing on that
    produces a retry loop in which the model changes everything except the
    mistake, so the arguments are repaired and the correction reported."""

    def test_a_leading_number_is_recovered_from_a_citation_string(self):
        vals, prov, notes = coerce_inputs({"t": "10.4 mm (IR-2026-0731, page 1)"})
        assert vals["t"] == 10.4
        assert "IR-2026-0731" in prov["t"]
        assert notes

    def test_a_nested_object_yields_its_value_and_its_source(self):
        vals, prov, _ = coerce_inputs({"t": {"value": 9.2, "source": "IR p.1"}})
        assert vals["t"] == 9.2 and prov["t"] == "IR p.1"

    def test_a_pure_citation_becomes_provenance_not_a_value(self):
        # "IR-2026-0731 p.1" is full of digits, none of which are the value.
        vals, prov, _ = coerce_inputs({"t": "IR-2026-0731 p.1"})
        assert "t" not in vals and prov["t"] == "IR-2026-0731 p.1"

    def test_end_to_end_recovery(self):
        r = evaluate("(t_prev - t_cur) / years",
                     {"t_prev": "12.0 mm (IR p.1)",
                      "t_cur": {"value": 10.4, "source": "IR p.1"},
                      "years": "3 years"})
        assert r["ok"] and abs(r["result"] - 0.533333333) < 1e-6


class TestInputVerification:
    def _evidence(self, tid, snippet):
        db.insert("evidence", {
            "id": db.new_id("ev"), "task_id": tid, "doc_id": "d", "page_no": 1,
            "snippet": snippet, "score": 1.0, "created_at": db.now()})

    def test_established_inputs_verify(self, task_id):
        self._evidence(task_id, "Nominal Thickness 12.0 mm Recorded 10.4 mm years 3")
        r = evaluate("(12.0 - 10.4) / 3", {}, task_id=task_id)
        assert r["inputs_verified"] and not r["unverified_inputs"]

    def test_an_invented_literal_is_caught(self, task_id):
        self._evidence(task_id, "Nominal Thickness 12.0 mm Recorded 10.4 mm years 3")
        r = evaluate("(12.4 - 10.4) / 3", {}, task_id=task_id)
        assert not r["inputs_verified"]
        assert any("12.4" in u for u in r["unverified_inputs"])

    def test_an_earlier_result_counts_as_established(self, task_id):
        self._evidence(task_id, "Design 16 bar Operating 12 bar")
        first = evaluate("16 - 12", {}, task_id=task_id)
        assert first["inputs_verified"]
        # 16 is in the evidence, 4 is the result of the calculation above.
        second = evaluate("16 - 4", {}, task_id=task_id)
        assert second["inputs_verified"], "a prior verified result was not accepted"

    def test_without_a_task_nothing_is_checked(self):
        verdicts, unverified = verify_inputs({"a": 1.0}, None)
        assert verdicts == {"a": True} and unverified == []
