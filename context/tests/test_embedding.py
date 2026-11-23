"""Pluggable semantic matching tests (fake embedder, no model/network)."""
import unittest

from context import embedding, event_match


def fake_multilingual_embed(texts):
    vectors = []
    for text in texts:
        if '第12赛季' in text or 'huge update' in text or 'season 12' in text.lower():
            vectors.append([1.0, 0.0, 0.2])
        elif '反作弊' in text or 'anti-cheat' in text.lower():
            vectors.append([0.0, 1.0, 0.2])
        else:
            vectors.append([0.2, 0.2, 1.0])
    return vectors


class SemanticMatchTests(unittest.TestCase):
    def test_cross_language_same_event_is_candidate(self):
        events = [
            {'event_id': 'zh', 'title': '《THE FINALS》第12赛季上线'},
            {'event_id': 'en', 'title': 'THE FINALS gets a huge update'},
            {'event_id': 'other', 'title': 'THE FINALS anti-cheat update'},
        ]
        candidates = embedding.semantic_same_event_candidates(
            events, [], fake_multilingual_embed, threshold=0.82)
        pairs = {(item['left_event_id'], item['right_event_id']) for item in candidates}
        self.assertIn(('zh', 'en'), pairs)
        self.assertNotIn(('zh', 'other'), pairs)
        self.assertNotIn(('en', 'other'), pairs)
        self.assertTrue(all(item['auto_merge'] is False for item in candidates))

    def test_embedder_vector_count_mismatch_is_an_error(self):
        events = [{'event_id': 'a', 'title': 'x'}, {'event_id': 'b', 'title': 'y'}]
        with self.assertRaises(ValueError):
            embedding.semantic_same_event_candidates(events, [], lambda texts: [[1.0]])

    def test_cosine_handles_zero_and_mismatch(self):
        self.assertEqual(embedding.cosine([], [1.0]), 0.0)
        self.assertEqual(embedding.cosine([1.0, 0.0], [1.0]), 0.0)
        self.assertEqual(embedding.cosine([0.0, 0.0], [1.0, 0.0]), 0.0)


if __name__ == '__main__':
    unittest.main()
