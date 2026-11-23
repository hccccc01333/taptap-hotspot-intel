"""V4 Acquisition Harness 的跨模块冻结契约（acquisition-contract-v0.1）。

本模块只定义数据结构与协议，不访问网络、不写数据库、不调用模型。
它是 ``acquisition`` 包内各模块（driver / policy / store / harness / skills / evaluate）
之间的唯一共享边界；修改这里等于修改契约，必须同时更新
``docs/V4_ARCHITECTURE.md`` 的契约章节并说明兼容影响。

四条不可协商的约束（与 V4 架构设计一致）：

1. 工具结果返回结构化 receipt，而不是一段自由文本；receipt 必须能回答
   「哪个任务、用哪个后端、对哪个页面版本、做了什么动作、得到什么证据、
   下一步是什么」。
2. **截图或 OCR 成功不等于事实成立。** :class:`Evidence` 的 ``verified`` 默认
   ``False``，只有独立 verifier 显式核对后才能变为 ``True``；提取动作本身
   没有任何路径可以把它设为真。
3. 所有证据绑定 ``page_version``。同一 URL 的页面内容变化后，旧证据只能作为
   历史版本，不能当作当前事实使用。
4. 策略只能通过 :class:`Action` 表达「想做什么」，预算、权限和阻塞页面由
   harness / router 约束；策略无法直接宣称某个事实已被证实。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Protocol, Sequence

SCHEMA_VERSION = 'acquisition-contract-v0.1'
TZ = timezone(timedelta(hours=8))

ACTION_KINDS = ('inspect', 'locate', 'extract', 'interact', 'stop')
BACKENDS = ('http', 'dom', 'playwright', 'ocr', 'vlm', 'skill', 'tabbit')
EXTRACTIONS = ('dom', 'http', 'ocr', 'vlm', 'skill')
RECEIPT_STATUSES = ('completed', 'failed', 'blocked', 'deferred', 'budget_exhausted', 'stale')
NEXT_STEPS = ('continue', 'verify_against_source', 'fallback', 'retry', 'stop', 'human_review')
TASK_STATUSES = ('pending', 'running', 'succeeded', 'failed', 'blocked', 'interrupted')
VERIFICATION_STATUSES = ('supported', 'needs_review', 'insufficient', 'unsupported', 'not_checked')
REGION_ROLES = ('navigation', 'article_body', 'activity_rules', 'comments', 'poster',
                'form', 'video', 'ad', 'login_wall', 'captcha', 'footer', 'section', 'unknown')

# 成本单位是 harness 内部的相对权重，用于约束执行，不是金额，也不进入事实质量评分。
DEFAULT_UNIT_WEIGHTS = {'inspect': 1, 'locate': 1, 'interact': 2, 'dom': 1, 'ocr': 3, 'vlm': 5, 'skill': 2}
OCR_CAPTURE_UNITS = DEFAULT_UNIT_WEIGHTS['ocr']
VLM_CAPTURE_UNITS = DEFAULT_UNIT_WEIGHTS['vlm']

# 回放驱动与固定基线共用的虚拟区域约定：
# - ``dom:body``：整页 DOM 正文（与 V3 browser_worker 的 body 读取对应）；
# - ``viewport:<i>``：按固定偏移截取的一屏（与 browser_worker.cjs 的三屏行为对应）。
# 固定三屏的偏移规则与现有实现一致，避免基线被我们写得比现状更弱。
DOM_BODY_REGION = 'dom:body'
VIEWPORT_REGION_PREFIX = 'viewport:'
FIXED_FRAME_COUNT = 3
FIXED_FRAME_STEP = 850
DEFAULT_VIEWPORT_HEIGHT = 1000

DATE_RE = re.compile(
    r'(?:19|20)\d{2}\s*(?:年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2})'
    r'|\d{1,2}\s*月\s*\d{1,2}\s*日'
)


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def canonical(value: Any) -> str:
    """稳定序列化：同一逻辑内容必须得到同一哈希，与字典插入顺序无关。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def stable_id(prefix: str, value: Any) -> str:
    return prefix + digest(value)[:24]


