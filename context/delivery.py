"""V4 delivery layer MVP: three deliverable types + interactive preview.

Takes an existing event pack (evidence + final intelligence/material), adds a
deterministic growth-creative draft built from project-original structure
patterns, validates the package, and renders a self-contained interactive HTML
preview.

Important boundaries:
- reference patterns are project-original structures, not TapTap official rules;
- "generation" here is deterministic assembly, not a new model call;
- growth creative is a draft with explicit preconditions, not a promised action;
- no publication, no V3 production write.
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DELIVERY_SCHEMA = 'v4-delivery-v0.1'
DELIVERY_TYPES = ('intelligence', 'material', 'growth_creative')


@dataclass(frozen=True)
class ReferencePattern:
    name: str
    structure: tuple[str, ...]
    use_for: str
    source_note: str = '项目原创结构模板，不是 TapTap 官方规范'


DEFAULT_PATTERNS: tuple[ReferencePattern, ...] = (
    ReferencePattern(
        name='版本速览',
        structure=('一句话结论', '三条以内关键事实', '未知与使用边界'),
        use_for='版本更新/活动公告类情报',
    ),
    ReferencePattern(
        name='社区话题参与',
        structure=('玩家问题', '可复现动作', 'TapTap 话题或游戏页', '前置条件'),
        use_for='增长创意中的参与路径草案',
    ),
    ReferencePattern(
        name='素材文案三段式',
        structure=('标题钩子', '正文信息', '行动引导'),
        use_for='社区动态/版本速览素材',
    ),
)


def _clean(value: Any, fallback: str = '') -> str:
    text = str(value or '').strip()
    return text or fallback


def build_growth_creative(intel: dict[str, Any], material: dict[str, Any],
                          facts: list[dict[str, Any]],
                          patterns: tuple[ReferencePattern, ...] = DEFAULT_PATTERNS) -> dict[str, Any]:
    """Deterministic growth-creative draft from existing intelligence/material.

    It never invents a TapTap entry, reward or official confirmation; the
    ``preconditions`` and ``status`` make that explicit.
    """
    one_line = _clean(intel.get('one_line')) or _clean(intel.get('summary'))[:120]
    summary = _clean(intel.get('summary'))
    why = _clean(intel.get('why_taptap'))
    boundary = _clean(material.get('usage_boundary'),
                      '不补写未确认信息，不承诺奖励或官方入口')
    fact_texts = [_clean(fact.get('text')) for fact in facts if _clean(fact.get('text'))]
    action = ('在 TapTap 对应游戏社区发起“你怎么看/你会怎么参与”的话题讨论，'
              '把本条情报转成玩家可回答的问题；入口与运营资源待确认。')
    return {
        'schema': DELIVERY_SCHEMA,
        'type': 'growth_creative',
        'pattern': patterns[1].name,
        'title': f"{_clean(material.get('title'), _clean(intel.get('headline'), '事件速览'))}"
                 f"｜玩家参与角度草案",
        'hook': one_line,
        'audience_need': why or '待验证：需要站内讨论样本后才能确认玩家需求。',
        'proposed_action': action,
        'copy_draft': (
            f"【标题】{one_line}\n\n"
            f"【正文】{summary[:300]}\n\n"
            f"【互动问题】如果这个变化落到你常玩的游戏里，你最想先体验哪一点？\n\n"
            f"【边界】{boundary}"
        ),
        'preconditions': [
            '确认 TapTap 对应游戏页/社区话题与可发布位置',
            '取得讨论样本或运营确认，避免把判断写成玩家需求',
            '核查素材与引文使用条件，未授权不发布',
        ],
        'status': 'draft_needs_operational_confirmation',
        'source_facts': fact_texts[:5],
        'pattern_sources': [pattern.source_note for pattern in patterns],
    }


def build_delivery(event: dict[str, Any], evidence: dict[str, Any],
                   final: dict[str, Any],
                   patterns: tuple[ReferencePattern, ...] = DEFAULT_PATTERNS,
                   reference_library: dict[str, Any] | None = None) -> dict[str, Any]:
    parsed = final.get('parsed') if isinstance(final.get('parsed'), dict) else {}
    intel = parsed.get('intelligence') if isinstance(parsed.get('intelligence'), dict) else {}
    material = parsed.get('material') if isinstance(parsed.get('material'), dict) else {}
    facts = parsed.get('facts') if isinstance(parsed.get('facts'), list) else []
    if reference_library:
        from .reference_library import build_creative_with_references
        creative = build_creative_with_references(intel, material, facts, event,
                                                  reference_library)
    else:
        creative = build_growth_creative(intel, material, facts, patterns)
    quote_checks = final.get('quote_checks') if isinstance(final.get('quote_checks'), dict) else {}
    package = {
        'schema': DELIVERY_SCHEMA,
        'event': {'event_id': event.get('event_id'), 'title': event.get('title'),
                  'url': event.get('url'), 'source': event.get('source'),
                  'published_at': event.get('published_at')},
        'intelligence': {
            'headline': _clean(intel.get('headline')),
            'one_line': _clean(intel.get('one_line')),
            'summary': _clean(intel.get('summary')),
            'why_taptap': _clean(intel.get('why_taptap')),
            'watch_next': list(intel.get('watch_next') or []),
            'facts': facts,
            'unknowns': list(parsed.get('unknowns') or []),
        },
        'material': {
            'title': _clean(material.get('title')),
            'channel': _clean(material.get('channel')),
            'draft': _clean(material.get('draft')),
            'usage_boundary': _clean(material.get('usage_boundary')),
        },
        'growth_creative': creative,
        'evidence': {
            'source_url': evidence.get('article', {}).get('url') or event.get('url'),
            'article_title': evidence.get('article', {}).get('title'),
            'fetched_at': evidence.get('article', {}).get('fetched_at'),
        },
        'checks': validate_delivery({
            'intelligence': intel, 'material': material,
            'growth_creative': creative, 'facts': facts,
            'quote_checks': quote_checks,
        }),
        'reference_library': ({'schema': reference_library.get('schema'),
                               'samples': len(reference_library.get('samples') or []),
                               'patterns': len(reference_library.get('patterns') or [])}
                              if reference_library else None),
        'status': 'draft',
        'note': '草稿交付包：三类型齐全，但未发布、未做独立事实核验；'
                '增长创意的入口与运营资源需确认。',
    }
    return package


def validate_delivery(package: dict[str, Any]) -> dict[str, Any]:
    intel = package.get('intelligence') or {}
    material = package.get('material') or {}
    creative = package.get('growth_creative') or {}
    facts = package.get('facts') or []
    checks = package.get('quote_checks') or {}
    supported = int(checks.get('facts_supported') or 0)
    total = int(checks.get('facts_total') or len(facts))
    required = {
        'intelligence': bool(_clean(intel.get('summary')) and facts),
        'material': all(_clean(material.get(key)) for key in
                        ('title', 'channel', 'draft', 'usage_boundary')),
        'growth_creative': all(_clean(creative.get(key)) for key in
                               ('title', 'hook', 'proposed_action', 'copy_draft',
                                'status')),
        'all_types_present': all(bool(package.get(section)) for section in
                                 ('intelligence', 'material', 'growth_creative')),
        'facts_have_quotes': bool(facts) and all(_clean(fact.get('quote')) for fact in facts),
        'quote_support': total > 0 and supported == total,
        'unknowns_present': bool(intel.get('unknowns') or creative.get('preconditions')),
        'creative_has_boundary': bool(_clean(creative.get('status'))),
    }
    return {'checks': required,
            'passed': all(required.values()),
            'quote_supported': supported,
            'quote_total': total}


def load_delivery_index(root: str | Path) -> dict[str, Any]:
    """Load the shared-frontend delivery index (or an empty manifest)."""
    root = Path(root)
    path = root / 'index.json'
    if not path.is_file():
        return {'schema': 'v4-delivery-index-v0.1', 'deliveries': [],
                'note': f'no delivery index at {path}'}
    return json.loads(path.read_text(encoding='utf-8'))


def render_html(package: dict[str, Any]) -> str:
    event = package.get('event') or {}
    intel = package.get('intelligence') or {}
    material = package.get('material') or {}
    creative = package.get('growth_creative') or {}
    checks = (package.get('checks') or {}).get('checks') or {}
    facts = intel.get('facts') or []
    unknowns = intel.get('unknowns') or []
    checks_html = ''.join(
        f'<li class="{"ok" if ok else "bad"}">{html.escape(name)}: '
        f'{"通过" if ok else "待补"}</li>' for name, ok in checks.items())
    facts_html = ''.join(
        f'<li><b>{html.escape(str(fact.get("text") or ""))}</b><br>'
        f'<span>引文：{html.escape(str(fact.get("quote") or ""))}</span></li>'
        for fact in facts)
    unknowns_html = ''.join(f'<li>{html.escape(str(item))}</li>' for item in unknowns)
    preconditions_html = ''.join(
        f'<li>{html.escape(str(item))}</li>' for item in (creative.get('preconditions') or []))
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(str(event.get('title') or 'V4 交付预览'))}</title>
<style>
:root{{--bg:#0f1115;--card:#171a21;--text:#e8eaed;--muted:#9aa0a6;--accent:#7c5cff;--ok:#45c486;--bad:#e07777}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.65 system-ui,"Segoe UI",sans-serif}}
header{{padding:28px;border-bottom:1px solid #262a33}}h1{{margin:0 0 8px;font-size:22px}}.muted{{color:var(--muted);font-size:13px}}
.tabs{{display:flex;gap:8px;padding:16px 28px;position:sticky;top:0;background:var(--bg);border-bottom:1px solid #262a33;flex-wrap:wrap}}
button{{background:#1e222b;color:var(--text);border:1px solid #2c323d;padding:8px 14px;border-radius:999px;cursor:pointer}}
button.active{{background:var(--accent);border-color:var(--accent)}}
main{{padding:24px 28px;max-width:980px}}section{{display:none;background:var(--card);border:1px solid #262a33;border-radius:14px;padding:20px}}
section.active{{display:block}}h2{{margin-top:0}}ul{{padding-left:20px}}.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}
.card{{border-left:3px solid var(--accent);padding:10px 14px;margin:12px 0;background:#12151b}}
pre{{white-space:pre-wrap;background:#101318;padding:14px;border-radius:10px;border:1px solid #262a33}}
</style></head><body>
<header><h1>{html.escape(str(event.get('title') or '事件'))}</h1>
<div class="muted">来源：{html.escape(str(event.get('url') or ''))} · 状态：{html.escape(str(package.get('status') or ''))}</div></header>
<div class="tabs">
<button class="active" data-tab="intel">情报</button>
<button data-tab="material">素材</button>
<button data-tab="creative">增长创意</button>
<button data-tab="checks">质量检查</button>
</div>
<main>
<section id="intel" class="active"><h2>情报</h2>
<p>{html.escape(str(intel.get('one_line') or ''))}</p>
<h3>摘要</h3><p>{html.escape(str(intel.get('summary') or ''))}</p>
<h3>为什么与 TapTap 有关（判断，不是事实）</h3><p>{html.escape(str(intel.get('why_taptap') or ''))}</p>
<h3>事实与引文</h3><ul>{facts_html}</ul>
<h3>未知</h3><ul>{unknowns_html}</ul></section>
<section id="material"><h2>素材</h2>
<p class="muted">{html.escape(str(material.get('channel') or ''))}</p>
<h3>{html.escape(str(material.get('title') or ''))}</h3>
<pre>{html.escape(str(material.get('draft') or ''))}</pre>
<h3>使用边界</h3><p>{html.escape(str(material.get('usage_boundary') or ''))}</p></section>
<section id="creative"><h2>增长创意（草稿）</h2>
<div class="card"><b>{html.escape(str(creative.get('title') or ''))}</b><br>
{html.escape(str(creative.get('hook') or ''))}</div>
<h3>建议动作</h3><p>{html.escape(str(creative.get('proposed_action') or ''))}</p>
<h3>文案草案</h3><pre>{html.escape(str(creative.get('copy_draft') or ''))}</pre>
<h3>前置条件</h3><ul>{preconditions_html}</ul>
<p class="muted">状态：{html.escape(str(creative.get('status') or ''))}</p></section>
<section id="checks"><h2>质量检查（代理指标）</h2><ul>{checks_html}</ul>
<p class="muted">逐字引文支持不等于语义支持；未做独立事实核验。</p></section>
</main>
<script>
document.querySelectorAll('button[data-tab]').forEach(function(btn){{
  btn.addEventListener('click', function(){{
    document.querySelectorAll('button[data-tab]').forEach(function(b){{b.classList.remove('active')}});
    document.querySelectorAll('main section').forEach(function(s){{s.classList.remove('active')}});
    btn.classList.add('active');
    document.getElementById(btn.dataset.tab).classList.add('active');
  }});
}});
</script></body></html>'''
