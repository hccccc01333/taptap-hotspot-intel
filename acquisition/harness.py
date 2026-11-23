"""唯一执行循环：预算、路由、幂等、阻塞页拦截、恢复与轨迹。

执行顺序（与接口冻结文档一致）：

1. 建 session/task 并获取租约（保存 fence token）；``resume=True`` 时读取已存状态，
   重新 open 页面，当前视图 ``page_version`` 与已存 ``latest_page_version`` 不一致
   即判 stale 停止（旧版本证据保留为历史，不冒充当前事实）。``driver.open`` 失败时
   落 failed inspect 收据与 ``open_failed`` 终态，任务不会停留在 pending。
2. 首次 ``inspect`` → receipt → 状态更新（恢复时幂等命中不复计步数预算）。
3. 循环 ``policy.next_action(goal, view, state)``：

   - 动作成本超预算 → ``budget_exhausted`` 停止；
   - 租约失效 → 本次不得再写入；若新 owner 已写终态则原样保留，本次返回其轨迹
     （任务状态可能仍为 pending/interrupted，由恢复流程处理）；写前续租并复核 owner + token；
   - 路由拒绝 → 记 ``deferred`` receipt，不执行驱动，继续由策略改选；
   - 幂等命中（同 task/page_version/action 的 completed receipt）→ 复用，不调驱动；
     非 completed 同键重试使用含尝试序号的幂等键，新收据不会被静默丢弃；
   - 执行驱动动作并生成 receipt；evidence 一律 ``verified=False``；
   - 页面阻塞时任何证据不得入库，receipt 状态 ``blocked``，立即停止；
   - 交互返回新 PageView 时更新当前版本与视图。

4. ``stop`` 动作或循环终止 → 写任务终态（带租约 fence）→ 由 store 组装
   :class:`TaskTrace`；``succeeded`` 还要求 required objective 有候选证据，
   否则记 ``failed`` + ``gap='objectives_uncovered'``。

设计约束：失败动作的成本照记（不因失败抹掉单位/捕获/OCR 计数）；策略无法直接
宣告事实成立，也无法绕过阻塞页面把证据写进存储。
"""
from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from . import model
from .model import (
    DOM_BODY_REGION,
    VIEWPORT_REGION_PREFIX,
    ZERO_COST,
    Action,
    Budget,
    Evidence,
    Observation,
    PageView,
    PolicyState,
    Receipt,
    ResearchGoal,
    Region,
    TaskTrace,
    candidate_text,
    fixed_frame_offsets,
    now_iso,
    stable_id,
)
from .receipts import build_receipt, receipt_key, retry_key
from .store import AcquisitionStore, LeaseLost

_INSPECT_COST = model.action_cost('inspect')
_BLOCKED_PREFIX = 'blocked:'
# 停止原因 → 任务终态：未完成且需要重跑/人工处理的任务不能写成 succeeded。
_FAILED_STOP_REASONS = ('max_steps', 'budget_exhausted', 'policy_error', 'harness_error',
                        'open_failed')
_BLOCKED_STOP_REASONS = ('page_version_changed', 'login_required', 'interrupted')
# 只有这些「正常完成」停止原因才可能对应 succeeded（仍要求 required 目标有候选证据）。
_COMPLETION_STOP_REASONS = ('objectives_covered', 'no_new_evidence',
                            'fixed_frames_exhausted', 'legacy_v3_exhausted')
# gap 是「未解决的缺口」：正常完成类停止原因不写缺口。
_GAP_STOP_REASONS = ('login_required', 'no_new_evidence', 'page_version_changed',
                     'budget_exhausted', 'max_steps', 'interrupted', 'policy_error',
                     'open_failed')
UNCOVERED_GAP = 'objectives_uncovered'


