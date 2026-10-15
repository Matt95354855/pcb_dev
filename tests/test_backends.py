"""Backend regression tests; native execution is deliberately optional."""

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from pcb_dev.backends import FreeroutingBackend, KiCadBackend, NgSpiceBackend, ToolResult, ToolRunner, generate
from pcb_dev.backends.kicad import _native_violations, _validate_export


def design():
    return {"schema_version": 1, "name": "Preview board", "version": "0.1.0",
            "board": {"width_mm": 40, "height_mm": 30, "layers": 4},
            "root": {"id": "board", "components": [
                {"id": "supply", "reference": "J1", "value": "Power input", "footprint": "Connector:RealPart", "pins": {"1": "VIN", "2": "GND"}, "position": {"x_mm": 5, "y_mm": 5, "rotation": 90, "side": "top"}}],
                "children": [{"id": "load", "components": [
                    {"id": "r", "reference": "R1", "value": "1k", "footprint": "Resistor_SMD:R_0603_1608Metric", "pins": {"2": "GND", "1": "VIN"}, "fitted": False}]}]}}


class GenerationTests(unittest.TestCase):
    def test_generation_is_deterministic_and_keeps_nested_global_nets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = generate(design(), root / "one")
            second = generate(design(), root / "two")
            self.assertEqual(first.keys(), second.keys())
            for key in first:
                self.assertEqual(first[key].read_bytes(), second[key].read_bytes(), key)
            board = first["board"].read_text()
            schematic = first["schematic"].read_text()
            self.assertIn("PCBDEV_PREVIEW_ONLY", board)
            self.assertIn("PCBDEV_PREVIEW_ONLY", schematic)
            self.assertIn('(4 "In1.Cu" signal)', board)
            self.assertIn('(at 5 5 90)', board)
            self.assertIn('(net 1 "GND")', board)
            self.assertIn('(net 2 "VIN")', board)
            self.assertNotIn('(segment ', board)
            self.assertIn('(dnp yes)', schematic)
            self.assertIn('(attr smd dnp)', board)
            xml = ET.parse(first["netlist"])
            self.assertEqual({c.attrib["ref"] for c in xml.findall("components/comp")}, {"J1", "R1"})
            vin = xml.find("nets/net[@name='VIN']")
            self.assertEqual({(node.attrib["ref"], node.attrib["pin"]) for node in vin}, {("J1", "1"), ("R1", "1")})
            manifest = json.loads(first["manifest"].read_text())
            self.assertFalse(manifest["fabrication_ready"])
            self.assertFalse(manifest["routed"])
            self.assertEqual(manifest["artifacts"]["board"]["sha256"], hashlib.sha256(first["board"].read_bytes()).hexdigest())

    def test_files_are_balanced_quoted_s_expressions(self):
        with tempfile.TemporaryDirectory() as directory:
            data = design()
            data["root"]["components"][0]["value"] = 'quoted " value \\ path (a)'
            files = generate(data, Path(directory))
            for kind in ("board", "schematic", "rules"):
                text = files[kind].read_text()
                # Ignore fully quoted atoms before testing structural parentheses.
                unquoted = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
                depth = 0
                for char in unquoted:
                    depth += (char == "(") - (char == ")")
                    self.assertGreaterEqual(depth, 0, kind)
                self.assertEqual(depth, 0, kind)

    def test_safe_output_name_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = design()
            data["name"] = "../../escape"
            files = generate(data, root)
            self.assertTrue(all(path.parent == root for path in files.values()))
            files["board"].unlink()
            other = root / "other.txt"
            other.write_text("untouched")
            files["board"].symlink_to(other)
            with self.assertRaises(ValueError):
                generate(data, root)
            self.assertEqual(other.read_text(), "untouched")

    def test_nonfinite_dimensions_fail_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            data = design()
            data["board"]["width_mm"] = float("nan")
            with self.assertRaises(ValueError):
                generate(data, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])


