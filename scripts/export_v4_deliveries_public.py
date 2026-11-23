"""Export V4 deliveries to the shared frontend's public static data path.

Copies only index-listed deliveries (never the whole local directory) into
``spatial/public/v4-deliveries/`` so the Pages build can serve them without a
backend. This is a deliberately small, public-safe payload: facts, quotes,
source URLs and creative drafts; no local paths or secrets.
"""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data' / 'v4' / 'deliveries'
TARGET = ROOT / 'spatial' / 'public' / 'v4-deliveries'


def main() -> int:
    index = json.loads((SOURCE / 'index.json').read_text(encoding='utf-8'))
    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.mkdir(parents=True, exist_ok=True)
    delivered = []
    for item in index.get('deliveries') or []:
        event_id = item.get('event_id')
        if not event_id:
            continue
        folder = SOURCE / event_id
        delivery = folder / 'delivery.json'
        if not delivery.is_file():
            continue
        target = TARGET / event_id
        target.mkdir(parents=True, exist_ok=True)
        payload = json.loads(delivery.read_text(encoding='utf-8'))
        package = copy.deepcopy(payload)
        package.pop('usage', None)
        creative = package.get('growth_creative')
        if isinstance(creative, dict):
            creative.pop('usage', None)
        (target / 'delivery.json').write_text(
            json.dumps(package, ensure_ascii=False, indent=2), encoding='utf-8')
        preview = folder / 'preview.html'
        if preview.is_file():
            shutil.copy2(preview, target / 'preview.html')
        delivered.append({**item, 'delivery': f'{event_id}/delivery.json'})
    public_index = {**index, 'deliveries': delivered,
                    'note': '公开静态交付索引；草稿，未发布。'}
    (TARGET / 'index.json').write_text(
        json.dumps(public_index, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'deliveries': len(delivered), 'target': str(TARGET)},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
