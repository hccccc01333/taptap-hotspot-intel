"""从 CHANGELOG.md 取最新版本小节，打 git tag 并发布 GitHub Release。

用法：
  python scripts/release_from_changelog.py --dry-run   # 只打印将执行的动作
  python scripts/release_from_changelog.py             # 打 tag（如缺）+ 创建 Release（如缺）

前置条件：CHANGELOG 的新版本小节已提交并推送，工作区干净，gh CLI 已登录。
幂等：tag 或 Release 已存在时跳过对应步骤，可安全重跑。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
TAG_PREFIX = "v"

SECTION_RE = re.compile(r"^## \[(?P<ver>[^\]]+)\] - (?P<date>\d{4}-\d{2}-\d{2})\s*$", re.M)


def parse_latest(changelog_text: str) -> tuple[str, str, str]:
    """返回 (version, date, notes_body)。

    小节标题形如 `## [3.0.0-alpha.28] - 2026-10-15`；`## [未发布]` 无日期、不匹配，
    因此自然被跳过。正文取到下一个 `## ` 标题为止。
    """
    matches = list(SECTION_RE.finditer(changelog_text))
    if not matches:
        raise ValueError("CHANGELOG.md 中没有找到版本小节（## [版本] - 日期）")
    first = matches[0]
    next_boundary = matches[1].start() if len(matches) > 1 else len(changelog_text)
    body = changelog_text[first.end():next_boundary].strip()
    return first.group("ver"), first.group("date"), body


def tag_for(version: str) -> str:
    """数字开头的版本号加 v 前缀；已带 v/V 前缀或具名版本原样保留。"""
    return version if version[:1] in ("v", "V") else TAG_PREFIX + version


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, check=check, capture_output=True, text=True, encoding="utf-8")


def release(dry_run: bool, allow_dirty: bool) -> None:
    version, date, body = parse_latest(CHANGELOG.read_text(encoding="utf-8"))
    tag = tag_for(version)
    print(f"CHANGELOG 最新版本：{version}（{date}）→ tag {tag}")

    dirty = [line for line in _run(["git", "status", "--porcelain"]).stdout.splitlines() if line.strip()]
    if dirty and not (allow_dirty or dry_run):
        raise SystemExit("工作区有未提交改动；先提交推送，或用 --allow-dirty 继续：\n" + "\n".join(dirty[:10]))

    tag_exists = bool(_run(["git", "tag", "-l", tag], check=False).stdout.strip())
    if not tag_exists and not dry_run:
        _run(["git", "tag", "-a", tag, "-m", f"{version}（{date}）\n\n详见 CHANGELOG.md 对应小节。"])
        _run(["git", "push", "origin", tag])
        print(f"已创建并推送 tag {tag}")
    elif tag_exists:
        print(f"tag {tag} 已存在，跳过")

    view = _run(["gh", "release", "view", tag], check=False)
    if view.returncode == 0:
        print(f"Release {tag} 已存在，跳过：{view.stdout.splitlines()[0]}")
        return
    if dry_run:
        print(f"[dry-run] 将创建 Release {tag}，说明为 CHANGELOG 对应小节（{len(body)} 字符）")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body)
        notes = f.name
    try:
        created = _run(["gh", "release", "create", tag, "--title", version, "--notes-file", notes])
        print(f"Release 已发布：{created.stdout.strip()}")
    finally:
        Path(notes).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的动作")
    parser.add_argument("--allow-dirty", action="store_true", help="工作区未提交时也继续（不建议）")
    args = parser.parse_args()
    try:
        release(args.dry_run, args.allow_dirty)
    except ValueError as e:
        raise SystemExit(str(e)) from e


if __name__ == "__main__":
    sys.exit(main())
