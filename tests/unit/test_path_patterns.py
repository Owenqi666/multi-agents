from fnmatch import fnmatchcase
from itertools import combinations_with_replacement, product

import pytest

from multi_agent.core import path_patterns
from multi_agent.core.path_patterns import (
    MAX_NFA_STATES,
    PatternBudgetExceeded,
    normalize_path,
    path_matches,
    patterns_cover,
    patterns_intersect,
)


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("a", "**", True),
        ("a/b/c", "**", True),
        ("a", "a/**", True),
        ("a/b", "a/**", True),
        ("a/b", "a/**/b", True),
        ("a/x/y/b", "a/**/b", True),
        ("b", "**/b", True),
        ("a/b", "**/b", True),
        ("a/b", "a/**/**/b", True),
        ("a", "a/*", False),
        ("a/b", "a*", False),
        ("x", "*", True),
        ("x/y", "*", False),
        ("a/x", "a/?", True),
        ("a/xy", "a/?", False),
        ("", "**", False),
        ("/", "**", False),
        ("a//b", "**", False),
        ("a/", "**", False),
        ("/a", "**", False),
        ("ß", "?", False),
        ("ß", "??", True),
        ("ﬁ", "??", True),
        ("E\u0301", "?", True),
        ("Straße/ﬁle", "STRASSE/FILE", True),
        ("src/?", "src/?", True),
        ("src/*", "src/?", True),
    ],
)
def test_concrete_paths_obey_segment_and_normalized_codepoint_semantics(
    path, pattern, expected
):
    assert path_matches(path, pattern) is expected


def test_normalization_is_shared_and_idempotent():
    value = "FRONT/E\u0301/STRAẞE/ﬁ"
    normalized = "front/é/strasse/fi"
    assert normalize_path(value) == normalized
    assert normalize_path(normalized) == normalized


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("frontend/**", "**/*.tsx", True),
        ("frontend/**", "frontend", True),
        ("frontend/*", "frontend", False),
        ("frontend/**", "backend/**", False),
        ("a?", "?b", True),
        ("a*", "b*", False),
        ("*", "a/b", False),
        ("a/**/b", "a/b", True),
        ("a/**/b", "a/c/**", True),
        ("a*/b", "a/c*", False),
        ("x/**/z", "x/y/**", True),
        ("**/x/**", "y/**", True),
        ("*", "?*", True),
        ("*", "**/**", True),
        ("ß", "SS", True),
        ("ﬁ", "fi", True),
        ("E\u0301", "é", True),
        ("?", "ß", False),
        ("??", "ß", True),
    ],
)
def test_symbolic_intersections_are_symmetric(left, right, expected):
    assert patterns_intersect(left, right) is expected
    assert patterns_intersect(right, left) is expected


@pytest.mark.parametrize(
    ("scope", "covering", "expected"),
    [
        ("x/*", ("x/?", "x/??*"), True),
        ("a/**", ("a", "a/*/**"), True),
        ("a/**/b", ("a/b", "a/*/**/b"), True),
        ("**", ("*", "*/*", "*/*/*/**"), True),
        ("*a*", ("a*", "?*a*"), True),
        ("a*b", ("ab", "a?*b"), True),
        ("*", ("?", "??"), False),
        ("**", ("*", "*/*"), False),
        ("a/**/b", ("a/b",), False),
        ("?", ("a", "b"), False),
        ("*", ("a*", "b*"), False),
        ("a", (), False),
        ("**", (), False),
        ("frontend/**", ("**",), True),
        ("frontend/**", ("frontend/*",), False),
        ("frontend/*", ("frontend/**",), True),
        ("ß", ("ss",), True),
        ("ß", ("?",), False),
        ("ß", ("??",), True),
        ("Café/**", ("CAFE\u0301/**",), True),
    ],
)
def test_union_coverage_is_exact_for_zero_segments_and_other_characters(
    scope, covering, expected
):
    assert patterns_cover(scope, covering) is expected
    assert patterns_cover(scope, tuple(reversed(covering))) is expected


def _segment_oracle(path, pattern):
    path = normalize_path(path).split("/")
    pattern = normalize_path(pattern).split("/")

    def matches(parts, segments):
        if not segments:
            return not parts
        if segments[0] == "**":
            return any(
                matches(parts[count:], segments[1:]) for count in range(len(parts) + 1)
            )
        return (
            bool(parts)
            and fnmatchcase(parts[0], segments[0])
            and matches(parts[1:], segments[1:])
        )

    return matches(path, pattern)


def test_globstar_matches_an_independent_segment_oracle():
    patterns = (
        "**",
        "*",
        "?",
        "a*",
        "a/**",
        "**/a",
        "**/a/**",
        "a/**/b",
        "**/**/a",
        "*/**/?",
        "**/a/**/b/**",
        "a/*/b",
    )
    for count in range(1, 4):
        for segments in product(("a", "b", "ab"), repeat=count):
            path = "/".join(segments)
            for pattern in patterns:
                assert path_matches(path, pattern) == _segment_oracle(path, pattern), (
                    path,
                    pattern,
                )


def test_symbolic_results_match_exhaustive_finite_alphabet_quotient():
    # These fixed-width patterns have at most two one-character segments.
    # 'x' represents every character other than the literals 'a' and 'b', so
    # this is exhaustive for this grammar subset, rather than file enumeration.
    patterns = ("a", "b", "?", "a/?", "?/a", "?/?", "a/a", "b/b")
    paths = tuple(
        "/".join(parts)
        for size in (1, 2)
        for parts in product(("a", "b", "x"), repeat=size)
    )
    languages = {
        pattern: {path for path in paths if _segment_oracle(path, pattern)}
        for pattern in patterns
    }
    for left, right in product(patterns, repeat=2):
        assert patterns_intersect(left, right) == bool(
            languages[left] & languages[right]
        )
    for scope in patterns:
        for covering in combinations_with_replacement(patterns, 2):
            expected = (
                languages[scope] <= languages[covering[0]] | languages[covering[1]]
            )
            assert patterns_cover(scope, covering) is expected


def test_search_budget_counts_dequeued_product_states():
    # initial, after 'a', after its trailing segment delimiter: three states.
    assert patterns_intersect("a", "a", state_budget=3)
    assert patterns_cover("a", ("a",), state_budget=3)
    with pytest.raises(PatternBudgetExceeded, match="search"):
        patterns_intersect("a", "a", state_budget=2)
    with pytest.raises(PatternBudgetExceeded, match="search"):
        patterns_cover("a", ("a",), state_budget=2)
    with pytest.raises(PatternBudgetExceeded, match="search"):
        patterns_intersect("a", "b", state_budget=0)
    with pytest.raises(PatternBudgetExceeded, match="search"):
        patterns_cover("a", (), state_budget=0)


def test_nfa_budget_applies_to_construction_and_covering_union(monkeypatch):
    monkeypatch.setattr(path_patterns, "MAX_NFA_STATES", 4)
    assert path_matches("a", "a")
    with pytest.raises(PatternBudgetExceeded, match="NFA"):
        path_matches("ab", "ab")
    with pytest.raises(PatternBudgetExceeded, match="NFA"):
        patterns_intersect("a", "ab")
    with pytest.raises(PatternBudgetExceeded, match="NFA"):
        patterns_cover("a", ("a", "b"))


def test_default_nfa_limit_rejects_oversized_input_without_approximation():
    with pytest.raises(PatternBudgetExceeded, match="NFA"):
        patterns_intersect("a" * MAX_NFA_STATES, "*")