def derive_page_version(snapshot: Any) -> str:
    """页面内容版本：证据绑定的依据。不是 HTTP 响应时间，也不是抓取时间。"""
    return 'sha256:' + digest(snapshot)


def structural_signature(regions: Iterable['Region']) -> str:
    """页面结构签名：只保留区域角色序列，用于判断 Skill 是否仍适用于同类页面。

    轻微布局变化（区域移动、文本更新）不应改变签名；主导航、规则区、评论区的
    消失或换位会改变签名。签名匹配只是 Skill 可复用的必要条件，不是事实核验。
    """
    roles = [region.role for region in regions if region.role not in ('ad', 'footer')]
    return 'struct:' + hashlib.sha256(canonical(roles).encode('utf-8')).hexdigest()[:16]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bbox(value: Any) -> tuple[int, int, int, int]:
    _require(isinstance(value, (tuple, list)) and len(value) == 4, 'bbox 必须为四元组')
    _require(all(_is_int(item) and item >= 0 for item in value), 'bbox 必须为非负整数')
    x, y, width, height = value
    _require(width > 0 and height > 0, 'bbox 宽高必须为正')
    return (x, y, width, height)


@dataclass(frozen=True)
class Cost:
    """一次动作或一条 receipt 的显性成本。"""
    units: int = 0
    captures: int = 0
    ocr_calls: int = 0
    vlm_calls: int = 0

    def __post_init__(self) -> None:
        _require(all(_is_int(v) and v >= 0 for v in (self.units, self.captures, self.ocr_calls, self.vlm_calls)),
                 '成本字段必须为非负整数')

    def __add__(self, other: 'Cost') -> 'Cost':
        if not isinstance(other, Cost):
            return NotImplemented
        return Cost(self.units + other.units, self.captures + other.captures,
                    self.ocr_calls + other.ocr_calls, self.vlm_calls + other.vlm_calls)

    def fits(self, budget: 'Budget') -> bool:
        return (self.units <= budget.max_units and self.captures <= budget.max_captures
                and self.ocr_calls <= budget.max_ocr_calls and self.vlm_calls <= budget.max_vlm_calls)

    def as_dict(self) -> dict[str, int]:
        return {'units': self.units, 'captures': self.captures,
                'ocr_calls': self.ocr_calls, 'vlm_calls': self.vlm_calls}


ZERO_COST = Cost()


def action_cost(kind: str, method: str | None = None) -> Cost:
    """动作的预算成本。ocr/vlm 提取各自包含一次截图。"""
    _require(kind in ACTION_KINDS, '未知动作类型')
    if kind == 'extract':
        _require(method in EXTRACTIONS, '提取动作必须指定合法通道')
        if method == 'ocr':
            return Cost(units=OCR_CAPTURE_UNITS, captures=1, ocr_calls=1)
        if method == 'vlm':
            return Cost(units=VLM_CAPTURE_UNITS, captures=1, vlm_calls=1)
        if method == 'skill':
            return Cost(units=DEFAULT_UNIT_WEIGHTS['skill'])
        return Cost(units=DEFAULT_UNIT_WEIGHTS['dom'])
    if kind == 'stop':
        return ZERO_COST
    return Cost(units=DEFAULT_UNIT_WEIGHTS[kind])


@dataclass(frozen=True)
class Budget:
    """执行边界。资源用于约束，不混入业务质量评分。"""
    max_units: int = 24
    max_captures: int = 8
    max_ocr_calls: int = 8
    max_vlm_calls: int = 0
    max_steps: int = 30

    def __post_init__(self) -> None:
        for name in ('max_units', 'max_captures', 'max_ocr_calls', 'max_vlm_calls', 'max_steps'):
            value = getattr(self, name)
            _require(_is_int(value) and value >= 0, f'{name} 必须为非负整数')


