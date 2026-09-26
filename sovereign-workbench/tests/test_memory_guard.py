"""Host memory is priced before any model loads.

Regression tests for the OOM found on the target machine: a sovereignty
self-test (which uses no model) was admitted to a 12 GB model, the residency
plan compared only its VRAM share with free VRAM, and ~5 GB spilled into a host
with ~5.7 GB available. systemd-oomd then killed the desktop.
"""
from __future__ import annotations

from sovereign import hardware
from sovereign.gateway.registry import ModelCard


def _card(weights: int, vram: int = 0) -> ModelCard:
    return ModelCard(name="big", backend="ollama", backend_ref="big:1",
                     weights_mb=weights, est_vram_mb=vram or weights,
                     caps={"text": 1.0})


def _snap(available: float, total: float = 16000.0) -> dict:
    # `_no_pressure` keeps these unit tests independent of the host's live PSI.
    return {"memory": {"available_mb": available, "total_mb": total},
            "_no_pressure": True}


def test_a_part_offloaded_model_is_refused_when_its_spill_does_not_fit():
    card = _card(12100, 7200)                     # gpt-oss-20b's real shape
    ok, why, need, room = hardware.memory_verdict(card, 7465, _snap(5700))
    assert not ok and need > room
    assert "OOM" in why and "host RAM" in why


def test_the_same_model_fits_on_a_host_with_room():
    # Room for the whole file during a non-mmap load, plus the reserve.
    ok, *_ = hardware.memory_verdict(_card(12100, 7200), 7465, _snap(20000, total=32000))
    assert ok


def test_a_model_that_fits_vram_still_needs_its_weights_in_ram_while_loading():
    # The crash: qwen3-8b (5.2 GB) fits VRAM whole, but a non-mmap load reads it
    # all into host RAM first.
    need = hardware.host_ram_need_mb(_card(5200, 5951), vram_available_mb=7400)
    assert need >= 5200 + hardware.RUNNER_OVERHEAD_MB
    ok, why, *_ = hardware.memory_verdict(_card(5200, 5951), 7400, _snap(6200))
    assert not ok                                   # 6.2 GB free minus the reserve