class AcquisitionHarness:
    """浏览器/回放取证任务的执行核心。

    :param driver: :class:`~acquisition.model.PageDriver` 实现（切片默认为
        :class:`acquisition.driver.FixtureDriver`）。
    :param policy: 观察策略，只需实现 ``next_action(goal, view, state) -> Action``。
    :param store: :class:`~acquisition.store.AcquisitionStore`。
    :param backend: receipt 里记录的后端；缺省取 ``driver.backend``。
    :param budgets: 执行边界，缺省 :class:`~acquisition.model.Budget`。
    :param router: 可选路由；实现 ``validate_action(action, view, budgets)`` 时，
        被拒绝的动作只记 ``deferred`` receipt，绝不执行驱动。
    """

    def __init__(self, driver, policy, store, *, backend: str | None = None,
                 budgets: Budget | None = None, router: Any = None) -> None:
        if store is None or not hasattr(store, 'append_receipt'):
            raise TypeError('store 必须是 AcquisitionStore')
        if not hasattr(policy, 'next_action'):
            raise TypeError('policy 必须实现 next_action(goal, view, state)')
        self.driver = driver
        self.policy = policy
        self.store = store
        self.backend = backend or getattr(driver, 'backend', 'playwright')
        if self.backend not in model.BACKENDS:
            raise ValueError(f'未知后端: {self.backend!r}')
        if budgets is None:
            budgets = Budget()
        if not isinstance(budgets, Budget):
            raise TypeError('budgets 必须是 Budget')
        self.budgets = budgets
        self.router = router
        self._session_id: str | None = None
        self._task_id: str | None = None
        self._owner: str | None = None
        self._lease_token: str | None = None

    # ------------------------------------------------------------------ 公共
    def run(self, goal: ResearchGoal, *, session_id: str | None = None,
            resume: bool = False) -> TaskTrace:
        """执行一次取证任务并返回可完整复盘的 :class:`TaskTrace`。

        ``resume=True`` 时按 ``goal.task_id``（可配合 ``session_id``）寻找最近任务：
        找到则继续未完成的工作，找不到则按新任务执行（保持幂等友好）。
        """
        if not isinstance(goal, ResearchGoal):
            raise TypeError('goal 必须是 ResearchGoal')
        owner = f'harness-{uuid.uuid4().hex[:12]}'
        task = self.store.find_task(goal.task_id, session_id) if resume else None
        snapshot = None
        if task is None:
            active_session = session_id or self.store.create_session(self.backend)
            task_id = self.store.create_task(active_session, goal)
        else:
            active_session = task['session_id']
            task_id = task['task_id']
            snapshot = self.store.resume(task_id)
        self._session_id = active_session
        self._task_id = task_id
        self._owner = owner
        self._lease_token = self.store.acquire_lease(active_session, owner)
        try:
            return self._run_locked(goal, snapshot)
        except LeaseLost:
            # 租约在准备阶段被新 owner 接管：不写终态、不覆盖新结果，按存储现状返回轨迹。
            return self.store.trace(task_id, goal, policy=self._policy_name(),
                                    backend=self.backend)
        finally:
            lease_token = self._lease_token
            self._session_id = None
            self._task_id = None
            self._owner = None
            self._lease_token = None
            try:
                self.store.release_lease(active_session, owner, lease_token)
            except LeaseLost:
                # 租约被其他执行者接管时不能再释放它；交给 owner 栅栏保证旧结果不覆盖。
                pass

    # ------------------------------------------------------------------ 内部
    def _policy_name(self) -> str:
        return str(getattr(self.policy, 'name', None) or type(self.policy).__name__)

    def _receipt_goal(self, goal: ResearchGoal) -> ResearchGoal:
        """收据的 task_id 使用持久化任务 id（store 主键），其余字段保持业务 goal。"""
        assert self._task_id is not None
        return replace(goal, task_id=self._task_id)

    def _assert_lease(self) -> None:
        assert self._session_id is not None and self._owner is not None
        self.store.assert_owner(self._session_id, self._owner, self._lease_token)

    def _renew_lease(self) -> None:
        """租约心跳：写收据/证据/终态前刷新 TTL，并复核 same-owner + token。"""
        assert self._session_id is not None and self._owner is not None
        self.store.renew_lease(self._session_id, self._owner, self._lease_token)

    def _append(self, receipt: Receipt) -> Receipt:
        """写收据前续租并复核租约：旧执行者不能覆盖新执行者的结果。"""
        self._renew_lease()
        return self.store.append_receipt(receipt)

    @staticmethod
    def _audit_key(base: str, tag: str) -> str:
        """审计型终端收据的唯一幂等键（stale / open_failed 每次都要独立留痕）。"""
        return f'{base}:{tag}:{uuid.uuid4().hex[:12]}'

    def _run_locked(self, goal: ResearchGoal, snapshot: dict | None) -> TaskTrace:
        try:
            view = self.driver.open(goal)
            if not isinstance(view, PageView):
                raise TypeError(f'驱动 open 必须返回 PageView，得到 {type(view).__name__}')
        except LeaseLost:
            raise
        except Exception as exc:  # 导航/加载失败是真实常见路径：必须落失败终态
            return self._finish_open_failed(goal, exc)

        if snapshot is not None:
            latest = snapshot.get('latest_page_version') or ''
            if latest and view.page_version != latest:
                return self._finish_stale(goal, latest, view)
            state = self._rebuild_state(goal, view, snapshot['receipts'])
        else:
            state = PolicyState(goal=goal, budgets=self.budgets,
                                page_version=view.page_version)

        stop_reason: str | None = None
        error: str | None = None
        try:
            if self.budgets.max_steps <= 0 or state.steps >= self.budgets.max_steps:
                stop_reason = 'max_steps'
            else:
                # 首次 inspect：不产生文字证据，只更新候选与成本。
                # resume 时它通常是已存收据的幂等命中：不复计步数，
                # 使 max_steps 边界下的恢复与全新运行执行同样多的动作（S1）。
                view, stop_reason = self._execute(goal, state, view, Action(kind='inspect'),
                                                  initial_action=True)
                while stop_reason is None:
                    if state.steps >= self.budgets.max_steps:
                        stop_reason = 'max_steps'
                        break
                    try:
                        action = self.policy.next_action(goal, view, state)
                    except Exception as exc:  # 策略实现错误不能伪装成成功
                        message = f'{type(exc).__name__}: {exc}'
                        self._record_policy_error(goal, state, view, message)
                        stop_reason, error = 'policy_error', message
                        break
                    if not isinstance(action, Action):
                        message = f'策略返回了非 Action 对象: {type(action).__name__}'
                        self._record_policy_error(goal, state, view, message)
                        stop_reason, error = 'policy_error', message
                        break
                    view, stop_reason = self._execute(goal, state, view, action)
        except LeaseLost:
            stop_reason, error = 'interrupted', None
        return self._finish(goal, stop_reason or 'stop', error)

    # ------------------------------------------------------------- 恢复与终态
    def _rebuild_state(self, goal: ResearchGoal, view: PageView,
                       receipts: list[Receipt]) -> PolicyState:
        """从已存收据重建策略状态；成本/步数/尝试次数不重复累计。"""
        state = PolicyState(goal=goal, budgets=self.budgets, page_version=view.page_version)
        spent = ZERO_COST
        for receipt in receipts:
            spent = spent + receipt.cost
        state.spent = spent
        state.steps = len(receipts)
        for receipt in receipts:
            key = f'{receipt.action}:{receipt.region or "-"}:{receipt.method or "-"}'
            state.attempted[key] = state.attempted.get(key, 0) + 1
            if receipt.status != 'completed':
                continue
            if receipt.action == 'interact' and receipt.region:
                state.interacted.add(receipt.region)
            for item in receipt.evidence:
                role = self._role_of(view, item.region)
                for objective in goal.objectives:
                    if objective.region_hints and role not in objective.region_hints:
                        continue
                    if not candidate_text(objective, item.text):
                        continue
                    bucket = state.candidates.setdefault(objective.objective_id, [])
                    if item.capture_id not in bucket:
                        bucket.append(item.capture_id)
        state.notes.append(f'恢复自 {len(receipts)} 条已存收据')
        return state

    @staticmethod
    def _role_of(view: PageView, region_id: str) -> str:
        if region_id == DOM_BODY_REGION or str(region_id).startswith(VIEWPORT_REGION_PREFIX):
            return 'section'
        try:
            return view.region(region_id).role
        except KeyError:
            return 'unknown'

    def _finish_stale(self, goal: ResearchGoal, latest: str, view: PageView) -> TaskTrace:
        """resume 时页面版本已变化：追加 stale 收据并停止，不把旧证据当当前事实。

        stale 收据不绑定具体 page_version（``page_version=''``）：我们拒绝把这次
        失败的恢复关联到任何可信的当前页面版本，同时避免覆盖任务记录的 latest。
        每次 stale 判定都留一条独立审计收据（唯一幂等键），不会被静默合并。
        """
        assert self._task_id is not None
        observation = Observation(action='inspect', page_version='', cost=ZERO_COST,
                                  note=f'page_version_changed:{latest}->{view.page_version}')
        base = receipt_key(task_id=self._task_id, backend=self.backend,
                           page_version='', action=Action(kind='inspect'))
        try:
            receipt = build_receipt(self._receipt_goal(goal), self.backend,
                                    Action(kind='inspect'), observation, view=None,
                                    status='stale', next='stop',
                                    idempotency_key=self._audit_key(base, 'stale'))
            self._append(receipt)
            self._finish_task('blocked', 'page_version_changed', None, 'page_version_changed')
        except LeaseLost:
            # 新 owner 已接管：不得覆盖其终态，按存储现状返回轨迹。
            pass
        return self.store.trace(self._task_id, goal, policy=self._policy_name(),
                                backend=self.backend)

    def _finish_open_failed(self, goal: ResearchGoal, exc: Exception) -> TaskTrace:
        """driver.open 失败：落 failed inspect 收据与失败终态，不让任务停留在 pending。"""
        assert self._task_id is not None
        message = f'{type(exc).__name__}: {exc}'
        action = Action(kind='inspect')
        observation = Observation(action='inspect', page_version='', cost=ZERO_COST,
                                  note=message)
        base = receipt_key(task_id=self._task_id, backend=self.backend,
                           page_version='', action=action)
        try:
            receipt = build_receipt(self._receipt_goal(goal), self.backend, action,
                                    observation, view=None, status='failed', next='stop',
                                    idempotency_key=self._audit_key(base, 'open_failed'))
            self._append(receipt)
            self._finish_task('failed', 'open_failed', message, 'open_failed')
        except LeaseLost:
            pass
        return self.store.trace(self._task_id, goal, policy=self._policy_name(),
                                backend=self.backend)

    def _finish(self, goal: ResearchGoal, stop_reason: str,
                error: str | None) -> TaskTrace:
        status = self._task_status(stop_reason)
        gap = self._gap(stop_reason)
        if status == 'succeeded':
            met, required = self._required_coverage(goal)
            if met < required:
                # 执行正常结束但 required 目标一条候选证据都没有：不能写成目标达成。
                status, gap = 'failed', UNCOVERED_GAP
        elif status == 'failed' and gap is None:
            # 未知/未归类停止原因也不得留下空缺口；先按候选覆盖判定是否算未覆盖。
            met, required = self._required_coverage(goal)
            if met < required:
                gap = UNCOVERED_GAP
        try:
            self._finish_task(status, stop_reason, error, gap)
        except LeaseLost:
            # 新 owner 已接管：不得覆盖其终态，按存储现状返回轨迹。
            pass
        assert self._task_id is not None
        return self.store.trace(self._task_id, goal, policy=self._policy_name(),
                                backend=self.backend)

    def _required_coverage(self, goal: ResearchGoal) -> tuple[int, int]:
        """required objective 中已有候选证据（candidate_text，不依赖 role 先验）的数量。"""
        required = goal.required_objectives
        if not required or self._task_id is None:
            return 0, len(required)
        evidence = self.store.evidence(self._task_id)
        met = sum(1 for objective in required
                  if any(candidate_text(objective, item.text) for item in evidence))
        return met, len(required)

    def _finish_task(self, status: str, stop_reason: str, error: str | None,
                     gap: str | None) -> None:
        assert self._task_id is not None
        self.store.finish_task(self._task_id, status=status, stop_reason=stop_reason,
                               error=error, gap=gap, session_id=self._session_id,
                               owner=self._owner, token=self._lease_token)

    @staticmethod
    def _task_status(stop_reason: str) -> str:
        if stop_reason == 'interrupted':
            return 'interrupted'
        if stop_reason in _FAILED_STOP_REASONS:
            return 'failed'
        if stop_reason in _BLOCKED_STOP_REASONS:
            return 'blocked'
        if stop_reason.startswith(_BLOCKED_PREFIX):
            return 'blocked'
        if stop_reason.startswith('failed:'):
            return 'failed'
        if stop_reason in _COMPLETION_STOP_REASONS:
            return 'succeeded'
        # 未知停止原因不静默判成功：执行结束 ≠ 目标达成。
        return 'failed'

    @staticmethod
    def _gap(stop_reason: str) -> str | None:
        if not stop_reason:
            return None
        if stop_reason.startswith(_BLOCKED_PREFIX):
            return stop_reason
        if stop_reason in _GAP_STOP_REASONS:
            return stop_reason
        return None

    def _resolve_receipt(self, state: PolicyState,
                         action: Action) -> tuple[str, Receipt | None]:
        """解析本次动作应使用的幂等键。

        - 基础键上的 completed 收据 → 幂等复用（不重复调用驱动）；
        - 基础键上是 failed/deferred/budget_exhausted 等非 completed 收据 →
          使用含尝试序号的 :func:`acquisition.receipts.retry_key`，让重试成功的
          completed 收据独立留痕（否则 ``INSERT OR IGNORE`` 会静默丢弃新收据，
          造成证据已入库但收据/成本缺失）。
        """
        assert self._task_id is not None
        base = receipt_key(task_id=self._task_id, backend=self.backend,
                           page_version=state.page_version, action=action)
        existing = self.store.receipt(self._task_id, base)
        if existing is None:
            return base, None
        if existing.status == 'completed':
            return base, existing
        attempt = 1
        while True:
            candidate_key = retry_key(base, attempt)
            candidate = self.store.receipt(self._task_id, candidate_key)
            if candidate is None:
                return candidate_key, None
            if candidate.status == 'completed':
                return candidate_key, candidate
            attempt += 1

    # ---------------------------------------------------------------- 单步执行
    def _execute(self, goal: ResearchGoal, state: PolicyState, view: PageView,
                 action: Action, *, initial_action: bool = False) -> tuple[PageView, str | None]:
        """执行一个动作，返回 (最新视图, 停止原因或 None)。"""
        self._assert_lease()
        key, existing = self._resolve_receipt(state, action)
        if existing is not None and existing.status == 'completed':
            # 幂等命中：复用结果，不重复调用驱动、不落新收据。
            # 恢复时的初始 inspect 已由已存收据计入步数，不再重复计数（S1）；
            # 其他重复提议仍计一次尝试，保证循环受 max_steps 约束、不空转。
            if not initial_action:
                self._count_attempt(state, action)
            if action.kind == 'stop':
                return view, existing.note or action.reason or 'stop'
            return view, None

        if action.kind != 'stop' and not state.can_afford(action.cost):
            note = (f'预算不足: 需要 {action.cost.as_dict()}，'
                    f'剩余单位 {state.remaining_units()}')
            self._append(self._build_control_receipt(
                goal, action, view, status='budget_exhausted', note=note,
                idempotency_key=key))
            self._count_attempt(state, action)
            return view, 'budget_exhausted'

        if action.kind != 'stop' and self.router is not None:
            allowed, reason = self._route(action, view)
            if not allowed:
                note = f'路由拒绝: {reason}'
                self._append(self._build_control_receipt(
                    goal, action, view, status='deferred', note=note,
                    idempotency_key=key))
                self._count_attempt(state, action)
                return view, None

        if action.kind == 'stop':
            observation = Observation(action='stop', page_version=view.page_version,
                                      cost=ZERO_COST, note=action.reason)
            receipt = build_receipt(self._receipt_goal(goal), self.backend, action,
                                    observation, view=view, idempotency_key=key)
            self._append(receipt)
            state.record(action, observation)
            state.stop_reason = action.reason
            return view, action.reason

        try:
            observation = self._call_driver(goal, view, action)
        except LeaseLost:
            raise
        except Exception as exc:  # 驱动失败：成本照记，记录 failed 收据，由策略决定是否继续
            message = f'{type(exc).__name__}: {exc}'
            observation = Observation(action=action.kind, page_version=state.page_version,
                                      cost=action.cost, note=message)
            receipt = build_receipt(self._receipt_goal(goal), self.backend, action,
                                    observation, view=view, status='failed',
                                    idempotency_key=key)
            self._append(receipt)
            state.spent = state.spent + action.cost
            self._count_attempt(state, action)
            return view, None

        new_view = observation.view or view
        cost = observation.cost if observation.cost != ZERO_COST else action.cost
        effective = replace(observation, cost=cost,
                            page_version=observation.page_version or state.page_version)
        page_blocked = bool(new_view.blocked_reason)
        action_blocked = bool(effective.blocked) or (page_blocked and action.kind in ('extract', 'interact'))
        if action_blocked:
            status = 'blocked'
            evidence: tuple[Evidence, ...] = ()
        else:
            status = 'completed'
            evidence = self._evidence_from(goal, effective, new_view)
        receipt = build_receipt(self._receipt_goal(goal), self.backend, action,
                                effective, view=new_view, evidence=evidence, status=status,
                                idempotency_key=key)
        self._append(receipt)
        for item in receipt.evidence:
            self.store.save_evidence(item, task_id=self._task_id or '',
                                     session_id=self._session_id, owner=self._owner,
                                     token=self._lease_token)
        state.record(action, effective)
        if page_blocked and action.kind in ('extract', 'interact'):
            # 阻塞页：证据一律不入库（上面已强制空证据），立即停止。
            return new_view, f'{_BLOCKED_PREFIX}{new_view.blocked_reason}'
        return new_view, None

    def _route(self, action: Action, view: PageView) -> tuple[bool, str]:
        try:
            result = self.router.validate_action(action, view, self.budgets)
        except Exception as exc:
            return False, f'路由异常 {type(exc).__name__}: {exc}'
        if not isinstance(result, tuple) or len(result) != 2:
            return False, '路由返回格式非法'
        allowed, reason = result
        return bool(allowed), str(reason or '')

    def _call_driver(self, goal: ResearchGoal, view: PageView,
                     action: Action) -> Observation:
        if action.kind == 'inspect':
            observation = self.driver.inspect(view)
        elif action.kind == 'locate':
            observation = self.driver.locate(view, goal)
        elif action.kind == 'interact':
            region = self._resolve_region(view, action.region_id or '')
            observation = self.driver.interact(view, region)
        elif action.kind == 'extract':
            region = self._resolve_region(view, action.region_id or '')
            observation = self.driver.extract(view, region, action.method or 'dom')
        else:  # pragma: no cover - Action 校验已保证
            raise ValueError(f'未知动作类型: {action.kind!r}')
        if not isinstance(observation, Observation):
            raise TypeError('驱动必须返回 Observation')
        return observation

    @staticmethod
    def _resolve_region(view: PageView, region_id: str) -> Region:
        """把语义区域 id 或虚拟区域 id（dom:body / viewport:i）解析为 Region。"""
        if not region_id:
            raise ValueError('动作缺少区域 id')
        try:
            return view.region(region_id)
        except KeyError:
            pass
        if region_id == DOM_BODY_REGION:
            width = view.viewport[0]
            height = max(view.viewport[1], view.scroll_height)
            return Region(region_id=DOM_BODY_REGION, role='section',
                          bbox=(0, 0, width, height), text='')
        if region_id.startswith(VIEWPORT_REGION_PREFIX):
            index_text = region_id[len(VIEWPORT_REGION_PREFIX):]
            if not index_text.isdigit():
                raise ValueError(f'视口编号不合法: {region_id!r}')
            index = int(index_text)
            offsets = fixed_frame_offsets(view.scroll_height, view.viewport[1])
            if index >= len(offsets):
                raise ValueError(f'视口编号超出固定帧范围: {region_id}')
            y = offsets[index]
            return Region(region_id=region_id, role='section',
                          bbox=(0, y, view.viewport[0], view.viewport[1]), scroll_y=y)
        raise ValueError(f'区域不存在: {region_id}')

    def _build_control_receipt(self, goal: ResearchGoal, action: Action, view: PageView,
                               *, status: str, note: str,
                               idempotency_key: str | None = None) -> Receipt:
        observation = Observation(action=action.kind, page_version=view.page_version,
                                  cost=ZERO_COST, note=note)
        return build_receipt(self._receipt_goal(goal), self.backend, action, observation,
                             view=view, status=status, idempotency_key=idempotency_key)

    @staticmethod
    def _count_attempt(state: PolicyState, action: Action) -> None:
        """未执行驱动、但已消耗一次策略尝试的动作（deferred / 预算不足）。"""
        state.steps += 1
        key = state.attempt_key(action)
        state.attempted[key] = state.attempted.get(key, 0) + 1

    def _record_policy_error(self, goal: ResearchGoal, state: PolicyState,
                             view: PageView, message: str) -> None:
        assert self._task_id is not None
        action = Action(kind='stop', reason='策略异常')
        observation = Observation(action='stop', page_version=view.page_version,
                                  cost=ZERO_COST, note=message)
        base = receipt_key(task_id=self._task_id, backend=self.backend,
                           page_version=view.page_version, action=action)
        receipt = build_receipt(self._receipt_goal(goal), self.backend, action, observation,
                                view=view, status='failed', next='stop',
                                idempotency_key=self._audit_key(base, 'policy_error'))
        self._append(receipt)
        self._count_attempt(state, action)

    def _evidence_from(self, goal: ResearchGoal, observation: Observation,
                       view: PageView) -> tuple[Evidence, ...]:
        """提取结果转证据：永远是 ``verified=False``，事实核验只能由独立 verifier 追加。"""
        if not observation.readable or not observation.extraction:
            return ()
        region_id = observation.region_id or DOM_BODY_REGION
        capture_id = observation.capture_id or stable_id('capture_', {
            'task_id': goal.task_id,
            'page_version': observation.page_version,
            'region': observation.region_id,
            'text': observation.text,
        })
        evidence = Evidence(capture_id=capture_id, page_version=observation.page_version,
                            region=region_id, extraction=observation.extraction,
                            text=observation.text, bbox=observation.bbox,
                            source_url=view.url, verified=False, note=observation.note,
                            at=now_iso())
        return (evidence,)