def fixed_frame_offsets(scroll_height: int, viewport_height: int = DEFAULT_VIEWPORT_HEIGHT) -> tuple[int, ...]:
    """与 ``agent_v3/browser_worker.cjs`` 固定三屏一致的滚动偏移。"""
    _require(_is_int(scroll_height) and scroll_height >= 0, 'scroll_height 必须为非负整数')
    _require(_is_int(viewport_height) and viewport_height > 0, 'viewport_height 必须为正整数')
    offsets: list[int] = []
    for index in range(FIXED_FRAME_COUNT):
        y = min(index * FIXED_FRAME_STEP, max(0, scroll_height - viewport_height))
        if offsets and y == offsets[-1]:
            break
        offsets.append(y)
    return tuple(offsets)


@dataclass(frozen=True)
class Objective:
    """研究目标中的一条可核查问题。不是答案，也不是事实结论。"""
    objective_id: str
    question: str
    answer_kind: Literal['date', 'text', 'presence', 'count'] = 'text'
    region_hints: tuple[str, ...] = ()
    required: bool = True

    def __post_init__(self) -> None:
        _require(bool(self.objective_id), 'objective_id 不能为空')
        _require(bool(self.question), 'objective question 不能为空')
        _require(self.answer_kind in ('date', 'text', 'presence', 'count'), '未知答案类型')
        _require(isinstance(self.region_hints, tuple) and all(isinstance(r, str) for r in self.region_hints),
                 'region_hints 须为字符串元组')


@dataclass(frozen=True)
class ResearchGoal:
    """一次取证任务的目标；由主 Agent 提出，Harness 负责执行约束。"""
    task_id: str
    question: str
    objectives: tuple[Objective, ...]
    allowed_hosts: tuple[str, ...] = ()
    policy_note: str = ''

    def __post_init__(self) -> None:
        _require(bool(self.task_id), 'task_id 不能为空')
        _require(bool(self.question), 'goal question 不能为空')
        _require(bool(self.objectives), '至少需要一个 objective')
        ids = [objective.objective_id for objective in self.objectives]
        _require(len(ids) == len(set(ids)), 'objective_id 必须唯一')
        _require(isinstance(self.allowed_hosts, tuple), 'allowed_hosts 须为元组')

    @property
    def required_objectives(self) -> tuple[Objective, ...]:
        return tuple(objective for objective in self.objectives if objective.required)


@dataclass(frozen=True)
class Region:
    """页面上的一个语义区域，由浏览器观察或回放样本提供。

    ``visual_only=True`` 表示该区域只有截图里可读的内容（图片/画布/图形），
    具体文字要等 OCR/VLM 提取后才知道，不能用任何占位文本冒充页面文字。
    """
    region_id: str
    role: str
    bbox: tuple[int, int, int, int]
    text: str = ''
    visual_text: str = ''
    visual_only: bool = False
    lazy: bool = False
    requires: str | None = None
    blocked: bool = False
    scroll_y: int = 0

    def __post_init__(self) -> None:
        _require(bool(self.region_id), 'region_id 不能为空')
        _require(self.role in REGION_ROLES, f'未知区域角色: {self.role}')
        object.__setattr__(self, 'bbox', _bbox(self.bbox))
        _require(_is_int(self.scroll_y) and self.scroll_y >= 0, 'scroll_y 必须为非负整数')
        _require(isinstance(self.visual_only, bool), 'visual_only 必须为布尔值')
        for name in ('text', 'visual_text'):
            _require(isinstance(getattr(self, name), str), f'{name} 必须为字符串')

    @property
    def visible_text(self) -> str:
        return self.text.strip()

    def candidate_text(self) -> str:
        """当前可读文本；视觉文本只有在 OCR/VLM 提取后才算读入。"""
        return self.text.strip()


