"""Run-scoped callbacks supplied by the AIOS transport, registered like local tools."""

from typing import Annotated, Literal

from pydantic import Field


class RemoteTools:
    def __init__(self, recall, progress, work_history=None, work_result=None):
        self.recall, self.progress = recall, progress
        self.work_history, self.work_result = work_history, work_result

    def aios_work_history(self):
        return self.work_history()

    def aios_read_work_result(
        self,
        run_id: Annotated[str, Field(min_length=1, max_length=80)],
        section: Literal['summary', 'findings', 'limitations', 'requested_content', 'report', 'checks'] = 'summary',
        cursor: Annotated[int, Field(ge=0, le=100000)] = 0,
        limit: Annotated[int, Field(ge=100, le=4000)] = 3000,
        revision: Annotated[str | None, Field(pattern=r'^[a-f0-9]{64}$')] = None,
    ):
        if cursor and revision is None:
            raise ValueError('Continue retrieval with the returned revision')
        return self.work_result(dict(run_id=run_id, section=section, cursor=cursor,
                                     limit=limit, revision=revision))

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
        if self.work_history is not None and self.work_result is not None:
            registry.add('aios_work_history', self.aios_work_history,
                         'List scoped prior work reports; historical evidence is not a current measurement',
                         'aios.work_evidence')
            registry.add('aios_read_work_result', self.aios_read_work_result,
                         'Read a retained worker report without executing work. Continue with next_cursor and revision.',
                         'aios.work_evidence')

