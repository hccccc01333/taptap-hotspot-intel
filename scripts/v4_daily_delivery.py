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
from context.file_io import atomic_write_json, output_lock  # noqa: E402
from context.reference_library import load_library  # noqa: E402
from scripts.v4_delivery_pack import build_pack  # noqa: E402

CST = timezone(timedelta(hours=8))


def _moment_id(url: str) -> str:
    match = re.search(r'/moment/(\d+)', str(url or ''))
    return match.group(1) if match else ''


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default


def _write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_json(path, value)


def select_candidates(csv_path: str | Path, out_dir: str | Path,
                      state_path: str | Path, *, now: datetime | None = None,
                      days: int = 7, limit: int | None = 5) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Return unseen recent moments, newest first, and the current ledger."""
    now = now or datetime.now(CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    if days < 1 or (limit is not None and limit < 1):
        raise ValueError('days and limit must be positive')
    state = _read_json(Path(state_path), {'schema': 'v4-daily-state-v0.1',
                                          'delivered': {}, 'failed': {}})
    state.setdefault('delivered', {})
    state.setdefault('failed', {})
    with Path(csv_path).open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    cutoff = now - timedelta(days=days)
    by_moment: dict[str, dict[str, str]] = {}
    for row in rows:
        moment_id = row.get('moment_id') or ''
        if not re.fullmatch(r'[0-9]+', moment_id) or moment_id in state['delivered']:
            continue
        try:
            published = datetime.fromtimestamp(int(row.get('publish_time') or 0), tz=CST)
        except (ValueError, OSError):
            continue
        if not cutoff <= published <= now:
            continue
        if len((row.get('summary') or '').strip()) < 40:
            continue
        previous = by_moment.get(moment_id)
        if not previous or int(row.get('publish_time') or 0) > int(previous.get('publish_time') or 0):
            by_moment[moment_id] = row
    candidates = sorted(by_moment.values(),
                        key=lambda row: int(row.get('publish_time') or 0), reverse=True)
    return candidates if limit is None else candidates[:limit], state


def run_daily(discover_csv: str | Path, pack_root: str | Path, out_dir: str | Path,
              state_path: str | Path, references: str | Path | None = None, *,
              now: datetime | None = None, days: int = 7, limit: int = 5,
              creative_generator=None) -> dict[str, Any]:
    out_dir = Path(out_dir)
    with output_lock(out_dir):
        return _run_daily(discover_csv, pack_root, out_dir, state_path, references,
                          now=now, days=days, limit=limit,
                          creative_generator=creative_generator)


def _run_daily(discover_csv: str | Path, pack_root: str | Path, out_dir: str | Path,
               state_path: str | Path, references: str | Path | None = None, *,
               now: datetime | None = None, days: int = 7, limit: int = 5,
               creative_generator=None) -> dict[str, Any]:
    out_dir, pack_root, state_path = Path(out_dir), Path(pack_root), Path(state_path)
    now = now or datetime.now(CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    if days < 1 or limit < 1:
        raise ValueError('days and limit must be positive')
    if not pack_root.is_dir():
        raise FileNotFoundError(f'research pack root not found: {pack_root}')
    index = _read_json(out_dir / 'index.json', {'deliveries': []})
    initial_state = _read_json(state_path, {'schema': 'v4-daily-state-v0.1',
                                            'delivered': {}, 'failed': {}})
    initial_state.setdefault('delivered', {})
    initial_state.setdefault('failed', {})
    available = {}
    for item in index.get('deliveries') or []:
        event_id = item.get('event_id')
        if not event_id:
            continue
        package_path = out_dir / event_id / 'delivery.json'
        try:
            package = _read_json(package_path, {})
        except (OSError, ValueError):
            continue
        moment_id = _moment_id((package.get('event') or {}).get('url'))
        if moment_id and package.get('checks', {}).get('passed') and (out_dir / event_id / 'preview.html').is_file():
            available[moment_id] = event_id
            prior = initial_state['delivered'].get(moment_id) or {}
            initial_state['delivered'][moment_id] = {
                'event_id': event_id,
                'delivered_at': prior.get('delivered_at') or package.get('review', {}).get('reviewed_at')}
            initial_state['failed'].pop(moment_id, None)
    for moment_id in list(initial_state['delivered']):
        if moment_id not in available:
            initial_state['delivered'].pop(moment_id, None)
    _write_json(state_path, initial_state)
    candidates, state = select_candidates(discover_csv, out_dir, state_path,
                                          now=now, days=days, limit=None)
    run_at = now.astimezone(CST).isoformat()
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

    def record_failure(item: dict[str, Any], error: str) -> None:
        failure = {'moment_id': item['moment_id'], 'title': item.get('title'),
                   'error': error[:500]}
        if item.get('event_id'):
            failure['event_id'] = item['event_id']
        report['failed'].append(failure)
        previous = state['failed'].get(item['moment_id']) or {}
        state['failed'][item['moment_id']] = {
            **failure, 'attempts': previous.get('attempts', 0) + 1,
            'last_attempt_at': run_at}

    try:
        library = None
        library_error = ''
        if references:
            try:
                library = load_library(references)
            except Exception as error:
                library_error = f'{type(error).__name__}: {error}'
        for row in candidates:
            if len(report['delivered']) >= limit:
                break
            moment_id = row['moment_id']
            folder = None
            for path in sorted(pack_root.iterdir()):
                if not path.is_dir() or not (path / 'evidence.json').is_file():
                    continue
                try:
                    evidence = _read_json(path / 'evidence.json', {})
                except (OSError, ValueError):
                    continue
                if _moment_id((evidence.get('event') or {}).get('url')) == moment_id:
                    folder = path
                    break
            if folder is None:
                record_failure({'moment_id': moment_id, 'title': row.get('title')},
                               'research pack missing')
                continue
            item = {'moment_id': moment_id, 'event_id': folder.name,
                    'title': row.get('title') or folder.name}
            existing_path = out_dir / folder.name / 'delivery.json'
            if existing_path.is_file():
                try:
                    existing = _read_json(existing_path, {})
                    existing_moment = _moment_id((existing.get('event') or {}).get('url'))
                except (OSError, ValueError):
                    existing_moment = ''
                if existing_moment and existing_moment != moment_id:
                    record_failure(item, 'event_id conflict with a different moment')
                    continue
            event_staging = staging / moment_id
            try:
                shutil.copytree(folder, event_staging / folder.name)
                _write_json(event_staging / 'summary.json', {'events': [item]})
                if library_error:
                    raise ValueError(library_error)
                build_pack(event_staging, out_dir, reference_library=library,
                           creative_generator=creative_generator, lock_output=False)
                package = _read_json(out_dir / folder.name / 'delivery.json', {})
                if not package.get('checks', {}).get('passed'):
                    raise ValueError('delivery checks failed')
            except Exception as error:
                record_failure(item, f'{type(error).__name__}: {error}')
                continue
            state['delivered'][moment_id] = {
                'event_id': folder.name, 'delivered_at': run_at}
            state['failed'].pop(moment_id, None)
            report['delivered'].append(item)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
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
