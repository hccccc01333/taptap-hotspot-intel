"""V4 Evidence Acquisition Harness（切片 1）。

本包是 ``docs/V4_ARCHITECTURE.md`` 中 Acquisition Harness 的第一个可评测纵向切片，
与 V3 生产链路并行存在；V3 仍使用 ``agent_v3`` 现有实现，本包不修改其行为。

模块边界：

- ``model``：跨模块冻结契约（receipt / evidence / action / policy 状态）；
- ``driver``：页面驱动（冻结回放驱动为默认；真实 Playwright 适配为可选后端）；
- ``store``：任务、session、receipt、证据的持久化与恢复；
- ``harness``：唯一执行循环，负责预算、幂等、阻塞页面与证据落库；
- ``policy``：固定三屏基线、现状 V3 基线、自适应观察策略；
- ``router``：按缺口与能力约束选择后端，并解释原因；
- ``skills``：skillproof 风格 Skill 注册、完成验证与复用门禁；
- ``evaluate``：冻结样本上的离线对照评测（机制验证，不是真实网页质量结论）。

设计底线：截图或 OCR 成功不等于事实成立；证据默认 ``verified=False``，
只有独立 verifier 能改变该状态。详见 ``model`` 模块说明。
"""
from .model import (  # noqa: F401
    ACTION_KINDS,
    BACKENDS,
    DEFAULT_UNIT_WEIGHTS,
    DOM_BODY_REGION,
    EXTRACTIONS,
    FIXED_FRAME_STEP,
    SCHEMA_VERSION,
    VIEWPORT_REGION_PREFIX,
    Action,
    Budget,
    Cost,
    Evidence,
    Observation,
    Objective,
    PageView,
    PolicyState,
    Receipt,
    Region,
    RegionHint,
    ResearchGoal,
    TaskTrace,
    Verification,
    candidate_text,
    fixed_frame_offsets,
    now_iso,
    stable_id,
    structural_signature,
)

__all__ = [
    'ACTION_KINDS', 'BACKENDS', 'DEFAULT_UNIT_WEIGHTS', 'DOM_BODY_REGION', 'EXTRACTIONS',
    'FIXED_FRAME_STEP', 'SCHEMA_VERSION', 'VIEWPORT_REGION_PREFIX', 'Action', 'Budget',
    'Cost', 'Evidence', 'Observation', 'Objective', 'PageView', 'PolicyState', 'Receipt',
    'Region', 'RegionHint', 'ResearchGoal', 'TaskTrace', 'Verification', 'candidate_text',
    'fixed_frame_offsets', 'now_iso', 'stable_id', 'structural_signature',
]
