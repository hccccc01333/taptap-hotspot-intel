"""Deterministic evaluation of business event packs and context cards.

These metrics are *proxy* checks for content quality, not fact verification:

- quote support: each fact quote must appear verbatim in the saved article;
- unsupported numeric/date atoms: numbers or dates in a fact's text that do
  not appear in the source body;
- material completeness: title/channel/draft/usage boundary non-empty;
- unknown handling: count of declared unknowns;
- context coverage: how many public context fields were derived;
- timeline/stale state: from the context store.

No model is called here. Low scores identify drafts to review; high scores do
not prove truth.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import engine

NUMBER_RE = re.compile(r'\d+(?:\.\d+)?%?|20\d{2}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*日')


def _norm(value: str) -> str:
    return re.sub(r'\s+', '', str(value or ''))


def _read_pack_event(pack_dir: Path, event_id: str) -> tuple[dict, dict]:
    folder = pack_dir / event_id
    evidence = json.loads((folder / 'evidence.json').read_text(encoding='utf-8'))
    final_path = folder / 'final.json'
    final = json.loads(final_path.read_text(encoding='utf-8')) if final_path.is_file() else {}
    return evidence, final


def score_content(parsed: dict[str, Any], body: str) -> dict[str, Any]:
    """Score one model output against its source body (deterministic only)."""
    facts = parsed.get('facts') if isinstance(parsed.get('facts'), list) else []
    supported = 0
    unsupported_atoms: list[str] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        quote = str(fact.get('quote') or '')
        text = str(fact.get('text') or '')
        if quote and _norm(quote) in _norm(body):
            supported += 1
        for atom in NUMBER_RE.findall(text):
            if _norm(atom) and _norm(atom) not in _norm(body):
                unsupported_atoms.append(atom)
    material = parsed.get('material') if isinstance(parsed.get('material'), dict) else {}
    material_fields = {key: bool(str(material.get(key) or '').strip())
                       for key in ('title', 'channel', 'draft', 'usage_boundary')}
    unknowns = parsed.get('unknowns') if isinstance(parsed.get('unknowns'), list) else []
    return {
        'facts_total': len([item for item in facts if isinstance(item, dict)]),
        'facts_supported': supported,
        'quote_support_rate': round(supported / len(facts), 3) if facts else 0.0,
        'unsupported_numeric_atoms': sorted(set(unsupported_atoms)),
        'unsupported_numeric_count': len(set(unsupported_atoms)),
        'material_fields': material_fields,
        'material_complete': all(material_fields.values()),
        'draft_chars': len(str(material.get('draft') or '')),
        'unknowns_count': len(unknowns),
    }


def evaluate_event(pack_dir: str | Path, event_id: str) -> dict[str, Any]:
    pack_dir = Path(pack_dir)
    evidence, final = _read_pack_event(pack_dir, event_id)
    article = evidence.get('article') or {}
    body = str(article.get('body') or '')
    parsed = final.get('parsed') if isinstance(final.get('parsed'), dict) else {}
    score = score_content(parsed, body)
    card = engine.context_from_article(
        evidence.get('event') or {}, article, final, version_id='eval_preview')
    score.update(event_id=event_id, context_fields={
        'platforms': len(card.get('platforms') or []),
        'dates': len(card.get('dates') or []),
        'prices': len(card.get('prices') or []),
        'unknowns': len(card.get('unknowns') or []),
    }, context_card=card)
    return score


def evaluate_pack(pack_dir: str | Path, *, db_path: str | Path | None = None) -> dict[str, Any]:
    pack_dir = Path(pack_dir)
    summary = json.loads((pack_dir / 'summary.json').read_text(encoding='utf-8'))
    events = [item for item in (summary.get('events') or [])
              if isinstance(item, dict) and item.get('event_id')]
    rows = []
    for item in events:
        event_id = item['event_id']
        try:
            row = evaluate_event(pack_dir, event_id)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            row = {'event_id': event_id, 'error': f'{type(exc).__name__}: {exc}'}
        if db_path is not None:
            try:
                detail = engine.event_detail(event_id, db_path)
                row['timeline_entries'] = len(detail.get('timeline') or [])
                row['stale_facts'] = sum(1 for fact in (detail.get('facts') or [])
                                         if fact.get('status') == 'stale')
            except (OSError, ValueError):
                row['timeline_entries'] = 0
                row['stale_facts'] = 0
        rows.append(row)
    valid = [row for row in rows if 'error' not in row]
    total_facts = sum(row['facts_total'] for row in valid)
    total_supported = sum(row['facts_supported'] for row in valid)
    return {
        'pack_dir': str(pack_dir),
        'events': rows,
        'aggregate': {
            'events': len(rows),
            'events_with_errors': len(rows) - len(valid),
            'facts_total': total_facts,
            'facts_supported': total_supported,
            'quote_support_rate': round(total_supported / total_facts, 3) if total_facts else 0.0,
            'unsupported_numeric_count': sum(row['unsupported_numeric_count'] for row in valid),
            'material_complete_events': sum(1 for row in valid if row['material_complete']),
            'unknowns_total': sum(row['unknowns_count'] for row in valid),
        },
    }


def compare_runs(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    def agg(run: dict[str, Any], key: str) -> float:
        return float((run.get('aggregate') or {}).get(key) or 0)
    return {
        'quote_support_delta': round(agg(candidate, 'quote_support_rate') -
                                     agg(baseline, 'quote_support_rate'), 3),
        'unsupported_numeric_delta': (agg(candidate, 'unsupported_numeric_count') -
                                      agg(baseline, 'unsupported_numeric_count')),
        'material_complete_delta': (agg(candidate, 'material_complete_events') -
                                    agg(baseline, 'material_complete_events')),
        'unknowns_delta': agg(candidate, 'unknowns_total') - agg(baseline, 'unknowns_total'),
        'candidate_not_worse': (
            agg(candidate, 'quote_support_rate') >= agg(baseline, 'quote_support_rate') and
            agg(candidate, 'unsupported_numeric_count') <= agg(baseline, 'unsupported_numeric_count') and
            agg(candidate, 'material_complete_events') >= agg(baseline, 'material_complete_events')),
    }
