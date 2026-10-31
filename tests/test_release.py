"""T43: Root-signed startup release checks, using temporary keys and scripted models only."""
import copy
import hashlib
import io
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from m2k import anchor, obs, trust
from m2k.adjudicate import adjudicate
from m2k.cli import main
from m2k.fingerprint import framework_fingerprint
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from tests.helpers import review, step


CODE = Path(trust.__file__).resolve().parent
CAPS = {"tools": ["list_files", "read_file", "write_file", "edit_file", "run_command"],
        "max_rounds": 100, "max_model_calls": 100, "max_tokens": 100000, "max_commands": 100, "network": False}


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.keys = self.root / "keys"
        self.setup, self.root_key = trust.dev_setup(self.keys, CAPS)
        self.pin = self.keys / "root-pin.json"
        self.pin.write_bytes(trust.canonical({"trust_domain": "TEST_ONLY",
                                             "public_key_b64": trust.public_key_b64(self.root_key)}))
        (self.keys / "charter.json").write_bytes(trust.canonical(self.setup.charter_env))
        self.root_pem = self.keys / "root.pem"
        self.root_pem.write_bytes(self.pem(self.root_key))
        self.config = self.root / "m2k.toml"
        self.config.write_bytes(b'ledger = "ledger.sqlite"\n[sandbox]\nbackend = "none"\n'
                                b'[models.worker]\nmodel = "worker"\n[models.reviewer]\nmodel = "reviewer"\n'
                                b'[trust]\ndomain = "TEST_ONLY"\nroot_pin = "keys/root-pin.json"\n'
                                b'charter = "keys/charter.json"\nissuer_key = "keys/issuer.pem"\n'
                                b'revocations = "keys/revocations.json"\nrelease = "keys/release.json"\n')
        obs_key, self.obs_public = trust.generate_keypair()
        (self.keys / "obs.pem").write_bytes(self.pem(obs_key))
        self.obs_block = ('[obs]\nkey = "keys/obs.pem"\nlog = "obs.jsonl"\npublic_key_b64 = '
                          + json.dumps(self.obs_public) + '\n').encode("utf-8")
        self.config.write_bytes(self.config.read_bytes() + self.obs_block)
        self.task = self.root / "task.toml"
        self.task.write_bytes(b'[task]\nid = "t43"\ngoal = "check startup"\nworkspace = "ws"\n'
                              b'[acceptance]\ncommand = "{python} check.py"\nfiles = ["check.py"]\n'
                              b'[budget]\nmax_tokens = 1000\n')
        workspace = self.root / "ws"
        workspace.mkdir()
        (workspace / "check.py").write_bytes(b'print("ok")\n')
        self.ledger_path = self.root / "ledger.sqlite"
        self.release = self.keys / "release.json"
        self.sign()
        self.save_good()

    @staticmethod
    def pem(key):
        return key.private_bytes(trust.serialization.Encoding.PEM, trust.serialization.PrivateFormat.PKCS8,
                                 trust.serialization.NoEncryption())

    def sign(self, key=None, **changes):
        body = {"schema": trust.RELEASE, "trust_domain": "TEST_ONLY", "manifest_id": "test-release",
                "slot": "test", "version": 1, "code": "dev", "config": trust.config_digest(self.config.read_bytes()), "issued_at": 1}
        self.release.write_bytes(trust.canonical(trust.sign(body | changes, key or self.root_key)))

    def save_good(self):
        self.good = {p: p.read_bytes() for p in
                     (self.config, self.release, self.pin, self.keys / "charter.json", self.keys / "revocations.json")}

    def restore(self):
        for path, raw in self.good.items():
            path.write_bytes(raw)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--config", str(self.config), *map(str, args)])
        self.assertNotIn("-----BEGIN PRIVATE KEY-----", out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def run_scripted(self, expected=0, probe=None, replies=None):
        worker = ScriptedModel(replies or [step("finish")], "worker")
        reviewer = ScriptedModel([review("GO")], "reviewer")
        worker.external = True

        def try_probe():
            worker.external = False  # Only the CLI startup probe is simulated; the task uses ScriptedModel.
            return probe() if probe else None

        worker.probe = Mock(side_effect=try_probe)
        with patch("m2k.cli.build_model", side_effect=[worker, reviewer]):
            code, out, err = self.cli("run", self.task)
        self.assertEqual(code, expected, out + err)
        worker.probe.assert_called_once_with()
        return worker, reviewer

    def refuse_then_recover(self, error_code):
        with patch("m2k.cli.build_model") as model, patch("m2k.external.CliAgent.probe") as probe, \
                patch("m2k.cli.sandboxes.choose") as sandbox:
            code, out, err = self.cli("run", self.task)
        self.assertEqual(code, 2, out + err)
        self.assertIn(f"拒绝启动：发行清单核对失败（{error_code}）：", err)
        self.assertIn("trust sign-release --root-key", err)
        model.assert_not_called()
        probe.assert_not_called()
        sandbox.assert_not_called()
        with Ledger(self.ledger_path) as ledger:
            events = ledger.events()
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual((event.task_id, event.kind), ("m2k.startup", "startup.refused"))
            self.assertEqual(event.payload["task"], "t43")
            self.assertEqual(event.payload["code"], error_code)
            self.assertTrue(event.payload["reason"])
            self.assertEqual(ledger.events("t43"), [])
            self.assertEqual(ledger.events(kind="task.submitted"), [])
            self.assertTrue(ledger.verify_chain()[0])
        self.restore()
        worker, reviewer = self.run_scripted()
        self.assertTrue(worker.calls)
        self.assertTrue(reviewer.calls)
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.last("t43", "verdict").payload["verdict"], "PASS")
            self.assertEqual(len(ledger.events("m2k.startup", "startup.refused")), 1)

    def test_dev_manifest_runs_to_pass(self):
        self.run_scripted()

    def assert_chain_startup_refused(self, expected):
        worker, reviewer = ScriptedModel([], "worker"), ScriptedModel([], "reviewer")
        with patch("m2k.cli.build_model", side_effect=[worker, reviewer]) as models, \
                patch("m2k.external.CliAgent.probe") as probe:
            try:
                code, out, err = self.cli("run", self.task)
            except SystemExit as e:
                self.fail(f"trust failure escaped as SystemExit: {e.code}")
        self.assertEqual(code, 2, out + err)
        self.assertIn(f"拒绝启动：信任链核对失败（{expected}）：", err)
        models.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(worker.calls + reviewer.calls, [])
        with Ledger(self.ledger_path) as ledger:
            refused = ledger.last("m2k.startup", "startup.refused")
            self.assertIsNotNone(refused)
            self.assertEqual(refused.payload["code"], expected)
            self.assertEqual(refused.payload["task"], "t43")
            self.assertTrue(refused.payload["reason"])
            self.assertEqual(ledger.events(kind="task.submitted"), [])
            self.assertTrue(ledger.verify_chain()[0])

    def test_charter_signed_by_another_root_refuses_before_models_or_probe(self):
        other, _ = trust.generate_keypair()
        (self.keys / "charter.json").write_bytes(trust.canonical(trust.sign(self.setup.charter_env["body"], other)))
        self.assert_chain_startup_refused("CHARTER_SIGNATURE")

    def test_load_setup_trust_error_is_recorded_as_startup_refusal(self):
        (self.keys / "issuer.pem").write_bytes(b"not an Ed25519 private key")
        self.assert_chain_startup_refused("KEY")

    def test_second_release_verification_trust_error_is_recorded_as_startup_refusal(self):
        verify, calls = trust.verify_release, []

        def fail_second(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise trust.TrustError("RELEASE_SIGNATURE", "release verification failed during setup")
            return verify(*args, **kwargs)

        with patch("m2k.cli.trust.verify_release", side_effect=fail_second):
            self.assert_chain_startup_refused("RELEASE_SIGNATURE")
        self.assertEqual(len(calls), 2)

    def test_malformed_release_witness_refusal_has_its_own_code_and_no_resign_hint(self):
        reason = "malformed release.checked: invalid slot"
        with patch.object(obs.ObsClient, "witness", side_effect=obs.ObsRefused(reason)):
            self.startup_refused("OBS_REFUSED", reason)
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.last("m2k.startup", "startup.refused").payload["reason"], reason)
        with patch.object(obs.ObsClient, "witness", side_effect=obs.ObsRefused(reason)):
            code, _, err = self.cli("run", self.task)
        self.assertEqual(code, 2)
        self.assertNotIn("sign-release", err)

    def test_trust_status_bad_manifest_signature_is_invalid_not_missing(self):
        other, _ = trust.generate_keypair()
        self.sign(key=other)
        code, out, err = self.cli("trust", "status")
        self.assertEqual(code, 1, out + err)
        self.assertIn("release manifest: invalid (RELEASE_SIGNATURE)\n", out)
        self.assertNotIn("release manifest: missing", out)

    def replace_root(self, production=False):
        self.setup, self.root_key = trust.dev_setup(self.root / "next-root", CAPS)
        self.pin.write_bytes(trust.canonical({"trust_domain": "TEST_ONLY",
                                             "public_key_b64": trust.public_key_b64(self.root_key)}))
        (self.keys / "charter.json").write_bytes(trust.canonical(self.setup.charter_env))
        (self.keys / "issuer.pem").write_bytes(self.setup.issuer_key_path.read_bytes())
        self.root_pem.write_bytes(self.pem(self.root_key))
        self.write_revocations(1)
        if production:
            self.make_production()
        self.sign(**({"trust_domain": "PRODUCTION", "code": trust.release_digest(CODE)} if production else {}))

    def root_swap_then_new_epoch(self, production):
        self.anchors(production=production)
        self.run_scripted()
        first = trust.fingerprint(self.root_key.public_key())
        self.replace_root(production)
        current = trust.fingerprint(self.root_key.public_key())
        domain = "PRODUCTION" if production else "TEST_ONLY"
        self.set_task_id("swapped")
        self.startup_refused("ROOT_CHANGED", f"root changed: {domain} first seen {first}, now {current}", "swapped")
        code, out, err = self.cli("trust", "status")
        self.assertEqual(code, 1, out + err)
        self.assertEqual(out.splitlines()[0], f"\033[1mRoot 指纹：{current}（{domain}）\033[0m")
        self.assertIn(f"obs: ok\nobs root: changed (OBS remembers {first}, config uses {current})\n", out)
        self.assertEqual(json.loads(out[out.index("{"):])["Root fingerprint"], current)

        archived = {}
        for path in (self.ledger_path, self.root / "obs.jsonl"):
            saved = path.with_name("old-" + path.name)
            archived[saved] = path.read_bytes()
            path.rename(saved)
        old_files = {p: p.read_bytes() for p in (self.repo / "anchors").glob("*.jsonl")}
        code, block, err = self.cli("obs", "keygen", "--out", self.root / "epoch-obs")
        self.assertEqual(code, 0, err)
        self.obs_public = trust.public_key_b64(trust.load_private_key(self.root / "epoch-obs/obs-key.pem"))
        self.config.write_bytes(self.config.read_bytes().replace(self.obs_block, block.encode("utf-8"))
                                .replace(b'ledger = "ledger.sqlite"', b'ledger = "epoch.sqlite"')
                                .replace(b'file = "anchors.jsonl"', b'file = "epoch-anchors.jsonl"'))
        self.ledger_path = self.root / "epoch.sqlite"
        self.sign(**({"trust_domain": domain, "code": trust.release_digest(CODE)} if production else {}))
        self.set_task_id("new-epoch")
        code, out, err = self.anchored_run()
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"\033[1mRoot 指纹：{current}（{domain}，OBS 已确认与首次见到的 Root 一致）\033[0m\n", err)
        self.assertNotIn("外部锚点核对失败", err)
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.last("new-epoch", "gate.adjudication").payload["verdict"], "PASS")
            new_file = self.repo / "anchors" / f"{ledger.ledger_id[:12]}.jsonl"
            self.assertTrue(new_file.is_file())
            self.assertNotIn(new_file, old_files)
        for path, content in (archived | old_files).items():
            self.assertEqual(path.read_bytes(), content)
        code, out, err = self.cli("trust", "status")
        self.assertEqual(code, 0, out + err)
        self.assertIn("obs: ok\nobs root: ok\n", out)

    def test_root_swap_refused_and_new_epoch_passes_test_only(self):
        self.root_swap_then_new_epoch(False)

    def test_root_swap_refused_and_new_epoch_passes_production(self):
        self.root_swap_then_new_epoch(True)

    def test_pinned_manifest_runs_to_pass(self):
        self.sign(code=trust.release_digest(CODE))
        self.run_scripted()

    def test_config_byte_changed_is_refused(self):
        self.config.write_bytes(self.config.read_bytes().replace(b'model = "reviewer"', b'model = "reviewex"'))
        self.refuse_then_recover("RELEASE_CONFIG")

    def test_obs_public_key_swap_is_refused(self):
        public = self.obs_public
        self.sign()
        self.save_good()
        other_public = trust.generate_keypair()[1]
        self.config.write_bytes(self.config.read_bytes().replace(public.encode(), other_public.encode()))
        self.refuse_then_recover("RELEASE_CONFIG")

    def test_valid_manifest_without_obs_is_refused_before_models_probe_or_sandbox(self):
        self.config.write_bytes(self.config.read_bytes().replace(self.obs_block, b""))
        self.sign()
        trust.verify_release(self.setup.pin, "TEST_ONLY", json.loads(self.release.read_bytes()),
                             CODE, self.config.read_bytes())
        with patch("m2k.cli.build_model") as model, patch("m2k.external.CliAgent.probe") as probe, \
                patch("m2k.cli.sandboxes.choose") as sandbox:
            code, out, err = self.cli("run", self.task)
        self.assertEqual(code, 2, out + err)
        self.assertIn("拒绝启动：配置了信任链但没有 OBS 独立旁证（[obs]）。", err)
        self.assertIn("m2k obs keygen --out <dir>", err)
        self.assertIn("m2k trust sign-release", err)
        model.assert_not_called()
        probe.assert_not_called()
        sandbox.assert_not_called()
        with Ledger(self.ledger_path) as ledger:
            events = ledger.events()
            self.assertEqual(len(events), 1)
            self.assertEqual((events[0].task_id, events[0].kind), ("m2k.startup", "startup.refused"))
            self.assertEqual(events[0].payload["code"], "OBS_REQUIRED")
            self.assertEqual(events[0].payload["task"], "t43")
            self.assertTrue(events[0].payload["reason"])
            self.assertEqual(ledger.events("t43"), [])
            self.assertEqual(ledger.events(kind="task.submitted"), [])
        self.restore()
        self.run_scripted()

    def test_trust_status_obs_states(self):
        for state in ("ok", "missing", "key mismatch"):
            self.restore()
            if state == "missing":
                self.config.write_bytes(self.config.read_bytes().replace(self.obs_block, b""))
            elif state == "key mismatch":
                other = trust.generate_keypair()[1]
                self.config.write_bytes(self.config.read_bytes().replace(self.obs_public.encode(), other.encode()))
            self.sign()
            with self.subTest(state=state):
                code, out, err = self.cli("trust", "status")
                self.assertEqual(code, 0 if state == "ok" else 1, out + err)
                self.assertIn("release manifest: ok\n", out)
                label = "ok (log not yet created)" if state == "ok" else state
                self.assertIn(f"obs: {label}\n", out)

    def test_init_dev_blocks_and_printed_sign_command_produce_an_accepted_config(self):
        directory = self.root / "new keys"
        code, out, err = self.cli("trust", "init-dev", "--out", directory)
        self.assertEqual(code, 0, err)
        block = out[out.index("[trust]"):out.index("TEST_ONLY：")]
        self.config.write_bytes(self.config.read_bytes().split(b"[trust]", 1)[0] + block.encode("utf-8"))
        command = next(line.split("运行：", 1)[1] for line in out.splitlines() if "sign-release --root-key" in line)
        self.assertEqual(self.cli(*shlex.split(command)[1:])[0], 0)
        code, out, err = self.cli("trust", "status")
        self.assertEqual(code, 0, out + err)
        self.assertIn("obs: ok (log not yet created)\n", out)
        self.assertTrue((directory / "obs/obs-key.pem").is_file())
        self.run_scripted()

    def test_missing_release_key_is_refused(self):
        self.config.write_bytes(self.config.read_bytes().replace(b'release = "keys/release.json"\n', b''))
        self.refuse_then_recover("RELEASE_SIGNATURE")

    def test_missing_release_file_is_refused(self):
        self.release.unlink()
        self.refuse_then_recover("RELEASE_SIGNATURE")

    def test_another_root_signature_is_refused(self):
        self.sign(key=trust.generate_keypair()[0])
        self.refuse_then_recover("RELEASE_SIGNATURE")

    def test_other_domain_is_refused(self):
        self.sign(trust_domain="PRODUCTION")
        self.refuse_then_recover("RELEASE_SIGNATURE")

    def make_production(self):
        self.pin.write_bytes(trust.canonical({"trust_domain": "PRODUCTION",
                                             "public_key_b64": trust.public_key_b64(self.root_key)}))
        self.config.write_bytes(self.config.read_bytes().replace(b'TEST_ONLY', b'PRODUCTION'))
        charter = self.setup.charter_env["body"] | {"trust_domain": "PRODUCTION", "release": trust.release_digest(CODE)}
        (self.keys / "charter.json").write_bytes(trust.canonical(trust.sign(charter, self.root_key)))
        revocations = self.setup.revocations()["body"] | {"trust_domain": "PRODUCTION"}
        (self.keys / "revocations.json").write_bytes(trust.canonical(trust.sign(revocations, self.root_key)))

    def test_production_dev_is_refused(self):
        self.make_production()
        self.sign(trust_domain="PRODUCTION")
        self.refuse_then_recover("RELEASE_CODE")

    def test_changed_pinned_code_is_refused(self):
        self.sign(code=trust.release_digest(CODE))
        with patch("m2k.trust.release_digest", return_value="changed temporary code"):
            self.refuse_then_recover("RELEASE_CODE")

    def test_crlf_config_verifies_lf_manifest_and_runs(self):
        self.config.write_bytes(self.config.read_bytes().replace(b"\n", b"\r\n"))
        self.run_scripted()

    def test_verify_release_checks_code_in_a_temporary_directory_and_production_pin(self):
        code = self.root / "code"
        code.mkdir()
        source = code / "module.py"
        source.write_bytes(b"value = 1\n")
        self.sign(code=trust.release_digest(code), trust_domain="PRODUCTION")
        pin = trust.Pin(self.root_key.public_key(), "PRODUCTION", self.setup.pin.fingerprint)
        env = json.loads(self.release.read_bytes())
        trust.verify_release(pin, "PRODUCTION", env, code, self.config.read_bytes())
        source.write_bytes(b"value = 2\n")
        with self.assertRaises(trust.TrustError) as stopped:
            trust.verify_release(pin, "PRODUCTION", env, code, self.config.read_bytes())
        self.assertEqual(stopped.exception.code, "RELEASE_CODE")

    def test_invalid_schema_signature_and_body_are_release_signature(self):
        raw = self.config.read_bytes()
        body = json.loads(self.release.read_bytes())["body"]
        cases = [None, [], {}, trust.sign(body | {"schema": trust.CHARTER}, self.root_key),
                 trust.sign({k: v for k, v in body.items() if k != "issued_at"}, self.root_key),
                 trust.sign(body, self.root_key) | {"schema": trust.CHARTER},
                 trust.sign(body, self.root_key) | {"signature_b64": "invalid"}]
        for env in cases:
            with self.subTest(env=env), self.assertRaises(trust.TrustError) as stopped:
                trust.verify_release(self.setup.pin, "TEST_ONLY", env, CODE, raw)
            self.assertEqual(stopped.exception.code, "RELEASE_SIGNATURE")
        for invalid in (b"{", b"\xff", b'{"schema":1,"schema":2}'):
            self.release.write_bytes(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(trust.TrustError) as stopped:
                trust.check_release({"release": "keys/release.json"}, self.root, CODE, raw)
            self.assertEqual(stopped.exception.code, "RELEASE_SIGNATURE")

    def test_sign_release_defaults_to_current_code_and_never_overwrites(self):
        for flags in ([], ["--code", "dev"]):
            out = self.root / ("dev.json" if flags else "pinned.json")
            args = ["trust", "sign-release", "--root-key", self.root_pem, "--domain", "TEST_ONLY", "--slot", "test", "--out", out, *flags]
            code, stdout, err = self.cli(*args)
            self.assertEqual(code, 0, stdout + err)
            body = trust.open_envelope(json.loads(out.read_bytes()), self.root_key.public_key())
            self.assertEqual(set(body), {"schema", "trust_domain", "manifest_id", "slot", "version", "code", "config", "issued_at"})
            self.assertEqual(body["schema"], "m2k.release/2")
            self.assertEqual((body["slot"], body["version"]), ("test", 2))
            self.assertTrue(body["manifest_id"])
            self.assertIs(type(body["issued_at"]), int)
            self.assertEqual(body["code"], "dev" if flags else trust.release_digest(CODE))
            self.assertEqual(body["config"], hashlib.sha256(self.config.read_bytes()).hexdigest())
            original = out.read_bytes()
            self.assertEqual(self.cli(*args)[0], 1)
            self.assertEqual(out.read_bytes(), original)

    def test_sign_release_refuses_production_dev_before_loading_key(self):
        out = self.root / "production.json"
        with patch("m2k.cli.trust.load_private_key") as load_key:
            code, _, err = self.cli("trust", "sign-release", "--root-key", "absent.pem", "--domain", "PRODUCTION",
                                     "--code", "dev", "--slot", "test", "--version", "1", "--out", out)
        self.assertEqual(code, 2, err)
        self.assertIn("PRODUCTION cannot use a dev release", err)
        load_key.assert_not_called()
        self.assertFalse(out.exists())

    def test_trust_status_release_states(self):
        for state in ("ok", "config changed", "code changed", "missing"):
            self.restore()
            if state == "config changed":
                self.config.write_bytes(self.config.read_bytes() + b"\n")
            elif state == "code changed":
                self.sign(code="0" * 64)
            elif state == "missing":
                self.release.unlink()
            with self.subTest(state=state):
                code, out, err = self.cli("trust", "status")
                self.assertEqual(code, 0 if state == "ok" else 1, out + err)
                self.assertIn("release manifest: " + state + "\n", out)
        self.restore()
        self.config.write_bytes(self.config.read_bytes().replace(b'release = "keys/release.json"\n', b''))
        code, out, err = self.cli("trust", "status")
        self.assertEqual(code, 1, err)
        self.assertIn("release manifest: missing", out)

    def test_production_status_reports_code_changed_before_charter_rejection(self):
        self.make_production()
        self.sign(trust_domain="PRODUCTION", code=trust.release_digest(CODE))
        with patch("m2k.trust.release_digest", return_value="changed code"):
            code, out, err = self.cli("trust", "status")
        self.assertEqual(code, 1, out + err)
        self.assertIn("release manifest: code changed\n", out)
        self.assertIn("RELEASE_CODE", err)

    def test_init_dev_prints_release_and_signing_command_without_signing_config(self):
        out = self.root / "new keys"
        code, stdout, err = self.cli("trust", "init-dev", "--out", out)
        self.assertEqual(code, 0, err)
        self.assertIn('release = ' + json.dumps((out / "release.json").as_posix()), stdout)
        self.assertIn('m2k trust sign-release --root-key ' + json.dumps((out / "root.pem").as_posix()), stdout)
        self.assertIn('--domain TEST_ONLY --code dev --slot dev --version 1 --out ' + json.dumps((out / "release.json").as_posix()), stdout)
        self.assertFalse((out / "release.json").exists())

    def test_fingerprint_and_single_config_read_precede_probe(self):
        reads, snapshots = [], []
        read_bytes = Path.read_bytes

        def read(path):
            raw = read_bytes(path)
            if path.absolute() == self.config:
                reads.append(raw)
            return raw

        def compute(code_dir, files, contents):
            self.assertFalse(self.ledger_path.exists())
            self.assertEqual(reads, [self.good[self.config]])
            self.assertIn(self.release, files)
            self.assertEqual(contents[self.config], self.good[self.config])
            snapshots.append(framework_fingerprint(code_dir, files, contents))
            return snapshots[-1]

        def probe():
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(reads, [self.good[self.config]])

        with patch.object(Path, "read_bytes", read), patch("m2k.cli.framework_fingerprint", side_effect=compute):
            self.run_scripted(probe=probe)
        with Ledger(self.ledger_path) as ledger:
            submitted = ledger.last("t43", "task.submitted").payload["framework"]
            self.assertEqual(submitted, snapshots[0])
            self.assertEqual(submitted["files"][str(self.release)], hashlib.sha256(self.good[self.release]).hexdigest())

    def test_config_and_manifest_swaps_during_probe_are_caught_before_model_calls(self):
        for path in (self.config, self.release):
            self.restore()

            def probe():
                path.write_bytes(path.read_bytes() + b"\n")

            worker, reviewer = ScriptedModel([], "worker"), ScriptedModel([], "reviewer")
            worker.external = True
            worker.probe = Mock(side_effect=probe)
            with self.subTest(path=path.name), patch("m2k.cli.build_model", side_effect=[worker, reviewer]):
                with self.assertRaisesRegex(SystemExit, "FRAMEWORK_CHANGED"):
                    self.cli("run", self.task)
                worker.probe.assert_called_once_with()
                self.assertEqual(worker.calls, [])
                self.assertEqual(reviewer.calls, [])
                with Ledger(self.ledger_path) as ledger:
                    self.assertEqual(ledger.events("t43", "task.submitted"), [])
                    self.assertEqual(ledger.events("t43", "delivery.intent"), [])

    def test_bytes_checked_before_a_swap_are_the_bytes_pinned_in_the_fingerprint(self):
        check_release = trust.check_release
        snapshots = []

        def check(section, base, code_dir, config_bytes):
            contents = check_release(section, base, code_dir, config_bytes)
            self.config.write_bytes(self.good[self.config] + b"\n")
            self.release.write_bytes(self.good[self.release] + b"\n")
            return contents

        def compute(code_dir, files, contents):
            snapshot = framework_fingerprint(code_dir, files, contents)
            for path in (self.config, self.release):
                self.assertEqual(snapshot["files"][str(path)], hashlib.sha256(self.good[path]).hexdigest())
            snapshots.append(snapshot)
            return snapshot

        worker, reviewer = ScriptedModel([], "worker"), ScriptedModel([], "reviewer")
        with patch("m2k.cli.trust.check_release", side_effect=check), \
                patch("m2k.cli.framework_fingerprint", side_effect=compute), \
                patch("m2k.cli.build_model", side_effect=[worker, reviewer]):
            with self.assertRaisesRegex(SystemExit, "FRAMEWORK_CHANGED"):
                self.cli("run", self.task)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(worker.calls, [])
        self.assertEqual(reviewer.calls, [])

    def test_manifest_swap_during_task_is_caught(self):
        def change(messages):
            self.release.write_bytes(self.release.read_bytes() + b"\n")
            return step("finish")

        self.run_scripted(expected=1, replies=[change])
        with Ledger(self.ledger_path) as ledger:
            self.assertIn("FRAMEWORK_CHANGED", ledger.last("t43", "verdict").payload["reasons"][0])
            self.assertEqual(ledger.events("t43", "delivery.intent"), [])

    def anchors(self, production=False):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        self.repo, self.anchors_path = self.root / "anchor-clone", self.root / "anchors.jsonl"
        subprocess.run(["git", "init", "--initial-branch=ledger-anchors", str(self.repo)], check=True, capture_output=True)
        for key, value in (("user.name", "Anchor Test"), ("user.email", "anchor@example.invalid"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            anchor.git(self.repo, "config", key, value)
        anchor.git(self.repo, "commit", "--allow-empty", "-m", "initial")
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        anchor.git(self.repo, "remote", "add", "origin", str(remote))
        anchor.git(self.repo, "push", "origin", "ledger-anchors")
        self.config.write_bytes(self.config.read_bytes() + b'[anchor]\nfile = "anchors.jsonl"\nrepo_dir = "anchor-clone"\n')
        self.task.write_bytes(self.task.read_bytes().replace(b'workspace = "ws"\n', b'workspace = "ws"\ndeliver_to = "out"\n'))
        if production:
            self.make_production()
        self.sign(**({"trust_domain": "PRODUCTION", "code": trust.release_digest(CODE)} if production else {}))
        self.api = self.enterContext(patch("m2k.anchor.activity", return_value=[]))

    def anchored_run(self, replies=None):
        models = [ScriptedModel(replies or [step("finish")], "worker"), ScriptedModel([review("GO")], "reviewer")]
        with patch("m2k.cli.build_model", side_effect=models):
            return self.cli("run", self.task)

    def test_production_requires_external_anchor_repo_before_models_probe_or_sandbox(self):
        self.make_production()
        self.sign(trust_domain="PRODUCTION", code=trust.release_digest(CODE))
        with patch("m2k.cli.build_model") as models, patch("m2k.cli.sandboxes.choose") as sandbox, \
                patch("m2k.external.CliAgent.probe") as probe:
            code, out, err = self.cli("run", self.task)
        self.assertEqual(code, 2, out + err)
        self.assertIn("ANCHOR_REQUIRED", err)
        models.assert_not_called()
        sandbox.assert_not_called()
        probe.assert_not_called()
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.events(kind="task.submitted"), [])
            self.assertEqual(ledger.last("m2k.startup", "startup.refused").payload["code"], "ANCHOR_REQUIRED")

    def test_checkpoint_is_published_before_delivery_and_remote_cli_reads_obs_once(self):
        self.anchors()
        code, out, err = self.anchored_run()
        self.assertEqual(code, 0, out + err)
        records = [json.loads(raw) for raw in self.anchors_path.read_bytes().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["v"], 2)
        with Ledger(self.ledger_path) as ledger:
            checkpoint = ledger.last("t43", "anchor.checkpoint")
            published = ledger.last("t43", "anchor.published")
            self.assertLess(ledger.last("t43", "gate.adjudication").seq, checkpoint.seq)
            self.assertLess(checkpoint.seq, published.seq)
            self.assertLess(ledger.last("t43", "delivery.intent").seq, ledger.last("t43", "delivery.result").seq)
            self.assertLess(ledger.last("t43", "delivery.result").seq, checkpoint.seq)
            self.assertEqual(published.payload, {"seq": checkpoint.seq, "commit": anchor.git(self.repo, "rev-parse", "HEAD")})
            self.assertEqual(records[0]["receipt"]["body"]["ledger_hash"], checkpoint.hash)
            self.assertTrue(ledger.last("t43", "delivery.result").payload["post_verified"])
        reads, read_bytes = [], Path.read_bytes

        def read(path):
            if path == self.root / "obs.jsonl":
                reads.append(path)
            return read_bytes(path)

        with patch.object(Path, "read_bytes", read):
            code, out, err = self.cli("check-ledger", "--remote")
        self.assertEqual(code, 0, out + err)
        self.assertIn("remote anchors OK", out)
        self.assertEqual(reads, [self.root / "obs.jsonl"])

    def test_production_remote_problem_refuses_before_models_probe_sandbox_or_submission(self):
        self.anchors(production=True)
        self.api.return_value = [{"activity_type": "force_push", "actor": {"login": "owner"}, "timestamp": "now"}]
        with patch("m2k.cli.build_model") as models, patch("m2k.cli.sandboxes.choose") as sandbox, \
                patch("m2k.external.CliAgent.probe") as probe:
            code, out, err = self.cli("run", self.task)
        self.assertEqual(code, 2, out + err)
        self.assertIn("ANCHOR_CHECK", err)
        models.assert_not_called()
        sandbox.assert_not_called()
        probe.assert_not_called()
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.events(kind="task.submitted"), [])
            self.assertEqual(ledger.last("m2k.startup", "startup.refused").payload["code"], "ANCHOR_CHECK")

    def publish_failure(self, production):
        self.anchors(production=production)

        def disconnect(messages):
            anchor.git(self.repo, "remote", "set-url", "origin", str(self.root / "missing.git"))
            return step("finish")

        code, out, err = self.anchored_run([disconnect])
        self.assertEqual(code, 2 if production else 0, out + err)
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.last("t43", "verdict").payload["verdict"], "PASS")
            self.assertIsNotNone(ledger.last("t43", "anchor.failed"))
            self.assertEqual(ledger.events("t43", "anchor.published"), [])
            if production:
                self.assertIn("ANCHOR_UNAVAILABLE", ledger.last("t43", "delivery.refused").payload["reason"])
                self.assertEqual(len(ledger.events("t43", "delivery.intent")), 1)
                self.assertTrue(ledger.last("t43", "delivery.result").payload["post_verified"])
                self.assertFalse((self.root / "out" / "t43").exists())
                self.assertFalse((self.root / "out" / ".m2k-staging-t43").exists())
                self.assertIn("t43: UNKNOWN", out)
            else:
                self.assertIn("\033[31m警告：外部锚点未发布：", err)
                self.assertTrue(ledger.last("t43", "delivery.result").payload["post_verified"])

    def test_production_publish_unreachable_refuses_delivery(self):
        self.publish_failure(True)

    def test_production_publish_failure_preserves_kernel_fail_and_exit_one(self):
        self.anchors(production=True)
        (self.root / "ws" / "check.py").write_bytes(b"raise SystemExit(1)\n")

        def disconnect(messages):
            anchor.git(self.repo, "remote", "set-url", "origin", str(self.root / "missing.git"))
            return step("finish")

        code, out, err = self.anchored_run([disconnect])
        self.assertEqual(code, 1, out + err)
        self.assertIn("t43: FAIL", out)
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.last("t43", "verdict").payload["verdict"], "FAIL")
            self.assertIsNotNone(ledger.last("t43", "anchor.failed"))
            self.assertIn("ANCHOR_UNAVAILABLE", ledger.last("t43", "delivery.refused").payload["reason"])
            self.assertEqual(ledger.events("t43", "delivery.intent"), [])
        self.assertFalse((self.root / "out" / "t43").exists())

    def test_test_only_publish_unreachable_warns_and_delivers(self):
        self.publish_failure(False)

    def test_test_only_remote_failure_is_warned_recorded_and_delivery_continues(self):
        self.anchors()
        self.api.side_effect = OSError("API unreachable")
        code, out, err = self.anchored_run()
        self.assertEqual(code, 0, out + err)
        self.assertIn("\033[31m警告：外部锚点核对失败：API unreachable", err)
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.last("m2k.startup", "anchor.warning").payload,
                             {"task": "t43", "problems": ["API unreachable"]})
            self.assertIsNotNone(ledger.last("t43", "anchor.published"))
            self.assertTrue(ledger.last("t43", "delivery.result").payload["post_verified"])

    def test_two_runs_in_fresh_logs_with_same_key_need_this_ledger_only_and_startup_remains_full(self):
        self.anchors()
        self.assertEqual(self.anchored_run()[0], 0)
        self.config.write_bytes(self.config.read_bytes().replace(b'ledger = "ledger.sqlite"', b'ledger = "second.sqlite"')
                                .replace(b'log = "obs.jsonl"', b'log = "second-obs.jsonl"')
                                .replace(b'file = "anchors.jsonl"', b'file = "second-anchors.jsonl"'))
        self.task.write_bytes(self.task.read_bytes().replace(b'id = "t43"', b'id = "second"'))
        self.sign()
        code, out, err = self.anchored_run()
        self.assertEqual(code, 0, out + err)
        self.assertIn("OBS log replaced or truncated", err)
        code, out, err = self.cli("check-ledger", "--remote")
        self.assertEqual(code, 1, out + err)
        self.assertIn("OBS log replaced or truncated", out)
        code, out, err = self.cli("check-ledger", "--remote", "--this-ledger-only")
        self.assertEqual(code, 0, out + err)
        self.assertIn("remote anchors OK", out)
        with Ledger(self.root / "second.sqlite") as ledger:
            self.assertIsNotNone(ledger.last("m2k.startup", "anchor.warning"))
            self.assertIsNotNone(ledger.last("second", "anchor.published"))

    def test_test_only_file_without_repo_writes_checkpoint_and_repo_override_checks_it(self):
        self.anchors()
        self.config.write_bytes(self.config.read_bytes().replace(b'repo_dir = "anchor-clone"\n', b''))
        self.sign()
        code, out, err = self.anchored_run()
        self.assertEqual(code, 0, out + err)
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.events("t43", "anchor.failed"), [])
            self.assertEqual(ledger.events("t43", "anchor.published"), [])
        anchor.publish(self.anchors_path, self.repo, push=True)
        self.assertEqual(self.cli("check-ledger", "--remote", "--repo-dir", self.repo, "--this-ledger-only")[0], 0)

    def test_anchor_repo_inside_a_workspace_is_refused_before_models(self):
        self.anchors()
        self.config.write_bytes(self.config.read_bytes().replace(b'repo_dir = "anchor-clone"', b'repo_dir = "ws/anchor-clone"'))
        self.sign()
        with patch("m2k.cli.build_model") as models, self.assertRaisesRegex(SystemExit, "inside the workspace"):
            self.cli("run", self.task)
        models.assert_not_called()
        self.api.assert_not_called()


    def set_task_id(self, task_id):
        lines = self.task.read_bytes().splitlines(keepends=True)
        self.task.write_bytes(b"".join(f'id = "{task_id}"\n'.encode() if line.startswith(b"id = ") else line for line in lines))

    def startup_refused(self, code, reason, task_id="t43"):
        log = self.root / "obs.jsonl"
        before = log.read_bytes() if log.exists() else b""
        with patch("m2k.cli.build_model") as models, patch("m2k.external.CliAgent.probe") as probe, \
                patch("m2k.cli.sandboxes.choose") as sandbox:
            result, out, err = self.cli("run", self.task)
        self.assertEqual(result, 2, out + err)
        self.assertIn(code, err)
        self.assertIn(reason, err)
        models.assert_not_called()
        probe.assert_not_called()
        sandbox.assert_not_called()
        with Ledger(self.ledger_path) as ledger:
            refused = ledger.last("m2k.startup", "startup.refused")
            self.assertEqual(refused.payload["task"], task_id)
            self.assertEqual(refused.payload["code"], code)
            self.assertIn(reason, refused.payload["reason"])
            if code in ("RELEASE_ROLLBACK", "ROOT_CHANGED"):
                self.assertEqual(refused.payload["reason"], reason)
            if code == "RELEASE_ROLLBACK":
                self.assertIn("sign-release", err)
                self.assertIn("--version", err)
            elif code == "ROOT_CHANGED":
                self.assertIn("README", err)
                self.assertIn("换 Root（合法做法）", err)
            self.assertEqual(ledger.events(task_id, "task.submitted"), [])
            self.assertTrue(ledger.verify_chain()[0])
        self.assertEqual(log.read_bytes() if log.exists() else b"", before)

    def write_revocations(self, version, releases=()):
        body = self.setup.revocations()["body"] | {"version": version, "revoked_releases": list(releases)}
        (self.keys / "revocations.json").write_bytes(trust.canonical(trust.sign(body, self.root_key)))

    def rollback_and_recover(self, production):
        if production:
            self.anchors(production=True)
        changes = {"trust_domain": "PRODUCTION", "code": trust.release_digest(CODE)} if production else {}
        self.sign(slot="a", version=1, manifest_id="a1", **changes)
        old_config, old_manifest = self.config.read_bytes(), self.release.read_bytes()
        self.config.write_bytes(old_config + b"# version 2 config\n")
        self.sign(slot="a", version=2, manifest_id="a2", **changes)
        self.run_scripted()
        self.config.write_bytes(old_config)
        self.release.write_bytes(old_manifest)
        for task_id in ("rollback", "rollback-after-another-restart"):
            self.set_task_id(task_id)
            self.startup_refused("RELEASE_ROLLBACK", "release rollback", task_id)
        self.sign(slot="a", version=3, manifest_id="a3", **changes)
        self.assertEqual(self.config.read_bytes(), old_config)
        self.set_task_id("recovered")
        self.run_scripted()
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.last("recovered", "gate.adjudication").payload["verdict"], "PASS")

    def test_rollback_refused_after_restart_and_old_config_resigned_as_v3_runs_test_only(self):
        self.rollback_and_recover(False)

    def test_rollback_refused_after_restart_and_old_config_resigned_as_v3_runs_production(self):
        self.rollback_and_recover(True)

    def test_slots_are_independent_and_equal_versions_of_the_same_manifest_run(self):
        self.sign(slot="a", version=2, manifest_id="a2")
        self.run_scripted()
        self.set_task_id("same-manifest")
        self.run_scripted()
        self.sign(slot="b", version=1, manifest_id="b1")
        self.set_task_id("other-slot")
        self.run_scripted()

    def test_different_manifests_at_the_same_slot_version_are_refused(self):
        self.sign(slot="a", version=2, manifest_id="first")
        self.run_scripted()
        self.sign(slot="a", version=2, manifest_id="second")
        self.set_task_id("conflict")
        self.startup_refused("RELEASE_ROLLBACK", "two manifests share slot a version 2", "conflict")

    def test_revoked_manifest_is_refused_before_any_model_probe_or_task(self):
        self.write_revocations(1, ["test-release"])
        self.refuse_then_recover("RELEASE_REVOKED")

    def test_numeric_revocations_and_an_owner_signed_config_without_a_list_alternate(self):
        self.write_revocations(3)
        self.sign(slot="with-list", manifest_id="with-list")
        with_list = self.config.read_bytes(), self.release.read_bytes()
        self.run_scripted()
        self.config.write_bytes(with_list[0].replace(b'revocations = "keys/revocations.json"\n', b''))
        self.sign(slot="without-list", manifest_id="without-list")
        without_list = self.config.read_bytes(), self.release.read_bytes()
        for task_id, content in (("without", without_list), ("with-again", with_list), ("without-again", without_list)):
            self.config.write_bytes(content[0])
            self.release.write_bytes(content[1])
            self.set_task_id(task_id)
            self.run_scripted()
        self.config.write_bytes(with_list[0])
        self.release.write_bytes(with_list[1])
        self.write_revocations(2)
        self.set_task_id("old-list")
        self.startup_refused("RELEASE_ROLLBACK", "revocations rollback", "old-list")

    def test_production_requires_revocations_before_obs_models_probe_or_sandbox(self):
        self.make_production()
        self.config.write_bytes(self.config.read_bytes().replace(b'revocations = "keys/revocations.json"\n', b''))
        self.sign(trust_domain="PRODUCTION", code=trust.release_digest(CODE))
        self.startup_refused("REVOCATIONS_REQUIRED", "PRODUCTION requires [trust] revocations")

    def test_dead_real_witness_at_release_check_is_obs_unavailable_and_closed(self):
        client_class = obs.ObsClient
        clients = []

        def start(*args, **kwargs):
            client = client_class(*args, **kwargs)
            clients.append(client)
            client.process.kill()
            client.process.wait()
            return client

        with patch("m2k.cli.obs.ObsClient", side_effect=start):
            self.startup_refused("OBS_UNAVAILABLE", "witness unavailable")
        self.assertEqual(len(clients), 1)
        self.assertIsNotNone(clients[0].process.poll())
        self.assertTrue(clients[0].process.stdin.closed)
        self.assertTrue(clients[0].process.stdout.closed)

    def test_release_is_witnessed_before_probe_and_checked_by_the_gate_and_cli(self):
        def probe():
            self.assertIn(f"\033[1mRoot 指纹：{self.setup.pin.fingerprint}（TEST_ONLY，OBS 已确认与首次见到的 Root 一致）\033[0m\n",
                          sys.stderr.getvalue())
            with Ledger(self.ledger_path) as ledger:
                event = ledger.last("m2k.startup", "release.checked")
                self.assertIsNotNone(event)
                receipt = ledger.last("m2k.startup", "obs.receipt").payload["receipt"]
                signed = obs.receipt_body(receipt, self.obs_public, obs.request(event, ledger.ledger_id))
                self.assertEqual(signed["release"], event.payload["release"])
                self.assertEqual(event.payload["release"]["revocations_version"], 0)
                self.assertEqual(ledger.events(kind="task.submitted"), [])

        with patch("m2k.kernel.adjudicate", wraps=adjudicate) as checked:
            self.run_scripted(probe=probe)
        self.assertIsNotNone(checked.call_args.kwargs["root_pin"])
        self.assertIsNotNone(checked.call_args.kwargs["revocations"])
        with Ledger(self.ledger_path) as ledger:
            submitted = ledger.last("t43", "task.submitted")
            release = submitted.payload["release"]
            event = ledger.last("m2k.startup", "release.checked")
            self.assertEqual(release, {"envelope": json.loads(self.release.read_bytes()),
                                      "config_sha256": trust.config_digest(self.config.read_bytes()), "checked_seq": event.seq})
            self.assertLess(event.seq, submitted.seq)
            self.assertEqual(ledger.last("t43", "gate.adjudication").payload["verdict"], "PASS")
        code, out, err = self.cli("adjudicate", "t43")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(out)["verdict"], "PASS")

    def test_adjudicator_reads_release_checked_without_listing_startup_history(self):
        self.run_scripted()
        with Ledger(self.ledger_path) as ledger:
            events = ledger.events

            def bounded_events(task_id=None, kind=None):
                self.assertNotEqual((task_id, kind), ("m2k.startup", "release.checked"))
                return events(task_id, kind)

            with patch.object(ledger, "events", side_effect=bounded_events):
                result = adjudicate(ledger, "t43", root_pin=self.setup.pin, revocations=self.setup.revocations(),
                                    obs_public_key=self.obs_public, obs_log=self.root / "obs.jsonl")
            self.assertEqual((result.verdict, result.reasons), ("PASS", []))

    def test_adjudicator_rejects_wrong_task_kind_or_nonmatching_checked_sequence(self):
        self.run_scripted()
        with Ledger(self.ledger_path) as ledger:
            original = ledger.last("t43", "task.submitted").payload
            checked = ledger.last("m2k.startup", "release.checked")
            impostors = [ledger.append(task, kind, checked.payload)
                         for task, kind in (("another-task", "release.checked"), ("m2k.startup", "another.kind"))]
        for checked_seq in [*(event.seq for event in impostors), str(checked.seq), [], {}, 2**63]:
            with self.subTest(checked_seq=checked_seq):
                payload = copy.deepcopy(original)
                payload["release"]["checked_seq"] = checked_seq
                result = self.amend_submitted(payload)
                self.assertEqual(result.verdict, "FAIL")
                self.assertEqual(result.reasons,
                                 ["release manifest: release.checked missing or names a different manifest or run"])

    def test_sign_release_proposes_next_version_without_comparing_previous_code_or_config(self):
        self.sign(code="0" * 64)
        self.config.write_bytes(self.config.read_bytes() + b"# config changed\n")
        target = self.root / "next.json"
        code, out, err = self.cli("trust", "sign-release", "--root-key", self.root_pem, "--domain", "TEST_ONLY",
                                  "--slot", "test", "--out", target)
        self.assertEqual(code, 0, out + err)
        self.assertIn("slot test version 2", out)
        body = trust.verify_release(self.setup.pin, "TEST_ONLY", json.loads(target.read_bytes()), CODE, self.config.read_bytes())
        self.assertEqual(body["version"], 2)

    def test_sign_release_requires_slot_and_an_explicit_version_if_current_cannot_verify(self):
        common = ("trust", "sign-release", "--root-key", self.root_pem, "--domain", "TEST_ONLY")
        with self.assertRaises(SystemExit) as stopped:
            self.cli(*common, "--version", "1", "--out", self.root / "missing-slot.json")
        self.assertEqual(stopped.exception.code, 2)
        for case in ("missing", "foreign", "domain", "slot", "v1", "invalid-version"):
            self.restore()
            if case == "missing":
                self.release.unlink()
            elif case == "foreign":
                self.sign(key=trust.generate_keypair()[0])
            elif case == "domain":
                self.sign(trust_domain="PRODUCTION")
            elif case == "slot":
                self.sign(slot="other")
            elif case == "invalid-version":
                self.sign(version=0)
            else:
                body = json.loads(self.release.read_bytes())["body"]
                del body["slot"]
                del body["version"]
                self.release.write_bytes(trust.canonical(trust.sign(body | {"schema": "m2k.release/1"}, self.root_key)))
            target = self.root / f"{case}.json"
            with self.subTest(case=case):
                code, out, err = self.cli(*common, "--slot", "test", "--out", target)
                self.assertEqual(code, 1, out + err)
                self.assertIn("--version", err)
                self.assertFalse(target.exists())
        code, out, err = self.cli(*common, "--slot", "test", "--version", "3", "--out", self.root / "explicit.json")
        self.assertEqual(code, 0, out + err)
        self.assertIn("slot test version 3", out)

    def test_manifest_slot_and_version_format_and_v1_downgrade_are_refused(self):
        good = json.loads(self.release.read_bytes())["body"]
        changes = [{"slot": value} for value in ("", "A", "-a", "a" * 33, 1, None)]
        changes += [{"version": value} for value in (0, -1, True, "1", None)]
        changes += [{"schema": "m2k.release/1"}, {"config": None}, {"code": []}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(trust.TrustError) as refused:
                trust.verify_release(self.setup.pin, "TEST_ONLY", trust.sign(good | change, self.root_key))
            self.assertEqual(refused.exception.code, "RELEASE_SIGNATURE")

    def amend_submitted(self, payload):
        with Ledger(self.ledger_path) as ledger:
            event = ledger.append("t43", "task.submitted", payload)
            with closing(obs.ObsClient([sys.executable, "-m", "m2k", "obs", "serve", "--key", str(self.keys / "obs.pem"),
                                "--log", str(self.root / "obs.jsonl")], self.obs_public)) as observer:
                receipt = observer.witness(event, ledger.ledger_id)
            ledger.append("t43", "obs.receipt", {"receipt": receipt})
            result = adjudicate(ledger, "t43", root_pin=self.setup.pin, revocations=self.setup.revocations(),
                                obs_public_key=self.obs_public, obs_log=self.root / "obs.jsonl")
        return result

    def test_adjudicator_names_swapped_envelope_config_code_missing_and_unwitnessed_checks(self):
        self.run_scripted()
        with Ledger(self.ledger_path) as ledger:
            original = ledger.last("t43", "task.submitted").payload
            checked = ledger.last("m2k.startup", "release.checked")
            unwitnessed = ledger.append("m2k.startup", "release.checked", checked.payload)
        for fault, reason in (("envelope", "different manifest"), ("config", "RELEASE_CONFIG"),
                              ("code", "RELEASE_CODE"), ("missing", "release.checked missing"),
                              ("unwitnessed", "no valid OBS receipt for release.checked")):
            payload = copy.deepcopy(original)
            record = payload["release"]
            if fault == "envelope":
                record["envelope"] = trust.sign(record["envelope"]["body"] | {"manifest_id": "another-valid-manifest"}, self.root_key)
            elif fault == "config":
                record["config_sha256"] = "0" * 64
            elif fault == "code":
                record["envelope"] = trust.sign(record["envelope"]["body"] | {"code": "0" * 64}, self.root_key)
            else:
                record["checked_seq"] = 999999 if fault == "missing" else unwitnessed.seq
            with self.subTest(fault=fault):
                result = self.amend_submitted(payload)
                self.assertEqual(result.verdict, "FAIL")
                self.assertEqual(len(result.reasons), 1, result.reasons)
                self.assertIn(reason, result.reasons[0])

    def test_cli_adjudication_rejects_a_manifest_revoked_after_the_run(self):
        self.run_scripted()
        self.write_revocations(1, ["test-release"])
        code, out, err = self.cli("adjudicate", "t43")
        self.assertEqual(code, 1, out + err)
        result = json.loads(out)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("RELEASE_REVOKED", " ".join(result["reasons"]))


    def test_cli_release_context_with_a_merged_child_checks_the_obs_log_once_per_gate(self):
        self.task.write_bytes(self.task.read_bytes() + b'\n[[subtasks]]\nid = "child"\ngoal = "write child result"\n'
                              b'writes = ["child.txt"]\nacceptance = "{python} check.py"\nacceptance_files = ["check.py"]\n'
                              b'[subtasks.budget]\nmax_rounds = 2\nmax_model_calls = 3\nmax_commands = 1\nmax_tokens = 500\n')
        worker = ScriptedModel([step("write_file", path="child.txt", content="child result"), step("finish"), step("finish")], "worker")
        reviewer = ScriptedModel([review("GO"), review("GO")], "reviewer")
        reads = []
        read_bytes = Path.read_bytes

        def read(path):
            if path == self.root / "obs.jsonl":
                reads.append(path)
            return read_bytes(path)

        with patch.object(Path, "read_bytes", read), patch("m2k.cli.build_model", side_effect=[worker, reviewer]):
            code, out, err = self.cli("run", self.task)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(reads), 2)
        self.assertEqual((self.root / "ws/child.txt").read_text(encoding="utf-8"), "child result")
        with Ledger(self.ledger_path) as ledger:
            self.assertIn("release", ledger.last("t43", "task.submitted").payload)
            self.assertNotIn("release", ledger.last("t43.child", "task.submitted").payload)
            self.assertEqual(ledger.last("t43", "gate.adjudication").payload["verdict"], "PASS")
            self.assertEqual(ledger.last("t43.child", "gate.adjudication").payload["verdict"], "PASS")

    def test_release_revocations_require_the_root_domain_schema_and_numeric_version(self):
        env = json.loads(self.release.read_bytes())
        body = self.setup.revocations()["body"]
        for changes in ({"trust_domain": "PRODUCTION"}, {"schema": "other"}, {"version": True},
                        {"version": -1}, {"revoked_releases": "test-release"}):
            with self.subTest(changes=changes), self.assertRaises(trust.TrustError) as refused:
                trust.verify_release(self.setup.pin, "TEST_ONLY", env, revocations=trust.sign(body | changes, self.root_key))
            self.assertEqual(refused.exception.code, "REVOCATIONS_SIGNATURE")
        with self.assertRaises(trust.TrustError) as refused:
            trust.verify_release(self.setup.pin, "TEST_ONLY", env, revocations=trust.sign(body, trust.generate_keypair()[0]))
        self.assertEqual(refused.exception.code, "REVOCATIONS_SIGNATURE")


if __name__ == "__main__":
    unittest.main()
