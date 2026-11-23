"""V4 切片 3 外部试点 runner：固定公开页面集合上的出网隔离与驱动机制运行报告。

本模块**不是**采集器，也不评估页面内容质量。它只回答一个问题：

    出网隔离与驱动在指定公开页面上是否按设计工作。

页面集合固定在 ``acquisition/fixtures/pilot/pages.json``（运行前冻结、不允许按结果
挑选或替换）。每页使用**独立的** ``PlaywrightDriver`` + ``AcquisitionStore``，默认只跑
``adaptive`` 策略，``--include-baseline`` 时额外跑 ``fixed_frames`` / ``legacy_v3``
对照。URL 模式必须按冻结文档 §3 打开出网隔离：

- ``allow_network=True`` 且 ``egress_allowlist`` 非空（否则 ``BrowserUnavailable``）；
- 由 driver 创建/启动 ``EgressProxy``，页面连接全部经过 allowlist 过滤代理；
- 从 ``driver.egress_records`` 读取 ``ConnectionRecord``（host/解析 IP/允许或拒绝/
  原因/双向字节）写进报告。

**保留全部失败**：加载失败、4xx/5xx（worker 映射为 ``blocked_reason`` 或加载失败）、
被代理拒绝、无区域、无证据、预算耗尽都原样写入报告；不重试到成功、不剔除页面、
不补写结论。证据文本只截断到 200 字写入**本地**输出，不提交页面正文。

输出 JSON + Markdown；结论口径只写「出网隔离与驱动在指定公开页面上是否按设计工作」，
不写采集成功率、事实正确率、OCR 准确率或平台适配结论。人工核查项（目标语义区域是否
读到、证据是否可定位、阻塞判定是否正确）只给机器观测值，``human_verdict`` 留空。

命令行::

    python -X utf8 -m acquisition.live_pilot --pages acquisition/fixtures/pilot/pages.json \
        [--out docs_local/evaluations/v4-live-pilot-20261113.json] [--include-baseline]

退出码：``0`` 已执行（页面失败不影响退出码，失败保留在报告里）；``1`` runner 内部异常；
``2`` 页面清单/参数错误；``3`` 未执行（真实浏览器资源或出网隔离 API 未就绪）。
"""
from __future__ import annotations

import argparse
import inspect
import ipaddress
import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from . import model
from .harness import AcquisitionHarness
from .policy import AdaptiveObservationPolicy, FixedFramePolicy, LegacyV3Policy
from .store import AcquisitionStore

PILOT_SCHEMA = 'acquisition-live-pilot-v0.1'
PAGES_SCHEMA = 'acquisition-live-pilot-pages-v0.1'

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PAGES = PROJECT_ROOT / 'acquisition' / 'fixtures' / 'pilot' / 'pages.json'
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / 'data' / 'v4' / 'acquisition' / 'pilot'

#: 证据文本写入报告的截断长度（字）；完整正文不落报告。
EVIDENCE_TEXT_LIMIT = 200
#: 单次运行最多写入的 egress 原始记录条数（聚合统计不截断）。
EGRESS_RECORD_LIMIT = 200

#: 默认策略与可选对照策略（顺序即报告顺序）。
POLICIES = ('adaptive', 'fixed_frames', 'legacy_v3')
BASELINE_POLICIES = ('fixed_frames', 'legacy_v3')

#: 与切片 2 live 验收一致的统一预算；预算只约束执行，不作为质量评分。
PILOT_BUDGET = model.Budget(max_units=24, max_captures=8, max_ocr_calls=8,
                            max_vlm_calls=0, max_steps=30)

ANSWER_KINDS = ('date', 'text', 'presence', 'count')

#: 结论口径（禁止扩写成采集成功率/事实正确率/OCR 准确率/平台适配）。
CONCLUSION_SCOPE = '出网隔离与驱动在指定公开页面上是否按设计工作'

EXIT_OK = 0
EXIT_INTERNAL_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_NOT_EXECUTED = 3

#: 外部试点禁止的 host 片段（TapTap/站内）与内网/保留域名后缀。
_FORBIDDEN_HOST_MARKERS = ('taptap',)
_FORBIDDEN_HOST_SUFFIXES = ('.localhost', '.localdomain', '.local', '.internal',
                            '.lan', '.home', '.corp', '.test', '.invalid')

_VIEWPORT_PREFIX = model.VIEWPORT_REGION_PREFIX
_DOM_BODY = model.DOM_BODY_REGION


# ---------------------------------------------------------------------------
# 页面清单（运行前冻结）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PilotPage:
    """固定页面清单中的一页；``hosts`` 即该页 driver 的 ``egress_allowlist``。"""

    page_id: str
    url: str
    hosts: tuple[str, ...]
    objective_question: str
    answer_kind: str
    region_hints: tuple[str, ...]
    note: str

    @property
    def host(self) -> str:
        return self.hosts[0]


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} 必须是非空字符串')
    return value.strip()


def _validate_public_hostname(host: str, label: str = 'host') -> str:
    """外部试点 host 必须是不含凭据/路径的公开域名；禁止 TapTap/内网/保留域名。"""
    value = str(host).strip().lower().rstrip('.')
    if not value:
        raise ValueError(f'{label} 不能为空')
    if '://' in value or '/' in value or '@' in value or ':' in value:
        raise ValueError(f'{label} 必须是纯 hostname（不含协议/路径/端口/凭据）: {host!r}')
    if any(marker in value for marker in _FORBIDDEN_HOST_MARKERS):
        raise ValueError(f'{label} 命中禁止的 TapTap/站内 host: {host!r}')
    if value in ('localhost', 'localhost.localdomain'):
        raise ValueError(f'{label} 不能是 localhost: {host!r}')
    if any(value.endswith(suffix) for suffix in _FORBIDDEN_HOST_SUFFIXES):
        raise ValueError(f'{label} 不能是内网/保留域名: {host!r}')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError(f'{label} 不能是 IP 字面量（URL 模式只接受公开域名）: {host!r}')
    return value


def _hosts_from_item(item: Mapping[str, Any], index: int) -> tuple[str, ...]:
    raw = item.get('hosts', item.get('host'))
    if isinstance(raw, str):
        candidates = [raw]
    elif isinstance(raw, list) and raw:
        candidates = raw
    else:
        raise ValueError(f'pages[{index}].host 必须是非空字符串或非空字符串数组')
    hosts: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, str):
            raise ValueError(f'pages[{index}].host 中的元素必须是字符串')
        hosts.append(_validate_public_hostname(candidate, f'pages[{index}].host'))
    if len(set(hosts)) != len(hosts):
        raise ValueError(f'pages[{index}].host 不允许重复')
    return tuple(hosts)


def _validate_page_url(url: str, hosts: tuple[str, ...], index: int) -> None:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ValueError(f'pages[{index}].url 非法: {exc}') from exc
    if parsed.scheme != 'https':
        raise ValueError(f'pages[{index}].url 必须是 https: {url!r}')
    if not parsed.hostname:
        raise ValueError(f'pages[{index}].url 缺少主机名: {url!r}')
    if parsed.username or parsed.password:
        raise ValueError(f'pages[{index}].url 不得携带凭据: {url!r}')
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f'pages[{index}].url 端口非法: {exc}') from exc
    if port not in (None, 443):
        raise ValueError(f'pages[{index}].url 只允许 443 端口: {url!r}')
    hostname = _validate_public_hostname(parsed.hostname, f'pages[{index}].url host')
    if not any(hostname == host or hostname.endswith('.' + host) for host in hosts):
        raise ValueError(
            f'pages[{index}] 的 url hostname {hostname!r} 不在 host 允许列表 {hosts!r} 内：'
            'egress_allowlist 必须覆盖该页')


