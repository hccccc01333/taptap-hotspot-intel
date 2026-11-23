"""V4 Acquisition Harness 的工具路由层（接口冻结见接口冻结文档 §2.6）。

Router 把「证据缺口」翻译成「下一个允许使用的后端与通道」，并显式记录被打回的
后端。它不执行动作、不产生证据、不判断事实：真正的执行、成本记账、阻塞页证据
拦截仍由 harness 负责。

后端优先级（冻结，数字越小越优先）::

    dom(1) < skill(2, 仅注册表授予) < ocr(3) < vlm(5, 默认禁用) < tabbit(默认禁用)

权限模型：

- ``allowed_backends`` 控制 dom / playwright / ocr / skill / http 等普通通道；
- ``vlm_enabled`` 与 ``tabbit_enabled`` 是视觉模型与外部 tabbit 通道的显式开关，
  默认关闭；即使写进 ``allowed_backends``，未显式打开也会被拒绝；
- ``authorized_login`` 控制登录墙：未授权时 ``login_required`` 缺口一律停止，
  不得把登录后内容伪装成公开证据；
- 阻塞页（``blocked_reason`` 非空或缺口为 ``blocked_page``）在**路由规划**阶段
  直接停止，任何后端都不允许作为下一步；动作执行后发现的阻塞由 harness 按
  执行循环约定拦截证据并停止（``validate_action`` 因此不重复拒绝，保证固定基线
  仍能如实记录阻塞页上的浪费成本）。

``validate_action`` 供 harness 复核策略动作：返回 ``(False, 原因)`` 时 harness 记
``deferred`` receipt，不得执行；这样策略层即使误提 vlm/tabbit 或未知区域，也不会
绕过能力约束。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from . import model

# 证据缺口原因（冻结枚举，新增原因必须同时改接口冻结文档与 harness）。
GAP_REASONS = (
    'dom_text_missing',
    'visual_only_region',
    'lazy_region',
    'login_required',
    'blocked_page',
    'skill_available',
)

# 冻结的后端优先级；http(4) 未出现在冻结序列里，仅作为显式授权的抓取通道兜底。
BACKEND_PRIORITY = {'dom': 1, 'skill': 2, 'ocr': 3, 'http': 4, 'vlm': 5, 'tabbit': 9}

# 可提取文字的后端（用于「被拒后端」清单，按优先级排序）。
EXTRACTION_BACKENDS = ('dom', 'skill', 'ocr', 'vlm', 'tabbit')

LOGIN_MARKER = 'login'


def _ordered(backends: Iterable[str]) -> tuple[str, ...]:
    """去重并按冻结优先级排序，保证 rejected 稳定可断言。"""
    unique: list[str] = []
    for backend in backends:
        if backend not in unique:
            unique.append(backend)
    return tuple(sorted(unique, key=lambda item: (BACKEND_PRIORITY.get(item, 99), item)))


@dataclass(frozen=True)
class EvidenceGap:
    """一条证据缺口：哪个 objective 缺什么形态的证据，以及可选的目标区域。"""

    objective_id: str
    reason: str
    region_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.objective_id, str) or not self.objective_id.strip():
            raise ValueError('EvidenceGap.objective_id 不能为空')
        if self.reason not in GAP_REASONS:
            raise ValueError(f'未知证据缺口原因: {self.reason}（允许：{", ".join(GAP_REASONS)}）')
        if self.region_id is not None and (not isinstance(self.region_id, str) or not self.region_id.strip()):
            raise ValueError('EvidenceGap.region_id 须为非空字符串或 None')


@dataclass(frozen=True)
class RouteDecision:
    """路由决定：选哪个后端/通道，是否允许执行，理由与被拒后端。

    ``backend is None`` 且 ``allowed is False`` 表示停止（例如阻塞页、未授权登录、
    视觉通道全部不可用）；``allowed is True`` 时 ``backend`` 必须存在，``method``
    对交互类动作为 ``None``。
    """

    backend: str | None
    method: str | None
    allowed: bool
    reason: str
    rejected: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.backend is not None and self.backend not in model.BACKENDS:
            raise ValueError(f'未知后端: {self.backend}')
        if self.method is not None and self.method not in model.EXTRACTIONS:
            raise ValueError(f'未知提取通道: {self.method}')
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError('路由决定必须写明理由')
        if self.allowed and self.backend is None:
            raise ValueError('允许执行的路由必须指定后端')
        if not self.allowed and self.backend is not None:
            raise ValueError('拒绝/停止的路由不得指定后端（停止时 backend 为 None）')
        object.__setattr__(self, 'rejected', _ordered(self.rejected))

    @property
    def stopped(self) -> bool:
        return not self.allowed


class AcquisitionRouter:
    """按证据缺口与能力约束选择后端，并解释选择与被拒理由。"""

    def __init__(
        self,
        *,
        allowed_backends: tuple[str, ...] = ('dom', 'playwright', 'ocr'),
        vlm_enabled: bool = False,
        tabbit_enabled: bool = False,
        authorized_login: bool = False,
    ) -> None:
        backends = tuple(dict.fromkeys(allowed_backends))
        for backend in backends:
            if backend not in model.BACKENDS:
                raise ValueError(f'未知后端: {backend}（允许：{", ".join(model.BACKENDS)}）')
        self.allowed_backends = backends
        self.vlm_enabled = bool(vlm_enabled)
        self.tabbit_enabled = bool(tabbit_enabled)
        self.authorized_login = bool(authorized_login)

    # ------------------------------------------------------------------ 权限
    def allows_backend(self, backend: str) -> bool:
        """后端是否被授权：vlm/tabbit 走显式开关，其余走 allowed_backends。"""
        if backend == 'vlm':
            return self.vlm_enabled
        if backend == 'tabbit':
            return self.tabbit_enabled
        return backend in self.allowed_backends

    def allows_extraction(self, method: str) -> bool:
        """某个提取通道是否可用；http 通道可由 tabbit 开关兜底。"""
        if method not in model.EXTRACTIONS:
            return False
        if method == 'http':
            return 'http' in self.allowed_backends or self.tabbit_enabled
        return self.allows_backend(method)

    # ------------------------------------------------------------------ 路由
    def route(self, gap: EvidenceGap, view: model.PageView, budgets: model.Budget | None = None) -> RouteDecision:
        """把证据缺口翻译成路由决定；无法安全取证时返回停止决定。"""
        if not isinstance(gap, EvidenceGap):
            raise TypeError('gap 必须是 EvidenceGap')
        if not isinstance(view, model.PageView):
            raise TypeError('view 必须是 PageView')

        region, gap_region_missing = self._target_region(gap, view)
        note = '（缺口区域不在当前视图，页面结构可能已变化，按缺口形态回退）' if gap_region_missing else ''

        # 1) 阻塞页：任何后端都不得提取，立即停止。
        if view.blocked_reason:
            return self._refuse(
                f'页面阻塞（{view.blocked_reason}）：停止取证，阻塞页文字与截图不得进入证据',
                EXTRACTION_BACKENDS,
            )
        if gap.reason == 'blocked_page':
            return self._refuse(
                '证据缺口为 blocked_page：阻塞页不得提取任何证据，直接停止',
                EXTRACTION_BACKENDS,
            )

        # 2) 登录墙：未授权一律停止；已授权走带登录态的 playwright 通道。
        if gap.reason == 'login_required':
            if not self.authorized_login:
                return self._refuse(
                    '登录墙缺口：未授权登录，停止取证，不得伪造登录后才能看到的内容',
                    EXTRACTION_BACKENDS,
                )
            if self.allows_backend('playwright'):
                return self._allow(
                    'playwright', 'dom',
                    '登录墙缺口且已授权登录：用带登录态的 playwright 读取登录后 DOM（证据仍标记未核验）',
                    (),
                )
            return self._refuse('已授权登录，但缺少 playwright 交互后端，无法维持登录态取证', ('playwright',))
        if region is not None and self._requires_login(region) and not self.authorized_login:
            return self._refuse(
                f'目标区域 {region.region_id} 需要登录（{region.requires}），未授权时不得交互或提取',
                EXTRACTION_BACKENDS,
            )

        # 3) 按缺口形态选择通道。
        if gap.reason == 'dom_text_missing':
            if region is not None and not region.text.strip() and region.visual_text.strip():
                return self._choose_visual(
                    f'目标区域 {region.region_id} 没有可读 DOM 文本，只有视觉文字{note}',
                    budgets,
                    rejected=('dom',),
                )
            return self._choose_dom(f'缺口为 DOM 文本缺失{note}', budgets)

        if gap.reason == 'visual_only_region':
            return self._choose_visual(f'缺口为仅视觉区域：DOM 读不到，必须 OCR/视觉通道{note}', budgets)

        if gap.reason == 'lazy_region':
            if region is not None and region.text.strip():
                return self._choose_dom(f'懒加载区域 {region.region_id} 已展开且 DOM 文本可读{note}', budgets)
            if self.allows_backend('playwright'):
                cost = model.action_cost('interact')
                if budgets is not None and not cost.fits(budgets):
                    return self._refuse(
                        f'懒加载区域需要一次交互（{cost.units} units），超出预算上限', ('playwright',)
                    )
                return self._allow(
                    'playwright', None,
                    f'懒加载区域需先交互展开：由 playwright 执行一次 interact，再按展开后的内容选择 DOM/OCR{note}',
                    (),
                )
            return self._refuse(
                f'懒加载区域尚未展开且缺少 playwright 交互后端，OCR 读不到未渲染内容{note}',
                ('playwright',),
            )

        # gap.reason == 'skill_available'
        if self.allows_backend('skill'):
            cost = model.action_cost('extract', 'skill')
            if budgets is not None and not cost.fits(budgets):
                return self._refuse(f'Skill 通道需要 {cost.units} units，超出预算上限', ('skill',))
            return self._allow(
                'skill', 'skill',
                f'缺口为 skill_available 且注册表已授予该 Skill：优先用 Skill 定位区域（优先级 2）{note}',
                (),
            )
        why = f'缺口为 skill_available，但 Skill 未由注册表授予（allowed_backends 不含 skill），改用普通通道{note}'
        if region is not None and not region.text.strip() and region.visual_text.strip():
            return self._choose_visual(why, budgets, rejected=('skill',))
        return self._choose_dom(why, budgets, rejected=('skill',))

    # -------------------------------------------------------------- 通道选择
    def _choose_dom(self, why: str, budgets: model.Budget | None, *, rejected: Iterable[str] = ()) -> RouteDecision:
        rejected_list = list(rejected)
        if not self.allows_backend('dom'):
            rejected_list.append('dom')
            return self._choose_visual(
                f'{why}；dom 后端未授权，回退视觉通道转录已渲染文字', budgets, rejected=rejected_list
            )
        cost = model.action_cost('extract', 'dom')
        if budgets is not None and not cost.fits(budgets):
            rejected_list.append('dom')
            return self._refuse(
                f'{why}；预算不足：dom 提取需要 {cost.units} units，超出预算上限', rejected_list
            )
        return self._allow(
            'dom', 'dom',
            f'{why}；选择 dom（优先级 1，成本最低 {cost.units} units，只读已渲染 DOM 文本）',
            rejected_list,
        )

    def _choose_visual(self, why: str, budgets: model.Budget | None, *, rejected: Iterable[str] = ()) -> RouteDecision:
        rejected_list = list(rejected)
        chain = ('ocr', 'vlm')
        for position, backend in enumerate(chain):
            if not self.allows_backend(backend):
                rejected_list.append(backend)
                continue
            method = backend
            cost = model.action_cost('extract', method)
            if budgets is not None and not cost.fits(budgets):
                rejected_list.append(backend)
                return self._refuse(
                    f'{why}；预算不足：{backend} 提取需要 {cost.units} units / {cost.captures} 次捕获，超出预算上限',
                    rejected_list,
                )
            # 选中通道之后，优先级更低但能力被关闭的通道同样属于「被打回」。
            for lower in chain[position + 1:]:
                if not self.allows_backend(lower):
                    rejected_list.append(lower)
            if not self.allows_backend('tabbit'):
                rejected_list.append('tabbit')
            return self._allow(
                backend, method,
                f'{why}；选择 {backend}（优先级 {BACKEND_PRIORITY[backend]}，成本 {cost.units} units，'
                '视觉转录结果仍是未核验证据）',
                rejected_list,
            )
        if self.allows_backend('tabbit'):
            cost = model.action_cost('extract', 'http')
            if budgets is not None and not cost.fits(budgets):
                rejected_list.append('tabbit')
                return self._refuse(f'{why}；预算不足：tabbit 外部通道需要 {cost.units} units，超出预算上限', rejected_list)
            return self._allow(
                'tabbit', 'http',
                f'{why}；ocr/vlm 均不可用，退回 tabbit 外部抓取通道（本切片只声明路由，抓取实现属下一阶段）',
                rejected_list,
            )
        rejected_list.append('tabbit')
        return self._refuse(
            f'{why}；ocr/vlm 未启用或未授权且 tabbit 默认禁用，无法取得视觉证据（不得伪造）',
            rejected_list,
        )

    # ------------------------------------------------------------ 动作复核
    def validate_action(
        self, action: model.Action, view: model.PageView, budgets: model.Budget | None = None
    ) -> tuple[bool, str]:
        """返回 ``(是否允许, 原因)``；``False`` 时 harness 记 deferred，不得执行。"""
        if not isinstance(action, model.Action):
            raise TypeError('action 必须是 model.Action')
        if not isinstance(view, model.PageView):
            raise TypeError('view 必须是 PageView')

        if action.kind == 'stop':
            return True, '停止动作不需要后端权限，允许执行'
        # 阻塞页在路由规划阶段一律停止（见 route）；但动作执行后才知道 observation
        # 是否 blocked，证据拦截与停止由 harness 按 §2.3 统一处理。这里不做第二道
        # 阻塞拒绝：否则固定基线连「执行后被记为 blocked 并停下」的机会都没有，
        # 也无法如实记录阻塞页上的浪费成本。
        cost = action.cost
        if budgets is not None and not cost.fits(budgets):
            return False, (
                f'预算不足：动作 {action.kind} 需要 {cost.units} units / {cost.captures} 次捕获，'
                f'超出预算上限（units={budgets.max_units}, captures={budgets.max_captures}）'
            )
        if action.kind in ('inspect', 'locate'):
            return True, f'允许 {action.kind}：只更新候选与成本，不产生文字证据'
        if action.kind == 'interact':
            return self._validate_interact(action, view)
        return self._validate_extract(action, view)

    def _validate_interact(self, action: model.Action, view: model.PageView) -> tuple[bool, str]:
        if not self.allows_backend('playwright'):
            return False, '交互动作需要 playwright 交互后端，但当前未授权（allowed_backends 不含 playwright）'
        region_kind, region, error = self._resolve_region(view, action.region_id)
        if error:
            return False, error
        if region_kind == 'virtual':
            return False, f'虚拟区域 {action.region_id} 不支持交互，只能作为提取目标'
        if self._requires_login(region) and not self.authorized_login:
            return False, f'区域 {region.region_id} 需要登录（{region.requires}），未授权时不得交互'
        return True, f'允许交互区域 {region.region_id}：用于展开懒加载区域，交互本身不产生文字证据'

    def _validate_extract(self, action: model.Action, view: model.PageView) -> tuple[bool, str]:
        method = action.method
        if method not in model.EXTRACTIONS:
            return False, f'未知提取通道: {method}'
        if method == 'vlm' and not self.vlm_enabled:
            return False, 'vlm 后端默认禁用：未设置 vlm_enabled=True 时不得请求视觉模型'
        if not self.allows_extraction(method):
            return False, (
                f'{method} 后端未授权：allowed_backends={self.allowed_backends}'
                '（skill 需注册表授予，vlm/tabbit 需显式启用）'
            )
        region_kind, region, error = self._resolve_region(view, action.region_id)
        if error:
            return False, error
        if region_kind == 'semantic':
            if region.blocked:
                return False, f'区域 {region.region_id} 已标记阻塞，不得提取为证据'
            if self._requires_login(region) and not self.authorized_login:
                return False, f'区域 {region.region_id} 需要登录（{region.requires}），未授权时不得提取'
            if region.lazy and not region.text.strip() and not region.visual_text.strip():
                return False, f'区域 {region.region_id} 是未展开的懒加载区域，应先执行 interact'
        cost = action.cost
        return True, (
            f'允许 {method} 提取 {action.region_id}（成本 {cost.units} units / {cost.captures} 次捕获，'
            '证据仍为未核验状态）'
        )

    # ---------------------------------------------------------------- 内部
    def _target_region(self, gap: EvidenceGap, view: model.PageView) -> tuple[model.Region | None, bool]:
        """定位缺口区域；虚拟区域与真正缺失的区域要区分开。"""
        if not gap.region_id:
            return None, False
        if gap.region_id == model.DOM_BODY_REGION or gap.region_id.startswith(model.VIEWPORT_REGION_PREFIX):
            return None, False
        try:
            return view.region(gap.region_id), False
        except KeyError:
            return None, True

    def _resolve_region(self, view: model.PageView, region_id: str | None) -> tuple[str, model.Region | None, str | None]:
        """把区域 id 解析成（语义区域 / 虚拟区域 / 错误）。"""
        if not region_id:
            return 'unknown', None, '提取或交互动作必须指定区域'
        if region_id == model.DOM_BODY_REGION:
            return 'virtual', None, None
        if region_id.startswith(model.VIEWPORT_REGION_PREFIX):
            suffix = region_id[len(model.VIEWPORT_REGION_PREFIX):]
            try:
                index = int(suffix)
            except ValueError:
                return 'unknown', None, f'非法虚拟区域 id: {region_id}'
            frames = model.fixed_frame_offsets(view.scroll_height, view.viewport[1])
            if not 0 <= index < len(frames):
                return 'unknown', None, f'虚拟屏幕 {region_id} 超出固定帧范围（当前页面只有 {len(frames)} 屏）'
            return 'virtual', None, None
        try:
            return 'semantic', view.region(region_id), None
        except KeyError:
            return 'unknown', None, f'未知区域: {region_id}（当前页面视图中不存在）'

    @staticmethod
    def _requires_login(region: model.Region | None) -> bool:
        return bool(region is not None and LOGIN_MARKER in (region.requires or '').lower())

    @staticmethod
    def _allow(backend: str, method: str | None, reason: str, rejected: Iterable[str]) -> RouteDecision:
        return RouteDecision(backend=backend, method=method, allowed=True, reason=reason, rejected=_ordered(rejected))

    @staticmethod
    def _refuse(reason: str, rejected: Iterable[str]) -> RouteDecision:
        return RouteDecision(backend=None, method=None, allowed=False, reason=reason, rejected=_ordered(rejected))


__all__ = ['BACKEND_PRIORITY', 'EXTRACTION_BACKENDS', 'GAP_REASONS', 'AcquisitionRouter',
           'EvidenceGap', 'RouteDecision']
