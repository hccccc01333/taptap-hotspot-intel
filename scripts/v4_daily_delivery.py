"""Daily incremental V4 delivery runner.

Selects recent, unseen TapTap discovery moments, copies their existing research
packs into an isolated staging directory, then merges new deliveries into the
shared output. The state ledger makes reruns idempotent and retains failures.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context.delivery_review import record_review  # noqa: E402
from context.reference_library import load_library  # noqa: E402
from scripts.v4_delivery_pack import build_pack  # noqa: E402

CST = timezone(timedelta(hours=8))


def _moment_id(url: str) -> str:
    match = re.search(r'/moment/(\d+)', str(url or ''))
    return match.group(1) if match else ''


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def select_candidates(csv_path: str | Path, out_dir: str | Path,
                      state_path: str | Path, *, now: datetime | None = None,
                      days: int = 7, limit: int = 5) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Return unseen recent moments, newest first, and the current ledger."""
    now = now or datetime.now(CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    state = _read_json(Path(state_path), {'schema': 'v4-daily-state-v0.1',
                                          'delivered': {}, 'failed': {}})
    with Path(csv_path).open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    cutoff = now - timedelta(days=days)
    candidates = []
    for row in rows:
        moment_id = row.get('moment_id') or ''
        if not moment_id or moment_id in state['delivered']:
            continue
        try:
            published = datetime.fromtimestamp(int(row.get('publish_time') or 0), tz=CST)
        except (ValueError, OSError):
            continue
        if not cutoff <= published <= now:
            continue
        if len((row.get('summary') or '').strip()) < 40:
            continue
        candidates.append(row)
    candidates.sort(key=lambda row: int(row.get('publish_time') or 0), reverse=True)
    return candidates[:max(1, limit)], state


def run_daily(discover_csv: str | Path, pack_root: str | Path, out_dir: str | Path,
              state_path: str | Path, references: str | Path | None = None, *,
              now: datetime | None = None, days: int = 7, limit: int = 5,
              creative_generator=None) -> dict[str, Any]:
    out_dir, pack_root, state_path = Path(out_dir), Path(pack_root), Path(state_path)
    if not pack_root.is_dir():
        raise FileNotFoundError(f'research pack root not found: {pack_root}')
    index = _read_json(out_dir / 'index.json', {'deliveries': []})
    initial_state = _read_json(state_path, {'schema': 'v4-daily-state-v0.1',
                                            'delivered': {}, 'failed': {}})
    for item in index.get('deliveries') or []:
        package = _read_json(out_dir / item['event_id'] / 'delivery.json', {})
        moment_id = _moment_id((package.get('event') or {}).get('url'))
        if moment_id:
            initial_state['delivered'].setdefault(moment_id, {
                'event_id': item['event_id'], 'delivered_at': package.get('review', {}).get('reviewed_at')})
    _write_json(state_path, initial_state)
    candidates, state = select_candidates(discover_csv, out_dir, state_path,
                                          now=now, days=days, limit=limit)
    run_at = (now or datetime.now(CST)).astimezone(CST).isoformat()
    report: dict[str, Any] = {'schema': 'v4-daily-run-v0.1', 'run_at': run_at,
                              'candidates': len(candidates), 'delivered': [],
                              'skipped': [], 'failed': []}
    with Path(discover_csv).open(encoding='utf-8-sig', newline='') as handle:
        observed = list(csv.DictReader(handle))
    seen = set()
    for row in observed:
        moment_id = row.get('moment_id') or ''
        if moment_id in state['delivered'] and moment_id not in seen:
            seen.add(moment_id)
            report['skipped'].append({'moment_id': moment_id, 'title': row.get('title'),
                                      'event_id': state['delivered'][moment_id].get('event_id'),
                                      'reason': 'already_delivered'})
    if not candidates:
        report['note'] = '没有符合条件的新事件；未修改交付索引。'
        state['updated_at'] = run_at
        _write_json(state_path, state)
        _write_json(out_dir / 'daily-last-run.json', report)
        return report
    staging = out_dir / '.daily-staging'
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    selected = []
    for row in candidates:
        moment_id = row['moment_id']
        folder = next((path for path in pack_root.iterdir()
                       if path.is_dir() and (path / 'evidence.json').is_file() and
                       _moment_id(_read_json(path / 'evidence.json', {}).get(
                           'event', {}).get('url')) == moment_id), None)
        if folder is None:
            failure = {'moment_id': moment_id, 'title': row.get('title'),
                       'error': 'research pack missing'}
            report['failed'].append(failure)
            previous = state['failed'].get(moment_id) or {}
            state['failed'][moment_id] = {**failure, 'attempts': previous.get('attempts', 0) + 1,
                                          'last_attempt_at': run_at}
            continue
        selected.append({'moment_id': moment_id, 'event_id': folder.name})
        shutil.copytree(folder, staging / folder.name)
    _write_json(staging / 'summary.json', {'events': selected})
    if selected:
        library = load_library(references) if references else None
        result = build_pack(staging, out_dir, reference_library=library,
                            creative_generator=creative_generator)
        successful = {item['event_id'] for item in result['events']
                      if item.get('preview') and item.get('checks_passed')}
        for item in selected:
            if item['event_id'] in successful:
                state['delivered'][item['moment_id']] = {
                    'event_id': item['event_id'], 'delivered_at': run_at}
                state['failed'].pop(item['moment_id'], None)
                report['delivered'].append(item)
            else:
                failure = {'moment_id': item['moment_id'], 'event_id': item['event_id'],
                           'error': 'delivery checks failed'}
                report['failed'].append(failure)
                previous = state['failed'].get(item['moment_id']) or {}
                state['failed'][item['moment_id']] = {
                    **failure, 'attempts': previous.get('attempts', 0) + 1,
                    'last_attempt_at': run_at}
    shutil.rmtree(staging)
    state['updated_at'] = run_at
    _write_json(state_path, state)
    _write_json(out_dir / 'daily-last-run.json', report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='V4 每日增量交付与人工审核')
    parser.add_argument('--discover-csv')
    parser.add_argument('--pack-root')
    parser.add_argument('--out', default='data/v4/deliveries')
    parser.add_argument('--state', default='data/v4/daily-state.json')
    parser.add_argument('--references')
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--review-event')
    parser.add_argument('--review-status', choices=('draft', 'needs_review', 'approved'))
    parser.add_argument('--reviewer')
    parser.add_argument('--note')
    args = parser.parse_args(argv)
    if args.review_event:
        review = record_review(args.out, args.review_event, args.review_status or '',
                               args.reviewer or '', args.note or '')
        print(json.dumps(review, ensure_ascii=False, indent=2))
        return 0
    if not all((args.discover_csv, args.pack_root)):
        parser.error('run mode requires --discover-csv and --pack-root')
    generator = None
    if args.references:
        from context.creative_model import generate_with_model
        from scripts.business_event_pack import OpenCodeGoClient
        client = OpenCodeGoClient()
        def generator(intel, material, facts, event, refs):
            return generate_with_model(event, intel, material, facts, refs, client,
                                       model='hy4-flash-free', session='v4-daily-creative')
    report = run_daily(args.discover_csv, args.pack_root, args.out, args.state,
                       args.references, days=args.days, limit=args.limit,
                       creative_generator=generator)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
