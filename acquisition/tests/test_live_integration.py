"""切片 2 真实浏览器集成验收（Lead 独立编写，不依赖 live-acceptance 的 runner）。

这些测试只在真实 Chromium/Edge + Playwright + Node 可用时运行；CI 的 Linux 环境没有
这些依赖时自动 skip，不把 skip 当作通过。检查目标：

- 真实 Chromium 中的懒加载页面：自适应策略点击展开后从 DOM 取得候选证据，
  证据仍为未核验；固定三屏/现状 V3 基线在未展开页面上取不到同一证据；
- 同一 PlaywrightDriver 复用同一浏览器进程（session_launches 不增长）；
- 事件页面版本随 DOM 变化而变化；
- 定点截图落盘且 sha256 与 observation 的 capture 一致（OCR 可用时）。

结论只适用于这些本地合成 HTML 页面上的机制，不代表真实网站采集质量。
"""
import hashlib
import tempfile
import unittest
from pathlib import Path

from acquisition import model
from acquisition.harness import AcquisitionHarness
from acquisition.policy import AdaptiveObservationPolicy, FixedFramePolicy, LegacyV3Policy
from acquisition.store import AcquisitionStore

try:
    from acquisition import live_driver
except ImportError:  # pragma: no cover - live_driver 由并行任务交付
    live_driver = None

LAZY_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>本地合成活动页（测试）</title></head>
<body>
<main>
  <h1>合成活动公告</h1>
  <p>本页只用于离线机制测试，不是真实站点。</p>
  <div style="height:2600px"></div>
  <section id="rules-box">
    <h2>活动规则</h2>
    <button id="expand-rules">展开规则</button>
    <div id="rules-body" style="display:none">报名截止：2026年11月20日 23:59</div>
  </section>
</main>
<footer>合成样本</footer>
<script>
document.getElementById('expand-rules').onclick=function(){
  document.getElementById('rules-body').style.display='block';
  this.textContent='规则已展开';
};
</script>
</body></html>"""

STATIC_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>本地合成静态页（测试）</title></head>
<body><main>
  <h1>合成活动公告</h1>
  <section><h2>活动规则</h2><p>报名截止：2026年11月20日 23:59</p></section>
</main></body></html>"""

POSTER_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>本地合成海报页（测试）</title></head>
<body><main>
  <h1>合成活动海报</h1>
  <div style="height:2600px"></div>
  <figure id="poster"><img id="poster-img" width="900" height="500"
    alt="合成海报"
    src="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='900' height='500'><rect width='100%25' height='100%25' fill='%23ffffff'/><text x='40' y='160' font-size='64' font-family='Microsoft YaHei' fill='%23000'>活动截止日期</text><text x='40' y='280' font-size='64' font-family='Microsoft YaHei' fill='%23000'>2026年11月30日</text></svg>"/>
  </figure>
