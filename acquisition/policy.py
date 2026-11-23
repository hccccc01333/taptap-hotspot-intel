"""V4 Acquisition Harness 的观察策略层（接口冻结见接口冻结文档 §2.5）。

策略只回答「下一步看哪里、用什么通道看、什么时候停」，输出 :class:`model.Action`：

- 不计算全局预算、不写数据库、不读 fixture 评测真值、不宣告事实成立；
- 预算复核、阻塞页证据拦截、幂等与证据落库全部由 harness 负责；
- 候选证据（``PolicyState.candidates``）只是「值得交给独立 verifier 的线索」，
  策略永远不能把 ``Evidence.verified`` 置真。

三个策略对应三组对照：

``FixedFramePolicy``
    被替换的截图策略基线：只按 :func:`model.fixed_frame_offsets` 逐屏 OCR，
    不做目标匹配、不提前停止。保留基线原貌，所以哪怕页面阻塞也照样截图，
    是否入库由 harness 统一拦截。

``LegacyV3Policy``
    现状 V3 行为基线：先读 ``dom:body`` 整页正文，再做固定三屏 OCR，同样不按
    目标裁剪或提前停止。

``AdaptiveObservationPolicy``
    动态观察：按证据缺口与区域先验排序；懒加载区域先 ``interact`` 再提取；
    DOM 可读优先 ``dom``，只有视觉文本才 ``ocr``（``allow_vlm=True`` 时允许
    vlm 作为视觉兜底，默认不请求）；覆盖全部 required objective 或连续无增益
    即停止。

停止原因写进 ``Action.reason``，由 harness 落到任务的 ``stop_reason``：

- ``blocked:<captcha|login_wall|access_denied>``：页面阻塞，立即停止；
- ``objectives_covered``：所有 required objective 都已有候选证据（未核验）；
- ``login_required``：剩余未覆盖目标只能依赖登录墙后的区域；
- ``budget_exhausted``：步数或单位预算不足；
- ``no_new_evidence``：连续 ``min_gain_rounds`` 步观察无新增候选，或已无可行动区域；
- ``fixed_frames_exhausted`` / ``legacy_v3_exhausted``：固定基线跑完全部固定动作。

策略假设 harness 按顺序执行它返回的动作、并用 ``PolicyState.record`` 记账；被
router 打回（deferred）的动作不记账，所以只有显式打开 ``allow_vlm`` 并且 router
同时启用 vlm 时才应请求视觉模型，否则会重复提议同一动作。
"""
from __future__ import annotations

from typing import Sequence

from . import model


def _viewport_region_id(index: int) -> str:
    return f'{model.VIEWPORT_REGION_PREFIX}{index}'


def _stop(reason: str) -> model.Action:
    return model.Action(kind='stop', reason=reason)


def _is_login_gated(region: model.Region) -> bool:
    """``requires`` 里出现 login 说明该区域只有登录后可读。"""
    return 'login' in (region.requires or '').lower()


def _viewport_frames(view: model.PageView) -> tuple[int, ...]:
    return model.fixed_frame_offsets(view.scroll_height, view.viewport[1])


class FixedFramePolicy:
    """被替换的截图策略基线：固定三屏 OCR，不按目标裁剪、不提前停止。"""

    name = 'fixed_frames'
    STOP_REASON = 'fixed_frames_exhausted'

    def next_action(self, goal: model.ResearchGoal, view: model.PageView, state: model.PolicyState) -> model.Action:
        frames = _viewport_frames(view)
        for index, offset in enumerate(frames):
            action = model.Action(
                kind='extract',
                region_id=_viewport_region_id(index),
                method='ocr',
                reason=(
                    f'固定三屏基线第 {index + 1}/{len(frames)} 屏（滚动偏移 y={offset}，视口高 '
                    f'{view.viewport[1]}）：只做 OCR 截图，不做目标匹配、不提前停止'
                ),
            )
            if state.attempts(action) < 1:
                return action
        return _stop(self.STOP_REASON)


class LegacyV3Policy:
    """现状 V3 行为基线：``dom:body`` 整页正文 + 固定三屏 OCR。"""

    name = 'legacy_v3'
    STOP_REASON = 'legacy_v3_exhausted'

    def next_action(self, goal: model.ResearchGoal, view: model.PageView, state: model.PolicyState) -> model.Action:
        body = model.Action(
            kind='extract',
            region_id=model.DOM_BODY_REGION,
            method='dom',
            reason='现状 V3 行为：先读整页 DOM 正文（dom:body），不做目标裁剪',
        )
        if state.attempts(body) < 1:
            return body
        frames = _viewport_frames(view)
        for index, offset in enumerate(frames):
            action = model.Action(
                kind='extract',
                region_id=_viewport_region_id(index),
                method='ocr',
                reason=(
                    f'现状 V3 行为：固定三屏 OCR 第 {index + 1}/{len(frames)} 屏（滚动偏移 y={offset}），'
                    '不按目标提前停止'
                ),
            )
            if state.attempts(action) < 1:
                return action
        return _stop(self.STOP_REASON)


