"""``acquisition.live_driver`` 测试（V4 切片 2 + 切片 3 出网隔离加固）。

覆盖四组内容：

1. FakeTransport 单元语义：open/inspect/locate/extract(dom/ocr)/interact/navigate/
   截图路径与 sha256 复核/未知区域/协议错误/断线 WorkerLost/OCR 不可用不伪造文字/
   session_launches=1 的会话复用；
2. 出网隔离（切片 3）：URL 模式必须 allow_network + 非空 egress_allowlist；driver
   自动创建/启动/停止自有 EgressProxy、注入代理不被 driver 停止；launch 命令携带
   proxy_server，worker 未确认 proxy/webrtc/websocket 能力时按协议错误拒绝；
   fixture 模式不启动代理；
3. 静态检查：Node worker 语法（``node --check``）、URL 安全负例（``http://``、
   ``https://localhost`` 被拒，且 worker 进程不崩溃）、URL 模式缺 proxy 真实返回
   ``egress_proxy_required``；
4. 真实浏览器 smoke：本机有 node + Playwright + Edge 时**真跑**，资源缺失时自动
   skip（不得失败）。真实测试只做机制验证；真实站点试点由 Lead 单独执行，这里的
   “真实浏览器 + 真实过滤代理”用例只验证浏览器确实经代理出网、私网解析被拒。
"""
from __future__ import annotations

import base64
import hashlib
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from acquisition import live_driver, model
from acquisition.egress_proxy import EgressProxy, is_public_address
from acquisition.live_driver import (
    BrowserUnavailable,
    FakeTransport,
    PlaywrightDriver,
    ProcessTransport,
    WorkerLost,
    WorkerProtocolError,
    available,
    ocr_available,
)

WORKER_PATH = Path(live_driver.__file__).with_name('browser_session_worker.cjs')
_PAGE_VERSION = 'sha256:' + 'a' * 64
_PAGE_VERSION_B = 'sha256:' + 'b' * 64

_REAL_BROWSER_OK, _REAL_BROWSER_REASON = available()
_OCR_OK, _OCR_REASON = ocr_available()


# --------------------------------------------------------------------------- 辅助构造
def make_goal(region_hints: tuple[str, ...] = ('activity_rules',)) -> model.ResearchGoal:
    objective = model.Objective(objective_id='deadline', question='确认活动截止日期',
                                answer_kind='date', region_hints=region_hints)
    return model.ResearchGoal(task_id='goal_live', question='确认活动截止日期',
                              objectives=(objective,), allowed_hosts=('www.taptap.cn',))


def region_payload(region_id: str = 'dom_1', *, role: str = 'activity_rules',
                   bbox=(120, 1500, 1200, 700), text: str = '', visual_hint: bool = False,
                   lazy: bool = False, requires: str | None = None, blocked: bool = False,
                   scroll_y: int | None = None) -> dict:
    payload = {'region_id': region_id, 'role': role, 'bbox': list(bbox), 'text': text,
               'visual_hint': visual_hint, 'lazy': lazy, 'requires': requires,
               'blocked': blocked, 'scroll_y': bbox[1] if scroll_y is None else scroll_y}
    return payload


def snapshot_payload(*, page_version: str = _PAGE_VERSION, url: str = 'fixture://local',
                     title: str = '合成活动公告', viewport=(1440, 1000),
                     scroll_height: int = 5200, blocked_reason: str | None = None,
                     dom_text: str = '活动公告正文：本页为本地合成场景。',
                     regions: list[dict] | None = None) -> dict:
    return {
        'page_version': page_version,
        'url': url,
        'title': title,
        'viewport': list(viewport),
        'scroll_height': scroll_height,
        'blocked_reason': blocked_reason,
        'dom_text': dom_text,
        'regions': regions if regions is not None else [region_payload()],
    }


def launch_payload(fixture_mode: bool = True, allow_url_mode: bool = False, *,
                   proxy_server: str | None = None,
                   webrtc_policy: str | None = None,
                   websocket_route_blocked: bool = True) -> dict:
    """模拟真实 worker 的 launch 响应（URL 模式需回显 proxy_server 等隔离字段）。"""
    if webrtc_policy is None and not fixture_mode:
        webrtc_policy = 'disable_non_proxied_udp'
    return {'protocol': live_driver.WORKER_PROTOCOL, 'fixture_mode': fixture_mode,
            'allow_url_mode': allow_url_mode, 'browser': 'TestBrowser/1.0',
            'proxy_server': None if fixture_mode else proxy_server,
            'webrtc_policy': None if fixture_mode else webrtc_policy,
            'websocket_route_blocked': websocket_route_blocked}


def url_launch_callable():
    """FakeTransport 响应：按收到的 launch 请求回显 proxy_server。"""
    def build(command: dict) -> dict:
        return launch_payload(False, command.get('allow_url_mode', False),
                              proxy_server=command.get('proxy_server'))
    return build


def proxy_endpoint(proxy: EgressProxy) -> tuple[str, int]:
    parsed = urlsplit(proxy.url)
    assert parsed.scheme == 'http' and parsed.hostname, proxy.url
    return parsed.hostname, int(parsed.port)


class SilentRemote:
    """假上游：立即 EOF；只让过滤代理完成一次 allowed CONNECT（不建立真实网络）。"""

    def __init__(self) -> None:
        self.sent = b''

    def settimeout(self, value) -> None:
        pass

    def sendall(self, data) -> None:
        self.sent += bytes(data)

    def recv(self, size: int) -> bytes:
        return b''

    def shutdown(self, how) -> None:
        pass

    def close(self) -> None:
        pass


class RecordingOcr:
    """注入用假 OCR：记录调用路径，返回脚本化结果。"""

    def __init__(self, text: str = '报名截止：2026年11月30日', status: str = 'ok') -> None:
        self.text = text
        self.status = status
        self.calls: list[Path] = []

    def __call__(self, image_path) -> dict:
        self.calls.append(Path(image_path))
        return {'status': self.status, 'text': self.text, 'lines': [], 'raw_text': self.text}


