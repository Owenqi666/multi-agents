"""Structured advice shared by project consultation consumers."""

from typing import TypedDict


class AdviceBackground(TypedDict):
    claim: str
    path: str


class ProjectAdvice(TypedDict):
    draft_goal: str
    background: list[AdviceBackground]
    assumptions: list[str]
    questions: list[str]
    remaining_disagreements: list[str]
