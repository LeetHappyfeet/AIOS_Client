"""Research nomination is citation-backed, run-scoped, and never imports content."""
from uuid import uuid4

import pytest

from aios_client.research.bridge import register
from aios_client.tools.registry import Registry


DOSSIER = str(uuid4())
EVIDENCE = "a" * 24


class Sources:
    sources = {"library": object()}

    def __init__(self, *, verified):
        self.verified = verified

    def research_evidence(self, evidence_id):
        assert evidence_id == EVIDENCE
        return {"receipt": {
            "evidence_id": evidence_id, "source_id": "library",
            "kind": "calibre_book", "title": "Planetary Science",
            "content_scope": "retrieved_passage" if self.verified else "discovery_excerpt",
            "read_verified": self.verified, "document_sha256": "b" * 64,
            "section": "chapter:1", "location": {"catalog_id": "book-42"},
            "retrieved_at": "2026-11-15T12:00:00+00:00",
        }}


def test_nomination_rejects_snippets_and_requires_open_dossier_identity():
    registry = Registry()
    registry.tool_mode = "research"
    sent = []
    register(registry, Sources(verified=False), lambda payload: sent.append(payload))
    with pytest.raises(ValueError, match="actually read"):
        registry.execute("research_nominate", {
            "evidence_id": EVIDENCE, "dossier_id": DOSSIER, "reason": "Relevant"})
    assert not sent
    assert "research_nominate" in [t["function"]["name"] for t in registry.catalog()]


def test_nomination_only_transmits_validated_receipt_and_reason():
    registry = Registry()
    registry.tool_mode = "research"
    sent = []
    register(registry, Sources(verified=True), lambda payload: sent.append(payload) or {
        "status": "nominated", "corpus_imported": False})
    result = registry.execute("research_nominate", {
        "evidence_id": EVIDENCE, "dossier_id": DOSSIER,
        "reason": "Contains a useful comparison"})
    assert result["status"] == "nominated"
    assert len(sent) == 1
    assert sent[0]["document_sha256"] == "b" * 64
    assert sent[0]["location"] == {"catalog_id": "book-42"}
    assert "text" not in sent[0] and "content" not in sent[0]


def test_nomination_rejects_malformed_dossier_uuid():
    registry = Registry()
    registry.tool_mode = "research"
    register(registry, Sources(verified=True), lambda _: {"status": "nominated"})
    with pytest.raises(ValueError):
        registry.execute("research_nominate", {
            "evidence_id": EVIDENCE, "dossier_id": "not-a-uuid",
            "reason": "Interesting"})
