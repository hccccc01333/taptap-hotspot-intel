"""命令行：在冻结回放样本上跑一次 Acquisition Harness（离线、不联网）。

用法示例::

    python -X utf8 -m acquisition.cli --page acquisition/fixtures/pages/static_article.json
    python -X utf8 -m acquisition.cli --page acquisition/fixtures/pages/activity_rules_lazy.json \\
        --policy adaptive --db .tmp/acq-demo.db --json

注意：本命令只回放 ``acquisition/fixtures/pages/*.json`` 合成样本，用于机制
对照；它不访问真实网页，也不代表真实站点上的取证质量。真值字段
（``expected_answer`` / ``expected_region_role``）只在评测脚本里比对，
本命令构造目标时不会读取。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import model
from .driver import FixtureDriver
from .harness import AcquisitionHarness
from .store import AcquisitionStore


def goal_from_page(page: dict, *, task_id: str | None = None) -> model.ResearchGoal:
    """从页面样本构造研究目标；只复制非真值字段（不含 expected_*）。"""
    raw_objectives = page.get('objectives') or []
    objectives = []
    for item in raw_objectives:
        objectives.append(model.Objective(
            objective_id=str(item.get('objective_id') or 'objective'),
            question=str(item.get('question') or ''),
            answer_kind=str(item.get('answer_kind') or 'text'),
            region_hints=tuple(item.get('region_hints') or ()),
            required=bool(item.get('required', True)),
        ))
    if not objectives:
        objectives.append(model.Objective(objective_id='general', question='页面主要内容是什么'))
    question = str(page.get('question') or page.get('label') or objectives[0].question)
    host = urlparse(str(page.get('url') or '')).hostname or ''
    return model.ResearchGoal(task_id=task_id or str(page.get('page_id') or 'fixture_task'),
                              question=question, objectives=tuple(objectives),
                              allowed_hosts=(host,) if host else ())


def _load_policy(name: str, page: dict):
    """按名字加载策略；策略模块由 acq-policy 提供，缺失时给出明确错误。"""
    try:
        from . import policy as policy_module
    except ImportError as exc:  # pragma: no cover - 依赖并行开发进度
        raise RuntimeError(f'策略模块 acquisition.policy 尚不可用: {exc}') from exc
    if name == 'fixed_frames':
        return policy_module.FixedFramePolicy()
    if name == 'legacy_v3':
        return policy_module.LegacyV3Policy()
    if name == 'adaptive':
        return policy_module.AdaptiveObservationPolicy()
    if name == 'adaptive_skill':
        try:
            from . import skills as skills_module
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(f'Skill 模块 acquisition.skills 尚不可用: {exc}') from exc
        root = Path(__file__).resolve().parent / 'fixtures' / 'skills'
        registry = skills_module.SkillRegistry.load(root)
        host = urlparse(str(page.get('url') or '')).hostname or ''
        provider = skills_module.SkillHintProvider(registry, host)
        return policy_module.AdaptiveObservationPolicy(hint_provider=provider)
    raise ValueError(f'未知策略: {name!r}')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m acquisition.cli',
        description='在冻结回放样本上运行一次 Acquisition Harness（离线机制验证）')
    parser.add_argument('--page', required=True, help='页面样本 JSON 路径')
    parser.add_argument('--policy', default='adaptive',
                        choices=('fixed_frames', 'legacy_v3', 'adaptive', 'adaptive_skill'),
                        help='观察策略（默认 adaptive）')
    parser.add_argument('--db', default=':memory:', help='SQLite 路径（默认内存库）')
    parser.add_argument('--resume', action='store_true', help='恢复最近一次同 goal 的任务')
    parser.add_argument('--json', action='store_true', help='输出 TaskTrace JSON')
    parser.add_argument('--max-units', type=int, default=None, help='覆盖预算：单位上限')
    parser.add_argument('--max-captures', type=int, default=None, help='覆盖预算：截图上限')
    parser.add_argument('--max-steps', type=int, default=None, help='覆盖预算：步数上限')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    page_path = Path(args.page)
    if not page_path.is_file():
        print(f'页面样本不存在: {page_path}', file=sys.stderr)
        return 2
    try:
        page = json.loads(page_path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        print(f'页面样本读取失败: {exc}', file=sys.stderr)
        return 2
    if not isinstance(page, dict):
        print('页面样本必须是 JSON 对象', file=sys.stderr)
        return 2

    budgets = model.Budget()
    overrides = {}
    if args.max_units is not None:
        overrides['max_units'] = args.max_units
    if args.max_captures is not None:
        overrides['max_captures'] = args.max_captures
    if args.max_steps is not None:
        overrides['max_steps'] = args.max_steps
    if overrides:
        budgets = model.Budget(**overrides)

    try:
        policy = _load_policy(args.policy, page)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    driver = FixtureDriver(page)
    goal = goal_from_page(page)
    with AcquisitionStore(args.db) as store:
        harness = AcquisitionHarness(driver, policy, store, budgets=budgets)
        trace = harness.run(goal, resume=args.resume)
        payload = trace.as_dict()
        verification = store.verifications(trace.task_id)
        payload['verifications'] = [
            {'verification_id': item.verification_id, 'capture_id': item.capture_id,
             'status': item.status, 'verifier': item.verifier} for item in verification]
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            coverage = trace.coverage(goal)
            all_unverified = all(not item.verified for item in trace.evidence)
            print(f'任务: {trace.task_id}  策略: {trace.policy}  后端: {trace.backend}')
            print(f'状态: {trace.status}  停止原因: {trace.stop_reason}  缺口: {trace.gap}')
            print(f'步数: {trace.steps}  单位: {trace.cost.units}  截图: {trace.cost.captures} '
                  f'OCR: {trace.cost.ocr_calls}  VLM: {trace.cost.vlm_calls}')
            print(f'证据: {len(trace.evidence)} 条（全部未核验={all_unverified}）  '
                  f'覆盖: {coverage[0]}/{coverage[1]}')
            print(f'页面版本: {len(trace.page_versions)} 个')
            for receipt in trace.receipts:
                print(f'  - {receipt.action:<8} {receipt.status:<15} '
                      f'region={receipt.region} method={receipt.method} '
                      f'next={receipt.next} cost={receipt.cost.as_dict()}')
    return 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
