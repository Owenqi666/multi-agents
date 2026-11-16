"""Exact glob-language operations on normalized repository-relative paths.

Inputs have already passed the CLI's path grammar. Both patterns and concrete
paths are NFC-normalized and case-folded before '*' and '?' are interpreted.
'?' consumes one code point of that normalized string; '**' consumes zero or
more whole segments. Empty paths and empty segments are excluded.

Every segment is internally terminated by '/', so a/b is encoded as a/b/.
This makes '**' an ordinary epsilon-NFA loop over a nonempty segment and its
terminator. A small validity DFA excludes empty segments and the empty path.
The search alphabet consists of every literal, '/' and one OTHER class: these
are exactly the equivalence classes observable by these NFAs.
"""

import unicodedata
from collections import deque
from dataclasses import dataclass

DEFAULT_STATE_BUDGET = 100_000
MAX_NFA_STATES = 4096


class PatternBudgetExceeded(ValueError):
    """An exact answer could not be obtained within the configured finite limit."""


def normalize_path(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


@dataclass(frozen=True, slots=True)
class _NFA:
    # None labels consume any non-slash character; target masks already include
    # epsilon closure. Bitsets avoid allocating large frozensets per DFA state.
    moves: tuple[tuple[tuple[str | None, int], ...], ...]
    initial: int
    accepting: int
    literals: frozenset[str]

    def advance(self, states: int, symbol: str | None) -> int:
        result = 0
        while states:
            bit = states & -states
            for label, targets in self.moves[bit.bit_length() - 1]:
                if label == symbol or (label is None and symbol != "/"):
                    result |= targets
            states ^= bit
        return result


def _compile(patterns: tuple[str, ...]) -> _NFA:
    edges: list[list[tuple[str | None, int]]] = []
    epsilon: list[list[int]] = []

    def state() -> int:
        if len(edges) >= MAX_NFA_STATES:
            raise PatternBudgetExceeded("path pattern NFA state limit exceeded")
        edges.append([])
        epsilon.append([])
        return len(edges) - 1

    initial = state()
    accepting = 0
    literals: set[str] = {"/"}
    for pattern in patterns:
        current = state()
        epsilon[initial].append(current)
        for segment in normalize_path(pattern).split("/"):
            if segment == "**":
                following = state()
                body = state()
                epsilon[current].append(following)
                edges[current].append((None, body))
                edges[body].extend(((None, body), ("/", current)))
                current = following
            else:
                for char in segment:
                    following = state()
                    if char == "*":
                        epsilon[current].append(following)
                        edges[current].append((None, current))
                    elif char == "?":
                        edges[current].append((None, following))
                    else:
                        literals.add(char)
                        edges[current].append((char, following))
                    current = following
                following = state()
                edges[current].append(("/", following))
                current = following
        accepting |= 1 << current

    # All epsilon edges point forward. Character-consuming loops may point
    # backward, but cannot contribute to epsilon closure.
    closures = [0] * len(edges)
    for index in range(len(edges) - 1, -1, -1):
        closure = 1 << index
        for following in epsilon[index]:
            closure |= closures[following]
        closures[index] = closure
    moves = tuple(
        tuple((label, closures[target]) for label, target in outgoing)
        for outgoing in edges
    )
    return _NFA(moves, closures[initial], accepting, frozenset(literals))


def _valid_step(state: int, symbol: str | None) -> int | None:
    # 0: no segment yet; 1: inside a nonempty segment; 2: just after a segment.
    if symbol == "/":
        return 2 if state == 1 else None
    return 1


def _has_witness(
    left: _NFA, right: _NFA, *, difference: bool, state_budget: int
) -> bool:
    alphabet: tuple[str | None, ...] = (*sorted(left.literals | right.literals), None)
    initial = (left.initial, right.initial, 0)
    queue = deque((initial,))
    seen = {initial}
    examined = 0
    while queue:
        left_states, right_states, validity = queue.popleft()
        examined += 1
        if examined > state_budget:
            raise PatternBudgetExceeded("path pattern search state budget exceeded")
        if (
            validity == 2
            and left_states & left.accepting
            and bool(right_states & right.accepting) != difference
        ):
            return True
        for symbol in alphabet:
            next_validity = _valid_step(validity, symbol)
            if next_validity is None:
                continue
            next_left = left.advance(left_states, symbol)
            if not next_left:
                continue
            next_right = right.advance(right_states, symbol)
            if not difference and not next_right:
                continue
            following = (next_left, next_right, next_validity)
            if following not in seen:
                seen.add(following)
                queue.append(following)
    return False


def path_matches(path: str, pattern: str) -> bool:
    """Match a concrete path, interpreting wildcard characters only in pattern."""
    nfa = _compile((pattern,))
    states = nfa.initial
    validity = 0
    for symbol in normalize_path(path) + "/":
        following = _valid_step(validity, symbol)
        if following is None:
            return False
        validity = following
        states = nfa.advance(states, symbol)
        if not states:
            return False
    return validity == 2 and bool(states & nfa.accepting)


def patterns_intersect(
    left: str, right: str, *, state_budget: int = DEFAULT_STATE_BUDGET
) -> bool:
    """Whether a nonempty normalized path belongs to both pattern languages."""
    return _has_witness(
        _compile((left,)),
        _compile((right,)),
        difference=False,
        state_budget=state_budget,
    )


def patterns_cover(
    scope: str, covering: tuple[str, ...], *, state_budget: int = DEFAULT_STATE_BUDGET
) -> bool:
    """Whether the union of covering languages includes every path in scope."""
    return not _has_witness(
        _compile((scope,)),
        _compile(covering),
        difference=True,
        state_budget=state_budget,
    )
