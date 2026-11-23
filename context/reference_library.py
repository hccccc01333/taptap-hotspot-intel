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


def _pattern(library: dict[str, Any], pattern_id: str) -> dict[str, Any] | None:
    for pattern in library.get('patterns') or []:
        if pattern.get('id') == pattern_id:
            return pattern
    return None


def build_creative_with_references(intel: dict[str, Any], material: dict[str, Any],
                                   facts: list[dict[str, Any]],
                                   event: dict[str, Any],
                                   library: dict[str, Any]) -> dict[str, Any]:
    """Deterministic creative draft using a small set of TapTap structure patterns."""
    one_line = str(intel.get('one_line') or intel.get('summary') or '')[:160]
    summary = str(intel.get('summary') or '')[:400]
    fact_texts = [str(fact.get('text') or '') for fact in facts if fact.get('text')]
    preferred_ids = ('official_3items', 'official_brand_story', 'nostalgia_hook',
                     'news_reaction', 'player_contrast', 'community_emotion',
                     'community_question', 'complaint_question')
    available = {pattern.get('id'): pattern for pattern in (library.get('patterns') or [])}
    used = [available[pattern_id] for pattern_id in preferred_ids
            if pattern_id in available][:3]
    if not used:
        used = list(library.get('patterns') or [])[:3]
    pattern_names = [pattern.get('name') for pattern in used]
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
    copy_draft = (
        f"【标题】{one_line}\n\n"
        f"【开头·童年记忆/官宣节奏】{summary[:220]}\n\n"
        f"【三项信息速览】\n" +
        '\n'.join(f"- {text}" for text in fact_texts[:3]) +
        f"\n\n【互动问题】如果联动真的落地，你最想先看到哪个角色能力进入对局？\n\n"
        f"【边界】{boundary or '不补写未确认信息，不复制参考原文与画面，不承诺联动内容之外的东西。'}"
    )
    creative = {
        'schema': 'growth-creative-reference-v0.1',
        'type': 'growth_creative',
        'title': f"{one_line}｜玩家互动角度草案",
        'hook': one_line,
        'audience_need': str(intel.get('why_taptap') or
                             '待验证：需要站内讨论样本确认玩家需求。'),
        'proposed_action': ('在 TapTap 对应游戏社区发起“最期待哪个能力进入对局”的讨论；'
                            '入口与运营资源待确认。'),
        'copy_draft': copy_draft,
        'used_patterns': pattern_names,
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
