"""V4 切片 3：出网隔离 —— 本地 allowlist 过滤 CONNECT 代理。

设计目标与 ``docs_local/journal/V4-切片3-出网隔离与外部试点-接口冻结-20261113.md``
§2 一致：

- **只接受 ``CONNECT host:443``**；其他方法 / 非 443 端口 / 非法请求行一律 403。
- **允许列表**：精确 hostname 或其子域（``example.com`` 允许 ``example.com`` 与
  ``*.example.com``；大小写不敏感）；空列表拒绝全部。
- **解析一次、校验全部 IP**：每次 CONNECT 只做一次 DNS 解析，所有解析结果都必须是
  公网地址（拒绝 loopback / 私网 / 链路本地 / 保留 / 组播 / 未指定，含 IPv4-mapped
  IPv6）；随后直接连接**已验证的 IP:443**，不再二次 DNS —— 防 DNS rebinding 与
  IP pinning 绕过。
- 转发只是 TCP 字节透传，**不做 TLS 解密 / 中间人**；浏览器证书校验仍然生效。
- 连接超时、并发连接上限、双向字节计数；**所有允许 / 拒绝都追加**
  :class:`ConnectionRecord`，供试点与验收核对实际出网 host/IP/原因。
- 线程安全；``stop()`` 关闭监听与所有活动连接；本模块不写任何文件。

本模块**不 import** ``agent_v3``：:func:`is_public_address` 复现 V3 ``usable_address``
的「仅公网」语义，但**不保留** fake-IP TUN 例外（未配置 loopback 代理时
198.18.0.0/15 属保留段，必须拒绝）。``bytes_out`` 表示浏览器→目标方向（出站）字节，
``bytes_in`` 表示目标→浏览器方向（入站）字节。

典型用法::

    proxy = EgressProxy(('www.example.com',))
    url = proxy.start()          # http://127.0.0.1:<port>
    ...                          # Chromium --proxy-server=<url>
    proxy.stop()
    for record in proxy.records:
        ...
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    'CONNECT_PORT',
    'ConnectionRecord',
    'EgressDenied',
    'EgressProxy',
    'is_public_address',
]

CONNECT_PORT = 443
_TZ = timezone(timedelta(hours=8))
_MAX_REQUEST_BYTES = 8192
_MAX_HOST_LENGTH = 253
_MAX_REASON_CHARS = 200
_TUNNEL_CHUNK = 65536
_IO_POLL_TIMEOUT = 1.0
_ACCEPT_POLL_TIMEOUT = 0.25
_ACCEPT_BACKLOG = 128
_ESTABLISHED = b'HTTP/1.1 200 Connection Established\r\n\r\n'
_STATUS_TEXT = {403: 'Forbidden', 408: 'Request Timeout', 503: 'Service Unavailable'}
_HOSTNAME_RE = re.compile(r'^[a-z0-9_](?:[a-z0-9._-]*[a-z0-9])?$')

Resolver = Callable[[str], Iterable[str]]
Connector = Callable[[tuple[str, int], float], socket.socket]


class EgressDenied(RuntimeError):
    """一次出网请求被拒绝（或无法安全转发）；``str(exc)`` 即审计原因。"""


@dataclass(frozen=True)
class ConnectionRecord:
    """一次 CONNECT 决定的审计记录。

    ``decision`` 为 ``'allowed'`` / ``'denied'``；``resolved_ips`` 是本次请求解析到的
    **全部** IP（即使因其中某个私网地址而被拒绝也会记录，便于取证）；``bytes_out``
    是浏览器→目标方向字节数，``bytes_in`` 是目标→浏览器方向字节数；``at`` 为
    ISO-8601（UTC+8）时间。
    """

    host: str
    port: int
    resolved_ips: tuple[str, ...]
    decision: str
    reason: str
    bytes_in: int
    bytes_out: int
    at: str

    def __post_init__(self) -> None:
        if not isinstance(self.host, str):
            raise TypeError('host 必须是字符串')
        if not isinstance(self.port, int) or isinstance(self.port, bool) or not 0 <= self.port <= 65535:
            raise ValueError('port 必须在 0..65535')
        object.__setattr__(self, 'resolved_ips', tuple(str(item) for item in self.resolved_ips))
        if self.decision not in ('allowed', 'denied'):
            raise ValueError('decision 必须是 allowed/denied')
        if not isinstance(self.reason, str):
            raise TypeError('reason 必须是字符串')
        for name in ('bytes_in', 'bytes_out'):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f'{name} 必须为非负整数')
        if not isinstance(self.at, str):
            raise TypeError('at 必须是字符串')


def _now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec='seconds')


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    text = str(value or '').strip()
    if not text:
        return None
    if text.startswith('[') and text.endswith(']'):
        text = text[1:-1]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def is_public_address(value: str) -> bool:
    """是否是可直连的公网单播地址（V3 ``usable_address`` 的严格子集）。

    拒绝：loopback、私网（含 CGNAT）、链路本地、保留、组播、未指定、IPv4-mapped
    IPv6（先折算回 IPv4 再判定）以及所有 ``is_global=False`` 的特殊用途段。
    本代理**不**接受 fake-IP TUN 段（198.18.0.0/15），因为它不是可直连的公网地址。
    """
    ip = _parse_ip(value)
    if ip is None:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        return False
    return bool(ip.is_global)


def _canonical_host(raw: str) -> str:
    """规范化 CONNECT 目标主机：去方括号/末尾点、小写、IP 用标准写法。"""
    host = str(raw or '').strip().lower()
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    if host.endswith('.'):
        host = host[:-1]
    if not host or len(host) > _MAX_HOST_LENGTH:
        raise EgressDenied('invalid_connect_target')
    ip = _parse_ip(host)
    if ip is not None:
        return str(ip)
    if not _HOSTNAME_RE.match(host) or '..' in host:
        raise EgressDenied('invalid_connect_target')
    return host


def _normalise_allowlist(allowlist: Sequence[str]) -> tuple[str, ...]:
    if isinstance(allowlist, (str, bytes)):
        raise ValueError('allowlist 必须是主机名序列，不能是单个字符串')
    entries: list[str] = []
    for raw in allowlist:
        if not isinstance(raw, str):
            raise ValueError(f'allowlist 条目必须是字符串: {raw!r}')
        text = raw.strip().lower()
        if text.startswith('[') and text.endswith(']'):
            text = text[1:-1]
        if text.startswith('.'):
            text = text[1:]
        if text.endswith('.'):
            text = text[:-1]
        if not text:
            raise ValueError('allowlist 不能包含空条目')
        if '/' in text or '@' in text or any(ch.isspace() for ch in text):
            raise ValueError(f'allowlist 条目非法: {raw!r}')
        ip = _parse_ip(text)
        if ip is not None:
            entries.append(str(ip))
            continue
        if ':' in text or not _HOSTNAME_RE.match(text) or '..' in text:
            raise ValueError(f'allowlist 条目非法: {raw!r}')
        entries.append(text)
    # 去重且保留配置顺序（records 原因可复现）。
    return tuple(dict.fromkeys(entries))


def _default_resolver(host: str) -> tuple[str, ...]:
    infos = socket.getaddrinfo(host, CONNECT_PORT, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if isinstance(address, str) and address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def _default_connector(address: tuple[str, int], timeout: float) -> socket.socket:
    """直接对**已校验 IP** 建 TCP 连接（不走 getaddrinfo，杜绝任何二次 DNS）。"""
    host, port = address
    family = socket.AF_INET6 if ':' in str(host) else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except OSError:
        sock.close()
        raise
    return sock


def _close_quietly(sock, *, shutdown: bool = False) -> None:
    if sock is None:
        return
    if shutdown:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        except Exception:  # pragma: no cover - 注入的假连接只需要 close()
            pass
    try:
        sock.close()
    except OSError:
        pass
    except Exception:  # pragma: no cover
        pass


def _split_host_port(target: str) -> tuple[str, int]:
    if not target:
        raise EgressDenied('invalid_connect_target')
    if target.startswith('['):
        end = target.find(']')
        if end <= 1:
            raise EgressDenied('invalid_connect_target')
        host = target[1:end]
        rest = target[end + 1:]
        if not rest.startswith(':'):
            raise EgressDenied('invalid_connect_target')
        port_text = rest[1:]
    else:
        if target.count(':') != 1:
            raise EgressDenied('invalid_connect_target')
        host, port_text = target.rsplit(':', 1)
    if not port_text.isdigit():
        raise EgressDenied('invalid_connect_target')
    return host, int(port_text)


def _header_end(buffer: bytes) -> int | None:
    """返回 HTTP 请求头结束位置（空行之后的下标）；未读到空行时返回 None。"""
    index = buffer.find(b'\r\n\r\n')
    if index >= 0:
        return index + 4
    index = buffer.find(b'\n\n')
    if index >= 0:
        return index + 2
    return None


def _parse_connect_line(line: bytes) -> tuple[str, int]:
    try:
        text = line.decode('ascii')
    except UnicodeDecodeError as exc:
        raise EgressDenied('invalid_request_line') from exc
    parts = text.split()
    if len(parts) != 3:
        raise EgressDenied('invalid_request_line')
    method, target, version = parts
    if not version.startswith('HTTP/'):
        raise EgressDenied('invalid_request_line')
    if method != 'CONNECT':
        raise EgressDenied('method_not_allowed')
    host_raw, port = _split_host_port(target)
    if port != CONNECT_PORT:
        raise EgressDenied(f'connect_port_forbidden:{port}')
    return _canonical_host(host_raw), port


class EgressProxy:
    """本地 HTTP CONNECT 过滤代理（Chromium ``--proxy-server`` 的唯一出口）。

    参数
    ----
    allowlist:
        允许的精确主机名 / 子域后缀；空序列表示**拒绝全部**。
    resolver:
        可注入的 DNS 解析器 ``resolver(host) -> tuple[str, ...]``；默认
        ``socket.getaddrinfo(host, 443)``。测试用假 DNS 注入。
    connector:
        可注入的 TCP 连接器 ``connector((ip, 443), timeout) -> socket``；默认
        ``socket.create_connection``。代理只会把**已校验的 IP** 交给它。
    timeout:
        读取 CONNECT 请求 / 建立上游连接的秒数。
    max_connections:
        同时允许的上游隧道数上限，超出后返回 503 并记录 ``max_connections``。
    """

    def __init__(self, allowlist: Sequence[str], *, resolver: Resolver | None = None,
                 connector: Connector | None = None, timeout: float = 10.0,
                 max_connections: int = 64) -> None:
        if resolver is not None and not callable(resolver):
            raise TypeError('resolver 必须可调用')
        if connector is not None and not callable(connector):
            raise TypeError('connector 必须可调用')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or float(timeout) <= 0:
            raise ValueError('timeout 必须为正数')
        if isinstance(max_connections, bool) or not isinstance(max_connections, int) or max_connections <= 0:
            raise ValueError('max_connections 必须为正整数')
        self._allowlist = _normalise_allowlist(allowlist)
        self._resolver = resolver
        self._connector = connector
        self._timeout = float(timeout)
        self._max_connections = int(max_connections)
        self._lock = threading.Lock()
        self._records: list[ConnectionRecord] = []
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._active_sockets: set[Any] = set()
        self._active_tunnels = 0
        self._url = ''

    # ---------------------------------------------------------------- 生命周期
    @property
    def url(self) -> str:
        """代理地址 ``http://127.0.0.1:<port>``；未启动时为空串（停止后保留末次地址）。"""
        return self._url

    @property
    def records(self) -> tuple[ConnectionRecord, ...]:
        """按时间顺序的全部允许/拒绝记录（返回不可变副本）。"""
        with self._lock:
            return tuple(self._records)

    def start(self) -> str:
        """绑定 127.0.0.1 的随机端口并开始监听；重复调用返回同一 url。"""
        with self._lock:
            if self._listener is not None:
                return self._url
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(('127.0.0.1', 0))
                listener.listen(_ACCEPT_BACKLOG)
                listener.settimeout(_ACCEPT_POLL_TIMEOUT)
            except OSError:
                listener.close()
                raise
            self._listener = listener
            self._url = f'http://127.0.0.1:{listener.getsockname()[1]}'
            self._stop_event = threading.Event()
            thread = threading.Thread(target=self._accept_loop, name='acq-egress-accept',
                                      daemon=True)
            self._accept_thread = thread
            thread.start()
            return self._url

    def stop(self) -> None:
        """关闭监听与所有活动连接；可重复调用。"""
        with self._lock:
            listener, self._listener = self._listener, None
            accept_thread, self._accept_thread = self._accept_thread, None
            active = list(self._active_sockets)
            self._stop_event.set()
        _close_quietly(listener)
        for sock in active:
            _close_quietly(sock, shutdown=True)
        if accept_thread is not None and accept_thread is not threading.current_thread():
            accept_thread.join(timeout=3.0)

    # ---------------------------------------------------------------- 请求判定
    def _allows(self, host: str) -> bool:
        if not self._allowlist:
            return False
        for entry in self._allowlist:
            if host == entry or host.endswith('.' + entry):
                return True
        return False

    def _read_connect_request(self, client: socket.socket) -> tuple[bytes, str, int]:
        """读取完整 CONNECT 请求头，返回 (头部之后的已多读字节, host, port)。

        必须消费到空行（``\\r\\n\\r\\n``）为止：CONNECT 请求头本身**不能**被当作隧道
        数据转发（否则会污染后续 TLS 握手），只有头部之后的字节才属于隧道。
        """
        buffer = b''
        while True:
            end = _header_end(buffer)
            if end is not None:
                head, pending = buffer[:end], buffer[end:]
                line_end = head.find(b'\r\n')
                if line_end < 0:
                    line_end = head.find(b'\n')
                line = head if line_end < 0 else head[:line_end]
                host, port = _parse_connect_line(line)
                return pending, host, port
            if len(buffer) > _MAX_REQUEST_BYTES:
                raise EgressDenied('request_too_large')
            try:
                chunk = client.recv(4096)
            except socket.timeout as exc:
                raise EgressDenied('request_timeout') from exc
            except OSError as exc:
                raise EgressDenied('client_closed') from exc
            if not chunk:
                raise EgressDenied('client_closed')
            buffer += chunk

    def _resolve(self, host: str) -> tuple[str, ...]:
        resolver = self._resolver or _default_resolver
        try:
            raw = resolver(host)
        except EgressDenied:
            raise
        except Exception as exc:
            raise EgressDenied(f'dns_lookup_failed:{type(exc).__name__}') from exc
        if isinstance(raw, (str, bytes)):
            raise EgressDenied('dns_invalid_result')
        try:
            items = list(raw)
        except TypeError as exc:
            raise EgressDenied('dns_invalid_result') from exc
        addresses: list[str] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise EgressDenied('dns_invalid_result')
            text = item.strip()
            try:
                canonical = str(ipaddress.ip_address(text))
            except ValueError as exc:
                raise EgressDenied('dns_invalid_result') from exc
            if canonical in seen:
                continue
            seen.add(canonical)
            addresses.append(text)
        return tuple(addresses)

    def _connect(self, resolved: tuple[str, ...]) -> socket.socket:
        connector = self._connector or _default_connector
        failures: list[str] = []
        for ip in resolved:
            try:
                remote = connector((ip, CONNECT_PORT), self._timeout)
            except Exception as exc:  # 连接失败按下一个已验证 IP 重试，仍不重新 DNS
                failures.append(f'{ip}:{type(exc).__name__}')
                continue
            if remote is None:
                failures.append(f'{ip}:no_socket')
                continue
            try:
                remote.settimeout(_IO_POLL_TIMEOUT)
            except (AttributeError, OSError):
                pass
            return remote
        raise EgressDenied('connect_failed:' + (','.join(failures) or 'no_address'))

    # ---------------------------------------------------------------- 记录
    def _add_record(self, host: str, port: int, resolved: Iterable[str], decision: str,
                    reason: str) -> int:
        safe_host = ''.join(ch for ch in str(host or '') if ch.isprintable())[:_MAX_HOST_LENGTH]
        safe_port = int(port) if isinstance(port, int) and 0 <= port <= 65535 else 0
        safe_reason = ''.join(ch for ch in str(reason) if ch.isprintable())[:_MAX_REASON_CHARS]
        record = ConnectionRecord(host=safe_host, port=safe_port,
                                  resolved_ips=tuple(resolved), decision=decision,
                                  reason=safe_reason, bytes_in=0, bytes_out=0, at=_now_iso())
        with self._lock:
            index = len(self._records)
            self._records.append(record)
        return index

    def _finalize(self, index: int, bytes_in: int, bytes_out: int) -> None:
        with self._lock:
            if 0 <= index < len(self._records):
                self._records[index] = replace(self._records[index],
                                               bytes_in=max(0, int(bytes_in)),
                                               bytes_out=max(0, int(bytes_out)))

    # ---------------------------------------------------------------- 槽位/套接字
    def _reserve_slot(self) -> bool:
        with self._lock:
            if self._active_tunnels >= self._max_connections:
                return False
            self._active_tunnels += 1
            return True

    def _release_slot(self) -> None:
        with self._lock:
            self._active_tunnels = max(0, self._active_tunnels - 1)

    # ---------------------------------------------------------------- 拒绝/转发
    def _deny(self, client: socket.socket, host: str, port: int, reason: str,
              resolved: Iterable[str] = (), *, status: int = 403) -> None:
        self._add_record(host, port, resolved, 'denied', reason)
        body = str(reason).encode('utf-8', 'replace')
        head = (
            f'HTTP/1.1 {status} {_STATUS_TEXT.get(status, "Forbidden")}\r\n'
            'Content-Type: text/plain; charset=utf-8\r\n'
            f'Content-Length: {len(body)}\r\n'
            'Connection: close\r\n\r\n'
        ).encode('ascii')
        try:
            client.sendall(head + body)
        except OSError:
            pass
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def _safe_deny(self, client: socket.socket, host: str, port: int, reason: str,
                   resolved: Iterable[str] = ()) -> None:
        try:
            self._deny(client, host, port, reason, resolved)
        except Exception:  # pragma: no cover - 审计失败不能打死代理
            pass

    def _tunnel(self, client: socket.socket, remote: socket.socket,
                pending: bytes) -> tuple[int, int]:
        """双向转发直到两端关闭；返回 (bytes_in, bytes_out)。"""
        counts = {'in': 0, 'out': len(pending)}

        def client_to_remote() -> None:
            try:
                if pending:
                    remote.sendall(pending)
                while not self._stop_event.is_set():
                    try:
                        chunk = client.recv(_TUNNEL_CHUNK)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    remote.sendall(chunk)
                    counts['out'] += len(chunk)
            except OSError:
                pass
            finally:
                try:
                    remote.shutdown(socket.SHUT_WR)
                except (OSError, AttributeError):
                    pass
                except Exception:  # pragma: no cover
                    pass

        def remote_to_client() -> None:
            try:
                while not self._stop_event.is_set():
                    try:
                        chunk = remote.recv(_TUNNEL_CHUNK)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    client.sendall(chunk)
                    counts['in'] += len(chunk)
            except OSError:
                pass
            finally:
                try:
                    client.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        out_thread = threading.Thread(target=client_to_remote, name='acq-egress-up', daemon=True)
        in_thread = threading.Thread(target=remote_to_client, name='acq-egress-down', daemon=True)
        out_thread.start()
        in_thread.start()
        deadline: float | None = None
        while True:
            out_thread.join(timeout=0.5)
            in_thread.join(timeout=0.5)
            if not out_thread.is_alive() and not in_thread.is_alive():
                break
            if self._stop_event.is_set():
                if deadline is None:
                    deadline = time.monotonic() + 5.0
                elif time.monotonic() >= deadline:  # pragma: no cover - 防御
                    break
        return counts['in'], counts['out']

    # ---------------------------------------------------------------- 主循环
    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            listener = self._listener
            if listener is None:
                break
            try:
                client, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self._stop_event.is_set():
                _close_quietly(client, shutdown=True)
                break
            try:
                client.settimeout(self._timeout)
            except OSError:
                _close_quietly(client)
                continue
            thread = threading.Thread(target=self._handle_client, args=(client,),
                                      name='acq-egress-conn', daemon=True)
            thread.start()

    def _handle_client(self, client: socket.socket) -> None:
        host = ''
        port = 0
        resolved: tuple[str, ...] = ()
        record_index: int | None = None
        remote: socket.socket | None = None
        slot = False
        bytes_in = 0
        bytes_out = 0
        with self._lock:
            self._active_sockets.add(client)
        try:
            try:
                pending, host, port = self._read_connect_request(client)
            except EgressDenied as exc:
                self._deny(client, host, port, str(exc))
                return
            if not self._allows(host):
                self._deny(client, host, port, 'host_not_allowed')
                return
            if not self._reserve_slot():
                self._deny(client, host, port, 'max_connections', status=503)
                return
            slot = True
            try:
                resolved = self._resolve(host)
            except EgressDenied as exc:
                self._deny(client, host, port, str(exc))
                return
            if not resolved:
                self._deny(client, host, port, 'dns_no_addresses')
                return
            blocked = next((ip for ip in resolved if not is_public_address(ip)), None)
            if blocked is not None:
                self._deny(client, host, port, f'non_public_address:{blocked}', resolved)
                return
            try:
                remote = self._connect(resolved)
            except EgressDenied as exc:
                self._deny(client, host, port, str(exc), resolved)
                return
            with self._lock:
                self._active_sockets.add(remote)
            record_index = self._add_record(host, port, resolved, 'allowed', 'allowed')
            try:
                client.sendall(_ESTABLISHED)
            except OSError:
                return
            bytes_in, bytes_out = self._tunnel(client, remote, pending)
        except EgressDenied as exc:  # pragma: no cover - 兜底
            self._safe_deny(client, host, port, str(exc), resolved)
        except OSError:
            self._safe_deny(client, host, port, 'io_error', resolved)
        except Exception as exc:  # pragma: no cover - 防御：连接线程异常不外溢
            self._safe_deny(client, host, port, f'proxy_error:{type(exc).__name__}', resolved)
        finally:
            if record_index is not None:
                self._finalize(record_index, bytes_in, bytes_out)
            if slot:
                self._release_slot()
            with self._lock:
                self._active_sockets.discard(client)
                if remote is not None:
                    self._active_sockets.discard(remote)
            _close_quietly(remote, shutdown=True)
            _close_quietly(client)
