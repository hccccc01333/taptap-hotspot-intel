"""V4 切片 2 本地真实浏览器验收 runner（真实 Chromium/Edge 上的机制验证）。

验收对象是 ``acquisition/fixtures/live/`` 下的 **本地合成 HTML 场景**，在真实
Chromium/Edge（Node + Playwright 持久会话 worker）里运行三条策略：

- ``fixed_frames``：被替换的固定三屏截图基线；
- ``legacy_v3``：现状 V3 行为基线（``dom:body`` + 固定三屏 OCR）；
- ``adaptive``：动态观察策略。

每个场景 × 每条策略使用独立的 ``PlaywrightDriver`` + ``AcquisitionHarness`` +
临时 SQLite，采集指标后计算运行前固定的一组检查项。资源缺失（node / playwright /
Edge / 本地 Windows OCR）时，对应用例与检查标记 ``skipped`` 并写清原因，命令退出 0：
**不把 skip 当作通过，也不用假结果冒充真实浏览器运行。**

结论口径（写入输出 JSON 的 ``conclusion`` 字段）：**只能**表述为「本地合成 HTML、
真实 Chromium/Edge 上的机制验证」，不能表述为真实网站采集成功率、OCR 准确率、
事实正确性或增长效果。命中 ``expected_answer`` 只是机制命中，不等于事实成立；
证据的 ``verified`` 始终为 ``False``。

命令行::

    python -X utf8 -m acquisition.live_acceptance --fixtures acquisition/fixtures/live [--out FILE]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import struct
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import model
from .harness import AcquisitionHarness
from .policy import AdaptiveObservationPolicy, FixedFramePolicy, LegacyV3Policy
from .store import AcquisitionStore

LIVE_SCHEMA = 'acquisition-live-acceptance-v0.1'

#: 三种对照策略（顺序即输出顺序）。
POLICIES = ('fixed_frames', 'legacy_v3', 'adaptive')

#: 与冻结文档 §5 一致：每场景每策略独立 driver/store，使用统一预算。
LIVE_BUDGET = model.Budget(max_units=24, max_captures=8, max_ocr_calls=8,
                           max_vlm_calls=0, max_steps=30)

#: 夹具文件头必须声明的口径。
FIXTURE_HEADER = '本地合成场景，不是真实站点'

#: 结论口径（禁止扩写成真实网站收益/OCR 准确率）。
CONCLUSION = (
    '机制结论（仅限本地合成 HTML 场景）：在真实 Chromium/Edge 持久浏览器会话上，'
    '动态观察策略能点击展开懒加载规则、对纯图片区域定点截图并 OCR，而固定三屏/现状 V3 '
    '基线在相同场景漏读；验证码页与登录墙缺口由执行层按规则拦截。这是本地合成页面上的'
    '机制验证，不代表真实网站采集成功率、OCR 准确率、事实正确性或增长效果。'
)

_WHITESPACE_RE = re.compile(r'\s+')
_CAPTURE_PREFIX = 'capture_'


# ---------------------------------------------------------------------------
# 场景定义（运行前固定：夹具、目标、预期文本与语义区域）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LiveScenario:
    """一个本地合成 HTML 场景及其冻结的验收目标。"""

    page_id: str
    filename: str
    label: str
    question: str
    objective_id: str
    answer_kind: str
    region_hints: tuple[str, ...]
    expected_answer: str
    expected_region_role: str
    blocked_reason: str | None = None
    #: 该场景的检查是否依赖本地 Windows OCR。
    requires_ocr: bool = False


SCENARIOS: tuple[LiveScenario, ...] = (
    LiveScenario(
        page_id='static_article',
        filename='static_article.html',
        label='静态文章页：规则与截止日期在 DOM 正文首屏',
        question='确认合成活动「海风与灯塔」的报名截止日期',
        objective_id='deadline',
        answer_kind='date',
        region_hints=('article_body',),
        expected_answer='2026年11月12日',
        expected_region_role='article_body',
    ),
    LiveScenario(
        page_id='lazy_rules',
        filename='lazy_rules.html',
        label='懒加载规则：规则折叠在按钮后、位于长页下方',
        question='确认合成活动「雾港回声」的报名截止日期',
        objective_id='deadline',
        answer_kind='date',
        region_hints=('activity_rules',),
        expected_answer='2026年11月20日',
        expected_region_role='activity_rules',
    ),
    LiveScenario(
        page_id='poster_text',
        filename='poster_text.html',
        label='纯图片海报：截止日期只画在图片里、位于第三屏之后',
        question='确认合成活动「星轨远航」海报中的截止日期',
        objective_id='deadline',
        answer_kind='date',
        region_hints=('poster',),
        expected_answer='2026年11月30日',
        expected_region_role='poster',
        requires_ocr=True,
    ),
    LiveScenario(
        page_id='captcha',
        filename='captcha.html',
        label='验证码阻塞页：任何策略都不得产出证据',
        question='确认合成活动「海风与灯塔」的报名截止日期',
        objective_id='deadline',
        answer_kind='date',
        region_hints=('activity_rules',),
        expected_answer='',
        expected_region_role='captcha',
        blocked_reason='captcha',
    ),
    LiveScenario(
        page_id='login_comments',
        filename='login_comments.html',
        label='登录墙评论区：只记录缺口、不伪造评论证据',
        question='确认合成活动「潮汐留言板」评论区里的玩家反馈内容',
        objective_id='comments_signal',
        answer_kind='text',
        region_hints=('comments',),
        expected_answer='限定徽章预约已开启',
        expected_region_role='comments',
    ),
)

SCENARIO_BY_FILENAME = {scenario.filename: scenario for scenario in SCENARIOS}
SCENARIO_BY_ID = {scenario.page_id: scenario for scenario in SCENARIOS}


def scenario_goal(scenario: LiveScenario) -> model.ResearchGoal:
    """把场景定义映射为冻结契约里的 ResearchGoal（不含任何预期答案信息）。"""
    objective = model.Objective(
        objective_id=scenario.objective_id,
        question=scenario.question,
        answer_kind=scenario.answer_kind,  # type: ignore[arg-type]
        region_hints=scenario.region_hints,
        required=True,
    )
    return model.ResearchGoal(task_id=f'live::{scenario.page_id}',
                              question=scenario.question,
                              objectives=(objective,),
                              policy_note='本地合成 HTML 场景验收，不是真实站点')


def _make_policy(policy_name: str):
    if policy_name == 'fixed_frames':
        return FixedFramePolicy()
    if policy_name == 'legacy_v3':
        return LegacyV3Policy()
    if policy_name == 'adaptive':
        return AdaptiveObservationPolicy()
    raise ValueError(f'未知 live 验收策略: {policy_name!r}')


# ---------------------------------------------------------------------------
# 环境探测（node / Playwright / Edge / 本地 Windows OCR）
# ---------------------------------------------------------------------------
def _probe_png() -> bytes:
    """生成一张纯白 PNG，用于探测本地 Windows OCR 运行时是否可用。

    只用标准库（zlib/struct）生成，不引入额外依赖；空白图也能判断 OCR 引擎是否
    能创建，从而把「OCR 缺失 → poster 检查 skip」落到实处。
    """
    width, height = 400, 200
    raw = b''.join(b'\x00' + b'\xff\xff\xff' * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xffffffff)

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 9))
            + chunk(b'IEND', b''))


def _probe_ocr() -> tuple[bool, str, str]:
    """探测本地 OCR。返回 ``(可用, 原因, 引擎名)``；不修改 agent_v3。"""
    try:
        from . import live_driver
    except ImportError:
        live_driver = None  # type: ignore[assignment]
    if live_driver is not None:
        probe = getattr(live_driver, 'ocr_available', None)
        if callable(probe):
            try:
                available, reason = probe()
                return bool(available), str(reason or ''), 'live_driver.ocr_available'
            except Exception as exc:  # pragma: no cover - 环境异常不掩埋
                return False, f'{type(exc).__name__}: {exc}', 'live_driver.ocr_available'
    folder = Path(tempfile.mkdtemp(prefix='acq_live_ocr_probe_'))
    try:
        probe_path = folder / 'probe.png'
        probe_path.write_bytes(_probe_png())
        try:
            from agent_v3.browser_tools import ocr as legacy_ocr  # 只读复用现有 Windows OCR
        except Exception as exc:  # pragma: no cover - Linux CI 无 PowerShell
            return False, f'OCR 模块不可用: {type(exc).__name__}: {exc}', ''
        try:
            result = legacy_ocr(probe_path)
        except Exception as exc:  # pragma: no cover
            return False, f'OCR 探测失败: {type(exc).__name__}: {exc}', ''
        if isinstance(result, dict) and result.get('status') == 'ok':
            return True, '', str(result.get('engine') or 'windows_ocr')
        detail = ''
        if isinstance(result, dict):
            detail = str(result.get('error') or result.get('status') or '')
        return False, detail or 'OCR 引擎不可用', ''
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def detect_environment() -> dict[str, Any]:
    """探测真实浏览器与本地 OCR 是否就绪，返回可序列化的环境描述。"""
    environment: dict[str, Any] = {
        'browser': False,
        'ocr': False,
        'reason': '',
        'ocr_reason': '',
        'ocr_engine': '',
    }
    try:
        from . import live_driver
    except Exception as exc:
        environment['reason'] = f'live_driver 未就绪: {type(exc).__name__}: {exc}'
        environment['ocr_reason'] = '真实浏览器不可用，未探测 OCR'
        return environment
    try:
        available, reason = live_driver.available()
    except Exception as exc:  # pragma: no cover - 环境异常不掩埋
        environment['reason'] = f'available() 异常: {type(exc).__name__}: {exc}'
        environment['ocr_reason'] = '真实浏览器不可用，未探测 OCR'
        return environment
    environment['browser'] = bool(available)
    environment['reason'] = str(reason or '')
    if not environment['browser']:
        environment['ocr_reason'] = '真实浏览器不可用，未探测 OCR'
        return environment
    environment.update(_probe_browser_info())
    blocked, blocked_detail = _probe_fixture_websocket()
    environment['fixture_websocket_blocked'] = blocked
    environment['fixture_websocket_detail'] = blocked_detail
    ocr_ok, ocr_reason, engine = _probe_ocr()
    environment['ocr'] = bool(ocr_ok)
    environment['ocr_reason'] = '' if ocr_ok else str(ocr_reason or '未知原因')
    environment['ocr_engine'] = engine if ocr_ok else ''
    return environment


def _probe_browser_info() -> dict[str, Any]:
    """记录浏览器/可执行文件/Node 版本，避免验收结论无法脱离本机复核。"""
    from . import live_driver

    folder = tempfile.mkdtemp(prefix='acq_live_env_')
    transport = None
    try:
        transport = live_driver.ProcessTransport(artifact_dir=Path(folder), timeout=60)
        transport.start()
        result = transport.call({
            'command': 'launch', 'fixture_mode': True, 'viewport': [1440, 1000],
            'artifact_dir': str(Path(folder)),
        }, timeout=60)
        return {key: result.get(key) for key in
                ('browser', 'executable', 'node', 'websocket_route_blocked')}
    except Exception as exc:
        return {'browser_info_error': f'{type(exc).__name__}: {exc}'}
    finally:
        try:
            if transport is not None:
                transport.close()
        except Exception:
            pass
        shutil.rmtree(folder, ignore_errors=True)


def _probe_fixture_websocket() -> tuple[bool | None, str]:
    """真实探测夹具模式是否拦截页面 WebSocket；返回 (是否拦截, 说明)。

    夹具模板是公开参数，必须在真实浏览器里验证"禁网"对 WebSocket 也成立，而不是
    只相信 HTTP route。探测失败不伪装成通过：返回 None，由检查项标记 skip。
    """
    import socket as socket_module

    from . import live_driver

    listener = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    listener.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1)
    listener.settimeout(1.5)
    folder = tempfile.mkdtemp(prefix='acq_live_ws_')
    driver = None
    try:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        html = ('<!doctype html><html><head><meta charset="utf-8"><title>ws 负例</title></head>'
                '<body><main><p>本地合成 WebSocket 负例，不是真实站点。</p></main>'
                f'<script>try{{new WebSocket("ws://127.0.0.1:{port}/ws");}}catch(e){{}}</script>'
                '</body></html>')
        goal = model.ResearchGoal('live_ws_negative', 'WebSocket 负例',
                                  (model.Objective('ws', 'WebSocket 负例', 'presence',
                                                   ('article_body',)),))
        driver = live_driver.PlaywrightDriver(
            fixture_html=html, artifact_dir=Path(folder) / 'captures', timeout=30)
        driver.open(goal)
        try:
            listener.settimeout(1.5)
            connection, _address = listener.accept()
        except socket_module.timeout:
            connection = None
        if connection is not None:
            connection.close()
            return False, '收到 WebSocket 连接（夹具禁网失效）'
        if not driver.launch_info.get('websocket_route_blocked'):
            return True, '未收到连接（routeWebSocket 不可用，由页面内 init script 禁用通道）'
        return True, '未收到连接（routeWebSocket + 页面内禁用双重拦截）'
    except Exception as exc:
        return None, f'{type(exc).__name__}: {exc}'
    finally:
        try:
            if driver is not None:
                driver.close()
        except Exception:
            pass
        listener.close()
        shutil.rmtree(folder, ignore_errors=True)


def _check_fixture_websocket_blocked(environment: dict) -> dict:
    """夹具模式的负例检查：页面尝试 WebSocket 时不得建立到本机监听端口的连接。"""
    check_id = 'fixture_mode_no_websocket'
    expected = '夹具模式下页面 WebSocket 不得建立到本机监听端口的连接'
    if not environment.get('browser'):
        return _skip(check_id, f"真实浏览器不可用：{environment.get('reason') or '未知原因'}",
                     expected)
    blocked = environment.get('fixture_websocket_blocked')
    detail = str(environment.get('fixture_websocket_detail') or '')
    if blocked is None:
        return _skip(check_id, f'WebSocket 封锁探测失败：{detail}', expected)
    return _check(check_id, blocked is True, expected, detail)


# ---------------------------------------------------------------------------
# 截图观测：透明包装 WorkerTransport，记录 worker 的 screenshot 响应
# ---------------------------------------------------------------------------
class RecordingTransport:
    """``WorkerTransport`` 的透明包装：记录请求/响应，供验收核对截图文件与 sha256。

    只依赖冻结文档 §3 的 ``WorkerTransport`` 协议（start/call/close/alive），
    不修改 worker、driver 的任何行为；真正的截图校验（路径与哈希）仍由 driver 完成，
    这里做验收侧的第二道核对，避免「driver 说截了图」而无独立检查。
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.commands: list[dict] = []
        self.responses: list[Any] = []

    def start(self) -> None:
        self._inner.start()

    def call(self, command: dict, timeout: float = 30) -> dict:
        result = self._inner.call(command, timeout=timeout)
        self.commands.append(dict(command))
        self.responses.append(result)
        return result

    def close(self) -> None:
        self._inner.close()

    @property
    def alive(self) -> bool:
        return bool(self._inner.alive)


