"""Per-run, deterministic research limits and bounded progress accounting.

These limits govern tool usage, not an LLM's claims of evidence sufficiency.
A refused operation is never recorded as a successful retrieval.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ResearchControl:
    max_searches: int = 8
    max_acquisitions: int = 6
    max_passages: int = 12
    max_failed_acquisitions: int = 4
    max_passage_scans: int = 4
    passage_scans: int = 0
    searches: int = 0
    acquisitions: int = 0
    passages: int = 0
    failed_acquisitions: int = 0
    verified_passages: list = field(default_factory=list)
    queries: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    blocked_operations: int = 0

    def allowed(self, operation: str) -> bool:
        allowed = {
            "search": self.searches < self.max_searches,
            "acquisition": self.acquisitions < self.max_acquisitions
                           and self.failed_acquisitions < self.max_failed_acquisitions,
            "passage": self.passages < self.max_passages,
            "passage_scan": self.passage_scans < self.max_passage_scans,
        }[operation]
        if not allowed:
            self.blocked_operations += 1
        return allowed

    def snapshot(self) -> dict:
        return {
            "searches": self.searches, "max_searches": self.max_searches,
            "passage_scans": self.passage_scans, "max_passage_scans": self.max_passage_scans,
            "acquisitions": self.acquisitions, "max_acquisitions": self.max_acquisitions,
            "passages": self.passages, "max_passages": self.max_passages,
            "failed_acquisitions": self.failed_acquisitions,
            "max_failed_acquisitions": self.max_failed_acquisitions,
            "blocked_operations": self.blocked_operations,
            "verified_passages": self.verified_passages[-12:],
            "queries": self.queries[-8:],
            "candidates": self.candidates[-12:],
        }
