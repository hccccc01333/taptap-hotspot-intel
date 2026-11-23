"""真实浏览器持久会话驱动（V4 切片 2，``acquisition-contract-v0.1`` 的 PageDriver 实现）。

V4 切片 3 增加出网隔离：URL 模式必须显式 ``allow_network=True`` + 非空
``egress_allowlist``，driver 自动启动本地 :class:`acquisition.egress_proxy.EgressProxy`
并让 Chromium 只能经它出网；``egress_records`` 暴露实际连接 host/IP/拒绝原因。

一次 ``launch`` 启动 Node worker + Playwright + 本机 Edge 浏览器；之后所有
``open`` / ``load_fixture`` / ``observe`` / ``extract_dom`` / ``screenshot`` /
``interact`` / ``navigate`` 复用同一个浏览器进程与页面（``session_launches`` 用于
验收“会话复用”断言）。

模块边界（与 ``docs_local/journal/V4-切片2-接口冻结-20261113.md`` §2/§3 一致）：

- :class:`ProcessTransport`：Node worker 子进程的 JSON lines 传输。资源缺失抛
  :class:`BrowserUnavailable`，断线/超时抛 :class:`WorkerLost`，协议错误抛
  :class:`WorkerProtocolError`。
- :class:`FakeTransport`：脚本化传输，仅供单测；可注入错误与断线。
- :class:`PlaywrightDriver`：把 worker snapshot 映射成 :class:`model.PageView` 与
  :class:`model.Observation`，并复核截图路径与 sha256。

安全与隐私（由 worker + 本地过滤代理实现，driver 负责编排）：

- URL 模式必须 ``allow_network=True`` 且 ``egress_allowlist`` 非空（固定页面集合的
  授权试点模式）；driver 自动创建并启动
  :class:`acquisition.egress_proxy.EgressProxy`，通过 launch 的 ``proxy_server`` 让
  Chromium 只能走 allowlist 过滤代理（DNS 只解析一次并 pin 到已验证公网 IP，防
  DNS rebinding；非 allowlist host / 私网地址在代理侧 403）。
- worker 在 URL 模式强制 Chromium 代理参数（禁 QUIC / 预连接 / 网络预测 / WebRTC
  非代理 UDP），只接受公网 HTTPS、每请求复核、隐藏视频、丢弃弹窗；两种模式都在
  页面内禁用 WebSocket/EventSource/RTCPeerConnection/sendBeacon，并用 routeWebSocket
  关闭连接。
- fixture 模式拦截全部网络请求，**不启动代理**，不加载用户 profile/cookie。

截图只允许落在 launch 时给定的 ``artifact_dir``，driver 侧再次复核路径与 sha256。

OCR 只在 ``extract('ocr')`` 时惰性调用现有 Windows OCR
（``agent_v3.browser_tools.ocr``，只读导入）或注入的 ``ocr_engine``。OCR 不可用时
返回空文本 + ``note='ocr_unavailable'``，**绝不伪造文字**；证据文本只来自真实 OCR
输出。

**OCR 文本只是视觉转录，不是页面原文。** 成功转录时 driver 会清理 OCR 产生的多余
空白（仅合并数字之间、汉字之间的空格，如把 ``1 1 月`` 合并为 ``11 月``，不修改任何
字符），并在 ``Observation.note`` 写明 :data:`OCR_TRANSCRIPTION_NOTE`
（“视觉转录，需对照截图与语义核验”）；该 note 随 receipt 落库。driver 不会把 OCR
文本标注为准确原文，也不能替代独立核验。

``worker`` snapshot 的 ``visual_hint=true`` 映射为 ``model.Region(visual_only=True,
visual_text='')``：只表示“该区域只有截图里可读的图文”，不带任何占位文本。
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence
from urllib.parse import urlsplit

from . import model
from .egress_proxy import ConnectionRecord, EgressProxy
from .model import (
    DOM_BODY_REGION,
    VIEWPORT_REGION_PREFIX,
    Observation,
    PageView,
    Region,
    ResearchGoal,
    fixed_frame_offsets,
)

WORKER_PROTOCOL = 'acquisition-browser-session-v0.1'
DEFAULT_ARTIFACT_ROOT = Path(__file__).resolve().parent.parent / 'data' / 'v4' / 'acquisition' / 'live'
DEFAULT_WORKER = Path(__file__).with_name('browser_session_worker.cjs')
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_PAGE_VERSION_PREFIX = 'sha256:'
_VISUAL_METHODS = ('ocr', 'vlm')
_UNSUPPORTED_EXTRACTION_METHODS = ('vlm', 'skill', 'http')
# OCR 文本只是视觉转录：note 明确要求对照截图与语义核验，不得当作页面原文。
OCR_TRANSCRIPTION_NOTE = '视觉转录，需对照截图与语义核验'
# OCR 识别会把连续数字/汉字拆出多余空白（如 "2026 年 1 1 月 30 日"）。这里只做
# 空白清理，不改动任何识别出的字符，属于转录格式归一，不是文本伪造。
_OCR_DIGIT_GAP_RE = re.compile(r'(?<=\d)[ \t]+(?=\d)')
_OCR_CJK_GAP_RE = re.compile(r'(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])')


class BrowserUnavailable(RuntimeError):
    """本机缺少 node / Playwright 模块 / 浏览器可执行文件等真实浏览器资源。"""


class WorkerLost(RuntimeError):
    """worker 进程断线、提前退出或响应超时。"""


class WorkerProtocolError(RuntimeError):
    """worker 返回了不符合协议的内容（含 worker 命令错误 {ok:false}）。"""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ''


def _parse_viewport(value: Any, label: str = 'viewport') -> tuple[int, int]:
    _require(isinstance(value, (list, tuple)) and len(value) == 2, f'{label} 必须为两个正整数')
    width, height = value
    _require(_is_int(width) and _is_int(height) and width > 0 and height > 0, f'{label} 必须为两个正整数')
    return (int(width), int(height))


def _region_bbox(value: Any) -> tuple[int, int, int, int]:
    _require(isinstance(value, (list, tuple)) and len(value) == 4, 'bbox 必须为四元组')
    x, y, width, height = value
    _require(all(_is_int(item) and item >= 0 for item in (x, y, width, height)),
             'bbox 必须为非负整数')
    _require(width > 0 and height > 0, 'bbox 宽高必须为正')
    return (int(x), int(y), int(width), int(height))


def _dedupe_join(parts: list[str]) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        text = part.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return '\n'.join(result)


def _is_virtual_region(region_id: str) -> bool:
    return region_id == DOM_BODY_REGION or region_id.startswith(VIEWPORT_REGION_PREFIX)


def _viewport_index(region_id: str) -> int:
    tail = region_id[len(VIEWPORT_REGION_PREFIX):]
    _require(tail.isdigit(), f'视口编号不合法: {region_id!r}')
    return int(tail)


def _normalise_egress_allowlist(values: Sequence[str]) -> tuple[str, ...]:
    """URL 模式出网 allowlist 的构造期校验（完整 host 语法由 EgressProxy 复核）。"""
    _require(values is not None, 'egress_allowlist 不能为 None')
    _require(not isinstance(values, (str, bytes)),
             'egress_allowlist 必须是主机名序列，不能是单个字符串')
    entries: list[str] = []
    for raw in values:
        _require(isinstance(raw, str), f'egress_allowlist 条目必须是字符串: {raw!r}')
        text = raw.strip()
        _require(bool(text), 'egress_allowlist 不能包含空条目')
        entries.append(text)
    return tuple(entries)


# --------------------------------------------------------------------------- 资源探测
def _find_node() -> str | None:
    """探测 Node.js：显式环境变量 → codex runtime 内置 → PATH。"""
    for name in ('V4_BROWSER_NODE', 'V3_BROWSER_NODE'):
        value = os.environ.get(name)
        if value and Path(value).is_file():
            return value
    home = Path.home()
    bundled = [
        home / '.cache' / 'codex-runtimes' / 'codex-primary-runtime' / 'dependencies' / 'node' / 'bin' / 'node.exe',
        home / '.cache' / 'codex-runtimes' / 'codex-primary-runtime' / 'dependencies' / 'node' / 'node.exe',
    ]
    for candidate in bundled:
        if candidate.is_file():
            return str(candidate)
    return shutil.which('node')


def _find_playwright_module() -> Path | None:
    """探测 Playwright Node 模块：显式环境变量 → codex runtime 默认路径。"""
    for name in ('V4_PLAYWRIGHT_MODULE', 'V3_PLAYWRIGHT_MODULE'):
        value = os.environ.get(name)
        if value and Path(value).exists():
            return Path(value)
    candidate = (Path.home() / '.cache' / 'codex-runtimes' / 'codex-primary-runtime'
                 / 'dependencies' / 'node' / 'node_modules' / 'playwright')
    if candidate.exists():
        return candidate
    return None


def _find_browser_executable() -> str | None:
    """探测 Edge/Chromium 可执行文件：显式环境变量 → 常见 Edge 安装路径。"""
    for name in ('V4_BROWSER_EXECUTABLE', 'V3_BROWSER_EXECUTABLE'):
        value = os.environ.get(name)
        if value and Path(value).is_file():
            return value
    for candidate in (
        'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
        'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
        '/usr/bin/microsoft-edge',
        '/usr/bin/microsoft-edge-stable',
        '/usr/bin/google-chrome',
        '/usr/bin/chromium',
        '/usr/bin/chromium-browser',
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def available() -> tuple[bool, str]:
    """真实浏览器资源是否就绪。

    返回 ``(True, 'ok')``；否则 ``(False, reason)``，``reason`` ∈
    ``worker_missing | node_missing | playwright_missing | browser_missing``，供测试/验收
    在资源缺失时 skip 而不是失败。
    """
    if not DEFAULT_WORKER.is_file():
        return False, 'worker_missing'
    if _find_node() is None:
        return False, 'node_missing'
    if _find_playwright_module() is None:
        return False, 'playwright_missing'
    if _find_browser_executable() is None:
        return False, 'browser_missing'
    return True, 'ok'


# --------------------------------------------------------------------------- 默认 OCR
_UNSET = object()
_DEFAULT_OCR_ENGINE: Any = _UNSET


def _default_ocr_engine() -> Callable[[Path], dict] | None:
    """惰性导入现有 Windows OCR 引擎；不可用时返回 ``None``。"""
    global _DEFAULT_OCR_ENGINE
    if _DEFAULT_OCR_ENGINE is _UNSET:
        try:
            from agent_v3.browser_tools import ocr as engine
        except Exception:  # pragma: no cover - 取决于运行环境
            engine = None
        _DEFAULT_OCR_ENGINE = engine
    return _DEFAULT_OCR_ENGINE


def ocr_available() -> tuple[bool, str]:
    """OCR 是否可用（验收层据此把 poster/OCR 检查标 skip）。"""
    if _default_ocr_engine() is None:
        return False, 'ocr_engine_missing'
    system_root = os.environ.get('SystemRoot') or 'C:/Windows'
    powershell = (Path(system_root) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe')
    if not powershell.is_file():
        return False, 'powershell_missing'
    if not (_PROJECT_ROOT / 'agent_v3' / 'windows_ocr.ps1').is_file():
        return False, 'ocr_script_missing'
    return True, 'ok'


# --------------------------------------------------------------------------- 传输协议
class WorkerTransport(Protocol):
    """Node worker 传输层的冻结接口。"""

    def start(self) -> None:
        ...

    def call(self, command: dict, timeout: float = 30) -> dict:
        ...

    def close(self) -> None:
        ...

    @property
    def alive(self) -> bool:
        ...


class FakeTransport:
    """脚本化传输，仅测试使用：按序返回响应，可注入 worker 错误/断线。"""

    def __init__(self, responses: list[dict] | None = None) -> None:
        self.responses: list[Any] = list(responses or [])
        self.commands: list[dict] = []
        self._started = False
        self._closed = False
        self._alive = False

    # ---------------------------------------------------------------- 测试注入
    def queue(self, result: dict) -> None:
        """排入一个成功的 result 响应。"""
        self.responses.append(result)

    def fail_next(self, error: str) -> None:
        """下一个请求返回 worker 命令错误（``{ok:false,error}`` 语义）。"""
        self.responses.append({'__worker_error__': str(error)})

    def crash(self) -> None:
        """模拟 worker 进程断线：后续 call 抛 WorkerLost。"""
        self._alive = False

    # -------------------------------------------------------------- WorkerTransport
    def start(self) -> None:
        if self._closed:
            raise WorkerLost('FakeTransport 已关闭，不能重新启动')
        self._started = True
        self._alive = True

    @property
    def alive(self) -> bool:
        return self._started and self._alive and not self._closed

    def call(self, command: dict, timeout: float = 30) -> dict:
        if not self._started:
            self.start()
        if self._closed:
            raise WorkerLost('FakeTransport 已关闭')
        if not self._alive:
            raise WorkerLost('FakeTransport 模拟断线')
        self.commands.append(dict(command))
        if not self.responses:
            raise WorkerProtocolError(f'FakeTransport 未提供脚本化响应: {command.get("command")!r}')
        item = self.responses.pop(0)
        if callable(item):
            item = item(dict(command))
        if isinstance(item, (WorkerLost, WorkerProtocolError)):
            raise item
        if isinstance(item, Exception):
            raise item
        if not isinstance(item, dict):
            raise WorkerProtocolError('FakeTransport 响应必须是字典')
        if '__worker_lost__' in item:
            self._alive = False
            raise WorkerLost(str(item['__worker_lost__']))
        if '__worker_error__' in item:
            raise WorkerProtocolError(str(item['__worker_error__']))
        return item

    def close(self) -> None:
        if self._closed:
            return
        self.commands.append({'command': 'close'})
        self._closed = True
        self._alive = False


class ProcessTransport:
    """Node worker 子进程传输：stdin/stdout 各一行一个 JSON，串行请求。

    :param node: 显式 Node 可执行文件；缺省自动探测。
    :param worker: worker 脚本路径；缺省 ``acquisition/browser_session_worker.cjs``。
    :param artifact_dir: 截图目录（仅记录；driver 在 ``launch`` 时下发真实目录）。
    :param timeout: 单请求默认超时（秒）。
    """

    def __init__(self, *, node: str | None = None, worker: Path | None = None,
                 artifact_dir: Path | None = None, timeout: float = 30) -> None:
        self._node_arg = node
        self._worker_arg = Path(worker) if worker is not None else None
        self.artifact_dir = Path(artifact_dir) if artifact_dir is not None else None
        self.timeout = float(timeout)
        self._process: subprocess.Popen | None = None
        self._lines: queue.Queue[Any] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._next_id = 0
        self._closed = False
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None

    # -------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._process is not None:
            return
        node = self._node_arg or _find_node()
        if node is None:
            raise BrowserUnavailable(
                '未找到 Node.js（V4_BROWSER_NODE / V3_BROWSER_NODE / codex runtime / PATH）')
        worker = self._worker_arg or DEFAULT_WORKER
        if not worker.is_file():
            raise BrowserUnavailable(f'未找到浏览器 worker 脚本: {worker}')
        playwright_module = _find_playwright_module()
        if playwright_module is None:
            raise BrowserUnavailable(
                '未找到 Playwright Node 模块（V4_PLAYWRIGHT_MODULE / V3_PLAYWRIGHT_MODULE / codex runtime 默认路径）')
        executable = _find_browser_executable()
        if executable is None:
            raise BrowserUnavailable(
                '未找到 Edge/Chromium 可执行文件（V4_BROWSER_EXECUTABLE / V3_BROWSER_EXECUTABLE / 默认 Edge 路径）')
        env = dict(os.environ)
        env.setdefault('V3_PLAYWRIGHT_MODULE', str(playwright_module))
        env.setdefault('V3_BROWSER_EXECUTABLE', executable)
        if self.artifact_dir is not None:
            env.setdefault('V4_BROWSER_ARTIFACT_DIR', str(self.artifact_dir))
        try:
            self._process = subprocess.Popen(
                [node, str(worker)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(_PROJECT_ROOT),
                env=env,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
            )
        except OSError as exc:  # pragma: no cover - 取决于运行环境
            raise BrowserUnavailable(f'启动 Node worker 失败: {exc}') from exc
        self._reader = threading.Thread(target=self._read_stdout, name='acq-worker-stdout', daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._read_stderr, name='acq-worker-stderr', daemon=True)
        self._stderr_reader.start()

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._lines.put(None)
            return
        try:
            for line in process.stdout:
                stripped = line.strip()
                if stripped:
                    self._lines.put(stripped)
        except (OSError, ValueError):  # pragma: no cover - 进程被终止
            pass
        finally:
            self._lines.put(None)

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                text = line.strip()
                if text:
                    self._stderr_tail.append(text)
        except (OSError, ValueError):  # pragma: no cover
            pass

    def _stderr_text(self) -> str:
        if not self._stderr_tail:
            return ''
        return ' | '.join(list(self._stderr_tail)[-5:])

    def _terminate(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.kill()
        except OSError:  # pragma: no cover
            pass
        try:
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):  # pragma: no cover
            pass

    @property
    def alive(self) -> bool:
        process = self._process
        return process is not None and not self._closed and process.poll() is None

    def call(self, command: dict, timeout: float = 30) -> dict:
        process = self._process
        if process is None:
            raise WorkerLost('worker 尚未启动')
        if self._closed:
            raise WorkerLost('worker 已关闭')
        if process.poll() is not None:
            raise WorkerLost(f'worker 进程已退出（stderr: {self._stderr_text() or "无"}）')
        self._next_id += 1
        request = {'id': self._next_id, **dict(command)}
        line = json.dumps(request, ensure_ascii=False, separators=(',', ':'))
        wait = self.timeout if timeout is None else float(timeout)
        try:
            assert process.stdin is not None
            process.stdin.write(line + '\n')
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise WorkerLost(f'写入 worker 失败: {exc}') from exc
        deadline = time.monotonic() + wait
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate()
                raise WorkerLost(
                    f'worker 响应超时（{wait:.1f}s）: command={command.get("command")!r}')
            try:
                item = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if item is None:
                self._terminate()
                raise WorkerLost(f'worker 提前退出（stderr: {self._stderr_text() or "无"}）')
            try:
                payload = json.loads(item)
            except json.JSONDecodeError as exc:
                raise WorkerProtocolError(f'worker 输出不是 JSON 行: {item[:200]!r}') from exc
            if not isinstance(payload, dict):
                raise WorkerProtocolError('worker 响应必须是 JSON 对象')
            if 'id' not in payload:
                # 进程退出前会输出 {"closed":true}；close 命令响应之前可能先读到，忽略。
                continue
            if payload.get('id') != request['id']:
                continue
            if payload.get('ok') is not True:
                raise WorkerProtocolError(str(payload.get('error') or 'worker 返回 ok=false'))
            result = payload.get('result')
            if not isinstance(result, dict):
                raise WorkerProtocolError('worker 响应的 result 必须是 JSON 对象')
            return result

    def close(self) -> None:
        if self._closed:
            return
        process = self._process
        if process is not None:
            try:
                if process.poll() is None and process.stdin is not None:
                    self._next_id += 1
                    request = {'id': self._next_id, 'command': 'close'}
                    process.stdin.write(json.dumps(request, ensure_ascii=False) + '\n')
                    process.stdin.flush()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        self._terminate()
            except (OSError, ValueError):
                self._terminate()
            finally:
                try:
                    if process.poll() is None:
                        self._terminate()
                except OSError:  # pragma: no cover
                    pass
                self._close_pipes(process)
        self._closed = True
        self._process = None

    def _close_pipes(self, process: subprocess.Popen) -> None:
        """显式关闭 stdin/stdout/stderr 管道并回收子进程，避免 TextIOWrapper 资源泄漏。"""
        for name in ('stdin', 'stdout', 'stderr'):
            stream = getattr(process, name, None)
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):  # pragma: no cover - 已关闭/已断
                pass
        try:
            if process.poll() is None:
                process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):  # pragma: no cover
            pass
        for reader in (self._reader, self._stderr_reader):
            if reader is not None:
                reader.join(timeout=2)
        self._reader = None
        self._stderr_reader = None


# --------------------------------------------------------------------------- 驱动
class PlaywrightDriver:
    """真实 Playwright + Edge 的持久会话驱动，实现 ``model.PageDriver``。

    构造时必须提供 ``url`` 或 ``fixture_html`` 之一（二者互斥）：

    - ``fixture_html``：本地合成 HTML，fixture 模式，页面内**所有网络请求被拦截**；
      不启动出网代理，行为与切片 2 一致；
    - ``url``：公网 HTTPS 页面，URL 模式（**必须** ``allow_network=True`` 且非空
      ``egress_allowlist``）：driver 自动启动
      :class:`acquisition.egress_proxy.EgressProxy` 并让 Chromium 只能经它出网；
      公网 IP 校验、每请求拦截、隐藏视频、丢弃弹窗仍由 worker 保留。

    ``transport`` 为 ``None`` 时内部创建 :class:`ProcessTransport`；传入时使用传入实例
    （例如验收层的 RecordingTransport 包装），driver 不会另建传输。
    """

    backend = 'playwright'

    def __init__(self, *, url: str | None = None, fixture_html: str | None = None,
                 transport: WorkerTransport | None = None,
                 ocr_engine: Callable[[Path], dict] | None = None,
                 artifact_dir: Path | None = None, viewport=(1440, 1000),
                 timeout: float = 30, allow_network: bool = False,
                 egress_allowlist: Sequence[str] = (),
                 proxy: EgressProxy | None = None) -> None:
        _require(url is not None or fixture_html is not None, '必须提供 url 或 fixture_html 之一')
        _require(not (url is not None and fixture_html is not None),
                 'url 与 fixture_html 只能提供其一')
        _require(isinstance(allow_network, bool), 'allow_network 必须为布尔值')
        allowlist = _normalise_egress_allowlist(egress_allowlist)
        if proxy is not None and not isinstance(proxy, EgressProxy):
            raise TypeError('proxy 必须是 EgressProxy')
        fixture_mode = fixture_html is not None
        if url is not None:
            _validate_public_https_syntax(url)
            if not allow_network:
                # URL 模式默认关闭：切片 2 的地址/逐请求校验挡不住重定向/预连接对私网
                # 端点的盲连接；切片 3 起必须显式 allow_network + egress_allowlist，
                # 由本地过滤代理强制出网 allowlist 与 DNS IP pinning。
                raise BrowserUnavailable(
                    'url_mode_disabled: URL 模式需显式 allow_network=True；'
                    '并配合非空 egress_allowlist 走本地出网过滤代理')
            if not allowlist:
                raise BrowserUnavailable(
                    'egress_allowlist_required: URL 模式必须提供非空 egress_allowlist；'
                    '出网只能经 allowlist 过滤代理放行这些 host')
        self._url = url
        self._fixture_html = fixture_html
        self._fixture_mode = fixture_mode
        self._allow_network = bool(allow_network)
        # fixture 模式不启动、不使用任何代理（行为与切片 2 完全一致）。
        self._egress_allowlist = () if fixture_mode else allowlist
        self._proxy: EgressProxy | None = None if fixture_mode else proxy
        self._owns_proxy = False
        self._viewport = _parse_viewport(viewport)
        self._timeout = float(timeout)
        self._transport = transport if transport is not None else ProcessTransport(
            artifact_dir=Path(artifact_dir) if artifact_dir is not None else None,
            timeout=timeout)
        self._owns_transport = transport is None
        self._explicit_artifact_dir = Path(artifact_dir) if artifact_dir is not None else None
        self._artifact_dir: Path | None = None
        self._ocr_engine = ocr_engine
        self._call_counts: dict[str, int] = {}
        self._session_launches = 0
        self._capture_count = 0
        self._started = False
        self._opened = False
        self._closed = False
        self._view: PageView | None = None
        self._launch_info: dict = {}

    # -------------------------------------------------------------- 公开属性
    @property
    def call_counts(self) -> dict[str, int]:
        """按命令名统计已发送的 worker 命令数（用于验收断言动作次数）。"""
        return dict(self._call_counts)

    @property
    def session_launches(self) -> int:
        """浏览器 launch 次数；同一驱动内 ``open``/``navigate`` 复用会话时应保持 1。"""
        return self._session_launches

    @property
    def capture_count(self) -> int:
        """本驱动已实际截取的画面数量（截图校验通过后计数）。"""
        return self._capture_count

    @property
    def transport(self) -> WorkerTransport:
        return self._transport

    @property
    def launch_info(self) -> dict:
        """worker launch 返回的浏览器/可执行文件/Node 版本与出网隔离能力信息。"""
        return dict(self._launch_info)

    @property
    def egress_records(self) -> tuple[ConnectionRecord, ...]:
        """出网过滤代理的允许/拒绝记录（host/IP/原因/字节）；fixture 模式为空元组。"""
        if self._proxy is None:
            return ()
        return self._proxy.records

    @property
    def egress_proxy(self) -> EgressProxy | None:
        """URL 模式使用的过滤代理实例（自有或注入）；fixture 模式为 ``None``。"""
        return self._proxy

    # ---------------------------------------------------------------- 内部
    def _hit(self, name: str) -> None:
        self._call_counts[name] = self._call_counts.get(name, 0) + 1

    def _call(self, command: dict, *, timeout: float | None = None) -> dict:
        if self._closed:
            raise WorkerLost('驱动已关闭')
        self._hit(str(command.get('command') or ''))
        wait = self._timeout if timeout is None else float(timeout)
        try:
            result = self._transport.call(dict(command), timeout=wait)
        except (WorkerLost, WorkerProtocolError):
            raise
        except Exception as exc:  # 传输层杂散异常按断线处理，避免吞掉真实故障
            raise WorkerLost(f'{type(exc).__name__}: {exc}') from exc
        if not isinstance(result, dict):
            raise WorkerProtocolError('worker 响应必须是 JSON 对象')
        return result

    def _default_artifact_dir(self) -> Path:
        token = time.strftime('%Y%m%d-%H%M%S')
        return DEFAULT_ARTIFACT_ROOT / f'session-{token}-{uuid.uuid4().hex[:6]}'

    def _artifact_root(self) -> Path:
        if self._artifact_dir is None:
            target = self._explicit_artifact_dir or self._default_artifact_dir()
            target = target.resolve()
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise BrowserUnavailable(f'无法创建截图目录 {target}: {exc}') from exc
            self._artifact_dir = target
        return self._artifact_dir

    def _ensure_started(self) -> None:
        if self._started:
            return
        try:
            self._transport.start()
        except (BrowserUnavailable, WorkerLost, WorkerProtocolError):
            raise
        except Exception as exc:
            raise BrowserUnavailable(f'启动浏览器会话失败: {exc}') from exc
        artifact_dir = self._artifact_root()
        proxy_url = ''
        if not self._fixture_mode:
            proxy_url = self._start_egress_proxy()
        command: dict[str, Any] = {
            'command': 'launch',
            'fixture_mode': bool(self._fixture_mode),
            'allow_url_mode': bool(self._allow_network and not self._fixture_mode),
            'viewport': [self._viewport[0], self._viewport[1]],
            'artifact_dir': str(artifact_dir),
        }
        if not self._fixture_mode:
            command['proxy_server'] = proxy_url
        try:
            result = self._call(command)
        except Exception:
            # launch 失败时不停掉可复用的传输（与切片 2 行为一致），但必须停掉自有代理。
            self._stop_owned_proxy()
            raise
        protocol = result.get('protocol')
        if protocol != WORKER_PROTOCOL:
            self._stop_owned_proxy()
            raise WorkerProtocolError(
                f'worker 协议不匹配: {protocol!r}（期望 {WORKER_PROTOCOL!r}）')
        if not self._fixture_mode:
            try:
                self._validate_launch_isolation(result, proxy_url)
            except WorkerProtocolError:
                self._stop_owned_proxy()
                raise
        self._started = True
        self._session_launches += 1
        self._launch_info = dict(result)

    # ---------------------------------------------------- 出网隔离（切片 3）
    def _start_egress_proxy(self) -> str:
        """返回出网过滤代理 url；自有代理在此创建并启动，注入代理必须已由调用方启动。"""
        if self._proxy is not None:
            if not self._proxy.url:
                raise BrowserUnavailable(
                    'egress_proxy_not_started: 注入的 EgressProxy 必须由调用方先 start()')
            return self._proxy.url
        proxy = EgressProxy(self._egress_allowlist)
        url = proxy.start()
        self._proxy = proxy
        self._owns_proxy = True
        return url

    def _stop_owned_proxy(self) -> None:
        """只停止 driver 自有的代理；注入的代理生命周期由调用方管理。"""
        if self._owns_proxy and self._proxy is not None:
            try:
                self._proxy.stop()
            except Exception:  # 关闭阶段的代理异常不向调用方抛出
                pass

    @staticmethod
    def _validate_launch_isolation(result: dict, proxy_url: str) -> None:
        """复核 worker 确认了出网隔离；缺任一项视为协议错误，而不是“大概没问题”。"""
        worker_proxy = result.get('proxy_server')
        if worker_proxy != proxy_url:
            raise WorkerProtocolError(
                f'worker 未确认出网代理: proxy_server={worker_proxy!r}（期望 {proxy_url!r}）')
        policy = result.get('webrtc_policy')
        if policy != 'disable_non_proxied_udp':
            raise WorkerProtocolError(
                f'worker 未启用 WebRTC 出网策略: webrtc_policy={policy!r}')
        if not isinstance(result.get('websocket_route_blocked'), bool):
            raise WorkerProtocolError('worker 未报告 websocket_route_blocked 能力标记')

    def _load_source(self, *, url: str | None = None, fixture_html: str | None = None) -> PageView:
        self._ensure_started()
        if fixture_html is not None:
            if not self._fixture_mode:
                raise ValueError('当前会话为 URL 模式，不能加载本地 HTML fixture')
            payload = self._call({'command': 'load_fixture', 'html': fixture_html})
        else:
            if self._fixture_mode:
                raise ValueError('当前会话为 fixture 模式（禁网），不能访问 URL')
            payload = self._call({'command': 'open', 'url': str(url)})
        view = self._view_from_snapshot(payload)
        self._view = view
        self._opened = True
        return view

    def _require_view(self, view: PageView) -> None:
        if self._closed:
            raise WorkerLost('驱动已关闭')
        if not self._started or not self._opened:
            raise RuntimeError('驱动尚未 open(goal)，不能执行页面动作')
        if not isinstance(view, PageView):
            raise TypeError('view 必须是 PageView')

    def _view_from_snapshot(self, payload: dict) -> PageView:
        if not isinstance(payload, dict):
            raise WorkerProtocolError('snapshot 必须是 JSON 对象')
        url = payload.get('url')
        if not isinstance(url, str) or not url:
            raise WorkerProtocolError('snapshot 缺少 url')
        title = payload.get('title')
        title = title if isinstance(title, str) else ''
        page_version = payload.get('page_version')
        if not isinstance(page_version, str) or not page_version.startswith(_PAGE_VERSION_PREFIX):
            raise WorkerProtocolError(f'snapshot 的 page_version 非法: {page_version!r}')
        viewport = _parse_viewport(payload.get('viewport', list(self._viewport)), 'snapshot viewport')
        scroll_height = payload.get('scroll_height', 0)
        if not _is_int(scroll_height) or scroll_height < 0:
            raise WorkerProtocolError('snapshot 的 scroll_height 非法')
        blocked_reason = payload.get('blocked_reason')
        if blocked_reason not in (None, 'captcha', 'login_wall', 'access_denied'):
            raise WorkerProtocolError(f'snapshot 的 blocked_reason 非法: {blocked_reason!r}')
        dom_text = payload.get('dom_text', '')
        if not isinstance(dom_text, str):
            raise WorkerProtocolError('snapshot 的 dom_text 必须是字符串')
        raw_regions = payload.get('regions', [])
        if not isinstance(raw_regions, list):
            raise WorkerProtocolError('snapshot 的 regions 必须是数组')
        regions = tuple(self._region_from(raw) for raw in raw_regions)
        return PageView(page_version=page_version, url=url, title=title,
                        viewport=viewport, scroll_height=int(scroll_height),
                        regions=regions, dom_text=dom_text, blocked_reason=blocked_reason)

    @staticmethod
    def _region_from(raw: Any) -> Region:
        if not isinstance(raw, dict):
            raise WorkerProtocolError('snapshot 的 region 必须是 JSON 对象')
        region_id = raw.get('region_id')
        if not isinstance(region_id, str) or not region_id:
            raise WorkerProtocolError('snapshot 的 region_id 非法')
        role = raw.get('role')
        if role not in model.REGION_ROLES:
            raise WorkerProtocolError(f'region role 非法: {role!r}')
        try:
            bbox = _region_bbox(raw.get('bbox'))
        except ValueError as exc:
            raise WorkerProtocolError(f'region bbox 非法: {exc}') from exc
        text = raw.get('text')
        text = text if isinstance(text, str) else ''
        visual_hint = bool(raw.get('visual_hint'))
        lazy = bool(raw.get('lazy'))
        requires = raw.get('requires')
        if requires is not None and not isinstance(requires, str):
            raise WorkerProtocolError('region requires 必须是字符串或 null')
        blocked = bool(raw.get('blocked'))
        scroll_y = raw.get('scroll_y', bbox[1])
        if not _is_int(scroll_y) or scroll_y < 0:
            raise WorkerProtocolError('region scroll_y 非法')
        # visual_hint 只映射为 visual_only 标志；visual_text 保持空，绝不写占位文本。
        return Region(region_id=region_id, role=role, bbox=bbox, text=text,
                      visual_text='', visual_only=visual_hint, lazy=lazy,
                      requires=requires, blocked=blocked, scroll_y=int(scroll_y))

    @staticmethod
    def _frame_offset(view: PageView, index: int) -> int:
        offsets = fixed_frame_offsets(view.scroll_height, view.viewport[1])
        _require(_is_int(index) and 0 <= index < len(offsets), f'视口编号超出固定帧范围: {index}')
        return offsets[index]

    def _band_text(self, view: PageView, offset: int) -> str:
        band_top, band_bottom = offset, offset + view.viewport[1]
        parts: list[str] = []
        for item in view.regions:
            _x, y, _width, height = item.bbox
            if y >= band_bottom or y + height <= band_top:
                continue
            if item.text.strip():
                parts.append(item.text)
        return _dedupe_join(parts)

    # ------------------------------------------------------ PageDriver: 打开页面
    def open(self, goal: ResearchGoal) -> PageView:
        """加载页面并返回初始视图；首次调用启动浏览器（launch），之后复用同一会话。"""
        if not isinstance(goal, ResearchGoal):
            raise TypeError('goal 必须是 ResearchGoal')
        if self._closed:
            raise WorkerLost('驱动已关闭')
        if self._fixture_mode:
            return self._load_source(fixture_html=self._fixture_html)
        return self._load_source(url=self._url)

    def navigate(self, *, url: str | None = None, fixture_html: str | None = None) -> PageView:
        """复用同一浏览器/页面加载另一 URL 或本地 HTML；不会重新 launch。"""
        if self._closed:
            raise WorkerLost('驱动已关闭')
        if (url is None) == (fixture_html is None):
            raise ValueError('navigate 必须并且只能提供 url 或 fixture_html 之一')
        if self._fixture_mode and url is not None:
            raise ValueError('当前会话为 fixture 模式（禁网），不能访问 URL')
        if not self._fixture_mode and fixture_html is not None:
            raise ValueError('当前会话为 URL 模式，不能加载本地 HTML fixture')
        return self._load_source(url=url, fixture_html=fixture_html)

    # ------------------------------------------------------ PageDriver: 观察/定位
    def inspect(self, view: PageView) -> Observation:
        """刷新页面快照；不产生文字证据，成本 = inspect。"""
        self._require_view(view)
        payload = self._call({'command': 'observe'})
        new_view = self._view_from_snapshot(payload)
        self._view = new_view
        note = f'blocked_page:{new_view.blocked_reason}' if new_view.blocked_reason else ''
        return Observation(action='inspect', page_version=new_view.page_version,
                           cost=model.action_cost('inspect'),
                           candidates=tuple(region.region_id for region in new_view.regions),
                           blocked=bool(new_view.blocked_reason), note=note, view=new_view)

    def locate(self, view: PageView, goal: ResearchGoal) -> Observation:
        """按目标先验给出候选区域顺序；不读取页面文字、不产生证据。"""
        self._require_view(view)
        if not isinstance(goal, ResearchGoal):
            raise TypeError('goal 必须是 ResearchGoal')
        ordered: list[str] = []
        seen: set[str] = set()

        def add(region: Region) -> None:
            if region.region_id not in seen:
                seen.add(region.region_id)
                ordered.append(region.region_id)

        for region in view.regions:
            if any(region.role in objective.region_hints for objective in goal.objectives):
                add(region)
        for region in view.regions:
            if any(model.candidate_text(objective, region.candidate_text())
                   for objective in goal.objectives):
                add(region)
        for region in view.regions:
            if region.visual_text.strip() or region.visual_only:
                add(region)
        for region in view.regions:
            add(region)
        return Observation(action='locate', page_version=view.page_version,
                           cost=model.action_cost('locate'), candidates=tuple(ordered),
                           note=f'候选区域 {len(ordered)} 个',
                           blocked=bool(view.blocked_reason))

    # ------------------------------------------------------ PageDriver: 提取
    def extract(self, view: PageView, region: Region, method: str) -> Observation:
        """按通道提取区域文本；``vlm``/``skill`` 不归 driver 实现，抛 ValueError。"""
        self._require_view(view)
        if not isinstance(region, Region):
            raise TypeError('region 必须是 Region')
        if method not in model.EXTRACTIONS:
            raise ValueError(f'未知提取通道: {method!r}')
        if method in _UNSUPPORTED_EXTRACTION_METHODS:
            raise ValueError(f'PlaywrightDriver 不支持 {method!r} 提取通道（交给 router/策略）')
        page_blocked = view.blocked_reason
        login_gated = 'login' in (region.requires or '').lower()
        if page_blocked or region.blocked or login_gated:
            return self._blocked_extract(view, region, method, page_blocked, login_gated)
        if method == 'dom':
            return self._extract_dom(view, region)
        return self._extract_ocr(view, region)

    def _extract_dom(self, view: PageView, region: Region) -> Observation:
        region_id = region.region_id
        note = ''
        if region_id == DOM_BODY_REGION:
            payload = self._call({'command': 'extract_dom', 'region_id': DOM_BODY_REGION})
            text = _as_text(payload.get('text')).strip()
            role = 'section'
            bbox = (0, 0, view.viewport[0], max(view.viewport[1], view.scroll_height))
        elif region_id.startswith(VIEWPORT_REGION_PREFIX):
            index = _viewport_index(region_id)
            offset = self._frame_offset(view, index)
            text = self._band_text(view, offset)
            role = 'section'
            bbox = (0, offset, view.viewport[0], view.viewport[1])
        else:
            payload = self._extract_dom_payload(region_id)
            text = _as_text(payload.get('text')).strip()
            payload_role = payload.get('role')
            role = payload_role if payload_role in model.REGION_ROLES else region.role
            if payload.get('bbox') is not None:
                try:
                    bbox = _region_bbox(payload.get('bbox'))
                except ValueError as exc:
                    raise WorkerProtocolError(f'extract_dom 的 bbox 非法: {exc}') from exc
            else:
                bbox = region.bbox
            if region.lazy and not text:
                note = 'lazy_not_loaded'
        return Observation(action='extract', page_version=view.page_version,
                           region_id=region_id, role=role, extraction='dom', text=text,
                           bbox=bbox, cost=model.action_cost('extract', 'dom'), note=note)

    def _extract_dom_payload(self, region_id: str) -> dict:
        try:
            return self._call({'command': 'extract_dom', 'region_id': region_id})
        except WorkerProtocolError as exc:
            if str(exc).startswith('unknown_region'):
                raise ValueError(f'区域不存在: {region_id}') from exc
            raise

    def _extract_ocr(self, view: PageView, region: Region) -> Observation:
        if region.region_id == DOM_BODY_REGION:
            raise ValueError('整页 DOM 虚拟区域不支持 OCR 截图（协议只允许区域或固定视口截图）')
        role = region.role
        if region.region_id.startswith(VIEWPORT_REGION_PREFIX):
            index = _viewport_index(region.region_id)
            self._frame_offset(view, index)  # 越界时与 model.fixed_frame_offsets 一致地报错
            shot = self._screenshot(viewport_index=index)
        else:
            shot = self._screenshot_region(region.region_id)
        text, ocr_note = self._ocr_text(shot['file'])
        # 成功转录的文本只是视觉转录：note 明确要求对照截图与语义核验（随 receipt 落库）。
        note = OCR_TRANSCRIPTION_NOTE if text else ocr_note
        capture_id = 'capture_' + shot['sha256'][:24]
        return Observation(action='extract', page_version=view.page_version,
                           region_id=region.region_id, role=role, extraction='ocr',
                           text=text, capture_id=capture_id, bbox=shot['bbox'],
                           cost=model.action_cost('extract', 'ocr'), note=note)

    def _blocked_extract(self, view: PageView, region: Region, method: str,
                         page_blocked: str | None, login_gated: bool) -> Observation:
        """阻塞页/登录墙区域：截图仍可留作人工核查，但文字一律丢弃（截图不能当证据）。"""
        cost = model.action_cost('extract', method)
        capture_id = None
        bbox = region.bbox
        if method in _VISUAL_METHODS:
            try:
                if region.region_id.startswith(VIEWPORT_REGION_PREFIX):
                    shot = self._screenshot(viewport_index=_viewport_index(region.region_id))
                elif region.region_id != DOM_BODY_REGION:
                    shot = self._screenshot_region(region.region_id)
                else:
                    shot = None
                if shot is not None:
                    capture_id = 'capture_' + shot['sha256'][:24]
                    bbox = shot['bbox']
                    self._ocr_text(shot['file'])  # 成本照记；文字丢弃，不伪造
            except (ValueError, WorkerProtocolError):
                capture_id = None
        if page_blocked:
            note = f'blocked_page:{page_blocked}'
        elif login_gated:
            note = 'login_required'
        else:
            note = 'region_blocked'
        return Observation(action='extract', page_version=view.page_version,
                           region_id=region.region_id, role=region.role, extraction=method,
                           text='', capture_id=capture_id, bbox=bbox, cost=cost,
                           blocked=True, note=note)

    # ------------------------------------------------------ PageDriver: 交互
    def interact(self, view: PageView, region: Region) -> Observation:
        """点击区域（或区域内的展开触发器）；点击无效/未知区域抛 ValueError。"""
        self._require_view(view)
        if not isinstance(region, Region):
            raise TypeError('region 必须是 Region')
        if _is_virtual_region(region.region_id):
            raise ValueError(f'虚拟区域不支持交互: {region.region_id!r}')
        try:
            payload = self._call({'command': 'interact', 'region_id': region.region_id,
                                  'action': 'click'})
        except WorkerProtocolError as exc:
            message = str(exc)
            if message.startswith(('unknown_region', 'click_failed', 'click_no_target',
                                   'region_detached')):
                raise ValueError(f'交互无效: {message}') from exc
            raise
        if payload.get('changed') is False:
            raise ValueError(f'点击未改变页面版本，交互无效: {region.region_id}')
        new_view = self._view_from_snapshot(payload)
        self._view = new_view
        return Observation(action='interact', page_version=new_view.page_version,
                           region_id=region.region_id, role=region.role,
                           cost=model.action_cost('interact'), view=new_view, note='clicked')

    # ------------------------------------------------------ 截图与 OCR
    def _screenshot_region(self, region_id: str) -> dict:
        try:
            return self._screenshot(region_id=region_id)
        except WorkerProtocolError as exc:
            if str(exc).startswith('unknown_region'):
                raise ValueError(f'区域不存在: {region_id}') from exc
            raise

    def _screenshot(self, *, region_id: str | None = None,
                    viewport_index: int | None = None) -> dict:
        command: dict[str, Any] = {'command': 'screenshot'}
        if region_id is not None:
            command['region_id'] = region_id
        elif viewport_index is not None:
            command['viewport_index'] = int(viewport_index)
        else:
            raise ValueError('截图必须指定 region_id 或 viewport_index')
        payload = self._call(command)
        artifact_root = self._artifact_root()
        raw_file = payload.get('file')
        if not isinstance(raw_file, str) or not raw_file:
            raise WorkerProtocolError('截图响应缺少 file')
        candidate = Path(raw_file)
        if not candidate.is_absolute():
            candidate = artifact_root / candidate
        resolved = candidate.resolve()
        if resolved != artifact_root and artifact_root not in resolved.parents:
            raise WorkerProtocolError(f'截图路径越界（必须在 artifact_dir 内）: {resolved}')
        if not resolved.is_file():
            raise WorkerProtocolError(f'截图文件不存在: {resolved}')
        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
        declared = str(payload.get('sha256') or '').strip().lower()
        if declared != actual:
            raise WorkerProtocolError(
                f'截图 sha256 不匹配: worker={declared or "缺失"} 实际={actual}')
        try:
            bbox = _region_bbox(payload.get('bbox'))
        except ValueError as exc:
            raise WorkerProtocolError(f'截图 bbox 非法: {exc}') from exc
        width = payload.get('width')
        height = payload.get('height')
        if not _is_int(width) or not _is_int(height) or width <= 0 or height <= 0:
            raise WorkerProtocolError('截图 width/height 非法')
        scroll_y = payload.get('scroll_y')
        if scroll_y is not None and (not _is_int(scroll_y) or scroll_y < 0):
            raise WorkerProtocolError('截图 scroll_y 非法')
        self._capture_count += 1
        return {'file': resolved, 'sha256': actual, 'bbox': bbox,
                'width': int(width), 'height': int(height),
                'scroll_y': int(scroll_y) if scroll_y is not None else None}

    def _ocr_text(self, image: Path) -> tuple[str, str]:
        """返回 (text, note)。

        OCR 不可用时 text=''、note='ocr_unavailable'，绝不伪造文字；成功转录时仅合并
        OCR 产生的多余空白（数字之间/汉字之间，不改字符），并把“视觉转录”提示留给
        调用方写入 ``Observation.note``（:data:`OCR_TRANSCRIPTION_NOTE`）。
        """
        engine = self._ocr_engine if self._ocr_engine is not None else _default_ocr_engine()
        if engine is None:
            return '', 'ocr_unavailable'
        try:
            result = engine(image)
        except Exception:  # OCR 失败按不可用处理，不生成任何文字
            return '', 'ocr_unavailable'
        text = ''
        if isinstance(result, str):
            text = result
        elif isinstance(result, dict):
            status = str(result.get('status') or '').strip().lower()
            if status in ('unavailable', 'failed', 'error'):
                return '', 'ocr_unavailable'
            value = result.get('text')
            text = value if isinstance(value, str) else ''
        else:
            return '', 'ocr_unavailable'
        text = text.strip()
        # 仅清理 OCR 转录产生的多余空白（数字之间、汉字之间），不改动识别内容。
        text = _OCR_DIGIT_GAP_RE.sub('', text)
        text = _OCR_CJK_GAP_RE.sub('', text)
        return text, ''

    # ------------------------------------------------------------------ 关闭
    def close(self) -> None:
        """关闭 worker/浏览器与自有出网代理；重复调用无副作用。"""
        if self._closed:
            return
        try:
            self._transport.close()
        except Exception:  # 关闭阶段的传输异常不向调用方抛出
            pass
        # 先关浏览器再停代理：浏览器关闭后不会再有 CONNECT，避免停止时残留半开隧道。
        self._stop_owned_proxy()
        self._closed = True
        self._opened = False


def _validate_public_https_syntax(url: str) -> None:
    """URL 模式的静态校验（DNS/公网判定由 worker 完成，这里先挡明显非法输入）。"""
    if not isinstance(url, str) or not url:
        raise ValueError('url 必须是非空字符串')
    parsed = urlsplit(url)
    if parsed.scheme != 'https':
        raise ValueError('URL 模式只支持公开 HTTPS 地址')
    if not parsed.hostname:
        raise ValueError('URL 缺少主机名')
    if parsed.username or parsed.password:
        raise ValueError('URL 不得携带凭据')
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f'URL 端口非法: {exc}') from exc
    if port not in (None, 443):
        raise ValueError('URL 只允许 443 端口')
    host = parsed.hostname.lower()
    if host in ('localhost', 'localhost.localdomain') or host.endswith('.local'):
        raise ValueError('URL 主机必须属于公网，不能是 localhost/内网主机名')


__all__ = [
    'WORKER_PROTOCOL',
    'OCR_TRANSCRIPTION_NOTE',
    'BrowserUnavailable',
    'ConnectionRecord',
    'EgressProxy',
    'FakeTransport',
    'PlaywrightDriver',
    'ProcessTransport',
    'WorkerLost',
    'WorkerProtocolError',
    'WorkerTransport',
    'available',
    'ocr_available',
]
