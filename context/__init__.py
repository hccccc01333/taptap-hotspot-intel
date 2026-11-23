"""V4 unified event / timeline / TapTap-context layer (deterministic MVP).

This package implements the *non-LLM* part of V4 §5:

- event identity and append-only versions;
- facts bound to a source page version, with stale detection when a later
  version no longer contains the quoted text;
- a timeline (source publication, observation, draft generation, version
  changes, stale facts);
- a context card derived only from public HTTP evidence, with explicit
  ``unknowns`` and a clearly-labelled relevance *hypothesis* (never a fact).

It does not write V3 production tables and does not fetch external pages by
itself; ingestion consumes a business event pack (``scripts/business_event_pack``).
"""
from .engine import (  # noqa: F401
    SCHEMA_VERSION,
    connect,
    context_card,
    event_detail,
    group_facts,
    ingest_pack,
    timeline,
)

__all__ = ['SCHEMA_VERSION', 'connect', 'context_card', 'event_detail',
           'group_facts', 'ingest_pack', 'timeline']