class FakeDriverTestBase(unittest.TestCase):
    """带 FakeTransport 的驱动测试基类：每次测试独立 artifact 目录。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix='acq-live-test-')
        self.addCleanup(self._tmp.cleanup)
        self.artifact_dir = Path(self._tmp.name)
        self.transport = FakeTransport()
        self.driver = PlaywrightDriver(fixture_html='<html><title>合成夹具</title></html>',
                                       transport=self.transport, artifact_dir=self.artifact_dir,
                                       timeout=5)

    # ---------------------------------------------------------------- 辅助
    def queue_launch(self, fixture_mode: bool = True) -> None:
        self.transport.queue(launch_payload(fixture_mode))

    def open_driver(self, payload: dict | None = None) -> model.PageView:
        self.queue_launch()
        self.transport.queue(payload or snapshot_payload())
        return self.driver.open(make_goal())

    def write_capture(self, name: str = 'frame-0.png',
                      data: bytes = b'\x89PNG\r\n\x1a\nfake-image-bytes') -> tuple[Path, str]:
        path = self.artifact_dir / name
        path.write_bytes(data)
        return path, hashlib.sha256(data).hexdigest()

    def screenshot_payload(self, path: Path, sha256: str, *, bbox=(120, 1500, 1200, 700),
                           width: int = 1200, height: int = 700,
                           scroll_y: int | None = 1500) -> dict:
        payload = {'file': str(path), 'sha256': sha256, 'bbox': list(bbox),
                   'width': width, 'height': height}
        if scroll_y is not None:
            payload['scroll_y'] = scroll_y
        return payload

    def prepare_ocr_extract(self, region_id: str = 'dom_1') -> tuple[Path, str]:
        """排队一次合法截图响应；返回 (path, sha256)。"""
        path, sha = self.write_capture()
        self.transport.queue(self.screenshot_payload(path, sha))
        return path, sha


# --------------------------------------------------------------------------- FakeTransport
class FakeTransportTests(unittest.TestCase):
    def test_queue_and_record_commands(self):
        transport = FakeTransport([{'value': 1}])
        transport.start()
        self.assertEqual(transport.call({'command': 'observe'}), {'value': 1})
        self.assertEqual(transport.commands, [{'command': 'observe'}])
        self.assertTrue(transport.alive)

    def test_fail_next_is_protocol_error(self):
        transport = FakeTransport()
        transport.start()
        transport.fail_next('boom')
        with self.assertRaises(WorkerProtocolError):
            transport.call({'command': 'observe'})
        self.assertTrue(transport.alive)

    def test_crash_raises_worker_lost(self):
        transport = FakeTransport()
        transport.start()
        transport.crash()
        self.assertFalse(transport.alive)
        with self.assertRaises(WorkerLost):
            transport.call({'command': 'observe'})

    def test_empty_script_is_protocol_error(self):
        transport = FakeTransport()
        transport.start()
        with self.assertRaises(WorkerProtocolError):
            transport.call({'command': 'observe'})

    def test_close_is_recorded_and_idempotent(self):
        transport = FakeTransport()
        transport.start()
        transport.close()
        transport.close()
        self.assertEqual(transport.commands, [{'command': 'close'}])
        self.assertFalse(transport.alive)


# --------------------------------------------------------------------------- 生命周期
class DriverLifecycleTests(FakeDriverTestBase):
    def test_open_launches_once_with_fixture_mode_and_artifact_dir(self):
        view = self.open_driver()
        self.assertIsInstance(view, model.PageView)
        self.assertEqual(self.driver.session_launches, 1)
        self.assertEqual(self.driver.backend, 'playwright')
        self.assertEqual(len(self.transport.commands), 2)
        launch = self.transport.commands[0]
        self.assertEqual(launch['command'], 'launch')
        self.assertIs(launch['fixture_mode'], True)
        self.assertEqual(launch['viewport'], [1440, 1000])
        self.assertEqual(Path(launch['artifact_dir']), self.artifact_dir.resolve())
        load = self.transport.commands[1]
        self.assertEqual(load['command'], 'load_fixture')
        self.assertIn('<html>', load['html'])
        self.assertEqual(self.driver.call_counts, {'launch': 1, 'load_fixture': 1})

    def test_navigate_fixture_reuses_same_browser(self):
        self.open_driver()
        self.transport.queue(snapshot_payload(page_version=_PAGE_VERSION_B,
                                              dom_text='第二个夹具正文'))
        view = self.driver.navigate(fixture_html='<html><title>第二夹具</title></html>')
        self.assertEqual(view.page_version, _PAGE_VERSION_B)
        self.assertEqual(self.driver.session_launches, 1)
        self.assertEqual(self.driver.call_counts['load_fixture'], 2)
        self.assertEqual(self.transport.commands[0]['command'], 'launch')
        self.assertEqual(self.transport.commands[2]['command'], 'load_fixture')

    def test_url_mode_driver_uses_open_command_and_owned_proxy(self):
        driver, transport = self._url_driver()
        transport.queue(url_launch_callable())
        transport.queue(snapshot_payload(url='https://www.taptap.cn/example/activity-001'))
        view = driver.open(make_goal())
        self.assertIn('taptap', view.url)
        launch = transport.commands[0]
        self.assertIs(launch['fixture_mode'], False)
        self.assertIs(launch['allow_url_mode'], True)
        self.assertRegex(launch['proxy_server'], r'^http://127\.0\.0\.1:\d+$')
        proxy = driver.egress_proxy
        self.assertIsNotNone(proxy)
        self.assertEqual(launch['proxy_server'], proxy.url)
        self.assertEqual(transport.commands[1]['command'], 'open')
        self.assertEqual(driver.egress_records, ())
        # 同一浏览器 navigate 到另一个公网 URL：不重新 launch、不重启代理
        proxy_url = launch['proxy_server']
        transport.queue(snapshot_payload(page_version=_PAGE_VERSION_B,
                                         url='https://www.taptap.cn/example/activity-002'))
        driver.navigate(url='https://www.taptap.cn/example/activity-002')
        self.assertEqual(driver.session_launches, 1)
        self.assertEqual(driver.call_counts['open'], 2)
        self.assertEqual(driver.egress_proxy.url, proxy_url)
        host, port = proxy_endpoint(proxy)
        driver.close()
        self.assertEqual(proxy.url, proxy_url)
        with self.assertRaises(OSError):  # 自有 proxy 随 close 停止监听
            socket.create_connection((host, port), timeout=1)

    def _url_driver(self, *, proxy: EgressProxy | None = None,
                    allowlist=('www.taptap.cn',),
                    url: str = 'https://www.taptap.cn/example/activity-001',
                    **kwargs) -> tuple[PlaywrightDriver, FakeTransport]:
        transport = FakeTransport()
        driver = PlaywrightDriver(url=url, transport=transport, artifact_dir=self.artifact_dir,
                                  allow_network=True, egress_allowlist=allowlist,
                                  proxy=proxy, timeout=5, **kwargs)
        return driver, transport

    def test_url_mode_requires_egress_allowlist(self):
        with self.assertRaises(BrowserUnavailable) as caught:
            PlaywrightDriver(url='https://www.taptap.cn/x', transport=FakeTransport(),
                             artifact_dir=self.artifact_dir, allow_network=True)
        self.assertIn('egress_allowlist_required', str(caught.exception))
        for bad in (None, 'www.taptap.cn', ('',), (123,)):
            with self.subTest(allowlist=bad):
                with self.assertRaises(ValueError):
                    PlaywrightDriver(url='https://www.taptap.cn/x', transport=FakeTransport(),
                                     artifact_dir=self.artifact_dir, allow_network=True,
                                     egress_allowlist=bad)

    def test_url_mode_invalid_allowlist_entry_fails_before_browser(self):
        driver, transport = self._url_driver(allowlist=('www.taptap.cn:443',), url='https://example.com/')
        with self.assertRaises(ValueError):
            driver.open(make_goal())
        self.assertFalse(transport.commands and
                         any(command['command'] == 'launch' for command in transport.commands))
        driver.close()

    def test_fixture_mode_does_not_start_proxy(self):
        self.open_driver()
        self.assertIsNone(self.driver.egress_proxy)
        self.assertEqual(self.driver.egress_records, ())
        self.assertNotIn('proxy_server', self.transport.commands[0])

    def test_fixture_mode_ignores_injected_proxy(self):
        proxy = EgressProxy(('example.com',))
        proxy.start()
        self.addCleanup(proxy.stop)
        transport = FakeTransport()
        driver = PlaywrightDriver(fixture_html='<html></html>', transport=transport,
                                  artifact_dir=self.artifact_dir, proxy=proxy, timeout=5)
        transport.queue(launch_payload(True))
        transport.queue(snapshot_payload())
        driver.open(make_goal())
        self.assertNotIn('proxy_server', transport.commands[0])
        self.assertIsNone(driver.egress_proxy)
        self.assertEqual(driver.egress_records, ())
        driver.close()
        # fixture 模式不使用注入代理，也不由 driver 停止它（生命周期归调用方）。
        socket.create_connection(proxy_endpoint(proxy), timeout=2).close()

    def test_injected_proxy_is_used_and_not_stopped_by_driver(self):
        proxy = EgressProxy(('example.com',), resolver=lambda host: ('93.184.216.34',),
                            connector=lambda address, timeout: SilentRemote())
        proxy.start()
        self.addCleanup(proxy.stop)
        driver, transport = self._url_driver(proxy=proxy, allowlist=('example.com',),
                                             url='https://example.com/acq')
        transport.queue(url_launch_callable())
        transport.queue(snapshot_payload(url='https://example.com/acq'))
        driver.open(make_goal())
        self.assertEqual(transport.commands[0]['proxy_server'], proxy.url)
        # 注入的代理真实工作，driver.egress_records 能看到实际 host/IP/决策。
        sock = socket.create_connection(proxy_endpoint(proxy), timeout=2)
        try:
            sock.sendall(b'CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n')
            head = sock.recv(4096)
        finally:
            sock.close()
        self.assertIn(b'HTTP/1.1 200', head)
        records = driver.egress_records
        self.assertTrue(any(record.decision == 'allowed' and record.host == 'example.com'
                            and record.resolved_ips == ('93.184.216.34',) for record in records))
        driver.close()
        # driver 只停止自有 proxy；注入实例仍由调用方管理并可继续使用。
        self.assertIs(driver.egress_proxy, proxy)
        socket.create_connection(proxy_endpoint(proxy), timeout=2).close()

    def test_launch_result_must_confirm_egress_isolation(self):
        def mismatch_proxy(command):
            return launch_payload(False, True, proxy_server='http://127.0.0.1:1')

        def wrong_webrtc(command):
            payload = launch_payload(False, True, proxy_server=command.get('proxy_server'))
            payload['webrtc_policy'] = 'default'
            return payload

        def bad_websocket_flag(command):
            payload = launch_payload(False, True, proxy_server=command.get('proxy_server'),
                                     websocket_route_blocked='yes')
            return payload

        for name, builder in (('proxy_mismatch', mismatch_proxy),
                              ('webrtc_policy', wrong_webrtc),
                              ('websocket_flag', bad_websocket_flag)):
            with self.subTest(case=name):
                driver, transport = self._url_driver()
                transport.queue(builder)
                with self.assertRaises(WorkerProtocolError):
                    driver.open(make_goal())
                # 拒绝启动时自有代理也必须停掉，不留下监听端口。
                proxy = driver.egress_proxy
                self.assertIsNotNone(proxy)
                host, port = proxy_endpoint(proxy)
                with self.assertRaises(OSError):
                    socket.create_connection((host, port), timeout=1)
                driver.close()

    def test_url_mode_launch_failure_stops_owned_proxy(self):
        driver, transport = self._url_driver()
        transport.fail_next('browser_launch_failed:missing browser')
        with self.assertRaises(WorkerProtocolError):
            driver.open(make_goal())
        proxy = driver.egress_proxy
        self.assertIsNotNone(proxy)
        host, port = proxy_endpoint(proxy)
        with self.assertRaises(OSError):
            socket.create_connection((host, port), timeout=1)
        driver.close()

    def test_url_mode_is_disabled_by_default(self):
        # URL 模式默认关闭：重定向/预连接可能触达私网端点，真实外部试点前需出网隔离。
        with self.assertRaises(live_driver.BrowserUnavailable) as caught:
            PlaywrightDriver(url='https://www.taptap.cn/example/activity-001',
                             transport=FakeTransport(), artifact_dir=self.artifact_dir)
        self.assertIn('url_mode_disabled', str(caught.exception))

    def test_navigate_rejects_cross_mode(self):
        self.open_driver()
        before = len(self.transport.commands)
        with self.assertRaises(ValueError):
            self.driver.navigate(url='https://www.taptap.cn/example/x')
        self.assertEqual(len(self.transport.commands), before)

    def test_navigate_requires_exactly_one_source(self):
        self.open_driver()
        with self.assertRaises(ValueError):
            self.driver.navigate()
        with self.assertRaises(ValueError):
            self.driver.navigate(url='https://example.com/', fixture_html='<html></html>')

    def test_close_is_idempotent_and_blocks_further_actions(self):
        view = self.open_driver()
        self.driver.close()
        self.driver.close()
        self.assertEqual(self.transport.commands[-1], {'command': 'close'})
        with self.assertRaises(WorkerLost):
            self.driver.open(make_goal())
        with self.assertRaises(WorkerLost):
            self.driver.inspect(view)

    def test_constructor_validation(self):
        with self.assertRaises(ValueError):
            PlaywrightDriver()
        with self.assertRaises(ValueError):
            PlaywrightDriver(url='https://example.com/', fixture_html='<html></html>')
        for bad_url in ('http://example.com/', 'https://user:pass@example.com/',
                        'https://example.com:8443/', 'https://localhost/', 'ftp://example.com/'):
            with self.subTest(url=bad_url):
                with self.assertRaises(ValueError):
                    PlaywrightDriver(url=bad_url, transport=FakeTransport(),
                                     artifact_dir=self.artifact_dir)
        with self.assertRaises(TypeError):
            self.driver.open('not-a-goal')

    def test_available_and_ocr_probe_shapes(self):
        ok, reason = available()
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(reason, str)
        self.assertEqual(ok, reason == 'ok')
        ocr_ok, ocr_reason = ocr_available()
        self.assertIsInstance(ocr_ok, bool)
        self.assertIsInstance(ocr_reason, str)
        self.assertEqual(ocr_ok, ocr_reason == 'ok')


# --------------------------------------------------------------------------- snapshot 映射
class SnapshotMappingTests(FakeDriverTestBase):
    def test_region_fields_and_visual_hint_mapping(self):
        regions = [
            region_payload('dom_1', role='article_body', bbox=(10, 20, 300, 400),
                           text='正文', scroll_y=20),
            region_payload('dom_2', role='poster', bbox=(0, 3200, 800, 500), text='',
                           visual_hint=True, scroll_y=3200),
            region_payload('dom_3', role='activity_rules', bbox=(0, 100, 500, 200), text='',
                           lazy=True, requires='click:展开规则', scroll_y=100),
            region_payload('dom_4', role='comments', bbox=(0, 200, 500, 200),
                           text='登录后查看评论', requires='login', scroll_y=200),
            region_payload('dom_5', role='captcha', bbox=(0, 0, 500, 200), text='安全验证',
                           blocked=True, scroll_y=0),
        ]
        view = self.open_driver(snapshot_payload(regions=regions))
        article = view.region('dom_1')
        self.assertEqual(article.bbox, (10, 20, 300, 400))
        self.assertFalse(article.visual_only)
        self.assertEqual(article.visual_text, '')
        poster = view.region('dom_2')
        # worker 的 visual_hint 只映射为 visual_only，不写任何占位文本。
        self.assertTrue(poster.visual_only)
        self.assertEqual(poster.visual_text, '')
        self.assertEqual(poster.text, '')
        lazy = view.region('dom_3')
        self.assertTrue(lazy.lazy)
        self.assertEqual(lazy.requires, 'click:展开规则')
        comments = view.region('dom_4')
        self.assertEqual(comments.requires, 'login')
        captcha = view.region('dom_5')
        self.assertTrue(captcha.blocked)
        self.assertEqual(captcha.role, 'captcha')
        self.assertEqual(view.viewport, (1440, 1000))
        self.assertEqual(view.scroll_height, 5200)
        self.assertEqual(view.regions, tuple(view.regions))

    def test_blocked_reason_is_preserved(self):
        view = self.open_driver(snapshot_payload(blocked_reason='captcha'))
        self.assertEqual(view.blocked_reason, 'captcha')

    def test_invalid_snapshot_payloads_raise_protocol_error(self):
        self.queue_launch()
        self.transport.queue({})  # 缺 page_version/url
        with self.assertRaises(WorkerProtocolError):
            self.driver.open(make_goal())

    def test_invalid_region_role_raises_protocol_error(self):
        self.queue_launch()
        self.transport.queue(snapshot_payload(regions=[region_payload(role='bogus_role')]))
        with self.assertRaises(WorkerProtocolError):
            self.driver.open(make_goal())

    def test_invalid_region_bbox_raises_protocol_error(self):
        self.queue_launch()
        self.transport.queue(snapshot_payload(regions=[region_payload(bbox=(0, 0, 0, 100))]))
        with self.assertRaises(WorkerProtocolError):
            self.driver.open(make_goal())

    def test_invalid_protocol_version_raises_protocol_error(self):
        self.transport.queue({'protocol': 'other-protocol', 'fixture_mode': True,
                              'browser': 'x'})
        self.transport.queue(snapshot_payload())
        with self.assertRaises(WorkerProtocolError):
            self.driver.open(make_goal())


# --------------------------------------------------------------------------- 观察/定位
class InspectLocateTests(FakeDriverTestBase):
    def test_inspect_refreshes_view_without_text(self):
        view = self.open_driver()
        refreshed = snapshot_payload(page_version=_PAGE_VERSION_B,
                                     regions=[region_payload('dom_1', text='更新后的正文'),
                                              region_payload('dom_2', role='poster',
                                                             bbox=(0, 300, 100, 100),
                                                             visual_hint=True)])
        self.transport.queue(refreshed)
        observation = self.driver.inspect(view)
        self.assertEqual(observation.action, 'inspect')
        self.assertEqual(observation.cost.units, 1)
        self.assertEqual(observation.cost.captures, 0)
        self.assertEqual(observation.text, '')
        self.assertEqual(observation.page_version, _PAGE_VERSION_B)
        self.assertEqual(observation.candidates, ('dom_1', 'dom_2'))
        self.assertIsNotNone(observation.view)
        self.assertEqual(observation.view.page_version, _PAGE_VERSION_B)
        self.assertEqual(self.driver.call_counts['observe'], 1)

    def test_inspect_blocked_page(self):
        view = self.open_driver(snapshot_payload(blocked_reason='captcha'))
        self.transport.queue(snapshot_payload(page_version=_PAGE_VERSION_B,
                                              blocked_reason='captcha'))
        observation = self.driver.inspect(view)
        self.assertTrue(observation.blocked)
        self.assertEqual(observation.note, 'blocked_page:captcha')

    def test_locate_orders_candidates_without_worker_call(self):
        regions = [
            region_payload('dom_1', role='navigation', bbox=(0, 0, 1440, 60), text='导航'),
            region_payload('dom_2', role='activity_rules', bbox=(0, 100, 500, 300),
                           text='', lazy=True, requires='click:展开规则'),
            region_payload('dom_3', role='article_body', bbox=(0, 400, 500, 300),
                           text='活动报名截止：2026年11月20日。'),
            region_payload('dom_4', role='poster', bbox=(0, 800, 400, 400), text='',
                           visual_hint=True),
        ]
        view = self.open_driver(snapshot_payload(regions=regions))
        before = len(self.transport.commands)
        observation = self.driver.locate(view, make_goal())
        self.assertEqual(observation.action, 'locate')
        self.assertEqual(observation.cost.units, 1)
        self.assertEqual(observation.candidates[0], 'dom_2')  # 区域先验命中
        self.assertEqual(observation.candidates, ('dom_2', 'dom_3', 'dom_4', 'dom_1'))
        self.assertEqual(len(self.transport.commands), before)


# --------------------------------------------------------------------------- DOM 提取
class ExtractDomTests(FakeDriverTestBase):
    def test_extract_dom_semantic_region(self):
        view = self.open_driver()
        self.transport.queue({'region_id': 'dom_1', 'role': 'activity_rules',
                              'bbox': [120, 1500, 1200, 700],
                              'text': '活动规则正文：截止 2026年11月30日。'})
        observation = self.driver.extract(view, view.region('dom_1'), 'dom')
        self.assertEqual(observation.action, 'extract')
        self.assertEqual(observation.extraction, 'dom')
        self.assertEqual(observation.cost.units, 1)
        self.assertEqual(observation.cost.captures, 0)
        self.assertIn('2026年11月30日', observation.text)
        self.assertIsNone(observation.capture_id)
        self.assertEqual(self.transport.commands[-1],
                         {'command': 'extract_dom', 'region_id': 'dom_1'})

    def test_extract_dom_body_virtual_region(self):
        view = self.open_driver(snapshot_payload(dom_text='整页 DOM 正文：合成样本。'))
        self.transport.queue({'region_id': 'dom:body', 'role': 'section',
                              'bbox': [0, 0, 1440, 5200],
                              'text': '整页 DOM 正文：合成样本。'})
        observation = self.driver.extract(
            view, model.Region(region_id='dom:body', role='section', bbox=(0, 0, 10, 10)), 'dom')
        self.assertEqual(observation.text, '整页 DOM 正文：合成样本。')
        self.assertEqual(observation.bbox, (0, 0, 1440, 5200))
        self.assertEqual(self.transport.commands[-1],
                         {'command': 'extract_dom', 'region_id': 'dom:body'})

    def test_extract_dom_viewport_band_is_local_synthesis(self):
        regions = [
            region_payload('dom_1', role='article_body', bbox=(0, 0, 1000, 300),
                           text='首屏正文'),
            region_payload('dom_2', role='comments', bbox=(0, 900, 1000, 300),
                           text='第二屏评论'),
        ]
        view = self.open_driver(snapshot_payload(regions=regions, scroll_height=3000))
        before = len(self.transport.commands)
        observation = self.driver.extract(
            view, model.Region(region_id='viewport:1', role='section', bbox=(0, 0, 10, 10)),
            'dom')
        self.assertEqual(observation.action, 'extract')
        self.assertEqual(observation.cost.units, 1)
        self.assertIn('第二屏评论', observation.text)
        self.assertNotIn('首屏正文', observation.text)  # 只合并与 y=850..1850 相交的区域
        self.assertEqual(len(self.transport.commands), before)
        self.assertIsNone(observation.capture_id)

    def test_extract_dom_unknown_region_raises_value_error(self):
        view = self.open_driver()
        self.transport.fail_next('unknown_region:dom_9')
        with self.assertRaises(ValueError):
            self.driver.extract(view, model.Region(region_id='dom_9', role='section',
                                                   bbox=(0, 0, 10, 10)), 'dom')

    def test_lazy_region_dom_not_loaded_note(self):
        view = self.open_driver(snapshot_payload(regions=[
            region_payload('dom_1', lazy=True, requires='click:展开规则', text='')]))
        self.transport.queue({'region_id': 'dom_1', 'role': 'activity_rules',
                              'bbox': [120, 1500, 1200, 700], 'text': ''})
        observation = self.driver.extract(view, view.region('dom_1'), 'dom')
        self.assertEqual(observation.text, '')
        self.assertEqual(observation.note, 'lazy_not_loaded')

    def test_unsupported_extraction_methods_raise_value_error(self):
        view = self.open_driver()
        for method in ('vlm', 'skill', 'http'):
            with self.subTest(method=method):
                with self.assertRaises(ValueError):
                    self.driver.extract(view, view.region('dom_1'), method)
        with self.assertRaises(ValueError):
            self.driver.extract(view, view.region('dom_1'), 'bogus')

    def test_blocked_page_dom_extract_returns_no_text(self):
        view = self.open_driver(snapshot_payload(blocked_reason='captcha'))
        before = len(self.transport.commands)
        observation = self.driver.extract(view, view.region('dom_1'), 'dom')
        self.assertTrue(observation.blocked)
        self.assertEqual(observation.text, '')
        self.assertEqual(observation.note, 'blocked_page:captcha')
        self.assertEqual(observation.cost.units, 1)
        self.assertEqual(len(self.transport.commands), before)

    def test_login_gated_region_extract_returns_no_text(self):
        view = self.open_driver(snapshot_payload(regions=[
            region_payload('dom_1', role='comments', text='玩家评论',
                           requires='login')]))
        before = len(self.transport.commands)
        observation = self.driver.extract(view, view.region('dom_1'), 'dom')
        self.assertTrue(observation.blocked)
        self.assertEqual(observation.text, '')
        self.assertEqual(observation.note, 'login_required')
        self.assertEqual(len(self.transport.commands), before)


# --------------------------------------------------------------------------- OCR 提取
class ExtractOcrTests(FakeDriverTestBase):
    def make_ocr_driver(self, ocr_engine) -> None:
        self.transport = FakeTransport()
        self.driver = PlaywrightDriver(fixture_html='<html></html>', transport=self.transport,
                                       artifact_dir=self.artifact_dir, ocr_engine=ocr_engine,
                                       timeout=5)

    def test_ocr_extract_returns_real_ocr_text_and_capture_id(self):
        engine = RecordingOcr('活动截止：2026年11月30日')
        self.make_ocr_driver(engine)
        view = self.open_driver(snapshot_payload(regions=[
            region_payload('dom_2', role='poster', bbox=(0, 3200, 800, 500), text='',
                           visual_hint=True)]))
        path, sha = self.prepare_ocr_extract('dom_2')
        observation = self.driver.extract(view, view.region('dom_2'), 'ocr')
        self.assertEqual(observation.extraction, 'ocr')
        self.assertIn('2026年11月30日', observation.text)
        self.assertEqual(observation.capture_id, 'capture_' + sha[:24])
        self.assertEqual(observation.cost.units, 3)
        self.assertEqual(observation.cost.captures, 1)
        self.assertEqual(observation.cost.ocr_calls, 1)
        # 视觉转录必须带“需对照截图与语义核验”的 note，不能当原文。
        self.assertEqual(observation.note, live_driver.OCR_TRANSCRIPTION_NOTE)
        self.assertIn('视觉转录', observation.note)
        self.assertIn('核验', observation.note)
        self.assertEqual(observation.bbox, (120, 1500, 1200, 700))
        self.assertEqual(engine.calls, [path])
        self.assertEqual(self.driver.capture_count, 1)
        self.assertEqual(self.transport.commands[-1],
                         {'command': 'screenshot', 'region_id': 'dom_2'})

    def test_ocr_extract_viewport_uses_viewport_index(self):
        engine = RecordingOcr('第一屏视觉文字')
        self.make_ocr_driver(engine)
        view = self.open_driver(snapshot_payload(scroll_height=3000))
        path, sha = self.write_capture()
        self.transport.queue(self.screenshot_payload(path, sha, bbox=(0, 850, 1440, 1000),
                                                     width=1440, height=1000, scroll_y=850))
        observation = self.driver.extract(
            view, model.Region(region_id='viewport:1', role='section', bbox=(0, 0, 10, 10)),
            'ocr')
        self.assertEqual(observation.text, '第一屏视觉文字')
        self.assertEqual(observation.bbox, (0, 850, 1440, 1000))
        self.assertEqual(observation.cost.captures, 1)
        self.assertEqual(self.transport.commands[-1],
                         {'command': 'screenshot', 'viewport_index': 1})
        self.assertEqual(len(engine.calls), 1)

    def test_ocr_unavailable_returns_empty_text_and_note(self):
        engine = RecordingOcr('', status='unavailable')
        self.make_ocr_driver(engine)
        view = self.open_driver(snapshot_payload(regions=[
            region_payload('dom_2', role='poster', text='', visual_hint=True)]))
        path, sha = self.prepare_ocr_extract('dom_2')
        observation = self.driver.extract(view, view.region('dom_2'), 'ocr')
        self.assertEqual(observation.text, '')
        self.assertEqual(observation.note, 'ocr_unavailable')
        self.assertEqual(observation.cost.captures, 1)  # 成本照记
        self.assertEqual(observation.cost.ocr_calls, 1)
        self.assertEqual(observation.capture_id, 'capture_' + sha[:24])
        self.assertEqual(len(engine.calls), 1)

    def test_ocr_engine_exception_is_treated_as_unavailable(self):
        def broken_engine(_path):
            raise RuntimeError('ocr crashed')

        self.make_ocr_driver(broken_engine)
        view = self.open_driver(snapshot_payload(regions=[
            region_payload('dom_2', role='poster', text='', visual_hint=True)]))
        self.prepare_ocr_extract('dom_2')
        observation = self.driver.extract(view, view.region('dom_2'), 'ocr')
        self.assertEqual(observation.text, '')
        self.assertEqual(observation.note, 'ocr_unavailable')

    def test_ocr_empty_result_is_not_fabricated(self):
        self.make_ocr_driver(RecordingOcr(''))
        view = self.open_driver(snapshot_payload(regions=[
            region_payload('dom_2', role='poster', text='', visual_hint=True)]))
        self.prepare_ocr_extract('dom_2')
        observation = self.driver.extract(view, view.region('dom_2'), 'ocr')
        self.assertEqual(observation.text, '')
        self.assertEqual(observation.note, '')

    def test_ocr_on_dom_body_virtual_region_raises_value_error(self):
        self.make_ocr_driver(RecordingOcr('x'))
        view = self.open_driver()
        before = len(self.transport.commands)
        with self.assertRaises(ValueError):
            self.driver.extract(
                view, model.Region(region_id='dom:body', role='section', bbox=(0, 0, 10, 10)),
                'ocr')
        self.assertEqual(len(self.transport.commands), before)

    def test_blocked_page_ocr_discards_text_but_keeps_capture(self):
        engine = RecordingOcr('安全验证 验证码')
        self.make_ocr_driver(engine)
        view = self.open_driver(snapshot_payload(blocked_reason='captcha', regions=[
            region_payload('dom_2', role='captcha', text='安全验证', blocked=True)]))
        path, sha = self.prepare_ocr_extract('dom_2')
        observation = self.driver.extract(view, view.region('dom_2'), 'ocr')
        self.assertTrue(observation.blocked)
        self.assertEqual(observation.text, '')
        self.assertEqual(observation.note, 'blocked_page:captcha')
        self.assertEqual(observation.capture_id, 'capture_' + sha[:24])
        self.assertEqual(observation.cost.ocr_calls, 1)
        self.assertEqual(len(engine.calls), 1)  # 截图 OCR 成本真实发生，但文字不入证据


# --------------------------------------------------------------------------- 截图校验
class ScreenshotValidationTests(FakeDriverTestBase):
    def test_screenshot_path_escape_raises_protocol_error(self):
        engine = RecordingOcr('x')
        self.make_ocr_driver(engine)
        view = self.open_driver(snapshot_payload(regions=[
            region_payload('dom_2', role='poster', text='', visual_hint=True)]))
        outside_dir = Path(tempfile.mkdtemp(prefix='acq-live-outside-'))
        self.addCleanup(shutil.rmtree, outside_dir, True)
        outside = outside_dir / 'frame-0.png'
        outside.write_bytes(b'evil')
        sha = hashlib.sha256(b'evil').hexdigest()
        self.transport.queue(self.screenshot_payload(outside, sha))
        with self.assertRaises(WorkerProtocolError):
            self.driver.extract(view, view.region('dom_2'), 'ocr')

    def test_screenshot_sha_mismatch_raises_protocol_error(self):
        engine = RecordingOcr('x')
        self.make_ocr_driver(engine)
        view = self.open_driver(snapshot_payload(regions=[
            region_payload('dom_2', role='poster', text='', visual_hint=True)]))
        path, _sha = self.write_capture()
        self.transport.queue(self.screenshot_payload(path, 'f' * 64))
        with self.assertRaises(WorkerProtocolError):
            self.driver.extract(view, view.region('dom_2'), 'ocr')

    def test_screenshot_missing_file_raises_protocol_error(self):
        engine = RecordingOcr('x')
        self.make_ocr_driver(engine)
        view = self.open_driver(snapshot_payload(regions=[
            region_payload('dom_2', role='poster', text='', visual_hint=True)]))
        missing = self.artifact_dir / 'frame-9.png'
        sha = hashlib.sha256(b'x').hexdigest()
        self.transport.queue(self.screenshot_payload(missing, sha))
        with self.assertRaises(WorkerProtocolError):
            self.driver.extract(view, view.region('dom_2'), 'ocr')

    def test_screenshot_invalid_bbox_raises_protocol_error(self):
        engine = RecordingOcr('x')
        self.make_ocr_driver(engine)
        view = self.open_driver(snapshot_payload(regions=[
            region_payload('dom_2', role='poster', text='', visual_hint=True)]))
        path, sha = self.write_capture()
        payload = self.screenshot_payload(path, sha)
        payload['bbox'] = [0, 0, 0, 100]
        self.transport.queue(payload)
        with self.assertRaises(WorkerProtocolError):
            self.driver.extract(view, view.region('dom_2'), 'ocr')

    def make_ocr_driver(self, ocr_engine) -> None:
        self.transport = FakeTransport()
        self.driver = PlaywrightDriver(fixture_html='<html></html>', transport=self.transport,
                                       artifact_dir=self.artifact_dir, ocr_engine=ocr_engine,
                                       timeout=5)


# --------------------------------------------------------------------------- 交互
class InteractTests(FakeDriverTestBase):
    def test_interact_returns_new_view_and_version(self):
        view = self.open_driver(snapshot_payload(regions=[
            region_payload('dom_1', lazy=True, requires='click:展开规则', text='')]))
        changed = snapshot_payload(page_version=_PAGE_VERSION_B, regions=[
            region_payload('dom_1', text='展开后的规则正文：2026年11月20日。')])
        changed['changed'] = True
        self.transport.queue(changed)
        observation = self.driver.interact(view, view.region('dom_1'))
        self.assertEqual(observation.action, 'interact')
        self.assertEqual(observation.cost.units, 2)
        self.assertIsNotNone(observation.view)
        self.assertNotEqual(observation.page_version, view.page_version)
        self.assertIn('2026年11月20日', observation.view.region('dom_1').text)
        self.assertEqual(self.transport.commands[-1],
                         {'command': 'interact', 'region_id': 'dom_1', 'action': 'click'})

    def test_interact_without_change_raises_value_error(self):
        view = self.open_driver(snapshot_payload(regions=[
            region_payload('dom_1', lazy=True, requires='click:展开规则', text='')]))
        unchanged = snapshot_payload(regions=[
            region_payload('dom_1', lazy=True, requires='click:展开规则', text='')])
        unchanged['changed'] = False
        self.transport.queue(unchanged)
        with self.assertRaises(ValueError):
            self.driver.interact(view, view.region('dom_1'))

    def test_interact_unknown_region_raises_value_error(self):
        view = self.open_driver()
        self.transport.fail_next('unknown_region:dom_9')
        with self.assertRaises(ValueError):
            self.driver.interact(view, model.Region(region_id='dom_9', role='section',
                                                    bbox=(0, 0, 10, 10)))

    def test_interact_click_failure_raises_value_error(self):
        view = self.open_driver()
        self.transport.fail_next('click_failed:element is not visible')
        with self.assertRaises(ValueError):
            self.driver.interact(view, view.region('dom_1'))

    def test_interact_virtual_regions_raise_value_error_without_worker_call(self):
        view = self.open_driver()
        before = len(self.transport.commands)
        for region_id in ('dom:body', 'viewport:0'):
            with self.subTest(region_id=region_id):
                with self.assertRaises(ValueError):
                    self.driver.interact(
                        view, model.Region(region_id=region_id, role='section',
                                           bbox=(0, 0, 10, 10)))
        self.assertEqual(len(self.transport.commands), before)


# --------------------------------------------------------------------------- 断线/协议错误
class TransportFailureTests(FakeDriverTestBase):
    def test_worker_lost_during_observe(self):
        view = self.open_driver()
        self.transport.queue({'__worker_lost__': 'process exited'})
        with self.assertRaises(WorkerLost):
            self.driver.inspect(view)

    def test_worker_crash_then_action_raises_worker_lost(self):
        view = self.open_driver()
        self.transport.crash()
        with self.assertRaises(WorkerLost):
            self.driver.inspect(view)

    def test_worker_command_error_raises_protocol_error(self):
        view = self.open_driver()
        self.transport.fail_next('page_not_ready')
        with self.assertRaises(WorkerProtocolError):
            self.driver.inspect(view)

    def test_malformed_worker_payload_raises_protocol_error(self):
        view = self.open_driver()
        self.transport.queue({'page_version': _PAGE_VERSION})  # 缺 url/regions 等字段
        with self.assertRaises(WorkerProtocolError):
            self.driver.inspect(view)

    def test_operation_after_worker_lost_close_swallows_error(self):
        view = self.open_driver()
        self.transport.crash()
        with self.assertRaises(WorkerLost):
            self.driver.inspect(view)
        self.driver.close()  # 断线后关闭不得抛错


# --------------------------------------------------------------------------- worker 静态检查
class WorkerSourceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), '本机没有 node，跳过 worker 语法检查')
    def test_node_check_worker_syntax(self):
        result = subprocess.run(['node', '--check', str(WORKER_PATH)],
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.stderr.strip())


# --------------------------------------------------------------------------- URL 安全负例
@unittest.skipUnless(_REAL_BROWSER_OK, f'真实浏览器资源不可用: {_REAL_BROWSER_REASON}')
class UrlSafetyWorkerTests(unittest.TestCase):
    """真实 worker 的 URL 安全负例：非 HTTPS / 非公网主机必须被拒绝且进程不崩溃。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix='acq-live-url-')
        self.addCleanup(self._tmp.cleanup)
        self.proxy = EgressProxy(('example.com',))
        self.proxy.start()
        self.addCleanup(self.proxy.stop)
        self.transport = ProcessTransport(artifact_dir=Path(self._tmp.name), timeout=90)
        self.transport.start()
        self.addCleanup(self._safe_close)
        launch = self.transport.call({
            'command': 'launch', 'fixture_mode': False, 'allow_url_mode': True,
            'viewport': [1440, 1000], 'artifact_dir': str(Path(self._tmp.name)),
            'proxy_server': self.proxy.url,
        }, timeout=90)
        self.assertEqual(launch.get('protocol'), live_driver.WORKER_PROTOCOL)
        self.assertEqual(launch.get('proxy_server'), self.proxy.url)
        self.assertEqual(launch.get('webrtc_policy'), 'disable_non_proxied_udp')
        self.assertIsInstance(launch.get('websocket_route_blocked'), bool)

    def _safe_close(self) -> None:
        try:
            self.transport.close()
        except Exception:
            pass

    def test_worker_rejects_http_and_non_public_https(self):
        cases = [('http://example.com/', 'public_https_required'),
                 ('https://localhost/', 'non_public_address'),
                 ('https://127.0.0.1/', 'non_public_address'),
                 ('https://user:pass@example.com/', 'public_https_required'),
                 ('https://example.com:8443/', 'public_https_required')]
        for url, expected in cases:
            with self.subTest(url=url):
                with self.assertRaises(WorkerProtocolError) as caught:
                    self.transport.call({'command': 'open', 'url': url}, timeout=30)
                self.assertIn(expected, str(caught.exception))
                self.assertTrue(self.transport.alive)  # 错误响应不得打死 worker
        with self.assertRaises(WorkerProtocolError) as caught:
            self.transport.call({'command': 'unknown_command'})
        self.assertIn('unknown_command', str(caught.exception))
        self.assertTrue(self.transport.alive)


