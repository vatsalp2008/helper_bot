from __future__ import annotations

import pytest

from helper.numbers import find_identifier_tokens, find_numbers, same_value


def nums(text: str) -> list[tuple[str, float, str]]:
    return [(n.raw, n.value, n.marker) for n in find_numbers(text, include_words=False)]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("~77% Car mAP@0.7 on KITTI", [("~77%", 77, "~"), ("0.7", 0.7, "")]),
        ("10M+ simulated events", [("10M+", 1e7, "+")]),
        ("10,000 trade events/sec", [("10,000", 10000, "")]),
        ("RMSE $45,000, MAE $32,000", [("$45,000", 45000, ""), ("$32,000", 32000, "")]),
        ("~$3.6B in daily trade", [("~$3.6B", 3.6e9, "~")]),
        ("p < 0.01 and p=0.06", [("< 0.01", 0.01, "<"), ("0.06", 0.06, "")]),
        ("Recall >0.70, F1 >0.65", [(">0.70", 0.7, ">"), (">0.65", 0.65, ">")]),
        ("45s→0.8s", [("45s", 45, ""), ("0.8s", 0.8, "")]),
        ("~800ms to ~40ms, a 20x improvement", [("~800ms", 800, "~"), ("~40ms", 40, "~"), ("20x", 20, "")]),
        ("+2,244/−100 lines", [("+2,244", 2244, ""), ("−100", 100, "")]),
        ("1400-1600 ELO", [("1400", 1400, ""), ("1600", 1600, "")]),
        ("5-10MB vs 150MB+", [("5", 5, ""), ("10MB", 10, ""), ("150MB+", 150, "+")]),
        ("sub-100ms real-time serving", [("100ms", 100, "")]),
        ("(3k★, ~1M LOC)", [("3k", 3000, ""), ("~1M", 1e6, "~")]),
        ("Ubuntu 22.04/24.04", [("22.04", 22.04, ""), ("24.04", 24.04, "")]),
        ("k-anonymity ≥5", [("≥5", 5, "≥")]),
    ],
)
def test_find_numbers(text: str, expected: list[tuple[str, float, str]]) -> None:
    assert nums(text) == expected


def test_identifiers_and_versions_are_not_numbers() -> None:
    assert nums("p99 latency, INT8, EC2, 3D detection, llama3.2, v19.4.22, CARLA 0.9.15, d8a32b6eb288, F1") == []


def test_word_numbers() -> None:
    words = [(n.raw, n.value) for n in find_numbers("Two additional PRs, the first three PRs, zero warnings") if n.is_word]
    assert words == [("Two", 2), ("three", 3), ("zero", 0)]
    assert not [n for n in find_numbers("a one-line change in Three.js") if n.is_word]


def test_identifier_tokens() -> None:
    tokens = find_identifier_tokens("p99 latency via INT8 on EC2, mAP@0.7, a 3-row dataset, 10M+ events, F1-score")
    assert tokens == {"p99", "INT8", "EC2", "F1-score"}


def test_same_value_ignores_formatting() -> None:
    assert same_value(1e4, 10000.0)
    assert not same_value(77.0, 7.7)
