"""Offline deployment checks using real trust commands; no provider calls or key reads."""
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from m2k.cli import main as m2k_main
from tools import quickstart


class QuickstartTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.deploy = self.root / "部署 space"
        self.config = self.deploy / "m2k.toml"
        self.addCleanup(patch.stopall)
        patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")).start()
        patch.object(quickstart.sandbox.Bwrap, "available", return_value=(False, "offline test host")).start()

    def call(self, fn, args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                code = fn(list(map(str, args)))
            except SystemExit as error:
                code = error.code
        return code, output.getvalue() + errors.getvalue()

    def init_args(self, worker="api", target=None):
        args = ["init", target or self.deploy, "--worker", worker, "--reviewer-base-url", "https://review.invalid/v1",
                "--reviewer-model", "review-model", "--reviewer-key-env", "T88_REVIEW_KEY", "--accept-no-sandbox"]
        if worker == "api":
            args += ["--worker-base-url", "https://worker.invalid/v1", "--worker-model", "work-model",
                     "--worker-key-env", "T88_WORK_KEY"]
        return args

    def init_ok(self, args=None):
        code, output = self.call(quickstart.main, args or self.init_args())
        self.assertEqual(code, 0, output)
        return tomllib.loads(self.config.read_text(encoding="utf-8")), output

    def status(self):
        return self.call(m2k_main, ["--config", self.config, "trust", "status"])

    def test_api_deployment_is_signed_and_only_stores_env_names(self):
        with patch.dict(os.environ, T88_WORK_KEY="fake-worker-secret", T88_REVIEW_KEY="fake-review-secret"):
            cfg, output = self.init_ok()
        self.assertEqual(self.status()[0], 0)
        self.assertIn("release manifest: ok", output)
        self.assertEqual(cfg["ledger"], "state/ledger.sqlite")
        self.assertEqual(cfg["sandbox"]["backend"], "none")
        self.assertEqual(cfg["trust"]["domain"], "TEST_ONLY")
        self.assertEqual(cfg["trust"]["release"], "keys/release-1.json")
        self.assertEqual(cfg["models"]["worker"]["api_key_env"], "T88_WORK_KEY")
        self.assertEqual(cfg["models"]["reviewer"]["api_key_env"], "T88_REVIEW_KEY")
        for path in self.deploy.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                self.assertNotIn(b"fake-worker-secret", data)
                self.assertNotIn(b"fake-review-secret", data)
        self.assertNotIn("fake-worker-secret", output)
        self.assertNotIn("fake-review-secret", output)
        self.assertTrue((self.deploy / "keys/release-1.json").is_file())
        self.assertEqual((self.deploy / "tasks/demo_calc/task.toml").read_bytes(),
                         (quickstart.ROOT / "examples/demo_calc/task.toml").read_bytes())
        self.assertTrue((self.deploy / "tasks/demo_calc/seed/check.py").is_file())
        self.assertFalse((self.deploy / "tasks/demo_calc/ws").exists())
        for command in ("ping", "run ", "status calc-001", "check-delivery "):
            self.assertIn(command, output)

    def test_key_files_are_checked_for_existence_but_never_opened(self):
        key = self.root / "秘密 key.txt"
        key.touch()
        args = self.init_args()
        for role, env in (("worker", "T88_WORK_KEY"), ("reviewer", "T88_REVIEW_KEY")):
            i = args.index(f"--{role}-key-env")
            args[i:i + 2] = [f"--{role}-key-file", str(key)]
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.resolve() == key:
                raise AssertionError("quickstart tried to open an API key file")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            cfg, _ = self.init_ok(args)
        self.assertEqual(cfg["models"]["worker"]["api_key_file"], key.as_posix())
        self.assertNotIn("api_key_env", cfg["models"]["worker"])
        self.assertEqual(self.status()[0], 0)

    def test_missing_key_file_and_incomplete_api_options_refuse_before_writing(self):
        args = self.init_args()
        i = args.index("--worker-key-env")
        args[i:i + 2] = ["--worker-key-file", str(self.root / "missing.txt")]
        self.assertEqual(self.call(quickstart.main, args)[0], 2)
        self.assertFalse(self.deploy.exists())
        for option in ("--worker-model", "--worker-base-url", "--worker-key-env", "--reviewer-model"):
            args = self.init_args()
            i = args.index(option)
            del args[i:i + 2]
            self.assertEqual(self.call(quickstart.main, args)[0], 2)
            self.assertFalse(self.deploy.exists())

    def test_vendor_presets_match_examples_without_starting_them(self):
        for name, example in (("codex", "codex_demo/m2k.codex.toml"),
                              ("claude-code", "claude_code_demo/m2k.claude.toml")):
            with self.subTest(worker=name):
                self.deploy = self.root / name
                self.config = self.deploy / "m2k.toml"
                cfg, _ = self.init_ok(self.init_args(name))
                expected = tomllib.loads((quickstart.ROOT / "examples" / example).read_text(encoding="utf-8"))
                self.assertEqual(cfg["models"]["worker"], expected["models"]["worker"])
                self.assertEqual(cfg["models"]["worker"]["kind"], "cli")

    def test_generic_cli_json_and_prompt_modes(self):
        for prompt in ("stdin", "arg"):
            with self.subTest(prompt=prompt):
                self.deploy = self.root / prompt
                self.config = self.deploy / "m2k.toml"
                command = ["my-agent", "--label", "中文 space"]
                cfg, _ = self.init_ok(self.init_args("cli") + ["--worker-command", json.dumps(command),
                                                                "--worker-prompt", prompt])
                self.assertEqual(cfg["models"]["worker"]["command"], command)
                self.assertEqual(cfg["models"]["worker"]["prompt"], prompt)
                self.assertNotIn("usage", cfg["models"]["worker"])
                self.assertNotIn("T88_REVIEW_KEY", cfg["models"]["worker"]["env"])
        for value in (None, "{", '"agent"', '{}', '[]', '[""]', '[1]', '["agent", null]'):
            with self.subTest(command=value):
                target = self.root / "invalid"
                args = self.init_args("cli", target)
                if value is not None:
                    args += ["--worker-command", value]
                self.assertEqual(self.call(quickstart.main, args)[0], 2)
                self.assertFalse(target.exists())

    def test_same_model_requires_explicit_opt_in(self):
        args = self.init_args()
        args[args.index("--reviewer-model") + 1] = " WORK-MODEL "
        self.assertEqual(self.call(quickstart.main, args)[0], 2)
        self.assertFalse(self.deploy.exists())
        cfg, _ = self.init_ok(args + ["--allow-same-model"])
        self.assertTrue(cfg["models"]["reviewer"]["allow_same_model"])

    def test_sandbox_requires_consent_and_uses_auto_when_available(self):
        args = self.init_args()
        args.remove("--accept-no-sandbox")
        code, output = self.call(quickstart.main, args)
        self.assertEqual(code, 2)
        self.assertIn("--accept-no-sandbox", output)
        self.assertFalse(self.deploy.exists())
        with patch.object(quickstart.sandbox.Bwrap, "available", return_value=(True, "")):
            cfg, _ = self.init_ok(args)
        self.assertEqual(cfg["sandbox"]["backend"], "auto")

    def test_refused_locations_remain_untouched(self):
        self.deploy.mkdir()
        keep = self.deploy / "keep.txt"
        keep.write_bytes(b"unchanged\x00\xff")
        for target in (self.deploy, keep, quickstart.ROOT, quickstart.ROOT / "T88-refused", quickstart.ROOT.parent):
            with self.subTest(target=target):
                self.assertEqual(self.call(quickstart.main, self.init_args(target=target))[0], 2)
                self.assertEqual(keep.read_bytes(), b"unchanged\x00\xff")
                self.assertEqual(list(self.deploy.iterdir()), [keep])
        self.assertFalse((quickstart.ROOT / "T88-refused").exists())

    def test_resign_preserves_all_bytes_except_release_value(self):
        self.deploy.mkdir()  # an existing empty deployment is accepted
        self.init_ok()
        raw = self.config.read_bytes().replace(b'"work-model"', b'"new-model"')
        raw = raw.replace(b'release = "keys/release-1.json"', b"release = 'keys/release-1.json'  # retain comment")
        raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n") + b"# keep trailing comment\r\n"
        self.config.write_bytes(raw)
        code, output = self.status()
        self.assertNotEqual(code, 0)
        self.assertIn("release manifest: config changed", output)
        code, output = self.call(quickstart.main, ["resign", self.deploy])
        self.assertEqual(code, 0, output)
        expected = raw.replace(b"'keys/release-1.json'", b'"keys/release-2.json"')
        self.assertEqual(self.config.read_bytes(), expected)
        before = tomllib.loads(raw.decode("utf-8"))
        after = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(after["trust"]["release"], "keys/release-2.json")
        after["trust"]["release"] = before["trust"]["release"]
        self.assertEqual(after, before)
        self.assertEqual(self.status()[0], 0)
        body = json.loads((self.deploy / "keys/release-2.json").read_text(encoding="utf-8"))["body"]
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["slot"], "dev")
        self.assertTrue((self.deploy / "keys/release-1.json").is_file())

    def test_resign_unreadable_root_or_status_key_rolls_back_and_allows_retry(self):
        for filename in ("root.pem", "issuer.pem"):
            with self.subTest(unreadable=filename):
                self.deploy = self.root / filename
                self.config = self.deploy / "m2k.toml"
                self.init_ok()
                raw = self.config.read_bytes().replace(b'"work-model"', b'"new-model"')
                self.config.write_bytes(raw)
                key = self.deploy / "keys" / filename
                original_open = Path.open

                def unreadable(path, *args, **kwargs):
                    if path == key:
                        raise PermissionError("test key is unreadable")
                    return original_open(path, *args, **kwargs)

                with patch.object(Path, "open", unreadable):
                    code, output = self.call(quickstart.main, ["resign", self.deploy])
                self.assertEqual(code, 2, output)
                self.assertIn("cannot load private key", output)
                if filename == "issuer.pem":
                    self.assertIn("slot dev version 2", output)  # signing succeeded; status failed
                self.assertEqual(self.config.read_bytes(), raw)
                self.assertFalse((self.deploy / "keys/release-2.json").exists())
                code, output = self.call(quickstart.main, ["resign", self.deploy])
                self.assertEqual(code, 0, output)
                self.assertEqual(self.status()[0], 0)
                body = json.loads((self.deploy / "keys/release-2.json").read_text(encoding="utf-8"))["body"]
                self.assertEqual(body["version"], 2)

    def test_resign_rejects_unsupported_release_syntax_without_writing(self):
        cfg, _ = self.init_ok()
        original = self.config.read_text(encoding="utf-8")
        assignment = 'release = "keys/release-1.json"'
        start, stop = original.index("[trust]"), original.index("[obs]")
        without_table = original[:start] + original[stop:]
        dotted = "\n".join(f"trust.{k} = {quickstart.quoted(v)}" for k, v in cfg["trust"].items())
        inline = ", ".join(f"{k} = {quickstart.quoted(v)}" for k, v in cfg["trust"].items())
        variants = [original.replace(assignment, f"release = {q}{value}{q}")
                    for q in ('"""', "'''")
                    for value in ("keys/release-1.json", "\nkeys/release-1.json", "keys/release-1.json\n")]
        variants += [dotted + "\n" + without_table, "trust = {" + inline + "}\n" + without_table,
                     original.replace(assignment, '"release" = "keys/release-1.json"')]
        for text in variants:
            with self.subTest(config=text[start:start + 100]):
                before = tomllib.loads(text)
                before["trust"]["release"] = cfg["trust"]["release"]
                self.assertEqual(before, cfg)
                raw = text.replace("\n", "\r\n").encode("utf-8")
                self.config.write_bytes(raw)
                code, output = self.call(quickstart.main, ["resign", self.deploy])
                self.assertEqual(code, 2, output)
                self.assertIn("请手工改成单行写法", output)
                self.assertEqual(self.config.read_bytes(), raw)
                self.assertFalse((self.deploy / "keys/release-2.json").exists())

    def test_resign_existing_output_refuses_without_editing_config(self):
        self.init_ok()
        raw = self.config.read_bytes()
        (self.deploy / "keys/release-2.json").write_text("keep", encoding="utf-8")
        self.assertEqual(self.call(quickstart.main, ["resign", self.deploy])[0], 2)
        self.assertEqual(self.config.read_bytes(), raw)
        self.assertEqual((self.deploy / "keys/release-2.json").read_text(encoding="utf-8"), "keep")

    def test_script_entry_point_works_from_outside_repository(self):
        result = subprocess.run([sys.executable, str(quickstart.ROOT / "tools/quickstart.py"), "--help"],
                                cwd=self.root, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("resign", result.stdout)


if __name__ == "__main__":
    unittest.main()
