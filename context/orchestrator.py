"""Minimal dynamic orchestration for one content task (V4 §6/§7 MVP).

The orchestrator is a capability controller, not a new LLM agent:

1. draft with the unlimited model (Beam Free by default);
2. deterministic quality check (facts count, verbatim quote support, unsupported
   numeric/date atoms, material fields);
3. **only if** a predeclared gate fails and repair budget remains, delegate one
   bounded repair task to the same model with the failed output and gaps;
4. re-check; only a passing draft is eligible for an optional final-model call
   (LongCat is never called automatically by this module unless the caller
   passes ``final_model`` and the draft passes every gate).

Every decision, model, usage and gap is recorded in an :class:`OrchestrationTrace`.
No publication, no V3 production write.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .evaluate import score_content


class CompletionClient(Protocol):
    def complete(self, prompt: str, *, model: str, max_tokens: int,
                 event_id: str, session: str) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class DeliveryPolicy:
    """Predeclared gates. Changing them requires a new experiment version."""
    min_facts: int = 8
    max_unsupported_numeric: int = 0
    require_full_quote_support: bool = True
    require_material: bool = True
    max_repairs: int = 1
    max_tokens: int = 12000

    def __post_init__(self) -> None:
        if self.min_facts < 1 or self.max_repairs < 0 or self.max_tokens < 256:
            raise ValueError('DeliverPolicy 参数无效')


@dataclass
class OrchestrationTrace:
    event_id: str
    status: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    parsed: dict[str, Any] | None = None
    score: dict[str, Any] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    repair_count: int = 0
    usage_total: dict[str, int] = field(default_factory=dict)
    context_card: dict[str, Any] | None = None
    final_prompt_mode: str = 'baseline'
    model_route: dict[str, Any] | None = None
    error: str = ''

    def add_usage(self, usage: dict[str, Any] | None) -> None:
        usage = usage or {}
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            value = usage.get(key)
            if isinstance(value, int):
                self.usage_total[key] = self.usage_total.get(key, 0) + value

    def as_dict(self) -> dict[str, Any]:
        return {'event_id': self.event_id, 'status': self.status, 'steps': self.steps,
                'parsed': self.parsed, 'score': self.score, 'gaps': self.gaps,
                'repair_count': self.repair_count, 'usage_total': self.usage_total,
                'context_card': self.context_card,
                'final_prompt_mode': self.final_prompt_mode,
                'model_route': self.model_route, 'error': self.error}


def quality_gaps(score: dict[str, Any], policy: DeliveryPolicy) -> list[str]:
    gaps: list[str] = []
    if int(score.get('facts_total') or 0) < policy.min_facts:
        gaps.append(f"facts_below_min:{score.get('facts_total', 0)}<{policy.min_facts}")
    if policy.require_full_quote_support and float(score.get('quote_support_rate') or 0) < 1.0:
        gaps.append(f"quote_support:{score.get('facts_supported', 0)}/{score.get('facts_total', 0)}")
    if int(score.get('unsupported_numeric_count') or 0) > policy.max_unsupported_numeric:
        gaps.append('unsupported_numeric_atoms:' +
                    ','.join(score.get('unsupported_numeric_atoms') or []))
    if policy.require_material and not score.get('material_complete'):
        missing = [key for key, ok in (score.get('material_fields') or {}).items() if not ok]
        gaps.append('material_incomplete:' + ','.join(missing))
    return gaps


def build_repair_prompt(base_prompt: str, previous: str, gaps: list[str],
                        policy: DeliveryPolicy) -> str:
    return base_prompt + f"""

上一版草稿未通过以下自动检查：
{json.dumps(gaps, ensure_ascii=False)}

上一版输出（只用于修正，不要保留其中的错误）：
\"\"\"{previous[:6000]}\"\"\"