@dataclass(frozen=True)
class PageView:
    """一次页面观察的结果。``page_version`` 是证据版本，不是抓取时间。"""
    page_version: str
    url: str
    title: str
    viewport: tuple[int, int]
    scroll_height: int
    regions: tuple[Region, ...]
    dom_text: str = ''
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        _require(bool(self.page_version), 'page_version 不能为空')
        _require(isinstance(self.viewport, tuple) and len(self.viewport) == 2
                 and all(_is_int(v) and v > 0 for v in self.viewport), 'viewport 必须为两个正整数')
        _require(_is_int(self.scroll_height) and self.scroll_height >= 0, 'scroll_height 必须为非负整数')
        _require(isinstance(self.regions, tuple), 'regions 须为元组')
        if self.blocked_reason is not None:
            _require(self.blocked_reason in ('captcha', 'login_wall', 'access_denied'), '未知阻塞类型')

    @property
    def signature(self) -> str:
        return structural_signature(self.regions)

    def region(self, region_id: str) -> Region:
        for region in self.regions:
            if region.region_id == region_id:
                return region
        raise KeyError(region_id)

    def roles(self) -> tuple[str, ...]:
        return tuple(region.role for region in self.regions)


@dataclass(frozen=True)
class Observation:
    """驱动层返回的一次动作结果；harness 据此生成 receipt 与证据。"""
    action: str
    page_version: str
    region_id: str | None = None
    role: str | None = None
    extraction: str | None = None
    text: str = ''
    capture_id: str | None = None
    bbox: tuple[int, int, int, int] | None = None
    cost: Cost = ZERO_COST
    blocked: bool = False
    candidates: tuple[str, ...] = ()
    view: PageView | None = None
    note: str = ''

    def __post_init__(self) -> None:
        _require(self.action in ACTION_KINDS, '未知动作类型')
        _require(self.role is None or self.role in REGION_ROLES, '未知区域角色')
        _require(self.extraction is None or self.extraction in EXTRACTIONS, '未知提取通道')
        _require(isinstance(self.text, str), 'text 必须为字符串')
        if self.bbox is not None:
            object.__setattr__(self, 'bbox', _bbox(self.bbox))
        _require(isinstance(self.cost, Cost), 'cost 须为 Cost')

    @property
    def readable(self) -> bool:
        """是否真的读到了可引用的页面文字；空文本、阻塞页面均不算。"""
        return bool(self.text.strip()) and not self.blocked


@dataclass(frozen=True)
class Evidence:
    """一条可引用证据。

    ``verified`` 是事实核验状态，不是提取成功状态：OCR/视觉提取成功后它仍然
    是 False，只有独立 verifier 对页面版本、引文位置与支持关系核对后才能置真。
    """
    capture_id: str
    page_version: str
    region: str
    extraction: str
    text: str
    bbox: tuple[int, int, int, int] | None = None
    source_url: str = ''
    verified: bool = False
    verifier: str | None = None
    skill_id: str | None = None
    note: str = ''
    at: str = ''

    def __post_init__(self) -> None:
        _require(bool(self.capture_id), 'capture_id 不能为空')
        _require(bool(self.page_version), 'page_version 不能为空')
        _require(self.extraction in EXTRACTIONS, '未知提取通道')
        _require(isinstance(self.text, str), 'text 必须为字符串')
        if self.bbox is not None:
            object.__setattr__(self, 'bbox', _bbox(self.bbox))
        _require(not self.verified or bool(self.verifier), 'verified 证据必须记录 verifier')
        if self.at:
            _require(isinstance(self.at, str), 'at 必须为字符串')

    @property
    def fingerprint(self) -> str:
        return stable_id('evidence_', {'capture_id': self.capture_id, 'page_version': self.page_version,
                                       'region': self.region, 'extraction': self.extraction,
                                       'text': self.text})

    def as_dict(self) -> dict[str, Any]:
        return {'capture_id': self.capture_id, 'page_version': self.page_version,
                'region': self.region, 'extraction': self.extraction, 'text': self.text,
                'bbox': list(self.bbox) if self.bbox else None, 'source_url': self.source_url,
                'verified': self.verified, 'verifier': self.verifier, 'skill_id': self.skill_id,
                'note': self.note, 'at': self.at}


