"""Incremental delivery and review-state tests (offline)."""
import csv
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from context import delivery, delivery_review
from scripts.v4_daily_delivery import run_daily, select_candidates
from scripts.v4_delivery_pack import build_pack

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 24, 12, tzinfo=CST)


def final_payload():
    return {'parsed': {'headline': '标题', 'one_line': '一句话',
            'facts': [{'text': '11月24日上线', 'quote': '计划于11月24日上线'}],
            'unknowns': ['入口待确认'],
            'intelligence': {'summary': '公开事件摘要', 'why_taptap': '判断：可讨论'},
            'material': {'title': '素材', 'channel': '社区动态', 'draft': '讨论草案',
                         'usage_boundary': '不补写未知信息'}},
            'quote_checks': {'facts_supported': 1, 'facts_total': 1}}


def make_pack(root: Path, event_id: str, moment_id: str, body: str = '计划于11月24日上线。' * 20):
    folder = root / event_id
    folder.mkdir(parents=True)
    url = 'https://www.taptap.cn/moment/' + moment_id
    (folder / 'evidence.json').write_text(json.dumps({
        'event': {'event_id': event_id, 'title': event_id, 'url': url,
                  'published_at': '2026-11-24T10:00:00+08:00'},
        'article': {'body': body, 'url': url, 'title': event_id}}, ensure_ascii=False), encoding='utf-8')
    (folder / 'final.json').write_text(json.dumps(final_payload(), ensure_ascii=False), encoding='utf-8')


def make_csv(path: Path):
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=['moment_id', 'title', 'summary', 'publish_time'])
        writer.writeheader()
        writer.writerow({'moment_id': '100', 'title': '新事件', 'summary': '公开事件摘要' * 10,
                         'publish_time': str(int(NOW.timestamp()) - 3600)})
        writer.writerow({'moment_id': '99', 'title': '旧事件', 'summary': '公开事件摘要' * 10,
                         'publish_time': str(int((NOW - timedelta(days=20)).timestamp()))})
        writer.writerow({'moment_id': '98', 'title': '短帖', 'summary': '太短',
                         'publish_time': str(int(NOW.timestamp()))})


