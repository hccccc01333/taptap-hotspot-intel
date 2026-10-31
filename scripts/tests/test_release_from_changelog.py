# -*- coding: utf-8 -*-
"""release_from_changelog 的解析与命名规则测试。"""
from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.release_from_changelog import parse_latest, tag_for  # noqa: E402

SAMPLE = """# 更新日志

## [未发布]

- 暂无。

## [3.0.0-alpha.29] - 2026-11-02

### Added

- 新能力。

## [3.0.0-alpha.28] - 2026-10-15

### Changed

- 旧内容。
"""


class ParseLatestTests(unittest.TestCase):
    def test_picks_first_dated_section_and_skips_unreleased(self):
        version, date, body = parse_latest(SAMPLE)
        self.assertEqual((version, date), ("3.0.0-alpha.29", "2026-11-02"))
        self.assertIn("新能力", body)
        self.assertNotIn("旧内容", body)
        self.assertNotIn("暂无", body)

    def test_last_section_extends_to_end_of_file(self):
        text = "# 头\n\n## [1.0.0] - 2026-01-01\n\n- 唯一小节。\n"
        version, date, body = parse_latest(text)
        self.assertEqual((version, date), ("1.0.0", "2026-01-01"))
        self.assertIn("唯一小节", body)

    def test_no_dated_section_raises(self):
        with self.assertRaises(ValueError):
            parse_latest("# 更新日志\n\n## [未发布]\n\n- 暂无。\n")


class TagNamingTests(unittest.TestCase):
    def test_numeric_version_gets_v_prefix(self):
        self.assertEqual(tag_for("3.0.0-alpha.28"), "v3.0.0-alpha.28")

    def test_prefixed_and_named_versions_unchanged(self):
        self.assertEqual(tag_for("v1.2.3"), "v1.2.3")
        self.assertEqual(tag_for("V2 基线"), "V2 基线")


if __name__ == "__main__":
    unittest.main()
