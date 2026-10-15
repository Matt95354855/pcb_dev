"""Regressions found during an independent integration review."""

import tempfile
import unittest
from pathlib import Path

from pcb_dev.catalog import CatalogError, ComponentCatalog
from pcb_dev.fabrication import ir_drop_v, track_width_mm


class IntegrationReviewTests(unittest.TestCase):
    def test_physical_estimates_reject_nonfinite_calculated_results(self):
        with self.assertRaises(ValueError):
            track_width_mm(1e308)
        with self.assertRaises(ValueError):
            ir_drop_v(1e308, 1e308, 1, 35)

    def test_catalog_duplicate_keys_cannot_override_review_status(self):
        text = ('{"schema_version":1,"parts":[{"id":"r1","description":"resistor",'
                '"category":"resistor","footprint":"R_0603",'
                '"status":"review","status":"approved"}]}')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.json"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(CatalogError):
                ComponentCatalog.load(path)


if __name__ == "__main__":
    unittest.main()
