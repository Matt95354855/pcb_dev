import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_dev.expressions import resolve_parameters
from pcb_dev.model import Block, Board, Component, Connection, Design, ModelError, Port, freeze_fingerprint, walk_components
from pcb_dev.packages import (
    BlockPackage, PackageError, PackageLock, PackagePin, apply_variant, create_package,
    instantiate_block, instantiate_package, load_lock, load_locked_package,
    load_package, lock_package, write_lock, write_package,
)


def design():
    return Design("package", "0.1.0", Board(30, 20), Block(
        "board", parameters={"vin": 5}, ports=[Port("external", "power", "output", required=False)], children=[
            Block("channel", version="1.2.3", parameters={"divider": 2}, equations={"vout": "vin / divider"},
                  ports=[Port("vin", "power", "input", required=False)],
                  components=[Component("bias", "R1", "10k", "Resistor_SMD:R_0603_1608Metric", {"1": "VCC", "2": "GND"}, fitted=False)],
                  children=[
                      Block("supply", ports=[Port("out", "power", "output", required=False)],
                            components=[Component("decouple", "C1", "100n", "Capacitor_SMD:C_0603_1608Metric", {"1": "VCC", "2": "GND"})]),
                      Block("sensor", depends_on=["board.channel.supply"],
                            resources={"power_sequence": {"after": ["board.channel.supply"], "delay_ms": 10,
                                                         "required_delay_ms": 5}, "i2c": [{"bus": "I2C", "address": 0x40}]},
                            ports=[Port("in", "power", "input", required=False)],
                            components=[Component("chip", "U1", "sensor", "Package_SO:SOIC-8", {"1": "VCC", "2": "GND", "3": "SIGNAL"})]),
                  ],
                  variants={"debug": {"dnp": ["supply.decouple"], "fit": ["bias"], "parameters": {"divider": 4}}}),
        ]), [Connection("board.external", "board.channel.vin", "VCC"),
             Connection("board.channel.supply.out", "board.channel.sensor.in", "VCC")])


def package():
    return create_package(design(), "board.channel", name="sensor-channel")


