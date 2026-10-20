"""Per-model calibration for the OpenRouter estimate (#514).

OpenRouter's model-level price is the top provider's, which is usually the
cheapest provider - a deployment that wants the estimate to track the vendor's
official price can scale each model in usage/pricing-markup.json without
rewriting the policy history.

    python tests/_test_pricing_markup.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_pricing

DOC = {
    "meta": {"usd_cny": 7.1},
    "models": {"m": {
        "unit": 1000000,
        "currency": "USD",
        "flat": {"input_cache_miss": 0.6, "input_cache_hit": 0.06, "output": 2.4},
    }},
}
ROW = {"model": "m", "prompt_tokens": 1000000, "cached_tokens": 0,
       "completion_tokens": 1000000, "at": 0}


class PricingMarkupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="wb-pricing-markup-")
        wb_pricing.set_data_dir(self.directory)

    def tearDown(self):
        wb_pricing.set_data_dir(None)
        shutil.rmtree(self.directory, ignore_errors=True)

    def write(self, payload):
        with open(os.path.join(self.directory, "pricing-markup.json"),
                  "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def test_without_a_file_the_estimate_is_unchanged(self):
        self.assertEqual(wb_pricing.markup_for("m"), 1.0)
        cost = wb_pricing.compute_row(ROW, pricing=DOC)
        self.assertAlmostEqual(cost["cny"], 3.0 * 7.1, places=6)

    def test_a_factor_scales_the_estimate(self):
        self.write({"m": 2.5})
        self.assertEqual(wb_pricing.markup_for("m"), 2.5)
        cost = wb_pricing.compute_row(ROW, pricing=DOC)
        self.assertAlmostEqual(cost["cny"], 3.0 * 7.1 * 2.5, places=6)

    def test_an_unknown_model_keeps_the_default(self):
        self.write({"m": 2.5})
        self.assertEqual(wb_pricing.markup_for("other"), 1.0)

    def test_invalid_values_are_ignored(self):
        self.write({"m": "not a number", "n": -2, "o": 0})
        self.assertEqual(wb_pricing.markup_for("m"), 1.0)
        self.assertEqual(wb_pricing.markup_for("n"), 1.0)
        self.assertEqual(wb_pricing.markup_for("o"), 1.0)

    def test_a_corrupt_file_falls_back_to_one(self):
        with open(os.path.join(self.directory, "pricing-markup.json"),
                  "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(wb_pricing.markup_for("m"), 1.0)


if __name__ == "__main__":
    unittest.main()