@unittest.skipUnless(_REAL_BROWSER_OK, f'真实浏览器资源不可用: {_REAL_BROWSER_REASON}')
class UrlModeGateWorkerTests(unittest.TestCase):
    """URL 模式默认关闭，且必须提供本地过滤代理（切片 3 出网隔离）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix='acq-live-url-gate-')
        self.addCleanup(self._tmp.cleanup)
        self.transport = ProcessTransport(artifact_dir=Path(self._tmp.name), timeout=90)
        self.transport.start()
        self.addCleanup(self._safe_close)

    def _safe_close(self) -> None:
        try:
            self.transport.close()
        except Exception:
            pass

    def _base(self) -> dict:
        return {'command': 'launch', 'fixture_mode': False, 'viewport': [1440, 1000],
                'artifact_dir': str(Path(self._tmp.name))}

    def _start_proxy(self) -> EgressProxy:
        proxy = EgressProxy(('example.com',))
        proxy.start()
        self.addCleanup(proxy.stop)
        return proxy

    def test_url_mode_requires_explicit_opt_in_and_egress_proxy(self):
        base = self._base()
        with self.assertRaises(WorkerProtocolError) as caught:
            self.transport.call(base, timeout=90)
        self.assertIn('url_mode_disabled', str(caught.exception))
        self.assertTrue(self.transport.alive, '拒绝 URL 模式不得打死 worker')
        # 开了 allow_url_mode 但没有 proxy：直接返回 egress_proxy_required，不启动浏览器。
        with self.assertRaises(WorkerProtocolError) as caught:
            self.transport.call({**base, 'allow_url_mode': True}, timeout=90)
        self.assertIn('egress_proxy_required', str(caught.exception))
        self.assertTrue(self.transport.alive, '拒绝缺代理不得打死 worker')
        proxy = self._start_proxy()
        result = self.transport.call({**base, 'allow_url_mode': True,
                                      'proxy_server': proxy.url}, timeout=90)
        self.assertEqual(result.get('protocol'), live_driver.WORKER_PROTOCOL)
        self.assertIs(result.get('fixture_mode'), False)
        self.assertEqual(result.get('proxy_server'), proxy.url)
        self.assertEqual(result.get('webrtc_policy'), 'disable_non_proxied_udp')
        self.assertIsInstance(result.get('websocket_route_blocked'), bool)

    def test_worker_rejects_invalid_or_missing_proxy_server(self):
        base = {**self._base(), 'allow_url_mode': True}
        for value, expected in ((None, 'egress_proxy_required'),
                                ('', 'egress_proxy_required'),
                                ('not-a-url', 'invalid_proxy_server'),
                                ('https://127.0.0.1:9', 'invalid_proxy_server'),
                                ('http://example.com:9', 'invalid_proxy_server'),
                                ('http://127.0.0.1', 'invalid_proxy_server'),
                                ('http://user:pass@127.0.0.1:9', 'invalid_proxy_server'),
                                ('http://127.0.0.1:9/path', 'invalid_proxy_server')):
            with self.subTest(proxy_server=value):
                with self.assertRaises(WorkerProtocolError) as caught:
                    self.transport.call({**base, 'proxy_server': value}, timeout=90)
                self.assertIn(expected, str(caught.exception))
                self.assertTrue(self.transport.alive)
        # 拒绝后仍可用合法代理启动，说明 worker 没有被错误请求污染。
        proxy = self._start_proxy()
        result = self.transport.call({**base, 'proxy_server': proxy.url}, timeout=90)
        self.assertEqual(result.get('protocol'), live_driver.WORKER_PROTOCOL)

    def test_fixture_launch_reports_isolation_fields_without_proxy(self):
        proxy = self._start_proxy()
        result = self.transport.call({
            'command': 'launch', 'fixture_mode': True, 'viewport': [1440, 1000],
            'artifact_dir': str(Path(self._tmp.name)), 'proxy_server': proxy.url,
        }, timeout=90)
        self.assertEqual(result.get('protocol'), live_driver.WORKER_PROTOCOL)
        self.assertIs(result.get('fixture_mode'), True)
        # fixture 模式禁网，不使用代理：隔离字段为空但能力标记仍要汇报。
        self.assertIsNone(result.get('proxy_server'))
        self.assertIsNone(result.get('webrtc_policy'))
        self.assertIsInstance(result.get('websocket_route_blocked'), bool)


@unittest.skipUnless(_REAL_BROWSER_OK, f'真实浏览器资源不可用: {_REAL_BROWSER_REASON}')
class RealBrowserEgressIsolationTests(unittest.TestCase):
    """真实 Chromium + 真实 EgressProxy：浏览器必须经代理出网（不读取真实站点内容）。

    这里只做负例：代理把 URL 主机的解析结果固定为私网，或拒绝上游连接，Chromium 因此
    无法建立隧道。DNS 结果由注入 resolver 固定，但**代理校验、403 响应与 Chromium 的
    代理行为都由真实组件执行**，不 mock 核心校验逻辑。测试前用真实 DNS 确认示例域名
    可被 worker 自己解析为公网地址；离线环境自动 skip。
    """

    HOST = 'example.com'

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix='acq-live-egress-')
        self.addCleanup(self._tmp.cleanup)
        self.artifact_dir = Path(self._tmp.name)

    def _public_addresses(self) -> list[str]:
        try:
            infos = socket.getaddrinfo(self.HOST, 443, type=socket.SOCK_STREAM)
        except OSError:
            self.skipTest('本机 DNS 不可用，跳过真实浏览器出网代理测试（CI 无外网）')
        addresses = [item[4][0] for item in infos if is_public_address(item[4][0])]
        if not addresses:
            self.skipTest('示例域名解析不到公网地址，跳过真实浏览器出网代理测试')
        return addresses

    def _make_driver(self, resolver, connector):
        proxy = EgressProxy((self.HOST,), resolver=resolver, connector=connector, timeout=5)
        proxy.start()
        self.addCleanup(proxy.stop)
        driver = PlaywrightDriver(url=f'https://{self.HOST}/acq-egress-negative',
                                  allow_network=True, egress_allowlist=(self.HOST,),
                                  proxy=proxy, artifact_dir=self.artifact_dir, timeout=90)
        self.addCleanup(driver.close)
        return driver, proxy

    def test_real_browser_uses_proxy_and_rejects_private_dns(self):
        self._public_addresses()  # worker 自己的 DNS 公网校验必须先能通过
        connector_calls: list[tuple[str, int]] = []

        def resolver(host):
            self.assertEqual(host, self.HOST)
            return ('10.0.0.5',)

        def connector(address, timeout):
            connector_calls.append(tuple(address))
            raise OSError('私网解析被拒后不得发起上游连接')

        driver, proxy = self._make_driver(resolver, connector)
        with self.assertRaises(WorkerProtocolError):
            driver.open(make_goal())
        launch = driver.launch_info
        self.assertEqual(launch.get('proxy_server'), proxy.url)
        self.assertEqual(launch.get('webrtc_policy'), 'disable_non_proxied_udp')
        self.assertIsInstance(launch.get('websocket_route_blocked'), bool)
        records = driver.egress_records
        self.assertTrue(any(record.decision == 'denied' and record.host == self.HOST
                            and 'non_public_address' in record.reason for record in records),
                        records)
        self.assertEqual(connector_calls, [])

    def test_real_browser_pins_validated_public_ip(self):
        public_ip = self._public_addresses()[0]
        connector_calls: list[tuple[str, int]] = []

        def resolver(host):
            self.assertEqual(host, self.HOST)
            return (public_ip,)

        def connector(address, timeout):
            connector_calls.append(tuple(address))
            raise OSError('tls endpoint intentionally unavailable')

        driver, proxy = self._make_driver(resolver, connector)
        with self.assertRaises(WorkerProtocolError):
            driver.open(make_goal())
        self.assertTrue(connector_calls, '真实 Chromium 必须经代理发起 CONNECT')
        for address in connector_calls:
            self.assertEqual(address[1], 443)
            self.assertEqual(address[0], public_ip)   # 连接的是已验证 IP
            self.assertNotEqual(address[0], self.HOST)  # 不是 hostname
        self.assertTrue(any(record.decision == 'denied'
                            and 'connect_failed' in record.reason
                            for record in driver.egress_records))


# --------------------------------------------------------------------------- 真实浏览器 smoke
_REAL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="500">'
    '<rect width="900" height="500" fill="#1e3a8a"/>'
    '<text x="450" y="200" font-size="64" fill="#ffffff" text-anchor="middle" '
    'font-family="Microsoft YaHei, sans-serif">报名截止：2026年11月30日</text>'
    '<text x="450" y="320" font-size="40" fill="#ffffff" text-anchor="middle" '
    'font-family="Microsoft YaHei, sans-serif">参与方式：完成本地合成任务</text></svg>'
)
_REAL_SVG_URL = 'data:image/svg+xml;base64,' + base64.b64encode(
    _REAL_SVG.encode('utf-8')).decode('ascii')

REAL_FIXTURE_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>活动公告（本地合成场景，不是真实站点）</title>
<style>
 body{margin:0;font-family:"Microsoft YaHei",sans-serif;color:#222;background:#fff}
 nav,header{background:#123;color:#fff;padding:12px} main{max-width:1000px;margin:0 auto;padding:24px}
 footer{background:#eee;padding:12px;color:#666} .filler{height:2600px;background:#fafafa}
 figure{margin:0;padding:16px;border:1px solid #ddd}
</style></head><body>
<!-- 本地合成场景，不是真实站点 -->
<nav>社区 首页 活动 我的</nav>
<main>
<h1>活动公告（本地合成场景）</h1>
<article><h2>活动说明</h2><p>本页面为本地合成的浏览器测试场景，不指向真实站点。</p></article>
<section aria-label="活动规则">
  <h2>活动规则</h2>
  <button type="button" id="expand-rules">展开规则</button>
  <div id="rules-body" hidden></div>
</section>
<div class="filler">占位长页内容（合成，用于把海报推到第三屏之后）。</div>
<figure id="poster" data-region-role="poster"><img width="900" height="500" alt="" src="__DATA_URL__"></figure>
<section aria-label="玩家评论"><h2>玩家评论</h2><p>登录后查看评论</p></section>
</main>
<footer>合成样本，仅用于本地机制评测</footer>
<script>
document.getElementById('expand-rules').addEventListener('click', function () {
  var body = document.getElementById('rules-body');
  body.hidden = false;
  body.innerHTML = '<p>报名截止：2026年11月20日。</p><p>参与方式：完成本地合成任务后领取限定徽章。</p>';
  this.textContent = '收起规则';
});
</script>
</body></html>""".replace('__DATA_URL__', _REAL_SVG_URL)

