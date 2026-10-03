#!/usr/bin/env python3
"""Command-level checks. The driver is a fixture, never live 5G evidence.

Failure cases: omitted trials, changed evidence, non-finite numbers,
failed cleanup, driver timeout, failed trials hidden by a median,
zero reference values, and missing receiver results.
"""
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


class ComparisonCommandTest(unittest.TestCase):
    def test_run_report_and_reject_changed_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            cwd = root / "work"
            cwd.mkdir()
            driver = root / "driver.py"
            driver.write_text('''import json, os, pathlib
p = pathlib.Path(os.environ["BENCH_RESULT_DIR"])
c = json.loads(os.environ["BENCH_CANDIDATE"])
(p / "proof.txt").write_text("fixture only")
v = 20 if c["core"] == "peer" else 10
(p / "measurement.json").write_text(json.dumps({
 "kind": "fixture", "cleanup_ok": True, "quality_errors": [],
 "metrics": {"attach_ready_ms": v},
 "features": {"registration": "passed"}, "evidence": ["proof.txt"]}))
''')
            plan = {
                "schema_version": 1, "mode": "fixture", "repetitions": 6, "seed": 17,
                "protocol": {"ue_counts": [1], "duration_seconds": 2, "warmup_seconds": 1,
                             "packet_bytes": 1200, "offered_mbps_per_ue": 1},
                "timeout_seconds": 10, "equivalence_percent": 10,
                "driver": [sys.executable, str(driver)], "cwd": str(cwd),
                "inputs": {str(driver): hashlib.sha256(driver.read_bytes()).hexdigest()},
                "candidates": [
                    {"id": "ours", "core": "ours", "radio": "common", "identity": {"core": "a", "radio": "r"}},
                    {"id": "repeat", "core": "ours", "radio": "common", "identity": {"core": "a", "radio": "r"}},
                    {"id": "peer", "core": "peer", "radio": "common", "identity": {"core": "b", "radio": "r"}},
                ],
            }
            config = root / "plan.json"
            config.write_text(json.dumps(plan))
            output = root / "run"
            cmd = [sys.executable, str(ROOT / "benchmark.py")]
            run = subprocess.run(cmd + ["run", str(config), "--output", str(output)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["status"], "verification_only")
            self.assertEqual(report["trial_count"], 18)
            self.assertTrue(report["self_checks"][0]["equivalent"])
            self.assertFalse(report["publishable"])
            self.assertTrue(all(r["decision"] == "verification_only" for r in report["comparisons"]))
            original = (output / "report.json").read_bytes()
            (cwd / ".benchmark.lock").unlink()
            cwd.rmdir()
            rerun = subprocess.run(cmd + ["report", str(output)], capture_output=True, text=True)
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            self.assertEqual(original, (output / "report.json").read_bytes())
            cwd.mkdir()
            missing = next(output.glob("trials/*/result.json"))
            saved = missing.read_bytes()
            missing.unlink()
            invalid = subprocess.run(cmd + ["report", str(output)], capture_output=True, text=True)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("missing trial", (output / "report.json").read_text())
            missing.write_bytes(saved)
            proof = next(output.glob("trials/*/proof.txt"))
            proof.write_text("changed")
            invalid = subprocess.run(cmd + ["report", str(output)], capture_output=True, text=True)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("hash", invalid.stderr)

            # A failed trial must remain in the sample counts and withhold every rank.
            driver.write_text(driver.read_text().replace('"quality_errors": []',
                '"quality_errors": ["injected failure"] if c["core"] == "peer" else []'))
            plan["inputs"][str(driver)] = hashlib.sha256(driver.read_bytes()).hexdigest()
            config.write_text(json.dumps(plan))
            failed_output = root / "failed"
            failed = subprocess.run(cmd + ["run", str(config), "--output", str(failed_output)], capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            failed_report = json.loads((failed_output / "report.json").read_text())
            self.assertEqual(failed_report["trial_count"], 18)
            self.assertEqual(len(failed_report["problems"]), 6)
            self.assertFalse(failed_report["publishable"])

            # Missing cleanup stops the batch. Its missing trials remain visible.
            driver.write_text(driver.read_text().replace('"cleanup_ok": True', '"cleanup_ok": False'))
            plan["inputs"][str(driver)] = hashlib.sha256(driver.read_bytes()).hexdigest()
            config.write_text(json.dumps(plan))
            stopped_output = root / "stopped"
            stopped = subprocess.run(cmd + ["run", str(config), "--output", str(stopped_output)], capture_output=True, text=True)
            self.assertNotEqual(stopped.returncode, 0)
            self.assertEqual(json.loads((stopped_output / "report.json").read_text())["trial_count"], 1)
            driver.write_text("import time\ntime.sleep(30)\n")
            plan["inputs"][str(driver)] = hashlib.sha256(driver.read_bytes()).hexdigest()
            plan["timeout_seconds"] = 1
            config.write_text(json.dumps(plan))
            timed_output = root / "timed"
            timed = subprocess.run(cmd + ["run", str(config), "--output", str(timed_output)], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(timed.returncode, 0)
            timed_report = json.loads((timed_output / "report.json").read_text())
            self.assertEqual(timed_report["trial_count"], 1)
            self.assertIn("exit 124", timed_report["problems"][0])

    def test_statistics_and_failed_runs(self):
        # These inputs cannot be produced predictably by timing a live core.
        import benchmark
        a = [10, 11, 9, 10, 11, 9]
        self.assertEqual(benchmark.paired_interval(a, a, 1), [0.0, 0.0])
        self.assertGreater(benchmark.paired_interval([v * 2 for v in a], a, 1)[0], 0)
        self.assertLess(benchmark.paired_interval([v / 2 for v in a], a, 1)[1], 0)
        self.assertEqual(benchmark.paired_interval([0] * 6, [0] * 6, 1), [0.0, 0.0])
        self.assertIsNone(benchmark.paired_interval([1] * 6, [0] * 6, 1))
        self.assertEqual(benchmark.absolute_interval([1] * 6, [0] * 6, 1), [1, 1])
        self.assertEqual(benchmark.absolute_interval([0] * 6, [1] * 6, 1), [-1, -1])
        with self.assertRaises(ValueError):
            benchmark.validate_measurement({"kind": "live", "cleanup_ok": True,
                "quality_errors": [], "metrics": {"attach_ready_ms": float("nan")},
                "features": {}, "evidence": []})
        with self.assertRaises(ValueError):
            benchmark.validate_measurement([])
        with self.assertRaises(ValueError):
            benchmark.evidence_hashes(ROOT, [None])

    def test_receiver_metrics(self):
        # Sender rate must never replace missing receiver data.
        import native_lab
        self.assertEqual(native_lab.receiver_metrics({"end": {"sum_received": {
            "bits_per_second": 12000000, "lost_percent": 2}}}, True), (12, 2))
        with self.assertRaises(ValueError):
            native_lab.receiver_metrics({"end": {"sum_sent": {"bits_per_second": 999}}}, False)
        with self.assertRaises(ValueError):
            native_lab.receiver_metrics({"error": "connection failed"}, False)
        samples = [{"time": n / 10, "roles": {"ue": {"cpu": .1 if n else 0}}} for n in range(30)]
        self.assertFalse(native_lab.generator_saturated(samples, "ue"))
        for n, sample in enumerate(samples):
            sample["roles"]["ue"]["cpu"] = n / 10
        self.assertTrue(native_lab.generator_saturated(samples, "ue"))


if __name__ == "__main__":
    unittest.main()
