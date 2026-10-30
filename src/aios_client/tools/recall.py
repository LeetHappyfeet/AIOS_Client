"""Run-scoped callbacks supplied by the AIOS transport, registered like local tools."""

from typing import Annotated, Literal

from pydantic import Field


class RemoteTools:
    def __init__(self, recall, progress):
        self.recall, self.progress = recall, progress

    def aios_recall(self, query: Annotated[str, Field(min_length=1, max_length=600)]):
        return self.recall(query)

    def aios_progress(
        self,
        kind: Literal["progress", "blocked"],
        summary: Annotated[str, Field(min_length=1, max_length=1000)],
    ):
        return {"reported": self.progress(kind, summary) is not False}

    def register(self, registry):
        registry.add(
            "aios_recall",
            self.aios_recall,
            "Recall permitted character knowledge; preserve status, provenance and knowledge flags",
            "aios.recall",
        )
        registry.add(
            "aios_progress",
            self.aios_progress,
            "Report a meaningful milestone or blocked reason",
            "aios.progress",
        )