class AdaptiveObservationPolicy:
    """动态观察策略：按缺口与区域先验选择动作，覆盖目标或连续无增益即停止。"""

    name = 'adaptive'

    OBJECTIVES_COVERED = 'objectives_covered'
    LOGIN_REQUIRED = 'login_required'
    BUDGET_EXHAUSTED = 'budget_exhausted'
    NO_NEW_EVIDENCE = 'no_new_evidence'
    BLOCKED_PREFIX = 'blocked:'

    def __init__(
        self,
        *,
        hint_provider: model.HintProvider | None = None,
        allow_vlm: bool = False,
        max_attempts_per_action: int = 1,
        min_gain_rounds: int = 2,
    ) -> None:
        if not isinstance(max_attempts_per_action, int) or isinstance(max_attempts_per_action, bool) \
                or max_attempts_per_action < 1:
            raise ValueError('max_attempts_per_action 必须为正整数')
        if not isinstance(min_gain_rounds, int) or isinstance(min_gain_rounds, bool) or min_gain_rounds < 1:
            raise ValueError('min_gain_rounds 必须为正整数')
        self.hint_provider = hint_provider
        self.allow_vlm = bool(allow_vlm)
        self.max_attempts_per_action = max_attempts_per_action
        self.min_gain_rounds = min_gain_rounds
        # 策略内部的无增益记账：只在 harness 真正消费掉一次 extract 动作后统计，
        # interact/inspect 属于必要前置步骤，不算「无新增候选」。
        self._last_steps: int | None = None
        self._last_candidates = 0
        self._pending_extract = False
        self._no_gain_rounds = 0

    # ---------------------------------------------------------------- 主入口
    def next_action(self, goal: model.ResearchGoal, view: model.PageView, state: model.PolicyState) -> model.Action:
        self._track_gain(state)

        # 阻塞页立即停止，不浪费预算也不产生任何证据。
        if view.blocked_reason:
            return _stop(f'{self.BLOCKED_PREFIX}{view.blocked_reason}')

        # required objective 全部有候选证据即可停止（候选 ≠ 事实成立）。
        if state.complete():
            return _stop(self.OBJECTIVES_COVERED)

        unmet = self._unmet_required(goal, state)
        if unmet and all(self._login_gated_objective(objective, view) for objective in unmet):
            return _stop(self.LOGIN_REQUIRED)
        if state.steps >= state.budgets.max_steps:
            return _stop(self.BUDGET_EXHAUSTED)
        if self._no_gain_rounds >= self.min_gain_rounds:
            return _stop(self.NO_NEW_EVIDENCE)

        action, had_candidates = self._choose(goal, view, state, unmet)
        if action is not None:
            self._pending_extract = action.kind == 'extract'
            return action
        if had_candidates:
            return _stop(self.BUDGET_EXHAUSTED)
        return _stop(self.NO_NEW_EVIDENCE)

    # ------------------------------------------------------------ 无增益记账
    def _track_gain(self, state: model.PolicyState) -> None:
        total = sum(len(bucket) for bucket in state.candidates.values())
        if self._last_steps is not None and state.steps > self._last_steps:
            if self._pending_extract:
                if total > self._last_candidates:
                    self._no_gain_rounds = 0
                else:
                    self._no_gain_rounds += state.steps - self._last_steps
        self._last_steps = state.steps
        self._last_candidates = total
        self._pending_extract = False

    # -------------------------------------------------------------- 候选动作
    def _choose(
        self,
        goal: model.ResearchGoal,
        view: model.PageView,
        state: model.PolicyState,
        unmet: Sequence[model.Objective],
    ) -> tuple[model.Action | None, bool]:
        """返回 (选中的动作 | None, 是否存在候选动作)；候选全部不可负担时后者为真。"""
        entries = self._candidate_entries(goal, view, state, unmet)
        if not entries:
            return None, False
        entries.sort(key=lambda item: item[0])
        for _rank, action in entries:
            if state.can_afford(action.cost):
                return action, True
        return None, True

    def _candidate_entries(
        self,
        goal: model.ResearchGoal,
        view: model.PageView,
        state: model.PolicyState,
        unmet: Sequence[model.Objective],
    ) -> list[tuple[tuple[int, float, int, int, int], model.Action]]:
        hint_scores = self._hint_scores(view, goal)
        entries: list[tuple[tuple[int, float, int, int, int], model.Action]] = []
        for objective_index, objective in enumerate(unmet):
            objective_entries: list[tuple[tuple[int, float, int, int, int], model.Action]] = []
            for region_index, region in enumerate(view.regions):
                if region.blocked or _is_login_gated(region):
                    continue
                if not model.candidate_region(objective, region):
                    continue
                priority_class = self._region_class(objective, region, hint_scores)
                for method_index, action in enumerate(self._actions_for_region(region, state)):
                    if state.attempts(action) >= self.max_attempts_per_action:
                        continue
                    rank = (
                        priority_class,
                        -hint_scores.get(region.region_id, 0.0),
                        objective_index,
                        region_index,
                        method_index,
                    )
                    objective_entries.append((rank, action))
            if not objective_entries:
                # 没有区域先验的 objective 才允许回退到虚拟区域（整页 DOM / 固定屏幕）。
                objective_entries = self._virtual_entries(objective, view, state, objective_index)
            entries.extend(objective_entries)
        return entries

    @staticmethod
    def _region_class(
        objective: model.Objective,
        region: model.Region,
        hint_scores: dict[str, float],
    ) -> int:
        """候选区域优先级类别（越小越先看），冻结顺序见接口文档 §2.5。"""
        if region.region_id in hint_scores:
            return 0  # 合法 Skill hint
        if objective.region_hints and region.role in objective.region_hints:
            return 1  # objective 的区域先验匹配
        if model.candidate_text(objective, region.text):
            return 2  # 已可读 DOM 文本本身就是候选
        if region.visual_text.strip() or region.visual_only:
            return 3  # 只有视觉内容，需要 OCR
        return 4  # 其他区域

    def _actions_for_region(self, region: model.Region, state: model.PolicyState) -> tuple[model.Action, ...]:
        """一个区域上的动作序列：懒加载先交互，DOM 可读优先 dom，纯视觉才 ocr。"""
        if (
            region.lazy
            and region.region_id not in state.interacted
            and not region.text.strip()
            and not region.visual_text.strip()
        ):
            return (
                model.Action(
                    kind='interact',
                    region_id=region.region_id,
                    reason=f'区域 {region.region_id} 是懒加载区域，先交互一次触发内容加载，再决定 DOM/OCR',
                ),
            )
        actions: list[model.Action] = []
        if region.text.strip():
            actions.append(model.Action(
                kind='extract', region_id=region.region_id, method='dom',
                reason=f'区域 {region.region_id} 的 DOM 文本可读，优先用 dom 提取（最低成本）',
            ))
        if region.visual_text.strip() or region.visual_only:
            actions.append(model.Action(
                kind='extract', region_id=region.region_id, method='ocr',
                reason=f'区域 {region.region_id} 的答案只在视觉文本里，用 OCR 转录',
            ))
            if self.allow_vlm:
                actions.append(model.Action(
                    kind='extract', region_id=region.region_id, method='vlm',
                    reason=f'区域 {region.region_id} 允许 vlm 兜底（仅当 OCR 尝试后仍缺证据）',
                ))
        return tuple(actions)

    def _virtual_entries(
        self,
        objective: model.Objective,
        view: model.PageView,
        state: model.PolicyState,
        objective_index: int,
    ) -> list[tuple[tuple[int, float, int, int, int], model.Action]]:
        """无区域先验时的回退：整页 DOM 正文，其次固定屏幕 OCR。"""
        if objective.region_hints:
            return []
        entries: list[tuple[tuple[int, float, int, int, int], model.Action]] = []
        body = model.Action(
            kind='extract', region_id=model.DOM_BODY_REGION, method='dom',
            reason='目标没有区域先验且语义区域不可读，回退读取整页 DOM 正文（dom:body）',
        )
        if view.dom_text.strip() and state.attempts(body) < self.max_attempts_per_action:
            entries.append(((5, 0.0, objective_index, -1, 0), body))
        frames = _viewport_frames(view)
        for index, _offset in enumerate(frames):
            action = model.Action(
                kind='extract', region_id=_viewport_region_id(index), method='ocr',
                reason=f'目标没有区域先验，回退固定屏幕 OCR（viewport:{index}）',
            )
            if state.attempts(action) < self.max_attempts_per_action:
                entries.append(((6, 0.0, objective_index, -1, index), action))
        return entries

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _unmet_required(goal: model.ResearchGoal, state: model.PolicyState) -> tuple[model.Objective, ...]:
        return tuple(
            objective for objective in goal.required_objectives
            if not state.candidates.get(objective.objective_id)
        )

    @staticmethod
    def _login_gated_objective(objective: model.Objective, view: model.PageView) -> bool:
        """该 objective 在本页是否只能靠登录墙后的区域满足。"""
        regions = [
            region for region in view.regions
            if not region.blocked and model.candidate_region(objective, region)
        ]
        return bool(regions) and all(_is_login_gated(region) for region in regions)

    def _hint_scores(self, view: model.PageView, goal: model.ResearchGoal) -> dict[str, float]:
        """只使用 hint_provider 返回且指向当前视图区域的结果；不自行编造先验。"""
        provider = self.hint_provider
        if provider is None:
            return {}
        hints = provider.hints(view, goal) or ()
        known = {region.region_id for region in view.regions}
        scores: dict[str, float] = {}
        for hint in hints:
            region_id = getattr(hint, 'region_id', None)
            if not region_id or region_id not in known:
                continue
            score = float(getattr(hint, 'score', 0.0) or 0.0)
            scores[region_id] = max(scores.get(region_id, 0.0), score)
        return scores


__all__ = ['AdaptiveObservationPolicy', 'FixedFramePolicy', 'LegacyV3Policy']
