"""``acquisition.egress_proxy`` 测试（V4 切片 3 出网隔离）。

全部用例都通过真实监听端口 + 真实 TCP CONNECT 请求验证；DNS 与上游连接用注入的假
resolver / connector 固定（不依赖外网），但**不改写任何被测校验逻辑**：allowlist
匹配、公网 IP 判定、解析一次后的 IP pinning、403/200 响应、字节计数都由
:class:`acquisition.egress_proxy.EgressProxy` 真实执行。

覆盖点与冻结文档 §2/§5 对齐：

- allowlist：精确 host、子域、大小写不敏感、前后缀混淆拒绝、空列表拒绝全部；
- 网络位置：loopback / 私网 / 链路本地 / 保留 / 组播 / 未指定 / IPv4-mapped 全拒绝；
- DNS：多答案含私网拒绝、空答案拒绝、解析失败拒绝、**解析一次**（不二次 DNS）、
  每次 CONNECT 重新校验（私网 rebinding 拒绝）；
- pinning：假 connector 断言连接目标是已验证 IP 而非 hostname；
- 协议：非 CONNECT 方法、非 443 端口、非法请求行、超大请求行一律 403；
- 记录：allowed/denied 均落 ConnectionRecord，host/IP/原因/双向字节/时间可核对；
- 生命周期：start 幂等、stop 关闭监听与活动连接、可重复 stop。
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
import unittest
from urllib.parse import urlsplit

from acquisition.egress_proxy import (
    CONNECT_PORT,
    ConnectionRecord,
    EgressDenied,
    EgressProxy,
    is_public_address,
)

# 公开文档保留地址（只用于注入 resolver，代理不会真的连外网）。
TRUSTED_IP = '93.184.216.34'
SECOND_PUBLIC_IP = '8.8.8.8'
PRIVATE_IPS = (
    '127.0.0.1', '10.0.0.5', '172.16.5.4', '192.168.1.1', '169.254.10.1',
    '0.0.0.0', '100.64.0.1', '198.18.0.1', '198.19.255.254', '224.0.0.1',
    '240.0.0.1', '::1', 'fe80::1', 'fc00::1', 'ff02::1', '2001:db8::1',
    '::ffff:127.0.0.1', '::ffff:10.0.0.5',
)
PUBLIC_IPS = (TRUSTED_IP, SECOND_PUBLIC_IP, '2606:4700:4700::1111')


# --------------------------------------------------------------------------- 传输辅助
def _endpoint(proxy: EgressProxy) -> tuple[str, int]:
    parsed = urlsplit(proxy.url)
    assert parsed.scheme == 'http' and parsed.hostname, proxy.url
    return parsed.hostname, int(parsed.port)


def _open_socket(proxy: EgressProxy) -> socket.socket:
    host, port = _endpoint(proxy)
    return socket.create_connection((host, port), timeout=5)


def _connect_request(target: str, *, method: str = 'CONNECT') -> bytes:
    return (f'{method} {target} HTTP/1.1\r\nHost: {target}\r\n'
            'Proxy-Connection: keep-alive\r\n\r\n').encode('ascii')


def _read_head(sock: socket.socket, timeout: float = 5.0) -> bytes:
    sock.settimeout(timeout)
    data = b''
    while b'\r\n\r\n' not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _read_until_eof(sock: socket.socket, timeout: float = 5.0) -> bytes:
    sock.settimeout(timeout)
    data = b''
    while True:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        data += chunk
    return data


def _wait_for(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- 假上游
class FakeRemote:
    """假上游连接：记录代理转发的字节，按脚本回送 reply 后 EOF。"""

    def __init__(self, reply: bytes = b'') -> None:
        self.reply = bytearray(reply)
        self.sent = b''
        self.closed = False
        self.timeout = None

    def settimeout(self, value) -> None:
        self.timeout = value

    def sendall(self, data: bytes) -> None:
        self.sent += bytes(data)

    def recv(self, size: int) -> bytes:
        if self.reply:
            chunk = bytes(self.reply[:size])
            del self.reply[:size]
            return chunk
        return b''

    def shutdown(self, how) -> None:  # pragma: no cover - 由代理调用但无需行为
        pass

    def close(self) -> None:
        self.closed = True


class BlockingRemote(FakeRemote):
    """recv 阻塞到 gate 置位后才 EOF，用于验证并发连接上限。"""

    def __init__(self, gate: threading.Event) -> None:
        super().__init__(b'')
        self._gate = gate

    def recv(self, size: int) -> bytes:
        while not self._gate.wait(0.05):
            if self.closed:
                return b''
        return b''


class RecordingConnector:
    """记录连接目标 ``(ip, 443)`` 的假 connector；绝不会拿到 hostname。"""

    def __init__(self, factory=None, error: Exception | None = None) -> None:
        self.calls: list[tuple[tuple[str, int], float]] = []
        self._factory = factory
        self._error = error

    def __call__(self, address, timeout: float):
        self.calls.append((tuple(address), timeout))
        if self._error is not None:
            raise self._error
        if self._factory is None:
            return FakeRemote()
        return self._factory()


# --------------------------------------------------------------------------- 基类
class ProxyTestBase(unittest.TestCase):
    def start_proxy(self, allowlist=('example.com',), *, resolver=None, connector=None,
                    **kwargs) -> EgressProxy:
        proxy = EgressProxy(allowlist, resolver=resolver, connector=connector, **kwargs)
        proxy.start()
        self.addCleanup(proxy.stop)
        return proxy

    def open_client(self, proxy: EgressProxy) -> socket.socket:
        sock = _open_socket(proxy)
        self.addCleanup(sock.close)
        return sock

    def expect_denied(self, proxy: EgressProxy, target: str, *, method: str = 'CONNECT',
                      status: int = 403) -> ConnectionRecord:
        sock = self.open_client(proxy)
        try:
            sock.sendall(_connect_request(target, method=method))
            head = _read_head(sock)
        finally:
            sock.close()
        self.assertIn(f'HTTP/1.1 {status}'.encode('ascii'), head, head[:200])
        self.assertTrue(_wait_for(lambda: any(r.decision == 'denied' for r in proxy.records)),
                        proxy.records)
        return [record for record in proxy.records if record.decision == 'denied'][-1]

    def expect_allowed(self, proxy: EgressProxy, target: str = 'example.com:443',
                       *, early_payload: bytes = b'') -> socket.socket:
        sock = self.open_client(proxy)
        sock.sendall(_connect_request(target) + early_payload)
        head = _read_head(sock)
        self.assertIn(b'HTTP/1.1 200 Connection Established', head, head[:200])
        return sock

    def finalised_allowed(self, proxy: EgressProxy, *, bytes_in: int | None = None,
                          bytes_out: int | None = None) -> ConnectionRecord:
        def ready() -> bool:
            for record in proxy.records:
                if record.decision != 'allowed':
                    continue
                if bytes_in is not None and record.bytes_in != bytes_in:
                    continue
                if bytes_out is not None and record.bytes_out != bytes_out:
                    continue
                return True
            return False

        self.assertTrue(_wait_for(ready), proxy.records)
        return [record for record in proxy.records if record.decision == 'allowed'][-1]


# --------------------------------------------------------------------------- 公网判定
class PublicAddressTests(unittest.TestCase):
    def test_public_addresses_accepted(self):
        for address in PUBLIC_IPS:
            with self.subTest(address=address):
                self.assertTrue(is_public_address(address))

    def test_non_public_addresses_rejected(self):
        for address in PRIVATE_IPS:
            with self.subTest(address=address):
                self.assertFalse(is_public_address(address))

    def test_garbage_and_special_text_rejected(self):
        for value in ('', '   ', 'not-an-ip', 'example.com', '999.1.1.1', '[::1'):
            with self.subTest(value=value):
                self.assertFalse(is_public_address(value))


# --------------------------------------------------------------------------- 允许列表
class AllowlistTests(ProxyTestBase):
    def _allowed_proxy(self, allowlist, *, connector=None):
        return self.start_proxy(allowlist, resolver=lambda host: (TRUSTED_IP,),
                                connector=connector or RecordingConnector())

    def test_exact_host_allowed(self):
        connector = RecordingConnector()
        proxy = self._allowed_proxy(('example.com',), connector=connector)
        sock = self.expect_allowed(proxy, 'example.com:443')
        sock.close()
        record = self.finalised_allowed(proxy)
        self.assertEqual(record.host, 'example.com')
        self.assertEqual(record.port, 443)
        self.assertEqual(record.resolved_ips, (TRUSTED_IP,))
        self.assertEqual(connector.calls[0][0], (TRUSTED_IP, 443))

    def test_subdomain_allowed_and_case_insensitive(self):
        proxy = self._allowed_proxy(('Example.COM',))
        for target in ('example.com:443', 'www.example.com:443', 'A.B.Example.Com:443'):
            with self.subTest(target=target):
                sock = self.expect_allowed(proxy, target)
                sock.close()
        hosts = [record.host for record in proxy.records if record.decision == 'allowed']
        self.assertEqual(hosts, ['example.com', 'www.example.com', 'a.b.example.com'])

    def test_suffix_and_prefix_confusion_rejected(self):
        proxy = self._allowed_proxy(('example.com',))
        for target in ('notexample.com:443', 'example.com.evil.test:443',
                       'example.co:443', 'example.com:80'):
            with self.subTest(target=target):
                self.expect_denied(proxy, target)
        reasons = [record.reason for record in proxy.records]
        self.assertEqual(reasons[:2], ['host_not_allowed', 'host_not_allowed'])
        self.assertIn('connect_port_forbidden:80', reasons[-1])

    def test_empty_allowlist_rejects_all(self):
        proxy = self.start_proxy((), resolver=lambda host: (TRUSTED_IP,))
        record = self.expect_denied(proxy, 'example.com:443')
        self.assertEqual(record.reason, 'host_not_allowed')
        self.assertEqual(record.resolved_ips, ())

    def test_ip_literal_allowlist_exact_match(self):
        proxy = self.start_proxy((TRUSTED_IP,), resolver=lambda host: (host,),
                                 connector=RecordingConnector())
        sock = self.expect_allowed(proxy, f'{TRUSTED_IP}:443')
        sock.close()
        record = self.finalised_allowed(proxy)
        self.assertEqual(record.host, TRUSTED_IP)
        self.assertEqual(record.resolved_ips, (TRUSTED_IP,))


# --------------------------------------------------------------------------- 公网 IP 强制
class NonPublicResolutionTests(ProxyTestBase):
    def test_private_and_reserved_ips_rejected_without_connecting(self):
        for ip in PRIVATE_IPS:
            with self.subTest(ip=ip):
                connector = RecordingConnector()
                proxy = self.start_proxy(('example.com',), resolver=lambda host, ip=ip: (ip,),
                                         connector=connector)
                record = self.expect_denied(proxy, 'example.com:443')
                self.assertEqual(record.decision, 'denied')
                self.assertIn('non_public_address', record.reason)
                self.assertEqual(record.resolved_ips, (ip,))
                self.assertEqual(connector.calls, [], '私网地址绝不允许发起连接')

    def test_ipv6_loopback_target_rejected(self):
        proxy = self.start_proxy(('::1',), resolver=lambda host: ('::1',))
        record = self.expect_denied(proxy, '[::1]:443')
        self.assertIn('non_public_address', record.reason)
        self.assertEqual(record.host, '::1')

    def test_dns_multiple_answers_with_private_rejected(self):
        connector = RecordingConnector()
        resolver_calls: list[str] = []

        def resolver(host):
            resolver_calls.append(host)
            return (TRUSTED_IP, '10.0.0.5')

        proxy = self.start_proxy(('example.com',), resolver=resolver, connector=connector)
        record = self.expect_denied(proxy, 'example.com:443')
        self.assertIn('non_public_address', record.reason)
        self.assertEqual(record.resolved_ips, (TRUSTED_IP, '10.0.0.5'))
        self.assertEqual(resolver_calls, ['example.com'])
        self.assertEqual(connector.calls, [])

    def test_dns_empty_answer_rejected(self):
        proxy = self.start_proxy(('example.com',), resolver=lambda host: ())
        record = self.expect_denied(proxy, 'example.com:443')
        self.assertEqual(record.reason, 'dns_no_addresses')

    def test_dns_lookup_failure_rejected(self):
        def resolver(host):
            raise OSError('dns down')

        proxy = self.start_proxy(('example.com',), resolver=resolver)
        record = self.expect_denied(proxy, 'example.com:443')
        self.assertIn('dns_lookup_failed', record.reason)

    def test_dns_rebinding_rejected_on_each_connect(self):
        answers = [(TRUSTED_IP,), ('10.0.0.5',)]
        resolver_calls: list[str] = []

        def resolver(host):
            resolver_calls.append(host)
            return answers[min(len(resolver_calls) - 1, len(answers) - 1)]

        remote = FakeRemote(b'ok')
        connector = RecordingConnector(lambda: remote)
        proxy = self.start_proxy(('example.com',), resolver=resolver, connector=connector)
        # 第一次解析返回公网 → 允许并 pin 到该 IP。
        sock = self.expect_allowed(proxy)
        self.assertEqual(_read_until_eof(sock), b'ok')
        sock.close()
        first = self.finalised_allowed(proxy)
        self.assertEqual(first.resolved_ips, (TRUSTED_IP,))
        # 第二次解析被“重绑定”到私网 → 同一 host 也必须拒绝，且不发起连接。
        second = self.expect_denied(proxy, 'example.com:443')
        self.assertIn('non_public_address', second.reason)
        self.assertEqual(second.resolved_ips, ('10.0.0.5',))
        self.assertEqual(len(resolver_calls), 2)
        self.assertEqual([call[0] for call in connector.calls], [(TRUSTED_IP, 443)])

    def test_connector_receives_validated_ip_not_hostname(self):
        connector = RecordingConnector()
        resolver_calls: list[str] = []
        proxy = self.start_proxy(
            ('example.com',),
            resolver=lambda host: (resolver_calls.append(host) or (TRUSTED_IP,)),
            connector=connector)
        sock = self.expect_allowed(proxy)
        sock.close()
        self.finalised_allowed(proxy)
        self.assertEqual(resolver_calls, ['example.com'], '每次 CONNECT 只允许解析一次')
        address, timeout = connector.calls[0]
        self.assertEqual(address, (TRUSTED_IP, 443))
        self.assertNotEqual(address[0], 'example.com')
        self.assertIsInstance(timeout, float)
        # 目标字符串必须是合法 IP，而不是被二次解析的 hostname。
        self.assertEqual(str(ipaddress.ip_address(address[0])), TRUSTED_IP)

    def test_connect_failure_is_denied_and_recorded(self):
        connector = RecordingConnector(error=OSError('connection refused'))
        proxy = self.start_proxy(('example.com',), resolver=lambda host: (TRUSTED_IP,),
                                 connector=connector)
        record = self.expect_denied(proxy, 'example.com:443')
        self.assertIn('connect_failed', record.reason)
        self.assertEqual(record.resolved_ips, (TRUSTED_IP,))
        self.assertEqual(len(connector.calls), 1)


# --------------------------------------------------------------------------- 协议形状
class ConnectProtocolTests(ProxyTestBase):
    def test_non_connect_methods_rejected(self):
        proxy = self.start_proxy(('example.com',), resolver=lambda host: (TRUSTED_IP,))
        for method in ('GET', 'POST', 'HEAD', 'connect'):
            with self.subTest(method=method):
                record = self.expect_denied(proxy, 'example.com:443', method=method)
                self.assertEqual(record.reason, 'method_not_allowed')

    def test_non_443_ports_rejected(self):
        proxy = self.start_proxy(('example.com',), resolver=lambda host: (TRUSTED_IP,))
        for port in (80, 8443, 22):
            with self.subTest(port=port):
                record = self.expect_denied(proxy, f'example.com:{port}')
                self.assertEqual(record.reason, f'connect_port_forbidden:{port}')

    def test_malformed_targets_rejected(self):
        proxy = self.start_proxy(('example.com',), resolver=lambda host: (TRUSTED_IP,))
        for target in ('example.com', 'http://example.com:443', 'example.com:443/path',
                       'user@example.com:443', ':443'):
            with self.subTest(target=target):
                sock = self.open_client(proxy)
                try:
                    sock.sendall(_connect_request(target))
                    head = _read_head(sock)
                finally:
                    sock.close()
                self.assertIn(b'HTTP/1.1 403', head, head[:200])

    def test_oversized_request_line_rejected(self):
        proxy = self.start_proxy(('example.com',), resolver=lambda host: (TRUSTED_IP,))
        sock = self.open_client(proxy)
        try:
            # 无空行的超长请求头：代理必须在读入超限时拒绝，而不是无限缓冲。
            sock.sendall(b'CONNECT ' + b'a' * 9000)
            head = _read_head(sock)
        finally:
            sock.close()
        self.assertIn(b'HTTP/1.1 403', head)
        self.assertTrue(_wait_for(lambda: any(
            r.decision == 'denied' and r.reason == 'request_too_large'
            for r in proxy.records)))


# --------------------------------------------------------------------------- 记录与转发
class RecordAndTunnelTests(ProxyTestBase):
    def test_denied_and_allowed_records_have_expected_fields(self):
        remote = FakeRemote(b'origin-data')
        connector = RecordingConnector(lambda: remote)
        proxy = self.start_proxy(('example.com',), resolver=lambda host: (TRUSTED_IP,),
                                 connector=connector)
        denied = self.expect_denied(proxy, 'evil.test:443')
        self.assertEqual(denied.decision, 'denied')
        self.assertEqual(denied.host, 'evil.test')
        self.assertEqual(denied.resolved_ips, ())
        self.assertEqual(denied.bytes_in, 0)
        self.assertEqual(denied.bytes_out, 0)
        self.assertIsInstance(denied.at, str)
        self.assertIn('T', denied.at)  # ISO-8601 时间
        sock = self.expect_allowed(proxy, early_payload=b'ping')
        self.assertEqual(_read_until_eof(sock), b'origin-data')
        sock.close()
        record = self.finalised_allowed(proxy, bytes_in=len(b'origin-data'),
                                        bytes_out=len(b'ping'))
        self.assertEqual(record.resolved_ips, (TRUSTED_IP,))
        self.assertEqual(record.reason, 'allowed')
        self.assertEqual(remote.sent, b'ping')
        self.assertIsInstance(proxy.records, tuple)
        self.assertTrue(all(isinstance(item, ConnectionRecord) for item in proxy.records))

    def test_early_payload_in_same_packet_is_forwarded_after_200(self):
        remote = FakeRemote()
        connector = RecordingConnector(lambda: remote)
        proxy = self.start_proxy(('example.com',), resolver=lambda host: (TRUSTED_IP,),
                                 connector=connector)
        sock = self.expect_allowed(proxy, early_payload=b'early-bytes')
        sock.close()
        self.assertTrue(_wait_for(lambda: remote.sent == b'early-bytes'), remote.sent)

    def test_sequential_concurrent_tunnels_are_recorded(self):
        connector = RecordingConnector(lambda: FakeRemote(b'ok'))
        proxy = self.start_proxy(('example.com',), resolver=lambda host: (TRUSTED_IP,),
                                 connector=connector)
        outcomes: list[bool] = []
        failures: list[BaseException] = []

        def worker() -> None:
            try:
                sock = _open_socket(proxy)
                sock.sendall(_connect_request('example.com:443'))
                head = _read_head(sock)
                outcomes.append(b'HTTP/1.1 200' in head)
                sock.close()
            except BaseException as exc:  # pragma: no cover - 失败时用于诊断
                failures.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(failures, [])
        self.assertEqual(outcomes, [True, True, True, True])
        self.assertTrue(_wait_for(lambda: sum(
            1 for record in proxy.records if record.decision == 'allowed') == 4),
            proxy.records)

    def test_max_connections_enforced(self):
        gate = threading.Event()
        remote = BlockingRemote(gate)
        connector = RecordingConnector(lambda: remote)
        proxy = self.start_proxy(('example.com',), resolver=lambda host: (TRUSTED_IP,),
                                 connector=connector, max_connections=1)
        first = self.expect_allowed(proxy)
        self.assertTrue(_wait_for(lambda: any(
            record.decision == 'allowed' for record in proxy.records)))
        sock = self.open_client(proxy)
        try:
            sock.sendall(_connect_request('example.com:443'))
            head = _read_head(sock)
        finally:
            sock.close()
        self.assertIn(b'HTTP/1.1 503', head)
        self.assertTrue(_wait_for(lambda: any(
            record.reason == 'max_connections' for record in proxy.records)))
        second = [record for record in proxy.records if record.reason == 'max_connections'][-1]
        self.assertEqual(second.decision, 'denied')
        gate.set()
        first.close()


# --------------------------------------------------------------------------- 生命周期
class LifecycleTests(unittest.TestCase):
    def test_start_is_idempotent_and_url_shape(self):
        proxy = EgressProxy(('example.com',), resolver=lambda host: (TRUSTED_IP,))
        self.addCleanup(proxy.stop)
        self.assertEqual(proxy.url, '')
        url = proxy.start()
        self.assertRegex(url, r'^http://127\.0\.0\.1:\d+$')
        self.assertEqual(proxy.start(), url)
        self.assertEqual(proxy.url, url)
        host, port = _endpoint(proxy)
        self.assertEqual(host, '127.0.0.1')
        self.assertTrue(0 < port < 65536)

    def test_stop_closes_listener_and_active_connection(self):
        proxy = EgressProxy(('example.com',), resolver=lambda host: (TRUSTED_IP,),
                            connector=RecordingConnector())
        url = proxy.start()
        host, port = _endpoint(proxy)
        sock = socket.create_connection((host, port), timeout=5)
        try:
            sock.sendall(_connect_request('example.com:443'))
            head = _read_head(sock)
            self.assertIn(b'HTTP/1.1 200', head)
            proxy.stop()
            proxy.stop()  # 可重复调用
            try:
                tail = sock.recv(1024)
            except OSError:
                tail = b''
            self.assertEqual(tail, b'')
        finally:
            sock.close()
        self.assertEqual(proxy.url, url, 'stop 后保留末次 url 便于取证')
        with self.assertRaises(OSError):
            socket.create_connection((host, port), timeout=1)
        self.assertTrue(any(record.decision == 'allowed' for record in proxy.records))

    def test_constructor_validation(self):
        with self.assertRaises(ValueError):
            EgressProxy('example.com')  # 单个字符串不是序列
        for bad in (('',), ('example.com:443',), ('http://example.com',),
                    ('exa mple.com',), (123,)):
            with self.subTest(allowlist=bad):
                with self.assertRaises(ValueError):
                    EgressProxy(bad)
        with self.assertRaises(ValueError):
            EgressProxy(('example.com',), timeout=0)
        with self.assertRaises(ValueError):
            EgressProxy(('example.com',), timeout=-1)
        with self.assertRaises(ValueError):
            EgressProxy(('example.com',), max_connections=0)
        with self.assertRaises(TypeError):
            EgressProxy(('example.com',), resolver='not-callable')
        with self.assertRaises(TypeError):
            EgressProxy(('example.com',), connector='not-callable')

    def test_no_file_side_effects(self):
        # 代理只做内存审计，不写仓库文件：构造/启动/停止后 __pycache__ 之外的仓库
        # 文件不应被本模块改动。这里用“记录只存在于内存”这一事实做轻量断言。
        proxy = EgressProxy(('example.com',), resolver=lambda host: (TRUSTED_IP,))
        proxy.start()
        try:
            self.assertEqual(proxy.records, ())
        finally:
            proxy.stop()


class DefaultConnectorTests(unittest.TestCase):
    """默认 connector 只接数值 IP：建连成功即证明不依赖任何 DNS 解析。"""

    def test_default_connector_connects_numeric_ip(self):
        from acquisition.egress_proxy import _default_connector

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(3)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        client = _default_connector(('127.0.0.1', port), 3.0)
        try:
            connection, _address = listener.accept()
            connection.close()
        finally:
            client.close()

    def test_default_connector_reports_failure(self):
        from acquisition.egress_proxy import _default_connector

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        listener.close()  # 端口已关闭：连接必须失败而不是挂起/做 DNS
        with self.assertRaises(OSError):
            _default_connector(('127.0.0.1', port), 1.0)


if __name__ == '__main__':
    unittest.main()
