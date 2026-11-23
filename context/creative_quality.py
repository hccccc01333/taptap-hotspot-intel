"""Quality checks for growth creatives (structure, safety, non-copying)."""
from __future__ import annotations

import re
from typing import Any

REQUIRED = ('title', 'hook', 'audience_need', 'proposed_action', 'copy_draft',
            'preconditions', 'status')
NUMBER_RE = re.compile(r'\d+(?:\.\d+)?%?|20\d{2}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*日')
WINDOW = 16


def _norm(text: str) -> str:
    return re.sub(r'\s+', '', str(text or ''))


def unsupported_numbers(copy_text: str, facts: list[dict[str, Any]]) -> list[str]:
    allowed = _norm(' '.join(f"{f.get('text') or ''} {f.get('quote') or ''}" for f in facts))
    return sorted({atom for atom in NUMBER_RE.findall(copy_text or '')
                   if _norm(atom) not in allowed})


def _windows(text: str, size: int = WINDOW) -> set[str]:
    compact = _norm(text)
    if len(compact) < size:
        return set()
    return {compact[i:i + size] for i in range(len(compact) - size + 1)}


def reference_overlap(copy_text: str, library: dict[str, Any] | None,
                      min_window: int = WINDOW,
                      event_url: str = '') -> list[str]:
    """Return reference ids whose long literal windows appear in the copy."""
    if not library:
        return []
    copy_windows = _windows(copy_text, min_window)
    if not copy_windows:
        return []
    hits = []
    for sample in library.get('samples') or []:
        if event_url and str(sample.get('url') or '') == event_url:
            continue
        excerpt = str(sample.get('excerpt') or '')
        if len(_norm(excerpt)) < min_window:
            continue
        if copy_windows & _windows(excerpt, min_window):
            hits.append(str(sample.get('ref_id')))
    return hits


def evaluate_creative(creative: dict[str, Any], facts: list[dict[str, Any]],
                      library: dict[str, Any] | None = None,
                      event_url: str = '') -> dict[str, Any]:
    creative = creative or {}
    copy_text = str(creative.get('copy_draft') or '')
    checks = {
        'required_fields': all(str(creative.get(field) or '').strip() for field in REQUIRED),
        'preconditions': (isinstance(creative.get('preconditions'), list)
                          and len(creative['preconditions']) >= 2),
        'copy_length': 20 <= len(_norm(copy_text)) <= 2000,
        'unsupported_numbers': len(unsupported_numbers(copy_text, facts)) == 0,
        'patterns_recorded': bool(creative.get('used_patterns')),
        'references_recorded': bool(creative.get('reference_samples')),
        'no_reference_copy': not reference_overlap(copy_text, library, event_url=event_url),
        'status_declared': str(creative.get('status') or '') ==
                           'draft_needs_operational_confirmation',
    }
    return {'checks': checks, 'passed': all(checks.values()),
            'unsupported_numbers': unsupported_numbers(copy_text, facts),
            'reference_overlap': reference_overlap(copy_text, library, event_url=event_url)}
