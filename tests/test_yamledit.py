from __future__ import annotations

import pytest
import yaml

from helper.yamledit import set_scalar

TEXT = """\
# top comment
tier1:                # identity
  preferred_name: null   # optional
  address:
    street: null
tier2:
  eeo:
    gender: null
"""


def test_sets_a_nested_value_and_keeps_every_comment() -> None:
    out = set_scalar(TEXT, "tier1.preferred_name", "Vee")
    assert '  preferred_name: "Vee"   # optional\n' in out
    assert out.startswith("# top comment\ntier1:                # identity\n")
    assert yaml.safe_load(out)["tier1"]["preferred_name"] == "Vee"


def test_sets_a_deeper_value() -> None:
    out = set_scalar(TEXT, "tier1.address.street", "1 Main St")
    assert '    street: "1 Main St"\n' in out


def test_only_the_matching_branch_changes() -> None:
    text = "a:\n  name: null\nb:\n  name: null\n"
    assert set_scalar(text, "b.name", "x") == 'a:\n  name: null\nb:\n  name: "x"\n'


def test_quotes_and_hashes_inside_values_round_trip() -> None:
    value = 'He said "hi" # not a comment'
    out = set_scalar(TEXT, "tier2.eeo.gender", value)
    assert yaml.safe_load(out)["tier2"]["eeo"]["gender"] == value


def test_quoted_old_value_with_a_hash_keeps_the_real_comment() -> None:
    text = 'a:\n  b: "x # y"  # real comment\n'
    assert set_scalar(text, "a.b", "z") == 'a:\n  b: "z"  # real comment\n'


@pytest.mark.parametrize("value,expected", [(True, True), (None, None), (3, 3), ("Yes", "Yes")])
def test_types_round_trip(value: object, expected: object) -> None:
    out = set_scalar(TEXT, "tier1.preferred_name", value)
    assert yaml.safe_load(out)["tier1"]["preferred_name"] == expected


def test_missing_path_raises_key_error() -> None:
    with pytest.raises(KeyError):
        set_scalar(TEXT, "tier1.nope", "x")


def test_refuses_flow_style_values() -> None:
    with pytest.raises(ValueError):
        set_scalar("a:\n  b: {c: 1}\n", "a.b", "x")
