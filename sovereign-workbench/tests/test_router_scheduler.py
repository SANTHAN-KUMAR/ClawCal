"""Task classification, capability/residency routing, and admission."""
from __future__ import annotations

import pytest

from sovereign import router
from sovereign.gateway.registry import registry


@pytest.fixture(scope="module", autouse=True)
def _seed():
    registry.seed()


class TestClassification:
    @pytest.mark.parametrize("prompt,expected", [
        ("Prepare an approval note based on this inspection report",
         "deliverable_drafting"),
        ("Fix this internal Python tool, the pytest suite fails", "coding"),
        ("Check whether V-204 exceeds its permitted operating pressure",
         "engineering_analysis"),
        ("Summarise these reports for the digest", "summarisation"),
        ("Process 50 inspection reports and prepare summaries", "summarisation"),
        ("According to the SOP, which clause applies?", "knowledge_qa"),
    ])
    def test_task_types(self, prompt, expected):
        assert router.classify(prompt).task_type == expected

    def test_a_drawing_attachment_overrides_the_text(self):
        c = router.classify("What does this show?",
                            attachments=[{"kind": "drawing", "pages": 1}])
        assert c.task_type == "drawing_analysis"

    def test_an_image_attachment_forces_a_vision_task(self):
        c = router.classify("Extract the findings",
                            attachments=[{"kind": "image", "pages": 1}])
        assert c.task_type == "vision_understanding"

    @pytest.mark.parametrize("prompt,priority", [
        ("Emergency: check the vessel for a leak", "CRITICAL"),
        ("Process this batch overnight", "BATCH"),
    ])
    def test_priority_is_inferred(self, prompt, priority):
        assert router.classify(prompt).priority == priority

    def test_every_decision_carries_its_signals(self):
        c = router.classify("Fix the failing pytest urgently")
        assert c.signals and any("matched" in s for s in c.signals)

    def test_classification_is_deterministic(self):
        a = router.classify("Prepare an approval note for V-204")
        b = router.classify("Prepare an approval note for V-204")
        assert (a.task_type, a.priority, a.est_context_tokens) == \
               (b.task_type, b.priority, b.est_context_tokens)


class TestSelection:
    def test_a_vision_task_requires_a_vision_model(self):
        c = router.classify("Read this scan", attachments=[{"kind": "image"}])
        d = router.select_model(c, resident=[])
        assert registry.get(d.model).cap("vision") >= 0.4

    def test_residency_is_preferred_when_the_gap_is_small(self):
        c = router.classify("Summarise this document for the digest",
                            priority_override="BATCH")
        best = router.select_model(c, resident=[]).model
        alt = next(m.name for m in registry.all()
                   if m.name != best and m.cap("text") > 0.5)
        d = router.select_model(c, resident=[alt])
        assert d.model == alt and d.resident_reuse

    def test_urgent_work_does_not_trade_capability_for_residency(self):
        """A resident but weaker model was observed conflating a pressure with a
        wall thickness on an approval note; urgency must outrank load time."""
        c = router.classify("Prepare an approval note for V-204",
                            priority_override="CRITICAL")
        d = router.select_model(c, resident=["qwen2.5-7b"])
        assert not d.resident_reuse or d.model != "qwen2.5-7b"

    def test_a_model_whose_context_budget_is_too_small_is_excluded(self):
        c = router.classify("Prepare an approval note")
        d = router.select_model(c, resident=[], budget_for=lambda card: 100)
        assert not d.model
        assert "capability" in d.reason or "context" in d.reason

    def test_the_decision_records_why(self):
        d = router.select_model(router.classify("Summarise this"), resident=[])
        assert len(d.reason) > 40
        assert d.considered, "no candidate comparison was recorded"

    def test_excluded_models_are_not_chosen(self):
        c = router.classify("Summarise this")
        first = router.select_model(c, resident=[]).model
        second = router.select_model(c, resident=[], exclude={first}).model
        assert second != first


class TestAdmission:
    def test_priority_ordering_is_total_and_deterministic(self):
        order = [router.PRIORITY_RANK[p] for p in router.PRIORITY_ORDER]
        assert order == sorted(order)
        assert router.PRIORITY_RANK["CRITICAL"] > router.PRIORITY_RANK["BATCH"]

    def test_capability_floors_reject_an_unfit_model(self):
        # The small vision model is fast enough to win on score at BATCH
        # priority, so a floor — not a weight — is what keeps it out of text work.
        card = registry.get("qwen2.5vl-3b")
        for task_type in ("coding", "summarisation", "deliverable_drafting"):
            fit, why = router.capability_fit(card, router.TASK_TYPES[task_type])
            assert fit == 0.0, f"{task_type} accepted the vision model"
            assert "below required floor" in why

    def test_a_capable_model_clears_the_floors(self):
        fit, why = router.capability_fit(registry.get("qwen3-8b"),
                                         router.TASK_TYPES["summarisation"])
        assert why is None and fit > 0.5

    def test_the_vision_model_still_wins_vision_work(self):
        fit, why = router.capability_fit(registry.get("qwen2.5vl-3b"),
                                         router.TASK_TYPES["vision_understanding"])
        assert why is None and fit > 0.5