def _page_from_item(item: Any, index: int) -> PilotPage:
    if not isinstance(item, Mapping):
        raise ValueError(f'pages[{index}] 必须是 JSON 对象')
    page_id = _text(item.get('page_id'), f'pages[{index}].page_id')
    url = _text(item.get('url'), f'pages[{index}].url')
    hosts = _hosts_from_item(item, index)
    question = _text(item.get('objective_question'), f'pages[{index}].objective_question')
    raw_kind = item.get('answer_kind', item.get('expected_answer_kind'))
    answer_kind = _text(raw_kind, f'pages[{index}].answer_kind')
    if answer_kind not in ANSWER_KINDS:
        raise ValueError(f'pages[{index}].answer_kind 非法: {answer_kind!r}（允许 {ANSWER_KINDS}）')
    raw_hints = item.get('region_hints', [])
    if not isinstance(raw_hints, list):
        raise ValueError(f'pages[{index}].region_hints 必须是数组')
    region_hints: list[str] = []
    for hint in raw_hints:
        if not isinstance(hint, str) or not hint.strip():
            raise ValueError(f'pages[{index}].region_hints 元素必须是非空字符串')
        value = hint.strip()
        if value not in model.REGION_ROLES:
            raise ValueError(f'pages[{index}].region_hints 含未知角色: {value!r}')
        if value in region_hints:
            raise ValueError(f'pages[{index}].region_hints 不允许重复: {value!r}')
        region_hints.append(value)
    note = item.get('note', '')
    if not isinstance(note, str):
        raise ValueError(f'pages[{index}].note 必须是字符串')
    _validate_page_url(url, hosts, index)
    return PilotPage(page_id=page_id, url=url, hosts=hosts,
                     objective_question=question, answer_kind=answer_kind,
                     region_hints=tuple(region_hints), note=note)


def load_pages(source: Any) -> tuple[PilotPage, ...]:
    """读取并校验固定页面清单；任何不合规都抛 :class:`ValueError`（不做容错替换）。"""
    path = Path(source)
    try:
        raw = path.read_text(encoding='utf-8')
    except OSError as exc:
        raise ValueError(f'无法读取页面清单 {path}: {exc}') from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f'页面清单不是合法 JSON（{path}）: {exc}') from exc
    if not isinstance(data, Mapping):
        raise ValueError('页面清单顶层必须是 JSON 对象')
    items = data.get('pages')
    if not isinstance(items, list) or not items:
        raise ValueError('页面清单 pages 必须是非空数组')
    pages = tuple(_page_from_item(item, index) for index, item in enumerate(items))
    page_ids = [page.page_id for page in pages]
    if len(page_ids) != len(set(page_ids)):
        raise ValueError(f'page_id 必须唯一: {page_ids}')
    return pages


def build_goal(page: PilotPage) -> model.ResearchGoal:
    """把页面清单项映射为冻结契约的 ResearchGoal（不含任何预期答案/正文真值）。"""
    objective = model.Objective(objective_id='pilot_fact',
                                question=page.objective_question,
                                answer_kind=page.answer_kind,  # type: ignore[arg-type]
                                region_hints=page.region_hints, required=True)
    return model.ResearchGoal(task_id=f'pilot::{page.page_id}',
                              question=page.objective_question,
                              objectives=(objective,), allowed_hosts=page.hosts,
                              policy_note='外部试点固定公开页面：只判断出网隔离与驱动是否按设计工作')


def make_policy(policy_name: str):
    if policy_name == 'adaptive':
        return AdaptiveObservationPolicy()
    if policy_name == 'fixed_frames':
        return FixedFramePolicy()
    if policy_name == 'legacy_v3':
        return LegacyV3Policy()
    raise ValueError(f'未知试点策略: {policy_name!r}')


# ---------------------------------------------------------------------------
# 环境探测：真实浏览器资源 + 出网隔离 API（冻结签名）
# ---------------------------------------------------------------------------
def egress_api_status() -> tuple[bool, str]:
    """按冻结文档 §2/§3 检查出网隔离 API 是否就绪；未就绪时试点必须 fail closed。"""
    try:
        from . import live_driver
    except Exception as exc:  # pragma: no cover - 取决于运行环境
        return False, f'live_driver_unavailable:{type(exc).__name__}:{exc}'
    driver_cls = getattr(live_driver, 'PlaywrightDriver', None)
    if driver_cls is None:
        return False, 'live_driver_missing_playwright_driver'
    try:
        parameters = inspect.signature(driver_cls.__init__).parameters
    except (TypeError, ValueError) as exc:
        return False, f'driver_signature_unavailable:{type(exc).__name__}:{exc}'
    missing = [name for name in ('allow_network', 'egress_allowlist') if name not in parameters]
    if missing:
        return False, 'driver_missing_params:' + ','.join(missing)
    if not hasattr(driver_cls, 'egress_records'):
        return False, 'driver_missing_egress_records'
    try:
        from .egress_proxy import ConnectionRecord, EgressProxy  # noqa: F401  仅检查可导入
    except Exception as exc:
        return False, f'egress_proxy_unavailable:{type(exc).__name__}:{exc}'
    return True, 'ok'


def detect_environment() -> dict[str, Any]:
    """探测真实浏览器、OCR 与出网隔离 API；``ready`` 为 False 时试点不得联网。"""
    environment: dict[str, Any] = {
        'browser': False, 'browser_reason': 'live_driver_unavailable',
        'ocr': False, 'ocr_reason': '真实浏览器不可用，未探测 OCR',
        'egress_api': False, 'egress_reason': '未探测',
        'ready': False, 'driver_source': 'acquisition.live_driver',
    }
    try:
        from . import live_driver
        available, reason = live_driver.available()
        environment['browser'] = bool(available)
        environment['browser_reason'] = str(reason or '')
        if available:
            try:
                ocr_available, ocr_reason = live_driver.ocr_available()
                environment['ocr'] = bool(ocr_available)
                environment['ocr_reason'] = '' if ocr_available else str(ocr_reason or '')
            except Exception as exc:  # pragma: no cover - 环境异常不掩埋
                environment['ocr_reason'] = f'ocr_available_error:{type(exc).__name__}:{exc}'
        else:
            environment['ocr_reason'] = '真实浏览器不可用，未探测 OCR'
    except Exception as exc:  # pragma: no cover
        environment['browser_reason'] = f'available_error:{type(exc).__name__}:{exc}'
    egress_ok, egress_reason = egress_api_status()
    environment['egress_api'] = bool(egress_ok)
    environment['egress_reason'] = str(egress_reason or '')
    environment['ready'] = bool(environment['browser']) and bool(environment['egress_api'])
    return environment


# ---------------------------------------------------------------------------
# 驱动构造：URL 模式 + 出网隔离（冻结签名）
# ---------------------------------------------------------------------------
class DriverFactory:
    """默认 driver 工厂：URL 模式 + ``allow_network`` + ``egress_allowlist``（§3）。"""

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = float(timeout)

    def __call__(self, *, page: PilotPage, artifact_dir: Path):
        from . import live_driver

        return live_driver.PlaywrightDriver(
            url=page.url,
            allow_network=True,
            egress_allowlist=tuple(page.hosts),
            artifact_dir=Path(artifact_dir),
            timeout=self.timeout,
        )


class RecordingDriver:
    """透明包装 ``PageDriver``：只记录首个 PageView，供报告核对加载状态与区域。

    不改变驱动行为，也不接触页面正文（区域只记录角色/长度，不记录文字）。
    """

    def __init__(self, driver: Any) -> None:
        self._driver = driver
        self.backend = str(getattr(driver, 'backend', 'playwright'))
        self.initial_view: model.PageView | None = None
        self.views: list[model.PageView] = []

    def open(self, goal: model.ResearchGoal) -> model.PageView:
        view = self._driver.open(goal)
        self.initial_view = view
        self.views.append(view)
        return view

    def inspect(self, view: model.PageView) -> model.Observation:
        return self._driver.inspect(view)

    def locate(self, view: model.PageView, goal: model.ResearchGoal) -> model.Observation:
        return self._driver.locate(view, goal)

    def extract(self, view: model.PageView, region: model.Region, method: str) -> model.Observation:
        return self._driver.extract(view, region, method)

    def interact(self, view: model.PageView, region: model.Region) -> model.Observation:
        return self._driver.interact(view, region)

    def close(self) -> None:
        closer = getattr(self._driver, 'close', None)
        if callable(closer):
            closer()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._driver, name)


