"""Minimal TapTap reference library and reference-driven creative draft.

The library is a local JSON snapshot of **public** TapTap observations plus
project-authored structure patterns. Patterns carry source references but the
generator only transfers structure/rhythm, never copies source text; every
creative records its references and rights caveat.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .creative_quality import evaluate_creative

REFERENCE_SCHEMA = 'taptap-reference-library-v0.1'


def load_library(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('schema') != REFERENCE_SCHEMA:
        raise ValueError(f"unsupported reference library schema: {data.get('schema')}")
    samples = data.get('samples') or []
    patterns = data.get('patterns') or []
    if not isinstance(samples, list) or not samples:
        raise ValueError('reference library has no samples')
    if not isinstance(patterns, list) or not patterns:
        raise ValueError('reference library has no patterns')
    ids = {sample.get('ref_id') for sample in samples if sample.get('ref_id')}
    for pattern in patterns:
        missing = [ref for ref in (pattern.get('source_refs') or []) if ref not in ids]
        if missing:
            raise ValueError(f"pattern {pattern.get('id')} references unknown samples: {missing}")
    return data


def select_patterns(library: dict[str, Any], event: dict[str, Any],
                    intel: dict[str, Any], material: dict[str, Any],
                    limit: int = 3) -> list[dict[str, Any]]:
    """Rank patterns by declared applicability and keep the strongest matches.

    The event's own sample may inform structure because excerpts are never sent
    to the model; literal-copy checking is handled separately. If a library has
    no applicability metadata, preserve its declared order.
    """
    patterns = library.get('patterns') or []
    candidates = list(enumerate(patterns))
    text = ' '.join(str(item or '') for item in (
        event.get('title'), event.get('objective'), intel.get('one_line'),
        intel.get('summary'), material.get('title'), material.get('draft')))
    ranked = []
    for index, pattern in candidates:
        keywords = pattern.get('apply_keywords') or []
        score = sum(1 for keyword in keywords if keyword and keyword in text)
        ranked.append((-score, index, pattern))
    ranked.sort(key=lambda row: (row[0], row[1]))
    if ranked and ranked[0][0] < 0:
        ranked = [row for row in ranked if row[0] == ranked[0][0]]
    return [row[2] for row in ranked[:max(1, limit)]]


def build_creative_with_references(intel: dict[str, Any], material: dict[str, Any],
                                    facts: list[dict[str, Any]],
                                    event: dict[str, Any],
                                    library: dict[str, Any]) -> dict[str, Any]:
    """Deterministic creative draft using selected TapTap structure patterns."""
    one_line = str(intel.get('one_line') or intel.get('summary') or '')[:160]
    fact_texts = [str(fact.get('text') or '') for fact in facts if fact.get('text')]
    used = select_patterns(library, event, intel, material)
    refs = []
    for pattern in used:
        for ref in pattern.get('source_refs') or []:
            if ref not in [item.get('ref_id') for item in refs]:
                sample = next((s for s in library['samples'] if s.get('ref_id') == ref), None)
                if sample:
                    refs.append({'ref_id': ref, 'url': sample.get('url'),
                                 'title': sample.get('title'),
                                 'rights_status': sample.get('rights_status')})
    boundary = str(material.get('usage_boundary') or '')
    fact_lines = '\n'.join(f'- {text}' for text in fact_texts[:3])
    question = str(material.get('draft') or '').strip()
    if not question or question == '文案':
        question = '关于这条消息，你最想继续确认或讨论哪一点？'
    creative = {
        'schema': 'growth-creative-reference-v0.1',
        'type': 'growth_creative',
        'title': f"{one_line}｜玩家互动角度草案",
        'hook': one_line,
        'audience_need': str(intel.get('why_taptap') or
                             '待验证：需要站内讨论样本确认玩家需求。'),
        'proposed_action': '在 TapTap 对应游戏社区发起与事件直接相关的讨论；入口与运营资源待确认。',
        'copy_draft': (f"【标题】{one_line}\n\n【事实速览】\n{fact_lines}\n\n"
                       f"【互动问题】{question}\n\n"
                       f"【边界】{boundary or '不补写未确认信息，不复制参考原文与画面，不承诺未公布内容。'}"),
        'used_patterns': [pattern.get('name') for pattern in used],
        'used_pattern_ids': [pattern.get('id') for pattern in used],
        'reference_samples': refs,
        'preconditions': [
            '确认 TapTap 游戏页/社区话题与可发布位置',
            '取得讨论样本或运营确认，避免把判断写成玩家需求',
            '核查参考样本与素材授权，不复制原文/画面',
        ],
        'status': 'draft_needs_operational_confirmation',
        'source_facts': fact_texts[:5],
        'pattern_sources': [pattern.get('source_refs') for pattern in used],
        'note': '结构参考来自公开 TapTap 观察样本；不是官方规范，未复制原文。',
    }
    creative['quality'] = evaluate_creative(creative, facts, library,
                                               event_url=str(event.get('url') or ''))
    return creative
