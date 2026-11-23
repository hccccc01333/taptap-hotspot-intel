"""Model health preflight and selection (V4 model routing MVP).

No model is assumed permanently available: each candidate gets one tiny JSON
health check; the router only selects a model whose check passed and whose
declared capability is allowed by policy. LongCat is skipped by default
(capped); Iceman is skipped by default because its terms allow training on
prompts/completions.

The health check itself is not a quality evaluation: it only answers
"can this model return a small valid JSON response right now?".
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

HEALTH_PROMPT = '只输出合法 JSON：{"ok": true}。不要解释，不要 Markdown 代码围栏。'
HEALTH_MAX_TOKENS = 1024

#: Declared capabilities; unknown models default to "no training, not capped".
MODEL_CAPABILITIES: dict[str, dict[str, Any]] = {
    'hy4-flash-free': {'training': False, 'capped': False, 'note': 'free; stability unverified'},
    'beam-free': {'training': False, 'capped': False, 'note': 'free; region-restricted observed'},
    'iceman-contributor-free': {'training': True, 'capped': False,
                                'note': 'terms allow training on prompts/completions'},
    'longcat-2.5-free': {'training': False, 'capped': True, 'note': '100-call cap'},
}


class HealthClient(Protocol):
    def complete(self, prompt: str, *, model: str, max_tokens: int,
                 event_id: str, session: str) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class ModelHealth:
    model: str
    status: str  # 'ok' | 'failed' | 'skipped'
    reason: str = ''
    latency_ms: int = 0
    finish_reason: str = ''
    json_valid: bool = False
    content_chars: int = 0
    cost_zero: bool = False
    training: bool = False
    capped: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 'ok'

    def as_dict(self) -> dict[str, Any]:
        return {'model': self.model, 'status': self.status, 'reason': self.reason,
                'latency_ms': self.latency_ms, 'finish_reason': self.finish_reason,
                'json_valid': self.json_valid, 'content_chars': self.content_chars,
                'cost_zero': self.cost_zero, 'training': self.training,
                'capped': self.capped}


@dataclass(frozen=True)
class ModelSelection:
    model: str | None
    fallback_from: tuple[str, ...] = ()
    reason: str = ''
    health: tuple[ModelHealth, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {'model': self.model, 'fallback_from': list(self.fallback_from),
                'reason': self.reason, 'health': [item.as_dict() for item in self.health]}


def capabilities(model: str) -> dict[str, Any]:
    return dict(MODEL_CAPABILITIES.get(model, {'training': False, 'capped': False,
                                                'note': 'unknown model'}))


def _parse_json(text: str) -> bool:
    raw = str(text or '').strip()
    if raw.startswith('```'):
        import re
        raw = re.sub(r'^```[a-zA-Z]*\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
    start, end = raw.find('{'), raw.rfind('}')
    if start < 0 or end < 0:
        return False
    try:
        value = json.loads(raw[start:end + 1])
    except ValueError:
        return False
    return isinstance(value, dict) and value.get('ok') is True


def check_model(client: HealthClient, model: str, *, session: str = 'model-preflight',
                max_tokens: int = HEALTH_MAX_TOKENS) -> ModelHealth:
    cap = capabilities(model)
    started = time.monotonic()
    try:
        result = client.complete(HEALTH_PROMPT, model=model, max_tokens=max_tokens,
                                 event_id='preflight', session=session)
        elapsed = int((time.monotonic() - started) * 1000)
        content = str(result.get('content') or '')
        usage = result.get('usage') or {}
        finish = str(result.get('finish_reason') or '')
        json_valid = _parse_json(content)
        cost = usage.get('cost')
        cost_zero = bool(cost == 0 or (usage.get('opencode') or {}).get('list_cost') == 0)
        ok = bool(json_valid and content.strip() and finish != 'length')
        reason = 'ok' if ok else (
            'empty_content' if not content.strip() else
            'finish_reason_length' if finish == 'length' else
            'invalid_json')
        return ModelHealth(model=model, status='ok' if ok else 'failed', reason=reason,
                           latency_ms=elapsed, finish_reason=finish, json_valid=json_valid,
                           content_chars=len(content), cost_zero=cost_zero,
                           training=bool(cap.get('training')), capped=bool(cap.get('capped')))
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return ModelHealth(model=model, status='failed',
                           reason=f'{type(exc).__name__}: {exc}',
                           latency_ms=elapsed, training=bool(cap.get('training')),
                           capped=bool(cap.get('capped')))


def preflight(client: HealthClient, candidates: Iterable[str], *, session: str = 'model-preflight',
              allow_training: bool = False, allow_capped: bool = False) -> list[ModelHealth]:
    results: list[ModelHealth] = []
    for model in candidates:
        cap = capabilities(model)
        if cap.get('capped') and not allow_capped:
            results.append(ModelHealth(model=model, status='skipped',
                                       reason='capped_model_disabled',
                                       training=bool(cap.get('training')), capped=True))
            continue
        if cap.get('training') and not allow_training:
            results.append(ModelHealth(model=model, status='skipped',
                                       reason='training_model_disabled',
                                       training=True, capped=bool(cap.get('capped'))))
            continue
        results.append(check_model(client, model, session=session))
    return results


def eligible_models(health: Iterable[ModelHealth], candidates: Iterable[str], *,
                    allow_training: bool = False,
                    allow_capped: bool = False) -> tuple[ModelHealth, ...]:
    """Healthy candidates in caller order, after capabilities/policy filtering."""
    by_model = {item.model: item for item in health}
    result: list[ModelHealth] = []
    for model in candidates:
        cap = capabilities(model)
        if cap.get('capped') and not allow_capped:
            continue
        if cap.get('training') and not allow_training:
            continue
        item = by_model.get(model)
        if item is not None and item.ok:
            result.append(item)
    return tuple(result)


def select_model(health: Iterable[ModelHealth], candidates: Iterable[str], *,
                 allow_training: bool = False, allow_capped: bool = False) -> ModelSelection:
    """Pick the first healthy candidate allowed by policy; keep all health records."""
    health_list = tuple(health)
    by_model = {item.model: item for item in health_list}
    failed: list[str] = []
    for model in candidates:
        cap = capabilities(model)
        if cap.get('capped') and not allow_capped:
            failed.append(f'{model}:capped_disabled')
            continue
        if cap.get('training') and not allow_training:
            failed.append(f'{model}:training_disabled')
            continue
        item = by_model.get(model)
        if item is None:
            failed.append(f'{model}:not_checked')
            continue
        if item.ok:
            return ModelSelection(model=model, fallback_from=tuple(failed),
                                  reason='first healthy allowed candidate',
                                  health=health_list)
        failed.append(f'{model}:{item.reason}')
    return ModelSelection(model=None, fallback_from=tuple(failed),
                          reason='no healthy allowed model', health=health_list)
