import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from m2k.tools import COMMAND_USER_ENV, REDACTED, command_env, run_shell
from m2k.types import EffectStatus

SECRET = "sk-m2k-test-" + "0123456789abcdef"
PRINT_VAR = '{python} -c "import os; print(os.environ.get(\'%s\', \'absent\'))"'


class KeyGuardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_cmd(self, command, **env):
        with mock.patch.dict(os.environ, env):
            return run_shell(command, self.cwd, 30)

    def test_secret_named_variables_are_not_inherited(self):
        for name in ("DEEPSEEK_API_KEY", "GLM_API_KEY", "GITHUB_TOKEN", "MY_SECRET", "DB_PASSWORD"):
            eff = self.run_cmd(PRINT_VAR % name, **{name: SECRET})
            self.assertEqual(eff.status, EffectStatus.COMPLETED, eff.output)
            self.assertEqual(eff.output.strip(), "absent", name)

    def test_ordinary_variables_are_kept(self):
        eff = self.run_cmd(PRINT_VAR % "M2K_PLAIN_SETTING", M2K_PLAIN_SETTING="hello")
        self.assertEqual(eff.output.strip(), "hello")
        env, _ = command_env()
        self.assertIn("PATH", {k.upper() for k in env})

    def test_secret_value_in_output_is_redacted(self):
        half = len(SECRET) // 2
        cmd = '{python} -c "print(\'%s\' + \'%s\')"' % (SECRET[:half], SECRET[half:])
        eff = self.run_cmd(cmd, DEEPSEEK_API_KEY=SECRET)
        self.assertNotIn(SECRET, eff.output)
        self.assertIn(REDACTED, eff.output)

    def test_command_user_variable_is_not_passed_on(self):
        with mock.patch.dict(os.environ, {COMMAND_USER_ENV: "someone"}):
            env, _ = command_env()
        self.assertNotIn(COMMAND_USER_ENV, env)

    @unittest.skipIf(os.name != "nt" and os.geteuid() == 0, "running as root")
    def test_command_user_fails_closed_without_root(self):
        eff = self.run_cmd("echo should-not-run", **{COMMAND_USER_ENV: "nobody"})
        self.assertEqual(eff.status, EffectStatus.FAILED)
        self.assertEqual(eff.detail.get("reason"), "START_FAILED")
        self.assertNotIn("should-not-run", eff.output)

    @unittest.skipIf(os.name == "nt" or os.geteuid() != 0, "needs POSIX root")
    def test_command_user_drops_privileges_when_root(self):
        import pwd
        nobody = pwd.getpwnam("nobody")
        eff = self.run_cmd('{python} -c "import os; print(os.getuid())"', **{COMMAND_USER_ENV: "nobody"})
        self.assertEqual(eff.output.strip(), str(nobody.pw_uid), eff.output)
        for bad in ("root", "no-such-user-m2k"):
            eff = self.run_cmd("echo should-not-run", **{COMMAND_USER_ENV: bad})
            self.assertEqual(eff.detail.get("reason"), "START_FAILED", bad)


if __name__ == "__main__":
    unittest.main()
