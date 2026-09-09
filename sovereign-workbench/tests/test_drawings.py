"""Engineering-drawing understanding, scored against ground truth."""
from __future__ import annotations

import json

import pytest

from sovereign.drawings import analyze, graph
from sovereign.drawings.model import (CONFIRMED, PROBABLE, UNRESOLVED, BBox,
                                      Polyline, Symbol)
from sovereign.drawings.tags import classify_prefix, recognise


@pytest.fixture(scope="module")
def vector(corpus_dir):
    return analyze.analyse_pdf(corpus_dir / "drawings/PID-204-01.pdf",
                               title="PID-204-01", use_vlm=False)


@pytest.fixture(scope="module")
def truth(corpus_dir):
    return json.loads((corpus_dir / "drawings/PID-204-01.truth.json").read_text())


class TestVectorPath:
    def test_the_vector_path_is_taken_for_a_cad_pdf(self, vector):
        assert vector.source_kind == "vector"

    def test_tags_are_recovered_exactly(self, vector, truth):
        score = analyze.score_against_truth(vector, truth)
        assert score["tags"]["f1"] == 1.0, score["tags"]

    def test_connectivity_is_recovered_exactly(self, vector, truth):
        score = analyze.score_against_truth(vector, truth)
        assert score["connectivity"]["f1"] == 1.0, score["connectivity"]

    def test_title_block_and_legend_are_not_equipment(self, vector):
        """Both are rectangles, exactly like a drum. What separates them is that
        they enclose text."""
        for s in vector.symbols:
            assert s.tag, f"an untagged shape survived as equipment: {s.label}"

    def test_instrument_leads_are_not_process_connections(self, vector):
        signals = [e for e in vector.edges if e.line_type == "instrument_signal"]
        assert signals, "dashed instrument leads were not distinguished"
        for e in signals:
            src = vector.symbol(e.src)
            dst = vector.symbol(e.dst)
            assert "instrument" in (src.sym_class, dst.sym_class)

    def test_a_relief_valve_is_reclassified_from_its_tag(self, vector):
        """A PSV and a manual valve are both drawn as a bowtie; only the tag
        distinguishes them."""
        psv = next(s for s in vector.symbols if s.tag == "PSV-204A")
        assert psv.sym_class == "relief_valve"
        assert any("reclassified" in p for p in psv.primitives)


class TestHonesty:
    def test_uncertainty_is_reported_rather_than_resolved(self, vector):
        statuses = {e.status for e in vector.edges}
        assert CONFIRMED in statuses
        assert (UNRESOLVED in statuses or PROBABLE in statuses or vector.warnings), \
            "the analysis claims total certainty"

    def test_a_dangling_line_is_reported_not_dropped(self, vector):
        # The PSV vents to atmosphere; that line has no second symbol.
        unresolved = [e for e in vector.edges if e.status == UNRESOLVED]
        assert unresolved
        assert all(e.rationale for e in unresolved)

    def test_absent_equipment_produces_a_refusal(self, vector):
        r = graph.trace_path(vector.symbols, vector.edges, "C-201", "P-999")
        assert not r["found"] and r["status"] == "CANNOT DETERMINE"
        assert "P-999" in r["reason"]

    def test_a_real_multi_hop_path_is_traced(self, vector):
        r = graph.trace_path(vector.symbols, vector.edges, "C-201", "P-101B")
        assert r["found"] and r["status"] == CONFIRMED
        assert "E-301" in r["path"] and "V-204" in r["path"]

    def test_the_raster_path_never_confirms_connectivity(self, corpus_dir):
        a = analyze.analyse_image(corpus_dir / "drawings/PID-204-01-scan.png",
                                  title="scan", use_vlm=False)
        assert a.summary()["confirmed_connectivity"] == 0
        assert any("raster" in w for w in a.warnings)


class TestGeometryKernel:
    def test_chaining_is_linear_not_quadratic(self):
        """The naive form took 5.5 s at 5 000 primitives; a uniform grid over the
        endpoints makes it effectively linear."""
        import random
        import time
        random.seed(7)
        lines = [Polyline(id=str(i),
                          points=[(random.uniform(0, 2000), random.uniform(0, 1400))
                                  for _ in range(2)])
                 for i in range(5000)]
        t0 = time.time()
        graph.chain_lines(lines)
        assert time.time() - t0 < 1.5, "line chaining has regressed to O(n^2)"

    def test_touching_runs_are_chained(self):
        a = Polyline(id="a", points=[(0, 0), (40, 0)])
        b = Polyline(id="b", points=[(40, 0), (90, 0)])
        chains = graph.chain_lines([a, b])
        assert len(chains) == 1 and chains[0].length == pytest.approx(90.0)

    def test_a_dashed_run_is_never_welded_to_a_solid_one(self):
        """Otherwise the graph gains a pipe where the drawing shows a signal."""
        a = Polyline(id="a", points=[(0, 0), (40, 0)], dashed=False)
        b = Polyline(id="b", points=[(40, 0), (90, 0)], dashed=True)
        chains = graph.chain_lines([a, b])
        assert len(chains) == 2
        assert {c.dashed for c in chains} == {True, False}


class TestTagRecognition:
    def test_a_balloon_two_liner_is_recombined(self):
        items = [("PI", BBox(0, 0, 12, 8), 0.9), ("204", BBox(0, 9, 14, 17), 0.9)]
        tags = recognise(items)
        assert [t.text for t in tags] == ["PI-204"]

    def test_a_hyphen_split_tag_is_recombined(self):
        items = [("V", BBox(0, 0, 6, 8), 0.9), ("-", BBox(7, 0, 9, 8), 0.9),
                 ("204", BBox(10, 0, 24, 8), 0.9)]
        assert [t.text for t in recognise(items)] == ["V-204"]

    @pytest.mark.parametrize("prefix,kind", [
        ("PI", "instrument"), ("LT", "instrument"), ("PSV", "equipment"),
        ("V", "equipment"), ("P", "equipment"), ("L", "line"), ("ZZ", "note"),
    ])
    def test_prefixes_are_typed(self, prefix, kind):
        assert classify_prefix(prefix) == kind


class TestSymbolGeometry:
    def test_bbox_distance_is_zero_inside(self):
        b = BBox(0, 0, 10, 10)
        assert b.distance_to(5, 5) == 0.0
        assert b.distance_to(13, 5) == pytest.approx(3.0)

    def test_bbox_iou(self):
        assert BBox(0, 0, 10, 10).iou(BBox(0, 0, 10, 10)) == pytest.approx(1.0)
        assert BBox(0, 0, 10, 10).iou(BBox(20, 20, 30, 30)) == 0.0
