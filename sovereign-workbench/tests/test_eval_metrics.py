"""The dirty-corpus scorer. A bug here would falsify every published number."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import eval_dirty as E  # noqa: E402


def test_cer_is_zero_for_identical_and_ignores_punctuation_spacing():
    assert E.cer("abc def", "abc def") == 0.0
    assert E.cer('the " Brave , " men', 'the "Brave," men') == 0.0


def test_cer_counts_real_errors_and_caps_at_one():
    assert E.cer("pressure 16", "pressure 18") == round(1 / 11, 3)
    assert E.cer("abc", "") == 1.0
    assert E.cer("abc", "x" * 50) == 1.0


def test_bag_scores_are_order_free_and_multiset():
    r, p = E.bag_scores(["a", "b", "b"], ["b", "a"])
    assert r == round(2 / 3, 3) and p == 1.0


def test_digits_match_receipt_values_across_separator_styles():
    assert E.digits("60.000") == E.digits("60,000") == "60000"
