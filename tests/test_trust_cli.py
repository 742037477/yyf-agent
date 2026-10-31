import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization

from m2k import obs, trust
from m2k.cli import main
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from tests.helpers import review, step
from tests.test_trust import Chain, CODE

CAPS = 'tools = ["read_file", "write_file"]\nmax_rounds = 5\nmax_model_calls = 8\nmax_tokens = 1000\nmax_commands = 3\nnetwork = false\n'


class TrustCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()
        self.keys = self.dir / "keys"
        self.keys.mkdir()
        self.config = self.dir / "m2k.toml"
        self.config.write_text('[trust]\ndomain = "TEST_ONLY"\nroot_pin = "keys/root-pin.json"\n'
                               'charter = "keys/charter.json"\nissuer_key = "keys/issuer.pem"\n'
                               'release = "keys/release.json"\n', encoding="utf-8")
        self.caps = self.keys / "caps.toml"
        self.caps.write_text(CAPS, encoding="utf-8")
        for role in ("root", "issuer"):
            self.assertEqual(self.cli("keygen", "--role", role, "--out", self.keys / role)[0], 0)
        self.charter = self.keys / "charter.json"
        self.assertEqual(self.sign_charter()[0], 0)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["obs", "keygen", "--out", str(self.dir / "obs")]), 0)
        self.config.write_text(out.getvalue() + self.config.read_text(encoding="utf-8"), encoding="utf-8")
        self.sign_release()

    def sign_release(self):
        path = self.keys / "release.json"
        path.unlink(missing_ok=True)
        domain = trust.load_pin(self.keys / "root-pin.json").trust_domain
        code, output = self.cli("sign-release", "--root-key", self.keys / "root.pem", "--domain", domain,
                                "--slot", "test", "--version", "1", "--out", path, *(["--code", "dev"] if domain == "TEST_ONLY" else []))
        self.assertEqual(code, 0, output)

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = main(["--config", str(self.config), "trust", *map(str, args)])
            except SystemExit as e:
                code = e.code
        output = stdout.getvalue() + stderr.getvalue()
        self.assert_no_private_key(output)
        return code, output

    def assert_no_private_key(self, output):
        self.assertNotIn("-----BEGIN PRIVATE KEY-----", output)
        for pem in self.keys.glob("*.pem"):
            for line in pem.read_text(encoding="utf-8").splitlines():
                if line and not line.startswith("-----"):
                    self.assertTrue(line not in output, "private key body leaked")

    def sign_charter(self, *extra, out=None, domain="TEST_ONLY"):
        return self.cli("sign-charter", "--root-key", self.keys / "root.pem", "--issuer-pin",
                        self.keys / "issuer-pin.json", "--caps", self.caps, "--domain", domain,
                        "--out", out or self.charter, *extra)

    def configure_revocations(self):
        with self.config.open("a", encoding="utf-8") as f:
            f.write('revocations = "keys/revocations.json"\n')
        self.sign_release()
        return self.keys / "revocations.json"

    def sign_revocations(self, out, *extra):
        return self.cli("sign-revocations", "--root-key", self.keys / "root.pem", "--domain", "TEST_ONLY",
                        "--version", "7", "--out", out, *extra)

    def same_model_task(self):
        self.config.write_text('ledger = "ledger.sqlite"\n[sandbox]\nbackend = "none"\n'
                               + self.config.read_text(encoding="utf-8")
                               + '\n[models.worker]\nmodel = "shared"\n[models.reviewer]\nmodel = "shared"\n'
                                 'allow_same_model = true\n', encoding="utf-8")
        task = self.dir / "task.toml"
        task.write_text('[task]\nid = "same-model"\ngoal = "check existing files"\nworkspace = "ws"\n'
                        'tools = ["read_file", "write_file"]\n'
                        '[acceptance]\ncommand = "{python} check.py"\nfiles = ["check.py"]\n'
                        '[budget]\nmax_rounds = 2\nmax_model_calls = 3\nmax_tokens = 100\nmax_commands = 1\n',
                        encoding="utf-8")
        (self.dir / "ws").mkdir()
        (self.dir / "ws/check.py").write_text('print("ok")\n', encoding="utf-8")
        return task

    def test_same_model_opt_in_requires_resigning_and_passes_test_only(self):
        task = self.same_model_task()
        self.config.write_text(self.config.read_text(encoding="utf-8").replace('allow_same_model = true',
                                                                               'allow_same_model = false'), encoding="utf-8")
        self.sign_release()
        self.config.write_text(self.config.read_text(encoding="utf-8").replace('allow_same_model = false',
                                                                               'allow_same_model = true'), encoding="utf-8")
        output, error = io.StringIO(), io.StringIO()
        with patch("m2k.cli.build_model") as build, redirect_stdout(output), redirect_stderr(error):
            code = main(["--config", str(self.config), "run", str(task)])
        self.assertEqual(code, 2, output.getvalue() + error.getvalue())
        self.assertIn("RELEASE_CONFIG", error.getvalue())
        build.assert_not_called()
        with Ledger(self.dir / "ledger.sqlite") as ledger:
            self.assertEqual(ledger.events("same-model"), [])
            self.assertEqual(ledger.last("m2k.startup", "startup.refused").payload["code"], "RELEASE_CONFIG")
        self.sign_release()
        worker, reviewer = ScriptedModel([step("finish")], "shared"), ScriptedModel([review("GO")], "shared")
        with patch("m2k.models.OpenAICompatModel", side_effect=[worker, reviewer]) as build, \
                redirect_stdout(output), redirect_stderr(error):
            code = main(["--config", str(self.config), "run", str(task)])
        self.assertEqual(code, 0, output.getvalue() + error.getvalue())
        self.assertEqual([call.kwargs for call in build.call_args_list], [{"model": "shared"}] * 2)
        with Ledger(self.dir / "ledger.sqlite") as ledger:
            self.assertEqual(ledger.last("same-model", "task.submitted").payload["review"], "same-model")
            self.assertEqual(ledger.last("same-model", "gate.adjudication").payload["verdict"], "PASS")

    def test_same_model_opt_in_passes_production_with_healthy_anchor_service(self):
        for role in ("root", "issuer"):
            pin = self.keys / f"{role}-pin.json"
            pin.write_text(json.dumps(json.loads(pin.read_text(encoding="utf-8"))
                                      | {"trust_domain": "PRODUCTION"}), encoding="utf-8")
        charter = self.keys / "production-charter.json"
        self.assertEqual(self.sign_charter(domain="PRODUCTION", out=charter)[0], 0)
        self.config.write_text(self.config.read_text(encoding="utf-8").replace('TEST_ONLY', 'PRODUCTION')
                               .replace('keys/charter.json', 'keys/production-charter.json')
                               + 'revocations = "keys/production-revocations.json"\n', encoding="utf-8")
        self.assertEqual(self.cli("sign-revocations", "--root-key", self.keys / "root.pem", "--domain", "PRODUCTION",
                                  "--version", "0", "--out", self.keys / "production-revocations.json")[0], 0)
        task = self.same_model_task()
        with self.config.open("a", encoding="utf-8") as f:
            f.write('[anchor]\nfile = "anchors.jsonl"\nrepo_dir = "anchor-repo"\n')
        self.sign_release()
        worker, reviewer = ScriptedModel([step("finish")], "shared"), ScriptedModel([review("GO")], "shared")
        output, error = io.StringIO(), io.StringIO()
        # Only remote anchor transport is simulated; signing, OBS, gates and adjudication remain real.
        with patch("m2k.models.OpenAICompatModel", side_effect=[worker, reviewer]), \
                patch("m2k.anchor.remote_check", return_value=[]), patch("m2k.anchor.publish", return_value="checkpoint"), \
                redirect_stdout(output), redirect_stderr(error):
            code = main(["--config", str(self.config), "run", str(task)])
        self.assertEqual(code, 0, output.getvalue() + error.getvalue())
        with Ledger(self.dir / "ledger.sqlite") as ledger:
            submitted = ledger.last("same-model", "task.submitted").payload
            self.assertEqual(submitted["review"], "same-model")
            self.assertEqual(submitted["trust"]["domain"], "PRODUCTION")
            self.assertEqual(ledger.last("same-model", "gate.adjudication").payload["verdict"], "PASS")

    def test_ping_accepts_same_model_config_without_adapter_argument_errors(self):
        self.same_model_task()
        models = [ScriptedModel(['{"ok": true}'], "shared") for _ in range(2)]
        with patch("m2k.models.OpenAICompatModel", side_effect=models) as build, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.config), "ping"]), 0)
        self.assertEqual([call.kwargs for call in build.call_args_list], [{"model": "shared"}] * 2)
        self.assertTrue(all(model.calls for model in models))

    def test_online_accept_accepts_same_model_config_without_adapter_argument_errors(self):
        self.same_model_task()
        models = [ScriptedModel([], "shared") for _ in range(2)]
        with patch("m2k.models.OpenAICompatModel", side_effect=models) as build, \
                patch("m2k.accept.run_accept", return_value=0) as accept:
            self.assertEqual(main(["--config", str(self.config), "accept"]), 0)
        self.assertEqual([call.kwargs for call in build.call_args_list], [{"model": "shared"}] * 2)
        self.assertEqual(accept.call_args.args[1], tuple(models))

    def test_full_setup_status_and_key_format(self):
        code, output = self.cli("status")
        self.assertEqual(code, 0, output)
        pin = trust.load_pin(self.keys / "root-pin.json")
        body = json.loads(self.charter.read_text(encoding="utf-8"))["body"]
        for value in ("TEST_ONLY", pin.fingerprint, body["charter_id"], "issuer fingerprint", "caps",
                      "revocation list version", '"issuer_key matches": true', "release: dev"):
            self.assertIn(value, output)
        self.assertEqual(body["caps"], {"tools": ["read_file", "write_file"], "max_rounds": 5,
                                      "max_model_calls": 8, "max_tokens": 1000, "max_commands": 3, "network": False})
        self.assertIs(type(body["issued_at"]), int)
        self.assertEqual(json.loads((self.keys / "root-pin.json").read_text())["schema"], "m2k.pin/1")
        if os.name == "posix":
            self.assertEqual((self.keys / "root.pem").stat().st_mode & 0o777, 0o600)

    def test_real_command_entry_point(self):
        result = subprocess.run([sys.executable, "-m", "m2k", "--config", str(self.config), "trust", "status"],
                                cwd=CODE.parent, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TEST_ONLY", result.stdout)
        self.assert_no_private_key(result.stdout + result.stderr)

    def test_status_prints_root_first_and_distinguishes_unrecorded_from_matching(self):
        fingerprint = trust.load_pin(self.keys / "root-pin.json").fingerprint

        def check(state, has_log=True):
            code, output = self.cli("status")
            self.assertEqual(code, 0, output)
            self.assertEqual(output.splitlines()[0], f"\033[1mRoot 指纹：{fingerprint}（TEST_ONLY）\033[0m")
            label = "ok" if has_log else "ok (log not yet created)"
            self.assertIn(f"obs: {label}\nobs root: {state}\n", output)
            self.assertEqual(json.loads(output[output.index("{"):])["Root fingerprint"], fingerprint)

        check("not yet recorded", has_log=False)
        section = tomllib.loads(self.config.read_text(encoding="utf-8"))["obs"]
        command = [sys.executable, "-m", "m2k", "obs", "serve", "--key", section["key"], "--log", section["log"]]
        with Ledger(self.dir / "memory.sqlite") as ledger, closing(obs.ObsClient(command, section["public_key_b64"])) as witness:
            event = ledger.append("startup", "anchor.checkpoint", {})
            witness.witness(event, ledger.ledger_id)
            check("not yet recorded")   # a log with no release.checked
            for domain, root in (("PRODUCTION", trust.fingerprint(trust.generate_keypair()[0].public_key())),
                                 ("TEST_ONLY", fingerprint)):
                release = dict(root=root, domain=domain, slot="test", version=1, manifest_id="test", revocations_version=None)
                event = ledger.append("startup", "release.checked", {"release": release})
                witness.witness(event, ledger.ledger_id)
                check("ok" if domain == "TEST_ONLY" else "not yet recorded")

    def test_status_reports_changed_root_and_retains_json(self):
        current = trust.load_pin(self.keys / "root-pin.json").fingerprint
        first = trust.fingerprint(trust.generate_keypair()[0].public_key())
        section = tomllib.loads(self.config.read_text(encoding="utf-8"))["obs"]
        command = [sys.executable, "-m", "m2k", "obs", "serve", "--key", section["key"], "--log", section["log"]]
        with Ledger(self.dir / "memory.sqlite") as ledger, closing(obs.ObsClient(command, section["public_key_b64"])) as witness:
            release = dict(root=first, domain="TEST_ONLY", slot="test", version=1, manifest_id="test", revocations_version=None)
            event = ledger.append("startup", "release.checked", {"release": release})
            witness.witness(event, ledger.ledger_id)
        code, output = self.cli("status")
        self.assertEqual(code, 1, output)
        self.assertEqual(output.splitlines()[0], f"\033[1mRoot 指纹：{current}（TEST_ONLY）\033[0m")
        self.assertIn(f"obs: ok\nobs root: changed (OBS remembers {first}, config uses {current})\n", output)
        self.assertEqual(json.loads(output[output.index("{"):])["Root fingerprint"], current)

    def test_release_version_uses_public_json_reader_and_preserves_private_alias(self):
        self.assertTrue(callable(getattr(trust, "read_json", None)), "trust.read_json is not exposed")
        self.assertIs(trust._read_json, trust.read_json)
        with patch("m2k.trust.read_json", wraps=trust.read_json) as reader:
            code, output = self.cli("sign-release", "--root-key", self.keys / "root.pem", "--domain", "TEST_ONLY",
                                    "--code", "dev", "--slot", "test", "--out", self.keys / "next-release.json")
        self.assertEqual(code, 0, output)
        reader.assert_called_once_with(self.keys / "release.json")
        self.assertIn("slot test version 2", output)
        import m2k.cli
        self.assertNotIn("trust._read_json", Path(m2k.cli.__file__).read_text(encoding="utf-8"))

    def test_production_root_generation_refused_without_files(self):
        prefix = self.keys / "production"
        code, output = self.cli("keygen", "--role", "root", "--domain", "PRODUCTION", "--out", prefix)
        self.assertEqual(code, 2)
        self.assertIn("正式 Root 只能在离线环境用自己的工具生成；本命令只生成 TEST_ONLY 的 Root", output)
        self.assertEqual(list(self.keys.glob("production*")), [])

    def test_outputs_cannot_be_overwritten(self):
        originals = {p: p.read_bytes() for p in self.keys.iterdir()}
        code, output = self.cli("keygen", "--role", "root", "--out", self.keys / "root")
        self.assertEqual(code, 1)
        self.assertIn(str(self.keys / "root.pem"), output)
        code, output = self.sign_charter()
        self.assertEqual(code, 1)
        self.assertIn(str(self.charter), output)
        code, output = self.sign_revocations(self.charter)
        self.assertEqual(code, 1)
        self.assertIn(str(self.charter), output)
        for p, data in originals.items():
            self.assertTrue(p.read_bytes() == data, f"existing file changed: {p.name}")
        pin_only = self.keys / "existing-pin.json"
        pin_only.write_text("existing", encoding="utf-8")
        code, output = self.cli("keygen", "--role", "issuer", "--out", self.keys / "existing")
        self.assertEqual(code, 1)
        self.assertIn(str(pin_only), output)
        self.assertFalse((self.keys / "existing.pem").exists())

    def test_modified_charter_signature_is_reported(self):
        env = json.loads(self.charter.read_text(encoding="utf-8"))
        env["body"]["issued_at"] += 1
        self.charter.write_text(json.dumps(env), encoding="utf-8")
        code, output = self.cli("status")
        self.assertEqual(code, 1)
        self.assertIn("CHARTER_SIGNATURE", output)

    def test_revoked_charter_is_reported_and_lists_are_signed(self):
        path = self.configure_revocations()
        charter_id = json.loads(self.charter.read_text(encoding="utf-8"))["body"]["charter_id"]
        code, output = self.sign_revocations(path, "--charter", charter_id, "other", "--credential", "c1",
                                             "--credential", "c2", "c3")
        self.assertEqual(code, 0, output)
        body = trust.open_envelope(json.loads(path.read_text()), trust.load_pin(self.keys / "root-pin.json").public_key)
        self.assertEqual(body["revoked_charters"], [charter_id, "other"])
        self.assertEqual(body["revoked_credentials"], ["c1", "c2", "c3"])
        self.assertEqual(body["version"], 7)
        code, output = self.cli("status")
        self.assertEqual(code, 1)
        self.assertIn("CHARTER_REVOKED", output)

    def test_release_revocation_ids_are_signed_and_status_rejects_the_current_manifest(self):
        path = self.configure_revocations()
        manifest_id = json.loads((self.keys / "release.json").read_bytes())["body"]["manifest_id"]
        code, output = self.sign_revocations(path, "--release", manifest_id, "another", "--release", "third")
        self.assertEqual(code, 0, output)
        body = trust.open_envelope(json.loads(path.read_bytes()), trust.load_pin(self.keys / "root-pin.json").public_key)
        self.assertEqual(body["revoked_releases"], [manifest_id, "another", "third"])
        code, output = self.cli("status")
        self.assertEqual(code, 1, output)
        self.assertIn("RELEASE_REVOKED", output)

    def test_revocations_version_status_and_reload_after_setup(self):
        path = self.configure_revocations()
        setup = trust.load_setup({"domain": "TEST_ONLY", "root_pin": "keys/root-pin.json",
                                  "charter": "keys/charter.json", "issuer_key": "keys/issuer.pem",
                                  "revocations": "keys/revocations.json"}, self.dir)
        self.assertEqual(setup.revocations_path, path)
        self.assertEqual(self.sign_revocations(path)[0], 0)
        self.assertEqual(setup.revocations()["body"]["version"], 7)
        code, output = self.cli("status")
        self.assertEqual(code, 0, output)
        self.assertIn('"revocation list version": 7', output)
        key = trust.load_private_key(self.keys / "root.pem")
        updated = trust.sign(setup.revocations()["body"] | {"version": 8, "revoked_credentials": ["later"]}, key)
        path.write_text(json.dumps(updated), encoding="utf-8")
        self.assertEqual(setup.revocations()["body"]["revoked_credentials"], ["later"])
        path.write_text("invalid JSON", encoding="utf-8")
        with self.assertRaises(trust.TrustError):
            setup.revocations()

    def test_issuer_key_mismatch_is_reported(self):
        self.assertEqual(self.cli("keygen", "--role", "issuer", "--out", self.keys / "other")[0], 0)
        self.config.write_text(self.config.read_text().replace("keys/issuer.pem", "keys/other.pem"), encoding="utf-8")
        self.sign_release()
        code, output = self.cli("status")
        self.assertEqual(code, 1)
        self.assertIn("ISSUER_KEY", output)
        self.assertIn('"issuer_key matches": false', output)

    def test_production_dev_charter_refused_without_output(self):
        path = self.keys / "production-charter.json"
        code, output = self.sign_charter("--release", "dev", domain="PRODUCTION", out=path)
        self.assertEqual(code, 2, output)
        self.assertFalse(path.exists())

    def test_current_release_defaults_and_digest_mismatch(self):
        path = self.keys / "current.json"
        self.assertEqual(self.sign_charter("--release", "current", "--charter-id", "chosen", out=path)[0], 0)
        self.config.write_text(self.config.read_text().replace("keys/charter.json", "keys/current.json"), encoding="utf-8")
        self.sign_release()
        body = json.loads(path.read_text())["body"]
        self.assertEqual(body["release"], trust.release_digest(CODE))
        self.assertEqual(body["charter_id"], "chosen")
        code, output = self.cli("status")
        self.assertEqual(code, 0, output)
        self.assertIn("matches this code", output)
        with patch("m2k.trust.release_digest", return_value="different code"):
            code, output = self.cli("status")
        self.assertEqual(code, 1)
        self.assertIn("does not match", output)
        self.assertIn("RELEASE", output)
        for role in ("root", "issuer"):
            pin = self.keys / f"{role}-pin.json"
            data = json.loads(pin.read_text()) | {"trust_domain": "PRODUCTION"}
            pin.write_text(json.dumps(data), encoding="utf-8")
        production = self.keys / "production.json"
        self.assertEqual(self.sign_charter(domain="PRODUCTION", out=production)[0], 0)
        self.config.write_text(self.config.read_text().replace("TEST_ONLY", "PRODUCTION")
                               .replace("keys/current.json", "keys/production.json"), encoding="utf-8")
        revocations = self.keys / "production-revocations.json"
        self.assertEqual(self.cli("sign-revocations", "--root-key", self.keys / "root.pem", "--domain", "PRODUCTION",
                                  "--version", "0", "--out", revocations)[0], 0)
        self.config.write_text(self.config.read_text(encoding="utf-8") + 'revocations = "keys/production-revocations.json"\n', encoding="utf-8")
        self.sign_release()
        self.assertEqual(self.cli("status")[0], 0)

    def test_signing_self_check_precedes_output(self):
        path = self.keys / "not-written.json"
        with patch("m2k.trust.open_envelope", side_effect=trust.TrustError("SIGNATURE", "self-check failed")):
            code, output = self.sign_charter(out=path)
        self.assertEqual(code, 1)
        self.assertIn("SIGNATURE", output)
        self.assertFalse(path.exists())

    def test_absent_section_and_bad_config_files(self):
        self.config.write_text("", encoding="utf-8")
        self.assertEqual(self.cli("status"), (1, "no [trust] configured\n"))
        base = {"domain": "TEST_ONLY", "root_pin": "keys/root-pin.json", "charter": "keys/charter.json",
                "issuer_key": "keys/issuer.pem"}
        for field in base:
            with self.subTest(missing=field), self.assertRaises(trust.TrustError) as e:
                trust.load_setup({k: v for k, v in base.items() if k != field}, self.dir)
            self.assertIn(field, str(e.exception))
        for section in (base | {"domain": "unknown"}, base | {"issuer_key": "missing.pem"},
                        base | {"root_pin": "missing.json"}, base | {"charter": "missing.json"},
                        base | {"credential_ttl_s": True}, base | {"credential_ttl_s": 0}):
            with self.subTest(section=section), self.assertRaises(trust.TrustError):
                trust.load_setup(section, self.dir)
        setup = trust.load_setup(base | {"credential_ttl_s": 60}, self.dir)
        self.assertEqual(setup.ttl_s, 60)
        self.assertIsNone(setup.revocations())
        self.assertEqual(trust.public_key_b64(setup.issuer_key()), json.loads(self.charter.read_text())["body"]["issuer_public_key_b64"])
        for malformed in ([], {"body": "invalid"}, {"body": None}):
            self.charter.write_text(json.dumps(malformed), encoding="utf-8")
            self.config.write_text('[trust]\ndomain = "TEST_ONLY"\nroot_pin = "keys/root-pin.json"\n'
                                   'charter = "keys/charter.json"\nissuer_key = "keys/issuer.pem"\n', encoding="utf-8")
            code, output = self.cli("status")
            self.assertEqual(code, 1)
            self.assertIn("FORMAT", output)

    def test_bad_private_key_errors_do_not_echo_its_contents(self):
        self.caps.write_text((self.keys / "root.pem").read_text(), encoding="utf-8")
        code, output = self.sign_charter(out=self.keys / "bad-caps.json")
        self.assertEqual(code, 1)
        self.assertIn("FORMAT", output)
        pem = self.keys / "issuer.pem"
        pem.write_text(pem.read_text().splitlines()[1], encoding="utf-8")
        code, output = self.cli("status")
        self.assertEqual(code, 1)
        self.assertIn("KEY", output)


class TrustLeftoversTest(Chain):
    def test_malformed_charter_issuer_is_format_in_both_verifiers(self):
        for value in ("not base64", "", None, 17, [], trust._b64(b"x" * 31)):
            charter = trust.sign(self.charter_body | {"issuer_public_key_b64": value}, self.root)
            with self.subTest(value=value):
                self.assertCode("FORMAT", charter=charter)
                with self.assertRaises(trust.TrustError) as e:
                    trust.verify_charter(self.pin, self.domain, charter, None, CODE)
                self.assertEqual(e.exception.code, "FORMAT")

    def test_pin_requires_json_object_and_utf8(self):
        for raw in (b"[]", b"null", b"17", b'"string"', b"true", b"\xff", b"{"):
            self.pin_path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(trust.TrustError) as e:
                trust.load_pin(self.pin_path)
            self.assertEqual(e.exception.code, "FORMAT")

    def test_pin_fingerprint_and_full_hash_require_exact_match(self):
        full = hashlib.sha256(self.root.public_key().public_bytes(serialization.Encoding.Raw,
                                                                serialization.PublicFormat.Raw)).hexdigest()
        for field, correct in (("fingerprint", self.pin.fingerprint), ("public_key_sha256", full)):
            for value in (correct, correct.upper(), correct + "0", correct[:-1], self.pin.fingerprint,
                          self.pin.fingerprint + "0" * 48, None):
                self.pin_path.write_text(json.dumps({"trust_domain": self.domain,
                                                    "public_key_b64": trust.public_key_b64(self.root), field: value}), encoding="utf-8")
                with self.subTest(field=field, value=value):
                    if value == correct:
                        self.assertEqual(trust.load_pin(self.pin_path).fingerprint, self.pin.fingerprint)
                    else:
                        with self.assertRaises(trust.TrustError) as e:
                            trust.load_pin(self.pin_path)
                        self.assertEqual(e.exception.code, "ROOT_PIN")

    def test_verify_charter_preserves_first_five_checks(self):
        cases = (("DOMAIN", "PRODUCTION", self.charter, None),
                 ("CHARTER_SIGNATURE", self.domain, self.charter | {"signature_b64": "invalid"}, None),
                 ("REVOCATIONS_SIGNATURE", self.domain, self.charter, self.revocations(key=self.issuer)),
                 ("CHARTER_REVOKED", self.domain, self.charter, self.revocations(charters=["ch-1"])),
                 ("RELEASE", self.domain, trust.sign(self.charter_body | {"release": "different"}, self.root), None))
        for code, domain, charter, revocations in cases:
            with self.subTest(code=code), self.assertRaises(trust.TrustError) as e:
                trust.verify_charter(self.pin, domain, charter, revocations, CODE)
            self.assertEqual(e.exception.code, code)
        body, revoked = trust.verify_charter(self.pin, self.domain, self.charter, self.revocations(credentials=["c1"]), CODE)
        self.assertEqual(body, self.charter_body)
        self.assertEqual(revoked, {"revoked_charters": [], "revoked_credentials": ["c1"]})


if __name__ == "__main__":
    unittest.main()
