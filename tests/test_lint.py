"""Audit I3: shell scripts at least parse, even where shellcheck is not installed."""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = ("install.sh", "uninstall.sh", "tests/run.sh")


class ShellLint(unittest.TestCase):
    def test_sh_syntax(self):
        for s in SCRIPTS:
            with self.subTest(script=s):
                p = subprocess.run(["sh", "-n", os.path.join(ROOT, s)], capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_shellcheck(self):
        p = subprocess.run(["shellcheck", "-s", "sh"] + [os.path.join(ROOT, s) for s in SCRIPTS],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