class PackageTests(unittest.TestCase):
    def test_extraction_captures_boundary_values_internal_links_and_paths(self):
        block_package = package()
        self.assertEqual(block_package.name, "sensor-channel")
        self.assertEqual(block_package.version, "1.2.3")
        self.assertEqual(block_package.root.parameters["vin"], 5)
        self.assertEqual(block_package.root.parameters["vout"], 2.5)
        self.assertEqual(block_package.root.children[1].depends_on, ["channel.supply"])
        self.assertEqual(block_package.root.children[1].resources["power_sequence"]["after"], ["channel.supply"])
        self.assertEqual(len(block_package.connections), 1)
        self.assertEqual(block_package.connections[0].source, "channel.supply.out")

    def test_repeated_instances_remap_references_nets_dependencies_and_connections(self):
        original = design()
        block_package = package()
        first = instantiate_package(original, "board", block_package, instance_id="one", reference_prefix="CH1_", net_prefix="CH1_")
        second = instantiate_package(first, "board", block_package, instance_id="two", reference_prefix="CH2_", net_prefix="CH2_")
        references = [component.reference for _, component in walk_components(second)]
        self.assertEqual(len(references), len(set(references)))
        self.assertIn("CH1_U1", references)
        self.assertIn("CH2_U1", references)
        self.assertEqual(second.root.children[1].children[1].depends_on, ["board.one.supply"])
        self.assertEqual(second.root.children[2].children[1].resources["power_sequence"]["after"], ["board.two.supply"])
        self.assertEqual(second.root.children[1].children[1].resources["i2c"][0]["bus"], "CH1_I2C")
        self.assertEqual(second.root.children[1].children[1].components[0].pins["3"], "CH1_SIGNAL")
        self.assertEqual(second.connections[-1].source, "board.two.supply.out")
        self.assertEqual(second.connections[-1].net, "CH2_VCC")
        self.assertEqual(len(original.root.children), 1)
        self.assertEqual(block_package.root.children[1].depends_on, ["channel.supply"])

    def test_shared_nets_are_explicit_and_other_nets_remain_isolated(self):
        block, connections = instantiate_block(package(), instance_id="one", reference_prefix="A_", net_prefix="A_", shared_nets=["GND"])
        self.assertEqual(block.components[0].pins, {"1": "A_VCC", "2": "GND"})
        self.assertEqual(connections[0].net, "A_VCC")
        self.assertEqual(block.children[1].resources["i2c"][0]["bus"], "A_I2C")

    def test_instance_boundary_override_recalculates_equations(self):
        project = instantiate_package(design(), "board", package(), instance_id="one", reference_prefix="A_", net_prefix="A_", parameters={"vin": 3.3})
        self.assertAlmostEqual(resolve_parameters(project)["board.one"]["vout"], 1.65)
        self.assertEqual(resolve_parameters(project)["board.channel"]["vout"], 2.5)

    def test_variant_explicit_fit_dnp_and_parameters_do_not_mutate_package(self):
        block_package = package()
        block, _ = instantiate_block(block_package, instance_id="debug", reference_prefix="D_", net_prefix="D_", variant="debug")
        self.assertTrue(block.components[0].fitted)
        self.assertFalse(block.children[0].components[0].fitted)
        self.assertEqual(block.parameters["divider"], 4)
        self.assertFalse(block_package.root.components[0].fitted)
        self.assertTrue(block_package.root.children[0].components[0].fitted)

    def test_unknown_or_malformed_variants_are_rejected(self):
        with self.assertRaisesRegex(PackageError, "unknown variant"):
            apply_variant(package().root, "missing")
        definitions = [
            {"dnp": ["missing"]}, {"dnp": ["bias", "bias"]}, {"dnp": "bias"},
            {"dnp": ["bias"], "fit": ["bias"]}, {"execute": "code"}, {"parameters": None},
        ]
        for definition in definitions:
            data = package().to_dict()
            data["root"]["variants"] = {"bad": definition}
            with self.subTest(definition=definition), self.assertRaises(PackageError):
                BlockPackage.from_dict(data)

    def test_recognized_i2c_port_paths_remap_without_net_prefixing(self):
        source = design()
        source.root.children[0].children[1].resources["i2c"][0]["bus"] = "board.channel.sensor.in"
        block_package = create_package(source, "board.channel", name="channel")
        instance = instantiate_package(source, "board", block_package, instance_id="one", reference_prefix="A_", net_prefix="A_")
        self.assertEqual(instance.root.children[1].children[1].resources["i2c"][0]["bus"], "board.one.sensor.in")

    def test_external_dependency_cannot_be_packaged_implicitly(self):
        source = design()
        source.root.children[0].depends_on = ["board"]
        with self.assertRaisesRegex(PackageError, "external dependency"):
            create_package(source, "board.channel", name="channel")

    def test_external_sequencing_reference_is_rejected(self):
        source = design()
        source.root.children[0].resources["power_sequence"] = ["board"]
        with self.assertRaisesRegex(PackageError, "external dependency"):
            create_package(source, "board.channel", name="channel")

    def test_export_strips_freeze_records_and_import_cannot_claim_them(self):
        source = design()
        source.root.children[0].frozen = True
        source.root.children[0].frozen_hash = freeze_fingerprint(source, "board.channel")
        block_package = create_package(source, "board.channel", name="channel")
        self.assertFalse(block_package.root.frozen)
        self.assertIsNone(block_package.root.frozen_hash)
        data = block_package.to_dict()
        data["root"]["frozen"] = True
        with self.assertRaisesRegex(PackageError, "freeze records"):
            BlockPackage.from_dict(data)
        with self.assertRaisesRegex(PackageError, "frozen"):
            apply_variant(source.root.children[0], "debug")

    def test_instantiation_respects_frozen_parent(self):
        source = design()
        source.root.frozen = True
        source.root.frozen_hash = freeze_fingerprint(source, "board")
        with self.assertRaisesRegex(ValueError, "frozen"):
            instantiate_package(source, "board", package(), instance_id="one", reference_prefix="A_", net_prefix="A_")

    def test_prefixes_and_parameters_are_required_valid_data(self):
        for fields in ({"instance_id": "bad.path"}, {"reference_prefix": ""}, {"net_prefix": ""},
                       {"parameters": []}, {"shared_nets": "GND"}, {"parameters": {"vin": float("nan")}}):
            options = {"instance_id": "one", "reference_prefix": "A_", "net_prefix": "A_"} | fields
            with self.subTest(fields=fields), self.assertRaises(ModelError):
                instantiate_block(package(), **options)

    def test_exact_semver_and_package_schema(self):
        for version in ("latest", "^1.0.0", "1.0", "01.2.3", "1.0.0-01", None):
            data = package().to_dict()
            data["version"] = version
            with self.subTest(version=version), self.assertRaises(PackageError):
                BlockPackage.from_dict(data)
        for version in ("0.1.0", "1.2.3-alpha.1", "1.2.3+build.5"):
            data = package().to_dict()
            data["version"] = version
            self.assertEqual(BlockPackage.from_dict(data).version, version)
        data = package().to_dict()
        data["execute"] = "python"
        with self.assertRaises(PackageError):
            BlockPackage.from_dict(data)

    def test_file_hash_version_and_deterministic_package_io(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "package.json"
            other = Path(temp) / "other.json"
            digest = write_package(package(), path)
            loaded = load_package(path, expected_version="1.2.3", expected_sha256=digest)
            self.assertEqual(write_package(loaded, other), digest)
            self.assertEqual(path.read_bytes(), other.read_bytes())
            with self.assertRaisesRegex(PackageError, "SHA-256"):
                load_package(path, expected_version="1.2.3", expected_sha256="0" * 64)
            with self.assertRaisesRegex(PackageError, "differs from pinned"):
                load_package(path, expected_version="1.2.4", expected_sha256=digest)
            path.write_text(path.read_text() + " ")
            with self.assertRaisesRegex(PackageError, "SHA-256"):
                load_package(path, expected_version="1.2.3", expected_sha256=digest)

    def test_matching_hash_does_not_allow_malformed_or_executable_data(self):
        payloads = [b'{"name":1,"name":2}', b'{"a":NaN}', b'{"hook":"__import__(os)"}', b'not json']
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "package.json"
            for payload in payloads:
                path.write_bytes(payload)
                with self.subTest(payload=payload), self.assertRaises(PackageError):
                    load_package(path, expected_version="1.2.3", expected_sha256=hashlib.sha256(payload).hexdigest())

    def test_package_size_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "package.json"
            digest = write_package(package(), path)
            with patch("pcb_dev.packages.MAX_PACKAGE_BYTES", 10), self.assertRaisesRegex(PackageError, "exceeds"):
                load_package(path, expected_version="1.2.3", expected_sha256=digest)

    def test_lock_pins_exact_bytes_and_updates_one_named_package(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "blocks" / "channel.json"
            path.parent.mkdir()
            lock_path = Path(temp) / "blocks.lock.json"
            write_package(package(), path)
            pin = lock_package(path, lock_path)
            self.assertEqual(pin.file, "blocks/channel.json")
            self.assertEqual(load_locked_package(lock_path, "sensor-channel").root.id, "channel")
            updated = package()
            updated.version = "1.2.4"
            write_package(updated, path)
            with self.assertRaisesRegex(PackageError, "SHA-256"):
                load_locked_package(lock_path, "sensor-channel")
            lock_package(path, lock_path)
            self.assertEqual(len(load_lock(lock_path).packages), 1)
            self.assertEqual(load_locked_package(lock_path, "sensor-channel").version, "1.2.4")
            with self.assertRaisesRegex(PackageError, "not pinned"):
                load_locked_package(lock_path, "missing")

    def test_lock_path_traversal_absolute_windows_and_duplicates_are_rejected(self):
        for file in ("../outside.json", "/tmp/outside.json", "a/../../b", "a\\b", "C:/b", "./a", "a//b", "a/"):
            with self.subTest(file=file), self.assertRaises(PackageError):
                PackagePin.from_dict({"name": "part", "version": "1.0.0", "file": file, "sha256": "0" * 64})
        pin = PackagePin("part", "1.0.0", "part.json", "0" * 64)
        with self.assertRaisesRegex(PackageError, "one version"):
            PackageLock.from_dict(PackageLock([pin, copy.deepcopy(pin)]).to_dict())

    def test_lock_symlink_escape_and_outside_pin_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "package.json"
            digest = write_package(package(), external)
            local = Path(temp) / "linked.json"
            local.symlink_to(external)
            lock_path = Path(temp) / "blocks.lock.json"
            write_lock(PackageLock([PackagePin("sensor-channel", "1.2.3", "linked.json", digest)]), lock_path)
            with self.assertRaisesRegex(PackageError, "symlink escapes"):
                load_locked_package(lock_path, "sensor-channel")
            with self.assertRaisesRegex(PackageError, "within the lock directory"):
                lock_package(external, lock_path)

    def test_lock_name_is_bound_to_package_content(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "package.json"
            digest = write_package(package(), path)
            lock_path = Path(temp) / "lock.json"
            write_lock(PackageLock([PackagePin("different", "1.2.3", "package.json", digest)]), lock_path)
            with self.assertRaisesRegex(PackageError, "name differs"):
                load_locked_package(lock_path, "different")


if __name__ == "__main__":
    unittest.main()
