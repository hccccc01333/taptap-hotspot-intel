"""Deterministic same-event / fact-conflict candidates for the event layer.

This is deliberately conservative and non-LLM:

- same-event candidates compare titles (minus the shared game subject), dates and
  fact-quote overlap; they are **candidates only** and never auto-merged;
- fact-conflict candidates look for active facts from different events that are
  textually similar but carry different numbers/dates (e.g. two prices);
- every candidate keeps source URLs and reasons so a human/source check can
  decide; no "winner" is chosen automatically.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Iterable

from . import engine

TOKEN_RE = re.compile(r'[A-Za-z0-9]+|[\u4e00-\u9fff]')
NUMBER_RE = re.compile(r'\d+(?:\.\d+)?%?|20\d{2}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*日')
STOP_WORDS = {'的', '了', '和', '与', '及', '在', '将', '是', '为', '对', '从', '把',
              '被', '并', '或', '等', '中', '后', '前', '新', '版', '游戏', '更新'}


def _tokens(text: str) -> set[str]:
    chars = [token for token in TOKEN_RE.findall(str(text or '')) if token not in STOP_WORDS]
    if not chars:
        return set()
    tokens = set(chars)
    # Chinese bigrams capture short title phrases better than single characters.
    compact = ''.join(chars)
    tokens.update(compact[index:index + 2] for index in range(len(compact) - 1))
    return tokens


def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _subject_tokens(subject: str) -> set[str]:
    return _tokens(subject)


def _pub_day(value: str) -> dt.date | None:
    text = str(value or '').strip()
    match = re.search(r'(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})', text)
    if not match:
        match = re.search(r'(20\d{2})(\d{2})(\d{2})', text)
    if not match:
        return None
    try:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def same_event_candidates(events: list[dict[str, Any]], facts: list[dict[str, Any]], *,
                          subject: str = '', threshold: float = 0.35) -> list[dict[str, Any]]:
    by_event: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        if fact.get('status') == 'active':
            by_event.setdefault(fact['event_id'], []).append(fact)
    subject_tokens = _subject_tokens(subject)
    signatures: dict[str, dict[str, Any]] = {}
    for event in events:
        title_tokens = _tokens(event.get('title') or '') - subject_tokens
        event_facts = by_event.get(event['event_id'], [])
        quote_tokens: set[str] = set()
        dates: set[str] = set()
        for fact in event_facts:
            quote_tokens |= _tokens(fact.get('quote') or '')
            dates |= {engine.normalize(atom) for atom in engine.DATE_RE.findall(
                f"{fact.get('text') or ''} {fact.get('quote') or ''}")}
        signatures[event['event_id']] = {'title': title_tokens, 'quotes': quote_tokens,
                                         'dates': dates,
                                         'pub_day': _pub_day(event.get('published_at') or '')}
    candidates: list[dict[str, Any]] = []
    event_list = [event for event in events if event['event_id'] in signatures]
    for index, left in enumerate(event_list):
        for right in event_list[index + 1:]:
            a, b = signatures[left['event_id']], signatures[right['event_id']]
            title_sim = _jaccard(a['title'], b['title'])
            quote_sim = _jaccard(a['quotes'], b['quotes'])
            union_dates = a['dates'] | b['dates']
            date_overlap = (len(a['dates'] & b['dates']) / len(union_dates)
                            if union_dates else 0.0)
            pub_proximity = 0.0
            if a['pub_day'] and b['pub_day']:
                delta = abs((a['pub_day'] - b['pub_day']).days)
                pub_proximity = 1.0 if delta <= 1 else (0.5 if delta <= 2 else 0.0)
            metadata_only = bool(left['event_id']) and (not a['quotes'] or not b['quotes'])
            score = round(0.35 * title_sim + 0.20 * quote_sim + 0.10 * date_overlap
                          + 0.35 * pub_proximity, 3)
            if score < threshold:
                continue
            candidates.append({
                'left_event_id': left['event_id'], 'right_event_id': right['event_id'],
                'score': score, 'title_similarity': round(title_sim, 3),
                'date_overlap': round(date_overlap, 3),
                'quote_similarity': round(quote_sim, 3),
                'publication_proximity': pub_proximity,
                'metadata_only': metadata_only,
                'auto_merge': False,
                'reason': ('候选：标题/日期/引文/发布日期邻近；需来源核查后才能合并'
                           + ('；至少一方无正文事实，仅为元数据候选' if metadata_only else '')),
            })
    return sorted(candidates, key=lambda item: item['score'], reverse=True)


def fact_conflict_candidates(facts: list[dict[str, Any]], *,
                             threshold: float = 0.55) -> list[dict[str, Any]]:
    active = [fact for fact in facts if fact.get('status') == 'active']
    candidates: list[dict[str, Any]] = []
    for index, left in enumerate(active):
        left_atoms = set(NUMBER_RE.findall(str(left.get('text') or '')))
        left_text = _tokens(str(left.get('text') or ''))
        for right in active[index + 1:]:
            if left['event_id'] == right['event_id']:
                continue
            right_atoms = set(NUMBER_RE.findall(str(right.get('text') or '')))
            if not left_atoms or not right_atoms or left_atoms == right_atoms:
                continue
            similarity = _jaccard(left_text, _tokens(str(right.get('text') or '')))
            if similarity < threshold:
                continue
            candidates.append({
                'left': {'event_id': left['event_id'], 'text': left['text'],
                         'quote': left['quote'], 'source_url': left.get('source_url')},
                'right': {'event_id': right['event_id'], 'text': right['text'],
                          'quote': right['quote'], 'source_url': right.get('source_url')},
                'left_atoms': sorted(left_atoms), 'right_atoms': sorted(right_atoms),
                'text_similarity': round(similarity, 3),
                'resolution': 'unresolved;需要来源核查，不能自动选一个',
            })
    return sorted(candidates, key=lambda item: item['text_similarity'], reverse=True)


def group_match_report(group_key: str, db_path: str | Path, *,
                       embedder: Any = None) -> dict[str, Any]:
    detail = engine.group_detail(group_key, db_path)
    if not detail:
        return {}
    subject = (detail.get('group') or {}).get('label') or ''
    raw_facts = engine.group_facts(group_key, db_path)
    semantic_candidates: list[dict[str, Any]] = []
    semantic_note = '未配置 embedding provider；仅元数据候选'
    if embedder is not None:
        from .embedding import semantic_same_event_candidates
        semantic_candidates = semantic_same_event_candidates(
            detail['events'], raw_facts, embedder)
        semantic_note = '已配置 embedding provider；候选仍需来源核查'
    return {
        'group_key': group_key,
        'events': [{'event_id': event['event_id'], 'title': event['title'],
                    'source_url': event.get('source_url')} for event in detail['events']],
        'same_event_candidates': same_event_candidates(
            detail['events'], raw_facts, subject=subject),
        'semantic_same_event_candidates': semantic_candidates,
        'semantic_note': semantic_note,
        'fact_conflict_candidates': fact_conflict_candidates(raw_facts),
        'note': '候选不是结论；same_event 不自动合并，冲突不自动裁决。',
    }