@dataclass(frozen=True)
class Action:
    """策略层唯一能够表达的动作。预算/权限由 harness 与 router 复核。"""
    kind: str
    region_id: str | None = None
    method: str | None = None
    reason: str = ''

    def __post_init__(self) -> None:
        _require(self.kind in ACTION_KINDS, '未知动作类型')
        if self.kind == 'extract':
            _require(bool(self.region_id), '提取动作必须指定区域')
            _require(self.method in EXTRACTIONS, '提取动作必须指定合法通道')
        elif self.kind == 'interact':
            _require(bool(self.region_id), '交互动作必须指定区域')
            _require(self.method is None, '交互动作不使用提取通道')
        elif self.kind == 'stop':
            _require(bool(self.reason), '停止动作必须说明原因')
        else:
            _require(self.region_id is None and self.method is None, '该动作不接受区域或通道')

    @property
    def cost(self) -> Cost:
        return action_cost(self.kind, self.method)

    def as_dict(self) -> dict[str, Any]:
        return {'kind': self.kind, 'region_id': self.region_id,
                'method': self.method, 'reason': self.reason}


@dataclass(frozen=True)
class Verification:
    """独立核验记录。核验失败不允许删改已有证据，只追加状态。"""
    verification_id: str
    capture_id: str
    status: str
    verifier: str
    detail: str = ''
    claim: str = ''
    at: str = ''

    def __post_init__(self) -> None:
        _require(bool(self.verification_id), 'verification_id 不能为空')
        _require(bool(self.capture_id), 'capture_id 不能为空')
        _require(self.status in VERIFICATION_STATUSES, '未知核验状态')
        _require(bool(self.verifier), '核验必须记录执行者')


@dataclass(frozen=True)
class Receipt:
    """统一的工具执行收据。

    示例（与 docs/V4_ARCHITECTURE.md 一致）::

        {
          "task_id": "research_001",
          "goal": "确认活动截止日期",
          "backend": "playwright",
          "action": "extract",
          "region": "activity_rules",
          "status": "completed",
          "evidence": [{"capture_id": "capture_001", "page_version": "sha256:...",
                        "bbox": [120, 320, 800, 400], "extraction": "ocr",
                        "verified": false}],
          "next": "verify_against_source"
        }
    """
    task_id: str
    goal: str
    backend: str
    action: str
    status: str
    page_version: str = ''
    region: str | None = None
    method: str | None = None
    evidence: tuple[Evidence, ...] = ()
    cost: Cost = ZERO_COST
    next: str = 'continue'
    error: str | None = None
    note: str = ''
    at: str = ''
    receipt_id: str = ''
    idempotency_key: str = ''

    def __post_init__(self) -> None:
        _require(bool(self.task_id), 'task_id 不能为空')
        _require(self.backend in BACKENDS, '未知后端')
        _require(self.action in ACTION_KINDS, '未知动作类型')
        _require(self.status in RECEIPT_STATUSES, '未知收据状态')
        _require(self.next in NEXT_STEPS, '未知下一步')
        _require(self.method is None or self.method in EXTRACTIONS, '未知提取通道')
        _require(isinstance(self.evidence, tuple) and all(isinstance(item, Evidence) for item in self.evidence),
                 'evidence 须为 Evidence 元组')
        _require(self.region is None or isinstance(self.region, str), 'region 须为字符串')
        if self.receipt_id:
            _require(bool(self.idempotency_key), '自定义 receipt_id 必须同时给出幂等键')
        if self.at:
            _require(isinstance(self.at, str), 'at 必须为字符串')

    def key(self) -> str:
        """同一任务、同一页面版本、同一动作（含区域/通道）得到同一幂等键。

        ``method`` 必须参与哈希：同一区域用 ``dom`` 与 ``ocr`` 是两次不同的取证，
        不能让后一次的幂等键与前者相同。
        """
        return stable_id('receipt_', {'task_id': self.task_id, 'page_version': self.page_version,
                                      'action': self.action, 'region': self.region,
                                      'method': self.method})

    def filled(self) -> 'Receipt':
        """补齐派生的 receipt_id / idempotency_key / at，字段本身不可变。"""
        key = self.idempotency_key or self.key()
        return replace(self, idempotency_key=key, receipt_id=self.receipt_id or key,
                       at=self.at or now_iso())

    def as_dict(self) -> dict[str, Any]:
        return {'schema': SCHEMA_VERSION, 'receipt_id': self.receipt_id, 'idempotency_key': self.idempotency_key,
                'task_id': self.task_id, 'goal': self.goal, 'backend': self.backend,
                'action': self.action, 'region': self.region, 'method': self.method,
                'status': self.status, 'page_version': self.page_version,
                'evidence': [item.as_dict() for item in self.evidence],
                'cost': self.cost.as_dict(), 'next': self.next, 'error': self.error,
                'note': self.note, 'at': self.at}


