"""Run-leased, citation-backed source nomination; no raw content publication."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import Field


def register(registry, research_tools, submit):
    if not research_tools.sources:
        return

    def research_nominate(
        evidence_id: Annotated[str, Field(pattern=r"^[a-f0-9]{24}$")],
        dossier_id: str,
        reason: Annotated[str, Field(min_length=1, max_length=600)],
    ):
        # The model can choose a citation, but cannot invent its provenance.
        UUID(dossier_id)
        receipt = research_tools.research_evidence(evidence_id)["receipt"]
        if not receipt.get("read_verified") or receipt.get("content_scope") != "retrieved_passage":
            raise ValueError("Only material actually read in this run can be nominated")
        item = {
            "dossier_id": dossier_id,
            "evidence_id": evidence_id,
            "source_id": receipt["source_id"],
            "kind": receipt["kind"],
            "title": receipt["title"],
            "content_scope": receipt["content_scope"],
            "document_sha256": receipt.get("document_sha256"),
            "section": receipt.get("section"),
            "retrieved_at": receipt.get("retrieved_at"),
            "public_url": receipt.get("public_url"),
            "location": receipt.get("location"),
            "reason": reason,
        }
        return submit(item)

    registry.add(
        "research_nominate", research_nominate,
        "Nominate one actually read source for a character-owned AIOS research dossier; "
        "records retrieval provenance only, without importing text or creating knowledge.",
        "research.read", effect="read",
    )
