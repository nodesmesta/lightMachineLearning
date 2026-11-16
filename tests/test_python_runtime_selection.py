import subprocess
import unittest

from sydeco_lightml_core.core import _python_with_pip


class PythonRuntimeSelectionTests(unittest.TestCase):
    def test_selects_an_interpreter_that_has_pip(self):
        executable = _python_with_pip()
        result = subprocess.run(
            [executable, "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
