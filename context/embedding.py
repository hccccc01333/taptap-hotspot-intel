"""Pluggable semantic same-event matching (multilingual embedding interface).

The event layer already produces deterministic metadata candidates (title/date/
quote overlap). Cross-language sources often share only the event date, so a
date-only candidate is weak. This module adds a **pluggable** embedding path:
the caller supplies an embedder (local multilingual model or an approved
provider); the code only computes cosine similarity and emits candidates.

No embedder is configured by default and no external call is made here.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Sequence

Embedder = Callable[[Sequence[str]], Sequence[Sequence[float]]]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


def _event_documents(events: list[dict[str, Any]], facts: list[dict[str, Any]],
                     max_quotes: int = 5) -> list[tuple[dict[str, Any], str]]:
    quotes: dict[str, list[str]] = {}
    for fact in facts:
        if fact.get('status') == 'active' and fact.get('quote'):
            quotes.setdefault(fact['event_id'], []).append(str(fact['quote']))
    documents: list[tuple[dict[str, Any], str]] = []
    for event in events:
        parts = [str(event.get('title') or '')]
        parts.extend(quotes.get(event['event_id'], [])[:max_quotes])
        text = '\n'.join(part for part in parts if part)
        if text.strip():
            documents.append((event, text))
    return documents


def semantic_same_event_candidates(events: list[dict[str, Any]],
                                   facts: list[dict[str, Any]],
                                   embedder: Embedder, *,
                                   threshold: float = 0.82,
                                   max_quotes: int = 5) -> list[dict[str, Any]]:
    """Embed event titles+quotes and return high-similarity candidate pairs.

    The embedder must be multilingual for cross-language sources. Threshold is a
    candidate gate, not a merge decision; results are never auto-merged.
    """
    documents = _event_documents(events, facts, max_quotes=max_quotes)
    if len(documents) < 2:
        return []
    vectors = embedder([text for _event, text in documents])
    if len(vectors) != len(documents):
        raise ValueError('embedder 返回的向量数与事件数不一致')
    candidates: list[dict[str, Any]] = []
    for index in range(len(documents)):
        left = documents[index][0]
        for other in range(index + 1, len(documents)):
            right = documents[other][0]
            similarity = cosine(vectors[index], vectors[other])
            if similarity < threshold:
                continue
            candidates.append({
                'left_event_id': left['event_id'], 'right_event_id': right['event_id'],
                'embedding_similarity': round(similarity, 3),
                'threshold': threshold,
                'auto_merge': False,
                'reason': '语义 embedding 相似度超过候选阈值；仍需来源核查，不自动合并',
            })
    return sorted(candidates, key=lambda item: item['embedding_similarity'], reverse=True)
