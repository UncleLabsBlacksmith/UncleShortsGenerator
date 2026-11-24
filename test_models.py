"""Price units, unknown prices, and price-aware model selection (offline)."""
import unittest
from decimal import Decimal
from model_picker import model_rates, price_text, filter_models


class ModelPriceTests(unittest.TestCase):
    def setUp(self):
        self.models = [
            {'id': 'a/text', 'pricing': {'prompt': '0.000001', 'completion': '0.000004'}},
            {'id': 'b/video', 'pricing_skus': {'per-video-second': '0.50', 'per-video-second-1080p': '0.75'}},
            {'id': 'a/zero', 'pricing': {'prompt': '0', 'completion': '0'}},
            {'id': 'c/unknown'},
            {'id': 'c/invalid', 'pricing': {'prompt': '-1', 'completion': 'NaN', 'audio': None}},
        ]

    def test_display_converts_tokens_and_preserves_video_skus(self):
        self.assertEqual(price_text(self.models[0]), '$1 input / 1M tokens; $4 output / 1M tokens')
        self.assertIn('$0.5 per-video-second', price_text(self.models[1]))
        self.assertIn('$0.75 per-video-second-1080p', price_text(self.models[1]))
        self.assertEqual(model_rates(self.models[0])['prompt'], Decimal('0.000001'))
        self.assertEqual(price_text(self.models[3]), price_text(self.models[4]))
        self.assertEqual(model_rates({'pricing': []}), {})

    def test_filters_keep_unknown_and_zero_prices_separate(self):
        self.assertEqual([m['id'] for m in filter_models(self.models, price='Zero listed')], ['a/zero'])
        self.assertEqual(len(filter_models(self.models, price='Unknown')), 2)
        self.assertEqual([m['id'] for m in filter_models(self.models, query='TEXT', provider='a', price='Paid')], ['a/text'])

    def test_price_cap_uses_selected_unit_and_matches_display_scale(self):
        self.assertEqual([m['id'] for m in filter_models(self.models, unit='prompt', maximum='1')], ['a/text', 'a/zero'])
        self.assertEqual([m['id'] for m in filter_models(self.models, unit='prompt', maximum='0.5')], ['a/zero'])
        self.assertEqual(filter_models(self.models, unit='per-video-second', maximum='0.4'), [])
        self.assertEqual(filter_models(self.models, unit='prompt', maximum='NaN'), [])
        self.assertEqual(filter_models(self.models, maximum='1'), [])


if __name__ == '__main__':
    unittest.main()
