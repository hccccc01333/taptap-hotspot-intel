"""SQLite 持久化：session / task / receipt / evidence / verification / lease。

设计取向（与 ``docs_local/journal/V4-切片-接口冻结-20261113.md`` 一致）：

- 收据按 ``idempotency_key`` 幂等，同一任务同一键永远不会出现两条；
- 证据按 fingerprint 幂等，同一截图文本不会重复入库；
- **``verify()`` 是唯一能把 ``evidence.verified`` 置真的入口**：提取收据永远
  以未核验状态落库，进库后只能由独立核验记录改变核验状态；
- 证据绑定 ``page_version``；旧版本证据保留，``resume()`` 给出
  ``latest_page_version`` 供 harness 判断页面是否已变化；
- 租约提供所有权栅栏：``assert_owner`` 在租约被接管或过期时抛 :class:`LeaseLost`。

本模块不访问网络、不调用模型，只依赖标准库 ``sqlite3`` 与冻结契约 ``model``。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from . import model
from .model import (
    BACKENDS,
    TASK_STATUSES,
    Cost,
    Evidence,
    Receipt,
    ResearchGoal,
    TaskTrace,
    Verification,
    ZERO_COST,
    now_iso,
)
from .receipts import validate_receipt

_SCHEMA = """
CREATE TABLE IF NOT EXISTS acq_session (
    session_id TEXT PRIMARY KEY,
    backend TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_epoch REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS acq_task (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    goal_task_id TEXT NOT NULL,
    goal_json TEXT NOT NULL,
    status TEXT NOT NULL,
    stop_reason TEXT,
    error TEXT,
    gap TEXT,
    latest_page_version TEXT NOT NULL DEFAULT '',
    steps INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    created_epoch REAL NOT NULL,
    updated_at TEXT NOT NULL,
    updated_epoch REAL NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_acq_task_goal ON acq_task(goal_task_id, session_id);
CREATE INDEX IF NOT EXISTS idx_acq_task_status ON acq_task(status, updated_epoch);

CREATE TABLE IF NOT EXISTS acq_receipt (
    task_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    goal TEXT NOT NULL,
    backend TEXT NOT NULL,
    action TEXT NOT NULL,
    region TEXT,
    method TEXT,
    status TEXT NOT NULL,
    page_version TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL,
    cost_units INTEGER NOT NULL DEFAULT 0,
    cost_captures INTEGER NOT NULL DEFAULT 0,
    cost_ocr_calls INTEGER NOT NULL DEFAULT 0,
    cost_vlm_calls INTEGER NOT NULL DEFAULT 0,
    next_step TEXT NOT NULL,
    error TEXT,
    note TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    PRIMARY KEY (task_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS acq_evidence (
    fingerprint TEXT PRIMARY KEY,
    capture_id TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    page_version TEXT NOT NULL,
    region TEXT NOT NULL,
    extraction TEXT NOT NULL,
    text TEXT NOT NULL,
    bbox_json TEXT,
    source_url TEXT NOT NULL DEFAULT '',
    verified INTEGER NOT NULL DEFAULT 0,
    verifier TEXT,
    skill_id TEXT,
    note TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_acq_evidence_capture ON acq_evidence(capture_id);
CREATE INDEX IF NOT EXISTS idx_acq_evidence_task ON acq_evidence(task_id, page_version);

-- 同一 fingerprint 的证据可能同时属于多个任务（例如同一冻结样本被不同策略重跑）：
-- 证据内容按 fingerprint 只存一份，任务归属用链接表表达。
CREATE TABLE IF NOT EXISTS acq_evidence_link (
    task_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY (task_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_acq_evidence_link_task ON acq_evidence_link(task_id);

CREATE TABLE IF NOT EXISTS acq_verification (
    verification_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL DEFAULT '',
    capture_id TEXT NOT NULL,
    status TEXT NOT NULL,
    verifier TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    claim TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_acq_verification_task ON acq_verification(task_id);
CREATE INDEX IF NOT EXISTS idx_acq_verification_capture ON acq_verification(capture_id);

CREATE TABLE IF NOT EXISTS acq_lease (
    session_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    token TEXT NOT NULL,
    expires_at REAL NOT NULL,
    ttl_seconds REAL NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class LeaseLost(RuntimeError):
    """租约已被其他执行者接管或已经过期；当前执行者必须停止写入。

    harness 捕获该异常后把任务标记为 ``interrupted``，等待恢复执行；
    它同时是「旧执行者不得覆盖新执行者结果」的栅栏。
    """


def _epoch(now: Any) -> float:
    """把测试传入的 now（None / 数值 / datetime / ISO 字符串）统一成 epoch 秒。"""
    if now is None:
        return time.time()
    if isinstance(now, bool):
        raise TypeError('now 不能是布尔值')
    if isinstance(now, (int, float)):
        return float(now)
    if isinstance(now, datetime):
        return now.timestamp()
    if isinstance(now, str):
        return datetime.fromisoformat(now).timestamp()
    raise TypeError('now 必须是 None、数值、datetime 或 ISO 字符串')


def _goal_to_dict(goal: ResearchGoal) -> dict[str, Any]:
    return {
        'task_id': goal.task_id,
        'question': goal.question,
        'objectives': [
            {'objective_id': objective.objective_id, 'question': objective.question,
             'answer_kind': objective.answer_kind,
             'region_hints': list(objective.region_hints),
             'required': objective.required}
            for objective in goal.objectives
        ],
        'allowed_hosts': list(goal.allowed_hosts),
        'policy_note': goal.policy_note,
    }


def _evidence_to_dict(evidence: Evidence) -> dict[str, Any]:
    return evidence.as_dict()


def _evidence_from_dict(item: Mapping[str, Any]) -> Evidence:
    data = dict(item)
    bbox = data.get('bbox')
    if isinstance(bbox, (list, tuple)):
        data['bbox'] = tuple(bbox)
    return Evidence(**data)


def _receipt_from_row(row: sqlite3.Row) -> Receipt:
    evidence = tuple(_evidence_from_dict(item) for item in json.loads(row['evidence_json']))
    cost = Cost(units=int(row['cost_units']), captures=int(row['cost_captures']),
                ocr_calls=int(row['cost_ocr_calls']), vlm_calls=int(row['cost_vlm_calls']))
    return Receipt(task_id=row['task_id'], goal=row['goal'], backend=row['backend'],
                   action=row['action'], status=row['status'],
                   page_version=row['page_version'], region=row['region'],
                   method=row['method'], evidence=evidence, cost=cost,
                   next=row['next_step'], error=row['error'], note=row['note'],
                   at=row['at'], receipt_id=row['receipt_id'],
                   idempotency_key=row['idempotency_key'])


def _receipt_signature(receipt: Receipt) -> tuple:
    """收据内容签名（忽略 at/receipt_id 等派生字段），用于检测幂等键冲突。"""
    evidence = tuple((item.capture_id, item.page_version, item.region, item.extraction,
                      item.text, item.bbox, item.verified, item.verifier, item.skill_id,
                      item.note) for item in receipt.evidence)
    return (receipt.task_id, receipt.goal, receipt.backend, receipt.action,
            receipt.status, receipt.page_version, receipt.region, receipt.method,
            receipt.cost, receipt.next, receipt.error, receipt.note, evidence)


class AcquisitionStore:
    """一次评测/运行使用的 SQLite 存储；构造参数为数据库路径（测试用临时文件）。"""

    def __init__(self, path) -> None:
        self.path = str(path)
        if self.path != ':memory:':
            parent = Path(self.path).expanduser().parent
            if not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ 基础
    def _conn_or_raise(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError('存储已关闭')
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> 'AcquisitionStore':
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        self.close()

    @contextmanager
    def _immediate(self):
        """``BEGIN IMMEDIATE`` 写事务：先校验租约再写入，避免检查与写入之间的竞态。

        已经处于事务中时（例如嵌套调用）不再重复 BEGIN，交由外层事务提交/回滚。
        """
        conn = self._conn_or_raise()
        if conn.in_transaction:
            yield conn
            return
        conn.execute('BEGIN IMMEDIATE')
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        conn.commit()

    def _fence(self, session_id: str | None, owner: str | None,
               token: str | None) -> None:
        """写入前租约栅栏；三个参数全部省略时保持旧行为（无 fence）。

        提供 fence 参数时要求同时给出 ``session_id`` 与 ``owner``；
        ``token`` 可选，给出时必须是 ``acquire_lease`` 返回的当前 token。
        """
        if session_id is None and owner is None and token is None:
            return
        if session_id is None or owner is None:
            raise ValueError('租约栅栏需要同时提供 session_id 与 owner')
        self._check_lease(session_id, owner, token)

    # ---------------------------------------------------------------- session
    def create_session(self, backend: str) -> str:
        if backend not in BACKENDS:
            raise ValueError(f'未知后端: {backend!r}')
        session_id = 'session_' + uuid.uuid4().hex[:24]
        epoch = time.time()
        with self._conn_or_raise() as conn:
            conn.execute('INSERT INTO acq_session(session_id, backend, created_at, created_epoch) '
                         'VALUES(?,?,?,?)', (session_id, backend, now_iso(), epoch))
        return session_id

    # ------------------------------------------------------------------ task
    def create_task(self, session_id: str, goal: ResearchGoal) -> str:
        if not isinstance(goal, ResearchGoal):
            raise TypeError('goal 必须是 ResearchGoal')
        conn = self._conn_or_raise()
        if conn.execute('SELECT 1 FROM acq_session WHERE session_id=?', (session_id,)).fetchone() is None:
            raise KeyError(f'未知 session: {session_id}')
        task_id = 'task_' + uuid.uuid4().hex[:24]
        stamp = now_iso()
        epoch = time.time()
        with conn:
            conn.execute(
                'INSERT INTO acq_task(task_id, session_id, goal_task_id, goal_json, status, '
                'latest_page_version, steps, created_at, created_epoch, updated_at, updated_epoch) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (task_id, session_id, goal.task_id, model.canonical(_goal_to_dict(goal)),
                 'pending', '', 0, stamp, epoch, stamp, epoch))
        return task_id

    def task(self, task_id: str) -> dict | None:
        conn = self._conn_or_raise()
        row = conn.execute('SELECT * FROM acq_task WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result['goal'] = json.loads(row['goal_json'])
        except (TypeError, ValueError):
            result['goal'] = None
        return result

    def find_task(self, goal_task_id: str, session_id: str | None = None) -> dict | None:
        """恢复辅助：按业务 goal.task_id 查最近创建的任务（可限定 session）。"""
        conn = self._conn_or_raise()
        if session_id is None:
            row = conn.execute(
                'SELECT task_id FROM acq_task WHERE goal_task_id=? '
                'ORDER BY created_epoch DESC, rowid DESC LIMIT 1', (goal_task_id,)).fetchone()
        else:
            row = conn.execute(
                'SELECT task_id FROM acq_task WHERE goal_task_id=? AND session_id=? '
                'ORDER BY created_epoch DESC, rowid DESC LIMIT 1', (goal_task_id, session_id)).fetchone()
        return None if row is None else self.task(row['task_id'])

    # --------------------------------------------------------------- receipt
    def append_receipt(self, receipt: Receipt) -> Receipt:
        """幂等追加一条收据；重复调用返回已存收据，不会产生第二条。"""
        if not isinstance(receipt, Receipt):
            raise TypeError('receipt 必须是 Receipt')
        receipt = receipt.filled()
        validate_receipt(receipt)
        for index, item in enumerate(receipt.evidence):
            if item.verified:
                raise ValueError(f'evidence[{index}] 已核验：事实核验只能通过 store.verify 追加')
        conn = self._conn_or_raise()
        if self.task(receipt.task_id) is None:
            raise KeyError(f'未知任务: {receipt.task_id}')
        stamp = now_iso()
        with self._immediate():
            cursor = conn.execute(
                'INSERT OR IGNORE INTO acq_receipt(task_id, idempotency_key, receipt_id, goal, backend, '
                'action, region, method, status, page_version, evidence_json, cost_units, cost_captures, '
                'cost_ocr_calls, cost_vlm_calls, next_step, error, note, at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (receipt.task_id, receipt.idempotency_key, receipt.receipt_id, receipt.goal,
                 receipt.backend, receipt.action, receipt.region, receipt.method,
                 receipt.status, receipt.page_version,
                 json.dumps([_evidence_to_dict(item) for item in receipt.evidence], ensure_ascii=False),
                 receipt.cost.units, receipt.cost.captures, receipt.cost.ocr_calls,
                 receipt.cost.vlm_calls, receipt.next, receipt.error, receipt.note, receipt.at))
            if cursor.rowcount:
                conn.execute(
                    'UPDATE acq_task SET steps = steps + 1, updated_at=?, updated_epoch=?, '
                    'latest_page_version = CASE WHEN ? <> \'\' THEN ? ELSE latest_page_version END '
                    'WHERE task_id=?',
                    (stamp, time.time(), receipt.page_version, receipt.page_version, receipt.task_id))
            else:
                # 幂等键已存在：同内容视为幂等重放；内容不同说明调用方复用了错误的键，
                # 必须报错而不是静默丢弃（否则新证据/成本会与收据脱节）。
                existing = self.receipt(receipt.task_id, receipt.idempotency_key)
                if existing is not None and _receipt_signature(existing) != _receipt_signature(receipt):
                    raise ValueError(
                        f'幂等键冲突：{receipt.idempotency_key} 已存在内容不同的收据；'
                        '重试动作应使用 receipts.retry_key 生成含尝试序号的幂等键')
        stored = self.receipt(receipt.task_id, receipt.idempotency_key)
        assert stored is not None
        return stored

    def receipt(self, task_id: str, idempotency_key: str) -> Receipt | None:
        conn = self._conn_or_raise()
        row = conn.execute('SELECT * FROM acq_receipt WHERE task_id=? AND idempotency_key=?',
                           (task_id, idempotency_key)).fetchone()
        return None if row is None else _receipt_from_row(row)

    def receipts(self, task_id: str) -> list[Receipt]:
        conn = self._conn_or_raise()
        rows = conn.execute('SELECT * FROM acq_receipt WHERE task_id=? ORDER BY rowid',
                            (task_id,)).fetchall()
        return [_receipt_from_row(row) for row in rows]

    # -------------------------------------------------------------- evidence
    def save_evidence(self, evidence: Evidence, *, task_id: str | None = None,
                      session_id: str | None = None, owner: str | None = None,
                      token: str | None = None) -> None:
        """按 fingerprint 幂等保存证据，并用链接表记录任务归属。

        ``task_id`` 为可选扩展：harness 显式传入以保证证据可归属任务；
        未传入时证据仍会按 fingerprint 落库，只是不归属某个任务的查询结果。

        ``session_id`` / ``owner`` / ``token`` 为可选租约栅栏：给出时在同一
        ``BEGIN IMMEDIATE`` 事务内先校验租约再写入，旧执行者无法把证据塞进
        已被新 owner 接管的 session；省略时保持旧行为（向后兼容）。
        """
        if not isinstance(evidence, Evidence):
            raise TypeError('evidence 必须是 Evidence')
        if evidence.verified:
            raise ValueError('save_evidence 不接受已核验证据：事实核验只能通过 verify 追加')
        conn = self._conn_or_raise()
        task_owner = task_id or ''
        if task_owner and self.task(task_owner) is None:
            raise KeyError(f'未知任务: {task_owner}')
        bbox = None
        if evidence.bbox is not None:
            bbox = json.dumps(list(evidence.bbox))
        with self._immediate():
            self._fence(session_id, owner, token)
            conn.execute(
                'INSERT OR IGNORE INTO acq_evidence(fingerprint, capture_id, task_id, page_version, '
                'region, extraction, text, bbox_json, source_url, verified, verifier, skill_id, note, at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (evidence.fingerprint, evidence.capture_id, task_owner, evidence.page_version,
                 evidence.region, evidence.extraction, evidence.text, bbox, evidence.source_url,
                 1 if evidence.verified else 0, evidence.verifier, evidence.skill_id,
                 evidence.note, evidence.at or now_iso()))
            if task_owner:
                conn.execute('INSERT OR IGNORE INTO acq_evidence_link(task_id, fingerprint) VALUES(?,?)',
                             (task_owner, evidence.fingerprint))

    def evidence(self, task_id: str, *, page_version: str | None = None) -> list[Evidence]:
        conn = self._conn_or_raise()
        if page_version is None:
            rows = conn.execute(
                'SELECT e.* FROM acq_evidence e JOIN acq_evidence_link l ON l.fingerprint=e.fingerprint '
                'WHERE l.task_id=? ORDER BY l.rowid', (task_id,)).fetchall()
        else:
            rows = conn.execute(
                'SELECT e.* FROM acq_evidence e JOIN acq_evidence_link l ON l.fingerprint=e.fingerprint '
                'WHERE l.task_id=? AND e.page_version=? ORDER BY l.rowid',
                (task_id, page_version)).fetchall()
        return [self._evidence_from_row(row) for row in rows]

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> Evidence:
        bbox = json.loads(row['bbox_json']) if row['bbox_json'] else None
        return Evidence(capture_id=row['capture_id'], page_version=row['page_version'],
                        region=row['region'], extraction=row['extraction'], text=row['text'],
                        bbox=tuple(bbox) if bbox else None, source_url=row['source_url'],
                        verified=bool(row['verified']), verifier=row['verifier'],
                        skill_id=row['skill_id'], note=row['note'], at=row['at'])

    def evidence_by_capture(self, capture_id: str) -> Evidence | None:
        """按 capture_id 查证据（核验/排障辅助）。"""
        conn = self._conn_or_raise()
        row = conn.execute('SELECT * FROM acq_evidence WHERE capture_id=? ORDER BY rowid LIMIT 1',
                           (capture_id,)).fetchone()
        return None if row is None else self._evidence_from_row(row)

    # ---------------------------------------------------------- verification
    def verify(self, verification: Verification) -> bool:
        """追加核验记录；只有 ``status='supported'`` 且证据存在时才置真。

        返回是否把证据置为已核验。``needs_review / insufficient / unsupported /
        not_checked`` 只追加记录，不改变证据的核验状态。
        """
        if not isinstance(verification, Verification):
            raise TypeError('verification 必须是 Verification')
        conn = self._conn_or_raise()
        row = conn.execute(
            'SELECT l.task_id FROM acq_evidence e '
            'LEFT JOIN acq_evidence_link l ON l.fingerprint=e.fingerprint '
            'WHERE e.capture_id=? ORDER BY e.rowid, l.rowid LIMIT 1',
            (verification.capture_id,)).fetchone()
        exists = conn.execute('SELECT 1 FROM acq_evidence WHERE capture_id=? LIMIT 1',
                              (verification.capture_id,)).fetchone() is not None
        task_id = (row['task_id'] or '') if row is not None else ''
        sets_verified = verification.status == 'supported' and exists
        with conn:
            conn.execute(
                'INSERT INTO acq_verification(verification_id, task_id, capture_id, status, '
                'verifier, detail, claim, at) VALUES(?,?,?,?,?,?,?,?)',
                (verification.verification_id, task_id, verification.capture_id,
                 verification.status, verification.verifier, verification.detail,
                 verification.claim, verification.at or now_iso()))
            if sets_verified:
                conn.execute('UPDATE acq_evidence SET verified=1, verifier=? WHERE capture_id=?',
                             (verification.verifier, verification.capture_id))
        return sets_verified

    def verifications(self, task_id: str) -> list[Verification]:
        conn = self._conn_or_raise()
        rows = conn.execute('SELECT * FROM acq_verification WHERE task_id=? ORDER BY rowid',
                            (task_id,)).fetchall()
        return [Verification(verification_id=row['verification_id'], capture_id=row['capture_id'],
                             status=row['status'], verifier=row['verifier'],
                             detail=row['detail'], claim=row['claim'], at=row['at'])
                for row in rows]

    # --------------------------------------------------------------- 任务终态
    def finish_task(self, task_id: str, *, status: str, stop_reason: str,
                    error: str | None = None, gap: str | None = None,
                    session_id: str | None = None, owner: str | None = None,
                    token: str | None = None) -> None:
        """写任务终态。

        ``session_id`` / ``owner`` / ``token`` 为可选租约栅栏：给出时先在同一
        ``BEGIN IMMEDIATE`` 事务内校验租约，旧执行者不能覆盖新 owner 的终态；
        省略时保持旧行为（向后兼容）。
        """
        if status not in TASK_STATUSES:
            raise ValueError(f'未知任务状态: {status!r}')
        conn = self._conn_or_raise()
        stamp = now_iso()
        with self._immediate():
            self._fence(session_id, owner, token)
            cursor = conn.execute(
                'UPDATE acq_task SET status=?, stop_reason=?, error=?, gap=?, finished_at=?, '
                'updated_at=?, updated_epoch=? WHERE task_id=?',
                (status, stop_reason or '', error, gap, stamp, stamp, time.time(), task_id))
            if not cursor.rowcount:
                raise KeyError(f'未知任务: {task_id}')

    def resume(self, task_id: str) -> dict:
        """读取恢复所需状态：任务、收据、证据、核验、最新页面版本。"""
        task = self.task(task_id)
        if task is None:
            raise KeyError(f'未知任务: {task_id}')
        receipts = self.receipts(task_id)
        evidence = self.evidence(task_id)
        latest = task.get('latest_page_version') or ''
        if not latest:
            for receipt in reversed(receipts):
                if receipt.page_version:
                    latest = receipt.page_version
                    break
        return {'task': task, 'receipts': receipts, 'evidence': evidence,
                'verifications': self.verifications(task_id),
                'latest_page_version': latest, 'session_id': task['session_id']}

    def mark_interrupted(self, *, stale_after_seconds: float = 600,
                         now: Any = None) -> list[str]:
        """把长时间未更新的 pending/running 任务标为 interrupted；返回任务 id 列表。"""
        if stale_after_seconds < 0:
            raise ValueError('stale_after_seconds 不能为负')
        conn = self._conn_or_raise()
        cutoff = _epoch(now) - float(stale_after_seconds)
        rows = conn.execute(
            'SELECT task_id FROM acq_task WHERE status IN (\'pending\',\'running\') '
            'AND updated_epoch <= ? ORDER BY created_epoch, rowid', (cutoff,)).fetchall()
        ids = [row['task_id'] for row in rows]
        if not ids:
            return []
        stamp = now_iso()
        with conn:
            for task_id in ids:
                conn.execute(
                    'UPDATE acq_task SET status=\'interrupted\', '
                    'stop_reason=CASE WHEN stop_reason IS NULL OR stop_reason=\'\' '
                    'THEN \'interrupted\' ELSE stop_reason END, '
                    'gap=CASE WHEN gap IS NULL OR gap=\'\' THEN \'interrupted\' ELSE gap END, '
                    'finished_at=?, updated_at=?, updated_epoch=? WHERE task_id=?',
                    (stamp, stamp, time.time(), task_id))
        return ids

    # ----------------------------------------------------------------- trace
    def trace(self, task_id: str, goal: ResearchGoal, *, policy: str, backend: str) -> TaskTrace:
        """由持久化状态组装完整轨迹（可复盘：goal → 每步 receipt → evidence → gap）。"""
        task = self.task(task_id)
        if task is None:
            raise KeyError(f'未知任务: {task_id}')
        receipts = self.receipts(task_id)
        evidence = self.evidence(task_id)
        versions: list[str] = []
        for receipt in receipts:
            if receipt.page_version and receipt.page_version not in versions:
                versions.append(receipt.page_version)
        cost = ZERO_COST
        for receipt in receipts:
            cost = cost + receipt.cost
        return TaskTrace(
            task_id=task_id,
            question=goal.question,
            backend=backend,
            policy=policy,
            status=task['status'],
            stop_reason=task['stop_reason'] or '',
            page_versions=tuple(versions),
            receipts=tuple(receipts),
            evidence=tuple(evidence),
            cost=cost,
            steps=int(task['steps'] or 0),
            gap=task['gap'],
            verifications=tuple(self.verifications(task_id)),
            started_at=task['created_at'] or '',
            finished_at=task['finished_at'] or '',
        )

    # ----------------------------------------------------------------- lease
    def acquire_lease(self, session_id: str, owner: str, ttl_seconds: float = 120) -> str:
        """获取（或接管）session 租约；返回租约 token。

        后取得租约的 owner 会接管 session，旧 owner 在下一次栅栏检查时失败；
        这是「旧执行者不得覆盖新执行者结果」的 fencing 语义。
        """
        if not owner:
            raise ValueError('租约必须记录 owner')
        if ttl_seconds <= 0:
            raise ValueError('ttl_seconds 必须为正')
        now = time.time()
        token = 'lease_' + uuid.uuid4().hex[:24]
        conn = self._conn_or_raise()
        with conn:
            conn.execute(
                'INSERT INTO acq_lease(session_id, owner, token, expires_at, ttl_seconds, updated_at) '
                'VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET '
                'owner=excluded.owner, token=excluded.token, expires_at=excluded.expires_at, '
                'ttl_seconds=excluded.ttl_seconds, updated_at=excluded.updated_at',
                (session_id, owner, token, now + float(ttl_seconds), float(ttl_seconds), now_iso()))
        return token

    def _lease_row(self, session_id: str) -> sqlite3.Row | None:
        conn = self._conn_or_raise()
        return conn.execute('SELECT * FROM acq_lease WHERE session_id=?', (session_id,)).fetchone()

    def _check_lease(self, session_id: str, owner: str,
                     token: str | None = None) -> sqlite3.Row:
        row = self._lease_row(session_id)
        if row is None:
            raise LeaseLost(f'租约不存在: {session_id}')
        if row['owner'] != owner:
            raise LeaseLost('租约已被其他执行者接管')
        if token is not None and row['token'] != token:
            raise LeaseLost('租约 token 不匹配（同一 owner 的租约已被重新获取）')
        if float(row['expires_at']) <= time.time():
            raise LeaseLost('租约已过期')
        return row

    def assert_owner(self, session_id: str, owner: str,
                     token: str | None = None) -> None:
        self._check_lease(session_id, owner, token)

    def renew_lease(self, session_id: str, owner: str,
                    token: str | None = None) -> None:
        """续租（同时作为长任务心跳）；token 给出时必须与当前租约一致。"""
        self._check_lease(session_id, owner, token)
        conn = self._conn_or_raise()
        ttl = float(self._lease_row(session_id)['ttl_seconds'])
        with conn:
            conn.execute('UPDATE acq_lease SET expires_at=?, updated_at=? WHERE session_id=?',
                         (time.time() + ttl, now_iso(), session_id))

    def release_lease(self, session_id: str, owner: str,
                      token: str | None = None) -> None:
        row = self._lease_row(session_id)
        if row is None:
            return
        if row['owner'] != owner:
            raise LeaseLost('租约已被其他执行者接管，不能释放')
        if token is not None and row['token'] != token:
            raise LeaseLost('租约 token 不匹配，不能释放')
        conn = self._conn_or_raise()
        with conn:
            conn.execute('DELETE FROM acq_lease WHERE session_id=? AND owner=?', (session_id, owner))
