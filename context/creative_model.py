"""Model-generated growth creative constrained by the TapTap reference library.

Only structure/rhythm patterns are given to the model, never sample excerpts.
The output is validated for required fields and for not inventing obvious
numeric/date atoms absent from the facts; failures return a structured error and
the caller keeps the deterministic draft.
"""
from __future__ import annotations

import json
import re
from typing import Any, Protocol

from .creative_quality import evaluate_creative
from .reference_library import select_patterns

REQUIRED_FIELDS = ('title', 'hook', 'audience_need', 'proposed_action',
                   'copy_draft', 'preconditions', 'status')
NUMBER_RE = re.compile(r'\d+(?:\.\d+)?%?|20\d{2}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*日')


class CreativeClient(Protocol):
    def complete(self, prompt: str, *, model: str, max_tokens: int,
                 event_id: str, session: str) -> dict[str, Any]:
        ...


def _parse_json(text: str) -> dict[str, Any]:
    raw = str(text or '').strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```[a-zA-Z]*\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
    start, end = raw.find('{'), raw.rfind('}')
    if start < 0 or end < 0:
        raise ValueError('模型输出中没有 JSON 对象')
    value = json.loads(raw[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError('模型输出不是 JSON 对象')
    return value


def build_prompt(event: dict[str, Any], intel: dict[str, Any], material: dict[str, Any],
                 facts: list[dict[str, Any]], library: dict[str, Any]) -> str:
    selected = select_patterns(library, event, intel, material)
    patterns = [
        {'id': pattern.get('id'), 'name': pattern.get('name'), 'structure': pattern.get('structure'),
         'use_when': pattern.get('use_when'), 'avoid': pattern.get('avoid')}
        for pattern in selected
    ]
    pattern_ids = [pattern.get('id') for pattern in selected]
    fact_lines = [f"- {fact.get('text')}｜引文：{fact.get('quote')}" for fact in facts]
    return f'''你是 TapTap 内容运营助手。根据事件、事实、已有情报和素材，生成一个**增长创意草案**。

事件标题：{event.get('title')}
已有情报：{intel.get('one_line')}
素材草案：{material.get('draft')}
素材使用边界：{material.get('usage_boundary')}
未知项：{json.dumps(intel.get('unknowns') or [], ensure_ascii=False)}
事实（只能使用这些，不得补写未给出的信息）：
{chr(10).join(fact_lines)}
参考结构（只迁移结构和节奏，不要复制任何参考原文或画面）：
{json.dumps(patterns, ensure_ascii=False, indent=2)}

要求：
- 只输出合法 JSON，不要 Markdown 代码围栏，不要解释；
- 字段：title, hook, audience_need, proposed_action, copy_draft, preconditions, status, used_pattern_ids；
- used_pattern_ids 从下列已选模式 id 中选择 1-3 个，不得自行新增模式；
- 不得编造事实未给出的时间、数字、入口、奖励或官方承诺；
- 不得把“玩家需求/效果”写成已证实；
- copy_draft 只能以素材草案为基础做轻微改写，不得扩展事实细节；
- 对玩家自述、未核实指控只做最低限度转述，不得把指控细节作为钩子或在正文中反复复述；
- preconditions 至少 2 条；
- status 固定为 draft_needs_operational_confirmation；
- copy_draft 用中文，包含可互动的提问，但不承诺奖励或入口。'''

def _unsupported_numbers(copy_text: str, facts: list[dict[str, Any]]) -> list[str]:
    allowed = ''.join(f"{fact.get('text') or ''} {fact.get('quote') or ''}" for fact in facts)
    allowed_norm = re.sub(r'\s+', '', allowed)
    bad = []
    for atom in NUMBER_RE.findall(copy_text or ''):
        if re.sub(r'\s+', '', atom) not in allowed_norm:
            bad.append(atom)
    return sorted(set(bad))


def generate_with_model(event: dict[str, Any], intel: dict[str, Any],
                        material: dict[str, Any], facts: list[dict[str, Any]],
                        library: dict[str, Any], client: CreativeClient, *,
                        model: str = 'hy4-flash-free', max_tokens: int = 8000,
                        session: str = 'v4-creative') -> dict[str, Any]:
    prompt = build_prompt(event, intel, material, facts, library)
    selected = select_patterns(library, event, intel, material)
    pattern_ids = [pattern.get('id') for pattern in selected]
    result = client.complete(prompt, model=model, max_tokens=max_tokens,
                             event_id=str(event.get('event_id') or 'creative'),
                             session=session)
    try:
        value = _parse_json(result.get('content') or '')
    except Exception as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}',
                'raw': str(result.get('content') or '')[:4000],
                'model': result.get('model') or model}
    missing = [field for field in REQUIRED_FIELDS if not value.get(field)]
    if missing:
        return {'ok': False, 'error': 'missing fields: ' + ','.join(missing),
                'raw': str(result.get('content') or '')[:4000],
                'model': result.get('model') or model}
    if not isinstance(value.get('preconditions'), list) or len(value['preconditions']) < 2:
        return {'ok': False, 'error': 'preconditions must be a list of at least 2 items',
                'raw': str(result.get('content') or '')[:4000],
                'model': result.get('model') or model}
    bad_numbers = _unsupported_numbers(str(value.get('copy_draft') or ''), facts)
    if bad_numbers:
        return {'ok': False, 'error': 'unsupported numeric atoms: ' + ','.join(bad_numbers),
                'raw': str(result.get('content') or '')[:4000],
                'model': result.get('model') or model}
    chosen_ids = value.get('used_pattern_ids')
    if (not isinstance(chosen_ids, list) or not chosen_ids or
            len(chosen_ids) > 3 or any(item not in pattern_ids for item in chosen_ids)):
        return {'ok': False, 'error': 'used_pattern_ids must select 1-3 offered ids',
                'raw': str(result.get('content') or '')[:4000],
                'model': result.get('model') or model}
    chosen = [pattern for pattern in selected if pattern.get('id') in chosen_ids]
    refs = []
    for pattern in chosen:
        for ref in pattern.get('source_refs') or []:
            sample = next((s for s in (library.get('samples') or [])
                           if s.get('ref_id') == ref), None)
            if sample and ref not in [item.get('ref_id') for item in refs]:
                refs.append({'ref_id': ref, 'url': sample.get('url'),
                             'title': sample.get('title'),
                             'rights_status': sample.get('rights_status')})
    quality = evaluate_creative({
        **value, 'used_patterns': [pattern.get('name') for pattern in chosen],
        'reference_samples': refs,
    }, facts, library, event_url=str(event.get('url') or ''))
    if not quality['passed']:
        return {'ok': False, 'error': 'quality_failed: ' + ','.join(
            key for key, ok in quality['checks'].items() if not ok),
                'raw': str(result.get('content') or '')[:4000],
                'model': result.get('model') or model}
    value['quality'] = quality
    value.update({
        'schema': 'growth-creative-model-v0.1',
        'type': 'growth_creative',
        'used_patterns': [pattern.get('name') for pattern in chosen],
        'reference_samples': refs,
        'generator': 'model',
        'model': result.get('model') or model,
        'usage': result.get('usage') or {},
        'note': '模型生成草案，受参考结构约束；未做事实核验，引用/发布前核查授权。',
    })
    return {'ok': True, 'creative': value}