class _LiveDriverFactory:
    """默认 driver 工厂：``ProcessTransport`` + ``RecordingTransport`` + ``PlaywrightDriver``。

    通过 ``last_recorder`` 把截图响应记录暴露给 :func:`run_scenario`；自定义
    ``driver_factory``（测试注入）不需要这个属性，此时截图检查会如实体现在结果里。
    """

    def __init__(self, *, ocr_engine: Callable[[Path], dict] | None = None) -> None:
        self.ocr_engine = ocr_engine
        self.last_recorder: RecordingTransport | None = None
        self.last_driver = None

    def __call__(self, *, fixture_html: str, artifact_dir: Path):
        from . import live_driver

        artifact_dir = Path(artifact_dir)
        transport = RecordingTransport(live_driver.ProcessTransport(artifact_dir=artifact_dir))
        kwargs: dict[str, Any] = {
            'fixture_html': fixture_html,
            'artifact_dir': artifact_dir,
            'transport': transport,
        }
        if self.ocr_engine is not None:
            kwargs['ocr_engine'] = self.ocr_engine
        driver = live_driver.PlaywrightDriver(**kwargs)
        self.last_recorder = transport
        self.last_driver = driver
        return driver


# ---------------------------------------------------------------------------
# 单场景运行
# ---------------------------------------------------------------------------
def _normalise(text: str) -> str:
    """去空白后比较，避免 OCR 转录的换行/空格影响「命中预期文本」判断。"""
    return _WHITESPACE_RE.sub('', text or '')


