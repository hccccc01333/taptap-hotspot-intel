"""Deterministic claim-vs-evidence checks for business records (Alpha25 P0).

Runs before a business record becomes usable. It never calls a model and never
rewrites content: it only classifies how well the delivered text is supported
by the quotes it cites, and records machine-readable reasons.

Status levels:
- supported:   deterministic checks passed.
- draft:       fact tokens are grounded, but declared production slots remain
               (link placeholders and similar pre-publication conditions).
- insufficient: high-specificity facts, participation-rule details or whole
               claim clauses are not directly supported by the cited quotes.
               These records stay in the database as drafts but leave the
               directly usable material bank.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher

VERSION = 'claim-verifier-v3.25'

# Reasons that block direct usability (evidence not sufficient).
INSUFFICIENT_CODES = frozenset({
    'claim_clause_not_quoted',      # a whole claim clause is not covered by the cited quotes
    'detail_not_quoted',            # dates/numbers/hashtags/titles absent from the cited quotes
    'rule_detail_title_only',       # rule details supported only by title-level quotes
    'sufficiency_not_grounded',     # "X即可参与" style sufficiency claims without a matching quote
    'placeholder_unknown_date',     # 截止X月X日 style unknown value presented in a fact slot
    'placeholder_generic_slot',     # #游戏名SideStory# style generic name slot
    'quote_missing',                # no quotes available to support the record
})
DRAFT_CODES = frozenset({
    'placeholder_link_slot',        # {{TapTap承接链接}} style declared link slot
    'placeholder_declared_pending', # 待替换/待确认 style declared pending items
    'placeholder_dangling_arrow',   # trailing arrow with no target
})

_RULE_TOKEN_HINTS = ('参与', '报名', '截止', '步骤', '入口', '专题', '资格', '规则', '细则', '名次', '获奖')
_SUFFICIENCY = re.compile(r'([\u4e00-\u9fa5A-Za-z0-9#]{2,18}?)(?:即可|就能|便可|就可以|只需|仅需)')
_PRIZE_CONTEXT = re.compile(r'[\u4e00-\u9fa5]{0,6}(?:奖品|奖励|赢取|获得|赢)[\u4e00-\u9fa5]{0,4}[【\[]([^】\]]{2,24})[】\]]')


def normalize(text):
    """Remove whitespace, unify full-width characters; never reorder content."""
    if not isinstance(text, str):
        return ''
    folded = unicodedata.normalize('NFKC', text)
    return re.sub(r'\s+', '', folded)


def _clauses(text):
    return [c for c in re.split(r'[。；;！？!?\n，,]+', normalize(text)) if c]


def _quote_blob(quotes):
    return '\n'.join(normalize(q.get('quote', '')) for q in quotes)


def _covered_ratio(clause, blob):
    if not clause:
        return 1.0
    matched = sum(block.size for block in SequenceMatcher(None, clause, blob, autojunk=False).get_matching_blocks())
    return matched / len(clause)


def specific_tokens(text):
    """High-specificity fact tokens that must appear in the cited quotes."""
    t = normalize(text)
    tokens = []
    tokens += re.findall(r'\d{2,4}年\d{1,2}月(?:\d{1,2}日)?', t)
    tokens += re.findall(r'\d{1,2}月\d{1,2}日', t)
    tokens += re.findall(r'\d{1,2}月(?=[起内底初])', t)
    tokens += re.findall(r'\d{1,2}:\d{2}', t)
    tokens += re.findall(r'\d+\+', t)
    tokens += re.findall(r'\d+(?=张|个|款|位|名|次|人|套|件|份|天|小时|分钟|%|周年|卡|关|层|页|份)', t)
    tokens += re.findall(r'\d{3,}', t)
    tokens += re.findall(r'#([^#\n]{2,24})#', t)
    tokens += re.findall(r'《([^》\n]{2,30})》', t)
    tokens += _PRIZE_CONTEXT.findall(t)
    return list(dict.fromkeys(tokens))


def _placeholder_reasons(text):
    t = normalize(text)
    reasons = []
    if re.search(r'[Xx×]{1,2}(?:月|日)', t) or re.search(r'[Xx×]{2,}', t):
        reasons.append(('placeholder_unknown_date', '存在未填的日期占位（如 X月X日）'))
    if re.search(r'\{\{[^}\n]{0,40}\}\}', t) or re.search(r'【待[替换补写填]{1,2}[^】\n]{0,24}】', t):
        reasons.append(('placeholder_link_slot', '存在待替换占位（承接链接等）'))
    if re.search(r'(?:待替换|待补充|待填写|请填写|请替换|占位符)', t):
        reasons.append(('placeholder_declared_pending', '文本自述仍有待替换/待确认项'))
    if re.search(r'#[^#\n]{0,20}(?:游戏名|名称)', t):
        reasons.append(('placeholder_generic_slot', '存在通用名称占位（如 #游戏名SideStory#）'))
    if t.endswith('→') or t.endswith('->') or re.search(r'→\s*$', t):
        reasons.append(('placeholder_dangling_arrow', '承接箭头缺少目标'))
    return reasons


def _sufficiency_reasons(text, blob):
    reasons = []
    for match in _SUFFICIENCY.finditer(normalize(text)):
        span = match.group(0)
        phrase = match.group(1)
        if span in blob:
            continue
        if re.search(re.escape(phrase) + r'.{0,6}(?:即可|就能|便可)', blob):
            continue
        reasons.append(('sufficiency_not_grounded', '参与方式断言“.%s.”未被引用逐字支持' % span[:20]))
        if len(reasons) >= 3:
            break
    return reasons


def _rule_detail_reasons(text, quotes, titles):
    t = normalize(text)
    if not any(token in t for token in _RULE_TOKEN_HINTS):
        return []
    for quote in quotes:
        q = normalize(quote.get('quote', ''))
        title = normalize((titles or {}).get(quote.get('evidence_id'), ''))
        if len(q) >= 10 and not (title and (q in title or title in q)):
            return []
    if not quotes:
        return []
    return [('rule_detail_title_only', '参与规则/步骤细节缺少正文级引用（仅有标题级引用）')]


def _coverage_reasons(text, quotes):
    blob = _quote_blob(quotes)
    if not blob:
        return [('quote_missing', '没有可核对的引用')]
    reasons = []
    for clause in _clauses(text):
        if len(clause) < 5:
            continue
        ratio = _covered_ratio(clause, blob)
        threshold = 0.55 if len(clause) >= 6 else 0.9
        if ratio < threshold:
            reasons.append(('claim_clause_not_quoted', '断言片段未被引用覆盖：' + clause[:36]))
            if len(reasons) >= 3:
                break
    return reasons


def _token_reasons(text, quotes, titles):
    blob = _quote_blob(quotes) + '\n' + '\n'.join(normalize(t) for t in (titles or {}).values())
    missing = [token for token in specific_tokens(text) if normalize(token) not in blob]
    if not missing:
        return []
    return [('detail_not_quoted', '具体细节未获引用支持：' + '、'.join(missing[:4]))]


def _verdict(scope, reasons, checked):
    codes = {code for code, _ in reasons}
    if codes.intersection(INSUFFICIENT_CODES):
        status = 'insufficient'
    elif codes.intersection(DRAFT_CODES):
        status = 'draft'
    else:
        status = 'supported'
    return {'status': status, 'version': VERSION, 'scope': scope,
            'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'checked': list(checked), 'reasons': [{'code': code, 'detail': detail} for code, detail in reasons[:8]]}


def verify_signal(signal, titles=None):
    """A game signal must state only what its cited quotes directly support."""
    text = signal.get('observed_change', '') or ''
    quotes = [f for f in signal.get('facts', []) if isinstance(f, dict)]
    reasons = []
    reasons += _coverage_reasons(text, quotes)
    reasons += _token_reasons(text, quotes, titles)
    reasons += _rule_detail_reasons(text, quotes, titles)
    reasons += _placeholder_reasons(text)
    return _verdict('game_signal', reasons, ('observed_change',))


def verify_material(item, quotes, titles=None):
    """Materials carry editable product text; fact tokens and rule mechanics must
    be grounded, while creative phrasing may add phrasing but not new facts."""
    texts = [item.get('content', '') or '']
    delivery = item.get('delivery') or {}
    if isinstance(delivery, dict) and delivery.get('body'):
        texts.append(delivery['body'])
    reasons = []
    for text in texts:
        reasons += _placeholder_reasons(text)
        reasons += _sufficiency_reasons(text, _quote_blob(quotes))
        reasons += _token_reasons(text, quotes, titles)
    reasons += _rule_detail_reasons('\n'.join(texts), quotes, titles)
    if not quotes:
        reasons.append(('quote_missing', '没有可核对的引用'))
    return _verdict('material', reasons, ('content', 'delivery.body'))


def verify_creative(payload, quotes, titles=None):
    """The consumer copy of a creative plan is checked like delivery text."""
    reasons = []
    copy = payload.get('copy', '') or ''
    reasons += _placeholder_reasons(copy)
    reasons += _sufficiency_reasons(copy, _quote_blob(quotes))
    reasons += _token_reasons(copy, quotes, titles)
    reasons += _rule_detail_reasons(copy, quotes, titles)
    if not quotes:
        reasons.append(('quote_missing', '没有可核对的引用'))
    return _verdict('creative', reasons, ('copy',))


def summarize(verdicts):
    counts = {'supported': 0, 'draft': 0, 'insufficient': 0}
    for verdict in verdicts:
        status = (verdict or {}).get('status')
        if status in counts:
            counts[status] += 1
    return counts
