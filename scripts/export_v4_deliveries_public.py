"""Export V4 deliveries to the shared frontend's public static data path.

Copies only index-listed deliveries (never the whole local directory) into
``spatial/public/v4-deliveries/`` so the Pages build can serve them without a
backend. This is a deliberately small, public-safe payload: facts, quotes,
source URLs and creative drafts; no local paths or secrets.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context.file_io import atomic_write_json, output_lock  # noqa: E402

SOURCE = ROOT / 'data' / 'v4' / 'deliveries'
TARGET = ROOT / 'spatial' / 'public' / 'v4-deliveries'


def main() -> int:
    with output_lock(SOURCE):
        return _export()


def _export() -> int:
    index = json.loads((SOURCE / 'index.json').read_text(encoding='utf-8'))
    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.mkdir(parents=True, exist_ok=True)
    delivered = []
    for item in index.get('deliveries') or []:
        event_id = item.get('event_id')
        if not event_id or not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', event_id):
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
        atomic_write_json(target / 'delivery.json', package)
        preview = folder / 'preview.html'
        if preview.is_file():
            shutil.copy2(preview, target / 'preview.html')
        delivered.append({**item, 'delivery': f'{event_id}/delivery.json'})
    public_index = {**index, 'deliveries': delivered,
                    'note': '公开静态交付索引；草稿，未发布。'}
    atomic_write_json(TARGET / 'index.json', public_index)
    print(json.dumps({'deliveries': len(delivered), 'target': str(TARGET)},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
