import copy
from pathlib import Path
import tempfile
import unittest

from pcb_dev.model import (
    Design, ModelError, block_fingerprint, canonical_json, dump_design,
    freeze_fingerprint, load_design, walk_blocks, walk_components, walk_ports,
)


def fixture():
    return {
        "schema_version": 1, "name": "Électronique", "version": "0.1.0",
        "board": {"width_mm": 30, "height_mm": 20, "layers": 2, "manufacturer": "jlcpcb"},
        "root": {
            "id": "board", "parameters": {"voltage": 3.3},
            "children": [
                {"id": "supply", "ports": [
                    {"id": "vout", "kind": "power", "direction": "output", "max_current_ma": 500}
                ]},
                {"id": "logic", "depends_on": ["board.supply"], "ports": [
                    {"id": "vcc", "kind": "power", "direction": "input", "current_draw_ma": 100}
                ], "components": [
                    {"id": "resistor", "reference": "R1", "value": "1k",
                     "footprint": "Resistor_SMD:R_0603_1608Metric", "pins": {"1": "VCC", "2": "GND"},
                     "position": {"x_mm": 5, "y_mm": 6}}
                ]},
            ],
        },
        "connections": [{"source": "board.supply.vout", "target": "board.logic.vcc", "net": "VCC"}],
        "metadata": {"author": "Test", "notes": [True, None, 1.5]},
    }


