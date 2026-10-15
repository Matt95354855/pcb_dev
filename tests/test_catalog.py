import unittest

from pcb_dev.catalog import CatalogError, ComponentCatalog, Part
from pcb_dev.fabrication import ir_drop_v, track_width_mm


def part(identifier="r1", **extra):
    return Part.from_dict({"id": identifier, "description": "resistor", "category": "resistor",
                           "footprint": "R_0603", "parameters": {"power_w": 0.1}, **extra})


class CatalogTests(unittest.TestCase):
    def test_selection_never_invents_missing_rating_or_stock(self):
        catalog = ComponentCatalog([part(status="approved"), part("r2", status="banned")])
        self.assertEqual(len(catalog.select("resistor", minimum={"power_w": 0.05})), 1)
        self.assertEqual(catalog.select("resistor", minimum={"voltage_v": 3.3}), [])
        self.assertEqual(catalog.select("resistor", require_stock=True), [])

    def test_review_parts_excluded_by_default(self):
        catalog = ComponentCatalog([part()])
        self.assertEqual(catalog.select("resistor"), [])
        self.assertEqual(len(catalog.select("resistor", approved_only=False)), 1)

    def test_invalid_data(self):
        for extra in ({"parameters": {"power": float("nan")}}, {"stock": True},
                      {"status": "good"}, {"unit_price": -1}, {"injected": "code"}):
            with self.subTest(extra=extra), self.assertRaises(CatalogError):
                part(**extra)
        with self.assertRaises(CatalogError):
            ComponentCatalog([part(), part()])
        with self.assertRaises(CatalogError):
            ComponentCatalog([part(alternatives=["missing"])])

    def test_stable_catalog_hash(self):
        self.assertEqual(ComponentCatalog([part(), part("r2")]).fingerprint(),
                         ComponentCatalog([part("r2"), part()]).fingerprint())

    def test_physical_estimates(self):
        self.assertGreater(track_width_mm(2), track_width_mm(1))
        self.assertLess(track_width_mm(1, copper_oz=2), track_width_mm(1))
        self.assertAlmostEqual(ir_drop_v(1, 100, 1), 0.048)
        for invalid in (float("nan"), True, -1):
            with self.assertRaises(ValueError):
                track_width_mm(invalid)
