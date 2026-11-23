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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context.delivery import DELIVERY_TYPES, build_delivery, render_html  # noqa: E402
from context.reference_library import load_library  # noqa: E402


def _read_final(folder: Path) -> dict:
    for name in ('final.json', 'accepted.json'):
        path = folder / name
        if path.is_file():
            return json.loads(path.read_text(encoding='utf-8'))
    return {}


def build_pack(pack_dir: str | Path, out_dir: str | Path,
               reference_library: dict | None = None) -> dict:
    pack_dir = Path(pack_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = json.loads((pack_dir / 'summary.json').read_text(encoding='utf-8'))
    rows = []
    for item in summary.get('events') or []:
        event_id = item.get('event_id')
        if not event_id:
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
                                   reference_library=reference_library)
        target = out_dir / event_id
        target.mkdir(parents=True, exist_ok=True)
        (target / 'delivery.json').write_text(
            json.dumps(package, ensure_ascii=False, indent=2), encoding='utf-8')
        (target / 'preview.html').write_text(render_html(package), encoding='utf-8')
        rows.append({'event_id': event_id, 'title': event.get('title'),
                     'status': package['status'],
                     'checks_passed': package['checks']['passed'],
                     'preview': f'{event_id}/preview.html'})
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
    index_manifest = {
        'schema': 'v4-delivery-index-v0.1',
        'generated_at': __import__('time').strftime('%Y-%m-%dT%H:%M:%S%z'),
        'deliveries': [
            {'event_id': row['event_id'], 'title': row.get('title'),
             'status': row.get('status'), 'checks_passed': row.get('checks_passed'),
             'delivery': f"{row['event_id']}/delivery.json",
             'preview': row.get('preview')}
            for row in rows if row.get('preview')
        ],
        'note': '共享前端契约：/api/v4/deliveries 读取本 index；草稿未发布。',
    }
    (out_dir / 'index.json').write_text(
        json.dumps(index_manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    result = {'pack': str(pack_dir), 'out': str(out_dir), 'events': rows}
    (out_dir / 'summary.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='V4 交付包：三类型成果 + HTML 预览')
    parser.add_argument('--pack', required=True, help='已有事件包目录')
    parser.add_argument('--out', required=True, help='交付包输出目录')
    parser.add_argument('--references', default=None, help='可选：TapTap 参考库 JSON')
    args = parser.parse_args(argv)
    library = load_library(args.references) if args.references else None
    result = build_pack(args.pack, args.out, reference_library=library)
    ok = sum(1 for item in result['events'] if item.get('preview'))
    print(json.dumps({'events': len(result['events']), 'previews': ok,
                      'out': result['out']}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