class RunnerTests(unittest.TestCase):
    def test_refuses_arbitrary_executables(self):
        with self.assertRaises(ValueError):
            ToolRunner().run("sh", ["-c", "echo unsafe"], Path.cwd())

    def test_missing_binary_is_unavailable_and_unexecuted(self):
        with patch("pcb_dev.backends.runner.shutil.which", return_value=None):
            result = ToolRunner().run("kicad-cli", ["--version"], Path.cwd())
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.executed)

    @unittest.skipIf(os.name != "posix", "requires executable POSIX fixture")
    def test_literal_arguments_and_bounded_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "ngspice"
            executable.write_text(f'#!{sys.executable}\nimport sys\nprint(sys.argv[1])\nprint("x" * 10000)\n')
            executable.chmod(0o700)
            result = ToolRunner(max_output_bytes=256).run(str(executable), ["$(touch SHOULD_NOT_EXIST)"], root)
            self.assertEqual(result.status, "success")
            self.assertIn("$(touch SHOULD_NOT_EXIST)", result.stdout)
            self.assertFalse((root / "SHOULD_NOT_EXIST").exists())
            self.assertLessEqual(len(result.stdout), 256)
            self.assertTrue(result.truncated)

    @unittest.skipIf(os.name != "posix", "requires executable POSIX fixture")
    def test_timeout_terminates_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "ngspice"
            executable.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(30)\n')
            executable.chmod(0o700)
            result = ToolRunner().run(str(executable), [], root, timeout=0.05)
            self.assertEqual(result.status, "timeout")
            self.assertTrue(result.executed)


class FakeRunner:
    def __init__(self, violation=False, malformed=False, missing=False, version="10.0.1", code=None):
        self.violation, self.malformed, self.missing, self.version = violation, malformed, missing, version
        self.code = code
        self.calls = []

    def run(self, executable, args, cwd, timeout=120):
        self.calls.append(args)
        command = (executable, *args)
        if args == ["--version"]:
            return ToolResult(command, "success", 0, self.version, executed=True)
        report = Path(args[args.index("--output") + 1])
        if args[1] in ("erc", "drc"):
            item = {"severity": "error", "type": "unconnected_items", "description": "Disconnected preview pad", "items": []}
            violations = [item] if self.violation else []
            payload = {"kicad_version": self.version, "source": Path(args[-1]).name, "date": "2026-10-15T12:00:00Z", "coordinate_units": "mm"}
            payload.update({"sheets": [{"path": "/", "violations": violations}]} if args[1] == "erc" else {"violations": violations, "unconnected_items": [], "schematic_parity": []})
            if not self.missing:
                report.write_text("{" if self.malformed else json.dumps(payload))
            code = self.code if self.code is not None else (5 if self.violation else 0)
            return ToolResult(command, "success" if code == 0 else "failed", code, executed=True)
        return ToolResult(command, "success", 0, executed=True)


