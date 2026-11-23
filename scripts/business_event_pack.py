"""V4 business event pack: HTTP evidence -> draft -> final content package.

This is a reproducible *business* workflow, not an auto-publisher:

- reads a frozen event list (public HTTP pages only);
- fetches article text and saves evidence + quote checks;
- drafts with an unlimited free model (Beam Free by default);
- optionally writes the final intelligence/material with LongCat 2.5 Free,
  at most one call per event and a hard 100-call ledger cap;
- never publishes; every pack includes a human review checklist.

Model calls require an OpenCode Go credential. Read from ``OPENCODE_API_KEY``
or the local OpenCode ``auth.json`` (``opencode-go`` entry); secrets are never
written to the package. The provider counts reasoning tokens as output; this
tool stores only ``message.content`` and reports provider usage separately.

Usage::

    python -X utf8 scripts/business_event_pack.py \
        --events docs_local/business-events.json \
        --out docs_local/evaluations/business-pack-20261115
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DRAFT_MODEL = 'beam-free'
DEFAULT_FINAL_MODEL = 'longcat-2.5-free'
DEFAULT_MAX_TOKENS = 12000
DEFAULT_GO_BASE = 'https://opencode.ai/inference/go/openai/v1'
LONGCAT_CAP = 100
USER_AGENT = 'taptap-hotspot-intel-business-pack/1.0'
ARTICLE_SELECTORS = ('.article-content', '.show_cont', '#news_article',
                     '.news-content', 'article')


class BusinessPackError(RuntimeError):
    """Base error for a pack that cannot continue."""


class ModelBudgetExceeded(BusinessPackError):
    """LongCat (or another capped model) reached its hard call cap."""


class FetchError(BusinessPackError):
    """The article could not be fetched."""


@dataclass(frozen=True)
class Event:
    event_id: str
    url: str
    title: str
    source: str
    published_at: str
    objective: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> 'Event':
        if not isinstance(value, dict):
            raise ValueError('event 必须是对象')
        fields = {}
        for key in ('event_id', 'url', 'title', 'source', 'published_at', 'objective'):
            item = value.get(key)
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f'event.{key} 不能为空')
            fields[key] = item.strip()
        parsed = urlparse(fields['url'])
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('event.url 必须是公开 HTTPS 页面')
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,80}', fields['event_id']):
            raise ValueError('event.event_id 需为小写字母数字/横线/下划线')
        return cls(**fields)


@dataclass
class CallRecord:
    call_id: str
    model: str
    event_id: str
    started_at: str
    status: str = 'reserved'
    usage: dict[str, Any] = field(default_factory=dict)
    content_chars: int = 0
    error: str = ''


class Ledger:
    """Append-only JSON call ledger with a hard cap for LongCat models.

    The record is written *before* the request (reserved), so a crash cannot
    silently exceed the cap. Free unlimited models are recorded without a cap.
    """

    def __init__(self, path: Path, *, longcat_cap: int = LONGCAT_CAP) -> None:
        self.path = Path(path)
        self.longcat_cap = longcat_cap
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            return value if isinstance(value, list) else []
        except (OSError, ValueError):
            return []

    def _write(self, records: list[dict[str, Any]]) -> None:
        temp = self.path.with_suffix(self.path.suffix + '.tmp')
        temp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, self.path)

    @staticmethod
    def is_capped(model: str) -> bool:
        return 'longcat' in model.lower()

    def reserve(self, model: str, event_id: str) -> str:
        records = self._read()
        if self.is_capped(model):
            used = sum(1 for item in records if self.is_capped(str(item.get('model', ''))))
            if used >= self.longcat_cap:
                raise ModelBudgetExceeded(
                    f'{model} 已达本地账本上限 {self.longcat_cap} 次；拒绝继续调用')
        call_id = 'call_' + uuid.uuid4().hex[:20]
        records.append({'call_id': call_id, 'model': model, 'event_id': event_id,
                        'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                        'status': 'reserved'})
        self._write(records)
        return call_id

    def finalize(self, call_id: str, *, status: str, usage: dict[str, Any] | None = None,
                 content_chars: int = 0, error: str = '') -> None:
        records = self._read()
        for item in records:
            if item.get('call_id') == call_id:
                item.update(status=status, usage=usage or {}, content_chars=content_chars,
                            error=error[:500], finished_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
                break
        self._write(records)


class ModelClient(Protocol):
    def complete(self, prompt: str, *, model: str, max_tokens: int,
                 event_id: str, session: str) -> dict[str, Any]:
        ...


class OpenCodeGoClient:
    """Minimal OpenCode Go chat client. Only ``message.content`` is kept."""

    def __init__(self, *, api_key: str | None = None, base_url: str = DEFAULT_GO_BASE,
                 timeout: float = 180.0, http_post: Callable[..., Any] | None = None,
                 ledger: Ledger | None = None) -> None:
        self.api_key = api_key or _load_api_key()
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self._http_post = http_post or _default_post
        self.ledger = ledger

    def complete(self, prompt: str, *, model: str, max_tokens: int,
                 event_id: str, session: str) -> dict[str, Any]:
        if not self.api_key:
            raise BusinessPackError('缺少 OpenCode Go 凭证（OPENCODE_API_KEY 或 auth.json）')
        call_id = None
        if self.ledger is not None:
            call_id = self.ledger.reserve(model, event_id)
        try:
            response = self._http_post(
                self.base_url + '/chat/completions',
                headers={'Authorization': 'Bearer ' + self.api_key,
                         'Content-Type': 'application/json',
                         'User-Agent': USER_AGENT,
                         'x-opencode-session': session},
                json={'model': model,
                      'messages': [
                          {'role': 'system', 'content': (
                              '你为 TapTap 社区运营撰写游戏情报与可编辑素材草案。'
                              '只依据用户给出的原文，不得补充外部知识；不确定就写进 unknowns。'
                              '充分思考后只输出合法 JSON，不要 Markdown 代码围栏，不要输出思考过程。')},
                          {'role': 'user', 'content': prompt},
                      ],
                      'max_tokens': max_tokens, 'temperature': 0.2},
                timeout=self.timeout)
            status_code = int(getattr(response, 'status_code', 200))
            body = getattr(response, 'text', '')
            if status_code != 200:
                raise BusinessPackError(f'模型接口 HTTP {status_code}: {body[:300]}')
            data = response.json()
            message = ((data.get('choices') or [{}])[0].get('message') or {})
            content = message.get('content') or ''
            if not isinstance(content, str) or not content.strip():
                raise BusinessPackError('模型没有返回 content（可能被 reasoning 截断）')
            choice = (data.get('choices') or [{}])[0]
            result = {'content': content, 'usage': data.get('usage') or {},
                      'model': data.get('model') or model, 'id': data.get('id'),
                      'finish_reason': choice.get('finish_reason') or ''}
            if call_id is not None:
                self.ledger.finalize(call_id, status='ok', usage=result['usage'],
                                     content_chars=len(content))
            return result
        except Exception as exc:
            if call_id is not None:
                self.ledger.finalize(call_id, status='failed', error=f'{type(exc).__name__}: {exc}')
            raise


def _default_post(url: str, **kwargs: Any) -> Any:
    import requests
    return requests.post(url, **kwargs)


def _load_api_key() -> str | None:
    key = os.environ.get('OPENCODE_API_KEY')
    if isinstance(key, str) and key.strip():
        return key.strip()
    try:
        auth = json.loads((Path.home() / '.local/share/opencode/auth.json')
                          .read_text(encoding='utf-8'))
        value = (auth.get('opencode-go') or {}).get('key')
        if isinstance(value, str) and value.strip():
            return value.strip()
    except (OSError, ValueError):
        pass
    return None


def fetch_article(url: str, *, fetch: Callable[..., Any] | None = None,
                  timeout: float = 25.0) -> dict[str, Any]:
    """Fetch a public HTTP article and cut obvious comment/nav chrome."""
    from bs4 import BeautifulSoup

    fetcher = fetch or _default_get
    try:
        response = fetcher(url, timeout=timeout,
                           headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                                                  'Chrome/120.0 Safari/537.36'})
    except Exception as exc:
        raise FetchError(f'{type(exc).__name__}: {exc}') from exc
    if int(getattr(response, 'status_code', 0)) != 200:
        raise FetchError(f'HTTP {getattr(response, "status_code", "?")}')
    response.encoding = getattr(response, 'apparent_encoding', None) or 'gb18030'
    soup = BeautifulSoup(response.text, 'html.parser')
    node = None
    for selector in ARTICLE_SELECTORS:
        node = soup.select_one(selector)
        if node is not None:
            break
    body = node.get_text('\n', strip=True) if node is not None else soup.get_text('\n', strip=True)
    body = re.sub(r'\n{2,}', '\n', body).strip()
    cut = body.find('相关资讯')
    if cut > 800:
        body = body[:cut].strip()
    if len(body) < 200:
        raise FetchError(f'正文过短（{len(body)} 字），保留为失败样本')
    return {'title': soup.title.get_text(strip=True) if soup.title else '',
            'body': body, 'url': url,
            'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}


def _default_get(url: str, **kwargs: Any) -> Any:
    import requests
    return requests.get(url, **kwargs)


def load_replay_article(replay_dir: str | Path, event_id: str) -> dict[str, Any]:
    """Load a saved article from a previous pack instead of re-fetching the URL."""
    base = Path(replay_dir)
    candidates = [base / event_id / 'evidence.json', base / f'{event_id}-evidence.json']
    for path in candidates:
        if path.is_file():
            payload = json.loads(path.read_text(encoding='utf-8'))
            article = payload.get('article') if isinstance(payload, dict) else None
            if not isinstance(article, dict) or len(str(article.get('body') or '')) < 200:
                raise BusinessPackError(f'回放证据无效: {path}')
            return article
    raise BusinessPackError(f'回放证据不存在: {event_id}（查过 {[str(p) for p in candidates]}）')


def build_prompt(event: Event, body: str) -> str:
    return f"""事件：{event.title}
