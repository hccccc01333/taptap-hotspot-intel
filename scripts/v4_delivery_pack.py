"""Assemble a V4 delivery pack (three deliverables + interactive preview).

No model calls, no network: it consumes an existing event pack's evidence and
final intelligence/material, adds a deterministic growth-creative draft and
renders a self-contained preview page. This is the delivery-layer MVP, not a
publisher.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context.delivery import DELIVERY_TYPES, build_delivery, render_html  # noqa: E402
from context.file_io import atomic_write_json, atomic_write_text, output_lock  # noqa: E402
from context.reference_library import load_library  # noqa: E402


def _read_final(folder: Path) -> dict:
    for name in ('final.json', 'accepted.json'):
        path = folder / name
        if path.is_file():
            return json.loads(path.read_text(encoding='utf-8'))
    return {}


def build_pack(pack_dir: str | Path, out_dir: str | Path,
               reference_library: dict | None = None,
               creative_generator=None, *, lock_output: bool = True) -> dict:
    out_dir = Path(out_dir)
    if not lock_output:
        return _build_pack(pack_dir, out_dir, reference_library, creative_generator)
    with output_lock(out_dir):
        return _build_pack(pack_dir, out_dir, reference_library, creative_generator)


def _build_pack(pack_dir: str | Path, out_dir: str | Path,
                reference_library: dict | None = None,
                creative_generator=None) -> dict:
    pack_dir = Path(pack_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = json.loads((pack_dir / 'summary.json').read_text(encoding='utf-8'))
    prior_index = out_dir / 'index.json'
    existing = (json.loads(prior_index.read_text(encoding='utf-8')).get('deliveries') or []
                if prior_index.is_file() else [])
    rows = list(existing)
    known = {row.get('event_id') for row in rows}
    for item in summary.get('events') or []:
        event_id = item.get('event_id')
        if not event_id or not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', event_id):
            rows.append({'event_id': event_id, 'status': 'skipped',
                         'error': 'invalid event_id'})
            continue
        folder = pack_dir / event_id
        evidence_path = folder / 'evidence.json'
        if not evidence_path.is_file():
            rows.append({'event_id': event_id, 'status': 'skipped',
                         'error': 'evidence.json missing'})
            continue
        evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
        final = _read_final(folder)
        event = evidence.get('event') or {'event_id': event_id, 'title': item.get('title')}
        package = build_delivery(event, evidence, final,
                                   reference_library=reference_library,
                                   creative_generator=creative_generator)
        previous_path = out_dir / event_id / 'delivery.json'
        if previous_path.is_file():
            from context.delivery_review import review_package
            previous = json.loads(previous_path.read_text(encoding='utf-8'))
            review_package(package, previous)
        target = out_dir / event_id
        target.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target / 'delivery.json', package)
        atomic_write_text(target / 'preview.html', render_html(package))
        row = {'event_id': event_id, 'title': event.get('title'),
               'status': package['status'], 'checks_passed': package['checks']['passed'],
               'preview': f'{event_id}/preview.html'}
        if event_id in known:
            rows = [old if old.get('event_id') != event_id else row for old in rows]
        else:
            rows.append(row)
            known.add(event_id)
    index = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>V4 交付包</title><style>body{margin:0;background:#0f1115;color:#e8eaed;',
             'font:15px/1.6 system-ui,"Segoe UI",sans-serif;padding:28px}h1{font-size:22px}',
             'a{color:#9d8cff;text-decoration:none}.item{background:#171a21;border:1px solid #262a33;',
             'border-radius:12px;padding:14px;margin:10px 0}span{color:#9aa0a6;font-size:13px}</style>',
             '</head><body><h1>V4 交付包（草稿，未发布）</h1>']
    for row in rows:
        if row.get('preview'):
            index.append(
                f'<div class="item"><a href="{html.escape(row["preview"])}">'
                f'{html.escape(str(row.get("title") or row["event_id"]))}</a>'
                f'<br><span>三类型：{" / ".join(DELIVERY_TYPES)} · 质量检查：'
                f'{"通过" if row.get("checks_passed") else "待补"}</span></div>')
        else:
            index.append(f'<div class="item">{html.escape(row["event_id"])}: '
                         f'{html.escape(str(row.get("error") or row.get("status")))}</div>')
    index.append('</body></html>')
    (out_dir / 'index.html').write_text('\n'.join(index), encoding='utf-8')
    manifest_rows = []
    for row in rows:
        if not row.get('preview'):
            continue
        package_path = out_dir / row['event_id'] / 'delivery.json'
        package = json.loads(package_path.read_text(encoding='utf-8')) if package_path.is_file() else {}
        review = package.get('review') or {}
        manifest_rows.append({
            'event_id': row['event_id'], 'title': row.get('title'),
            'status': package.get('status') or row.get('status'),
            'checks_passed': row.get('checks_passed'),
            'delivery': f"{row['event_id']}/delivery.json", 'preview': row.get('preview'),
            'review_status': review.get('status'), 'review_risks': review.get('risks') or [],
            'published_at': (package.get('event') or {}).get('published_at'),
        })
    index_manifest = {
        'schema': 'v4-delivery-index-v0.1',
        'generated_at': __import__('time').strftime('%Y-%m-%dT%H:%M:%S%z'),
        'deliveries': manifest_rows,
        'note': '共享前端契约：/api/v4/deliveries 读取本 index；自动检查不等于人工审核。',
    }
    atomic_write_json(out_dir / 'index.json', index_manifest)
    result = {'pack': str(pack_dir), 'out': str(out_dir), 'events': rows}
    atomic_write_json(out_dir / 'summary.json', result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='V4 交付包：三类型成果 + HTML 预览')
    parser.add_argument('--pack', required=True, help='已有事件包目录')
    parser.add_argument('--out', required=True, help='交付包输出目录')
    parser.add_argument('--references', default=None, help='可选：TapTap 参考库 JSON')
    parser.add_argument('--creative-model', default=None,
                        help='可选：用模型生成增长创意（如 hy4-flash-free）')
    args = parser.parse_args(argv)
    library = load_library(args.references) if args.references else None
    generator = None
    if args.creative_model:
        from context.creative_model import generate_with_model
        from scripts.business_event_pack import OpenCodeGoClient
        client = OpenCodeGoClient()
        def generator(intel, material, facts, event, refs):
            if not refs:
                return {'ok': False, 'error': 'model creative requires --references'}
            return generate_with_model(event, intel, material, facts, refs, client,
                                       model=args.creative_model,
                                       session='v4-creative-mvp')
    result = build_pack(args.pack, args.out, reference_library=library,
                        creative_generator=generator)
    ok = sum(1 for item in result['events']
             if item.get('preview') and item.get('checks_passed'))
    failed = sum(1 for item in result['events']
                 if not item.get('preview') or not item.get('checks_passed'))
    print(json.dumps({'events': len(result['events']), 'previews': ok,
                      'failed': failed, 'out': result['out']}, ensure_ascii=False, indent=2))
    return 0 if ok and not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
