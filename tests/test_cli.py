import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opslens.__main__ import DEFAULT_CASE, _provider
from opslens.providers import ChatProvider, DeepSeekProvider


class CliTests(unittest.TestCase):
    def setUp(self):
        # Unit tests must never read the developer's optional local credentials.
        loader = patch("opslens.__main__.load_local_env")
        loader.start()
        self.addCleanup(loader.stop)

    def test_bundled_demo_runs_outside_the_source_directory(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(DEFAULT_CASE.read_bytes(), (root / "data/cases/inc001.json").read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run(
                [sys.executable, "-m", "opslens", "demo"], cwd=directory,
                text=True, encoding="utf-8", capture_output=True, timeout=15,
                env={**os.environ, "PYTHONPATH": str(root), "PYTHONIOENCODING": "utf-8"},
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result["status"], "completed")
            self.assertTrue(Path(result["report"]).is_file())

    def test_demo_pause_resume_export_and_score(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            common = ["--db", str(output / "runs.sqlite3"), "--output", str(output)]

            def run(*arguments):
                process = subprocess.run([sys.executable, "-m", "opslens", *arguments],
                                         cwd=root, text=True, encoding="utf-8", capture_output=True,
                                         env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=15)
                self.assertEqual(process.returncode, 0, process.stderr)
                return json.loads(process.stdout)

            paused = run("demo", "--stop-after", "2", *common)
            self.assertEqual(paused["status"], "paused")
            finished = run("resume", paused["run_id"], *common)
            self.assertEqual((finished["status"], finished["steps"]), ("completed", 4))
            report = Path(finished["report"])
            self.assertIn("固定脚本演示", report.read_text(encoding="utf-8"))
            scored = run("evaluate", str(report.parent / "result.json"), "--output", str(output / "score.json"))
            self.assertTrue(scored["scripted_demo"])
            self.assertEqual(scored["aggregate"]["n_task_success"], 1)
            self.assertFalse(scored["usage_complete"])

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-local-secret"}, clear=True)
    def test_default_live_profile_is_deepseek(self):
        provider = _provider("live")
        self.assertIsInstance(provider, DeepSeekProvider)
        self.assertEqual(provider.model, "deepseek-flash")
        self.assertNotIn("test-local-secret", provider.name)

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_key_never_silently_uses_demo(self):
        with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
            _provider("live")

    @patch.dict(os.environ, {"OPSLENS_API_KEY": "secret", "OPSLENS_MODEL": "model",
                            "OPSLENS_BASE_URL": "https://example.invalid/v1"}, clear=True)
    def test_compatible_profile_remains_available(self):
        self.assertIsInstance(_provider("live", "compatible"), ChatProvider)


if __name__ == "__main__":
    unittest.main()