来源：{event.url}（{event.source}，发布时间 {event.published_at}）
研究问题：{event.objective}

原文（只可使用以下内容，引文必须逐字来自这里）：
\"\"\"{body[:9000]}\"\"\"

请充分思考后输出以下 JSON（只输出 JSON，不要 Markdown 代码围栏）：
{{
  "headline": "不超过 40 字",
  "one_line": "一句话事实概括，不超过 80 字",
  "facts": [{{"text": "用于支撑情报的必要事实", "quote": "原文逐字引句"}}],
  "unknowns": ["原文没有说明、不能确认的事项"],
  "intelligence": {{
    "summary": "发生了什么、关键数字/时间、边界",
    "why_taptap": "为什么与 TapTap 社区/运营有关（只做谨慎推断，标明是判断而非事实）",
    "watch_next": ["后续值得跟踪的具体点"]
  }},
  "material": {{
    "title": "素材标题（可编辑）",
    "channel": "适用 TapTap 场景，如社区动态/版本速览",
    "draft": "可直接编辑的中文文案，事实性内容必须能在 facts/引文中找到依据",
    "usage_boundary": "使用边界（哪些不能写、哪些仍需核实）"
  }}
}}
要求：facts 的 quote 必须逐字出现在原文中；不能确认的内容写入 unknowns；
不要把原文没有的数字、日期、评价写成事实；不要输出思考过程。"""


def build_context_prompt(event: Event, body: str, card: dict[str, Any]) -> str:
    """Baseline prompt plus a compact, explicitly-labelled context card."""
    view = {key: card.get(key) for key in
            ('subject', 'platforms', 'dates', 'prices', 'unknowns',
             'relevance_hypothesis', 'extraction_basis')}
    return build_prompt(event, body) + f"""