@dataclass(frozen=True)
class RegionHint:
    """Skill/Router 提供的区域先验；只影响观察顺序，不产生事实结论。"""
    region_id: str
    score: float
    source: str
    note: str = ''

    def __post_init__(self) -> None:
        _require(bool(self.region_id), 'hint 必须指定区域')
        _require(isinstance(self.score, (int, float)) and 0 <= float(self.score) <= 1, 'hint 分值须在 0..1')
        _require(bool(self.source), 'hint 必须说明来源')


class PageDriver(Protocol):
    """浏览器/回放后端的统一驱动接口。真实 Playwright 与冻结回放共用这一层。"""
    backend: str

    def open(self, goal: ResearchGoal) -> PageView:
        ...

    def inspect(self, view: PageView) -> Observation:
        ...

    def locate(self, view: PageView, goal: ResearchGoal) -> Observation:
        ...

    def extract(self, view: PageView, region: Region, method: str) -> Observation:
        ...

    def interact(self, view: PageView, region: Region) -> Observation:
        ...

    def close(self) -> None:
        ...


class ObservationPolicy(Protocol):
    """观察策略。只输出 Action，不读写数据库、不宣告事实。"""
    name: str

    def next_action(self, goal: ResearchGoal, view: PageView, state: 'PolicyState') -> Action:
        ...


class HintProvider(Protocol):
    """可选的区域先验来源（例如通过完成验证的 Skill）。"""
    name: str

    def hints(self, view: PageView, goal: ResearchGoal) -> tuple[RegionHint, ...]:
        ...


class EvidenceVerifier(Protocol):
    """独立证据核验器。与提取动作分离，不允许提取器给自己发合格证。"""
    verifier_id: str

    def verify(self, evidence: Evidence, view: PageView | None) -> Verification:
        ...


def candidate_text(objective: Objective, text: str) -> bool:
    """判断一段文本是否可能回答某个 objective。

    这是召回判断，不是事实核验：命中只说明「值得保留并交给独立核验」。
    """
    stripped = text.strip()
    if not stripped:
        return False
    if objective.answer_kind == 'date':
        return bool(DATE_RE.search(stripped))
    if objective.answer_kind == 'count':
        return bool(re.search(r'\d+', stripped))
    if objective.answer_kind == 'presence':
        return len(stripped) >= 4
    return len(stripped) >= 6


def candidate_region(objective: Objective, region: Region) -> bool:
    """区域角色是否与 objective 的先验相符；无先验时不做限制。"""
    return not objective.region_hints or region.role in objective.region_hints


