"""Review state for V4 deliveries; automatic checks never imply approval."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .file_io import atomic_write_json, atomic_write_text, output_lock

REVIEW_STATUSES = ('draft', 'needs_review', 'approved')
DEFAULT_RISKS = ['independent_fact_check_pending', 'human_approval_pending']


def _content_hash(package: dict[str, Any]) -> str:
    content = {key: package.get(key) for key in
               ('event', 'intelligence', 'material', 'growth_creative', 'checks')}
    evidence = dict(package.get('evidence') or {})
    evidence.pop('fetched_at', None)
    content['evidence'] = evidence
    creative = dict(content['growth_creative'] or {})
    creative.pop('quality', None)
    creative.pop('usage', None)
    content['growth_creative'] = creative
    raw = json.dumps(content, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def review_package(package: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Attach explicit review metadata without overwriting an existing approval."""
    previous = previous or {}
    history = list(previous.get('review_history') or [])
    approved = previous.get('review', {}).get('status') == 'approved'
    current_hash = _content_hash(package)
    previous_hash = previous.get('review', {}).get('content_hash')
    stale = not previous_hash or previous_hash != current_hash
    next_status = 'needs_review' if package.get('checks', {}).get('passed') else 'draft'
    if approved and (not package.get('checks', {}).get('passed') or stale):
        history.append({'previous_status': 'approved', 'status': next_status,
                        'reviewer': 'system', 'note': '内容或自动检查变化，原审核失效。',
                        'reviewed_at': datetime.now(timezone.utc).isoformat()})
        approved = False
    review = dict(previous.get('review') or {})
    if not approved:
        review = {
            'status': next_status,
            'reviewer': None,
            'note': '自动检查通过不等于人工审核通过。',
            'reviewed_at': None,
        }
    risks = list(DEFAULT_RISKS if review.get('status') != 'approved' else [])
    for risk in previous.get('review', {}).get('risks') or []:
        if risk not in risks:
            risks.append(risk)
    review['risks'] = risks
    review['content_hash'] = current_hash
    package['status'] = review['status']
    package['review'] = review
    package['review_history'] = history
    return package


def record_review(root: str | Path, event_id: str, status: str, reviewer: str,
                  note: str) -> dict[str, Any]:
    """Append a human review decision under the shared output lock."""
    if status not in REVIEW_STATUSES:
        raise ValueError(f'unsupported review status: {status}')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', event_id):
        raise ValueError('invalid event_id')
    if not reviewer.strip() or not note.strip():
        raise ValueError('reviewer and note are required')
    root = Path(root)
    path = root / event_id / 'delivery.json'
    with output_lock(root):
        package = json.loads(path.read_text(encoding='utf-8'))
        if status == 'approved' and not package.get('checks', {}).get('passed'):
            raise ValueError('cannot approve a delivery with failed automatic checks')
        now = datetime.now(timezone.utc).isoformat()
        previous = package.get('review') or {}
        risks = [risk for risk in (previous.get('risks') or []) if risk not in DEFAULT_RISKS]
        review = {'status': status, 'reviewer': reviewer.strip(), 'note': note.strip(),
                  'reviewed_at': now, 'risks': risks, 'content_hash': _content_hash(package)}
        history = list(package.get('review_history') or [])
        history.append({'previous_status': previous.get('status'), **review})
        package['status'] = status
        package['review'] = review
        package['review_history'] = history[-50:]
        from .delivery import render_html
        preview = render_html(package)
        index_path = root / 'index.json'
        index = json.loads(index_path.read_text(encoding='utf-8')) if index_path.is_file() else {}
        for item in index.get('deliveries') or []:
            if item.get('event_id') == event_id:
                item['status'] = status
                item['review_status'] = status
                item['review_risks'] = risks
        atomic_write_text(root / event_id / 'preview.html', preview)
        atomic_write_json(path, package)
        atomic_write_json(index_path, index)
        return review


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)