请输出完整修正版 JSON，要求：
- facts 不少于 {policy.min_facts} 条，且每条 quote 必须逐字来自原文；
- 不出现原文没有的数字/日期；
- material 的 title/channel/draft/usage_boundary 全部非空；
- 不要输出思考过程或 Markdown 代码围栏。"""


def _parse(content: str) -> dict[str, Any]:
    raw = str(content or '').strip()
    if raw.startswith('```'):
        import re
        raw = re.sub(r'^```[a-zA-Z]*\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
    start, end = raw.find('{'), raw.rfind('}')
    if start < 0 or end < 0:
        raise ValueError('模型输出中没有 JSON 对象')
    value = json.loads(raw[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError('模型输出 JSON 不是对象')
    return value


def run_orchestrated(*, event_id: str, base_prompt: str, body: str, client: CompletionClient,
                     policy: DeliveryPolicy | None = None,
                     draft_model: str = 'beam-free',
                     final_model: str | None = None,
                     final_prompt: str | None = None,
                     context_card_builder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                     context_prompt_builder: Callable[[dict[str, Any]], str] | None = None,
                     candidate_models: tuple[str, ...] | None = None,
                     model_health: Any = None, allow_training: bool = False,
                     allow_capped: bool = False,
                     session: str = 'orchestrator') -> OrchestrationTrace:
    policy = policy or DeliveryPolicy()
    trace = OrchestrationTrace(event_id=event_id, status='drafting')
    try:
        draft = None
        if candidate_models:
            from .model_router import eligible_models, select_model
            route = select_model(model_health or (), candidate_models,
                                 allow_training=allow_training, allow_capped=allow_capped)
            trace.model_route = route.as_dict()
            trace.steps.append({'stage': 'model_route',
                                'decision': 'selected' if route.model else 'failed',
                                'model': route.model,
                                'fallback_from': list(route.fallback_from),
                                'reason': route.reason})
            if route.model is None:
                trace.status = 'failed'
                trace.error = 'no_healthy_model: ' + route.reason
                return trace
            eligible = eligible_models(model_health or (), candidate_models,
                                       allow_training=allow_training,
                                       allow_capped=allow_capped)
            queue = [route.model] + [item.model for item in eligible
                                     if item.model != route.model]
            last_error: Exception | None = None
            for index, model in enumerate(queue):
                try:
                    draft = client.complete(base_prompt, model=model,
                                            max_tokens=policy.max_tokens,
                                            event_id=event_id, session=session)
                    if index:
                        trace.steps.append({'stage': 'model_fallback',
                                            'decision': 'succeeded', 'model': model})
                    draft_model = model
                    break
                except Exception as exc:
                    last_error = exc
                    trace.steps.append({'stage': 'model_attempt', 'model': model,
                                        'decision': 'failed',
                                        'error': f'{type(exc).__name__}: {exc}'})
            if draft is None:
                raise last_error or RuntimeError('no model call succeeded')
        else:
            draft = client.complete(base_prompt, model=draft_model,
                                    max_tokens=policy.max_tokens, event_id=event_id,
                                    session=session)
        trace.add_usage(draft.get('usage'))
        draft_value = _parse(draft['content'])
        score = score_content(draft_value, body)
        gaps = quality_gaps(score, policy)
        trace.steps.append({'stage': 'draft', 'model': draft.get('model') or draft_model,
                            'decision': 'accepted' if not gaps else 'rejected',
                            'gaps': gaps, 'usage': draft.get('usage') or {},
                            'content_chars': len(draft['content'])})
        trace.parsed, trace.score, trace.gaps = draft_value, score, gaps
        repair_count = 0
        while gaps and repair_count < policy.max_repairs:
            repair_count += 1
            prompt = build_repair_prompt(base_prompt, draft['content'], gaps, policy)
            repaired = client.complete(prompt, model=draft_model,
                                       max_tokens=policy.max_tokens,
                                       event_id=event_id, session=session)
            trace.add_usage(repaired.get('usage'))
            repaired_value = _parse(repaired['content'])
            repaired_score = score_content(repaired_value, body)
            repaired_gaps = quality_gaps(repaired_score, policy)
            trace.steps.append({'stage': 'repair', 'attempt': repair_count,
                                'model': repaired.get('model') or draft_model,
                                'decision': 'accepted' if not repaired_gaps else 'rejected',
                                'gaps': repaired_gaps, 'usage': repaired.get('usage') or {},
                                'content_chars': len(repaired['content'])})
            trace.repair_count = repair_count
            trace.parsed, trace.score, trace.gaps = repaired_value, repaired_score, repaired_gaps
            gaps = repaired_gaps
        if gaps:
            trace.status = 'needs_review'
            return trace
        if final_model:
            prompt_for_final = final_prompt or base_prompt
            if context_card_builder is not None and context_prompt_builder is not None:
                card = context_card_builder(trace.parsed or {})
                trace.context_card = card
                prompt_for_final = context_prompt_builder(card)
                trace.final_prompt_mode = 'context_card'
                trace.steps.append({'stage': 'context', 'decision': 'built',
                                    'platforms': card.get('platforms') or [],
                                    'dates': card.get('dates') or [],
                                    'prices': card.get('prices') or []})
            final = client.complete(prompt_for_final, model=final_model,
                                    max_tokens=policy.max_tokens, event_id=event_id,
                                    session=session)
            trace.add_usage(final.get('usage'))
            try:
                final_value = _parse(final['content'])
            except Exception as exc:
                trace.steps.append({'stage': 'final', 'model': final_model,
                                    'decision': 'parse_failed',
                                    'error': f'{type(exc).__name__}: {exc}',
                                    'usage': final.get('usage') or {},
                                    'content_chars': len(final.get('content') or ''),
                                    'raw': str(final.get('content') or '')[:4000]})
                trace.error = f'final_parse_failed: {exc}'
                trace.status = 'final_failed'
                return trace
            final_score = score_content(final_value, body)
            final_gaps = quality_gaps(final_score, policy)
            trace.steps.append({'stage': 'final', 'model': final_model,
                                'decision': 'accepted' if not final_gaps else 'rejected',
                                'gaps': final_gaps, 'usage': final.get('usage') or {},
                                'content_chars': len(final['content'])})
            trace.parsed, trace.score, trace.gaps = final_value, final_score, final_gaps
            trace.status = 'final_delivered' if not final_gaps else 'needs_review'
            return trace
        trace.status = 'ready_for_final'
        return trace
    except Exception as exc:
        trace.error = f'{type(exc).__name__}: {exc}'
        trace.status = 'failed'
        return trace
