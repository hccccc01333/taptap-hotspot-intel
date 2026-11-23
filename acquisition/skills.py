"""Skill 适配：skillproof 风格 Skill 的加载、门禁、区域先验与流程完成验证。

Skill 是「页面结构先验 + 流程说明 + 完成条件」的静态描述（目录内
``SKILL.md`` / ``manifest.json`` / ``verify.json``），不是可执行代码：本模块
从不执行 ``SKILL.md`` 中的任何内容，只读取 manifest 与 verify 两个 JSON。

三条不可协商的边界：

1. 只有通过门禁的 Skill 才能产出 :class:`acquisition.model.RegionHint`。门禁包括：
   主机 ∈ ``allowed_hosts``、``permissions.site_rules_reviewed = true``、
   页面结构签名 ∈ ``structural_signatures``、``permissions.login_required``
   与页面登录场景一致，以及页面未被整体阻塞。区域先验只影响观察顺序，
   不构成事实结论。
2. :meth:`SkillRegistry.verify` 只检查**流程完成条件**（区域存在、提取非空、
   文本模式命中、页面未阻塞、证据版本是当前版本），返回独立的
   :class:`SkillVerification`；它不读取也不改写任何
   :class:`acquisition.model.Evidence`，更不会把 ``Evidence.verified`` 置真。
   事实核验只能由独立的 ``model.EvidenceVerifier`` 完成。
3. Skill 不读取样本中的 ``expected_answer`` / ``expected_region_role`` 等评测真值；
   结构签名只由区域角色序列计算（见 ``model.structural_signature``），与答案内容无关。

参考：``acquisition/fixtures/README.md`` 与
``docs_local/journal/V4-切片-接口冻结-20261113.md`` §2.7。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .model import (
    REGION_ROLES,
    PageView,
    RegionHint,
    ResearchGoal,
    TaskTrace,
    now_iso,
)

#: Skill 目录内固定文件名。
MANIFEST_FILE = 'manifest.json'
VERIFY_FILE = 'verify.json'
SKILL_FILE = 'SKILL.md'

#: 两个 JSON 的 schema 标识；与 fixtures/README.md 一致。
SKILL_MANIFEST_SCHEMA = 'acquisition-skill-manifest-v0.1'
SKILL_VERIFY_SCHEMA = 'acquisition-skill-verify-v0.1'

#: ``model.structural_signature`` 的输出格式（ ``struct:`` + 16 位十六进制）。
STRUCTURAL_SIGNATURE_RE = re.compile(r'^struct:[0-9a-f]{16}$')

#: verify.json 必须恰好覆盖的五类流程完成检查。
VERIFY_KINDS = (
    'region_present',
    'extraction_non_empty',
    'text_pattern',
    'page_not_blocked',
    'page_version_current',
)

#: Skill 先验的基础分值：按 manifest.region_roles 顺序递减。
_HINT_BASE_SCORE = 0.9
_HINT_SCORE_STEP = 0.2
#: 角色命中目标 objective.region_hints 时的加分。
_HINT_OBJECTIVE_BOOST = 0.1


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _read_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        raise ValueError(f'Skill 文件不是合法 JSON: {path}: {error}') from error
    _require(isinstance(raw, dict), f'Skill 文件根节点必须是对象: {path}')
    return raw


def _string_tuple(raw: dict, key: str, context: str, *,
                  allowed: tuple[str, ...] | None = None) -> tuple[str, ...]:
    """读取非空字符串列表并校验取值集合与重复项。"""
    value = raw.get(key)
    _require(isinstance(value, list) and bool(value), f'{context}: {key} 必须是非空字符串列表')
    result: list[str] = []
    for item in value:
        _require(isinstance(item, str) and bool(item.strip()), f'{context}: {key} 含非字符串或空字符串项')
        item = item.strip()
        if allowed is not None:
            _require(item in allowed, f'{context}: {key} 含未知取值 {item!r}')
        result.append(item)
    _require(len(set(result)) == len(result), f'{context}: {key} 不允许重复项')
    return tuple(result)


@dataclass(frozen=True)
class SkillManifest:
    """一个 Skill 的加载结果与门禁输入；不包含任何可执行内容。"""
    skill_id: str
    version: str
    allowed_hosts: tuple[str, ...]
    structural_signatures: tuple[str, ...]
    region_roles: tuple[str, ...]
    permissions: dict
    skill_dir: Path


@dataclass(frozen=True)
class SkillVerification:
    """Skill 流程完成验证结果。

    注意：``passed=True`` 只表示「按该 Skill 描述的流程走完了」，不是事实核验，
    与任何 :class:`acquisition.model.Evidence` 的 ``verified`` 字段无关。
    """
    skill_id: str
    passed: bool
    checks: tuple[dict, ...]
    at: str


def _parse_manifest(skill_dir: Path) -> SkillManifest:
    """读取并校验一个 Skill 目录的 manifest.json。"""
    context = f'Skill {skill_dir.name}'
    manifest_path = skill_dir / MANIFEST_FILE
    _require(manifest_path.is_file(), f'{context}: 缺少 {MANIFEST_FILE}')
    raw = _read_json(manifest_path)
    _require(raw.get('schema') == SKILL_MANIFEST_SCHEMA,
             f'{context}: manifest schema 必须为 {SKILL_MANIFEST_SCHEMA}')

    skill_id = raw.get('skill_id')
    _require(isinstance(skill_id, str) and bool(skill_id.strip()), f'{context}: skill_id 不能为空')
    skill_id = skill_id.strip()
    _require(skill_id == skill_dir.name, f'{context}: skill_id 必须与目录名一致（目录名 {skill_dir.name}）')

    version = raw.get('version')
    _require(isinstance(version, str) and bool(version.strip()), f'{context}: version 不能为空')

    allowed_hosts = _string_tuple(raw, 'allowed_hosts', context)
    for host in allowed_hosts:
        _require('://' not in host and '/' not in host and ':' not in host,
                 f'{context}: allowed_hosts 只接受主机名（不含 scheme/端口/路径）：{host!r}')

    signatures = _string_tuple(raw, 'structural_signatures', context)
    for signature in signatures:
        _require(bool(STRUCTURAL_SIGNATURE_RE.match(signature)),
                 f'{context}: structural_signatures 含非法签名 {signature!r}（应由 model.structural_signature 生成）')

    region_roles = _string_tuple(raw, 'region_roles', context, allowed=REGION_ROLES)

    permissions = raw.get('permissions')
    _require(isinstance(permissions, dict), f'{context}: permissions 必须是对象')
    _require(isinstance(permissions.get('site_rules_reviewed'), bool),
             f'{context}: permissions.site_rules_reviewed 必须是布尔值（表示站点规则是否已人工复核）')
    _require(isinstance(permissions.get('login_required'), bool),
             f'{context}: permissions.login_required 必须是布尔值（表示该 Skill 是否面向需登录场景）')

    verify_ref = raw.get('verify', VERIFY_FILE)
    _require(verify_ref == VERIFY_FILE, f'{context}: verify 字段必须指向 {VERIFY_FILE}')
    _require((skill_dir / VERIFY_FILE).is_file(), f'{context}: 缺少 {VERIFY_FILE}')

    skill_md = skill_dir / SKILL_FILE
    _require(skill_md.is_file(), f'{context}: 缺少 {SKILL_FILE}')
    _require(bool(skill_md.read_text(encoding='utf-8').strip()),
             f'{context}: {SKILL_FILE} 不能为空（说明文档可以不执行，但必须存在）')

    return SkillManifest(
        skill_id=skill_id,
        version=version.strip(),
        allowed_hosts=allowed_hosts,
        structural_signatures=signatures,
        region_roles=region_roles,
        permissions=dict(permissions),
        skill_dir=skill_dir,
    )


def _parse_verify(skill_dir: Path, skill_id: str) -> dict:
    """读取并校验 verify.json，返回归一化后的检查规格。"""
    context = f'Skill {skill_id}'
    verify_path = skill_dir / VERIFY_FILE
    _require(verify_path.is_file(), f'{context}: 缺少 {VERIFY_FILE}')
    raw = _read_json(verify_path)
    _require(raw.get('schema') == SKILL_VERIFY_SCHEMA,
             f'{context}: verify schema 必须为 {SKILL_VERIFY_SCHEMA}')
    _require(raw.get('skill_id') == skill_id, f'{context}: verify.skill_id 与 manifest 不一致')

    checks_raw = raw.get('checks')
    _require(isinstance(checks_raw, list) and bool(checks_raw),
             f'{context}: verify.checks 必须是非空列表')
    checks: list[dict] = []
    seen_ids: set[str] = set()
    seen_kinds: list[str] = []
    for item in checks_raw:
        _require(isinstance(item, dict), f'{context}: verify.checks 每项必须是对象')
        check_id = item.get('id')
        _require(isinstance(check_id, str) and bool(check_id.strip()), f'{context}: 检查项必须有 id')
        check_id = check_id.strip()
        _require(check_id not in seen_ids, f'{context}: 检查项 id 重复：{check_id}')
        seen_ids.add(check_id)
        kind = item.get('kind')
        _require(kind in VERIFY_KINDS, f'{context}: 未知检查类型 {kind!r}')
        check: dict = {'id': check_id, 'kind': kind, 'note': str(item.get('note') or '')}
        if kind == 'region_present':
            check['roles'] = _string_tuple(item, 'roles', f'{context} 检查 {check_id}', allowed=REGION_ROLES)
        elif kind == 'extraction_non_empty':
            min_chars = item.get('min_chars', 1)
            _require(isinstance(min_chars, int) and not isinstance(min_chars, bool) and min_chars > 0,
                     f'{context} 检查 {check_id}: min_chars 必须是正整数')
            check['min_chars'] = min_chars
        elif kind == 'text_pattern':
            patterns = _string_tuple(item, 'patterns', f'{context} 检查 {check_id}')
            for pattern in patterns:
                _require(bool(pattern.strip()), f'{context} 检查 {check_id}: 文本模式不能为空')
            check['patterns'] = patterns
        checks.append(check)
        seen_kinds.append(kind)
    _require(sorted(seen_kinds) == sorted(VERIFY_KINDS),
             f'{context}: verify.json 必须恰好包含五类流程完成检查 {VERIFY_KINDS}')
    return {'skill_id': skill_id, 'checks': tuple(checks)}


def _login_required_scenario(manifest: SkillManifest, view: PageView) -> bool:
    """判断页面在 Skill 关心的目标区域上是否属于「需要登录」场景。

    只检查目标角色区域与整体登录墙，不把普通页面的其他区域误判为登录场景。
    """
    if view.blocked_reason == 'login_wall':
        return True
    target_roles = set(manifest.region_roles)
    return any(
        region.role in target_roles and (region.requires or '').strip().lower().startswith('login')
        for region in view.regions
    )


def _manifest_eligible(manifest: SkillManifest, view: PageView, *,
                       host: str, signature: str) -> bool:
    """Skill 门禁：host、站点规则复核、结构签名、登录场景。"""
    if host not in {item.lower() for item in manifest.allowed_hosts}:
        return False
    if manifest.permissions.get('site_rules_reviewed') is not True:
        return False
    if signature not in manifest.structural_signatures:
        return False
    return _login_required_scenario(manifest, view) == bool(manifest.permissions.get('login_required'))


def _check_result(check: dict, *, passed: bool, expected: str, actual: str) -> dict:
    return {
        'id': check['id'],
        'kind': check['kind'],
        'passed': bool(passed),
        'expected': expected,
        'actual': actual,
        'detail': check.get('note', ''),
    }


class SkillRegistry:
    """Skill 注册表：加载目录、门禁、区域先验与流程完成验证。

    ``load(root)`` 会读取 ``root/<skill_id>/`` 下的 ``manifest.json`` 与
    ``verify.json``；任一 Skill 不合法都会立即抛 ``ValueError``，避免带着坏
    Skill 进入评测。
    """

    def __init__(self, manifests: tuple[SkillManifest, ...] = (),
                 verify_specs: dict[str, dict] | None = None) -> None:
        self._manifests = tuple(manifests)
        self._verify_specs = dict(verify_specs or {})

    # ------------------------------------------------------------------ 加载
    @classmethod
    def load(cls, root: Path) -> 'SkillRegistry':
        """从 ``root`` 目录加载全部 Skill；目录不存在抛 ``FileNotFoundError``。"""
        root = Path(root)
        if not root.is_dir():
            raise FileNotFoundError(f'Skill 根目录不存在: {root}')
        manifests: list[SkillManifest] = []
        verify_specs: dict[str, dict] = {}
        skill_dirs = sorted((path for path in root.iterdir()
                             if path.is_dir() and not path.name.startswith('.')),
                            key=lambda path: path.name)
        for skill_dir in skill_dirs:
            manifest = _parse_manifest(skill_dir)
            verify_spec = _parse_verify(skill_dir, manifest.skill_id)
            _require(manifest.skill_id not in verify_specs,
                     f'Skill {manifest.skill_id}: skill_id 重复')
            manifests.append(manifest)
            verify_specs[manifest.skill_id] = verify_spec
        return cls(tuple(manifests), verify_specs)

    def manifests(self) -> tuple[SkillManifest, ...]:
        """已加载的 Skill（只读快照）。"""
        return self._manifests

    # ------------------------------------------------------------------ 门禁
    def eligible(self, view: PageView, *, host: str) -> tuple[SkillManifest, ...]:
        """返回当前页面可用的 Skill。

        ``host`` 是页面主机名（不含 scheme/端口）。阻塞页面一律不可用：
        在验证码、登录墙、访问拒绝页面上不应使用任何区域先验。
        """
        host_key = (host or '').strip().lower()
        if view.blocked_reason is not None:
            return ()
        signature = view.signature
        return tuple(manifest for manifest in self._manifests
                     if _manifest_eligible(manifest, view, host=host_key, signature=signature))

    # ------------------------------------------------------------------ 先验
    def hints(self, view: PageView, goal: ResearchGoal) -> tuple[RegionHint, ...]:
        """按合法 Skill 产出区域先验。

        - 只对 :meth:`eligible` 为真的页面产出 hint；
        - 区域按 ``manifest.region_roles`` 顺序递减打分，命中目标
          ``objective.region_hints`` 的角色再加权；
        - 分值统一 clamp 到 ``[0, 1]``：角色数量较多时低优先级角色也不会得到
          负分（``RegionHint`` 要求 ``0 <= score <= 1``，加载期允许任意多的
          合法区域角色，因此这里必须保证不越界）；
        - hint 只影响观察顺序，不构成事实结论，也不产生证据。
        """
        host = (urlparse(view.url).hostname or '')
        eligible = self.eligible(view, host=host)
        if not eligible:
            return ()
        objective_roles = {role for objective in goal.objectives for role in objective.region_hints}
        best: dict[str, RegionHint] = {}
        for manifest in eligible:
            for index, role in enumerate(manifest.region_roles):
                base = _HINT_BASE_SCORE - _HINT_SCORE_STEP * index
                boost = _HINT_OBJECTIVE_BOOST if role in objective_roles else 0.0
                for region in view.regions:
                    if region.role != role or region.blocked:
                        continue
                    score = float(min(1.0, max(0.0, round(base + boost, 2))))
                    hint = RegionHint(
                        region_id=region.region_id,
                        score=score,
                        source=manifest.skill_id,
                        note=f'Skill {manifest.skill_id} 提供的区域先验（仅影响观察顺序，不是事实结论）',
                    )
                    previous = best.get(region.region_id)
                    if previous is None or hint.score > previous.score:
                        best[region.region_id] = hint
        return tuple(sorted(best.values(), key=lambda hint: (-hint.score, hint.region_id)))

    # ------------------------------------------------------------------ 完成验证
    def verify(self, manifest: SkillManifest, view: PageView, trace: TaskTrace) -> SkillVerification:
        """检查 Skill 的流程完成条件，返回 :class:`SkillVerification`。

        边界（务必保持）：本方法只**读取**轨迹与页面视图，绝不修改证据；
        返回的 ``passed`` 与 ``Evidence.verified`` 没有任何写入关系。即使全部
        检查通过，证据的 ``verified`` 仍然是 ``False``，事实核验必须交给独立
        verifier（本切片只验证「不能置真」这一边界）。
        """
        spec = self._verify_specs.get(manifest.skill_id)
        if spec is None:
            spec = _parse_verify(manifest.skill_dir, manifest.skill_id)
        checks = tuple(self._run_verify_check(check, view, trace) for check in spec['checks'])
        return SkillVerification(
            skill_id=manifest.skill_id,
            passed=all(check['passed'] for check in checks),
            checks=checks,
            at=now_iso(),
        )

    @staticmethod
    def _run_verify_check(check: dict, view: PageView, trace: TaskTrace) -> dict:
        """执行单个流程完成检查；只读视图与轨迹。"""
        kind = check['kind']
        if kind == 'region_present':
            roles = check['roles']
            present = [region.role for region in view.regions]
            missing = [role for role in roles if role not in present]
            return _check_result(
                check,
                passed=not missing,
                expected='页面存在区域角色：' + '、'.join(roles),
                actual='全部存在' if not missing else '缺失：' + '、'.join(missing),
            )
        if kind == 'extraction_non_empty':
            min_chars = int(check.get('min_chars', 1))
            texts = [item.text.strip() for item in trace.evidence]
            usable = [text for text in texts if len(text) >= min_chars]
            return _check_result(
                check,
                passed=bool(usable),
                expected=f'至少一条长度 ≥{min_chars} 的提取证据',
                actual=f'证据 {len(texts)} 条，其中可用 {len(usable)} 条',
            )
        if kind == 'text_pattern':
            patterns = check['patterns']
            texts = [item.text for item in trace.evidence]
            missing = [pattern for pattern in patterns if not any(pattern in text for text in texts)]
            return _check_result(
                check,
                passed=not missing,
                expected='证据文本命中：' + '、'.join(patterns),
                actual='全部命中' if not missing else '缺少模式：' + '、'.join(missing),
            )
        if kind == 'page_not_blocked':
            blocked = view.blocked_reason
            return _check_result(
                check,
                passed=blocked is None,
                expected='页面未被整体阻塞',
                actual='无阻塞' if blocked is None else f'阻塞类型：{blocked}',
            )
        if kind == 'page_version_current':
            versions = [item.page_version for item in trace.evidence]
            stale = [version for version in versions if version != view.page_version]
            return _check_result(
                check,
                passed=bool(versions) and not stale,
                expected='全部证据版本等于当前页面版本',
                actual=(f'证据 {len(versions)} 条，其中旧版本 {len(stale)} 条'
                        if stale else f'证据 {len(versions)} 条，均为当前版本'),
            )
        raise ValueError(f'未知的流程完成检查类型: {kind}')  # pragma: no cover - 加载期已拦截


class SkillHintProvider:
    """把 :class:`SkillRegistry` 适配为 ``model.HintProvider``。

    仅当 ``view.url`` 的主机与构造时给定的 ``host`` 一致时，才查询注册表；
    这样策略拿到的 hint 一定来自「当前场景主机」的合法 Skill。
    """
    name = 'skill_hints'

    def __init__(self, registry: SkillRegistry, host: str) -> None:
        self._registry = registry
        self.host = (host or '').strip().lower()

    def hints(self, view: PageView, goal: ResearchGoal) -> tuple[RegionHint, ...]:
        view_host = (urlparse(view.url).hostname or '').strip().lower()
        if view_host != self.host:
            return ()
        return self._registry.hints(view, goal)


__all__ = [
    'MANIFEST_FILE',
    'SKILL_FILE',
    'SKILL_MANIFEST_SCHEMA',
    'SKILL_VERIFY_SCHEMA',
    'VERIFY_FILE',
    'VERIFY_KINDS',
    'SkillHintProvider',
    'SkillManifest',
    'SkillRegistry',
    'SkillVerification',
]