class AdapterTests(unittest.TestCase):
    def test_native_success_requires_fresh_valid_json_and_input_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = generate(design(), root / "input")
            fake = FakeRunner()
            checks = KiCadBackend(fake).check(files["board"], files["schematic"], root / "reports")
            for item in checks.values():
                self.assertEqual(item["status"], "passed")
                self.assertTrue(item["executed"])
                self.assertRegex(item["input_sha256"], r"^[a-f0-9]{64}$")
                self.assertTrue(Path(item["report_path"]).is_file())
                self.assertEqual(item["violations"], [])
            for command in fake.calls[1:]:
                self.assertIn("--severity-all", command)
                self.assertIn("--exit-code-violations", command)

    def test_exit_five_is_violation_evidence_never_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = generate(design(), root / "input")
            result = KiCadBackend(FakeRunner(violation=True)).check(files["board"], files["schematic"], root / "reports")
            for item in result.values():
                self.assertEqual(item["status"], "failed")
                self.assertEqual(item["returncode"], 5)
                self.assertEqual(len(item["violations"]), 1)
                self.assertIn("native_report", item)

    def test_exit_zero_cannot_hide_reported_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = generate(design(), root / "input")
            result = KiCadBackend(FakeRunner(violation=True, code=0)).check(files["board"], files["schematic"], root / "reports")
            self.assertTrue(all(item["status"] == "failed" for item in result.values()))

    def test_malformed_missing_and_stale_reports_fail_closed(self):
        for flags in ({"malformed": True}, {"missing": True}, {"code": 2}):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                files = generate(design(), root / "input")
                output = root / "reports"
                output.mkdir()
                (output / "drc.json").write_text('{"violations":[]}')
                result = KiCadBackend(FakeRunner(**flags)).check(files["board"], files["schematic"], output)
                self.assertTrue(all(item["status"] == "failed" for item in result.values()))

    def test_unsupported_version_does_not_execute_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = generate(design(), root / "input")
            fake = FakeRunner(version="8.0.9")
            result = KiCadBackend(fake).check(files["board"], files["schematic"], root / "reports")
            self.assertEqual(len(fake.calls), 1)
            self.assertTrue(all(not item["executed"] and item["status"] == "failed" for item in result.values()))

    def test_export_exit_zero_without_artifacts_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = generate(design(), root / "input")
            result = KiCadBackend(FakeRunner()).export(files["board"], files["schematic"], root / "exports", layers=4)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["fabrication_ready"])
            self.assertTrue(all(item["status"] == "failed" for item in result["products"].values()))

    def test_ignored_checks_and_exclusions_remain_blocking(self):
        report = {"source": "board.kicad_pcb", "date": "2026-10-15T12:00:00Z", "kicad_version": "10.0.2", "coordinate_units": "mm", "violations": [], "schematic_parity": [], "unconnected_items": [], "ignored_checks": [{"key": "clearance", "description": "clearance ignored"}]}
        findings = _native_violations(report, "drc")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "ignored_check")
        report["violations"] = [{"severity": "error", "type": "shorting_items", "description": "excluded short", "items": [], "excluded": True}]
        self.assertTrue(_native_violations(report, "drc")[0]["excluded"])
        report["included_severities"] = ["error"]
        with self.assertRaises(ValueError):
            _native_violations(report, "drc")

    def test_report_source_and_schema_must_match(self):
        class AlteredRunner(FakeRunner):
            def run(self, executable, args, cwd, timeout=120):
                result = super().run(executable, args, cwd, timeout)
                if args[1:2] in (["erc"], ["drc"]):
                    path = Path(args[args.index("--output") + 1])
                    payload = json.loads(path.read_text())
                    payload.update(source="another-file.kicad_pcb", **{"$schema": "https://schemas.kicad.org/drc.v999.json"})
                    path.write_text(json.dumps(payload))
                return result
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = generate(design(), root / "input")
            result = KiCadBackend(AlteredRunner()).check(files["board"], files["schematic"], root / "reports")
            self.assertTrue(all(item["status"] == "failed" for item in result.values()))

    def test_native_netlist_has_input_and_output_digests(self):
        class NetlistRunner(FakeRunner):
            def run(self, executable, args, cwd, timeout=120):
                if args[:3] == ["sch", "export", "netlist"]:
                    output = Path(args[args.index("--output") + 1])
                    output.write_text(f'<export><design><source>{args[-1]}</source></design><components/><nets/></export>')
                    return ToolResult((executable, *args), "success", 0, executed=True)
                return super().run(executable, args, cwd, timeout)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = generate(design(), root / "input")
            result = KiCadBackend(NetlistRunner()).export_netlist(files["schematic"], root / "native.net")
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["input_sha256"], hashlib.sha256(files["schematic"].read_bytes()).hexdigest())
            self.assertEqual(result["output_sha256"], hashlib.sha256(Path(result["path"]).read_bytes()).hexdigest())

    def test_native_netlist_refuses_dtd_and_stale_output(self):
        class EntityRunner(FakeRunner):
            def run(self, executable, args, cwd, timeout=120):
                if args[:3] == ["sch", "export", "netlist"]:
                    Path(args[args.index("--output") + 1]).write_text('<!DOCTYPE export [<!ENTITY bad "bad">]><export><components/><nets/></export>')
                    return ToolResult((executable, *args), "success", 0, executed=True)
                return super().run(executable, args, cwd, timeout)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = generate(design(), root / "input")
            result = KiCadBackend(EntityRunner()).export_netlist(files["schematic"], root / "native.net")
            self.assertEqual(result["status"], "failed")
            self.assertIsNone(result["output_sha256"])

    def test_export_structure_rejects_partial_gerber_and_invalid_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layer = root / "board-F_Cu.gbr"
            layer.write_text("%FSLAX46Y46*%\n%MOMM*%\nM02*\n")
            with self.assertRaises(ValueError):
                _validate_export("gerbers", [layer], 2)
            csv = root / "positions.csv"
            csv.write_text("Ref,Val,Package,PosX,PosY,Rot,Side\nR1,1k,R_0603,nan,0,0,top\n")
            with self.assertRaises(ValueError):
                _validate_export("positions", [csv], 2)


class FakeSimulationRunner:
    def __init__(self, bad_log=False, missing=False):
        self.bad_log, self.missing = bad_log, missing
        self.calls = []

    def run(self, executable, args, cwd, timeout=120):
        self.calls.append(args)
        if args == ["--version"]:
            return ToolResult((executable, *args), "success", 0, "ngspice-46", executed=True)
        if not self.missing and "-r" in args:
            raw = Path(args[args.index("-r") + 1])
            raw.write_bytes(b"Title: example\nDate: today\nPlotname: Transient Analysis\nFlags: real\nNo. Variables: 2\nNo. Points: 2\nVariables:\n0 time time\n1 v(out) voltage\nBinary:\n" + b"\x00" * 32)
        Path(args[args.index("-o") + 1]).write_text("Error: simulation aborted\n" if self.bad_log else "No. of Data Rows: 2\nrise = 1.5e-6\n")
        return ToolResult((executable, *args), "success", 0, executed=True)


