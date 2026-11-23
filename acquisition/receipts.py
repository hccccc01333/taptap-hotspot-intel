"""工具收据（Receipt）的构建与校验。

收据是工具结果的统一结构，必须能回答「哪个任务、用哪个后端、对哪个页面版本、
做了什么动作、得到什么证据、下一步是什么」。本模块只做构造与校验；
持久化在 :mod:`acquisition.store`，执行在 :mod:`acquisition.harness`。

四条校验底线：

- 阻塞页面/阻塞动作的收据**不得携带证据**（截图 OCR 成功不等于可以引用）；
- ``verified=True`` 的证据必须记录 verifier，提取动作不能给自己发合格证；
- 收据内证据的 ``page_version`` 必须与收据的页面版本一致（证据绑定页面版本）；
- ``next`` 默认由证据有无与阻塞状态推导，调用方只能在合法枚举内显式覆盖。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .model import (
    ACTION_KINDS,
    BACKENDS,
    EXTRACTIONS,
    NEXT_STEPS,
    RECEIPT_STATUSES,
    SCHEMA_VERSION,
    ZERO_COST,
    Action,
    Cost,
    Evidence,
    Observation,
    PageView,
    Receipt,
    ResearchGoal,
    now_iso,
)

_BLOCKED_STOP_STATUSES = ('blocked', 'stale', 'budget_exhausted')


def _fail(message: str) -> None:
    raise ValueError(message)


def receipt_key(*, task_id: str, backend: str, page_version: str, action: Action) -> str:
    """执行前计算幂等键；与 :meth:`acquisition.model.Receipt.key` 保持同一公式。"""
    if not isinstance(action, Action):
        raise TypeError('action 必须是 Action')
    probe = Receipt(task_id=task_id, goal='', backend=backend, action=action.kind,
                    status='completed', page_version=page_version,
                    region=action.region_id, method=action.method)
    return probe.key()


def retry_key(base_key: str, attempt: int) -> str:
    """重试动作的幂等键：同一动作第 n 次重试使用独立键，避免非 completed 收据
    被 ``INSERT OR IGNORE`` 静默丢弃（成本少记、证据与收据脱节）。

    ``attempt <= 0`` 返回基础键；``attempt >= 1`` 返回 ``<base>:retry:<n>``。
    """
    if not isinstance(base_key, str) or not base_key:
        raise ValueError('基础幂等键必须为非空字符串')
    if not _is_int(attempt):
        raise TypeError('attempt 必须为整数')
    if attempt <= 0:
        return base_key
    return f'{base_key}:retry:{attempt}'


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def derive_status(action: Action, observation: Observation, view: PageView | None) -> str:
    """默认收据状态：只有提取/交互动作才把「页面阻塞」视为收据阻塞。"""
    if observation.blocked:
        return 'blocked'
    if action.kind in ('extract', 'interact') and view is not None and view.blocked_reason:
        return 'blocked'
    return 'completed'


def derive_next(status: str, evidence: Iterable[Evidence]) -> str:
    """由收据状态与证据推导下一步。"""
    items = tuple(evidence)
    if status in _BLOCKED_STOP_STATUSES:
        return 'stop'
    if items:
        return 'verify_against_source'
    if status == 'failed':
        return 'retry'
    return 'continue'


def _checked_evidence(evidence: Iterable[Evidence]) -> tuple[Evidence, ...]:
    if evidence is None:
        return ()
    items = tuple(evidence)
    for index, item in enumerate(items):
        if not isinstance(item, Evidence):
            _fail(f'evidence[{index}] 必须是 Evidence')
        if item.verified and not item.verifier:
            _fail(f'evidence[{index}] verified=True 时必须记录 verifier')
    return items


def build_receipt(goal: ResearchGoal, backend: str, action: Action, observation: Observation,
                  *, view: PageView | None = None, evidence: Iterable[Evidence] = (),
                  status: str | None = None, next: str | None = None,
                  idempotency_key: str | None = None) -> Receipt:
    """构造一条收据；``status`` / ``next`` 未给出时按阻塞状态与证据推导。

    ``idempotency_key`` 是可选扩展：重试同一动作时调用方可传入
    :func:`retry_key` 生成的含尝试序号幂等键，使新收据与旧的非 completed
    收据分键共存（成本、证据与收据都不脱节）。省略时按
    :meth:`acquisition.model.Receipt.key` 派生基础键。
    """
    if not isinstance(goal, ResearchGoal):
        raise TypeError('goal 必须是 ResearchGoal')
    if backend not in BACKENDS:
        _fail(f'未知后端: {backend!r}')
    if not isinstance(action, Action):
        raise TypeError('action 必须是 Action')
    if not isinstance(observation, Observation):
        raise TypeError('observation 必须是 Observation')
    if view is not None and not isinstance(view, PageView):
        raise TypeError('view 必须是 PageView 或 None')
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not idempotency_key:
            _fail('idempotency_key 必须为非空字符串')

    items = _checked_evidence(evidence)
    final_status = status if status is not None else derive_status(action, observation, view)
    if final_status not in RECEIPT_STATUSES:
        _fail(f'未知收据状态: {final_status!r}')
    if final_status == 'blocked' and items:
        _fail('阻塞页面/阻塞动作的收据不得携带证据')
    if observation.blocked and items:
        _fail('阻塞动作的收据不得携带证据')
    if view is not None and view.blocked_reason and items:
        _fail('阻塞页面的收据不得携带证据')

    if next is not None and next not in NEXT_STEPS:
        _fail(f'未知下一步: {next!r}')
    if next is not None:
        final_next = next
    elif action.kind == 'stop':
        final_next = 'stop'
    else:
        final_next = derive_next(final_status, items)

    page_version = observation.page_version or (view.page_version if view is not None else '')
    receipt = Receipt(
        task_id=goal.task_id,
        goal=goal.question,
        backend=backend,
        action=action.kind,
        status=final_status,
        page_version=page_version,
        region=action.region_id or observation.region_id,
        method=action.method,
        evidence=items,
        cost=observation.cost if isinstance(observation.cost, Cost) else ZERO_COST,
        next=final_next,
        error=observation.note or None if final_status == 'failed' else None,
        note=observation.note,
        idempotency_key=idempotency_key or '',
    )
    return receipt.filled()


def validate_receipt(receipt: Receipt) -> Receipt:
    """校验已构造的收据对象；不合法时抛 ``ValueError``（store 落库前调用）。"""
    if not isinstance(receipt, Receipt):
        raise TypeError('receipt 必须是 Receipt')
    if receipt.backend not in BACKENDS:
        _fail(f'未知后端: {receipt.backend!r}')
    if receipt.action not in ACTION_KINDS:
        _fail(f'未知动作类型: {receipt.action!r}')
    if receipt.status not in RECEIPT_STATUSES:
        _fail(f'未知收据状态: {receipt.status!r}')
    if receipt.next not in NEXT_STEPS:
        _fail(f'未知下一步: {receipt.next!r}')
    if receipt.method is not None and receipt.method not in EXTRACTIONS:
        _fail(f'未知提取通道: {receipt.method!r}')
    if receipt.status == 'blocked' and receipt.evidence:
        _fail('阻塞页面/阻塞动作的收据不得携带证据')
    for index, item in enumerate(receipt.evidence):
        if not isinstance(item, Evidence):
            _fail(f'evidence[{index}] 必须是 Evidence')
        if item.verified and not item.verifier:
            _fail(f'evidence[{index}] verified=True 时必须记录 verifier')
        if receipt.page_version and item.page_version != receipt.page_version:
            _fail(f'evidence[{index}] 页面版本必须与收据页面版本一致')
    return receipt


def validate_receipt_payload(payload: Mapping[str, Any]) -> dict:
    """校验 ``Receipt.as_dict()`` 形态的字典；返回原字典（不修改内容）。"""
    if not isinstance(payload, Mapping):
        _fail('收据载荷必须是 JSON 对象')
    data = dict(payload)
    schema = data.get('schema')
    if schema is not None and schema != SCHEMA_VERSION:
        _fail(f'未知收据规范: {schema!r}')
    for field in ('task_id', 'backend', 'action', 'status', 'next'):
        if field not in data:
            _fail(f'收据载荷缺少字段: {field}')
    if not data.get('task_id'):
        _fail('task_id 不能为空')
    if data['backend'] not in BACKENDS:
        _fail(f'未知后端: {data["backend"]!r}')
    if data['action'] not in ACTION_KINDS:
        _fail(f'未知动作类型: {data["action"]!r}')
    if data['status'] not in RECEIPT_STATUSES:
        _fail(f'未知收据状态: {data["status"]!r}')
    if data['next'] not in NEXT_STEPS:
        _fail(f'未知下一步: {data["next"]!r}')
    method = data.get('method')
    if method is not None and method not in EXTRACTIONS:
        _fail(f'未知提取通道: {method!r}')
    evidence = data.get('evidence')
    if evidence is None:
        evidence = []
    if not isinstance(evidence, (list, tuple)):
        _fail('evidence 必须为数组')
    page_version = data.get('page_version') or ''
    for index, item in enumerate(evidence):
        if not isinstance(item, Mapping):
            _fail(f'evidence[{index}] 必须是 JSON 对象')
        if not item.get('capture_id'):
            _fail(f'evidence[{index}] 缺少 capture_id')
        item_version = item.get('page_version')
        if not item_version:
            _fail(f'evidence[{index}] 缺少 page_version')
        if page_version and item_version != page_version:
            _fail(f'evidence[{index}] 页面版本必须与收据页面版本一致')
        if item.get('extraction') not in EXTRACTIONS:
            _fail(f'evidence[{index}] 未知提取通道: {item.get("extraction")!r}')
        if item.get('verified') and not item.get('verifier'):
            _fail(f'evidence[{index}] verified=True 时必须记录 verifier')
    if data['status'] == 'blocked' and evidence:
        _fail('阻塞页面/阻塞动作的收据不得携带证据')
    return payload


__all__ = [
    'build_receipt',
    'derive_next',
    'derive_status',
    'receipt_key',
    'retry_key',
    'validate_receipt',
    'validate_receipt_payload',
]
