from __future__ import annotations

import pytest

from helper.techvocab import (
    build_vocabulary,
    is_tech_like,
    mask_comparisons,
    split_tech_item,
    split_top_level,
)

LEXICON = {
    "aliases": {
        "PostgreSQL": ["Postgres"],
        "Kubernetes": ["K8s"],
        "Vision Framework": ["Apple Vision", "Vision"],
        "Stripe API": ["Stripe"],
    },
    "common": {"misc": ["Electron"]},
    "case_sensitive": ["Go", "Express", "Stripe"],
    "exclude": ["Vision"],
    "ignore": ["R squared", "R²", "R&D"],
}
MASTER_TERMS = ["C++", "C", "C#", "R", "Go", "Express", "Event-Driven Architecture", "Random Forest",
                "MediaPipe Tasks Vision", "React 19", "React", "Node.js"]


@pytest.fixture(scope="module")
def vocab():  # type: ignore[no-untyped-def]
    return build_vocabulary(LEXICON, MASTER_TERMS)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Rewrote it in C++ and C#, then C.", {"C++", "C#", "C"}),
        ("C++ only", {"C++"}),
        ("validated results in R", {"R"}),
        ("reaching R squared of 0.82", set()),
        ("R² of 0.82", set()),
        ("R&D budget", set()),
        ("a Go-based service", {"Go"}),
        ("go further on Google", set()),
        ("an Express server", {"Express"}),
        ("expressed interest", set()),
        ("a Postgres-backed store on K8s", {"PostgreSQL", "Kubernetes"}),
        ("an event driven architecture", {"Event-Driven Architecture"}),
        ("Event-Driven Architectures", {"Event-Driven Architecture"}),
        ("random forests", {"Random Forest"}),
        ("MediaPipe Tasks Vision hand tracking", {"MediaPipe Tasks Vision"}),
        ("Apple Vision segmentation", {"Vision Framework"}),
        ("Computer Vision coursework", set()),
        ("React 19 and Node.js", {"React", "Node.js"}),
    ],
)
def test_find(vocab, text: str, expected: set[str]) -> None:  # type: ignore[no-untyped-def]
    assert vocab.find_canonical(text) == expected


def test_canonical_lookup(vocab) -> None:  # type: ignore[no-untyped-def]
    assert vocab.canonical("Vision") == "Vision Framework"  # excluded from matching, still resolves
    assert vocab.canonical("React 19") == "React"
    assert vocab.canonical("postgres") == "PostgreSQL"
    assert vocab.canonical("go") is None  # case-sensitive names only resolve with their own casing


def test_comparisons_are_masked(vocab) -> None:  # type: ignore[no-untyped-def]
    assert vocab.find_canonical(mask_comparisons("an Airbnb/Uber/Stripe-style platform")) == set()
    assert vocab.find_canonical(mask_comparisons("(5-10MB vs 150MB+ Electron)")) == set()
    assert vocab.find_canonical(mask_comparisons("Stripe billing")) == {"Stripe API"}


@pytest.mark.parametrize(
    "item,expected",
    [
        ("AWS (ECS, RDS, VPC, IAM)", ["AWS", "ECS", "RDS", "VPC", "IAM"]),
        ("Java 21", ["Java 21", "Java"]),
        ("LSTM/GRU", ["LSTM", "GRU"]),
        ("Yjs + y-websocket", ["Yjs", "y-websocket"]),
        ("TypeScript (strict)", ["TypeScript"]),
        ("Tauri v2 (Rust)", ["Tauri v2", "Rust", "Tauri"]),
        ("CI/CD", ["CI/CD"]),
        ("Gemini API (Gemma 4)", ["Gemini API", "Gemma 4", "Gemma"]),
        ("DataFusion (Apache Arrow query engine)", ["DataFusion"]),
        ("**C# (new)**", ["C#"]),
    ],
)
def test_split_tech_item(item: str, expected: list[str]) -> None:
    assert split_tech_item(item) == expected


@pytest.mark.parametrize(
    "name,expected",
    [("prompt engineering", False), ("Multi-agent systems", False), ("quantization", False),
     ("signal filtering", False), ("Random Forest", True), ("shadcn/ui", True), ("C#", True), ("Docker", True)],
)
def test_is_tech_like(name: str, expected: bool) -> None:
    assert is_tech_like(name) is expected


def test_split_top_level_respects_parentheses() -> None:
    assert split_top_level("a (b, c), d") == ["a (b, c)", "d"]
    assert split_top_level("x (y — z) — note", " — ") == ["x (y — z)", "note"]
