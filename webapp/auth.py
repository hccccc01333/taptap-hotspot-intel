# -*- coding: utf-8 -*-
"""webapp 认证：角色登录 + HMAC 令牌（骨架版）。

★ 设计 §45 的终态是「公司 SSO / OAuth」；骨架先落**角色模型本身**（§43 矩阵），
  令牌只证明"你是哪个角色"，写路径的真正闸门在 execution.audit.require_role ——
  就算令牌被伪造，agent 角色也拿不到 approve/publish/kill_switch。

用户文件只保存在本地；新账号使用带随机盐的 PBKDF2。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

_execution_root = str(Path(__file__).resolve().parents[1] / "execution")
if _execution_root not in sys.path:
    sys.path.insert(0, _execution_root)
from execution.audit import ROLES  # 同一张角色表（§43）

WEBAPP_DIR = Path(__file__).resolve().parent
USERS_FILE = WEBAPP_DIR / "auth_users.json"

TOKEN_TTL_SEC = 12 * 3600


def _secret_path() -> Path:
    env = os.environ.get("L6_EXECUTION_DB")
    base = Path(env).parent if env else Path(os.getcwd()) / "data" / "state"
    base.mkdir(parents=True, exist_ok=True)
    return base / "webapp_secret.key"


def _secret() -> bytes:
    p = _secret_path()
    if not p.exists():
        import secrets
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(secrets.token_bytes(32))
    return p.read_bytes()


def _load_users() -> Dict[str, Dict[str, Any]]:
    path = Path(os.environ.get("TAPTAP_AUTH_USERS_FILE") or USERS_FILE)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        users = json.load(f)
    return {u["actor"]: u for u in users}


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("密码至少需要 12 个字符")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return f"pbkdf2_sha256$600000${salt}${digest}"


def _verify_password(user: Dict[str, Any], password: str) -> bool:
    encoded = user.get("password_hash")
    if encoded is not None:
        try:
            algorithm, rounds, salt, expected = encoded.split("$")
            iterations = int(rounds)
            if algorithm != "pbkdf2_sha256" or not 100000 <= iterations <= 1000000:
                return False
            salt_bytes = bytes.fromhex(salt)
            if len(salt_bytes) != 16 or len(expected) != 64:
                return False
            actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt_bytes, iterations).hex()
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError, AttributeError):
            return False
    # Compatibility for existing local installations; never used by account creation.
    digest = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(digest, user.get("sha256", ""))


def initialize_user(actor: str, role: str, password: str) -> Path:
    if not actor.strip() or "|" in actor or role not in ROLES:
        raise ValueError("账号名称或角色无效")
    path = Path(os.environ.get("TAPTAP_AUTH_USERS_FILE") or USERS_FILE)
    # Initialization refuses to replace any existing file, including legacy accounts.
    user = {"actor": actor, "role": role, "password_hash": hash_password(password), "disabled": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump([user], stream, ensure_ascii=False, indent=2)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def verify_login(actor: str, password: str) -> Optional[Dict[str, Any]]:
    """登录校验。成功返回 {"actor","role"}，失败 None（不区分账号/密码错误）。"""
    user = _load_users().get(actor)
    if not user or user.get("disabled"):
        return None
    if not _verify_password(user, password or ""):
        return None
    role = user.get("role")
    if role not in ROLES:
        return None
    return {"actor": actor, "role": role}


def issue_token(actor: str, role: str) -> str:
    """令牌 = base64(payload) + "." + HMAC 签名。

    ★ base64 的原因：actor 是中文，HTTP 头只允许 ASCII——
      这个坑在测试里以 UnicodeEncodeError 的形式现形，不是猜的。
    """
    exp = int(time.time()) + TOKEN_TTL_SEC
    payload = f"{actor}|{role}|{exp}"
    raw = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    sig = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def parse_token(token: str) -> Optional[Dict[str, Any]]:
    """令牌 → {actor, role}；过期/伪造 → None。"""
    try:
        raw, sig = token.split(".")
        payload = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
        expect = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        actor, role, exp = payload.split("|")
        if int(exp) < int(time.time()):
            return None
        if role not in ROLES:
            return None
        return {"actor": actor, "role": role}
    except (ValueError, TypeError):
        return None


def main() -> None:
    import argparse
    import getpass
    parser = argparse.ArgumentParser(description="初始化本地账号；不会覆盖现有账号文件")
    parser.add_argument("--init-user", required=True)
    parser.add_argument("--role", choices=sorted(ROLES), default="operator")
    args = parser.parse_args()
    password = getpass.getpass("密码（至少12字符）：")
    if password != getpass.getpass("再次输入密码："):
        parser.error("两次密码不一致")
    try:
        path = initialize_user(args.init_user, args.role, password)
    except (ValueError, FileExistsError) as error:
        parser.error("账号文件已存在，不会覆盖" if isinstance(error, FileExistsError) else str(error))
    print(f"本地账号已初始化：{path}；请勿提交账号文件。")


if __name__ == "__main__":
    main()