补充 Context Card（由公开原文与已核对引文生成，不是事实核验）：
{json.dumps(view, ensure_ascii=False, indent=2)}

使用要求：
- Context Card 只用于确定重点和边界，**不得因此减少事实数量**；
  facts 应列出原文支持的必要的全部事实（目标 8-12 条）；
- platforms/dates/prices 是文本抽取，引用时仍需用原文逐字引文支持；
- relevance_hypothesis 是判断，不是事实，不要写成已证实结论；
- unknowns 必须保留或进一步具体化；原文没有的年份/时区/平台范围不要补写。"""


def parse_json(text: str) -> dict[str, Any]:
    raw = str(text or '').strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```[a-zA-Z]*\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
    start, end = raw.find('{'), raw.rfind('}')
    if start < 0 or end < 0:
        raise ValueError('模型输出中没有 JSON 对象')
    value = json.loads(raw[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError('模型输出的 JSON 不是对象')
    return value


def normalize(value: str) -> str:
    return re.sub(r'\s+', '', str(value or ''))


def verify_quotes(value: dict[str, Any], body: str) -> dict[str, Any]:
    norm_body = normalize(body)
    facts = value.get('facts') if isinstance(value.get('facts'), list) else []
    rows = []
    for index, fact in enumerate(facts, 1):
        if not isinstance(fact, dict):
            rows.append({'index': index, 'supported': False, 'text': '', 'quote': ''})
            continue
        quote = str(fact.get('quote') or '')
        rows.append({'index': index, 'supported': bool(quote.strip()) and normalize(quote) in norm_body,
                     'text': str(fact.get('text') or '')[:300], 'quote': quote[:300]})
    return {'facts_total': len(rows),
            'facts_supported': sum(1 for row in rows if row['supported']), 'facts': rows}


def product_chars(parsed: dict[str, Any]) -> int:
    return len(json.dumps({key: parsed.get(key) for key in
                           ('headline', 'one_line', 'facts', 'intelligence', 'material', 'unknowns')},
                          ensure_ascii=False))


def render_markdown(event: Event, parsed: dict[str, Any], checks: dict[str, Any],
                    model: str, usage: dict[str, Any]) -> str:
    intel = parsed.get('intelligence') or {}
    material = parsed.get('material') or {}
    reasoning = ((usage or {}).get('completion_tokens_details') or {}).get('reasoning_tokens')
    lines = [f"# 业务草稿：{parsed.get('headline') or event.title}", '',
             f"- 事件：{event.title}", f"- 来源：{event.url}",
             f"- 模型：{model}；输出 token：{(usage or {}).get('completion_tokens')}"
             f"（其中 reasoning：{reasoning}）",
             f"- 引文核对：{checks['facts_supported']}/{checks['facts_total']} 条逐字支持",
             f"- 成品字数：{product_chars(parsed)} 字（不含 reasoning）", '',
             '## 一句话', '', parsed.get('one_line') or '', '',
             '## 事实与引文', '']
    for fact in checks['facts']:
        mark = '✅' if fact['supported'] else '⚠️ 未在原文逐字找到'
        lines.append(f"- {mark} {fact['text']}  ")
        lines.append(f"  引文：{fact['quote']}")
    lines += ['', '## 情报', '', f"**摘要**：{intel.get('summary') or ''}", '',
              f"**为什么与 TapTap 有关（判断，不是事实）**：{intel.get('why_taptap') or ''}", '',
              '**后续跟踪**：']
    lines += [f"- {item}" for item in (intel.get('watch_next') or [])]
    lines += ['', '## 可编辑素材', '', f"**标题**：{material.get('title') or ''}", '',
              f"**适用场景**：{material.get('channel') or ''}", '',
              '**文案草稿**：', '', material.get('draft') or '', '',
              f"**使用边界**：{material.get('usage_boundary') or ''}", '',
              '## 未确认事项', '']
    lines += [f"- {item}" for item in (parsed.get('unknowns') or [])] or ['- 无']
    lines += ['', '---', '本文件为模型生成草稿，引文核对只做逐字匹配，不代表事实语义核验；',
              '发布前必须由人工核查来源、时间与使用边界。']
    return '\n'.join(lines)


def _review_checklist(records: list[dict[str, Any]]) -> str:
    lines = ['# 人工核查清单（模型不填写判定）', '',
             '每项由人工核查后填写：通过 / 不通过 / 待查；未填写即未核查。', '',
             '| 事件 | 年份/时间 | 数字与单位 | 引文是否支持结论 | 平台/范围 | 使用边界 | 风险 |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    for record in records:
        if not record.get('delivered'):
            lines.append(f"| {record.get('event_id')} | — | — | — | — | — | 交付失败，需查错误 |")
            continue
        lines.append(f"| {record.get('event_id')} | 待查 | 待查 | 待查 | 待查 | 待查 | 待查 |")
    lines += ['', '核查后仍需保留：页面 URL、抓取时间、模型输出中的 unknowns、'
                  '任何冲突（例如页面日期与正文时间不一致）。']
    return '\n'.join(lines)


def run_pack(events: list[Event], out_dir: Path, *, client: ModelClient,
             fetcher: Callable[..., Any] | None = None, draft_model: str = DEFAULT_DRAFT_MODEL,
             final_model: str | None = DEFAULT_FINAL_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS,
             session: str | None = None, use_context: bool = False,
             orchestrate: bool = False, min_facts: int = 8,
             candidate_models: tuple[str, ...] | None = None,
             model_health: Any = None,
             allow_training: bool = False,
             allow_capped: bool = False,
             replay_dir: str | Path | None = None) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    session_id = session or ('pack-' + uuid.uuid4().hex[:20])
    records: list[dict[str, Any]] = []
    for event in events:
        folder = out_dir / event.event_id
        folder.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {'event_id': event.event_id, 'title': event.title,
                                  'url': event.url, 'delivered': False}
        try:
            if replay_dir is not None:
                article = load_replay_article(replay_dir, event.event_id)
                record['replayed'] = True
            else:
                article = fetch_article(event.url, fetch=fetcher)
            (folder / 'evidence.json').write_text(
                json.dumps({'event': event.__dict__, 'article': article},
                           ensure_ascii=False, indent=2), encoding='utf-8')
            record['article_chars'] = len(article['body'])
            prompt = build_prompt(event, article['body'])
            if orchestrate:
                from context.orchestrator import DeliveryPolicy, run_orchestrated
                policy = DeliveryPolicy(min_facts=min_facts, max_repairs=1,
                                        max_tokens=max_tokens)
                card_builder = None
                prompt_builder = None
                if use_context:
                    from context.engine import context_from_article

                    def card_builder(parsed):
                        return context_from_article(
                            event.__dict__, article, {'parsed': parsed},
                            version_id='orchestrate')

                    def prompt_builder(card):
                        return build_context_prompt(event, article['body'], card)

                trace = run_orchestrated(
                    event_id=event.event_id, base_prompt=prompt, body=article['body'],
                    client=client, policy=policy, draft_model=draft_model,
                    final_model=final_model, context_card_builder=card_builder,
                    context_prompt_builder=prompt_builder,
                    candidate_models=candidate_models, model_health=model_health,
                    allow_training=allow_training, allow_capped=allow_capped,
                    session=session_id)
                if trace.context_card is not None:
                    (folder / 'context-card.json').write_text(
                        json.dumps(trace.context_card, ensure_ascii=False, indent=2),
                        encoding='utf-8')
                    record['context_card'] = True
                (folder / 'orchestration-trace.json').write_text(
                    json.dumps(trace.as_dict(), ensure_ascii=False, indent=2), encoding='utf-8')
                record.update(orchestrated=True, status=trace.status,
                              repair_count=trace.repair_count, gaps=trace.gaps,
                              facts_total=(trace.score or {}).get('facts_total'),
                              facts_supported=(trace.score or {}).get('facts_supported'),
                              usage=trace.usage_total)
                if trace.status in ('ready_for_final', 'final_delivered') and trace.parsed:
                    checks = verify_quotes(trace.parsed, article['body'])
                    last = trace.steps[-1] if trace.steps else {}
                    model = str(last.get('model') or draft_model)
                    usage = last.get('usage') or {}
                    file_name = 'final.json' if trace.status == 'final_delivered' else 'accepted.json'
                    (folder / file_name).write_text(json.dumps(
                        {'model': model, 'usage': usage, 'parsed': trace.parsed,
                         'quote_checks': checks, 'orchestration': trace.as_dict()},
                        ensure_ascii=False, indent=2), encoding='utf-8')
                    (folder / 'intel-material.md').write_text(
                        render_markdown(event, trace.parsed, checks, model, usage),
                        encoding='utf-8')
                    record.update(delivered=True, model=model,
                                  product_chars=product_chars(trace.parsed))
                else:
                    (folder / 'needs-review.json').write_text(json.dumps(
                        trace.as_dict(), ensure_ascii=False, indent=2), encoding='utf-8')
                    record['error'] = trace.error or ('needs_review: ' +
                                                      ','.join(trace.gaps or ['unknown']))
                records.append(record)
                continue
            draft = client.complete(prompt, model=draft_model, max_tokens=max_tokens,
                                    event_id=event.event_id, session=session_id)
            try:
                draft_value = parse_json(draft['content'])
            except Exception:
                (folder / 'draft-raw.txt').write_text(draft['content'], encoding='utf-8')
                raise
            draft_checks = verify_quotes(draft_value, article['body'])
            (folder / 'draft.json').write_text(json.dumps(
                {'model': draft['model'], 'usage': draft['usage'], 'parsed': draft_value,
                 'quote_checks': draft_checks, 'request_id': draft.get('id')},
                ensure_ascii=False, indent=2), encoding='utf-8')
            record['draft_supported'] = draft_checks['facts_supported']
            record['draft_total'] = draft_checks['facts_total']
            final_prompt = prompt
            if use_context:
                from context.engine import context_from_article
                card = context_from_article(event.__dict__, article, draft_value,
                                            version_id='draft_preview')
                (folder / 'context-card.json').write_text(
                    json.dumps(card, ensure_ascii=False, indent=2), encoding='utf-8')
                record['context_card'] = True
                final_prompt = build_context_prompt(event, article['body'], card)
            if final_model:
                final = client.complete(final_prompt, model=final_model, max_tokens=max_tokens,
                                        event_id=event.event_id, session=session_id)
                try:
                    final_value = parse_json(final['content'])
                except Exception:
                    (folder / 'final-raw.txt').write_text(final['content'], encoding='utf-8')
                    raise
                final_checks = verify_quotes(final_value, article['body'])
                (folder / 'final.json').write_text(json.dumps(
                    {'model': final['model'], 'usage': final['usage'], 'parsed': final_value,
                     'quote_checks': final_checks, 'request_id': final.get('id')},
                    ensure_ascii=False, indent=2), encoding='utf-8')
                (folder / 'intel-material.md').write_text(
                    render_markdown(event, final_value, final_checks, final['model'],
                                    final['usage']), encoding='utf-8')
                record.update(delivered=True, model=final['model'],
                              facts_supported=final_checks['facts_supported'],
                              facts_total=final_checks['facts_total'],
                              usage=final['usage'], product_chars=product_chars(final_value))
            else:
                (folder / 'draft.md').write_text(
                    render_markdown(event, draft_value, draft_checks, draft['model'],
                                    draft['usage']), encoding='utf-8')
                record.update(delivered=True, model=draft['model'],
                              facts_supported=draft_checks['facts_supported'],
                              facts_total=draft_checks['facts_total'],
                              usage=draft['usage'], product_chars=product_chars(draft_value),
                              final_skipped=True)
        except ModelBudgetExceeded as exc:
            record['error'] = f'ModelBudgetExceeded: {exc}'
            (folder / 'error.json').write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                               encoding='utf-8')
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            (folder / 'error.json').write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                               encoding='utf-8')
        records.append(record)

    summary = {'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
               'session': session_id, 'events': records,
               'policy': {'draft_model': draft_model, 'final_model': final_model,
                          'max_tokens': max_tokens,
                          'longcat_calls_per_event': 1,
                          'thinking': 'unrestricted; reasoning discarded; only content stored',
                          'publish': 'never; human review required',
                          'orchestrate': orchestrate, 'min_facts': min_facts,
                          'candidate_models': list(candidate_models or ()),
                          'allow_training': allow_training,
                          'allow_capped': allow_capped}}
    (out_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                          encoding='utf-8')
    lines = ['# V4 业务事件包', '',
             '| 事件 | 模型 | 交付 | 引文支持 | 成品字数 | 备注 |',
             '| --- | --- | --- | --- | --- | --- |']
    for record in records:
        if record.get('delivered'):
            lines.append(f"| {record['event_id']} | {record.get('model')} | 是 | "
                         f"{record.get('facts_supported')}/{record.get('facts_total')} | "
                         f"{record.get('product_chars', '-')} | 人工核查待完成 |")
        else:
            lines.append(f"| {record['event_id']} | - | 否 | - | - | {record.get('error', '')} |")
    lines += ['', '本包只含草稿：未发布、未通过 V3 完整交付守卫、未做独立人工事实核查。',
              'LongCat 每事件最多 1 次；reasoning 不落库；provider usage 含 reasoning。']
    (out_dir / 'README.md').write_text('\n'.join(lines), encoding='utf-8')
    (out_dir / 'review-checklist.md').write_text(_review_checklist(records), encoding='utf-8')
    return summary


def write_group_summary(pack_dir: str | Path, db_path: str | Path) -> dict[str, Any]:
    """Ingest a pack into the context store and write a same-game group summary."""
    from context.engine import group_detail, groups, ingest_pack

    pack_dir = Path(pack_dir)
    result = ingest_pack(pack_dir, db_path)
    listed = groups(db_path)
    details = {row['group_key']: group_detail(row['group_key'], db_path) for row in listed}
    payload = {'ingest': result, 'groups': listed, 'details': details}
    (pack_dir / 'groups.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                          encoding='utf-8')
    return payload


def load_events(path: Path) -> list[Event]:
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    values = raw.get('events') if isinstance(raw, dict) else raw
    if not isinstance(values, list) or not values:
        raise ValueError('事件清单需要非空数组或 {"events": [...]}')
    return [Event.from_dict(item) for item in values]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='V4 业务事件包：HTTP 证据 -> 草稿 -> 最终内容包')
    parser.add_argument('--events', required=True, help='事件清单 JSON')
    parser.add_argument('--out', required=True, help='输出目录（建议 docs_local/... 或 data/v4/...）')
    parser.add_argument('--draft-model', default=DEFAULT_DRAFT_MODEL)
    parser.add_argument('--final-model', default=DEFAULT_FINAL_MODEL)
    parser.add_argument('--no-final', action='store_true', help='只生成草稿，不调用最终模型')
    parser.add_argument('--max-tokens', type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument('--ledger', default=str(ROOT / 'docs_local' / 'evaluations' /
                                               'model_call_ledger.json'))
    parser.add_argument('--session', default=None)
    parser.add_argument('--context-card', action='store_true',
                        help='用草稿事实生成 Context Card 并注入最终稿提示')
    parser.add_argument('--context-db', default=None,
                        help='可选：把输出包 ingest 到事件库并写 groups.json')
    parser.add_argument('--orchestrate', action='store_true',
                        help='用条件修复编排替代固定草稿/最终稿流程')
    parser.add_argument('--min-facts', type=int, default=8,
                        help='编排门槛：最少事实数（默认 8）')
    parser.add_argument('--auto-model', action='store_true',
                        help='先做模型预检，再按健康候选选择草稿/修复模型')
    parser.add_argument('--candidate-models', default='hy4-flash-free,beam-free',
                        help='逗号分隔的候选模型（默认 hy4-flash-free,beam-free）')
    parser.add_argument('--allow-training', action='store_true',
                        help='允许使用会训练数据的模型（如 Iceman）')
    parser.add_argument('--allow-capped', action='store_true',
                        help='允许使用有调用上限的模型（如 LongCat）')
    parser.add_argument('--replay-dir', default=None,
                        help='从已保存的事件包回放 evidence，不重新抓取 URL')
    args = parser.parse_args(argv)
    if args.max_tokens < 256:
        parser.error('--max-tokens 至少 256')
    try:
        events = load_events(Path(args.events))
    except (OSError, ValueError) as exc:
        print(f'事件清单错误: {exc}', file=sys.stderr)
        return 2
    ledger = Ledger(Path(args.ledger))
    client = OpenCodeGoClient(ledger=ledger)
    final_model = None if args.no_final else args.final_model
    candidates = tuple(item.strip() for item in args.candidate_models.split(',')
                       if item.strip())
    health = None
    if args.auto_model:
        from context.model_router import preflight
        Path(args.out).mkdir(parents=True, exist_ok=True)
        health = preflight(client, candidates, allow_training=args.allow_training,
                           allow_capped=args.allow_capped, session='pack-preflight')
        (Path(args.out) / 'model-preflight.json').write_text(
            json.dumps([item.as_dict() for item in health], ensure_ascii=False, indent=2),
            encoding='utf-8')
    try:
        summary = run_pack(events, Path(args.out), client=client,
                           draft_model=args.draft_model, final_model=final_model,
                           max_tokens=args.max_tokens, session=args.session,
                           use_context=args.context_card, orchestrate=args.orchestrate,
                           min_facts=args.min_facts,
                           candidate_models=candidates if args.auto_model else None,
                           model_health=health,
                           allow_training=args.allow_training,
                           allow_capped=args.allow_capped,
                           replay_dir=args.replay_dir)
        if args.context_db:
            write_group_summary(args.out, args.context_db)
    except BusinessPackError as exc:
        print(f'业务包失败: {exc}', file=sys.stderr)
        return 3
    delivered = sum(1 for item in summary['events'] if item.get('delivered'))
    print(f"完成：{delivered}/{len(events)} 个事件；输出 {args.out}")
    return 0 if delivered == len(events) else 1


if __name__ == '__main__':
    raise SystemExit(main())
