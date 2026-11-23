"""driver 模块测试：懒加载、视觉文本通道、阻塞页、固定三屏、真值不泄漏。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from acquisition import model
from acquisition.driver import FixtureDriver, LegacyBrowseAdapter


def make_goal() -> model.ResearchGoal:
    objective = model.Objective(objective_id='deadline', question='确认活动截止日期',
                                answer_kind='date', region_hints=('activity_rules',))
    return model.ResearchGoal(task_id='goal_1', question='确认活动截止日期',
                              objectives=(objective,), allowed_hosts=('www.taptap.cn',))


def article_region() -> dict:
    return {'region_id': 'article', 'role': 'article_body', 'bbox': [100, 200, 1200, 600],
            'text': '活动时间为2026年11月10日。', 'visual_text': '', 'lazy': False,
            'requires': None, 'blocked': False, 'scroll_y': 200}


def lazy_region(*, text: str = '', visual_text: str = '截止日期：2026年11月20日',
                region_id: str = 'rules') -> dict:
    return {'region_id': region_id, 'role': 'activity_rules', 'bbox': [120, 1500, 1200, 700],
            'text': text, 'visual_text': visual_text, 'lazy': True,
            'requires': 'click:展开规则', 'blocked': False, 'scroll_y': 1500}


def poster_region() -> dict:
    return {'region_id': 'poster', 'role': 'poster', 'bbox': [300, 300, 600, 500],
            'text': '', 'visual_text': '海报文字：限时活动', 'lazy': False,
            'requires': None, 'blocked': False, 'scroll_y': 300}


def make_page(**overrides) -> dict:
    page = {
        'schema': 'acquisition-fixture-v0.1',
        'page_id': 'test_page',
        'label': '合成测试页（不是真实站点录制）',
        'url': 'https://www.taptap.cn/example/test-001',
        'title': '活动公告（合成）',
        'viewport': [1440, 1000],
        'scroll_height': 2500,
        'blocked_reason': None,
        'dom_text': '活动公告正文：活动时间为2026年11月10日。',
        'regions': [article_region(), lazy_region(), poster_region()],
        'objectives': [{
            'objective_id': 'deadline', 'question': '确认活动截止日期', 'answer_kind': 'date',
            'region_hints': ['activity_rules'], 'required': True,
            'expected_answer': '2026年11月20日', 'expected_region_role': 'activity_rules',
        }],
        'skill_ref': 'taptap-activity-rules',
    }
    page.update(overrides)
    return page


def virtual_region(region_id: str) -> model.Region:
    return model.Region(region_id=region_id, role='section', bbox=(0, 0, 1440, 1000), text='')


class DriverTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.goal = make_goal()
        self.page = make_page()
        self.driver = FixtureDriver(self.page)
        self.view = self.driver.open(self.goal)


class LazyLoadingTests(DriverTestBase):
    def test_lazy_region_not_readable_before_interact(self):
        lazy = self.view.region('rules')
        self.assertEqual(lazy.text, '')
        self.assertEqual(lazy.visual_text, '')
        dom_result = self.driver.extract(self.view, lazy, 'dom')
        self.assertFalse(dom_result.blocked)
        self.assertEqual(dom_result.text, '')
        self.assertEqual(dom_result.note, 'lazy_not_loaded')
        self.assertEqual(dom_result.cost.units, 1)  # 成本照记
        ocr_result = self.driver.extract(self.view, lazy, 'ocr')
        self.assertEqual(ocr_result.text, '')
        self.assertEqual(ocr_result.note, 'lazy_not_loaded')
        self.assertEqual(ocr_result.cost.captures, 1)
        self.assertEqual(self.driver.capture_count, 1)

    def test_interact_creates_new_page_version_and_reveals_visual_text(self):
        lazy = self.view.region('rules')
        observation = self.driver.interact(self.view, lazy)
        self.assertIsNotNone(observation.view)
        self.assertNotEqual(observation.view.page_version, self.view.page_version)
        self.assertEqual(observation.action, 'interact')
        self.assertEqual(observation.cost.units, 2)
        revealed = observation.view.region('rules')
        self.assertIn('2026年11月20日', revealed.visual_text)
        ocr_after = self.driver.extract(observation.view, revealed, 'ocr')
        self.assertIn('截止日期：2026年11月20日', ocr_after.text)
        # 触发后 text 仍然是空（视觉文本不属于 DOM），dom 通道读不到它。
        dom_after = self.driver.extract(observation.view, revealed, 'dom')
        self.assertEqual(dom_after.text, '')

    def test_dom_body_after_interact_contains_expanded_lazy_dom_text(self):
        rules_with_dom = lazy_region(text='规则正文：参与方式为公开转发。', visual_text='')
        page = make_page(regions=[article_region(), rules_with_dom,
                                  lazy_region(region_id='rules2', visual_text='另一段视觉文字')])
        page['dom_text'] = '活动公告正文：活动时间为2026年11月10日。'
        driver = FixtureDriver(page)
        view = driver.open(self.goal)
        self.assertNotIn('规则正文', view.dom_text)
        body_before = driver.extract(view, virtual_region(model.DOM_BODY_REGION), 'dom')
        self.assertNotIn('规则正文', body_before.text)
        observation = driver.interact(view, view.region('rules'))
        body_after = driver.extract(observation.view, virtual_region(model.DOM_BODY_REGION), 'dom')
        self.assertIn('规则正文', body_after.text)
        self.assertNotIn('另一段视觉文字', body_after.text)  # visual_text 不属于 DOM

    def test_interact_non_lazy_region_does_not_change_version(self):
        observation = self.driver.interact(self.view, self.view.region('article'))
        self.assertEqual(observation.page_version, self.view.page_version)
        self.assertIsNone(observation.view)
        self.assertEqual(observation.note, 'no_state_change')


class VisualTextChannelTests(DriverTestBase):
    def test_dom_channel_never_returns_visual_text(self):
        poster = self.view.region('poster')
        result = self.driver.extract(self.view, poster, 'dom')
        self.assertEqual(result.text, '')
        self.assertFalse(result.blocked)
        article = self.driver.extract(self.view, self.view.region('article'), 'dom')
        self.assertEqual(article.text, '活动时间为2026年11月10日。')
        self.assertNotIn('海报文字', article.text)

    def test_ocr_and_vlm_channels_read_visual_text(self):
        poster = self.view.region('poster')
        ocr = self.driver.extract(self.view, poster, 'ocr')
        self.assertIn('海报文字：限时活动', ocr.text)
        self.assertEqual(ocr.extraction, 'ocr')
        self.assertTrue(ocr.capture_id)
        self.assertEqual(ocr.cost.as_dict(),
                         {'units': 3, 'captures': 1, 'ocr_calls': 1, 'vlm_calls': 0})
        vlm = self.driver.extract(self.view, poster, 'vlm')
        self.assertIn('海报文字：限时活动', vlm.text)
        self.assertEqual(vlm.cost.as_dict(),
                         {'units': 5, 'captures': 1, 'ocr_calls': 0, 'vlm_calls': 1})
        self.assertEqual(self.driver.capture_count, 2)

    def test_capture_id_is_stable(self):
        poster = self.view.region('poster')
        first = self.driver.extract(self.view, poster, 'ocr')
        second = self.driver.extract(self.view, poster, 'ocr')
        self.assertEqual(first.capture_id, second.capture_id)
        self.assertEqual(self.driver.capture_count, 2)  # 两次截图成本照记

    def test_ocr_reads_dom_text_plus_visual_text(self):
        result = self.driver.extract(self.view, self.view.region('article'), 'ocr')
        self.assertIn('活动时间为2026年11月10日', result.text)


class BlockedPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.goal = make_goal()
        self.driver = FixtureDriver(make_page(blocked_reason='captcha'))
        self.view = self.driver.open(self.goal)

    def test_any_extraction_returns_blocked_without_text(self):
        self.assertEqual(self.view.blocked_reason, 'captcha')
        for method in ('dom', 'ocr', 'vlm'):
            result = self.driver.extract(self.view, self.view.region('article'), method)
            self.assertTrue(result.blocked)
            self.assertEqual(result.text, '')
            self.assertEqual(result.note, 'blocked_page:captcha')
        self.assertGreaterEqual(self.driver.capture_count, 2)  # ocr/vlm 截图成本照记

    def test_blocked_page_interact_is_blocked(self):
        result = self.driver.interact(self.view, self.view.region('rules'))
        self.assertTrue(result.blocked)
        self.assertEqual(result.note, 'blocked_page:captcha')
        self.assertIsNone(result.view)

    def test_inspect_on_blocked_page_is_not_a_blocked_receipt(self):
        result = self.driver.inspect(self.view)
        self.assertFalse(result.blocked)
        self.assertEqual(result.text, '')
        self.assertIn('captcha', result.note)


class LoginRegionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.goal = make_goal()
        comments = {'region_id': 'comments', 'role': 'comments',
                    'bbox': [100, 1200, 1200, 400], 'text': '玩家评论内容',
                    'visual_text': '玩家评论截图文字', 'lazy': False,
                    'requires': 'login', 'blocked': False, 'scroll_y': 1200}
        self.driver = FixtureDriver(make_page(regions=[article_region(), comments]))
        self.view = self.driver.open(self.goal)

    def test_login_required_region_hidden_and_blocked(self):
        comments = self.view.region('comments')
        self.assertEqual(comments.text, '')
        self.assertEqual(comments.visual_text, '')
        dom_result = self.driver.extract(self.view, comments, 'dom')
        self.assertTrue(dom_result.blocked)
        self.assertEqual(dom_result.note, 'login_required')
        self.assertEqual(dom_result.text, '')
        interaction = self.driver.interact(self.view, comments)
        self.assertTrue(interaction.blocked)
        self.assertEqual(interaction.note, 'login_required')

    def test_other_regions_still_readable(self):
        result = self.driver.extract(self.view, self.view.region('article'), 'dom')
        self.assertFalse(result.blocked)
        self.assertIn('2026年11月10日', result.text)


class FixedFrameViewportTests(DriverTestBase):
    def test_viewport_bbox_follows_model_fixed_frame_offsets(self):
        view = self.view
        offsets = model.fixed_frame_offsets(view.scroll_height, view.viewport[1])
        self.assertEqual(offsets, (0, 850, 1500))  # 与固定三屏约定一致
        for index, expected_y in enumerate(offsets):
            result = self.driver.extract(view, virtual_region(f'viewport:{index}'), 'ocr')
            self.assertEqual(result.role, 'section')
            self.assertEqual(result.bbox, (0, expected_y, 1440, 1000))
            result_dom = self.driver.extract(view, virtual_region(f'viewport:{index}'), 'dom')
            self.assertEqual(result_dom.bbox, (0, expected_y, 1440, 1000))

    def test_viewport_ocr_concatenates_intersecting_regions(self):
        first = self.driver.extract(self.view, virtual_region('viewport:0'), 'ocr')
        self.assertIn('活动时间为2026年11月10日', first.text)
        self.assertIn('海报文字：限时活动', first.text)
        self.assertNotIn('截止日期：2026年11月20日', first.text)  # rules 在第二屏之外
        second = self.driver.extract(self.view, virtual_region('viewport:2'), 'ocr')
        # 第二屏（y=1500 起）覆盖懒加载规则区域，但未触发时不可读。
        self.assertNotIn('截止日期：2026年11月20日', second.text)
        # viewport:0 的 dom 通道只读 text，不读 visual_text。
        first_dom = self.driver.extract(self.view, virtual_region('viewport:0'), 'dom')
        self.assertIn('活动时间为2026年11月10日', first_dom.text)
        self.assertNotIn('海报文字', first_dom.text)

    def test_viewport_after_lazy_expand_reads_visual_text(self):
        observation = self.driver.interact(self.view, self.view.region('rules'))
        view = observation.view
        result = self.driver.extract(view, virtual_region('viewport:1'), 'ocr')
        self.assertIn('截止日期：2026年11月20日', result.text)

    def test_viewport_index_out_of_range_raises(self):
        short_driver = FixtureDriver(make_page(scroll_height=800))
        short_view = short_driver.open(self.goal)
        self.assertEqual(model.fixed_frame_offsets(800, 1000), (0,))
        short_driver.extract(short_view, virtual_region('viewport:0'), 'ocr')
        with self.assertRaises(ValueError):
            short_driver.extract(short_view, virtual_region('viewport:1'), 'ocr')

    def test_dom_body_extraction_matches_view_dom_text(self):
        result = self.driver.extract(self.view, virtual_region(model.DOM_BODY_REGION), 'dom')
        self.assertEqual(result.text, self.view.dom_text.strip())
        self.assertEqual(result.role, 'section')
        self.assertGreaterEqual(result.bbox[1] + result.bbox[3], self.view.scroll_height)


class InspectLocateTests(DriverTestBase):
    def test_inspect_returns_candidates_without_text(self):
        result = self.driver.inspect(self.view)
        self.assertEqual(result.action, 'inspect')
        self.assertEqual(result.text, '')
        self.assertFalse(result.readable)
        self.assertEqual(result.candidates, ('article', 'rules', 'poster'))
        self.assertEqual(result.cost.units, 1)

    def test_locate_orders_region_hint_matches_first_without_text(self):
        result = self.driver.locate(self.view, self.goal)
        self.assertEqual(result.action, 'locate')
        self.assertEqual(result.candidates[0], 'rules')  # objective.region_hints 命中
        self.assertEqual(result.text, '')
        self.assertFalse(result.readable)

    def test_inspect_and_locate_do_not_create_captures(self):
        self.driver.inspect(self.view)
        self.driver.locate(self.view, self.goal)
        self.assertEqual(self.driver.capture_count, 0)


class DriverLifecycleTests(unittest.TestCase):
    def test_page_actions_before_open_raise(self):
        goal = make_goal()
        reference_view = FixtureDriver(make_page()).open(goal)
        driver = FixtureDriver(make_page())
        with self.assertRaises(RuntimeError):
            driver.inspect(reference_view)
        with self.assertRaises(RuntimeError):
            driver.extract(reference_view, reference_view.region('article'), 'dom')

    def test_close_blocks_further_actions(self):
        driver = FixtureDriver(make_page())
        goal = make_goal()
        view = driver.open(goal)
        driver.close()
        with self.assertRaises(RuntimeError):
            driver.inspect(view)
        with self.assertRaises(RuntimeError):
            driver.open(goal)
        self.assertEqual(driver.call_counts.get('close'), 1)

    def test_from_file_reads_utf8_sample(self):
        page = make_page()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'page.json'
            path.write_text(json.dumps(page, ensure_ascii=False), encoding='utf-8')
            driver = FixtureDriver.from_file(path)
            view = driver.open(make_goal())
            self.assertEqual(view.title, '活动公告（合成）')
            self.assertIn('2026年11月10日', view.dom_text)

    def test_call_counts_track_actions(self):
        driver = FixtureDriver(make_page())
        goal = make_goal()
        view = driver.open(goal)
        driver.inspect(view)
        driver.extract(view, view.region('article'), 'dom')
        driver.extract(view, view.region('poster'), 'ocr')
        self.assertEqual(driver.call_counts.get('open'), 1)
        self.assertEqual(driver.call_counts.get('inspect'), 1)
        self.assertEqual(driver.call_counts.get('extract'), 2)
        self.assertEqual(driver.call_counts.get('extract:dom'), 1)
        self.assertEqual(driver.call_counts.get('extract:ocr'), 1)


class NoTruthLeakTests(DriverTestBase):
    def test_view_does_not_expose_expected_truth(self):
        rendered_view = repr(self.view)
        self.assertNotIn('expected_answer', rendered_view)
        self.assertNotIn('expected_region_role', rendered_view)
        # 懒加载未触发：真值文本在 view 中也必须不可读。
        self.assertNotIn('2026年11月20日', rendered_view)

    def test_observations_do_not_contain_expected_fields(self):
        poster = self.view.region('poster')
        for method in ('dom', 'ocr'):
            result = self.driver.extract(self.view, poster, method)
            self.assertNotIn('expected', repr(result))
        self.assertNotIn('expected', repr(self.driver.inspect(self.view)))
        self.assertNotIn('expected', repr(self.driver.locate(self.view, self.goal)))


class LegacyBrowseAdapterTests(unittest.TestCase):
    def _capture(self, *, status: str = 'ok') -> dict:
        return {
            'status': status,
            'resolved_url': 'https://www.taptap.cn/example/comments-001',
            'title': '评论区（合成）',
            'body': '帖子正文：活动时间为2026年11月10日。',
            'captures': [
                {'file': 'frame-0.png', 'scroll_y': 0, 'width': 1440, 'height': 1000,
                 'ocr': {'text': '第一屏 OCR 文本'}, 'comment_transcript': ''},
                {'file': 'frame-1.png', 'scroll_y': 850, 'width': 1440, 'height': 1000,
                 'ocr': {'text': '第二屏 OCR 文本'}, 'comment_transcript': ''},
            ],
            'comment_cards': [
                {'id': 'c1', 'text': '玩家评论一', 'frame_index': 0,
                 'bbox': {'x': 10, 'y': 20, 'width': 300, 'height': 80}},
                {'id': 'c2', 'text': '玩家评论二', 'frame_index': 1,
                 'bbox': {'x': 10, 'y': 30, 'width': 300, 'height': 80}},
            ],
            'comment_region': {'status': 'found'},
        }

    def test_maps_fixed_frames_and_comment_cards_to_page_view(self):
        adapter = LegacyBrowseAdapter(self._capture())
        self.assertEqual(adapter.backend, 'playwright')
        view = adapter.to_page_view()
        self.assertTrue(view.page_version)
        self.assertEqual(view.dom_text, '帖子正文：活动时间为2026年11月10日。')
        region_ids = [region.region_id for region in view.regions]
        self.assertIn('viewport:0', region_ids)
        self.assertIn('viewport:1', region_ids)
        self.assertIn('comments', region_ids)
        self.assertEqual(view.scroll_height, 1850)
        comments = view.region('comments')
        self.assertEqual(comments.role, 'comments')
        self.assertIn('玩家评论一', comments.text)
        self.assertIn('玩家评论二', comments.text)

    def test_extract_dom_body_and_frame_ocr(self):
        adapter = LegacyBrowseAdapter(self._capture())
        view = adapter.open(make_goal())
        body = adapter.extract(view, model.Region(region_id=model.DOM_BODY_REGION,
                                                  role='section', bbox=(0, 0, 1440, 1000)),
                               'dom')
        self.assertIn('活动时间为2026年11月10日', body.text)
        dom_frame = adapter.extract(view, view.region('viewport:0'), 'dom')
        self.assertEqual(dom_frame.text, '')  # 截图 OCR 文本只能作为视觉文本
        ocr_frame = adapter.extract(view, view.region('viewport:0'), 'ocr')
        self.assertIn('第一屏 OCR 文本', ocr_frame.text)
        self.assertEqual(ocr_frame.cost.captures, 1)

    def test_blocked_capture_maps_to_blocked_page_and_extraction(self):
        adapter = LegacyBrowseAdapter(self._capture(status='blocked'))
        view = adapter.to_page_view()
        self.assertEqual(view.blocked_reason, 'access_denied')
        result = adapter.extract(view, view.region('viewport:0'), 'ocr')
        self.assertTrue(result.blocked)
        self.assertEqual(result.text, '')

    def test_interact_is_not_supported(self):
        adapter = LegacyBrowseAdapter(self._capture())
        view = adapter.open(make_goal())
        with self.assertRaises(NotImplementedError):
            adapter.interact(view, view.region('viewport:0'))


if __name__ == '__main__':
    unittest.main()
