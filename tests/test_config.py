import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from opslens.config import load_local_env


class ConfigTests(unittest.TestCase):
    @patch.dict(os.environ, {"OPSLENS_MODEL": "existing-model"}, clear=True)
    def test_local_settings_do_not_override_environment_or_execute_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# local only\nOPSLENS_MODEL=other\nDEEPSEEK_API_KEY='literal$(text)'\n", encoding="utf-8")
            load_local_env(path)
        self.assertEqual(os.environ["OPSLENS_MODEL"], "existing-model")
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "literal$(text)")

    def test_invalid_config_error_does_not_echo_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("TYPO=super-secret", encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                load_local_env(path)
            self.assertNotIn("super-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