class SimulationTests(unittest.TestCase):
    def test_simulation_preserves_engine_artifact_and_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            netlist = root / "circuit.cir"
            netlist.write_text("RC test\nV1 in 0 PULSE(0 1 0 1n 1n 1m 2m)\nR1 in out 1k\nC1 out 0 1u\n.tran 1u 2m\n.meas tran rise FIND v(out) AT=1m\n.end\n")
            fake = FakeSimulationRunner()
            result = NgSpiceBackend(fake).simulate(netlist, root / "results")
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["measurements"], {"rise": 1.5e-6})
            self.assertEqual(result["waveform"]["points"], 2)
            self.assertFalse(result["hardware_validated"])
            self.assertIn("-n", fake.calls[-1])
            self.assertIn("-b", fake.calls[-1])
            self.assertNotIn("-r", fake.calls[-1])
            self.assertIn("measurement_tool", result)

    def test_control_scripts_and_external_models_never_execute(self):
        for directive in (".control\nshell touch bad\n.endc", ".include secrets.lib", ".lib arbitrary.lib", ".hdl malicious.osdi"):
            with self.subTest(directive=directive), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "bad.cir"
                source.write_text(f"Test\nR1 in 0 1k\n{directive}\n.op\n.end\n")
                fake = FakeSimulationRunner()
                result = NgSpiceBackend(fake).simulate(source, root / "output")
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["executed"])
                self.assertEqual(fake.calls, [])

    def test_errors_or_missing_waveforms_fail_closed(self):
        for flags in ({"bad_log": True}, {"missing": True}):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "test.cir"
                source.write_text("Test\nV1 in 0 1\nR1 in 0 1k\n.op\n.end\n")
                result = NgSpiceBackend(FakeSimulationRunner(**flags)).simulate(source, root / "output")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["paths"], [])


class FakeRoutingRunner:
    def __init__(self, malformed=False, missing=False):
        self.malformed, self.missing = malformed, missing
        self.calls = []

    def run(self, executable, args, cwd, timeout=120):
        self.calls.append(args)
        if args == ["-version"]:
            return ToolResult((executable, *args), "success", 0, stderr='openjdk version "25.0.1"', executed=True)
        if not self.missing:
            session = Path(args[args.index("-do") + 1])
            session.write_text("junk" if self.malformed else '(session "preview" (base_design "preview") (routes (resolution mm 1000) (network_out (net "VIN" (wire (path F.Cu 0.25 1 1 2 2))))))')
        return ToolResult((executable, *args), "success", 0, executed=True)


class RoutingTests(unittest.TestCase):
    @staticmethod
    def jar(root):
        path = root / "freerouting.jar"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nImplementation-Version: 2.3.0\n")
        return path

    def test_explicit_dsn_to_session_does_not_claim_verified_board(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jar = self.jar(root)
            source = root / "board.dsn"
            source.write_text('(pcb "preview" (structure) (network))')
            fake = FakeRoutingRunner()
            result = FreeroutingBackend(jar, fake, expected_sha256=hashlib.sha256(jar.read_bytes()).hexdigest()).route(source, root / "output", max_passes=5)
            self.assertEqual(result["status"], "passed")
            self.assertTrue(Path(result["paths"][0]).is_file())
            self.assertFalse(result["routing_verified"])
            self.assertFalse(result["board_updated"])
            self.assertFalse(result["fabrication_ready"])
            self.assertIn("-da", fake.calls[-1])
            self.assertIn("--api_server.enabled=false", fake.calls[-1])

    def test_wrong_jar_digest_never_runs_java(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jar = self.jar(root)
            source = root / "board.dsn"
            source.write_text('(pcb "preview" (structure) (network))')
            fake = FakeRoutingRunner()
            result = FreeroutingBackend(jar, fake, expected_sha256="0" * 64).route(source, root / "output")
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["executed"])
            self.assertEqual(fake.calls, [])

    def test_missing_and_malformed_sessions_fail_closed(self):
        for flags in ({"missing": True}, {"malformed": True}):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "board.dsn"
                source.write_text('(pcb "preview" (structure) (network))')
                result = FreeroutingBackend(self.jar(root), FakeRoutingRunner(**flags)).route(source, root / "output")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["paths"], [])


if __name__ == "__main__":
    unittest.main()
