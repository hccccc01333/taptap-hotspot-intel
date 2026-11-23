"""SQLite-backed event, version, fact and context store (V4 §5 MVP)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 'v4-context-mvp-v0.1'
TZ = timezone(timedelta(hours=8))

PLATFORM_KEYWORDS = ('Steam', 'PS4', 'PS5', 'PlayStation', 'Switch', 'Xbox',
                     'PC', 'iOS', 'Android', '移动端', '主机')
DATE_RE = re.compile(r'(?:20\d{2}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日|20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}')
PRICE_RE = re.compile(r'(?:售价|价格|定价)?\s*(\d+(?:\.\d+)?)\s*日元|¥\s*(\d+(?:\.\d+)?)')
NUMBER_RE = re.compile(r'\d+(?:\.\d+)?%?|20\d{2}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*日')


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def normalize(value: str) -> str:
    return re.sub(r'\s+', '', str(value or ''))


def stable_id(prefix: str, value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return prefix + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


def game_key_from_title(title: str) -> str:
    """Conservative group key: prefer book-title content, else first title segment."""
    text = str(title or '').strip()
    match = re.search(r'《([^》]{1,80})》', text)
    if match:
        return normalize(match.group(1)).strip('：: -_').casefold()
    text = re.split(r'[：:|丨]', text, maxsplit=1)[0]
    return (normalize(text)[:60] or 'unknown').casefold()


def groups(db_path: str | Path) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        return [dict(row) for row in conn.execute(
            'SELECT g.*, COUNT(l.event_id) AS events FROM event_group g '
            'LEFT JOIN event_group_link l USING(group_key) GROUP BY g.group_key '
            'ORDER BY g.last_seen DESC')]
    finally:
        conn.close()


def add_event_stub(*, event_id: str, title: str, source_url: str, published_at: str = '',
                   game_key: str | None = None, db_path: str | Path) -> dict[str, Any]:
    """Register a metadata-only source (no body) for cross-source candidate checks."""
    conn = connect(db_path)
    try:
        stamp = now_iso()
        key = game_key or game_key_from_title(title)
        subject = re.sub(r'^[《【\[]|[》】\]]$', '', str(title or '')).split('：')[0].strip()[:80]
        conn.execute('''INSERT INTO event(event_id,title,subject,source_url,first_seen,last_seen,
                        game_key,published_at) VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(event_id) DO UPDATE SET last_seen=excluded.last_seen,
                        title=excluded.title, subject=excluded.subject,
                        game_key=excluded.game_key, published_at=excluded.published_at''',
                     (event_id, title, subject, source_url, stamp, stamp, key, published_at))
        conn.execute('''INSERT INTO event_group(group_key,label,first_seen,last_seen)
                        VALUES(?,?,?,?) ON CONFLICT(group_key) DO UPDATE SET
                        last_seen=excluded.last_seen''', (key, subject, stamp, stamp))
        conn.execute('INSERT OR IGNORE INTO event_group_link VALUES(?,?,?,?)',
                     (event_id, key, 'metadata_only', stamp))
        _timeline(conn, event_id, stamp, 'source_metadata_only', title, source_url,
                  {'published_at': published_at})
        conn.commit()
        return {'event_id': event_id, 'game_key': key, 'title': title,
                'source_url': source_url, 'published_at': published_at}
    finally:
        conn.close()


def group_facts(group_key: str, db_path: str | Path) -> list[dict[str, Any]]:
    """Raw fact rows (with event_id) for all events in a group, for matching."""
    conn = connect(db_path)
    try:
        event_ids = [row['event_id'] for row in conn.execute(
            'SELECT event_id FROM event_group_link WHERE group_key=?', (group_key,))]
        if not event_ids:
            return []
        placeholders = ','.join('?' for _ in event_ids)
        return [dict(row) for row in conn.execute(
            f'SELECT * FROM fact WHERE event_id IN ({placeholders}) ORDER BY first_seen',
            event_ids)]
    finally:
        conn.close()


def group_detail(group_key: str, db_path: str | Path) -> dict[str, Any]:
    """Multi-source view: events, deduplicated facts, timeline and conflict candidates."""
    conn = connect(db_path)
    try:
        group = conn.execute('SELECT * FROM event_group WHERE group_key=?', (group_key,)).fetchone()
        if group is None:
            return {}
        events = [dict(row) for row in conn.execute(
            'SELECT e.* FROM event e JOIN event_group_link l USING(event_id) '
            'WHERE l.group_key=? ORDER BY e.first_seen', (group_key,))]
        event_ids = [event['event_id'] for event in events]
        facts = []
        if event_ids:
            placeholders = ','.join('?' for _ in event_ids)
            facts = [dict(row) for row in conn.execute(
                f'SELECT * FROM fact WHERE event_id IN ({placeholders}) '
                'ORDER BY first_seen', event_ids)]
        timeline_rows = []
        if event_ids:
            placeholders = ','.join('?' for _ in event_ids)
            timeline_rows = [dict(row) for row in conn.execute(
                f'SELECT * FROM timeline WHERE event_id IN ({placeholders}) ORDER BY at, rowid',
                event_ids)]
        # Deduplicate facts by (text, quote) across sources; keep source event ids.
        deduped: dict[str, dict[str, Any]] = {}
        for fact in facts:
            key = stable_id('gfact_', {'text': fact['text'], 'quote': fact['quote']})
            row = deduped.setdefault(key, {'fact_key': key, 'text': fact['text'],
                                           'quote': fact['quote'],
                                           'statuses': [], 'source_urls': [],
                                           'event_ids': []})
            row['statuses'].append(fact['status'])
            row['event_ids'].append(fact['event_id'])
            if fact.get('source_url') and fact['source_url'] not in row['source_urls']:
                row['source_urls'].append(fact['source_url'])
        # Conflict candidates: distinct date/price atoms across active facts.
        def atoms(pattern: re.Pattern[str]) -> dict[str, list[str]]:
            values: dict[str, list[str]] = {}
            for fact in facts:
                if fact['status'] != 'active':
                    continue
                for atom in pattern.findall(f'{fact["text"]} {fact["quote"]}'):
                    values.setdefault(normalize(atom), []).append(fact['event_id'])
            return values

        conflicts = []
        dates = atoms(DATE_RE)
        if len(dates) > 1:
            conflicts.append({'kind': 'date_candidates', 'values': sorted(dates)})
        prices = atoms(PRICE_RE)
        if len(prices) > 1:
            conflicts.append({'kind': 'price_candidates', 'values': sorted(prices)})
        return {'group': dict(group), 'events': events,
                'facts': list(deduped.values()), 'timeline': timeline_rows,
                'conflict_candidates': conflicts,
                'note': '冲突候选只是不同来源的文本原子不一致，未判定谁正确；需人工/来源核查。'}
    finally:
        conn.close()


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS event(
        event_id TEXT PRIMARY KEY, title TEXT, subject TEXT, source_url TEXT,
        first_seen TEXT, last_seen TEXT);
      CREATE TABLE IF NOT EXISTS event_version(
        version_id TEXT PRIMARY KEY, event_id TEXT, page_version TEXT, content_hash TEXT,
        fetched_at TEXT, source_url TEXT, created_at TEXT,
        UNIQUE(event_id, page_version));
      CREATE TABLE IF NOT EXISTS fact(
        fact_id TEXT PRIMARY KEY, event_id TEXT, version_id TEXT, text TEXT, quote TEXT,
        source_url TEXT, quote_supported INTEGER, status TEXT, first_seen TEXT,
        last_seen TEXT, stale_at TEXT, superseded_by TEXT);
      CREATE TABLE IF NOT EXISTS timeline(
        entry_id TEXT PRIMARY KEY, event_id TEXT, at TEXT, kind TEXT, summary TEXT,
        source_url TEXT, payload TEXT);
      CREATE TABLE IF NOT EXISTS context_card(
        card_id TEXT PRIMARY KEY, event_id TEXT, version_id TEXT, created_at TEXT,
        payload TEXT, UNIQUE(event_id, version_id));
      CREATE TABLE IF NOT EXISTS event_group(
        group_key TEXT PRIMARY KEY, label TEXT, first_seen TEXT, last_seen TEXT);
      CREATE TABLE IF NOT EXISTS event_group_link(
        event_id TEXT PRIMARY KEY, group_key TEXT, relation TEXT, created_at TEXT);
    ''')
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(event)')}
    if 'game_key' not in columns:
        conn.execute('ALTER TABLE event ADD COLUMN game_key TEXT')
    if 'published_at' not in columns:
        conn.execute('ALTER TABLE event ADD COLUMN published_at TEXT')
    conn.commit()
    return conn


def _event_from_pack(pack_dir: Path) -> list[dict[str, Any]]:
    summary = json.loads((pack_dir / 'summary.json').read_text(encoding='utf-8'))
    events = summary.get('events') if isinstance(summary, dict) else None
    if not isinstance(events, list):
        raise ValueError('summary.json 缺少 events 列表')
    return [item for item in events if isinstance(item, dict) and item.get('event_id')]


def _read_event_files(pack_dir: Path, event_id: str) -> tuple[dict, dict, dict]:
    folder = pack_dir / event_id
    evidence = json.loads((folder / 'evidence.json').read_text(encoding='utf-8'))
    final_path = folder / 'final.json'
    final = json.loads(final_path.read_text(encoding='utf-8')) if final_path.is_file() else {}
    return evidence, final, folder


def _timeline(conn: sqlite3.Connection, event_id: str, at: str, kind: str,
              summary: str, source_url: str = '', payload: Any = None) -> None:
    entry_id = stable_id('tl_', {'event_id': event_id, 'at': at, 'kind': kind,
                                 'summary': summary, 'source': source_url,
                                 'payload': payload})
    try:
        conn.execute('INSERT INTO timeline VALUES(?,?,?,?,?,?,?)',
                     (entry_id, event_id, at, kind, summary[:500], source_url,
                      json.dumps(payload, ensure_ascii=False)))
    except sqlite3.IntegrityError:
        pass


def _select_context_text(event: dict[str, Any], article: dict[str, Any],
                         quotes: list[str]) -> tuple[str, dict[str, int]]:
    """Quotes first, then article paragraphs relevant to the event.

    Navigation, related links and comment blocks are excluded; paragraphs are
    kept only when they mention the subject or an event action. This is still a
    heuristic: it reduces noise, it does not verify facts.
    """
    body = str(article.get('body') or '')
    title = str(event.get('title') or article.get('title') or '')
    quoted = [quote for quote in quotes if quote.strip()]
    core_match = re.search(r'《([^》]{2,60})》', title)
    subject_core = core_match.group(1) if core_match else title[:12]
    action_words = ('宣布', '上线', '发售', '登陆', '售价', '价格', '支持', '平台',
                    '赛季', '版本', '更新', '截止', '活动', '预约', '公测')
    drop_words = ('相关资讯', '热门评论', '他们都在说', '玩家点评', '登录', '注册',
                  '首页', '商城', '论坛', '专题', '专区', '主站', '专栏')
    paragraphs: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r'\n+', body):
        line = raw.strip()
        if len(line) < 20 or any(word in line for word in drop_words):
            continue
        if subject_core not in line and not any(word in line for word in action_words):
            continue
        key = normalize(line)
        if key in seen:
            continue
        seen.add(key)
        paragraphs.append(line)
        if len(paragraphs) >= 12 or sum(len(item) for item in paragraphs) >= 2500:
            break
    selected = quoted + paragraphs
    # De-duplicate while preserving quotes first.
    deduped: list[str] = []
    seen.clear()
    for item in selected:
        key = normalize(item)
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)
    return '\n'.join(deduped), {'quotes': len(quoted), 'paragraphs': len(paragraphs)}


def _keyword_present(text: str, word: str) -> bool:
    if word == 'iOS':
        return bool(re.search(r'(?<![A-Za-z])iOS(?![A-Za-z])', text))
    if re.search(r'[\u4e00-\u9fff]', word):
        return word in text
    return bool(re.search(r'(?<![A-Za-z])' + re.escape(word) + r'(?![A-Za-z])',
                          text, re.I))


def _extract_context(event: dict[str, Any], article: dict[str, Any], final: dict[str, Any],
                     version_id: str) -> dict[str, Any]:
    title = str(event.get('title') or article.get('title') or '')
    parsed = final.get('parsed') if isinstance(final.get('parsed'), dict) else {}
    facts = parsed.get('facts') if isinstance(parsed.get('facts'), list) else []
    quotes = [str(item.get('quote') or '').strip() for item in facts if isinstance(item, dict)]
    quotes = [quote for quote in quotes if quote]
    source_text, basis = _select_context_text(event, article, quotes)
    platforms = [word for word in PLATFORM_KEYWORDS if _keyword_present(source_text, word)]
    dates = sorted({match.group(0) for match in DATE_RE.finditer(source_text)})[:8]
    prices = []
    for match in PRICE_RE.finditer(source_text):
        value = match.group(1) or match.group(2)
        if value:
            prices.append(value + ' 日元')
    unknowns = [str(item) for item in (parsed.get('unknowns') or [])][:12]
    subject = re.sub(r'^[《【\[]|[》】\]]$', '', title).split('：')[0].strip()[:80]
    mobile_related = any(_keyword_present(source_text, word)
                         for word in ('iOS', 'Android', '移动端', '手游'))
    hypothesis = ('移动端/手游相关，与 TapTap 分发或社区讨论可能存在直接关系'
                  if mobile_related else
                  '以 PC/主机为主，与 TapTap 移动生态关系较弱；可作行业动态或社区话题，'
                  '不宜直接当作应用内分发内容')
    return {
        'schema': SCHEMA_VERSION, 'event_id': event['event_id'],
        'version_id': version_id, 'subject': subject,
        'source_category': 'public_http_observation',
        'source_urls': [event.get('url') or article.get('url')],
        'fetched_at': article.get('fetched_at') or now_iso(),
        'platforms': platforms, 'dates': dates, 'prices': prices,
        'unknowns': unknowns, 'extraction_basis': basis,
        'relevance_hypothesis': {'text': hypothesis, 'kind': 'judgement_not_fact'},
        'facts_available': len(parsed.get('facts') or []),
        'note': 'Context card 仅由公开 HTTP 原文与模型输出中的 unknowns 生成；'
                '平台/日期/价格是文本抽取，未做事实核验；相关性是判断，不是事实。',
    }


def context_from_article(event: dict[str, Any], article: dict[str, Any],
                         final: dict[str, Any] | None = None,
                         version_id: str = 'preview') -> dict[str, Any]:
    """Build a context card from one article without writing the store."""
    return _extract_context(event, article, final or {}, version_id)


def ingest_pack(pack_dir: str | Path, db_path: str | Path) -> dict[str, Any]:
    """Idempotently ingest a business event pack into the event/context store.

    Re-ingesting the same page version is a no-op. A new page version marks
    older facts stale when their quote is no longer present in the new body.
    """
    pack_dir = Path(pack_dir)
    conn = connect(db_path)
    created = {'events': 0, 'versions': 0, 'facts': 0, 'stale': 0, 'cards': 0}
    try:
        for item in _event_from_pack(pack_dir):
            event_id = item['event_id']
            evidence, final, _folder = _read_event_files(pack_dir, event_id)
            event = evidence.get('event') or {}
            article = evidence.get('article') or {}
            body = str(article.get('body') or '')
            if len(body) < 200:
                continue
            title = str(event.get('title') or item.get('title') or '')
            url = str(event.get('url') or article.get('url') or '')
            subject = re.sub(r'^[《【\[]|[》】\]]$', '', title).split('：')[0].strip()[:80]
            stamp = article.get('fetched_at') or now_iso()
            page_version = 'sha256:' + hashlib.sha256(normalize(body).encode('utf-8')).hexdigest()
            version_id = stable_id('ver_', {'event_id': event_id, 'page_version': page_version})
            existing = conn.execute('SELECT 1 FROM event WHERE event_id=?', (event_id,)).fetchone()
            game_key = str(event.get('game') or game_key_from_title(title))
            published_at = str(event.get('published_at') or '')
            conn.execute('''INSERT INTO event(event_id,title,subject,source_url,first_seen,last_seen,game_key,published_at)
                            VALUES(?,?,?,?,?,?,?,?)
                            ON CONFLICT(event_id) DO UPDATE SET last_seen=excluded.last_seen,
                            title=excluded.title, subject=excluded.subject,
                            game_key=excluded.game_key, published_at=excluded.published_at''',
                         (event_id, title, subject, url, stamp, stamp, game_key, published_at))
            conn.execute('''INSERT INTO event_group(group_key,label,first_seen,last_seen)
                            VALUES(?,?,?,?) ON CONFLICT(group_key) DO UPDATE SET
                            last_seen=excluded.last_seen,
                            label=CASE WHEN event_group.label='' THEN excluded.label ELSE event_group.label END''',
                         (game_key, subject, stamp, stamp))
            conn.execute('''INSERT OR IGNORE INTO event_group_link
                            VALUES(?,?,?,?)''', (event_id, game_key, 'same_subject', stamp))
            if not existing:
                created['events'] += 1
                _timeline(conn, event_id, stamp, 'event_created', title, url)
            cursor = conn.execute('''INSERT OR IGNORE INTO event_version
                VALUES(?,?,?,?,?,?,?)''',
                (version_id, event_id, page_version,
                 hashlib.sha256(body.encode('utf-8')).hexdigest(), stamp, url, now_iso()))
            if cursor.rowcount:
                created['versions'] += 1
                _timeline(conn, event_id, stamp, 'source_observed', f'读取页面 {page_version[:14]}', url)
                # Older facts whose quote disappeared become stale.
                old = conn.execute('''SELECT fact_id,quote FROM fact
                    WHERE event_id=? AND status='active' AND version_id<>?''',
                    (event_id, version_id)).fetchall()
                stale_ids = [row['fact_id'] for row in old
                             if normalize(row['quote']) and normalize(row['quote']) not in normalize(body)]
                for fact_id in stale_ids:
                    conn.execute('''UPDATE fact SET status='stale', stale_at=?, superseded_by=?
                                    WHERE fact_id=?''', (stamp, version_id, fact_id))
                    created['stale'] += 1
                    _timeline(conn, event_id, stamp, 'fact_stale',
                              f'旧版本引文在新版本中消失: {fact_id}', url)
            parsed = final.get('parsed') if isinstance(final.get('parsed'), dict) else {}
            facts = parsed.get('facts') if isinstance(parsed.get('facts'), list) else []
            for fact in facts:
                if not isinstance(fact, dict):
                    continue
                text = str(fact.get('text') or '').strip()
                quote = str(fact.get('quote') or '').strip()
                if not text or not quote:
                    continue
                supported = bool(normalize(quote)) and normalize(quote) in normalize(body)
                fact_id = stable_id('fact_', {'event_id': event_id, 'text': text, 'quote': quote})
                cursor = conn.execute('''INSERT OR IGNORE INTO fact
                    (fact_id,event_id,version_id,text,quote,source_url,quote_supported,status,
                     first_seen,last_seen,stale_at,superseded_by)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (fact_id, event_id, version_id, text, quote, url,
                     1 if supported else 0, 'active', stamp, stamp, None, None))
                if cursor.rowcount:
                    created['facts'] += 1
                    _timeline(conn, event_id, stamp, 'fact_recorded',
                              f"{'引文支持' if supported else '引文未逐字支持'}: {text[:80]}",
                              url)
            card = _extract_context(event, article, final, version_id)
            cursor = conn.execute('''INSERT OR IGNORE INTO context_card
                VALUES(?,?,?,?,?)''',
                (stable_id('card_', {'event_id': event_id, 'version': version_id}),
                 event_id, version_id, now_iso(), json.dumps(card, ensure_ascii=False)))
            if cursor.rowcount:
                created['cards'] += 1
                _timeline(conn, event_id, now_iso(), 'context_card_built',
                          card['relevance_hypothesis']['text'][:120], url,
                          {'version_id': version_id})
        conn.commit()
    finally:
        conn.close()
    return created


def event_detail(event_id: str, db_path: str | Path) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        event = conn.execute('SELECT * FROM event WHERE event_id=?', (event_id,)).fetchone()
        if event is None:
            return {}
        versions = [dict(row) for row in conn.execute(
            'SELECT * FROM event_version WHERE event_id=? ORDER BY created_at', (event_id,))]
        facts = [dict(row) for row in conn.execute(
            'SELECT * FROM fact WHERE event_id=? ORDER BY first_seen', (event_id,))]
        card = conn.execute('''SELECT payload FROM context_card WHERE event_id=?
            ORDER BY created_at DESC LIMIT 1''', (event_id,)).fetchone()
        return {'event': dict(event), 'versions': versions, 'facts': facts,
                'context_card': json.loads(card['payload']) if card else None,
                'timeline': timeline(event_id, db_path)}
    finally:
        conn.close()


def context_card(event_id: str, db_path: str | Path) -> dict[str, Any]:
    return event_detail(event_id, db_path).get('context_card') or {}


def timeline(event_id: str, db_path: str | Path) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        return [dict(row) for row in conn.execute(
            'SELECT * FROM timeline WHERE event_id=? ORDER BY at, rowid', (event_id,))]
    finally:
        conn.close()