SECOND_FIXTURE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>第二个本地合成页面</title></head><body>
<!-- 本地合成场景，不是真实站点 -->
<main><h1>第二个本地合成页面</h1><p>报名截止：2026年12月1日。</p></main>
</body></html>"""


@unittest.skipUnless(_REAL_BROWSER_OK, f'真实浏览器资源不可用: {_REAL_BROWSER_REASON}')
class RealBrowserDriverTests(unittest.TestCase):
    """真实 Edge + Playwright 冒烟：只验证本地合成 HTML 上的机制，不访问真实站点。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix='acq-live-real-')
        self.addCleanup(self._tmp.cleanup)
        self.artifact_dir = Path(self._tmp.name)
        self.driver = PlaywrightDriver(fixture_html=REAL_FIXTURE_HTML,
                                       artifact_dir=self.artifact_dir, timeout=90)
        self.addCleanup(self._safe_close)

    def _safe_close(self) -> None:
        try:
            self.driver.close()
        except Exception:
            pass

    def test_real_open_observe_interact_extract(self):
        view = self.driver.open(make_goal())
        self.assertIsInstance(view, model.PageView)
        self.assertEqual(self.driver.session_launches, 1)
        self.assertGreaterEqual(len(view.regions), 4)
        roles = view.roles()
        self.assertIn('activity_rules', roles)
        self.assertIn('poster', roles)
        self.assertIn('navigation', roles)
        self.assertIn('footer', roles)
        # 折叠规则在交互前不可读，且弹窗/视频类资源已被处理（fixture 禁网，无网络请求可断言）
        lazy = next(region for region in view.regions
                    if region.role == 'activity_rules' and region.lazy)
        self.assertEqual(lazy.text, '')
        self.assertTrue((lazy.requires or '').startswith('click:'))
        self.assertNotIn('2026年11月20日', view.dom_text)
        poster = next(region for region in view.regions if region.role == 'poster')
        self.assertTrue(poster.visual_only)
        self.assertEqual(poster.visual_text, '')

        observation = self.driver.inspect(view)
        self.assertEqual(observation.action, 'inspect')
        self.assertEqual(observation.page_version, view.page_version)
        self.assertIsNotNone(observation.view)

        interacted = self.driver.interact(observation.view, lazy)
        self.assertIsNotNone(interacted.view)
        self.assertNotEqual(interacted.page_version, view.page_version)
        revealed = interacted.view.region(lazy.region_id)
        self.assertFalse(revealed.lazy)
        dom = self.driver.extract(interacted.view, revealed, 'dom')
        self.assertIn('2026年11月20日', dom.text)
        self.assertEqual(dom.extraction, 'dom')
        self.assertEqual(self.driver.session_launches, 1)
        self.assertGreaterEqual(self.driver.call_counts.get('interact', 0), 1)

    def test_real_session_reuse_and_page_version_stability(self):
        first = self.driver.open(make_goal())
        repeated = self.driver.inspect(first)
        self.assertEqual(repeated.page_version, first.page_version)
        second = self.driver.navigate(fixture_html=SECOND_FIXTURE_HTML)
        self.assertNotEqual(second.page_version, first.page_version)
        back = self.driver.navigate(fixture_html=REAL_FIXTURE_HTML)
        self.assertEqual(back.page_version, first.page_version)  # 内容确定 ⇒ 版本稳定
        self.assertEqual(self.driver.session_launches, 1)
        self.assertEqual(self.driver.call_counts['launch'], 1)

    def test_real_screenshot_sha256_and_artifact_dir(self):
        view = self.driver.open(make_goal())
        poster = next(region for region in view.regions if region.role == 'poster')
        self.assertTrue(poster.visual_only)
        # 通过 ocr 通道触发区域截图；OCR 结果是否可读由 test_real_ocr 断言。
        observation = self.driver.extract(view, poster, 'ocr')
        self.assertIsNotNone(observation.capture_id)
        self.assertEqual(self.driver.capture_count, 1)
        files = sorted(self.artifact_dir.glob('frame-*.png'))
        self.assertEqual(len(files), 1)
        data = files[0].read_bytes()
        expected = 'capture_' + hashlib.sha256(data).hexdigest()[:24]
        self.assertEqual(observation.capture_id, expected)
        self.assertEqual(observation.bbox, poster.bbox)
        self.assertTrue(str(files[0].resolve()).startswith(str(self.artifact_dir.resolve())))

    @unittest.skipUnless(_OCR_OK, f'Windows OCR 不可用: {_OCR_REASON}')
    def test_real_ocr_reads_poster_text_as_evidence(self):
        view = self.driver.open(make_goal())
        poster = next(region for region in view.regions if region.role == 'poster')
        observation = self.driver.extract(view, poster, 'ocr')
        self.assertEqual(observation.note, live_driver.OCR_TRANSCRIPTION_NOTE)
        self.assertIn('视觉转录', observation.note)
        self.assertIn('2026', observation.text)
        self.assertIn('30', observation.text)
        self.assertFalse(observation.blocked)
        # 证据只来自真实 OCR 输出，不依赖任何占位文本；且 OCR 文本被标记为视觉转录。
        self.assertNotEqual(observation.text, poster.visual_text)
        self.assertEqual(poster.visual_text, '')

    def test_real_fixture_mode_blocks_websocket(self):
        import socket

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(1.5)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        html = ('<!doctype html><html><head><meta charset="utf-8"><title>ws 负例</title></head>'
                '<body><main><p>本地合成 WebSocket 负例，不是真实站点。</p></main>'
                f'<script>try{{new WebSocket("ws://127.0.0.1:{port}/ws");}}catch(e){{}}</script>'
                '</body></html>')
        self.driver.open(make_goal())
        self.driver.navigate(fixture_html=html)
        try:
            connection, _address = listener.accept()
        except socket.timeout:
            connection = None
        if connection is not None:
            connection.close()
            self.fail('夹具模式未拦截 WebSocket：页面连接到达本机监听端口')
        # routeWebSocket 可用时应有能力标记；旧版 Playwright 时由页面内 init script 兜底。
        self.assertIn('websocket_route_blocked', self.driver.launch_info)


if __name__ == '__main__':
    unittest.main()
