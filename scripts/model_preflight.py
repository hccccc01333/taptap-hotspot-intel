"""Model health preflight CLI (no LongCat by default).

Usage::

    python -X utf8 scripts/model_preflight.py --out docs_local/evaluations/model-preflight.json

Each candidate gets one tiny JSON health check. Capped models (LongCat) and
training-consented models (Iceman) are skipped unless explicitly allowed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context.model_router import MODEL_CAPABILITIES, preflight, select_model  # noqa: E402
from scripts.business_event_pack import OpenCodeGoClient  # noqa: E402

DEFAULT_CANDIDATES = ('hy4-flash-free', 'beam-free', 'iceman-contributor-free')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='模型可用性/小 JSON 输出预检')
    parser.add_argument('--out', default=None, help='JSON 输出路径（可选）')
    parser.add_argument('--include-training', action='store_true',
                        help='允许检查会用于训练的模型（如 Iceman）')
    parser.add_argument('--include-capped', action='store_true',
                        help='允许检查有调用次数上限的模型（如 LongCat）')
    parser.add_argument('--session', default='model-preflight')
    args = parser.parse_args(argv)
    candidates = list(DEFAULT_CANDIDATES)
    if args.include_capped:
        candidates.append('longcat-2.5-free')
    client = OpenCodeGoClient()
    started = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    health = preflight(client, candidates, session=args.session,
                       allow_training=args.include_training,
                       allow_capped=args.include_capped)
    selection = select_model(health, candidates, allow_training=args.include_training,
                             allow_capped=args.include_capped)
    payload = {'generated_at': started, 'candidates': candidates,
               'allow_training': args.include_training,
               'allow_capped': args.include_capped,
               'capabilities': {model: MODEL_CAPABILITIES.get(model, {}) for model in candidates},
               'health': [item.as_dict() for item in health],
               'selection': selection.as_dict()}
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    for item in health:
        print(f"{item.model}\t{item.status}\t{item.reason}\t{item.latency_ms}ms")
    print(f"selected: {selection.model or 'NONE'} ({selection.reason})")
    return 0 if selection.model else 3


if __name__ == '__main__':
    raise SystemExit(main())