class ModelTests(unittest.TestCase):
    def test_defaults_and_roundtrip(self):
        design = Design.from_dict(fixture())
        self.assertEqual(design.root.version, "0.1.0")
        self.assertEqual(design.root.children[0].ports[0].current_draw_ma, 0)
        self.assertTrue(design.root.children[1].components[0].fitted)
        self.assertEqual(design.root.children[1].components[0].position.side, "top")
        self.assertEqual(Design.from_dict(design.to_dict()).to_dict(), design.to_dict())

    def test_walkers_return_absolute_paths_in_source_order(self):
        design = Design.from_dict(fixture())
        self.assertEqual([p for p, _ in design.walk_blocks()], ["board", "board.supply", "board.logic"])
        self.assertEqual([p for p, _ in walk_blocks(design.root)], ["board", "board.supply", "board.logic"])
        self.assertEqual([p for p, _ in walk_ports(design)], ["board.supply.vout", "board.logic.vcc"])
        self.assertEqual([p for p, _ in walk_components(design)], ["board.logic.resistor"])

    def test_input_and_output_do_not_alias_mutable_parameters(self):
        data = fixture()
        data["root"]["parameters"]["nested"] = {"array": [1, 2]}
        design = Design.from_dict(data)
        data["root"]["parameters"]["nested"]["array"].append(3)
        output = design.to_dict()
        output["root"]["parameters"]["nested"]["array"].append(4)
        self.assertEqual(design.root.parameters["nested"]["array"], [1, 2])

    def test_deterministic_utf8_io_and_hash(self):
        design = Design.from_dict(fixture())
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "one.json"
            second = Path(temp) / "two.json"
            dump_design(design, first)
            dump_design(load_design(first), second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertIn("Électronique", first.read_text(encoding="utf-8"))
            self.assertTrue(first.read_bytes().endswith(b"\n"))
        self.assertEqual(len(design.fingerprint()), 64)
        reordered = dict(reversed(list(fixture().items())))
        self.assertEqual(design.fingerprint(), Design.from_dict(reordered).fingerprint())
        design.root.description = "changed"
        self.assertNotEqual(design.fingerprint(), Design.from_dict(fixture()).fingerprint())

    def test_canonical_json_rejects_nonfinite_numbers(self):
        with self.assertRaises(ModelError):
            canonical_json({"value": float("nan")})

    def test_schema_version_is_integer_one(self):
        for value in (True, 1.0, "1", 0, 2, None):
            with self.subTest(value=value):
                data = fixture()
                data["schema_version"] = value
                with self.assertRaisesRegex(ModelError, "schema_version"):
                    Design.from_dict(data)

    def test_unknown_fields_are_rejected_at_every_schema_level(self):
        accessors = [
            lambda d: d, lambda d: d["board"], lambda d: d["root"],
            lambda d: d["root"]["children"][0]["ports"][0],
            lambda d: d["root"]["children"][1]["components"][0],
            lambda d: d["root"]["children"][1]["components"][0]["position"],
            lambda d: d["connections"][0],
        ]
        for accessor in accessors:
            data = fixture()
            accessor(data)["misspelled"] = 1
            with self.subTest(data=data), self.assertRaisesRegex(ModelError, "unknown field"):
                Design.from_dict(data)
        data = fixture()
        data["root"]["constraints"] = [{"expression": "voltage > 0", "message": "voltage", "extra": 1}]
        with self.assertRaisesRegex(ModelError, "unknown field"):
            Design.from_dict(data)

    def test_booleans_and_nonfinite_values_are_not_numbers(self):
        for value in (True, False, "30", None, float("nan"), float("inf"), -float("inf"), 10 ** 1000):
            with self.subTest(value=str(value)[:20]):
                data = fixture()
                data["board"]["width_mm"] = value
                with self.assertRaises(ModelError):
                    Design.from_dict(data)
        for value in (float("nan"), float("inf")):
            data = fixture()
            data["metadata"]["nested"] = {"bad": [value]}
            with self.assertRaisesRegex(ModelError, "finite"):
                Design.from_dict(data)

    def test_negative_dimensions_currents_and_invalid_layers(self):
        for key, value in (("width_mm", 0), ("height_mm", -1), ("layers", 3), ("layers", True), ("layers", 2.0)):
            data = fixture()
            data["board"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ModelError):
                Design.from_dict(data)
        data = fixture()
        data["root"]["children"][0]["ports"][0]["max_current_ma"] = -1
        with self.assertRaises(ModelError):
            Design.from_dict(data)

    def test_port_bounds_and_enums(self):
        for fields in ({"voltage_min": 5, "voltage_max": 3.3}, {"kind": "serial"},
                       {"direction": "both"}, {"required": 1}, {"impedance_ohm": 0}):
            data = fixture()
            data["root"]["children"][0]["ports"][0].update(fields)
            with self.subTest(fields=fields), self.assertRaises(ModelError):
                Design.from_dict(data)

    def test_defaults_do_not_replace_invalid_supplied_values(self):
        for field_name, bad_value in (("ports", None), ("components", {}), ("children", False),
                                      ("parameters", []), ("frozen", 0), ("version", None),
                                      ("resources", None), ("variants", []), ("description", 1)):
            data = fixture()
            data["root"][field_name] = bad_value
            with self.subTest(field_name=field_name), self.assertRaises(ModelError):
                Design.from_dict(data)

    def test_duplicate_paths_and_global_references(self):
        data = fixture()
        data["root"]["children"].append({"id": "logic"})
        with self.assertRaisesRegex(ModelError, "duplicate member"):
            Design.from_dict(data)
        data = fixture()
        data["root"]["ports"] = [{"id": "logic", "kind": "digital", "direction": "input"}]
        with self.assertRaisesRegex(ModelError, "duplicate member"):
            Design.from_dict(data)
        data = fixture()
        clone = copy.deepcopy(data["root"]["children"][1]["components"][0])
        clone["id"] = "other"
        clone["reference"] = "r1"
        data["root"]["children"][0]["components"] = [clone]
        with self.assertRaisesRegex(ModelError, "duplicate component reference"):
            Design.from_dict(data)

    def test_missing_endpoints_and_duplicate_connections(self):
        data = fixture()
        data["connections"][0]["target"] = "board.logic.missing"
        with self.assertRaisesRegex(ModelError, "unknown port"):
            Design.from_dict(data)
        data = fixture()
        duplicate = copy.deepcopy(data["connections"][0])
        duplicate["source"], duplicate["target"] = duplicate["target"], duplicate["source"]
        data["connections"].append(duplicate)
        with self.assertRaisesRegex(ModelError, "duplicate connection"):
            Design.from_dict(data)

    def test_identifiers_forbid_dot_whitespace_and_unicode(self):
        for identifier in ("a.b", "bad id", "", "électronique", "a/b", "a\\b"):
            data = fixture()
            data["root"]["id"] = identifier
            with self.subTest(identifier=identifier), self.assertRaises(ModelError):
                Design.from_dict(data)

    def test_equation_targets_must_be_usable_names(self):
        for target in ("a-b", "a.b", "class", "__class__"):
            data = fixture()
            data["root"]["equations"] = {target: "1"}
            with self.subTest(target=target), self.assertRaises(ModelError):
                Design.from_dict(data)

    def test_missing_position_is_permitted_but_invalid_position_is_not(self):
        data = fixture()
        del data["root"]["children"][1]["components"][0]["position"]
        self.assertIsNone(Design.from_dict(data).root.children[1].components[0].position)
        data["root"]["children"][1]["components"][0]["position"] = {"x_mm": 0}
        with self.assertRaises(ModelError):
            Design.from_dict(data)

    def test_malformed_json_duplicate_keys_and_constants(self):
        for content in ('{"root": {}, "root": {}}', '{"value": NaN}', '{"value": Infinity}', '{', '\ud800'):
            with self.subTest(content=repr(content)), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "bad.json"
                path.write_bytes(content.encode("utf-8", errors="surrogatepass"))
                with self.assertRaises(ModelError):
                    load_design(path)

    def test_json_key_types_and_surrogates_are_rejected(self):
        data = fixture()
        data["metadata"][1] = "bad"
        with self.assertRaises(ModelError):
            Design.from_dict(data)
        data = fixture()
        data["name"] = "\ud800"
        with self.assertRaises(ModelError):
            Design.from_dict(data)

    def test_excessive_block_nesting_is_rejected(self):
        data = fixture()
        current = data["root"]
        for i in range(70):
            current["children"] = [{"id": f"level{i}"}]
            current = current["children"][0]
        data["connections"] = []
        with self.assertRaisesRegex(ModelError, "nesting"):
            Design.from_dict(data)

    def test_frozen_hash_ignores_freeze_metadata(self):
        design = Design.from_dict(fixture())
        block = design.root.children[1]
        intrinsic = block_fingerprint(block)
        digest = freeze_fingerprint(design, "board.logic")
        block.frozen = True
        block.frozen_hash = digest
        self.assertEqual(block_fingerprint(block), intrinsic)
        self.assertEqual(freeze_fingerprint(design, "board.logic"), digest)
        self.assertEqual(Design.from_dict(design.to_dict()).root.children[1].frozen_hash, digest)

    def test_frozen_hash_detects_upstream_and_inherited_changes(self):
        design = Design.from_dict(fixture())
        digest = freeze_fingerprint(design, "board.logic")
        design.root.children[0].ports[0].max_current_ma = 600
        self.assertNotEqual(freeze_fingerprint(design, "board.logic"), digest)
        design = Design.from_dict(fixture())
        design.root.parameters["voltage"] = 5
        self.assertNotEqual(freeze_fingerprint(design, "board.logic"), digest)

    def test_frozen_hash_ignores_unrelated_changes_and_detects_connection_changes(self):
        data = fixture()
        data["root"]["children"].append({"id": "unrelated"})
        design = Design.from_dict(data)
        digest = freeze_fingerprint(design, "board.logic")
        design.root.children[2].description = "unrelated edit"
        self.assertEqual(freeze_fingerprint(design, "board.logic"), digest)
        design.connections[0].net = "VCC_CHANGED"
        self.assertNotEqual(freeze_fingerprint(design, "board.logic"), digest)
        with self.assertRaisesRegex(ModelError, "unknown block"):
            freeze_fingerprint(design, "board.missing")

    def test_dump_revalidates_direct_mutations(self):
        design = Design.from_dict(fixture())
        design.board.width_mm = True
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(ModelError):
            dump_design(design, Path(temp) / "invalid.json")

    def test_read_errors_are_model_errors(self):
        with self.assertRaises(ModelError):
            load_design("/path/that/does/not/exist/pcb-dev-test.json")


if __name__ == "__main__":
    unittest.main()
