"""冻结回放样本上的离线对照评测（机制验证，不是真实网页质量结论）。

评测对象是**合成冻结样本**（``acquisition/fixtures/pages/``），用于回答机制问题：

- 动态观察策略在懒加载、纯视觉海报、布局变化页面上，能否取得固定三屏
  基线取不到的证据；
- 静态页面上动态策略是否更省成本；
- 阻塞页面是否零证据泄漏、登录缺口是否被记录、预算是否受控；
- Skill 门禁与「完成验证不置真」边界是否成立；
- 两次运行是否可复现、样本评测真值是否泄漏给驱动/策略。

对照策略（``POLICIES``）：

- ``fixed_frames``：仅固定三屏 OCR（被替换的截图策略）；
- ``legacy_v3``：``dom:body`` + 固定三屏 OCR（现状 V3 行为）；
- ``adaptive``：动态观察（无 Skill 先验）；
- ``adaptive_skill``：动态观察 + 合法 Skill 区域先验。

结论口径（写入输出 JSON 的 ``conclusion`` 字段）：**只能**表述为「合成冻结
样本上的机制对照」，不能表述为真实网页质量、事实正确性或事实核验能力的提升。
命中冻结样本的 ``expected_answer`` 不等于事实成立；证据的 ``verified`` 始终为
``False``，事实核验属于独立 verifier 的职责，本切片只验证「不被置真」这一边界。

命令行::

    python -m acquisition.evaluate --fixtures acquisition/fixtures/pages [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from . import model
from .driver import FixtureDriver
from .harness import AcquisitionHarness
from .policy import AdaptiveObservationPolicy, FixedFramePolicy, LegacyV3Policy
from .skills import SkillHintProvider, SkillRegistry
from .store import AcquisitionStore

EVAL_SCHEMA = 'acquisition-eval-v0.1'

#: 四种对照策略（顺序即输出顺序）。
POLICIES = ('fixed_frames', 'legacy_v3', 'adaptive', 'adaptive_skill')

#: fixtures/README.md 规定的 7 个必需样本。
REQUIRED_PAGES = (
    'static_article',
    'activity_rules_lazy',
    'poster_image_only',
    'layout_changed_v1',
    'layout_changed_v2',
    'login_comments',
    'captcha_blocked',
)

#: 与 harness 默认预算一致；评测显式传入并检查不超。
EVAL_BUDGET = model.Budget(max_units=24, max_captures=8, max_ocr_calls=8,
                           max_vlm_calls=0, max_steps=30)

#: 结论口径（禁止扩写成真实网页收益/事实核验提升）。
CONCLUSION = (
    '机制结论（仅限合成冻结样本）：动态观察策略在懒加载、纯视觉海报与布局变化样本上'
    '获得固定截图基线取不到的可定位证据，且在静态样本上成本更低；这是机制对照，'
    '不代表真实网页质量，也不构成事实核验或事实正确性提升。'
)

#: 评测真值泄漏探针：替换 expected_* 后，若驱动/策略读取真值，哨兵会出现在轨迹里。
TRUTH_SENTINEL = '__EXPECTED_TRUTH_SENTINEL_9f3c__'
TRUTH_SENTINEL_ROLE = '__truth_role_sentinel__'

_WHITESPACE_RE = re.compile(r'\s+')


# ---------------------------------------------------------------------------
# 读取样本与构造任务
# ---------------------------------------------------------------------------
def load_fixture(page_path) -> dict:
    """读取一个页面样本 JSON（仅评测侧使用，可读取 expected_* 真值）。"""
    return json.loads(Path(page_path).read_text(encoding='utf-8'))


def _fixture_paths(fixtures_dir) -> list[Path]:
    return sorted(Path(fixtures_dir).glob('*.json'), key=lambda path: path.name)


def _skills_root(fixtures_dir) -> Path:
    return Path(fixtures_dir).resolve().parent / 'skills'


def _load_registry(fixtures_dir) -> SkillRegistry:
    """加载与 ``pages/`` 同级的 ``skills/``；缺失时退化为空注册表。"""
    root = _skills_root(fixtures_dir)
    try:
        return SkillRegistry.load(root)
    except FileNotFoundError:
        return SkillRegistry()


def goal_from_fixture(page: dict) -> tuple[model.ResearchGoal, str]:
    """把样本 objectives 映射为冻结契约里的 ResearchGoal（忽略 expected_* 真值）。"""
    host = (urlparse(page['url']).hostname or '').strip().lower()
    objectives = tuple(
        model.Objective(
            objective_id=raw['objective_id'],
            question=raw['question'],
            answer_kind=raw['answer_kind'],
            region_hints=tuple(raw['region_hints']),
            required=bool(raw['required']),
        )
        for raw in page['objectives']
    )
    goal = model.ResearchGoal(
        task_id=f"eval::{page['page_id']}",
        question=page.get('title') or page.get('label') or page['page_id'],
        objectives=objectives,
        allowed_hosts=(host,) if host else (),
    )
    return goal, host


def _make_policy(policy_name: str, *, registry: SkillRegistry, host: str):
    if policy_name == 'fixed_frames':
        return FixedFramePolicy()
    if policy_name == 'legacy_v3':
        return LegacyV3Policy()
    if policy_name == 'adaptive':
        return AdaptiveObservationPolicy()
    if policy_name == 'adaptive_skill':
        return AdaptiveObservationPolicy(hint_provider=SkillHintProvider(registry, host))
    raise ValueError(f'未知评测策略: {policy_name}')


def _budget_dict(budget: model.Budget = EVAL_BUDGET) -> dict:
    return {
        'max_units': budget.max_units,
        'max_captures': budget.max_captures,
        'max_ocr_calls': budget.max_ocr_calls,
        'max_vlm_calls': budget.max_vlm_calls,
        'max_steps': budget.max_steps,
    }


# ---------------------------------------------------------------------------
# 单次运行与指标
# ---------------------------------------------------------------------------
def _normalise(text: str) -> str:
    """去空白后比较，避免 OCR 转录的换行/空格影响「命中预期文本」判断。"""
    return _WHITESPACE_RE.sub('', text or '')


def _region_roles(page: dict) -> dict[str, str]:
    return {raw['region_id']: raw['role'] for raw in page['regions']}


def _objective_hit(page: dict, trace: model.TaskTrace, raw_objective: dict) -> dict:
    """单个 objective 的命中判断。

    ``answer_hit``：证据文本包含冻结样本的 expected_answer（≠ 事实成立）；
    ``located_hit``：命中的证据还要能定位到 expected_region_role 对应的语义区域。
    虚拟区域（dom:body / viewport:<i>）没有语义区域角色，因此不算可定位。
    """
    expected = _normalise(raw_objective['expected_answer'])
    expected_role = raw_objective.get('expected_region_role')
    roles = _region_roles(page)
    matched = [item for item in trace.evidence if expected and expected in _normalise(item.text)]
    return {
        'objective_id': raw_objective['objective_id'],
        'required': bool(raw_objective['required']),
        'answer_hit': bool(matched),
        'located_hit': any(roles.get(item.region) == expected_role for item in matched),
    }


def _metrics_from_trace(page: dict, policy_name: str, trace: model.TaskTrace) -> dict:
    objective_hits = [_objective_hit(page, trace, raw) for raw in page['objectives']]
    required_hits = [item for item in objective_hits if item['required']]
    answer_hit = bool(required_hits) and all(item['answer_hit'] for item in required_hits)
    located_hit = bool(required_hits) and all(item['located_hit'] for item in required_hits)
    blocked = page.get('blocked_reason')
    cost = trace.cost
    within_budget = (
        cost.units <= EVAL_BUDGET.max_units
        and cost.captures <= EVAL_BUDGET.max_captures
        and cost.ocr_calls <= EVAL_BUDGET.max_ocr_calls
        and cost.vlm_calls <= EVAL_BUDGET.max_vlm_calls
        and trace.steps <= EVAL_BUDGET.max_steps
    )
    return {
        'task_id': trace.task_id,
        'page_id': page['page_id'],
        'policy': policy_name,
        'answer_hit': answer_hit,
        'located_hit': located_hit,
        'evidence_count': len(trace.evidence),
        'captures': cost.captures,
        'ocr_calls': cost.ocr_calls,
        'vlm_calls': cost.vlm_calls,
        'units': cost.units,
        'steps': trace.steps,
        'stop_reason': trace.stop_reason,
        'gap': trace.gap,
        'status': trace.status,
        'evidence_verified': [item.verified for item in trace.evidence],
        'blocked_evidence_leak': bool(blocked) and bool(trace.evidence),
        'within_budget': within_budget,
        'evidence_fingerprints': [item.fingerprint for item in trace.evidence],
        'objective_hits': objective_hits,
    }


def _run_page(page_path: Path, page: dict, policy_name: str, *,
              registry: SkillRegistry, store_path=None) -> dict:
    """用指定策略在该样本上跑一次 AcquisitionHarness，返回 {metrics, trace}。"""
    goal, host = goal_from_fixture(page)
    policy = _make_policy(policy_name, registry=registry, host=host)
    driver = FixtureDriver.from_file(page_path)
    temporary_dir = None
    if store_path is None:
        temporary_dir = tempfile.mkdtemp(prefix='acq_eval_store_')
        store_path = Path(temporary_dir) / 'store.sqlite3'
    store = AcquisitionStore(Path(store_path))
    try:
        harness = AcquisitionHarness(driver, policy, store, budgets=EVAL_BUDGET)
        trace = harness.run(goal)
    finally:
        try:
            store.close()
        finally:
            driver.close()
            if temporary_dir is not None:
                shutil.rmtree(temporary_dir, ignore_errors=True)
    return {'metrics': _metrics_from_trace(page, policy_name, trace), 'trace': trace}


def run_fixture(page_path, policy_name: str, *, store_path=None) -> dict:
    """在单个冻结样本上跑一种策略，返回该次运行的指标字典。

    ``store_path`` 为空时使用临时 SQLite 文件并在结束后清理，保证离线、可复现。
    """
    if policy_name not in POLICIES:
        raise ValueError(f'未知评测策略: {policy_name}')
    page_path = Path(page_path)
    page = load_fixture(page_path)
    registry = _load_registry(page_path.parent)
    return _run_page(page_path, page, policy_name, registry=registry,
                     store_path=store_path)['metrics']


def _open_view(page_path: Path, page: dict) -> model.PageView:
    """打开一次回放驱动取页面视图（只用于 Skill 门禁/完成验证检查）。"""
    goal, _host = goal_from_fixture(page)
    driver = FixtureDriver.from_file(page_path)
    try:
        return driver.open(goal)
    finally:
        driver.close()


# ---------------------------------------------------------------------------
# 汇总检查项
# ---------------------------------------------------------------------------
def _check(check_id: str, passed: bool, expected: str, actual: str, detail: str = '') -> dict:
    return {'id': check_id, 'passed': bool(passed), 'expected': expected,
            'actual': actual, 'detail': detail}


def _names(page_ids) -> str:
    return '、'.join(page_ids) if page_ids else '无'


def _check_adaptive_not_below_fixed(page_runs: dict) -> dict:
    violations = []
    for page_id, runs in page_runs.items():
        for policy in ('adaptive', 'adaptive_skill'):
            if runs[policy]['metrics']['answer_hit'] < runs['fixed_frames']['metrics']['answer_hit']:
                violations.append(f'{page_id}/{policy}')
    actual = '所有页面 adaptive 与 adaptive_skill ≥ fixed_frames' if not violations \
        else f'低于基线：{_names(violations)}'
    return _check('adaptive_answer_not_below_fixed_frames', not violations,
                  '每个样本 adaptive/adaptive_skill.answer_hit ≥ fixed_frames.answer_hit', actual)


def _check_dynamic_not_below_legacy(page_runs: dict) -> dict:
    violations = []
    for page_id, runs in page_runs.items():
        for policy in ('adaptive', 'adaptive_skill'):
            if runs[policy]['metrics']['answer_hit'] < runs['legacy_v3']['metrics']['answer_hit']:
                violations.append(f'{page_id}/{policy}')
    actual = '所有页面 adaptive 与 adaptive_skill ≥ legacy_v3' if not violations \
        else f'低于基线：{_names(violations)}'
    return _check('dynamic_answer_not_below_legacy_v3', not violations,
                  '每个样本 adaptive/adaptive_skill.answer_hit ≥ legacy_v3.answer_hit', actual)


def _check_adaptive_beats_baselines(page_runs: dict, page_id: str) -> dict:
    adaptive = page_runs[page_id]['adaptive']['metrics']
    adaptive_skill = page_runs[page_id]['adaptive_skill']['metrics']
    fixed = page_runs[page_id]['fixed_frames']['metrics']
    legacy = page_runs[page_id]['legacy_v3']['metrics']
    passed = (adaptive['answer_hit'] and adaptive_skill['answer_hit']
              and not fixed['answer_hit'] and not legacy['answer_hit'])
    actual = (f"adaptive={adaptive['answer_hit']}, adaptive_skill={adaptive_skill['answer_hit']}, "
              f"fixed_frames={fixed['answer_hit']}, legacy_v3={legacy['answer_hit']}")
    return _check(f'adaptive_beats_baselines_on_{page_id}', passed,
                  'adaptive 与 adaptive_skill 命中；fixed_frames 与 legacy_v3 均不命中', actual)


def _check_located(page_runs: dict, page_id: str, check_id: str) -> dict:
    adaptive = page_runs[page_id]['adaptive']['metrics']
    hit = adaptive['answer_hit'] and adaptive['located_hit']
    roles = [objective.get('expected_region_role', '')
             for objective in page_runs[page_id]['page']['objectives']]
    actual = f"answer_hit={adaptive['answer_hit']}, located_hit={adaptive['located_hit']}"
    return _check(check_id, hit, 'adaptive 命中且证据可定位到语义区域（role 相符）',
                  actual, detail=f'期望区域角色：{_names(roles)}')


def _check_static_adaptive_units(page_runs: dict) -> dict:
    adaptive = page_runs['static_article']['adaptive']['metrics']
    legacy = page_runs['static_article']['legacy_v3']['metrics']
    passed = adaptive['units'] < legacy['units']
    return _check('adaptive_units_below_legacy_on_static', passed,
                  'static_article 上 adaptive.units < legacy_v3.units',
                  f"adaptive {adaptive['units']} vs legacy_v3 {legacy['units']}",
                  detail='units 是资源约束指标，不作为质量评分')


def _check_no_leak(page_runs: dict) -> dict:
    leaks = [f'{page_id}/{policy}'
             for page_id, runs in page_runs.items()
             for policy in POLICIES
             if runs[policy]['metrics']['blocked_evidence_leak']]
    return _check('no_blocked_evidence_leak', not leaks,
                  '阻塞页面任何策略都不入库证据（blocked_evidence_leak=0）',
                  '全部为 0' if not leaks else '泄漏：' + _names(leaks))


def _check_blocked_page_no_evidence(page_runs: dict) -> dict:
    runs = page_runs['captcha_blocked']
    offenders = [policy for policy in POLICIES
                 if runs[policy]['metrics']['evidence_count']]
    return _check('blocked_page_has_no_evidence', not offenders,
                  'captcha_blocked 上四种策略证据数均为 0',
                  '全部为 0' if not offenders else '有证据：' + _names(offenders),
                  detail='固定基线仍可能消耗截图成本，但证据不得入库')


def _check_login_gap(page_runs: dict) -> dict:
    outcome = page_runs['login_comments']['adaptive']
    metrics = outcome['metrics']
    trace = outcome['trace']
    blocked_receipt = any(receipt.status == 'blocked' for receipt in trace.receipts)
    recorded = (
        metrics['stop_reason'] == 'login_required'
        or metrics['gap'] == 'login_required'
        or any(receipt.status == 'blocked' and 'login' in (receipt.note or receipt.error or '').lower()
               for receipt in trace.receipts)
    )
    leaked = any(_normalise(objective['expected_answer']) in _normalise(item.text)
                 for objective in page_runs['login_comments']['page']['objectives']
                 for item in trace.evidence)
    actual = (f"stop_reason={metrics['stop_reason']}, gap={metrics['gap']}, "
              f"blocked_receipt={blocked_receipt}, 评论证据泄漏={leaked}")
    return _check('login_gap_recorded', recorded and not leaked,
                  'adaptive 在登录评论区记录 login_required 缺口并停止，不产出评论证据', actual)


def _check_budgets(page_runs: dict) -> dict:
    over = [f'{page_id}/{policy}'
            for page_id, runs in page_runs.items()
            for policy in POLICIES
            if not runs[policy]['metrics']['within_budget']]
    return _check('budgets_respected', not over,
                  '所有运行 units/captures/ocr/vlm/steps 不超预算',
                  '全部在预算内' if not over else '超预算：' + _names(over),
                  detail=json.dumps(_budget_dict(), ensure_ascii=False))


def _check_skill_gates(page_runs: dict, fixtures_dir) -> list[dict]:
    """Skill 门禁：合法页面可用、错误主机/结构变化被拒。"""
    registry = _load_registry(fixtures_dir)
    pages_dir = Path(fixtures_dir)
    activity_ids = ('layout_changed_v1', 'layout_changed_v2')
    eligible_ok = True
    details = []
    for page_id in activity_ids:
        page = page_runs[page_id]['page']
        view = _open_view(pages_dir / f'{page_id}.json', page)
        host = (urlparse(page['url']).hostname or '').lower()
        manifests = registry.eligible(view, host=host)
        ok = len(manifests) == 1 and manifests[0].skill_id == 'taptap-activity-rules'
        eligible_ok = eligible_ok and ok
        details.append(f'{page_id}:{ok}')
    checks = [_check('skill_gate_eligible_on_activity_pages', eligible_ok,
                     '活动结构样本上合法 Skill 可用（host/签名/权限均满足）',
                     '；'.join(details))]

    layout_page = page_runs['layout_changed_v1']['page']
    layout_view = _open_view(pages_dir / 'layout_changed_v1.json', layout_page)
    wrong_host = registry.eligible(layout_view, host='evil.example')
    checks.append(_check('skill_gate_rejects_wrong_host', not wrong_host,
                         'allowed_hosts 之外的页面必须不可用',
                         '拒绝' if not wrong_host else '错误地可用'))

    poster_page = page_runs['poster_image_only']['page']
    poster_view = _open_view(pages_dir / 'poster_image_only.json', poster_page)
    host = (urlparse(poster_page['url']).hostname or '').lower()
    changed = registry.eligible(poster_view, host=host)
    checks.append(_check('skill_gate_rejects_changed_signature', not changed,
                         '结构签名不匹配的页面必须不可用',
                         '拒绝' if not changed else '错误地可用'))
    return checks


def _check_skill_verification(page_runs: dict, fixtures_dir) -> dict:
    """Skill 完成验证可以为 passed，但绝不能把 Evidence.verified 置真。"""
    registry = _load_registry(fixtures_dir)
    page = page_runs['static_article']['page']
    page_path = Path(fixtures_dir) / 'static_article.json'
    view = _open_view(page_path, page)
    host = (urlparse(page['url']).hostname or '').lower()
    manifests = registry.eligible(view, host=host)
    if not manifests:
        return _check('skill_verification_does_not_set_verified', False,
                      'SkillVerification.passed 可成立，但 evidence.verified 必须全为 False',
                      '合法 Skill 在 static_article 上不可用，无法执行完成验证')
    trace = page_runs['static_article']['adaptive_skill']['trace']
    verification = registry.verify(manifests[0], view, trace)
    verified_count = sum(1 for item in trace.evidence if item.verified)
    passed = verification.passed and verified_count == 0
    return _check('skill_verification_does_not_set_verified', passed,
                  'SkillVerification.passed=True 且 Evidence.verified 全为 False',
                  f'verification.passed={verification.passed}, verified_count={verified_count}',
                  detail='完成验证只证明流程完成；事实核验必须交给独立 verifier')


def _check_determinism(page_runs: dict, fixtures_dir) -> dict:
    """两次独立运行（新 store）证据指纹必须一致。"""
    samples = (('layout_changed_v2', 'adaptive'), ('poster_image_only', 'adaptive_skill'))
    mismatch = []
    details = []
    for page_id, policy in samples:
        fixture_path = Path(fixtures_dir) / f'{page_id}.json'
        page = page_runs[page_id]['page']
        registry = _load_registry(fixtures_dir)
        first = page_runs[page_id][policy]['metrics']['evidence_fingerprints']
        second = _run_page(fixture_path, page, policy, registry=registry)['metrics']['evidence_fingerprints']
        details.append(f'{page_id}/{policy}:{len(first)} 条')
        if first != second:
            mismatch.append(f'{page_id}/{policy}')
    return _check('fingerprints_deterministic', not mismatch,
                  '两次独立运行的证据指纹列表完全一致',
                  '一致（' + '；'.join(details) + '）' if not mismatch
                  else '不一致：' + _names(mismatch))


def _check_truth_not_leaked(page_runs: dict, fixtures_dir) -> dict:
    """把 expected_* 换成哨兵后重跑，驱动/策略/轨迹里不得出现哨兵。"""
    source = Path(fixtures_dir) / 'static_article.json'
    if not source.is_file():
        return _check('fixture_truth_not_leaked', False,
                      'expected_* 不得进入驱动 PageView / 轨迹', '缺少 static_article 样本')
    page = load_fixture(source)
    for objective in page['objectives']:
        objective['expected_answer'] = TRUTH_SENTINEL
        objective['expected_region_role'] = TRUTH_SENTINEL_ROLE
    workdir = tempfile.mkdtemp(prefix='acq_truth_probe_')
    try:
        poisoned_path = Path(workdir) / 'static_article.json'
        poisoned_path.write_text(json.dumps(page, ensure_ascii=False, indent=2), encoding='utf-8')
        outcome = _run_page(poisoned_path, page, 'adaptive', registry=SkillRegistry())
        trace_blob = model.canonical(outcome['trace'].as_dict())
        view = _open_view(poisoned_path, page)
        view_blob = model.canonical({
            'url': view.url,
            'title': view.title,
            'dom_text': view.dom_text,
            'regions': [{'region_id': region.region_id, 'role': region.role,
                         'text': region.text, 'visual_text': region.visual_text,
                         'requires': region.requires}
                        for region in view.regions],
        })
        leaked_in_trace = TRUTH_SENTINEL in trace_blob or TRUTH_SENTINEL_ROLE in trace_blob
        leaked_in_view = TRUTH_SENTINEL in view_blob or TRUTH_SENTINEL_ROLE in view_blob
        hit_sentinel = outcome['metrics']['answer_hit']
        passed = not (leaked_in_trace or leaked_in_view or hit_sentinel)
        return _check('fixture_truth_not_leaked', passed,
                      'expected_* 不得进入驱动 PageView / 轨迹 / 命中判断',
                      '未泄漏' if passed else
                      f'泄漏：trace={leaked_in_trace}, view={leaked_in_view}, answer_hit={hit_sentinel}',
                      detail='哨兵法：把 expected_answer/expected_region_role 替换为哨兵后重跑')
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _check_pages_complete(page_runs: dict) -> dict:
    present = set(page_runs)
    missing = [page_id for page_id in REQUIRED_PAGES if page_id not in present]
    extra = sorted(present - set(REQUIRED_PAGES))
    passed = not missing and not extra
    actual = '7 个必需样本齐全' if passed else (
        ('缺少：' + _names(missing) if missing else '') +
        ('；多余：' + _names(extra) if extra else ''))
    return _check('fixture_pages_complete', passed,
                  f'样本目录恰好包含 7 个必需页面：{_names(REQUIRED_PAGES)}', actual)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def _markdown_table(result: dict) -> str:
    lines = ['| 页面 | 策略 | answer_hit | located_hit | 证据 | captures | ocr | units | steps | stop_reason | gap | 泄漏 |',
             '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for page in result['pages']:
        for policy_name in POLICIES:
            run = page['runs'][policy_name]
            lines.append(
                f"| {page['page_id']} | {policy_name} | {str(run['answer_hit']).lower()} | "
                f"{str(run['located_hit']).lower()} | {run['evidence_count']} | {run['captures']} | "
                f"{run['ocr_calls']} | {run['units']} | {run['steps']} | {run['stop_reason']} | "
                f"{run['gap'] or '-'} | {str(run['blocked_evidence_leak']).lower()} |")
    lines.append('')
    lines.append('| check | passed | expected | actual |')
    lines.append('| --- | --- | --- | --- |')
    for check in result['checks']:
        lines.append(f"| {check['id']} | {str(check['passed']).lower()} | "
                     f"{check['expected']} | {check['actual']} |")
    return '\n'.join(lines)


def _print_report(result: dict) -> None:
    print('# V4 Acquisition 切片离线对照（合成冻结样本）')
    print()
    print(f"样本目录：`{result['fixtures_dir']}`；契约：`{result['contract']}`；"
          f"策略：{', '.join(result['policies'])}")
    print()
    print(_markdown_table(result))
    print()
    summary = result['summary']
    print(f"checks：{summary['checks_passed']}/{summary['checks_total']} 通过"
          f"（all_passed={summary['all_passed']}）")
    print()
    print(f"结论：{result['conclusion']}")


def _write_json(result: dict, out) -> None:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def evaluate(fixtures_dir, *, out=None) -> dict:
    """在 ``fixtures_dir`` 的全部冻结页面上跑四种策略，输出指标与检查项。"""
    fixtures_dir = Path(fixtures_dir)
    if not fixtures_dir.is_dir():
        raise FileNotFoundError(f'样本目录不存在: {fixtures_dir}')
    page_paths = _fixture_paths(fixtures_dir)
    if not page_paths:
        raise FileNotFoundError(f'样本目录中没有 JSON 页面: {fixtures_dir}')
    registry = _load_registry(fixtures_dir)

    page_runs: dict[str, dict] = {}
    pages_out: list[dict] = []
    workdir = tempfile.mkdtemp(prefix='acq_eval_matrix_')
    try:
        for page_path in page_paths:
            page = load_fixture(page_path)
            page_id = page['page_id']
            if page_id in page_runs:
                raise ValueError(f'页面 page_id 重复: {page_id}')
            runs: dict[str, dict] = {}
            for policy_name in POLICIES:
                store_path = Path(workdir) / f'{page_id}__{policy_name}.sqlite3'
                runs[policy_name] = _run_page(page_path, page, policy_name,
                                              registry=registry, store_path=store_path)
            page_runs[page_id] = {'page': page, **runs}
            pages_out.append({
                'page_id': page_id,
                'label': page.get('label', ''),
                'url': page['url'],
                'blocked_reason': page.get('blocked_reason'),
                'objectives': [
                    {'objective_id': raw['objective_id'],
                     'question': raw['question'],
                     'answer_kind': raw['answer_kind'],
                     'region_hints': list(raw['region_hints']),
                     'required': bool(raw['required'])}
                    for raw in page['objectives']
                ],
                'runs': {policy_name: runs[policy_name]['metrics']
                         for policy_name in POLICIES},
            })
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    checks: list[dict] = [_check_pages_complete(page_runs),
                          _check_adaptive_not_below_fixed(page_runs),
                          _check_dynamic_not_below_legacy(page_runs)]
    checks.extend([
        _check_adaptive_beats_baselines(page_runs, 'activity_rules_lazy'),
        _check_adaptive_beats_baselines(page_runs, 'poster_image_only'),
        _check_adaptive_beats_baselines(page_runs, 'layout_changed_v1'),
        _check_adaptive_beats_baselines(page_runs, 'layout_changed_v2'),
        _check_located(page_runs, 'static_article', 'static_adaptive_located_hit'),
        _check_located(page_runs, 'activity_rules_lazy', 'lazy_adaptive_located_hit'),
        _check_located(page_runs, 'poster_image_only', 'poster_adaptive_located_hit'),
        _check_located(page_runs, 'layout_changed_v1', 'layout_v1_adaptive_located_hit'),
        _check_located(page_runs, 'layout_changed_v2', 'layout_v2_adaptive_located_hit'),
        _check_static_adaptive_units(page_runs),
        _check_no_leak(page_runs),
        _check_blocked_page_no_evidence(page_runs),
        _check_login_gap(page_runs),
        _check_budgets(page_runs),
    ])
    checks.extend(_check_skill_gates(page_runs, fixtures_dir))
    checks.append(_check_skill_verification(page_runs, fixtures_dir))
    checks.append(_check_determinism(page_runs, fixtures_dir))
    checks.append(_check_truth_not_leaked(page_runs, fixtures_dir))

    summary = {
        'checks_total': len(checks),
        'checks_passed': sum(1 for check in checks if check['passed']),
        'all_passed': all(check['passed'] for check in checks),
    }
    result = {
        'schema': EVAL_SCHEMA,
        'contract': model.SCHEMA_VERSION,
        'generated_at': model.now_iso(),
        'fixtures_dir': str(fixtures_dir),
        'policies': list(POLICIES),
        'budgets': _budget_dict(),
        'pages': pages_out,
        'checks': checks,
        'summary': summary,
        'conclusion': CONCLUSION,
    }
    if out is not None:
        _write_json(result, out)
    _print_report(result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='V4 Acquisition 切片离线对照评测（合成冻结样本，机制验证）')
    parser.add_argument('--fixtures', default='acquisition/fixtures/pages',
                        help='页面样本目录（默认 acquisition/fixtures/pages）')
    parser.add_argument('--out', default=None,
                        help='可选：把完整 JSON 结果写入文件')
    args = parser.parse_args(argv)
    result = evaluate(Path(args.fixtures), out=Path(args.out) if args.out else None)
    return 0 if result['summary']['all_passed'] else 1


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