def test_a_declared_mmap_backend_budgets_only_the_spill(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_BACKEND_MMAP", "1")
    need = hardware.host_ram_need_mb(_card(3300, 4200), vram_available_mb=7000)
    assert need == hardware.RUNNER_OVERHEAD_MB


def test_memory_pressure_refuses_any_load(monkeypatch):
    monkeypatch.setattr(hardware, "memory_pressure", lambda: "swap is 100% used")
    ok, why, *_ = hardware.memory_verdict(
        _card(1000, 1200), 7000, {"memory": {"available_mb": 12000, "total_mb": 16000}})
    assert not ok and "swap" in why


def test_the_reserve_scales_with_the_machine():
    assert hardware.ram_reserve_mb(16000) >= 1920          # 12% of RAM
    assert hardware.ram_reserve_mb(256000) >= 30000        # a server keeps more


def test_a_model_free_workflow_is_admitted_without_routing(monkeypatch):
    from sovereign.runtime.scheduler import scheduler
    task = {"id": "t-selftest", "workflow": "sovereignty_proof",
            "task_type": "general", "priority": "HIGH", "owner": "x",
            "est_context_tokens": 4000, "title": "self-test"}
    d = scheduler.evaluate(task)
    assert d.outcome == "ADMIT" and d.model == ""
    assert "no model" in d.reason


def test_a_backend_memory_refusal_is_learned_and_does_not_trip_the_breaker():
    from sovereign import db
    from sovereign.gateway import gateway as gw
    from sovereign.gateway.gateway import _backend_memory_refusal, _learn_footprint
    from sovereign.gateway.registry import registry
    err = ("model requires more system memory (10.3 GiB) than is available (10.2 GiB)")
    assert round(_backend_memory_refusal(err)) == 10547
    assert _backend_memory_refusal("connection reset") is None
    registry.seed()
    card = registry.get("qwen2.5vl-7b")
    _learn_footprint(card, _backend_memory_refusal(err))
    learned = registry.get("qwen2.5vl-7b")
    assert learned.est_vram_mb == 10547
    row = db.query_one("SELECT edited_by FROM model_registry WHERE name='qwen2.5vl-7b'")
    assert row["edited_by"] == "backend-measured"
    registry.seed()                                   # the catalogue must not undo it
    assert registry.get("qwen2.5vl-7b").est_vram_mb == 10547


def test_two_reader_agreement_and_illegibility():
    from sovereign.knowledge import ocr
    same = ocr.agreement("Design pressure 16 bar g, wall 12.0 mm",
                         "Design pressure: 16 bar g. Wall 12.0 mm")
    assert same["words"] >= 0.75 and same["numbers"] == 1.0
    digit_slip = ocr.agreement("wall 12.0 mm pressure 16", "wall 12.8 mm pressure 16")
    assert digit_slip["numbers"] < ocr.AGREE_MIN_NUMBERS     # one wrong digit fails
    assert ocr._mostly_illegible("[illegible] [illegible] total [illegible] 4")
    assert not ocr._mostly_illegible("Total 60.000 [illegible] cash 100.000")


def test_a_catalogue_correction_reaches_an_existing_install():
    from sovereign import db
    from sovereign.gateway.registry import registry
    registry.seed()
    db.update("model_registry", "name", "qwen3-vl-8b", {"backend_ref": "qwen3-vl:8b"})
    registry.invalidate()
    registry.seed()                       # the shipped ref must win over the stale one
    assert registry.get("qwen3-vl-8b").backend_ref == "qwen3-vl:8b-instruct"


def test_degenerate_vlm_output_is_not_a_transcription():
    from sovereign.knowledge import ocr
    assert ocr.degenerate("The image is too blurry to read the text.")
    loop = "ITEM 1 PRICE 100 " * 60
    assert "repeats" in (ocr.degenerate(loop) or "")
    real = ("ATT. GEN. ADMIN. OFFICE Fax: (614) 466-5087 Dec 10 '98 17:46 "
            "Attorney General Betty D. Montgomery. FAX COVER SHEET. DATE: "
            "December 10, 1998. TO: George Baroody. FROM: Carol Nelson.") * 2
    assert ocr.degenerate(real) is None


def test_agreement_catches_values_one_reader_dropped():
    from sovereign.knowledge import ocr
    dropped = ocr.agreement("Sub Total 36000 Tunai Kembalian",
                            "Sub Total 36000 Tunai 50000 Kembalian 14000")
    assert dropped["numbers"] < ocr.AGREE_MIN_NUMBERS


def test_vram_held_by_no_resident_model_is_transient_not_permanent(monkeypatch):
    from sovereign.gateway.registry import ModelCard
    import importlib
    R = importlib.import_module("sovereign.runtime.residency")
    mgr = R.ResidencyManager()
    monkeypatch.setattr(mgr, "refresh", lambda: {})          # backend lists nothing...
    monkeypatch.setattr(R.hardware, "snapshot", lambda: {    # ...but the GPU is busy
        "usable_vram_mb": 7488.0, "free_vram_mb": 3900.0,
        "gpu": {"used_mb": 3600.0, "total_mb": 8188.0},
        "memory": {"available_mb": 9000.0, "total_mb": 16000.0}})
    card = ModelCard(name="m8", backend="ollama", backend_ref="m8", est_vram_mb=5951,
                     weights_mb=5200, caps={"text": 1})
    plan = mgr.plan(card)
    assert not plan.feasible and plan.transient and "unload" in plan.reason


def test_a_text_model_never_falls_back_to_a_vision_model():
    from sovereign.gateway import gateway as gw
    from sovereign.gateway.registry import registry
    registry.seed()
    chain = gw.fallback_chain(registry.get("qwen3-8b"))
    assert all(c.modality != "vision" for c in chain)