def _payload(response: Any) -> dict | None:
    """取 WorkerTransport 返回的结果字典；兼容误传整包响应的实现。"""
    if not isinstance(response, dict):
        return None
    nested = response.get('result')
    if isinstance(nested, dict) and 'file' in nested:
        return nested
    return response


def _inside_artifact_dir(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except OSError:  # pragma: no cover - 路径异常按不通过处理
        return False
    return resolved == root_resolved or root_resolved in resolved.parents


def _validate_screenshots(recorder: RecordingTransport | None, artifact_dir: Path,
                          trace: model.TaskTrace) -> tuple[list[dict], str]:
    """核对 worker 报告的每张截图：文件存在、sha256 一致、位于 artifact_dir 内。"""
    if recorder is None:
        return [], 'unavailable'
    root = Path(artifact_dir)
    records: list[dict] = []
    for command, response in zip(recorder.commands, recorder.responses):
        if not isinstance(command, dict) or command.get('command') != 'screenshot':
            continue
        payload = _payload(response)
        if not isinstance(payload, dict):
            records.append({'file': '', 'reported_sha256': '', 'actual_sha256': '',
                            'exists': False, 'sha256_match': False,
                            'inside_artifact_dir': False, 'expected_capture_id': ''})
            continue
        reported_sha = str(payload.get('sha256') or '')
        reported_file = payload.get('file')
        path: Path | None = None
        if reported_file:
            path = Path(str(reported_file))
            if not path.is_absolute():
                path = root / path
        exists = bool(path is not None and path.is_file())
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest() if exists and path is not None else ''
        records.append({
            # 只保留文件名：artifact_dir 是临时目录、运行结束即清理，绝对路径在结果里会失效。
            'file': path.name if path is not None else '',
            'reported_sha256': reported_sha,
            'actual_sha256': actual_sha,
            'exists': exists,
            'sha256_match': bool(reported_sha) and actual_sha == reported_sha,
            'inside_artifact_dir': bool(path is not None and _inside_artifact_dir(path, root)),
            'expected_capture_id': (_CAPTURE_PREFIX + reported_sha[:24]) if reported_sha else '',
        })
    # OCR 证据的 capture_id 必须能从截图 sha256 推导出来（capture_ + sha[:24]）。
    expected_ids = {record['expected_capture_id'] for record in records if record['expected_capture_id']}
    ocr_capture_ids = [item.capture_id for item in trace.evidence if item.extraction == 'ocr']
    capture_ids_ok = all(capture_id in expected_ids for capture_id in ocr_capture_ids)
    return records, ('worker_transport' if capture_ids_ok else 'capture_id_mismatch')


def _inspect_view_details(view: model.PageView) -> dict[str, dict[str, Any]]:
    """记录初始视图的区域角色与视觉提示，供 located/visual_only 检查使用。"""
    details: dict[str, dict[str, Any]] = {}
    for region in view.regions:
        details[region.region_id] = {
            'role': region.role,
            'visual_only': bool(getattr(region, 'visual_only', False)),
            'text_length': len(region.text.strip()),
            'requires': region.requires,
            'lazy': region.lazy,
        }
    return details


def _metrics_from_trace(scenario: LiveScenario, trace: model.TaskTrace,
                        region_details: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """把一次真实运行整理成检查项使用的指标（命中只是机制命中，不是事实成立）。"""
    expected = _normalise(scenario.expected_answer)
    matched = ([item for item in trace.evidence if expected in _normalise(item.text)]
               if expected else [])
    roles = {region_id: detail['role'] for region_id, detail in region_details.items()}
    answer_hit = bool(matched)
    located_hit = any(roles.get(item.region) == scenario.expected_region_role for item in matched)
    cost = trace.cost
    within_budget = (cost.units <= LIVE_BUDGET.max_units
                     and cost.captures <= LIVE_BUDGET.max_captures
                     and cost.ocr_calls <= LIVE_BUDGET.max_ocr_calls
                     and cost.vlm_calls <= LIVE_BUDGET.max_vlm_calls
                     and trace.steps <= LIVE_BUDGET.max_steps)
    return {
        'answer_hit': answer_hit,
        'located_hit': located_hit,
        'evidence_count': len(trace.evidence),
        'captures': cost.captures,
        'ocr_calls': cost.ocr_calls,
        'vlm_calls': cost.vlm_calls,
        'units': cost.units,
        'steps': trace.steps,
        'interacts': sum(1 for receipt in trace.receipts
                         if receipt.action == 'interact' and receipt.status == 'completed'),
        'stop_reason': trace.stop_reason,
        'gap': trace.gap,
        'status': trace.status,
        'evidence_verified': all(not item.verified for item in trace.evidence),
        'within_budget': within_budget,
    }


def run_scenario(page_path, policy_name: str, *, driver_factory=None) -> dict:
    """在本地 HTML 夹具上跑一条策略，返回可序列化的指标、截图与证据摘要。

    ``driver_factory`` 供离线测试注入假驱动，调用约定为
    ``driver_factory(fixture_html=..., artifact_dir=...) -> PageDriver``；默认使用
    ``acquisition.live_driver`` 的真实 ``ProcessTransport`` + ``PlaywrightDriver``。

    图片/OCR 资源缺失不是本函数的职责：调用方（:func:`run_acceptance`）先探测环境，
    决定哪些场景应跳过；本函数在资源缺失时会如实记录 driver 的失败原因。
    """
    page_path = Path(page_path)
    if policy_name not in POLICIES:
        raise ValueError(f'未知 live 验收策略: {policy_name!r}')
    scenario = SCENARIO_BY_FILENAME.get(page_path.name)
    if scenario is None:
        raise ValueError(f'未知 live 夹具文件: {page_path.name}')
    html = page_path.read_text(encoding='utf-8')
    goal = scenario_goal(scenario)
    artifact_dir = Path(tempfile.mkdtemp(prefix=f'acq_live_{scenario.page_id}_{policy_name}_'))
    result: dict[str, Any] = {
        'page_id': scenario.page_id,
        'policy': policy_name,
        'fixture': str(page_path),
        'skipped': False,
        'skip_reason': '',
        'error': None,
        'initial_page_version': '',
        'page_versions': [],
        'region_details': {},
        'screenshots': [],
        'screenshot_source': 'unavailable',
        'metrics': {},
        'evidence': [],
        'receipts': [],
    }
    driver = None
    store: AcquisitionStore | None = None
    try:
        factory = driver_factory if driver_factory is not None else _LiveDriverFactory()
        driver = factory(fixture_html=html, artifact_dir=artifact_dir)
        recorder = getattr(factory, 'last_recorder', None)
        store = AcquisitionStore(artifact_dir / 'store.sqlite3')
        view = driver.open(goal)
        result['initial_page_version'] = view.page_version
        result['region_details'] = _inspect_view_details(view)
        harness = AcquisitionHarness(driver, _make_policy(policy_name), store,
                                     budgets=LIVE_BUDGET)
        trace = harness.run(goal)
        screenshots, screenshot_source = _validate_screenshots(recorder, artifact_dir, trace)
        result['screenshots'] = screenshots
        result['screenshot_source'] = screenshot_source
        result['metrics'] = _metrics_from_trace(scenario, trace, result['region_details'])
        result['page_versions'] = list(trace.page_versions)
        result['evidence'] = [
            {'capture_id': item.capture_id, 'page_version': item.page_version,
             'region': item.region, 'extraction': item.extraction,
             'text': item.text, 'verified': item.verified}
            for item in trace.evidence
        ]
        result['receipts'] = [
            {'action': receipt.action, 'region': receipt.region, 'method': receipt.method,
             'status': receipt.status, 'page_version': receipt.page_version,
             'cost': receipt.cost.as_dict(), 'note': receipt.note,
             'evidence_capture_ids': [item.capture_id for item in receipt.evidence]}
            for receipt in trace.receipts
        ]
        return result
    except Exception as exc:  # 真实运行失败必须如实报告，不能伪装成 skip
        result['error'] = f'{type(exc).__name__}: {exc}'
        return result
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # pragma: no cover - 关闭失败不掩盖主结果
                pass
        if driver is not None:
            try:
                driver.close()
            except Exception:  # pragma: no cover - 关闭失败不掩盖主结果
                pass
        shutil.rmtree(artifact_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 确定性 / 会话复用
# ---------------------------------------------------------------------------
def _run_page_versions(page_path: Path, driver_factory=None) -> dict:
    """重新跑一次 adaptive，只返回初始版本与轨迹版本，用于稳定性检查。"""
    outcome = run_scenario(page_path, 'adaptive', driver_factory=driver_factory)
    if outcome.get('error'):
        return {'page_id': outcome['page_id'], 'ok': False,
                'reason': outcome['error'], 'initial_page_version': '',
                'page_versions': []}
    if outcome.get('skipped'):
        return {'page_id': outcome['page_id'], 'ok': False,
                'reason': outcome.get('skip_reason', ''), 'initial_page_version': '',
                'page_versions': []}
    return {'page_id': outcome['page_id'], 'ok': True, 'reason': '',
            'initial_page_version': outcome['initial_page_version'],
            'page_versions': outcome['page_versions']}


def _check_page_version_stability(first_runs: dict, fixtures_dir: Path,
                                  environment: dict, *, driver_factory=None) -> dict[str, Any]:
    """对每个实际运行过的场景重跑一次 adaptive，比较 page_version 是否稳定。"""
    comparisons: list[dict] = []
    for scenario in SCENARIOS:
        first = first_runs.get(scenario.page_id, {}).get('adaptive')
        if not environment.get('browser'):
            comparisons.append({'page_id': scenario.page_id, 'ok': False, 'skipped': True,
                                'reason': '真实浏览器不可用'})
            continue
        if first is None or first.get('skipped') or first.get('error'):
            comparisons.append({'page_id': scenario.page_id, 'ok': False, 'skipped': True,
                                'reason': '首轮没有可比较的运行结果'})
            continue
        second = _run_page_versions(fixtures_dir / scenario.filename, driver_factory)
        stable = (second.get('ok')
                  and second.get('initial_page_version') == first.get('initial_page_version')
                  and second.get('page_versions') == first.get('page_versions'))
        comparisons.append({
            'page_id': scenario.page_id,
            'ok': bool(stable),
            'skipped': False,
            'reason': '' if stable else str(second.get('reason') or '两次运行的 page_version 不一致'),
            'first_initial': first.get('initial_page_version', ''),
            'second_initial': second.get('initial_page_version', ''),
            'first_versions': list(first.get('page_versions') or []),
            'second_versions': list(second.get('page_versions') or []),
        })
    return {'comparisons': comparisons, 'performed': bool(environment.get('browser'))}


def _run_session_reuse_check(fixtures_dir: Path, environment: dict,
                             *, driver_factory=None) -> dict[str, Any]:
    """同一 driver 连续 navigate 两个本地夹具后，浏览器 launch 次数仍必须为 1。"""
    if not environment.get('browser'):
        return {'performed': False, 'ok': False, 'reason': '真实浏览器不可用',
                'session_launches': None, 'versions': []}
    static_html = (fixtures_dir / SCENARIO_BY_ID['static_article'].filename).read_text(encoding='utf-8')
    lazy_html = (fixtures_dir / SCENARIO_BY_ID['lazy_rules'].filename).read_text(encoding='utf-8')
    goal = scenario_goal(SCENARIO_BY_ID['static_article'])
    artifact_dir = Path(tempfile.mkdtemp(prefix='acq_live_session_reuse_'))
    driver = None
    try:
        factory = driver_factory if driver_factory is not None else _LiveDriverFactory()
        driver = factory(fixture_html=static_html, artifact_dir=artifact_dir)
        first = driver.open(goal)
        second = driver.navigate(fixture_html=lazy_html)
        launches = int(getattr(driver, 'session_launches', -1))
        ok = (launches == 1 and bool(first.page_version) and bool(second.page_version)
              and first.page_version != second.page_version)
        return {'performed': True, 'ok': ok, 'reason': '',
                'session_launches': launches,
                'versions': [first.page_version, second.page_version]}
    except Exception as exc:  # 真跑失败必须如实记录
        return {'performed': True, 'ok': False,
                'reason': f'{type(exc).__name__}: {exc}',
                'session_launches': None, 'versions': []}
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:  # pragma: no cover
                pass
        shutil.rmtree(artifact_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 检查项（运行前固定；资源缺失时标 skipped）
# ---------------------------------------------------------------------------
def _check(check_id: str, passed: bool, expected: str, actual: str,
           detail: str = '') -> dict[str, Any]:
    return {'id': check_id, 'passed': bool(passed), 'skipped': False, 'reason': '',
            'expected': expected, 'actual': actual, 'detail': detail}


def _fail(check_id: str, expected: str, actual: str, detail: str = '') -> dict[str, Any]:
    return {'id': check_id, 'passed': False, 'skipped': False, 'reason': '',
            'expected': expected, 'actual': actual, 'detail': detail}


def _skip(check_id: str, reason: str, expected: str = '',
          detail: str = '') -> dict[str, Any]:
    return {'id': check_id, 'passed': None, 'skipped': True, 'reason': str(reason),
            'expected': expected, 'actual': 'skipped', 'detail': detail}


def _gate(runs: dict, page_id: str, policies: tuple[str, ...], environment: dict,
          *, require_ocr: bool = False) -> tuple[str, str]:
    """统一处理资源缺失 / 场景跳过 / 运行失败：返回 ``(ok|skip|fail, 原因)``。"""
    if not environment.get('browser'):
        return 'skip', f"真实浏览器不可用：{environment.get('reason') or '未知原因'}"
    if require_ocr and not environment.get('ocr'):
        return 'skip', f"本地 Windows OCR 不可用：{environment.get('ocr_reason') or '未知原因'}"
    page_runs = runs.get(page_id) or {}
    for policy in policies:
        outcome = page_runs.get(policy)
        if outcome is None:
            return 'fail', f'缺少 {page_id}/{policy} 的运行结果'
        if outcome.get('skipped'):
            return 'skip', f"{page_id}/{policy} 跳过：{outcome.get('skip_reason') or '未知原因'}"
        if outcome.get('error'):
            return 'fail', f"{page_id}/{policy} 运行失败：{outcome['error']}"
    return 'ok', ''


def _metric(runs: dict, page_id: str, policy: str) -> dict:
    return ((runs.get(page_id) or {}).get(policy) or {}).get('metrics') or {}


def _executed_runs(runs: dict) -> list[tuple[str, str, dict]]:
    """真正执行过（非 skipped、非 error）的运行，供「无实际运行」时统一 skip。"""
    return [(page_id, policy, outcome)
            for page_id, page_runs in runs.items()
            for policy, outcome in page_runs.items()
            if not outcome.get('skipped') and not outcome.get('error')]


def _all_skipped_reason(runs: dict) -> str:
    reasons = sorted({str(outcome.get('skip_reason'))
                      for page_runs in runs.values()
                      for outcome in page_runs.values()
                      if outcome.get('skipped') and outcome.get('skip_reason')})
    return reasons[0] if reasons else '所有场景/策略运行均被跳过'


def _simple_check(check_id: str, expected: str, gate: tuple[str, str],
                  compute: Callable[[], tuple[bool, str, str]], *, detail: str = '') -> dict:
    """按 gate 结果执行检查；compute 返回 ``(passed, actual, detail)``。"""
    if gate[0] == 'skip':
        return _skip(check_id, gate[1], expected, detail=detail)
    if gate[0] == 'fail':
        return _fail(check_id, expected, gate[1], detail=detail)
    passed, actual, extra = compute()
    return _check(check_id, passed, expected, actual, detail=(detail + extra))


# ---- 单个检查项 ------------------------------------------------------------
def _check_fixtures_complete(fixtures_dir: Path) -> dict:
    expected_files = [scenario.filename for scenario in SCENARIOS]
    missing = [name for name in expected_files if not (fixtures_dir / name).is_file()]
    extra = sorted(path.name for path in fixtures_dir.glob('*.html')
                   if path.name not in expected_files) if fixtures_dir.is_dir() else []
    passed = not missing and not extra
    actual = '5 个必需 live 夹具齐全' if passed else (
        ('缺少：' + '、'.join(missing) if missing else '')
        + ('；多余：' + '、'.join(extra) if extra else ''))
    return _check('live_fixtures_complete', passed,
                  f'夹具目录恰好包含 {len(expected_files)} 个本地合成 HTML：{"、".join(expected_files)}',
                  actual)


def _check_browser_available(environment: dict) -> dict:
    if environment.get('browser'):
        return _check('live_browser_available', True, '真实浏览器（node + Playwright + Edge）可用',
                      f"available()={environment.get('reason') or 'ok'}")
    return _skip('live_browser_available',
                 f"真实浏览器不可用：{environment.get('reason') or '未知原因'}",
                 '真实浏览器（node + Playwright + Edge）可用')


def _check_ocr_available(environment: dict) -> dict:
    if environment.get('ocr'):
        return _check('live_ocr_available', True, '本地 Windows OCR 可用',
                      f"engine={environment.get('ocr_engine') or 'windows_ocr'}")
    reason = environment.get('ocr_reason') or 'OCR 不可用'
    return _skip('live_ocr_available', f'本地 Windows OCR 不可用：{reason}',
                 '本地 Windows OCR 可用')


def _check_static_hit(runs: dict, environment: dict) -> dict:
    check_id = 'static_all_policies_hit'
    expected = 'static_article 上 fixed_frames / legacy_v3 / adaptive 均命中预期日期'
    gate = _gate(runs, 'static_article', POLICIES, environment, require_ocr=True)

    def compute():
        hits = {policy: bool(_metric(runs, 'static_article', policy).get('answer_hit'))
                for policy in POLICIES}
        passed = all(hits.values())
        return passed, json.dumps(hits, ensure_ascii=False), ''

    return _simple_check(check_id, expected, gate, compute)


def _check_static_units(runs: dict, environment: dict) -> dict:
    check_id = 'static_adaptive_units_below_legacy'
    expected = 'static_article 上 adaptive.units < legacy_v3.units'
    gate = _gate(runs, 'static_article', ('adaptive', 'legacy_v3'), environment)

    def compute():
        adaptive = int(_metric(runs, 'static_article', 'adaptive').get('units') or 0)
        legacy = int(_metric(runs, 'static_article', 'legacy_v3').get('units') or 0)
        return adaptive < legacy, f'adaptive {adaptive} vs legacy_v3 {legacy}', ''

    return _simple_check(check_id, expected, gate, compute,
                         detail='units 是资源约束指标，不作为质量评分')


def _check_lazy_hit(runs: dict, environment: dict) -> dict:
    check_id = 'lazy_adaptive_hit'
    expected = 'lazy_rules 上 adaptive 点击展开后命中规则截止日期'
    gate = _gate(runs, 'lazy_rules', ('adaptive',), environment)

    def compute():
        metrics = _metric(runs, 'lazy_rules', 'adaptive')
        return (bool(metrics.get('answer_hit')),
                f"answer_hit={metrics.get('answer_hit')}, stop_reason={metrics.get('stop_reason')}", '')

    return _simple_check(check_id, expected, gate, compute)


def _check_lazy_baselines_miss(runs: dict, environment: dict) -> dict:
    check_id = 'lazy_baselines_miss'
    expected = 'lazy_rules 上 fixed_frames 与 legacy_v3 都未命中规则截止日期'
    gate = _gate(runs, 'lazy_rules', ('fixed_frames', 'legacy_v3'), environment)

    def compute():
        hits = {policy: bool(_metric(runs, 'lazy_rules', policy).get('answer_hit'))
                for policy in ('fixed_frames', 'legacy_v3')}
        return not any(hits.values()), json.dumps(hits, ensure_ascii=False), ''

    return _simple_check(check_id, expected, gate, compute)


def _check_lazy_interacted(runs: dict, environment: dict) -> dict:
    check_id = 'lazy_adaptive_interacted'
    expected = 'lazy_rules 上 adaptive 至少执行一次 interact（点击展开按钮）'
    gate = _gate(runs, 'lazy_rules', ('adaptive',), environment)

    def compute():
        interacts = int(_metric(runs, 'lazy_rules', 'adaptive').get('interacts') or 0)
        return interacts >= 1, f'interacts={interacts}', ''

    return _simple_check(check_id, expected, gate, compute)


def _check_lazy_version_changed(runs: dict, environment: dict) -> dict:
    check_id = 'lazy_page_version_changed'
    expected = 'lazy_rules 上 adaptive 轨迹出现 ≥2 个页面版本（点击展开改变 page_version）'
    gate = _gate(runs, 'lazy_rules', ('adaptive',), environment)

    def compute():
        versions = (runs.get('lazy_rules') or {}).get('adaptive', {}).get('page_versions') or []
        return len(versions) >= 2, f'page_versions={len(versions)}', ''

    return _simple_check(check_id, expected, gate, compute)


def _check_lazy_located(runs: dict, environment: dict) -> dict:
    check_id = 'lazy_adaptive_located'
    expected = 'lazy_rules 上 adaptive 命中证据可定位到 activity_rules 语义区域'
    gate = _gate(runs, 'lazy_rules', ('adaptive',), environment)

    def compute():
        metrics = _metric(runs, 'lazy_rules', 'adaptive')
        passed = bool(metrics.get('answer_hit')) and bool(metrics.get('located_hit'))
        return passed, f"located_hit={metrics.get('located_hit')}", ''

    return _simple_check(check_id, expected, gate, compute)


def _check_poster_ocr_hit(runs: dict, environment: dict) -> dict:
    check_id = 'poster_adaptive_ocr_hit'
    expected = 'poster_text 上 adaptive 对纯图片海报定点截图 + OCR 命中截止日期'
    gate = _gate(runs, 'poster_text', ('adaptive',), environment, require_ocr=True)

    def compute():
        metrics = _metric(runs, 'poster_text', 'adaptive')
        passed = bool(metrics.get('answer_hit')) and int(metrics.get('ocr_calls') or 0) >= 1
        return passed, (f"answer_hit={metrics.get('answer_hit')}, ocr_calls={metrics.get('ocr_calls')}, "
                        f"captures={metrics.get('captures')}"), ''

    return _simple_check(check_id, expected, gate, compute,
                         detail='OCR 是转录，不是事实核验')


def _check_poster_baselines_miss(runs: dict, environment: dict) -> dict:
    check_id = 'poster_baselines_miss'
    expected = 'poster_text 上 fixed_frames 与 legacy_v3 都未命中（第三屏之外 + DOM 无日期）'
    gate = _gate(runs, 'poster_text', ('fixed_frames', 'legacy_v3'), environment,
                 require_ocr=True)

    def compute():
        hits = {policy: bool(_metric(runs, 'poster_text', policy).get('answer_hit'))
                for policy in ('fixed_frames', 'legacy_v3')}
        return not any(hits.values()), json.dumps(hits, ensure_ascii=False), ''

    return _simple_check(check_id, expected, gate, compute)


def _check_poster_visual_only(runs: dict, environment: dict) -> dict:
    check_id = 'poster_region_visual_only'
    expected = 'poster_text 的 poster 区域 visual_only=True 且可读文字为空/极短（视觉区域不伪造文本）'
    gate = _gate(runs, 'poster_text', ('adaptive',), environment, require_ocr=True)

    def compute():
        details = (runs.get('poster_text') or {}).get('adaptive', {}).get('region_details') or {}
        posters = [detail for detail in details.values() if detail.get('role') == 'poster']
        if not posters:
            return False, '初始视图中没有 role=poster 的区域', ''
        poster = posters[0]
        short_text = int(poster.get('text_length') or 0) < 120
        passed = bool(poster.get('visual_only')) and short_text
        return passed, (f"visual_only={poster.get('visual_only')}, "
                        f"text_length={poster.get('text_length')}"), ''

    return _simple_check(check_id, expected, gate, compute)


def _check_captcha_no_evidence(runs: dict, environment: dict) -> dict:
    check_id = 'captcha_no_evidence'
    expected = 'captcha 上三条策略证据数均为 0（阻塞页证据不得入库）'
    gate = _gate(runs, 'captcha', POLICIES, environment)

    def compute():
        counts = {policy: int(_metric(runs, 'captcha', policy).get('evidence_count') or 0)
                  for policy in POLICIES}
        return all(count == 0 for count in counts.values()), json.dumps(counts), ''

    return _simple_check(check_id, expected, gate, compute,
                         detail='固定基线可能在阻塞前消耗截图成本，但证据不得入库')


def _check_captcha_zero_captures(runs: dict, environment: dict) -> dict:
    check_id = 'captcha_adaptive_zero_captures'
    expected = 'captcha 上 adaptive inspect 后立即停止，captures=0'
    gate = _gate(runs, 'captcha', ('adaptive',), environment)

    def compute():
        metrics = _metric(runs, 'captcha', 'adaptive')
        captures = int(metrics.get('captures') or 0)
        stop = str(metrics.get('stop_reason') or '')
        return captures == 0 and 'captcha' in stop, f'captures={captures}, stop_reason={stop}', ''

    return _simple_check(check_id, expected, gate, compute)


def _check_login_gap(runs: dict, environment: dict) -> dict:
    check_id = 'login_required_gap'
    expected = 'login_comments 上 adaptive 记录 login_required 缺口、证据数为 0'
    gate = _gate(runs, 'login_comments', ('adaptive',), environment)

    def compute():
        metrics = _metric(runs, 'login_comments', 'adaptive')
        passed = (metrics.get('gap') == 'login_required'
                  and int(metrics.get('evidence_count') or 0) == 0)
        return passed, (f"gap={metrics.get('gap')}, evidence={metrics.get('evidence_count')}, "
                        f"stop_reason={metrics.get('stop_reason')}"), ''

    return _simple_check(check_id, expected, gate, compute)


def _check_login_no_fabricated(runs: dict, environment: dict) -> dict:
    check_id = 'login_no_fabricated_comments'
    expected = 'login_comments 上任何策略都不命中评论预期文本（不得伪造评论证据）'
    gate = _gate(runs, 'login_comments', POLICIES, environment)

    def compute():
        hits = {policy: bool(_metric(runs, 'login_comments', policy).get('answer_hit'))
                for policy in POLICIES}
        return not any(hits.values()), json.dumps(hits, ensure_ascii=False), ''

    return _simple_check(check_id, expected, gate, compute)


def _check_screenshots(runs: dict, environment: dict) -> dict:
    check_id = 'screenshots_exist_and_sha256'
    expected = 'worker 报告的每张截图文件存在、sha256 一致、位于 artifact_dir 内，且 OCR 证据 capture_id 可回溯'
    if not environment.get('browser'):
        return _skip(check_id, f"真实浏览器不可用：{environment.get('reason') or '未知原因'}", expected)
    if not _executed_runs(runs):
        return _skip(check_id, _all_skipped_reason(runs), expected)
    records: list[dict] = []
    failures: list[str] = []
    sources: set[str] = set()
    for page_id, page_runs in runs.items():
        for policy, outcome in page_runs.items():
            if outcome.get('skipped') or outcome.get('error'):
                continue
            sources.add(str(outcome.get('screenshot_source') or 'unavailable'))
            for record in outcome.get('screenshots') or []:
                label = f"{page_id}/{policy}"
                if not record.get('exists'):
                    failures.append(f'{label}: 文件不存在 {record.get("file")}')
                if not record.get('sha256_match'):
                    failures.append(f'{label}: sha256 不一致')
                if not record.get('inside_artifact_dir'):
                    failures.append(f'{label}: 文件不在 artifact_dir 内')
                records.append(record)
    if not records:
        return _fail(check_id, expected, '没有观测到任何截图响应',
                     detail=f'screenshot_sources={sorted(sources)}')
    if 'unavailable' in sources or 'capture_id_mismatch' in sources:
        failures.append(f'截图观测能力异常：{sorted(sources)}')
    passed = not failures
    actual = (f'{len(records)} 张截图全部校验通过' if passed
              else '；'.join(failures[:6]) + ('…' if len(failures) > 6 else ''))
    return _check(check_id, passed, expected, actual)


def _check_page_version_current(runs: dict, environment: dict) -> dict:
    check_id = 'evidence_page_version_current'
    expected = ('每条证据的 page_version 与其产生收据的页面版本一致，且属于该轨迹观察到的版本；'
                '旧版本证据不得冒充当前页面事实')
    if not environment.get('browser'):
        return _skip(check_id, f"真实浏览器不可用：{environment.get('reason') or '未知原因'}", expected)
    if not _executed_runs(runs):
        return _skip(check_id, _all_skipped_reason(runs), expected)
    mismatches: list[str] = []
    checked = 0
    for page_id, page_runs in runs.items():
        for policy, outcome in page_runs.items():
            if outcome.get('skipped') or outcome.get('error'):
                continue
            versions = list(outcome.get('page_versions') or [])
            initial = outcome.get('initial_page_version') or ''
            if not versions and initial:
                versions = [initial]
            allowed = set(versions)
            # 收据是证据的来源：证据版本必须与产生它的收据版本一致。
            produced_by: dict[str, str] = {}
            for receipt in outcome.get('receipts') or []:
                for capture_id in receipt.get('evidence_capture_ids') or []:
                    produced_by[capture_id] = receipt.get('page_version') or ''
            for item in outcome.get('evidence') or []:
                checked += 1
                page_version = item.get('page_version') or ''
                if page_version not in allowed:
                    mismatches.append(f'{page_id}/{policy}: 版本 {page_version[:20]} 不在轨迹中')
                    continue
                origin = produced_by.get(str(item.get('capture_id') or ''))
                if origin is not None and origin != page_version:
                    mismatches.append(
                        f'{page_id}/{policy}: 证据版本 {page_version[:20]} != 来源收据版本 {origin[:20]}')
    passed = not mismatches
    actual = (f'{checked} 条证据均绑定到轨迹版本与来源收据' if passed
              else '；'.join(mismatches[:6]))
    return _check(check_id, passed, expected, actual)


def _check_evidence_never_verified(runs: dict, environment: dict) -> dict:
    check_id = 'evidence_never_verified'
    expected = '所有运行中证据 verified 均为 False（提取成功 ≠ 事实核验）'
    if not environment.get('browser'):
        return _skip(check_id, f"真实浏览器不可用：{environment.get('reason') or '未知原因'}", expected)
    if not _executed_runs(runs):
        return _skip(check_id, _all_skipped_reason(runs), expected)
    verified = [f'{page_id}/{policy}'
                for page_id, page_runs in runs.items()
                for policy, outcome in page_runs.items()
                if not outcome.get('skipped') and not outcome.get('error')
                and any(item.get('verified') for item in outcome.get('evidence') or [])]
    passed = not verified
    actual = '全部为 False' if passed else '出现已核验证据：' + '、'.join(verified)
    return _check(check_id, passed, expected, actual)


def _check_page_version_stable(determinism: dict, environment: dict) -> dict:
    check_id = 'page_version_stable_across_runs'
    expected = '同一场景两次独立运行（新 driver/store）的 page_version 序列完全一致'
    if not environment.get('browser'):
        return _skip(check_id, f"真实浏览器不可用：{environment.get('reason') or '未知原因'}",
                     expected)
    comparisons = determinism.get('comparisons') or []
    compared = [item for item in comparisons if not item.get('skipped')]
    if not compared:
        return _skip(check_id, '没有可比较的两次运行结果', expected)

    def _stable(item: dict) -> bool:
        # 不盲信预先算好的 ok：直接复核初始版本与完整版本序列，防止比较逻辑退化。
        return (bool(item.get('ok'))
                and item.get('first_initial') == item.get('second_initial')
                and list(item.get('first_versions') or []) == list(item.get('second_versions') or []))

    unstable = [item['page_id'] for item in compared if not _stable(item)]
    passed = not unstable
    actual = (f'{len(compared)} 个场景两次运行版本一致' if passed
              else '不稳定：' + '、'.join(unstable))
    return _check(check_id, passed, expected, actual)


def _check_session_reuse(session_reuse: dict, environment: dict) -> dict:
    check_id = 'browser_session_reuse'
    expected = '同一 driver 连续 navigate 两个夹具后 session_launches 仍为 1'
    if not environment.get('browser'):
        return _skip(check_id, f"真实浏览器不可用：{environment.get('reason') or '未知原因'}",
                     expected)
    if not session_reuse.get('performed'):
        return _skip(check_id, str(session_reuse.get('reason') or '未执行会话复用检查'), expected)
    if not session_reuse.get('ok'):
        return _fail(check_id, expected, f"复用检查失败：{session_reuse.get('reason') or '未知原因'}",
                     detail=f"versions={session_reuse.get('versions')}")
    return _check(check_id, True, expected,
                  f"session_launches={session_reuse.get('session_launches')}, "
                  f"两个夹具版本不同")


def _check_budgets(runs: dict, environment: dict) -> dict:
    check_id = 'budgets_respected'
    expected = ('所有运行 units/captures/ocr/vlm/steps 不超 '
                f'{LIVE_BUDGET.max_units}/{LIVE_BUDGET.max_captures}/'
                f'{LIVE_BUDGET.max_ocr_calls}/{LIVE_BUDGET.max_vlm_calls}/'
                f'{LIVE_BUDGET.max_steps}')
    if not environment.get('browser'):
        return _skip(check_id, f"真实浏览器不可用：{environment.get('reason') or '未知原因'}",
                     expected, detail=json.dumps(LIVE_BUDGET.__dict__, ensure_ascii=False))
    if not _executed_runs(runs):
        return _skip(check_id, _all_skipped_reason(runs), expected,
                     detail=json.dumps(LIVE_BUDGET.__dict__, ensure_ascii=False))
    over = [f'{page_id}/{policy}'
            for page_id, page_runs in runs.items()
            for policy, outcome in page_runs.items()
            if not outcome.get('skipped') and not outcome.get('error')
            and not (outcome.get('metrics') or {}).get('within_budget')]
    passed = not over
    return _check(check_id, passed, expected,
                  '全部在预算内' if passed else '超预算：' + '、'.join(over),
                  detail=json.dumps(LIVE_BUDGET.__dict__, ensure_ascii=False))


def compute_checks(runs: dict, environment: dict, determinism: dict,
                   session_reuse: dict, fixtures_dir: Path) -> list[dict]:
    """计算运行前固定的一组检查项（顺序固定，便于逐项对照历史结果）。"""
    fixtures_dir = Path(fixtures_dir)
    return [
        _check_fixtures_complete(fixtures_dir),
        _check_browser_available(environment),
        _check_ocr_available(environment),
        _check_fixture_websocket_blocked(environment),
        _check_static_hit(runs, environment),
        _check_static_units(runs, environment),
        _check_lazy_hit(runs, environment),
        _check_lazy_baselines_miss(runs, environment),
        _check_lazy_interacted(runs, environment),
        _check_lazy_version_changed(runs, environment),
        _check_lazy_located(runs, environment),
        _check_poster_ocr_hit(runs, environment),
        _check_poster_baselines_miss(runs, environment),
        _check_poster_visual_only(runs, environment),
        _check_captcha_no_evidence(runs, environment),
        _check_captcha_zero_captures(runs, environment),
        _check_login_gap(runs, environment),
        _check_login_no_fabricated(runs, environment),
        _check_screenshots(runs, environment),
        _check_page_version_current(runs, environment),
        _check_evidence_never_verified(runs, environment),
        _check_page_version_stable(determinism, environment),
        _check_session_reuse(session_reuse, environment),
        _check_budgets(runs, environment),
    ]


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def _summary(checks: list[dict], environment: dict) -> dict:
    failed = [check for check in checks if not check['skipped'] and check['passed'] is False]
    passed = [check for check in checks if not check['skipped'] and check['passed']]
    skipped = [check for check in checks if check['skipped']]
    return {
        'checks_total': len(checks),
        'checks_passed': len(passed),
        'checks_failed': len(failed),
        'checks_skipped': len(skipped),
        'all_passed': not failed,
        'live_ran': bool(environment.get('browser')),
        'failed_check_ids': [check['id'] for check in failed],
        'skipped_check_ids': [check['id'] for check in skipped],
    }


def _metric_row(run: dict) -> str:
    metrics = run.get('metrics') or {}
    if run.get('skipped'):
        return f"| - | - | - | - | - | - | - | skipped | - |"
    if run.get('error'):
        return f"| - | - | - | - | - | - | - | error | - |"
    return (f"| {str(metrics.get('answer_hit')).lower()} | {str(metrics.get('located_hit')).lower()} | "
            f"{metrics.get('evidence_count', 0)} | {metrics.get('captures', 0)} | "
            f"{metrics.get('ocr_calls', 0)} | {metrics.get('units', 0)} | "
            f"{metrics.get('steps', 0)} | {metrics.get('stop_reason') or '-'} | "
            f"{metrics.get('gap') or '-'} |")


def _markdown_table(result: dict) -> str:
    lines = ['| 场景 | 策略 | answer_hit | located_hit | 证据 | captures | ocr | units | steps | stop_reason | gap |',
             '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for scenario in result['scenarios']:
        for policy in POLICIES:
            run = scenario['runs'].get(policy) or {'skipped': True}
            lines.append(f"| {scenario['page_id']} | {policy} " + _metric_row(run))
    lines.append('')
    lines.append('| check | passed | skipped | expected | actual |')
    lines.append('| --- | --- | --- | --- | --- |')
    for check in result['checks']:
        passed = 'skip' if check['skipped'] else str(bool(check['passed'])).lower()
        actual = check['actual'] if not check['skipped'] else f"skipped: {check['reason']}"
        lines.append(f"| {check['id']} | {passed} | {str(check['skipped']).lower()} | "
                     f"{check['expected']} | {actual} |")
    return '\n'.join(lines)


def _print_report(result: dict) -> None:
    environment = result['environment']
    print('# V4 切片 2 本地真实浏览器验收（本地合成 HTML 场景）')
    print()
    print(f"夹具目录：`{result['fixtures_dir']}`；契约：`{result['contract']}`；"
          f"策略：{', '.join(result['policies'])}")
    print(f"浏览器：{'可用' if environment.get('browser') else '不可用（' + str(environment.get('reason')) + '）'}；"
          f"OCR：{'可用' if environment.get('ocr') else '不可用（' + str(environment.get('ocr_reason')) + '）'}")
    print()
    print(_markdown_table(result))
    print()
    summary = result['summary']
    print(f"checks：{summary['checks_passed']} passed / {summary['checks_failed']} failed / "
          f"{summary['checks_skipped']} skipped（共 {summary['checks_total']}，"
          f"all_passed={summary['all_passed']}）")
    print()
    print(f"结论：{result['conclusion']}")


def _write_json(result: dict, out) -> None:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _run_matrix(fixtures_dir: Path, environment: dict,
                driver_factory=None) -> tuple[dict, list[dict]]:
    """按「场景 × 策略」运行（资源缺失的场景/用例标 skipped），返回 runs 与 scenarios 输出。"""
    runs: dict[str, dict[str, dict]] = {}
    scenarios_out: list[dict] = []
    for scenario in SCENARIOS:
        page_path = fixtures_dir / scenario.filename
        runs[scenario.page_id] = {}
        scenario_out = {'page_id': scenario.page_id, 'label': scenario.label,
                        'fixture': str(page_path), 'blocked_reason': scenario.blocked_reason,
                        'requires_ocr': scenario.requires_ocr, 'runs': {}}
        for policy in POLICIES:
            if not environment.get('browser'):
                reason = f"真实浏览器不可用：{environment.get('reason') or '未知原因'}"
                outcome = {'page_id': scenario.page_id, 'policy': policy,
                           'fixture': str(page_path), 'skipped': True,
                           'skip_reason': reason, 'error': None, 'metrics': {},
                           'page_versions': [], 'screenshots': [],
                           'screenshot_source': 'unavailable', 'region_details': {},
                           'initial_page_version': '', 'evidence': [], 'receipts': []}
            elif not page_path.is_file():
                outcome = {'page_id': scenario.page_id, 'policy': policy,
                           'fixture': str(page_path), 'skipped': True,
                           'skip_reason': f'夹具文件缺失: {page_path.name}',
                           'error': None, 'metrics': {}, 'page_versions': [],
                           'screenshots': [], 'screenshot_source': 'unavailable',
                           'region_details': {}, 'initial_page_version': '',
                           'evidence': [], 'receipts': []}
            elif scenario.requires_ocr and not environment.get('ocr'):
                outcome = {'page_id': scenario.page_id, 'policy': policy,
                           'fixture': str(page_path), 'skipped': True,
                           'skip_reason': (f"本地 Windows OCR 不可用："
                                           f"{environment.get('ocr_reason') or '未知原因'}"),
                           'error': None, 'metrics': {}, 'page_versions': [],
                           'screenshots': [], 'screenshot_source': 'unavailable',
                           'region_details': {}, 'initial_page_version': '',
                           'evidence': [], 'receipts': []}
            else:
                outcome = run_scenario(page_path, policy, driver_factory=driver_factory)
                if outcome.get('error') and str(outcome['error']).startswith('BrowserUnavailable'):
                    # 环境探测通过但启动浏览器仍失败（例如资源在被探测后消失）：按资源缺失
                    # 标 skipped 而不是伪装成「策略失败」，但仍如实写明原因。
                    outcome = {'page_id': scenario.page_id, 'policy': policy,
                               'fixture': str(page_path), 'skipped': True,
                               'skip_reason': f"真实浏览器资源不可用：{outcome['error']}",
                               'error': None, 'metrics': {}, 'page_versions': [],
                               'screenshots': [], 'screenshot_source': 'unavailable',
                               'region_details': {}, 'initial_page_version': '',
                               'evidence': [], 'receipts': []}
            runs[scenario.page_id][policy] = outcome
            scenario_out['runs'][policy] = outcome
        scenarios_out.append(scenario_out)
    return runs, scenarios_out


def run_acceptance(fixtures_dir, *, out=None) -> dict:
    """在本地真实浏览器上跑完整验收矩阵，输出 JSON 结果并打印 Markdown 报告。

    资源缺失（node/playwright/edge/OCR）时对应用例与检查标 ``skipped``，命令返回 0；
    真实运行失败则如实记为 failed（``main`` 返回非 0）。
    """
    fixtures_dir = Path(fixtures_dir)
    if not fixtures_dir.is_dir():
        raise FileNotFoundError(f'夹具目录不存在: {fixtures_dir}')
    environment = detect_environment()
    runs, scenarios_out = _run_matrix(fixtures_dir, environment)
    determinism = _check_page_version_stability(runs, fixtures_dir, environment)
    session_reuse = _run_session_reuse_check(fixtures_dir, environment)
    checks = compute_checks(runs, environment, determinism, session_reuse, fixtures_dir)
    summary = _summary(checks, environment)
    result = {
        'schema': LIVE_SCHEMA,
        'contract': model.SCHEMA_VERSION,
        'generated_at': model.now_iso(),
        'fixtures_dir': str(fixtures_dir),
        'policies': list(POLICIES),
        'budgets': {'max_units': LIVE_BUDGET.max_units,
                    'max_captures': LIVE_BUDGET.max_captures,
                    'max_ocr_calls': LIVE_BUDGET.max_ocr_calls,
                    'max_vlm_calls': LIVE_BUDGET.max_vlm_calls,
                    'max_steps': LIVE_BUDGET.max_steps},
        'environment': environment,
        'scenarios': scenarios_out,
        'determinism': determinism,
        'session_reuse': session_reuse,
        'checks': checks,
        'summary': summary,
        'conclusion': CONCLUSION,
    }
    if out is not None:
        _write_json(result, out)
    _print_report(result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='V4 切片 2 本地真实浏览器验收（本地合成 HTML 场景，机制验证）')
    parser.add_argument('--fixtures', default='acquisition/fixtures/live',
                        help='本地合成 HTML 夹具目录（默认 acquisition/fixtures/live）')
    parser.add_argument('--out', default=None,
                        help='可选：把完整 JSON 结果写入文件（同时打印 Markdown 报告）')
    args = parser.parse_args(argv)
    try:
        result = run_acceptance(Path(args.fixtures), out=Path(args.out) if args.out else None)
    except FileNotFoundError as exc:
        print(f'错误：{exc}')
        return 2
    # 只有真实运行失败才返回非 0；资源缺失导致的 skip 不当作失败（要求退出 0）。
    return 0 if result['summary']['all_passed'] else 1


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