class DailyDeliveryTests(unittest.TestCase):
    def test_selection_excludes_old_short_and_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); csv_path = root / 'posts.csv'; make_csv(csv_path)
            state = root / 'state.json'
            state.write_text(json.dumps({'delivered': {'100': {}}, 'failed': {}}), encoding='utf-8')
            candidates, _ = select_candidates(csv_path, root / 'out', state, now=NOW)
            self.assertEqual(candidates, [])

    def test_daily_is_idempotent_and_preserves_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); packs = root / 'packs'; make_pack(packs, 'event100', '100')
            make_csv(root / 'posts.csv')
            out = root / 'out'; state = root / 'state.json'
            first = run_daily(root / 'posts.csv', packs, out, state, now=NOW)
            self.assertEqual(len(first['delivered']), 1)
            package = json.loads((out / 'event100' / 'delivery.json').read_text(encoding='utf-8'))
            self.assertEqual(package['status'], 'needs_review')
            delivery_review.record_review(out, 'event100', 'approved', '复核人', '已核查来源')
            before = (out / 'event100' / 'delivery.json').read_bytes()
            second = run_daily(root / 'posts.csv', packs, out, state, now=NOW)
            self.assertEqual(second['delivered'], [])
            self.assertEqual(second['skipped'][0]['moment_id'], '100')
            self.assertEqual(second['skipped'][0]['reason'], 'already_delivered')
            self.assertEqual((out / 'event100' / 'delivery.json').read_bytes(), before)
            index = json.loads((out / 'index.json').read_text(encoding='utf-8'))
            self.assertEqual(index['deliveries'][0]['review_status'], 'approved')

    def test_missing_research_pack_is_recorded_and_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); make_csv(root / 'posts.csv')
            out = root / 'out'; state = root / 'state.json'; packs = root / 'packs'; packs.mkdir()
            first = run_daily(root / 'posts.csv', packs, out, state, now=NOW)
            self.assertEqual(first['failed'][0]['error'], 'research pack missing')
            ledger = json.loads(state.read_text(encoding='utf-8'))
            self.assertEqual(ledger['failed']['100']['attempts'], 1)
            make_pack(packs, 'event100', '100')
            second = run_daily(root / 'posts.csv', packs, out, state, now=NOW)
            self.assertEqual(len(second['delivered']), 1)

    def test_duplicate_moment_rows_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); csv_path = root / 'posts.csv'; make_csv(csv_path)
            with csv_path.open('a', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['moment_id', 'title', 'summary', 'publish_time'])
                writer.writerow({'moment_id': '100', 'title': '重复事件', 'summary': '公开事件摘要' * 10,
                                 'publish_time': str(int(NOW.timestamp()) - 7200)})
            candidates, _ = select_candidates(csv_path, root / 'out', root / 'state.json', now=NOW)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]['title'], '新事件')

    def test_corrupt_pack_does_not_block_other_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); packs = root / 'packs'
            make_pack(packs, 'event100', '100'); make_pack(packs, 'event101', '101')
            (packs / 'event101' / 'final.json').write_text('{bad json', encoding='utf-8')
            with (root / 'posts.csv').open('w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['moment_id', 'title', 'summary', 'publish_time'])
                writer.writeheader()
                for mid in ('100', '101'):
                    writer.writerow({'moment_id': mid, 'title': '事件' + mid,
                                     'summary': '公开事件摘要' * 10,
                                     'publish_time': str(int(NOW.timestamp()) - 3600)})
            report = run_daily(root / 'posts.csv', packs, root / 'out', root / 'state.json', now=NOW)
            self.assertEqual([item['event_id'] for item in report['delivered']], ['event100'])
            self.assertEqual(report['failed'][0]['moment_id'], '101')
            self.assertIn('JSONDecodeError', report['failed'][0]['error'])
            self.assertTrue((root / 'out' / 'event100' / 'delivery.json').is_file())
            self.assertFalse((root / 'out' / '.daily-staging').exists())

    def test_missing_output_is_rebuilt_from_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); packs = root / 'packs'; make_pack(packs, 'event100', '100')
            make_csv(root / 'posts.csv'); out = root / 'out'; state = root / 'state.json'
            run_daily(root / 'posts.csv', packs, out, state, now=NOW)
            (out / 'event100' / 'delivery.json').unlink()
            report = run_daily(root / 'posts.csv', packs, out, state, now=NOW)
            self.assertEqual(len(report['delivered']), 1)
            self.assertTrue((out / 'event100' / 'delivery.json').is_file())

    def test_failed_checks_are_retried_not_promoted_to_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); packs = root / 'packs'; make_pack(packs, 'event100', '100')
            payload = final_payload(); payload['parsed']['material'] = {}
            (packs / 'event100' / 'final.json').write_text(json.dumps(payload), encoding='utf-8')
            make_csv(root / 'posts.csv'); out = root / 'out'; state = root / 'state.json'
            first = run_daily(root / 'posts.csv', packs, out, state, now=NOW)
            second = run_daily(root / 'posts.csv', packs, out, state, now=NOW)
            self.assertEqual(first['delivered'], [])
            self.assertEqual(second['delivered'], [])
            self.assertEqual(second['failed'][0]['moment_id'], '100')
            ledger = json.loads(state.read_text(encoding='utf-8'))
            self.assertNotIn('100', ledger['delivered'])
            self.assertEqual(ledger['failed']['100']['attempts'], 2)

    def test_missing_newest_pack_does_not_starve_older_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); packs = root / 'packs'; make_pack(packs, 'event101', '101')
            with (root / 'posts.csv').open('w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['moment_id', 'title', 'summary', 'publish_time'])
                writer.writeheader()
                for mid, seconds in (('100', 3600), ('101', 7200)):
                    writer.writerow({'moment_id': mid, 'title': '事件' + mid,
                                     'summary': '公开事件摘要' * 10,
                                     'publish_time': str(int(NOW.timestamp()) - seconds)})
            report = run_daily(root / 'posts.csv', packs, root / 'out', root / 'state.json',
                               now=NOW, limit=1)
            self.assertEqual([item['moment_id'] for item in report['delivered']], ['101'])
            self.assertEqual(report['failed'][0]['moment_id'], '100')

    def test_approved_legacy_package_without_hash_is_invalidated(self):
        package = delivery.build_delivery({'event_id': 'e', 'title': '事件'},
                                          {'article': {}}, final_payload())
        package['review'] = {'status': 'approved', 'reviewer': '旧审核人',
                             'note': '旧审核', 'risks': []}
        previous = copy.deepcopy(package)
        package['material']['draft'] = '内容已变化'
        delivery_review.review_package(package, previous)
        self.assertEqual(package['status'], 'needs_review')
        self.assertEqual(package['review_history'][-1]['reviewer'], 'system')

    def test_evidence_or_check_change_invalidates_approval(self):
        for mutate in ('evidence', 'checks'):
            with self.subTest(mutate=mutate):
                package = delivery.build_delivery({'event_id': 'e', 'title': '事件'},
                                                  {'article': {}}, final_payload())
                package['review']['status'] = 'approved'
                package['review']['content_hash'] = delivery_review._content_hash(package)
                previous = copy.deepcopy(package)
                if mutate == 'evidence':
                    package['evidence']['source_url'] = 'https://example.com/changed'
                else:
                    package['checks']['quote_supported'] = 0
                delivery_review.review_package(package, previous)
                self.assertEqual(package['status'], 'needs_review')

    def test_invalid_windows_are_rejected(self):
        with self.assertRaises(ValueError):
            select_candidates('/not-used', '/not-used', '/not-used', days=0)

    def test_incremental_pack_preserves_existing_index_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / 'out'; out.mkdir()
            (out / 'index.json').write_text(json.dumps({'deliveries': [
                {'event_id': 'old', 'title': '旧事件', 'status': 'approved', 'checks_passed': True,
                 'preview': 'old/preview.html'}]}), encoding='utf-8')
            old = out / 'old'; old.mkdir()
            package = delivery.build_delivery({'event_id': 'old', 'title': '旧'}, {'article': {}}, final_payload())
            package['status'] = 'approved'; package['review'] = {'status': 'approved', 'risks': []}
            (old / 'delivery.json').write_text(json.dumps(package, ensure_ascii=False), encoding='utf-8')
            packs = root / 'packs'; make_pack(packs, 'new', '101')
            (packs / 'summary.json').write_text(json.dumps({'events': [{'event_id': 'new'}]}), encoding='utf-8')
            build_pack(packs, out)
            index = json.loads((out / 'index.json').read_text(encoding='utf-8'))
            self.assertEqual([row['event_id'] for row in index['deliveries']], ['old', 'new'])

    def test_content_change_invalidates_approval(self):
        package = delivery.build_delivery({'event_id': 'e', 'title': '事件'},
                                          {'article': {}}, final_payload())
        package['review']['status'] = 'approved'
        package['review']['content_hash'] = delivery_review._content_hash(package)
        package['material']['draft'] = '人工修改后的新内容'
        delivery_review.review_package(package, package)
        self.assertEqual(package['status'], 'needs_review')
        self.assertEqual(package['review_history'][-1]['reviewer'], 'system')

    def test_failed_checks_cannot_be_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / 'out'; event = out / 'e'; event.mkdir(parents=True)
            package = delivery.build_delivery({'event_id': 'e', 'title': '事件'},
                                              {'article': {}}, final_payload())
            package['checks']['passed'] = False
            (event / 'delivery.json').write_text(json.dumps(package), encoding='utf-8')
            with self.assertRaises(ValueError):
                delivery_review.record_review(out, 'e', 'approved', '复核人', '理由')

    def test_review_requires_reviewer_and_note(self):
        with self.assertRaises(ValueError):
            delivery_review.record_review('/not-used', 'e', 'approved', '', '')


if __name__ == '__main__':
    unittest.main()
