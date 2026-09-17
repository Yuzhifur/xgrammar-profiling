import os
import subprocess
import sys
import unittest


class WorkerStdoutGuardTests(unittest.TestCase):
    def test_prints_inside_guard_go_to_stderr_and_stdout_is_restored(self):
        script = (
            "import os, sys\n"
            "from xgrammar_profile.replay import worker_stdout_guard\n"
            "with worker_stdout_guard():\n"
            "    print('stray python print')\n"
            "    os.write(1, b'stray fd write\\n')\n"
            "print('{\"record\": true}', flush=True)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            text=True,
            capture_output=True,
            check=True,
            env={**os.environ, "PYTHONNOUSERSITE": "1"},
        )
        self.assertEqual(completed.stdout, '{"record": true}\n')
        self.assertIn("stray python print", completed.stderr)
        self.assertIn("stray fd write", completed.stderr)


if __name__ == "__main__":
    unittest.main()