@dataclass
class PolicyState:
    """策略决策所需的可变状态；由 harness 独占维护，策略只读。"""
    goal: ResearchGoal
    budgets: Budget
    page_version: str
    spent: Cost = ZERO_COST
    steps: int = 0
    candidates: dict[str, list[str]] = field(default_factory=dict)
    attempted: dict[str, int] = field(default_factory=dict)
    interacted: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    stop_reason: str | None = None

    def can_afford(self, cost: Cost) -> bool:
        total = self.spent + cost
        return total.fits(self.budgets)

    def complete(self) -> bool:
        """是否每个 required objective 都至少有一条候选证据（不代表事实已核验）。"""
        return all(self.candidates.get(objective.objective_id) for objective in self.goal.required_objectives)

    def coverage(self) -> tuple[int, int]:
        required = self.goal.required_objectives
        met = sum(1 for objective in required if self.candidates.get(objective.objective_id))
        return met, len(required)

    def remaining_units(self) -> int:
        return max(0, self.budgets.max_units - self.spent.units)

    def attempt_key(self, action: Action) -> str:
        return f'{action.kind}:{action.region_id or "-"}:{action.method or "-"}'

    def attempts(self, action: Action) -> int:
        return self.attempted.get(self.attempt_key(action), 0)

    def record(self, action: Action, observation: Observation) -> None:
        """消费一次动作结果：累计成本、步数，并把候选证据挂到 objective 上。"""
        self.spent = self.spent + action.cost
        self.steps += 1
        key = self.attempt_key(action)
        self.attempted[key] = self.attempted.get(key, 0) + 1
        self.page_version = observation.page_version
        if action.kind == 'interact' and action.region_id:
            self.interacted.add(action.region_id)
        if not observation.readable or not observation.extraction:
            return
        role = observation.role or 'unknown'
        for objective in self.goal.objectives:
            if objective.region_hints and role not in objective.region_hints:
                continue
            if not candidate_text(objective, observation.text):
                continue
            capture_id = observation.capture_id or stable_id('capture_', {
                'task_id': self.goal.task_id, 'page_version': observation.page_version,
                'region': observation.region_id, 'text': observation.text})
            bucket = self.candidates.setdefault(objective.objective_id, [])
            if capture_id not in bucket:
                bucket.append(capture_id)


@dataclass(frozen=True)
class TaskTrace:
    """一次研究取证任务的完整轨迹，用于验收、排障和离线对照。"""
    task_id: str
    question: str
    backend: str
    policy: str
    status: str
    stop_reason: str
    page_versions: tuple[str, ...]
    receipts: tuple[Receipt, ...]
    evidence: tuple[Evidence, ...]
    cost: Cost
    steps: int
    gap: str | None = None
    verifications: tuple[Verification, ...] = ()
    started_at: str = ''
    finished_at: str = ''

    def __post_init__(self) -> None:
        _require(isinstance(self.verifications, tuple)
                 and all(isinstance(item, Verification) for item in self.verifications),
                 'verifications 须为 Verification 元组')

    def coverage(self, goal: ResearchGoal) -> tuple[int, int]:
        met = sum(1 for objective in goal.required_objectives
                  if any(candidate_text(objective, item.text) for item in self.evidence))
        return met, len(goal.required_objectives)

    def as_dict(self) -> dict[str, Any]:
        return {'schema': SCHEMA_VERSION, 'task_id': self.task_id, 'question': self.question,
                'backend': self.backend, 'policy': self.policy, 'status': self.status,
                'stop_reason': self.stop_reason, 'page_versions': list(self.page_versions),
                'receipts': [item.as_dict() for item in self.receipts],
                'evidence': [item.as_dict() for item in self.evidence],
                'cost': self.cost.as_dict(), 'steps': self.steps, 'gap': self.gap,
                'verifications': [{'verification_id': item.verification_id,
                                   'capture_id': item.capture_id, 'status': item.status,
                                   'verifier': item.verifier, 'detail': item.detail,
                                   'claim': item.claim, 'at': item.at}
                                  for item in self.verifications],
                'started_at': self.started_at, 'finished_at': self.finished_at}