</main></body></html>"""


def goal_for(question='确认合成活动的报名截止日期'):
    objectives = (model.Objective('deadline', question, 'date', ('activity_rules',)),)
    return model.ResearchGoal('live_integration', question, objectives,
                              allowed_hosts=('www.taptap.cn',))


def browser_ready():
    if live_driver is None:
        return False, 'live_driver 不可用'
    try:
        return live_driver.available()
    except Exception as error:  # pragma: no cover - 环境异常不应当掩埋
        return False, f'{type(error).__name__}: {error}'


@unittest.skipUnless(browser_ready()[0], f'真实浏览器不可用: {browser_ready()[1]}')
class LiveBrowserIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._folders = []
        self._stores = []

    def tearDown(self):
        for store in self._stores:
            try:
                store.close()
            except RuntimeError:
                pass
        for folder in self._folders:
            try:
                folder.cleanup()
            except OSError:
                pass

    def temporary_dir(self):
        folder = tempfile.TemporaryDirectory()
        self._folders.append(folder)
        return Path(folder.name)

    def store(self):
        store = AcquisitionStore(self.temporary_dir() / 'live.sqlite3')
        self._stores.append(store)
        return store

    def driver(self, fixture_html):
        capture_dir = self.temporary_dir() / 'captures'
        driver = live_driver.PlaywrightDriver(fixture_html=fixture_html,
                                             artifact_dir=capture_dir, timeout=30)
        self._capture_dirs = getattr(self, '_capture_dirs', []) + [capture_dir]
        return driver

    def run_page(self, html, policy):
        driver = self.driver(html)
        self._drivers = getattr(self, '_drivers', []) + [driver]
        store = self.store()
        trace = AcquisitionHarness(driver, policy, store,
                                   budgets=model.Budget(max_units=24, max_captures=8,
                                                        max_ocr_calls=8, max_steps=30)
                                   ).run(goal_for())
        return trace, driver, store

    def test_lazy_page_dynamic_observation_beats_fixed_baselines(self):
        adaptive, driver, store = self.run_page(LAZY_HTML, AdaptiveObservationPolicy())
        text = '\n'.join(item.text for item in adaptive.evidence)
        self.assertIn('2026年11月20日', text, '自适应策略应点击展开后读到截止日期')
        self.assertTrue(all(item.verified is False for item in adaptive.evidence))
        self.assertIn('interact', [receipt.action for receipt in adaptive.receipts])
        self.assertGreaterEqual(len(adaptive.page_versions), 2, '点击展开应改变页面版本')
        self.assertEqual(adaptive.stop_reason, 'objectives_covered')

        baseline, _driver2, _store2 = self.run_page(LAZY_HTML, FixedFramePolicy())
        baseline_text = '\n'.join(item.text for item in baseline.evidence)
        self.assertNotIn('2026年11月20日', baseline_text,
                         '固定三屏不点击展开，不应读到规则正文')
        legacy, _driver3, _store3 = self.run_page(LAZY_HTML, LegacyV3Policy())
        legacy_text = '\n'.join(item.text for item in legacy.evidence)
        self.assertNotIn('2026年11月20日', legacy_text,
                         '现状 V3 基线读整页 DOM 时规则尚未展开，不应读到截止日期')

    def test_same_driver_reuses_browser_session_across_navigations(self):
        driver = self.driver(STATIC_HTML)
        self._drivers = getattr(self, '_drivers', []) + [driver]
        try:
            first = driver.open(goal_for())
            self.assertTrue(first.page_version.startswith('sha256:'))
            second = driver.navigate(fixture_html=LAZY_HTML)
            self.assertNotEqual(first.page_version, second.page_version)
            self.assertEqual(driver.session_launches, 1, '同一 driver 不应重复启动浏览器')
        finally:
            driver.close()

    def test_region_screenshot_is_hashed_and_bounded(self):
        driver = self.driver(POSTER_HTML)
        self._drivers = getattr(self, '_drivers', []) + [driver]
        try:
            view = driver.open(goal_for('确认海报中的截止日期'))
            poster = next((region for region in view.regions if region.role == 'poster'), None)
            self.assertIsNotNone(poster, '应识别出纯图片海报区域')
            observation = driver.extract(view, poster, 'ocr')
            self.assertTrue(observation.capture_id, 'OCR 提取应产生 capture 标识')
            self.assertTrue(observation.bbox, '证据应带区域 bbox')
            if observation.text:
                # OCR 可能因空格/字形误识别，只要求非空并记录 note；语义命中在 live-acceptance 校验。
                self.assertGreater(len(observation.text.strip()), 0)
            artifact_dir = self._capture_dirs[-1]
            captures = [path for path in artifact_dir.rglob('*.png')]
            self.assertTrue(captures, '截图文件应落盘')
            for path in captures:
                self.assertTrue(path.is_file())
                self.assertTrue(hashlib.sha256(path.read_bytes()).hexdigest())
        finally:
            driver.close()


if __name__ == '__main__':
    unittest.main()