# ---------------------------------------------------------------------------
# 摘要构造
# ---------------------------------------------------------------------------
def truncate_text(text: str, limit: int = EVIDENCE_TEXT_LIMIT) -> str:
    """把证据文本截断到 ``limit`` 字；完整正文不进入报告。"""
    if not isinstance(text, str):
        raise TypeError('text 必须为字符串')
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError('limit 必须为非负整数')
    return text[:limit]


def _record_field(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _normalise_egress_record(record: Any) -> dict[str, Any]:
    resolved = _record_field(record, 'resolved_ips', ()) or ()
    try:
        ips = [str(item) for item in resolved]
    except TypeError:
        ips = []
    def _as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
    return {
        'host': str(_record_field(record, 'host', '') or ''),
        'port': _as_int(_record_field(record, 'port', 0)),
        'resolved_ips': ips,
        'decision': str(_record_field(record, 'decision', '') or ''),
        'reason': str(_record_field(record, 'reason', '') or ''),
        'bytes_in': _as_int(_record_field(record, 'bytes_in', 0)),
        'bytes_out': _as_int(_record_field(record, 'bytes_out', 0)),
        'at': str(_record_field(record, 'at', '') or ''),
    }


def _host_allowed(host: str, allowlist: Sequence[str]) -> bool:
    value = str(host).strip().lower().rstrip('.')
    for allowed in allowlist:
        candidate = str(allowed).strip().lower().rstrip('.')
        if not candidate:
            continue
        if value == candidate or value.endswith('.' + candidate):
            return True
    return False


def _read_egress(driver: Any) -> tuple[bool, tuple[Any, ...], str]:
    """读取 ``driver.egress_records``；属性缺失时如实标记不可用，不推断隔离效果。"""
    try:
        raw = getattr(driver, 'egress_records', None)
    except Exception as exc:  # pragma: no cover - 驱动异常不掩埋
        return False, (), f'egress_records_error:{type(exc).__name__}:{exc}'
    if raw is None:
        return False, (), 'driver_missing_egress_records'
    try:
        records = tuple(raw)
    except TypeError:
        return False, (), 'egress_records_not_iterable'
    return True, records, ''


def _egress_summary(available: bool, records: Sequence[Any], allowlist: Sequence[str],
                    reason: str = '') -> dict[str, Any]:
    normalised = [_normalise_egress_record(record) for record in records]
    allowed = [item for item in normalised if item['decision'] == 'allowed']
    denied = [item for item in normalised if item['decision'] == 'denied']
    other = [item for item in normalised if item['decision'] not in ('allowed', 'denied')]
    aggregates: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in normalised:
        key = (item['host'], item['decision'], item['reason'])
        entry = aggregates.setdefault(key, {
            'host': item['host'], 'decision': item['decision'], 'reason': item['reason'],
            'count': 0, 'bytes_in': 0, 'bytes_out': 0, 'resolved_ips': [],
        })
        entry['count'] += 1
        entry['bytes_in'] += item['bytes_in']
        entry['bytes_out'] += item['bytes_out']
        for ip in item['resolved_ips']:
            if ip not in entry['resolved_ips']:
                entry['resolved_ips'].append(ip)
    violations = [item for item in allowed if not _host_allowed(item['host'], allowlist)]
    return {
        'available': bool(available),
        'reason': str(reason or ''),
        'allowlist': [str(host) for host in allowlist],
        'records_total': len(normalised),
        'records_truncated': len(normalised) > EGRESS_RECORD_LIMIT,
        'records': normalised[:EGRESS_RECORD_LIMIT],
        'aggregates': list(aggregates.values()),
        'allowed': len(allowed),
        'denied': len(denied),
        'other': len(other),
        'bytes_in_total': sum(item['bytes_in'] for item in normalised),
        'bytes_out_total': sum(item['bytes_out'] for item in normalised),
        'allowlist_violations': violations,
    }


def _launch_summary(driver: Any) -> dict[str, Any]:
    try:
        info = getattr(driver, 'launch_info', None) or {}
    except Exception:  # pragma: no cover
        info = {}
    if not isinstance(info, Mapping):
        info = {}
    keys = ('browser', 'executable', 'node', 'proxy_server', 'webrtc_policy',
            'websocket_route_blocked', 'fixture_mode', 'allow_url_mode')
    summary: dict[str, Any] = {key: info.get(key) for key in keys if key in info}
    for name in ('session_launches', 'capture_count'):
        try:
            value = getattr(driver, name, None)
        except Exception:  # pragma: no cover
            value = None
        if value is not None:
            summary[name] = value
    summary['egress_proxy_configured'] = bool(summary.get('proxy_server'))
    return summary


def _load_summary(page: PilotPage, view: model.PageView | None,
                  trace: model.TaskTrace | None, failure_stage: str | None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        'status': 'not_attempted', 'requested_url': page.url, 'final_url': '',
        'redirected': False, 'title': '', 'blocked_reason': None,
        'dom_text_length': 0, 'page_version': '', 'regions_count': 0, 'region_roles': [],
    }
    if view is not None:
        dom_text = view.dom_text or ''
        summary.update({
            'final_url': view.url,
            'redirected': bool(view.url) and view.url != page.url,
            'title': view.title,
            'blocked_reason': view.blocked_reason,
            'dom_text_length': len(dom_text),
            'page_version': view.page_version,
            'regions_count': len(view.regions),
            'region_roles': [region.role for region in view.regions],
        })
        if view.blocked_reason:
            summary['status'] = f'blocked:{view.blocked_reason}'
        elif not view.regions and not dom_text.strip():
            summary['status'] = 'empty_page'
        else:
            summary['status'] = 'loaded'
    elif trace is not None and trace.stop_reason == 'open_failed':
        summary['status'] = 'open_failed'
    elif failure_stage in ('environment', 'egress_api_not_ready'):
        summary['status'] = 'not_attempted'
    return summary


def _region_summary(view: model.PageView | None) -> dict[str, Any]:
    if view is None:
        return {'count': 0, 'roles': [], 'details': []}
    details = [{
        'region_id': region.region_id,
        'role': region.role,
        'bbox': list(region.bbox),
        'text_length': len(region.text.strip()),
        'visual_only': bool(region.visual_only),
        'lazy': bool(region.lazy),
        'requires': region.requires,
        'blocked': bool(region.blocked),
    } for region in view.regions]
    return {'count': len(view.regions),
            'roles': [region.role for region in view.regions],
            'details': details}


def _receipt_summary(receipt: model.Receipt) -> dict[str, Any]:
    return {
        'action': receipt.action,
        'region': receipt.region,
        'method': receipt.method,
        'status': receipt.status,
        'page_version': receipt.page_version,
        'cost': receipt.cost.as_dict(),
        'note': receipt.note,
        'error': receipt.error,
        'evidence_capture_ids': [item.capture_id for item in receipt.evidence],
        'at': receipt.at,
    }


def _evidence_summary(evidence: model.Evidence) -> dict[str, Any]:
    text = evidence.text or ''
    return {
        'capture_id': evidence.capture_id,
        'page_version': evidence.page_version,
        'region': evidence.region,
        'extraction': evidence.extraction,
        'text': truncate_text(text),
        'text_length': len(text),
        'text_truncated': len(text) > EVIDENCE_TEXT_LIMIT,
        'bbox': list(evidence.bbox) if evidence.bbox else None,
        'source_url': evidence.source_url,
        'verified': bool(evidence.verified),
        'note': evidence.note,
        'locator_complete': bool(evidence.capture_id and evidence.page_version
                                 and evidence.region and evidence.extraction),
    }


def _count_by(items: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or '')
        counts[value] = counts.get(value, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 单次运行（页面 × 策略）
# ---------------------------------------------------------------------------
def _best_effort_trace(store: AcquisitionStore, page: PilotPage,
                       policy_name: str) -> model.TaskTrace | None:
    """harness 异常时尽量取回已落库轨迹；取不到就如实返回 None。"""
    try:
        task = store.find_task(f'pilot::{page.page_id}')
        if task is None:
            return None
        return store.trace(task['task_id'], build_goal(page), policy=policy_name,
                           backend='playwright')
    except Exception:  # pragma: no cover - 排障辅助，不掩盖主错误
        return None


def _base_run_record(page: PilotPage, policy_name: str, run_dir: Path,
                     store_path: Path) -> dict[str, Any]:
    return {
        'page_id': page.page_id,
        'policy': policy_name,
        'skip': False,
        'skip_reason': '',
        'executed': False,
        'failure_stage': None,
        'error': None,
        'status': '',
        'stop_reason': '',
        'gap': None,
        'page_version': '',
        'page_versions': [],
        'load': {},
        'regions': {'count': 0, 'roles': [], 'details': []},
        'receipts': [],
        'receipt_status_counts': {},
        'evidence': [],
        'evidence_count': 0,
        'cost': {'units': 0, 'captures': 0, 'ocr_calls': 0, 'vlm_calls': 0},
        'steps': 0,
        'egress': _egress_summary(False, (), page.hosts, 'not_read'),
        'launch': {},
        'goal': {
            'task_id': f'pilot::{page.page_id}',
            'objective_question': page.objective_question,
            'answer_kind': page.answer_kind,
            'region_hints': list(page.region_hints),
            'egress_allowlist': list(page.hosts),
        },
        'artifacts': {'dir': str(run_dir), 'store': str(store_path)},
    }


def _run_one(page: PilotPage, policy_name: str, *, artifacts_root: Path,
             driver_factory: Any, budgets: model.Budget) -> dict[str, Any]:
    """跑一页一条策略；所有失败（构造/加载/执行/代理拒绝/预算）原样记录，不重试。"""
    run_dir = Path(artifacts_root) / f'{page.page_id}__{policy_name}'
    store_path = run_dir / 'store.sqlite3'
    record = _base_run_record(page, policy_name, run_dir, store_path)
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        record['failure_stage'] = 'runner'
        record['error'] = f'无法创建运行目录 {run_dir}: {type(exc).__name__}: {exc}'
        record['load'] = _load_summary(page, None, None, record['failure_stage'])
        return record

    driver: Any = None
    wrapper: RecordingDriver | None = None
    store: AcquisitionStore | None = None
    trace: model.TaskTrace | None = None
    egress: tuple[bool, tuple[Any, ...], str] = (False, (), 'not_read')
    try:
        try:
            driver = driver_factory(page=page, artifact_dir=run_dir)
        except Exception as exc:
            record['failure_stage'] = 'driver_construct'
            record['error'] = f'{type(exc).__name__}: {exc}'
            record['load'] = _load_summary(page, None, None, record['failure_stage'])
            record['egress'] = _egress_summary(False, (), page.hosts,
                                               'driver_not_constructed')
            return record
        wrapper = RecordingDriver(driver)
        store = AcquisitionStore(store_path)
        try:
            harness = AcquisitionHarness(wrapper, make_policy(policy_name), store,
                                         budgets=budgets)
            trace = harness.run(build_goal(page))
        except Exception as exc:
            record['failure_stage'] = 'harness'
            record['error'] = f'{type(exc).__name__}: {exc}'
            trace = _best_effort_trace(store, page, policy_name)
        record['executed'] = True
        record['launch'] = _launch_summary(wrapper)
    except Exception as exc:  # 防御：runner 自身异常也要落报告
        record['failure_stage'] = 'runner'
        record['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # pragma: no cover - 关闭失败不掩盖主结果
                pass
        if wrapper is not None:
            try:
                wrapper.close()
            except Exception:  # pragma: no cover
                pass
            # 关闭后再读 egress 记录：代理 stop() 会结束隧道并把双向字节写回 ConnectionRecord。
            try:
                egress = _read_egress(wrapper)
            except Exception:  # pragma: no cover - 记录读取失败不掩盖主结果
                pass

    view = wrapper.initial_view if wrapper is not None else None
    record['load'] = _load_summary(page, view, trace, record['failure_stage'])
    record['regions'] = _region_summary(view)
    if trace is not None:
        record['status'] = trace.status
        record['stop_reason'] = trace.stop_reason
        record['gap'] = trace.gap
        record['page_versions'] = list(trace.page_versions)
        record['receipts'] = [_receipt_summary(receipt) for receipt in trace.receipts]
        record['receipt_status_counts'] = _count_by(record['receipts'], 'status')
        record['evidence'] = [_evidence_summary(item) for item in trace.evidence]
        record['evidence_count'] = len(record['evidence'])
        record['cost'] = trace.cost.as_dict()
        record['steps'] = trace.steps
    if record['page_versions']:
        record['page_version'] = record['page_versions'][0]
    elif view is not None:
        record['page_version'] = view.page_version
    available, raw_records, reason = egress
    record['egress'] = _egress_summary(available, raw_records, page.hosts, reason)
    return record


def _skipped_run(page: PilotPage, policy_name: str, blockers: Sequence[Mapping[str, Any]],
                 artifacts_root: Path) -> dict[str, Any]:
    """未执行（环境/出网隔离 API 未就绪）：不联网，但保留页面与跳过原因。"""
    run_dir = Path(artifacts_root) / f'{page.page_id}__{policy_name}'
    record = _base_run_record(page, policy_name, run_dir, run_dir / 'store.sqlite3')
    record['skip'] = True
    record['skip_reason'] = '；'.join(f"{item.get('stage')}: {item.get('reason')}"
                                     for item in blockers) or '未执行'
    record['failure_stage'] = str(blockers[0].get('stage')) if blockers else 'not_executed'
    record['load'] = _load_summary(page, None, None, record['failure_stage'])
    record['load']['status'] = 'not_attempted'
    record['egress'] = _egress_summary(False, (), page.hosts, 'not_executed')
    return record


def _runner_error_run(page: PilotPage, policy_name: str, exc: Exception,
                      artifacts_root: Path) -> dict[str, Any]:
    run_dir = Path(artifacts_root) / f'{page.page_id}__{policy_name}'
    record = _base_run_record(page, policy_name, run_dir, run_dir / 'store.sqlite3')
    record['failure_stage'] = 'runner'
    record['error'] = f'{type(exc).__name__}: {exc}'
    record['load'] = _load_summary(page, None, None, record['failure_stage'])
    record['egress'] = _egress_summary(False, (), page.hosts, 'not_executed')
    return record


# ---------------------------------------------------------------------------
# 失败清单（全部保留）与人工核查项
# ---------------------------------------------------------------------------
def _page_failures(page: PilotPage, runs: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把一次页面运行中所有失败/缺口原样列出；不合并成成功、不重试。"""
    failures: list[dict[str, Any]] = []
    for policy_name, run in runs.items():
        base = {'policy': policy_name}
        if run.get('skip'):
            failures.append({**base, 'kind': 'not_executed',
                             'stage': run.get('failure_stage'),
                             'detail': str(run.get('skip_reason') or '未执行')})
            continue
        if run.get('failure_stage') and run.get('failure_stage') != 'harness':
            failures.append({**base, 'kind': 'run_error',
                             'stage': run.get('failure_stage'),
                             'detail': str(run.get('error') or '')})
        if run.get('failure_stage') == 'harness':
            failures.append({**base, 'kind': 'run_error', 'stage': 'harness',
                             'detail': str(run.get('error') or '')})
        load = run.get('load') or {}
        status = str(load.get('status') or '')
        if status == 'open_failed':
            note = ''
            receipts = run.get('receipts') or []
            if receipts:
                note = str(receipts[0].get('note') or '')
            failures.append({**base, 'kind': 'load_failed',
                             'detail': str(run.get('error') or note or 'driver.open 失败')})
        elif status.startswith('blocked:'):
            failures.append({**base, 'kind': 'blocked_page',
                             'blocked_reason': load.get('blocked_reason'),
                             'detail': (f"阻塞页证据必须为 0，实际证据数={run.get('evidence_count', 0)}；"
                                        f"stop_reason={run.get('stop_reason') or '-'}")})
        elif (run.get('executed') and status in ('loaded', 'empty_page')
              and (run.get('regions') or {}).get('count') == 0):
            failures.append({**base, 'kind': 'no_regions',
                             'detail': '驱动未观察到任何语义区域；adaptive 只能回退虚拟区域'
                                       '（dom:body / viewport:i），目标语义区域不可核对'})
        if (run.get('executed') and status in ('loaded', 'empty_page')
                and int(run.get('evidence_count') or 0) == 0):
            failures.append({**base, 'kind': 'no_evidence',
                             'detail': (f"证据数 0；stop_reason={run.get('stop_reason') or '-'}；"
                                        f"gap={run.get('gap') or '-'}")})
        if run.get('stop_reason') == 'budget_exhausted':
            failures.append({**base, 'kind': 'budget_exhausted',
                             'detail': '预算耗尽后停止；失败动作成本照记，未重试到成功'})
        for aggregate in (run.get('egress') or {}).get('aggregates') or []:
            if aggregate.get('decision') == 'denied':
                host = str(aggregate.get('host') or '')
                failures.append({**base, 'kind': 'egress_denied',
                                 'host': host,
                                 'reason': aggregate.get('reason'),
                                 'detail': (f"代理拒绝 host={host or '（无 host，非 CONNECT 请求）'} "
                                            f"reason={aggregate.get('reason')} "
                                            f"count={aggregate.get('count')}；按原样保留")})
    return failures


def _manual_review(page: PilotPage, runs: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """人工核查项：机器只给观测值，判定留空（``human_verdict=None``）。"""
    executed = {name: run for name, run in runs.items() if run.get('executed')}
    region_roles = sorted({role for run in executed.values()
                           for role in (run.get('regions') or {}).get('roles') or []})
    evidence_regions = sorted({str(item.get('region')) for run in executed.values()
                               for item in run.get('evidence') or []})
    hint_roles = [role for role in page.region_hints if role in region_roles]
    virtual_regions = [region for region in evidence_regions
                       if region == _DOM_BODY or region.startswith(_VIEWPORT_PREFIX)]
    if not executed:
        region_observation = 'not_executed'
    elif not region_roles:
        region_observation = 'no_regions_observed'
    elif page.region_hints and not hint_roles:
        region_observation = 'hint_roles_not_observed'
    elif page.region_hints:
        region_observation = 'hint_roles_observed'
    else:
        region_observation = 'no_region_hint_configured'
    items: list[dict[str, Any]] = [{
        'item': 'target_semantic_region_read',
        'question': '目标语义区域是否读到？',
        'observed': {
            'region_hints': list(page.region_hints),
            'observed_region_roles': region_roles,
            'hint_roles_observed': hint_roles,
            'regions_count_by_policy': {name: (run.get('regions') or {}).get('count', 0)
                                        for name, run in executed.items()},
            'evidence_regions': evidence_regions,
            'virtual_region_evidence': virtual_regions,
        },
        'machine_observation': region_observation,
        'human_verdict': None,
    }]

    locators: list[dict[str, Any]] = []
    for policy_name, run in executed.items():
        for item in run.get('evidence') or []:
            locators.append({
                'policy': policy_name,
                'capture_id': item.get('capture_id'),
                'page_version': item.get('page_version'),
                'region': item.get('region'),
                'extraction': item.get('extraction'),
                'bbox': item.get('bbox'),
                'locator_complete': bool(item.get('locator_complete')),
            })
    if not executed:
        locator_observation = 'not_executed'
    elif not locators:
        locator_observation = 'no_evidence'
    elif all(item['locator_complete'] for item in locators):
        locator_observation = 'all_fields_present'
    else:
        locator_observation = 'missing_fields'
    items.append({
        'item': 'evidence_locatable',
        'question': '证据是否可定位（page_version / region / bbox / capture_id 是否齐全）？',
        'observed': {'evidence_count': len(locators), 'locators': locators},
        'machine_observation': locator_observation,
        'human_verdict': None,
    })

    blocked = {name: {
        'blocked_reason': (run.get('load') or {}).get('blocked_reason'),
        'status': run.get('status'),
        'stop_reason': run.get('stop_reason'),
        'evidence_count': run.get('evidence_count', 0),
    } for name, run in executed.items() if (run.get('load') or {}).get('blocked_reason')}
    if not executed:
        blocking_observation = 'not_executed'
    elif blocked and all(int(item['evidence_count']) == 0 for item in blocked.values()):
        blocking_observation = 'blocked_detected_evidence_zero'
    elif blocked:
        blocking_observation = 'blocked_but_evidence_present'
    else:
        blocking_observation = 'no_block_detected'
    items.append({
        'item': 'blocking_judgement_correct',
        'question': '页面阻塞判定是否正确（captcha / login_wall / access_denied 是否误判或漏判）？',
        'observed': {
            'blocked_runs': blocked,
            'loaded_runs': {name: (run.get('load') or {}).get('status')
                            for name, run in executed.items() if name not in blocked},
            'http_status_available': False,
        },
        'machine_observation': blocking_observation,
        'human_verdict': None,
    })
    return items


# ---------------------------------------------------------------------------
# 报告汇总 / 机械检查 / 结论
# ---------------------------------------------------------------------------
def _summary(page_records: Sequence[Mapping[str, Any]],
             failures: Sequence[Mapping[str, Any]], executed: bool) -> dict[str, Any]:
    runs = [run for page in page_records for run in (page.get('runs') or {}).values()]
    executed_runs = [run for run in runs if run.get('executed')]
    kinds: dict[str, int] = {}
    for failure in failures:
        kind = str(failure.get('kind') or '')
        kinds[kind] = kinds.get(kind, 0) + 1
    return {
        'executed': bool(executed),
        'pages_total': len(page_records),
        'runs_total': len(runs),
        'runs_executed': len(executed_runs),
        'runs_skipped': len(runs) - len(executed_runs),
        'runs_loaded': sum(1 for run in executed_runs
                           if (run.get('load') or {}).get('status') == 'loaded'),
        'runs_blocked': sum(1 for run in executed_runs
                            if str((run.get('load') or {}).get('status') or '').startswith('blocked:')),
        'runs_load_failed': sum(1 for run in executed_runs
                                if (run.get('load') or {}).get('status') == 'open_failed'),
        'runs_with_evidence': sum(1 for run in executed_runs
                                  if int(run.get('evidence_count') or 0) > 0),
        'evidence_total': sum(int(run.get('evidence_count') or 0) for run in runs),
        'captures_total': sum(int((run.get('cost') or {}).get('captures') or 0) for run in runs),
        'units_total': sum(int((run.get('cost') or {}).get('units') or 0) for run in runs),
        'egress_allowed': sum(int((run.get('egress') or {}).get('allowed') or 0) for run in runs),
        'egress_denied': sum(int((run.get('egress') or {}).get('denied') or 0) for run in runs),
        'failures_total': len(failures),
        'failure_kinds': kinds,
    }


def _check(check_id: str, passed: bool, expected: str, actual: str) -> dict[str, Any]:
    return {'id': check_id, 'passed': bool(passed), 'skipped': False,
            'expected': expected, 'actual': actual}


def _skip_check(check_id: str, reason: str, expected: str) -> dict[str, Any]:
    return {'id': check_id, 'passed': None, 'skipped': True,
            'expected': expected, 'actual': 'skipped', 'reason': str(reason)}


def _mechanical_checks(page_records: Sequence[Mapping[str, Any]],
                       policies: Sequence[str], executed: bool) -> list[dict[str, Any]]:
    """运行前固定的机械检查（只核对报告事实，不评价页面质量）。"""
    checks: list[dict[str, Any]] = []
    expected_pages = '每个输入页面在报告中都保留记录（不剔除失败页）'
    if not executed:
        checks.append(_skip_check('pages_retained', '试点未执行', expected_pages))
    else:
        retained = len(page_records) == len({page.get('page_id') for page in page_records})
        checks.append(_check('pages_retained', retained, expected_pages,
                             f'{len(page_records)} 个页面记录，page_id 唯一'))

    expected_runs = f'每页每策略恰好 1 次运行（不重试到成功）：{", ".join(policies)}'
    if not executed:
        checks.append(_skip_check('single_run_per_page_policy', '试点未执行', expected_runs))
    else:
        counts = {page.get('page_id'): len(page.get('runs') or {}) for page in page_records}
        single = all(count == len(policies) for count in counts.values())
        checks.append(_check('single_run_per_page_policy', single, expected_runs,
                             f'每页运行数={sorted(set(counts.values()))}'))

    runs = [run for page in page_records for run in (page.get('runs') or {}).values()]
    executed_runs = [run for run in runs if run.get('executed')]
    expected_egress = '每个已执行运行都能读取 driver.egress_records（不推断隔离效果）'
    if not executed_runs:
        checks.append(_skip_check('egress_records_available', '没有已执行的运行', expected_egress))
    else:
        unavailable = [f"{run.get('page_id')}/{run.get('policy')}"
                       for run in executed_runs
                       if not (run.get('egress') or {}).get('available')]
        checks.append(_check('egress_records_available', not unavailable, expected_egress,
                             '全部可用' if not unavailable else '不可用：' + '、'.join(unavailable)))

    expected_allow = '允许的连接 host 必须命中该页 egress_allowlist（记录型断言）'
    violations = [f"{run.get('page_id')}/{run.get('policy')}:{item.get('host')}"
                  for run in executed_runs
                  for item in (run.get('egress') or {}).get('allowlist_violations') or []]
    if not executed_runs:
        checks.append(_skip_check('egress_allowlist_respected', '没有已执行的运行', expected_allow))
    elif all(not (run.get('egress') or {}).get('available') for run in executed_runs):
        checks.append(_skip_check('egress_allowlist_respected', 'egress 记录不可用', expected_allow))
    else:
        checks.append(_check('egress_allowlist_respected', not violations, expected_allow,
                             '无越界连接' if not violations else '越界：' + '、'.join(violations[:8])))

    expected_truncate = f'所有证据文本写入报告时不超过 {EVIDENCE_TEXT_LIMIT} 字（不提交正文）'
    if not executed_runs:
        checks.append(_skip_check('evidence_text_truncated', '没有已执行的运行', expected_truncate))
    else:
        over = [f"{run.get('page_id')}/{run.get('policy')}"
                for run in executed_runs
                for item in run.get('evidence') or []
                if len(str(item.get('text') or '')) > EVIDENCE_TEXT_LIMIT]
        checks.append(_check('evidence_text_truncated', not over, expected_truncate,
                             '全部截断到上限内' if not over else '超长：' + '、'.join(over[:8])))

    expected_verified = '所有证据 verified=False（提取成功 ≠ 事实核验）'
    if not executed_runs:
        checks.append(_skip_check('evidence_never_verified', '没有已执行的运行', expected_verified))
    else:
        verified = [f"{run.get('page_id')}/{run.get('policy')}"
                    for run in executed_runs
                    for item in run.get('evidence') or [] if item.get('verified')]
        checks.append(_check('evidence_never_verified', not verified, expected_verified,
                             '全部为 False' if not verified else '出现已核验证据：' + '、'.join(verified[:8])))

    expected_review = '每个页面都列出 3 个人工核查项（语义区域/证据定位/阻塞判定），判定留空'
    review_counts = {page.get('page_id'): len(page.get('manual_review') or [])
                     for page in page_records}
    review_ok = all(count == 3 for count in review_counts.values())
    checks.append(_check('manual_review_listed', review_ok, expected_review,
                         f'每页核查项数={sorted(set(review_counts.values()))}'))
    return checks


def _conclusion(report: Mapping[str, Any]) -> str:
    summary = report.get('summary') or {}
    environment = report.get('environment') or {}
    if not report.get('executed'):
        blockers = report.get('blockers') or []
        detail = '；'.join(f"{item.get('stage')}: {item.get('reason')}" for item in blockers)
        return (f'本次试点未执行（{detail or "前置条件未就绪"}），未产生任何外部页面结果；'
                f'结论范围仅限：{CONCLUSION_SCOPE}。')
    prefix = ''
    if report.get('synthetic'):
        prefix = ('注意：本次运行注入了测试替身驱动（synthetic=true），只验证 runner 逻辑，'
                  '不是外部试点结果。')
    return (
        f'{prefix}本次试点在指定 {summary.get("pages_total", 0)} 个公开页面上执行了 '
        f'{summary.get("runs_executed", 0)}/{summary.get("runs_total", 0)} 个运行'
        f'（策略：{", ".join(report.get("policies") or [])}）；'
        f'加载成功 {summary.get("runs_loaded", 0)}、阻塞 {summary.get("runs_blocked", 0)}、'
        f'加载失败 {summary.get("runs_load_failed", 0)}；'
        f'egress 连接记录 allowed={summary.get("egress_allowed", 0)}、'
        f'denied={summary.get("egress_denied", 0)}；'
        f'证据 {summary.get("evidence_total", 0)} 条（全部 verified=False）。'
        f'报告逐页保留全部失败（{summary.get("failures_total", 0)} 项），未重试到成功、未剔除页面。'
        f'结论范围仅限：{CONCLUSION_SCOPE}；不提供采集成功率、事实正确率、OCR 准确率或平台适配结论。'
        f'（浏览器 {environment.get("browser_reason") or environment.get("browser")!s}，'
        f'出网隔离 API {environment.get("egress_reason") or environment.get("egress_api")!s}）'
    )


def _limitations(report: Mapping[str, Any]) -> list[str]:
    limitations = [
        '驱动 PageView 契约不暴露 HTTP 状态码：4xx 中 401→login_wall、403/429→access_denied 由 worker '
        '映射；5xx 只能通过加载失败/空页/错误原样记录，报告不猜测状态码。',
        f'证据文本写入报告时截断到 {EVIDENCE_TEXT_LIMIT} 字，仅用于本地排障与人工定位；'
        '本报告不提交页面正文。',
        '证据 verified 恒为 False：提取/OCR 成功不等于事实成立，事实核验只能由独立 verifier 追加。',
        'egress 记录来自 driver.egress_records（EgressProxy ConnectionRecord）；'
        '驱动未暴露该属性时标记 available=false，不推断出网隔离效果。',
        '页面集合为运行前固定清单；页面内容变化只改变 page_version，不改清单、不重试、不换页。',
        f'结论口径固定为「{CONCLUSION_SCOPE}」：不计算采集成功率，不评估事实正确率/OCR 准确率/平台适配。',
    ]
    if report.get('synthetic'):
        limitations.append('本次运行注入了测试替身驱动（synthetic=true）：只用于验证 runner 的'
                           '报告结构与失败保留逻辑，不是外部试点结果。')
    return limitations


def _build_report(page_list: Sequence[PilotPage], pages_source: str,
                  page_records: list[dict[str, Any]], failures: list[dict[str, Any]],
                  environment: Mapping[str, Any], policies: Sequence[str],
                  budgets: model.Budget, artifacts_root: Path, executed: bool,
                  blockers: Sequence[Mapping[str, Any]], synthetic: bool) -> dict[str, Any]:
    report: dict[str, Any] = {
        'schema': PILOT_SCHEMA,
        'contract': model.SCHEMA_VERSION,
        'pages_schema': PAGES_SCHEMA,
        'generated_at': model.now_iso(),
        'pages_file': pages_source,
        'policies': list(policies),
        'budgets': {
            'max_units': budgets.max_units,
            'max_captures': budgets.max_captures,
            'max_ocr_calls': budgets.max_ocr_calls,
            'max_vlm_calls': budgets.max_vlm_calls,
            'max_steps': budgets.max_steps,
        },
        'environment': dict(environment),
        'executed': bool(executed),
        'blockers': [dict(item) for item in blockers],
        'synthetic': bool(synthetic),
        'artifacts_root': str(artifacts_root),
        'egress_allowlist': sorted({host for page in page_list for host in page.hosts}),
        'pages': page_records,
        'failures': failures,
        'summary': _summary(page_records, failures, executed),
        'manual_review_items': [
            {'item': 'target_semantic_region_read', 'question': '目标语义区域是否读到？'},
            {'item': 'evidence_locatable',
             'question': '证据是否可定位（page_version / region / bbox / capture_id 是否齐全）？'},
            {'item': 'blocking_judgement_correct',
             'question': '页面阻塞判定是否正确（captcha / login_wall / access_denied 是否误判或漏判）？'},
        ],
        'conclusion_scope': CONCLUSION_SCOPE,
    }
    report['mechanical_checks'] = _mechanical_checks(page_records, policies, executed)
    report['limitations'] = _limitations(report)
    report['conclusion'] = _conclusion(report)
    return report


# ---------------------------------------------------------------------------
# Markdown 渲染 / 落盘
# ---------------------------------------------------------------------------
def _md_cell(value: Any, limit: int = 160) -> str:
    text = str(value if value is not None else '')
    text = text.replace('\r', ' ').replace('\n', ' ').replace('|', '\\|')
    if len(text) > limit:
        text = text[:limit] + '…'
    return text


def _short_version(value: Any) -> str:
    text = str(value or '')
    return text[:23] if text else '-'


def render_markdown(report: Mapping[str, Any]) -> str:
    environment = report.get('environment') or {}
    summary = report.get('summary') or {}
    lines: list[str] = []
    lines.append('# V4 切片 3 外部试点报告（固定公开页面集合）')
    lines.append('')
    lines.append(f"- 生成时间：{report.get('generated_at', '')}")
    lines.append(f"- 页面清单：`{report.get('pages_file', '')}`（schema `{report.get('pages_schema', '')}`）")
    lines.append(f"- 契约：`{report.get('contract', '')}`；策略：{', '.join(report.get('policies') or [])}")
    budgets = report.get('budgets') or {}
    lines.append(f"- 预算：units≤{budgets.get('max_units')}、captures≤{budgets.get('max_captures')}、"
                 f"ocr≤{budgets.get('max_ocr_calls')}、vlm≤{budgets.get('max_vlm_calls')}、"
                 f"steps≤{budgets.get('max_steps')}")
    lines.append(f"- 浏览器：{'可用' if environment.get('browser') else '不可用'} "
                 f"({environment.get('browser_reason') or 'ok'})；"
                 f"OCR：{'可用' if environment.get('ocr') else '不可用'} "
                 f"({environment.get('ocr_reason') or 'ok'})；"
                 f"出网隔离 API：{'就绪' if environment.get('egress_api') else '未就绪'} "
                 f"({environment.get('egress_reason') or 'ok'})")
    lines.append(f"- 执行状态：{'已执行' if report.get('executed') else '未执行'}"
                 + ('（synthetic=true：测试替身驱动，仅验证 runner 逻辑）'
                    if report.get('synthetic') else ''))
    lines.append('')
    lines.append(f'**结论口径**：{report.get("conclusion_scope", CONCLUSION_SCOPE)}')
    lines.append('')
    lines.append('## 逐页运行结果')
    lines.append('')
    lines.append('| 页面 | 策略 | 加载状态 | page_version | 区域数 | receipts | 证据 | captures | units | stop_reason | gap | egress allowed/denied |')
    lines.append('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |')
    for page in report.get('pages') or []:
        for policy_name, run in (page.get('runs') or {}).items():
            if run.get('skip'):
                lines.append(f"| {_md_cell(page.get('page_id'), 60)} | {policy_name} | skipped | - | - | - | - | - | - | - | - | - |")
                continue
            load = run.get('load') or {}
            cost = run.get('cost') or {}
            egress = run.get('egress') or {}
            lines.append(
                f"| {_md_cell(page.get('page_id'), 60)} | {policy_name} | {_md_cell(load.get('status'), 40)} | "
                f"{_short_version(run.get('page_version'))} | {(run.get('regions') or {}).get('count', 0)} | "
                f"{len(run.get('receipts') or [])} | {run.get('evidence_count', 0)} | "
                f"{cost.get('captures', 0)} | {cost.get('units', 0)} | "
                f"{_md_cell(run.get('stop_reason'), 48) or '-'} | {_md_cell(run.get('gap'), 40) or '-'} | "
                f"{egress.get('allowed', 0)}/{egress.get('denied', 0)} |")
    lines.append('')
    lines.append('## 保留的失败（不剔除、不重试到成功、不补写结论）')
    lines.append('')
    failures = report.get('failures') or []
    if not failures:
        lines.append('- 无失败记录（注意：无失败 ≠ 结论成立，仍需人工核查）')
    for failure in failures:
        lines.append(f"- `{failure.get('page_id')}` / {failure.get('policy')} / "
                     f"**{failure.get('kind')}**：{_md_cell(failure.get('detail'), 220)}")
    lines.append('')
    lines.append('## egress 连接记录摘要（ConnectionRecord）')
    lines.append('')
    lines.append('| 页面 | 策略 | 可用 | host | decision | reason | 次数 | bytes_in | bytes_out | resolved_ips |')
    lines.append('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |')
    egress_rows = 0
    for page in report.get('pages') or []:
        for policy_name, run in (page.get('runs') or {}).items():
            egress = run.get('egress') or {}
            if not egress.get('aggregates'):
                continue
            for aggregate in egress.get('aggregates') or []:
                egress_rows += 1
                lines.append(
                    f"| {_md_cell(page.get('page_id'), 60)} | {policy_name} | "
                    f"{str(bool(egress.get('available'))).lower()} | {_md_cell(aggregate.get('host'), 60)} | "
                    f"{_md_cell(aggregate.get('decision'), 20)} | {_md_cell(aggregate.get('reason'), 60)} | "
                    f"{aggregate.get('count', 0)} | {aggregate.get('bytes_in', 0)} | "
                    f"{aggregate.get('bytes_out', 0)} | {_md_cell(', '.join(aggregate.get('resolved_ips') or []), 80)} |")
    if not egress_rows:
        lines.append('| - | - | - | - | - | - | - | - | - | 无记录（未执行或驱动未暴露 egress_records） |')
    lines.append('')
    lines.append('## 人工核查项（机器只给观测值，判定留空）')
    lines.append('')
    lines.append('| 页面 | 核查项 | 机器观测 | 观测值 | 人工判定 |')
    lines.append('| --- | --- | --- | --- | --- |')
    for page in report.get('pages') or []:
        for item in page.get('manual_review') or []:
            observed = item.get('observed') or {}
            lines.append(
                f"| {_md_cell(page.get('page_id'), 60)} | {_md_cell(item.get('question'), 80)} | "
                f"{_md_cell(item.get('machine_observation'), 40)} | "
                f"{_md_cell(json.dumps(observed, ensure_ascii=False), 400)} | 待人工填写 |")
    lines.append('')
    lines.append('## 机械检查')
    lines.append('')
    lines.append('| check | passed | skipped | expected | actual |')
    lines.append('| --- | --- | --- | --- | --- |')
    for check in report.get('mechanical_checks') or []:
        passed = 'skip' if check.get('skipped') else str(bool(check.get('passed'))).lower()
        actual = check.get('actual') if not check.get('skipped') else f"skipped: {check.get('reason')}"
        lines.append(f"| {_md_cell(check.get('id'), 60)} | {passed} | "
                     f"{str(bool(check.get('skipped'))).lower()} | {_md_cell(check.get('expected'), 160)} | "
                     f"{_md_cell(actual, 200)} |")
    lines.append('')
    lines.append('## 结论')
    lines.append('')
    lines.append(str(report.get('conclusion') or ''))
    lines.append('')
    lines.append('## 限制与口径')
    lines.append('')
    for limitation in report.get('limitations') or []:
        lines.append(f'- {limitation}')
    lines.append('')
    return '\n'.join(lines)


def output_paths(out: Any) -> tuple[Path, Path]:
    """``--out x.json`` → ``(x.json, x.md)``；无后缀时自动补 ``.json`` / ``.md``。"""
    path = Path(out)
    if path.suffix.lower() == '.json':
        return path, path.with_suffix('.md')
    if path.suffix:
        return path.with_suffix(path.suffix + '.json'), path.with_suffix('.md')
    return path.with_name(path.name + '.json'), path.with_name(path.name + '.md')


def write_report(report: Mapping[str, Any], out: Any) -> dict[str, str]:
    """把 JSON 与 Markdown 写盘（本地输出，不提交外部正文）。"""
    json_path, markdown_path = output_paths(out)
    for target in (json_path, markdown_path):
        target.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                         encoding='utf-8')
    markdown_path.write_text(render_markdown(report) + '\n', encoding='utf-8')
    return {'json': str(json_path), 'markdown': str(markdown_path)}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_pilot(pages: Any, *, out: Any = None, include_baseline: bool = False,
              artifacts_dir: Any = None, driver_factory: Any = None,
              environment: Mapping[str, Any] | None = None,
              timeout: float = 60.0, budgets: model.Budget | None = None,
              print_report: bool = True) -> dict[str, Any]:
    """在固定公开页面集合上跑一次外部试点，返回（并可落盘）完整报告。

    ``driver_factory`` 只供离线单测注入测试替身（报告会标 ``synthetic=true``）；
    真实试点永远使用 :class:`DriverFactory`（URL 模式 + 出网隔离）。
    ``environment`` 也仅供测试注入；省略时现场探测，未就绪则**不联网**并如实报告。
    """
    if isinstance(pages, (str, Path)):
        page_list = load_pages(pages)
        pages_source = str(pages)
    else:
        page_list = tuple(pages)
        if not page_list:
            raise ValueError('至少需要一个页面')
        for page in page_list:
            if not isinstance(page, PilotPage):
                raise TypeError('pages 必须是 PilotPage 序列或 pages.json 路径')
        pages_source = '<内存页面清单>'

    policies = ('adaptive',) + (BASELINE_POLICIES if include_baseline else ())
    budget = PILOT_BUDGET if budgets is None else budgets
    if not isinstance(budget, model.Budget):
        raise TypeError('budgets 必须是 model.Budget')
    environment_dict: dict[str, Any] = dict(environment) if environment is not None else detect_environment()
    ready = bool(environment_dict.get('browser')) and bool(environment_dict.get('egress_api'))
    blockers: list[dict[str, Any]] = []
    if not environment_dict.get('browser'):
        blockers.append({'stage': 'environment',
                         'reason': str(environment_dict.get('browser_reason')
                                       or '真实浏览器资源不可用')})
    if not environment_dict.get('egress_api'):
        blockers.append({'stage': 'egress_api_not_ready',
                         'reason': str(environment_dict.get('egress_reason')
                                       or '出网隔离 API 未就绪')})
    synthetic = driver_factory is not None
    if synthetic:
        environment_dict['driver_source'] = 'injected_test_double'

    if artifacts_dir is not None:
        artifacts_root = Path(artifacts_dir)
    else:
        artifacts_root = DEFAULT_ARTIFACT_ROOT / (
            f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}")
    if ready:
        artifacts_root.mkdir(parents=True, exist_ok=True)
    factory = driver_factory if synthetic else DriverFactory(timeout=timeout)

    page_records: list[dict[str, Any]] = []
    all_failures: list[dict[str, Any]] = []
    for page in page_list:
        runs: dict[str, dict[str, Any]] = {}
        for policy_name in policies:
            if not ready:
                runs[policy_name] = _skipped_run(page, policy_name, blockers, artifacts_root)
            else:
                try:
                    runs[policy_name] = _run_one(page, policy_name,
                                                 artifacts_root=artifacts_root,
                                                 driver_factory=factory, budgets=budget)
                except Exception as exc:  # 防御：单次运行异常也要落报告
                    runs[policy_name] = _runner_error_run(page, policy_name, exc,
                                                          artifacts_root)
        page_failures = _page_failures(page, runs)
        all_failures.extend({'page_id': page.page_id, **item} for item in page_failures)
        page_records.append({
            'page_id': page.page_id,
            'url': page.url,
            'hosts': list(page.hosts),
            'objective_question': page.objective_question,
            'answer_kind': page.answer_kind,
            'region_hints': list(page.region_hints),
            'note': page.note,
            'runs': runs,
            'failures': page_failures,
            'manual_review': _manual_review(page, runs),
        })

    report = _build_report(page_list, pages_source, page_records, all_failures,
                           environment_dict, policies, budget, artifacts_root,
                           ready, blockers, synthetic)
    if out is not None:
        report['outputs'] = write_report(report, out)
    if print_report:
        print(render_markdown(report))
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m acquisition.live_pilot',
        description='V4 切片 3 外部试点：固定公开页面集合上的出网隔离与驱动机制运行报告'
                    '（保留全部失败，不写采集成功率/事实正确率/OCR 准确率/平台适配结论）')
    parser.add_argument('--pages', default=str(DEFAULT_PAGES),
                        help='固定页面清单 JSON（默认 acquisition/fixtures/pilot/pages.json）')
    parser.add_argument('--out', default=None,
                        help='可选：JSON 输出路径；同名 .md 同时写出（默认只打印 Markdown）')
    parser.add_argument('--include-baseline', action='store_true',
                        help='额外运行 fixed_frames / legacy_v3 对照（默认只跑 adaptive）')
    parser.add_argument('--artifacts-dir', default=None,
                        help='截图/store 落盘目录（默认 data/v4/acquisition/pilot/run-<时间戳>，'
                             '已被 .gitignore 忽略）')
    parser.add_argument('--timeout', type=float, default=60.0,
                        help='单次 worker 请求超时秒数（默认 60）')
    return parser


def main(argv: Any = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        load_pages(args.pages)
    except (ValueError, OSError) as exc:
        print(f'错误：{exc}')
        return EXIT_CONFIG_ERROR
    try:
        report = run_pilot(
            args.pages,
            out=args.out,
            include_baseline=bool(args.include_baseline),
            artifacts_dir=args.artifacts_dir,
            timeout=float(args.timeout),
        )
    except (ValueError, OSError) as exc:
        print(f'错误：{exc}')
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # pragma: no cover - runner 内部异常必须显式失败
        print(f'错误：试点 runner 内部异常：{type(exc).__name__}: {exc}')
        return EXIT_INTERNAL_ERROR
    return EXIT_OK if report.get('executed') else EXIT_NOT_EXECUTED


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
