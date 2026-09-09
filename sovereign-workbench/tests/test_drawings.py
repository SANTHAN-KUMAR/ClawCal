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
        ("PI", "instrument"), ("LT", "instrument"), ("AIT", "instrument"),
        ("PSV", "equipment"), ("V", "equipment"), ("P", "equipment"),
        ("BV", "equipment"), ("L", "line"),
        # ISA 5.1 generates instrument prefixes up to three letters, so a
        # four-letter run is neither equipment nor an instrument.
        ("ZZZZ", "note"),
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


class TestRealWorldTagReading:
    """Regressions from real refinery and utility drawings.

    Every string below is literal OCR output from the King County effluent P&ID
    (drawing WW510-P-60003). The original pattern capped sequence numbers at four
    digits and matched *none* of them: real plant tags encode area, unit and
    sequence in one number, so `BV510544F` has six. Teaching examples use V-204;
    operating plants do not.
    """

    from sovereign.drawings.tags import repair_tag, snap_reading  # noqa

    OCR_TO_TRUTH = {
        "BVS10344F": "BV-510544F", "BYS10544N": "BV-510544N",
        "CVS10503": "CV-510503", "5VS105448": "SV-510544B",
        "BVS10544H": "BV-510544H", "T510644": "T-510644",
        "BVS10544E": "BV-510544E", "SVS10503": "SV-510503",
        "PI510503B": "PI-510503B", "NVS10544A": "NV-510544A",
        "AIT520282": "AIT-520282", "FS510504": "FS-510504",
        "ME510503C": "ME-510503C",
    }

    def test_six_digit_plant_tags_are_matched_at_all(self):
        from sovereign.drawings.tags import TAG_RE
        for tag in ("BV-510544F", "AIT-520282", "SV-510503A", "PNL-520811"):
            assert TAG_RE.match(tag), f"{tag} does not match the tag pattern"

    def test_grammar_repair_recovers_most_readings_without_a_register(self):
        from sovereign.drawings.tags import repair_tag
        ok = sum(repair_tag(raw)[0] == truth
                 for raw, truth in self.OCR_TO_TRUTH.items())
        assert ok >= 11, (
            f"only {ok}/{len(self.OCR_TO_TRUTH)} readings repaired; the "
            f"position-aware digit/letter repair has regressed")

    def test_the_plant_register_resolves_every_reading(self):
        """The industrial answer: the plant knows its own tag list."""
        from sovereign.drawings.tags import snap_reading
        lex = set(self.OCR_TO_TRUTH.values())
        ok = sum(snap_reading(raw, lex)[0] == truth
                 for raw, truth in self.OCR_TO_TRUTH.items())
        assert ok == len(self.OCR_TO_TRUTH), f"only {ok} snapped correctly"

    def test_an_ambiguous_reading_is_left_alone(self):
        """Returning the wrong equipment is worse than returning none."""
        from sovereign.drawings.tags import snap_to_lexicon
        snapped, dist = snap_to_lexicon("BV-510544X",
                                        {"BV-510544A", "BV-510544B"})
        assert dist == -1 and snapped == "BV-510544X"

    def test_an_explicit_separator_is_honoured(self):
        """Re-deriving the split scores `P` + 1204 over `PI` + 204."""
        from sovereign.drawings.tags import repair_tag
        assert repair_tag("PI-204")[0] == "PI-204"
        assert repair_tag("P-101A")[0] == "P-101A"

    def test_document_references_are_kept_out_of_the_register(self):
        """A lexicon seeded with JULY-2026 would snap equipment onto dates."""
        from sovereign.drawings.tags import looks_like_equipment_tag
        for bad in ("IR-2026", "JULY-2026", "MECH-014", "YEAR-2016", "WW-510"):
            assert not looks_like_equipment_tag(bad), bad
        for good in ("V-204", "BV-510544F", "PSV-204A", "AIT-520282"):
            assert looks_like_equipment_tag(good), good


class TestPageAssessment:
    """A page of tables produces the same primitives as a P&ID."""

    def _binary(self, path):
        import cv2
        from sovereign.drawings.raster import _binarise
        return _binarise(cv2.imread(str(path), cv2.IMREAD_GRAYSCALE))

    def test_tabular_regions_are_located(self, corpus_dir):
        """Title blocks and legends must be set aside, not read as equipment."""
        from sovereign.drawings.raster import assess_page, _word_estimate
        import cv2
        img = cv2.imread(str(corpus_dir / "drawings/PID-204-01-scan.png"),
                         cv2.IMREAD_GRAYSCALE)
        a = assess_page(self._binary(corpus_dir / "drawings/PID-204-01-scan.png"),
                        None, _word_estimate(img))
        assert "tabular_regions" in a and "tabular_coverage" in a
        assert a["is_drawing"], "a real drawing was refused as a table"

    def test_resolution_is_reported_from_the_recogniser(self, corpus_dir):
        """Contour-based estimates rated a smaller render as having taller text;
        the OCR word boxes are the honest measure."""
        from sovereign.drawings.raster import assess_resolution
        import cv2
        img = cv2.imread(str(corpus_dir / "drawings/PID-204-01-scan.png"),
                         cv2.IMREAD_GRAYSCALE)
        low = assess_resolution(img, [6.0] * 40)
        high = assess_resolution(img, [22.0] * 40)
        assert not low["text_legible"] and high["text_legible"]
        assert "300 DPI" in low["advice"] and high["advice"] == ""
