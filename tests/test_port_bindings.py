"""Structural port-to-pin contracts and legacy fingerprint compatibility."""

import copy
import unittest

from pcb_dev.model import Design, ModelError, PinBinding, block_fingerprint, freeze_fingerprint
from pcb_dev.packages import create_package, instantiate_package
from pcb_dev.transactions import TransactionError, apply_patch


def legacy_data():
    return {"schema_version": 1, "name": "legacy-port-hash", "version": "0.2.0",
            "board": {"width_mm": 20, "height_mm": 10}, "root": {
                "id": "board", "parameters": {"vin": 5},
                "ports": [{"id": "vcc", "kind": "power", "direction": "input", "required": False}],
                "components": [{"id": "u1", "reference": "U1", "value": "demo", "footprint": "Test:Demo",
                                "pins": {"1": "VCC", "2": "GND"}}]}}


def mapped_data():
    data = legacy_data()
    data["root"]["ports"][0].update(net="VCC", pin_bindings=[{"component": "u1", "pin": "1"}])
    return data


class PortBindingModelTests(unittest.TestCase):
    def test_legacy_canonical_design_block_and_freeze_hashes_are_unchanged(self):
        # Captured from the unmodified 0.2 model before adding these fields.
        design = Design.from_dict(legacy_data())
        self.assertEqual(design.fingerprint(), "5a1999ca229acc7e694e48c127c2df633052bc3b5c63261cf62a6485fe6338fd")
        self.assertEqual(block_fingerprint(design.root), "a32a33fa204d25734e423c6ca6ba470d8bee6fa7982afc1a601e996621245f82")
        self.assertEqual(freeze_fingerprint(design, "board"), "e9880fbc946d685df1ee7001913c574c62d91b885b66bb5dd017fc61cd40a0f2")

    def test_empty_new_fields_are_omitted_and_explicit_defaults_remain_canonical(self):
        data = legacy_data()
        data["root"]["ports"][0].update(net=None, pin_bindings=[])
        design = Design.from_dict(data)
        port = design.to_dict()["root"]["ports"][0]
        self.assertNotIn("net", port)
        self.assertNotIn("pin_bindings", port)
        self.assertEqual(design.fingerprint(), Design.from_dict(legacy_data()).fingerprint())

    def test_explicit_binding_is_typed_and_survives_roundtrip(self):
        design = Design.from_dict(mapped_data())
        port = design.root.ports[0]
        self.assertEqual(port.net, "VCC")
        self.assertEqual(port.pin_bindings, [PinBinding("u1", "1")])
        serialized = design.to_dict()
        self.assertEqual(serialized["root"]["ports"][0]["pin_bindings"], [{"component": "u1", "pin": "1"}])
        self.assertEqual(Design.from_dict(serialized).to_dict(), serialized)

    def test_serialized_bindings_do_not_alias_the_input_or_model(self):
        data = mapped_data()
        design = Design.from_dict(data)
        data["root"]["ports"][0]["pin_bindings"][0]["pin"] = "2"
        serialized = design.to_dict()
        serialized["root"]["ports"][0]["pin_bindings"][0]["component"] = "other"
        self.assertEqual(design.root.ports[0].pin_bindings, [PinBinding("u1", "1")])

    def test_abstract_port_can_declare_intended_net_without_physical_targets(self):
        data = legacy_data()
        data["root"]["ports"][0]["net"] = "VCC"
        port = Design.from_dict(data).root.ports[0]
        self.assertEqual(port.net, "VCC")
        self.assertEqual(port.pin_bindings, [])

    def test_nonempty_bindings_require_explicit_intended_net(self):
        for value in (None, "omitted"):
            data = mapped_data()
            if value == "omitted":
                del data["root"]["ports"][0]["net"]
            else:
                data["root"]["ports"][0]["net"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ModelError, "explicit port net"):
                Design.from_dict(data)

    def test_invalid_net_and_binding_container_types_are_rejected(self):
        for value in ("", "  ", True, 1, [], {}):
            data = mapped_data()
            data["root"]["ports"][0]["net"] = value
            with self.subTest(net=value), self.assertRaises(ModelError):
                Design.from_dict(data)
        for value in (None, {}, "u1.1", True):
            data = mapped_data()
            data["root"]["ports"][0]["pin_bindings"] = value
            with self.subTest(bindings=value), self.assertRaises(ModelError):
                Design.from_dict(data)

    def test_pin_binding_object_has_strict_fields_and_string_scalar_types(self):
        for binding in ({"component": "u1"}, {"pin": "1"}, {"component": "u1", "pin": "1", "net": "VCC"},
                        {"component_id": "u1", "pin": "1"}, {"component": "u1", "pin": 1},
                        {"component": "u1", "pin": True}, {"component": "u1", "pin": ""},
                        {"component": None, "pin": "1"}, "u1.1", None):
            data = mapped_data()
            data["root"]["ports"][0]["pin_bindings"] = [binding]
            with self.subTest(binding=binding), self.assertRaises(ModelError):
                Design.from_dict(data)

    def test_targets_must_be_direct_components_of_the_port_block(self):
        for component in ("missing", "child.u1", "board.u1", "../u1", "u1/other"):
            data = mapped_data()
            data["root"]["ports"][0]["pin_bindings"][0]["component"] = component
            with self.subTest(component=component), self.assertRaises(ModelError):
                Design.from_dict(data)
        data = mapped_data()
        data["root"]["children"] = [{"id": "child", "components": data["root"].pop("components")}]
        with self.assertRaisesRegex(ModelError, "same block"):
            Design.from_dict(data)

    def test_target_pin_must_exist_in_the_component_net_map(self):
        data = mapped_data()
        data["root"]["ports"][0]["pin_bindings"][0]["pin"] = "99"
        with self.assertRaisesRegex(ModelError, "unknown component pin u1.99"):
            Design.from_dict(data)

    def test_alphanumeric_and_leading_zero_pin_numbers_are_exact_strings(self):
        data = mapped_data()
        data["root"]["components"][0]["pins"] = {"A1": "VCC", "01": "VCC"}
        data["root"]["ports"][0]["pin_bindings"] = [{"component": "u1", "pin": "A1"}, {"component": "u1", "pin": "01"}]
        self.assertEqual([binding.pin for binding in Design.from_dict(data).root.ports[0].pin_bindings], ["A1", "01"])

    def test_multiple_distinct_targets_per_port_are_supported(self):
        data = mapped_data()
        data["root"]["components"][0]["pins"]["3"] = "VCC"
        data["root"]["ports"][0]["pin_bindings"].append({"component": "u1", "pin": "3"})
        self.assertEqual(len(Design.from_dict(data).root.ports[0].pin_bindings), 2)

    def test_repeated_target_within_a_port_or_shared_between_ports_is_rejected(self):
        data = mapped_data()
        data["root"]["ports"][0]["pin_bindings"] *= 2
        with self.assertRaisesRegex(ModelError, "duplicate pin binding"):
            Design.from_dict(data)
        data = mapped_data()
        other = copy.deepcopy(data["root"]["ports"][0])
        other["id"] = "same_pin_again"
        data["root"]["ports"].append(other)
        with self.assertRaisesRegex(ModelError, "already bound by port 'vcc'"):
            Design.from_dict(data)

    def test_local_component_identifiers_can_repeat_in_different_blocks(self):
        data = mapped_data()
        child = copy.deepcopy(data["root"])
        child["id"] = "child"
        child["components"][0]["reference"] = "U2"
        data["root"]["children"] = [child]
        design = Design.from_dict(data)
        self.assertEqual(design.root.ports[0].pin_bindings, design.root.children[0].ports[0].pin_bindings)

    def test_mixed_net_and_dnp_are_preserved_for_electrical_checks(self):
        data = mapped_data()
        data["root"]["ports"][0]["net"] = "WRONG_INTENT"
        data["root"]["components"][0]["fitted"] = False
        design = Design.from_dict(data)
        self.assertEqual(design.root.ports[0].net, "WRONG_INTENT")
        self.assertFalse(design.root.components[0].fitted)
        self.assertEqual(design.root.components[0].pins["1"], "VCC")

    def test_recorded_legacy_freeze_remains_valid_after_loading(self):
        data = legacy_data()
        data["root"]["frozen"] = True
        data["root"]["frozen_hash"] = "e9880fbc946d685df1ee7001913c574c62d91b885b66bb5dd017fc61cd40a0f2"
        design = Design.from_dict(data)
        self.assertEqual(freeze_fingerprint(design, "board"), design.root.frozen_hash)

    def test_frozen_context_includes_unconnected_net_intentions(self):
        data = {"schema_version": 1, "name": "net-context", "version": "0.3.0",
                "board": {"width_mm": 20, "height_mm": 20}, "root": {"id": "board", "children": [
                    {"id": "first", "ports": [{"id": "rail", "kind": "power", "direction": "passive", "required": False, "net": "VCC"}]},
                    {"id": "second", "ports": [{"id": "rail", "kind": "power", "direction": "passive", "required": False, "net": "VCC"}]},
                    {"id": "isolated"},
                ]}}
        design = Design.from_dict(data)
        digest = freeze_fingerprint(design, "board.first")
        design.root.children[2].description = "isolated edit"
        self.assertEqual(freeze_fingerprint(design, "board.first"), digest)
        design.root.children[1].description = "shared intent edit"
        self.assertNotEqual(freeze_fingerprint(design, "board.first"), digest)
        design.root.children[0].frozen = True
        design.root.children[0].frozen_hash = freeze_fingerprint(design, "board.first")
        with self.assertRaisesRegex(TransactionError, "invalidate a frozen"):
            apply_patch(design, [{"op": "update_block", "block": "board.second", "changes": {"description": "upstream edit"}}], prevent_regressions=False)

    def test_package_instances_prefix_intent_and_physical_net_together(self):
        original = Design.from_dict(mapped_data())
        package = create_package(original, "board", name="mapped-block", version="0.3.0")
        target = Design.from_dict({"schema_version": 1, "name": "host", "version": "0.3.0",
                                   "board": {"width_mm": 20, "height_mm": 20}, "root": {"id": "host"}})
        instance = instantiate_package(target, "host", package, instance_id="first", reference_prefix="A_", net_prefix="A_")
        first = instance.root.children[0]
        self.assertEqual(first.ports[0].net, "A_VCC")
        self.assertEqual(first.components[0].pins["1"], "A_VCC")
        self.assertEqual(first.components[0].reference, "A_U1")
        self.assertEqual(first.ports[0].pin_bindings, [PinBinding("u1", "1")])
        second_design = instantiate_package(instance, "host", package, instance_id="second", reference_prefix="B_", net_prefix="B_")
        self.assertEqual(second_design.root.children[1].ports[0].net, "B_VCC")
        self.assertEqual(second_design.root.children[0].ports[0].net, "A_VCC")
        self.assertEqual(package.root.ports[0].net, "VCC")

    def test_package_shared_nets_and_nested_local_bindings_are_preserved(self):
        data = mapped_data()
        data["root"]["ports"][0]["net"] = "GND"
        data["root"]["ports"][0]["pin_bindings"][0]["pin"] = "2"
        nested = copy.deepcopy(data["root"])
        nested["id"] = "child"
        nested["components"][0]["reference"] = "U2"
        data["root"]["children"] = [nested]
        package = create_package(Design.from_dict(data), "board", name="nested", version="0.3.0")
        from pcb_dev.packages import instantiate_block
        instance, _ = instantiate_block(package, instance_id="instance", reference_prefix="A_", net_prefix="A_", shared_nets=["GND"])
        self.assertEqual(instance.ports[0].net, "GND")
        self.assertEqual(instance.children[0].ports[0].net, "GND")
        self.assertEqual(instance.children[0].ports[0].pin_bindings, [PinBinding("u1", "2")])
        self.assertEqual(instance.children[0].components[0].pins["2"], "GND")

    def test_assembly_variant_dnp_does_not_remove_intent_or_mapping(self):
        data = mapped_data()
        data["root"]["variants"] = {"unmounted": {"dnp": ["u1"]}}
        package = create_package(Design.from_dict(data), "board", name="variant", version="0.3.0")
        from pcb_dev.packages import instantiate_block
        instance, _ = instantiate_block(package, instance_id="unmounted", reference_prefix="D_", net_prefix="D_", variant="unmounted")
        self.assertFalse(instance.components[0].fitted)
        self.assertEqual(instance.ports[0].net, "D_VCC")
        self.assertEqual(instance.ports[0].pin_bindings, [PinBinding("u1", "1")])


if __name__ == "__main__":
    unittest.main()
