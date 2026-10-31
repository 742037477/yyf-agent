import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import chdir, closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from m2k import anchor, obs, trust
from m2k.cli import main
from m2k.ledger import GENESIS, Ledger
from m2k.models import ScriptedModel
from tests.helpers import GOOD_CODE, review, step


class AnchorTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.ledger = self.enterContext(Ledger(self.root / "ledger.sqlite"))
        self.path = self.root / "nested" / "anchors.jsonl"

    def seed(self):
        first = self.ledger.append("task", "task.submitted", {"goal": "normal task"})
        last = self.ledger.append("task", "verdict", {"verdict": "PASS"})
        return first, last

    def test_empty_ledger_has_no_head_or_anchor(self):
        with self.assertRaisesRegex(ValueError, "empty ledger"):
            anchor.head(self.ledger)
        with self.assertRaisesRegex(ValueError, "empty ledger"):
            anchor.append(self.path, self.ledger, "empty")
        self.assertFalse(self.path.exists())

    def test_append_twice_preserves_bytes_and_links_exact_previous_line(self):
        first, last = self.seed()
        expected = {"seq": last.seq, "hash": last.hash, "events": 2, "ledger_id": first.hash}
        self.assertEqual(anchor.head(self.ledger), expected)
        record = anchor.append(self.path, self.ledger, "中文记录")
        original = self.path.read_bytes()
        self.assertEqual(json.loads(original), record)
        self.assertEqual(record["prev"], GENESIS)
        self.assertEqual(record["v"], 1)
        self.assertEqual(record["note"], "中文记录")
        self.assertTrue(record["ts"].endswith("+00:00"))
        self.assertNotIn(b"\r", original)
        second = anchor.append(self.path, self.ledger, "second")
        self.assertEqual(second["prev"], hashlib.sha256(original).hexdigest())
        self.assertEqual(self.path.read_bytes().splitlines(keepends=True)[0], original)
        self.assertEqual(anchor.check(self.path, self.ledger), [])
        self.assertEqual(self.ledger.verify_chain(), (True, None))

    def test_unanchored_tail_is_allowed(self):
        self.seed()
        anchor.append(self.path, self.ledger, "before tail")
        self.ledger.append("task", "next", {"value": 1})
        self.assertEqual(anchor.check(self.path, self.ledger), [])

    def test_changed_stored_hash_reports_anchored_sequence_without_recomputation(self):
        _, last = self.seed()
        anchor.append(self.path, self.ledger, "verdict")
        self.ledger._db.execute("UPDATE events SET hash = ? WHERE seq = ?", (GENESIS, last.seq))
        self.assertEqual(self.ledger.verify_chain(), (False, last.seq))
        self.assertEqual(anchor.check(self.path, self.ledger),
                         [f"ledger rewritten at anchored seq {last.seq}"])

    def test_deleted_tail_is_truncated_even_when_chain_still_verifies(self):
        _, last = self.seed()
        anchor.append(self.path, self.ledger, "verdict")
        self.ledger._db.execute("DELETE FROM events WHERE seq = ?", (last.seq,))
        self.assertEqual(self.ledger.verify_chain(), (True, None))
        self.assertEqual(anchor.check(self.path, self.ledger),
                         [f"ledger truncated at anchored seq {last.seq}"])

    def test_fully_deleted_ledger_reports_truncation(self):
        self.seed()
        anchor.append(self.path, self.ledger, "verdict")
        self.ledger._db.execute("DELETE FROM events")
        self.assertEqual(anchor.check(self.path, self.ledger), ["ledger truncated at anchored seq 2"])

    def test_missing_anchored_event_with_later_tail_is_rewritten_not_truncated(self):
        _, last = self.seed()
        anchor.append(self.path, self.ledger, "verdict")
        tail = self.ledger.append("task", "next", {"value": 1})
        self.ledger._db.execute("DELETE FROM events WHERE seq = ?", (last.seq,))
        self.assertEqual(self.ledger.verify_chain(), (False, tail.seq))
        self.assertEqual(anchor.check(self.path, self.ledger),
                         [f"ledger rewritten at anchored seq {last.seq}"])

    def test_edited_anchor_line_breaks_link_at_next_line(self):
        self.seed()
        anchor.append(self.path, self.ledger, "original")
        anchor.append(self.path, self.ledger, "second")
        self.path.write_bytes(self.path.read_bytes().replace(b"original", b"modified", 1))
        self.assertEqual(anchor.check(self.path, self.ledger), ["anchor file chain broken at line 2"])

    def test_foreign_ledger_is_reported_once_and_not_compared(self):
        self.seed()
        anchor.append(self.path, self.ledger, "first")
        anchor.append(self.path, self.ledger, "second")
        with Ledger(self.root / "other.sqlite") as other:
            other.append("other", "task.submitted", {"goal": "different"})
            self.assertEqual(anchor.check(self.path, other), ["anchors for a different ledger_id skipped"])

    def test_missing_empty_and_malformed_files_are_not_ok(self):
        self.seed()
        with self.assertRaises(FileNotFoundError):
            anchor.check(self.path, self.ledger)
        self.path.parent.mkdir()
        for data, expected in ((b"", "anchor file is empty"), (b"not json\n", "chain broken at line 1"),
                               (b"{}\n", "chain broken at line 1"), (b"[]\n", "chain broken at line 1"),
                               (b"\xff\n", "chain broken at line 1")):
            with self.subTest(data=data):
                self.path.write_bytes(data)
                self.assertIn(expected, anchor.check(self.path, self.ledger)[0])

    def test_append_refuses_incomplete_line_without_changing_file(self):
        self.seed()
        anchor.append(self.path, self.ledger, "first")
        data = self.path.read_bytes().rstrip(b"\n")
        self.path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "incomplete last line"):
            anchor.append(self.path, self.ledger, "second")
        self.assertEqual(self.path.read_bytes(), data)


class AnchorCliTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "config" / "m2k.toml"
        self.config.parent.mkdir()
        self.config.write_text('ledger = "ledger.sqlite"\nanchor_file = "anchors.jsonl"\n'
                               '[sandbox]\nbackend = "none"\n[models.worker]\n[models.reviewer]\n', encoding="utf-8")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.enterContext(chdir(elsewhere))
        self.arguments = ["--config", str(self.config)]
        self.ledger_path = self.config.parent / "ledger.sqlite"
        self.anchor_path = self.config.parent / "anchors.jsonl"

    def invoke(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(self.arguments + list(args))
        return code, output.getvalue(), error.getvalue()

    def seed(self):
        with Ledger(self.ledger_path) as ledger:
            ledger.append("existing", "task.submitted", {"goal": "existing"})

    def task(self, task_id):
        workspace = self.root / task_id
        workspace.mkdir()
        (workspace / "check.py").write_text("from calc import add\nassert add(2, 3) == 5\n", encoding="utf-8")
        path = self.root / f"{task_id}.toml"
        path.write_text(f'[task]\nid = "{task_id}"\ngoal = "Write add"\nworkspace = "{task_id}"\n'
                        '[acceptance]\nfiles = ["check.py"]\ncommand = "{python} check.py"\n', encoding="utf-8")
        return path

    def test_config_relative_anchor_and_manual_command_print_exact_record(self):
        self.seed()
        code, output, error = self.invoke("anchor", "--note", "manual")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.encode("utf-8"), self.anchor_path.read_bytes())
        self.assertEqual(json.loads(output)["note"], "manual")
        code, output, _ = self.invoke("check-ledger")
        self.assertEqual(code, 0)
        self.assertEqual(output, "ledger hash chain OK\nanchors OK (1 checked)\n")

    def test_explicit_paths_override_config_and_are_relative_to_current_folder(self):
        self.seed()
        code, _, _ = self.invoke("anchor", "--out", "explicit.jsonl")
        self.assertEqual(code, 0)
        self.assertTrue(Path("explicit.jsonl").exists())
        self.assertFalse(self.anchor_path.exists())
        self.assertEqual(self.invoke("check-ledger", "--anchors", "explicit.jsonl")[0], 0)
        self.assertEqual(self.invoke("check-ledger")[0], 1)

    def test_empty_ledger_and_missing_output_configuration_fail_cleanly(self):
        code, _, error = self.invoke("anchor")
        self.assertEqual(code, 1)
        self.assertIn("empty ledger", error)
        self.assertFalse(self.anchor_path.exists())
        self.config.write_text('ledger = "ledger.sqlite"\n', encoding="utf-8")
        self.assertIn("--out FILE", self.invoke("anchor")[2])

    def test_malformed_anchor_config_is_clear_in_run_anchor_and_check_ledger(self):
        task = self.task("malformed-anchor")
        message = "[anchor] must be a table with a non-empty string file (and repo_dir, if set)"
        shapes = ['anchor = 1\n', 'anchor = []\n', 'anchor = ""\n', 'anchor = false\n', '[anchor]\n']
        shapes += [f'[anchor]\nfile = {value}\n' for value in ('1', '[]', 'false', '""', '{}')]
        shapes += [f'[anchor]\nfile = "anchors.jsonl"\nrepo_dir = {value}\n'
                   for value in ('1', '[]', 'false', '""', '{}')]
        script = ('import sys\nfrom unittest.mock import patch\nfrom m2k.cli import main\n'
                  'from m2k.models import ScriptedModel\nfrom tests.helpers import review, step\n'
                  'with patch("m2k.cli.build_model", side_effect=[ScriptedModel([step("finish")], "w"), '
                  'ScriptedModel([review("GO")], "r")]):\n    sys.exit(main())\n')
        for shape in shapes:
            self.config.write_text('ledger = "ledger.sqlite"\n' + shape
                                   + '[sandbox]\nbackend = "none"\n[models.worker]\n[models.reviewer]\n', encoding="utf-8")
            for command in (("run", "--untrusted", str(task)), ("anchor",), ("check-ledger",)):
                with self.subTest(shape=shape, command=command):
                    result = subprocess.run([sys.executable, "-c", script, *self.arguments, *command],
                                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                            text=True, encoding="utf-8", errors="replace")
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(message, result.stderr)
                    self.assertNotIn("Traceback", result.stderr)

    def test_chain_failure_precedes_anchor_difference_and_exits_one(self):
        self.seed()
        with Ledger(self.ledger_path) as ledger:
            event = ledger.append("existing", "verdict", {"verdict": "PASS"})
            anchor.append(self.anchor_path, ledger, "last")
            ledger._db.execute("UPDATE events SET hash = ? WHERE seq = ?", (GENESIS, event.seq))
        code, output, _ = self.invoke("check-ledger")
        self.assertEqual(code, 1)
        self.assertEqual(output.splitlines(), [f"ledger hash chain BROKEN at seq {event.seq}",
                                             f"ledger rewritten at anchored seq {event.seq}"])

    def test_malformed_anchor_fails_and_plain_chain_check_still_works_without_config(self):
        self.seed()
        self.anchor_path.write_bytes(b"{}\n")
        self.assertIn("chain broken at line 1", self.invoke("check-ledger")[1])
        self.assertEqual(self.invoke("check-ledger")[0], 1)
        self.config.write_text('ledger = "ledger.sqlite"\n', encoding="utf-8")
        self.assertEqual(self.invoke("check-ledger")[:2], (0, "ledger hash chain OK\n"))

    def test_first_run_skips_empty_before_anchor_and_records_verdict(self):
        task = self.task("first")

        def finish(messages):
            self.assertFalse(self.anchor_path.exists())
            return step("finish")

        models = [ScriptedModel([step("write_file", path="calc.py", content=GOOD_CODE), finish], "worker"),
                  ScriptedModel([review("GO")], "reviewer")]
        with patch("m2k.cli.build_model", side_effect=models):
            self.assertEqual(self.invoke("run", "--untrusted", str(task))[0], 0)
        records = [json.loads(line) for line in self.anchor_path.read_bytes().splitlines()]
        self.assertEqual([r["note"] for r in records], ["first PASS"])
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(records[0]["hash"], ledger.events()[-1].hash)
            self.assertEqual(anchor.check(self.anchor_path, ledger), [])

    def test_run_records_before_submission_and_after_each_verdict(self):
        self.seed()
        for task_id, response, verdict, exit_code in (("pass", review("GO"), "PASS", 0),
                                                      ("fail", review("NO_GO"), "FAIL", 1),
                                                      ("unknown", "unparseable", "UNKNOWN", 2)):
            task = self.task(task_id)
            with Ledger(self.ledger_path) as ledger:
                previous = anchor.head(ledger)

            def write(messages):
                records = [json.loads(line) for line in self.anchor_path.read_bytes().splitlines()]
                self.assertEqual(records[-1]["note"], f"before {task_id}")
                self.assertEqual({k: records[-1][k] for k in previous}, previous)
                return step("write_file", path="calc.py", content=GOOD_CODE)

            models = [ScriptedModel([write, step("finish")], "worker"),
                      ScriptedModel([response], "reviewer")]
            with self.subTest(verdict=verdict), patch("m2k.cli.build_model", side_effect=models):
                self.assertEqual(self.invoke("run", "--untrusted", str(task))[0], exit_code)
                records = [json.loads(line) for line in self.anchor_path.read_bytes().splitlines()]
                self.assertEqual(records[-1]["note"], f"{task_id} {verdict}")
                with Ledger(self.ledger_path) as ledger:
                    verdicts = ledger.events(task_id, "verdict")
                    self.assertEqual(len(verdicts), 1)
                    self.assertEqual(verdicts[0].payload["verdict"], verdict)
                    self.assertEqual(records[-1]["hash"], anchor.head(ledger)["hash"])
                    self.assertGreater(ledger.last(task_id, "gate.adjudication").seq, verdicts[0].seq)
                    self.assertEqual(anchor.check(self.anchor_path, ledger), [])
        self.assertEqual(len(records), 6)

    def test_anchor_inside_workspace_is_refused_before_models_or_submission(self):
        task = self.task("unsafe")
        self.config.write_text('ledger = "ledger.sqlite"\nanchor_file = "../unsafe/anchors.jsonl"\n'
                               '[sandbox]\nbackend = "none"\n[models.worker]\n[models.reviewer]\n', encoding="utf-8")
        models = [ScriptedModel([], "worker"), ScriptedModel([], "reviewer")]
        with patch("m2k.cli.build_model", side_effect=models), self.assertRaisesRegex(SystemExit, "inside the workspace"):
            self.invoke("run", "--untrusted", str(task))
        self.assertTrue(all(not model.calls for model in models))
        with Ledger(self.ledger_path) as ledger:
            from tests.test_startup_refusals import assert_refusal
            assert_refusal(self, self.ledger_path, "unsafe", "ANCHOR_CONFIG")
        self.assertFalse((self.root / "unsafe" / "anchors.jsonl").exists())


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class AnchorPushTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "clone"
        self.repo.mkdir()
        self.git("init", "--initial-branch=ledger-anchors")
        self.git("config", "user.name", "Anchor Test")
        self.git("config", "user.email", "anchor-test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        self.ledger = self.enterContext(Ledger(self.root / "ledger.sqlite"))
        self.ledger.append("normal", "task.submitted", {"goal": "normal"})
        self.path = self.root / "anchors.jsonl"
        anchor.append(self.path, self.ledger, "original")
        self.script = Path(__file__).resolve().parents[1] / "tools" / "anchor_push.py"
        self.target = self.repo / "anchors" / f"{anchor.head(self.ledger)['ledger_id'][:12]}.jsonl"

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def publish(self, *args):
        return subprocess.run([sys.executable, str(self.script), "--anchors", str(self.path),
                               "--repo-dir", str(self.repo), *args], capture_output=True, text=True)

    def test_publish_accepts_append_and_unchanged_file_without_extra_commit(self):
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("copied", result.stdout)
        self.assertEqual(self.target.read_bytes(), self.path.read_bytes())
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         f"anchors: {anchor.head(self.ledger)['ledger_id'][:12]} up to seq 1")
        original_commit = self.git("rev-parse", "HEAD")
        self.assertEqual(self.publish().returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), original_commit)
        self.ledger.append("normal", "verdict", {"verdict": "PASS"})
        anchor.append(self.path, self.ledger, "next")
        self.assertEqual(self.publish().returncode, 0)
        self.assertEqual(self.target.read_bytes(), self.path.read_bytes())
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "2")
        self.assertTrue(self.git("log", "-1", "--format=%s").endswith("up to seq 2"))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_non_prefix_update_is_refused_without_changing_published_copy(self):
        self.assertEqual(self.publish().returncode, 0)
        original, commit = self.target.read_bytes(), self.git("rev-parse", "HEAD")
        self.path.write_bytes(original.replace(b"original", b"modified"))
        result = self.publish()
        self.assertEqual(result.returncode, 1)
        self.assertIn("anchor file is not an extension of the published one", result.stderr)
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), commit)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_truncated_update_is_refused(self):
        original = self.path.read_bytes()
        anchor.append(self.path, self.ledger, "second")
        self.assertEqual(self.publish().returncode, 0)
        published = self.target.read_bytes()
        self.path.write_bytes(original)
        result = self.publish()
        self.assertEqual(result.returncode, 1)
        self.assertIn("not an extension", result.stderr)
        self.assertEqual(self.target.read_bytes(), published)

    def test_wrong_branch_and_dirty_clone_are_refused(self):
        self.assertEqual(self.publish().returncode, 0)
        self.git("switch", "-c", "other")
        result = self.publish()
        self.assertEqual(result.returncode, 1)
        self.assertIn("branch ledger-anchors", result.stderr)
        self.git("switch", "ledger-anchors")
        (self.repo / "unrelated.txt").write_text("unrelated", encoding="utf-8")
        result = self.publish()
        self.assertEqual(result.returncode, 1)
        self.assertIn("clean working tree", result.stderr)

    def test_malformed_anchor_file_is_refused_before_copy(self):
        self.path.write_bytes(b"{}\n")
        result = self.publish()
        self.assertEqual(result.returncode, 1)
        self.assertIn("chain broken at line 1", result.stderr)
        self.assertFalse(self.target.exists())

    def test_push_uses_ledger_anchors_branch_in_local_bare_remote(self):
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        self.git("remote", "add", "origin", str(remote))
        self.git("commit", "--allow-empty", "-m", "initial")
        self.git("push", "origin", "ledger-anchors")
        result = self.publish("--push")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pushed ledger-anchors", result.stdout)
        remote_head = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "ledger-anchors"],
                                     check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(remote_head, self.git("rev-parse", "HEAD"))

    def test_first_push_creates_absent_branch_and_second_integrates_remote(self):
        remote, peer = self.root / "empty.git", self.root / "peer"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        self.git("remote", "add", "origin", str(remote))
        self.assertEqual(self.git("ls-remote", "--heads", "origin", "ledger-anchors"), "")
        with patch("m2k.anchor.git", wraps=anchor.git) as calls:
            first = anchor.publish(self.path, self.repo, push=True)
        self.assertFalse(any(c.args[1] in ("fetch", "rebase") for c in calls.call_args_list))
        self.assertEqual(anchor.git(remote, "rev-parse", "ledger-anchors"), first)
        subprocess.run(["git", "clone", "--branch", "ledger-anchors", str(remote), str(peer)],
                       check=True, capture_output=True)
        for key, value in (("user.name", "Anchor Test"), ("user.email", "peer@example.invalid"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            anchor.git(peer, "config", key, value)
        (peer / "peer.txt").write_text("another publisher\n", encoding="utf-8")
        anchor.git(peer, "add", "peer.txt")
        anchor.git(peer, "commit", "-m", "peer publication")
        peer_head = anchor.git(peer, "rev-parse", "HEAD")
        anchor.git(peer, "push", "origin", "ledger-anchors")
        self.ledger.append("normal", "verdict", {"verdict": "PASS"})
        anchor.append(self.path, self.ledger, "second publication")
        with patch("m2k.anchor.git", wraps=anchor.git) as calls:
            second = anchor.publish(self.path, self.repo, push=True)
        commands = [c.args[1] for c in calls.call_args_list]
        self.assertIn("fetch", commands)
        self.assertIn("rebase", commands)
        self.assertEqual(anchor.git(remote, "rev-parse", "ledger-anchors"), second)
        self.assertEqual(anchor.git(remote, "rev-list", "ledger-anchors").splitlines(), [second, peer_head, first])
        relative = self.target.relative_to(self.repo).as_posix()
        self.assertEqual(anchor.git(remote, "show", f"ledger-anchors:{relative}", raw=True), self.path.read_bytes())
        self.assertEqual(anchor.git(remote, "show", "ledger-anchors:peer.txt"), "another publisher")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_git_uses_c_locale_and_preserves_the_callers_environment(self):
        result = subprocess.CompletedProcess([], 0, b"ok\n", b"")
        with patch.dict(os.environ, {"LC_ALL": "fr_FR.UTF-8", "LANGUAGE": "fr", "T70_CALLER": "preserved",
                                     "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper",
                                     "GIT_CONFIG_VALUE_0": "caller-helper"}):
            original = dict(os.environ)
            with patch("m2k.anchor.subprocess.run", return_value=result) as run:
                self.assertEqual(anchor.git(self.repo, "status"), "ok")
                self.assertEqual(anchor.git(self.repo, "status", raw=True), b"ok\n")
            for call in run.call_args_list:
                self.assertTrue(call.kwargs.get("env") == original | {"LC_ALL": "C", "LANGUAGE": "C"},
                                "git must override only LC_ALL and LANGUAGE in a copy of the caller's environment")
            self.assertTrue(dict(os.environ) == original, "the caller's environment must not be mutated")

    def test_git_failure_keeps_actual_stderr(self):
        args = ["rev-parse", "--verify", "refs/heads/absent"]
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        expected = " | ".join(result.stderr.decode("utf-8", errors="replace")[-300:].strip().splitlines())
        with self.assertRaises(ValueError) as caught:
            anchor.git(self.repo, *args)
        self.assertEqual(str(caught.exception), f"git rev-parse failed (exit {result.returncode}): {expected}")

    def test_git_error_masks_credentials_headers_and_invalid_utf8(self):
        stderr = (b"old diagnostic " * 40 + b"\nfatal: https://user:secret@host/repo\n"
                  b"Authorization: Bearer header-secret\nhttp.extraheader=extra-secret\nbad: \xff\n")
        result = subprocess.CompletedProcess([], 128, b"", stderr)
        with patch("m2k.anchor.subprocess.run", return_value=result), self.assertRaises(ValueError) as caught:
            anchor.git(self.repo, "fetch", "origin")
        message = str(caught.exception)
        self.assertIn("git fetch failed (exit 128): ", message)
        self.assertIn("https://***@host/repo", message)
        self.assertIn("Authorization: ***", message)
        self.assertIn("extraheader=***", message)
        self.assertIn("bad: \ufffd", message)
        self.assertNotIn("\n", message)
        for secret in ("user:secret", "header-secret", "extra-secret"):
            self.assertNotIn(secret, message)

    def test_git_redacts_long_header_before_truncation_and_preserves_success(self):
        result = subprocess.CompletedProcess([], 1, b"", b"Authorization: Bearer " + b"private" * 100)
        with patch("m2k.anchor.subprocess.run", return_value=result), self.assertRaises(ValueError) as caught:
            anchor.git(self.repo, "push")
        self.assertNotIn("private", str(caught.exception))
        result = subprocess.CompletedProcess([], 0, b"  success\n", b"ignored")
        with patch("m2k.anchor.subprocess.run", return_value=result):
            self.assertEqual(anchor.git(self.repo, "status"), "success")
            self.assertEqual(anchor.git(self.repo, "status", raw=True), b"  success\n")

    def test_git_error_keeps_only_last_300_characters(self):
        stderr = ("discard this prefix " * 30 + "tail " * 80 + "\n").encode("utf-8")
        result = subprocess.CompletedProcess([], 1, b"", stderr)
        with patch("m2k.anchor.subprocess.run", return_value=result), self.assertRaises(ValueError) as caught:
            anchor.git(self.repo, "push")
        self.assertEqual(str(caught.exception), "git push failed (exit 1): " + stderr.decode()[-300:].strip())

    def test_git_redacts_complete_url_credentials_containing_at_sign(self):
        result = subprocess.CompletedProcess([], 1, b"", b"fatal: https://user:part@secret@host/repo")
        with patch("m2k.anchor.subprocess.run", return_value=result), self.assertRaises(ValueError) as caught:
            anchor.git(self.repo, "fetch")
        self.assertEqual(str(caught.exception), "git fetch failed (exit 1): fatal: https://***@host/repo")


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class SignedAnchorTest(unittest.TestCase):
    def setUp(self):
        AnchorTest.setUp(self)
        self.repo = self.root / "clone"
        self.repo.mkdir()
        self.git("init", "--initial-branch=ledger-anchors")
        for key, value in (("user.name", "Anchor Test"), ("user.email", "anchor@example.invalid"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            self.git("config", key, value)
        self.git("commit", "--allow-empty", "-m", "initial")
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "origin", "ledger-anchors")
        self.key, self.public = trust.generate_keypair()
        self.key_path, self.log = self.root / "obs.pem", self.root / "obs.jsonl"
        self.key_path.write_bytes(self.key.private_bytes(trust.serialization.Encoding.PEM,
                                  trust.serialization.PrivateFormat.PKCS8, trust.serialization.NoEncryption()))
        self.api = self.enterContext(patch("m2k.anchor.activity", return_value=[]))

    git = AnchorPushTest.git

    def checkpoint(self, count=1):
        for _ in range(count):
            event = self.ledger.append("task", "anchor.checkpoint", {})
            output = io.StringIO()
            obs.serve(self.key_path, self.log, io.StringIO(json.dumps(obs.request(event, self.ledger.ledger_id)) + "\n"), output)
            receipt = json.loads(output.getvalue())
            self.assertNotIn("error", receipt)
            self.ledger.append("task", "obs.receipt", {"receipt": receipt})
        return anchor.append(self.path, self.ledger, "checkpoint", receipt=receipt)

    def publish(self):
        return anchor.publish(self.path, self.repo, push=True)

    def remote(self, **kwargs):
        return anchor.remote_check(self.path, self.ledger, self.repo, self.public, self.log, **kwargs)

    def process_checkpoint(self):
        command = [sys.executable, "-m", "m2k", "obs", "serve", "--key", str(self.key_path), "--log", str(self.log)]
        with closing(obs.ObsClient(command, self.public)) as witness:
            event = self.ledger.append("task", "anchor.checkpoint", {})
            receipt = witness.witness(event, self.ledger.ledger_id)
            self.ledger.append("task", "obs.receipt", {"receipt": receipt})
        return anchor.append(self.path, self.ledger, "checkpoint", receipt=receipt)

    def new_notary(self, name):
        self.key, self.public = trust.generate_keypair()
        self.key_path, self.log = self.root / f"{name}.pem", self.root / f"{name}.jsonl"
        self.key_path.write_bytes(self.key.private_bytes(trust.serialization.Encoding.PEM,
                                  trust.serialization.PrivateFormat.PKCS8, trust.serialization.NoEncryption()))

    def clone(self, name):
        repo = self.root / name
        subprocess.run(["git", "clone", "--config", "core.autocrlf=false", "--branch", "ledger-anchors",
                        str(self.root / "remote.git"), str(repo)],
                       check=True, capture_output=True)
        for key, value in (("user.name", "Anchor Test"), ("user.email", "anchor@example.invalid"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            anchor.git(repo, "config", key, value)
        return repo

    def test_diverged_clone_replays_unpushed_anchors_without_rewriting_remote_history(self):
        other = self.clone("other")
        with Ledger(self.root / "other.sqlite") as ledger:
            ledger.append("other", "task.submitted", {"goal": "other ledger"})
            path = self.root / "other.jsonl"
            record = anchor.append(path, ledger, "unpushed")
            unpushed = anchor.publish(path, other, push=False)
            own_record = self.checkpoint()
            self.publish()
            remote = self.root / "remote.git"
            original = anchor.git(remote, "rev-list", "ledger-anchors").splitlines()
            ledger.append("other", "verdict", {"verdict": "PASS"})
            anchor.append(path, ledger, "next")
            with patch("m2k.anchor.git", wraps=anchor.git) as calls:
                published = anchor.publish(path, other, push=True)
            self.assertEqual(published, anchor.git(remote, "rev-parse", "ledger-anchors"))
            for rec, expected in ((record, path.read_bytes()), (own_record, self.path.read_bytes())):
                self.assertEqual(anchor.git(remote, "show", f"ledger-anchors:anchors/{rec['ledger_id'][:12]}.jsonl", raw=True),
                                 expected)
            history = anchor.git(remote, "rev-list", "ledger-anchors").splitlines()
            self.assertTrue(set(original).issubset(history))
            self.assertNotIn(unpushed, history)
            self.assertEqual(anchor.git(remote, "rev-list", "--merges", "ledger-anchors"), "")
            self.assertEqual(anchor.git(other, "status", "--porcelain"), "")
            self.assertFalse(any("--force" in c.args or "reset" in c.args for c in calls.call_args_list))
        self.assertEqual(self.remote(), [])

    def publish_with_races(self, rejections):
        self.checkpoint()
        other, run = self.clone("racer"), anchor.git
        pushes = []

        def race(repo, *args, **kwargs):
            if Path(repo) == self.repo and args[0] == "push":
                pushes.append(args)
                if len(pushes) <= rejections:
                    name = f"race-{len(pushes)}.txt"
                    (other / name).write_text("concurrent anchor publisher\n", encoding="utf-8")
                    run(other, "add", "--", name)
                    run(other, "commit", "-m", "concurrent publication")
                    run(other, "push", "origin", "ledger-anchors")
            return run(repo, *args, **kwargs)

        with patch("m2k.anchor.git", side_effect=race) as calls:
            if rejections == 1:
                self.publish()
            else:
                with self.assertRaisesRegex(ValueError, "git push failed"):
                    self.publish()
        self.assertEqual(pushes, [("push", "origin", "ledger-anchors")] * 2)
        self.assertEqual(sum(c.args[1] == "fetch" for c in calls.call_args_list), 2)
        self.assertEqual(sum(c.args[1:3] == ("rebase", "refs/remotes/origin/ledger-anchors")
                             for c in calls.call_args_list), 2)
        self.assertFalse(any("--force" in c.args or "reset" in c.args for c in calls.call_args_list))
        if rejections == 1:
            self.assertEqual(self.remote(), [])

    def test_publish_retries_one_non_fast_forward_race(self):
        self.publish_with_races(1)

    def test_publish_stops_after_second_non_fast_forward_race(self):
        self.publish_with_races(2)

    def test_publish_checks_remote_prefix_after_rebase(self):
        self.checkpoint()
        self.publish()
        stale = self.root / "stale.jsonl"
        stale.write_bytes(self.path.read_bytes())
        behind = self.clone("behind")
        self.checkpoint()
        current = self.publish()
        with self.assertRaisesRegex(ValueError, "not an extension of the published one"):
            anchor.publish(stale, behind, push=True)
        self.assertEqual(anchor.git(behind, "rev-parse", "HEAD"), current)
        self.assertEqual(anchor.git(behind, "status", "--porcelain"), "")

    def test_failed_rebase_is_aborted_before_copying_anchor(self):
        collision = self.repo / "collision.txt"
        collision.write_text("original\n", encoding="utf-8")
        self.git("add", "collision.txt")
        self.git("commit", "-m", "shared file")
        self.git("push", "origin", "ledger-anchors")
        other = self.clone("conflict")
        (other / "collision.txt").write_text("local change\n", encoding="utf-8")
        anchor.git(other, "add", "collision.txt")
        anchor.git(other, "commit", "-m", "local change")
        before = anchor.git(other, "rev-parse", "HEAD")
        collision.write_text("remote change\n", encoding="utf-8")
        self.git("add", "collision.txt")
        self.git("commit", "-m", "remote change")
        self.git("push", "origin", "ledger-anchors")
        record = self.checkpoint()
        with patch("m2k.anchor.git", wraps=anchor.git) as calls:
            with self.assertRaisesRegex(ValueError, "git rebase failed.*:"):
                anchor.publish(self.path, other, push=True)
        self.assertIn((other, "rebase", "--abort"), [c.args for c in calls.call_args_list])
        self.assertEqual(anchor.git(other, "rev-parse", "HEAD"), before)
        self.assertEqual(anchor.git(other, "status", "--porcelain"), "")
        self.assertFalse((other / "anchors" / f"{record['ledger_id'][:12]}.jsonl").exists())

    def test_publish_without_push_never_fetches_or_rebases(self):
        self.checkpoint()
        with patch("m2k.anchor.git", wraps=anchor.git) as calls:
            anchor.publish(self.path, self.repo, push=False)
        self.assertFalse(any(c.args[1] in ("fetch", "rebase", "push") for c in calls.call_args_list))

    def test_malformed_published_records_do_not_hide_later_notary_or_file_problems(self):
        record = self.checkpoint()
        old_notary = record["receipt"]["body"]["notary"]
        malformed = [b"[]", b"null", b"17", b"{}", b'{"v":2}', b'{"v":2,"receipt":{"body":{}}}',
                     b"not json", b'{"v":2,"receipt":{"body":{"notary":[]}}}']
        own = f"anchors/{record['ledger_id'][:12]}.jsonl"
        target = self.repo / own
        target.parent.mkdir()
        later = record | {"prev": hashlib.sha256(malformed[-1] + b"\n").hexdigest()}
        data = b"\n".join(malformed) + b"\n" + trust.canonical(later) + b"\n"
        target.write_bytes(data)
        self.path.write_bytes(data)
        (target.parent / "z-later.jsonl").write_bytes(b"{}\n")
        self.git("add", "anchors")
        self.git("commit", "-m", "malformed published records")
        self.git("push", "origin", "ledger-anchors")
        self.new_notary("current")
        for scope in (False, True):
            with self.subTest(scope=scope):
                problems = self.remote(this_ledger_only=scope)
                expected = [f"{own}: malformed anchor record at line {n}" for n in range(1, len(malformed) + 1)]
                if not scope:
                    expected.append("anchors/z-later.jsonl: malformed anchor record at line 1")
                self.assertEqual([p for p in problems if "malformed anchor record" in p], expected)
                self.assertTrue(any(p.startswith(f"notary changed: {own} has anchors signed by {old_notary},")
                                    for p in problems), problems)
                self.assertNotIn("'notary'", problems)

    def test_remote_reports_each_distinct_notary_change_in_own_file_in_both_scopes(self):
        old = trust.fingerprint(self.key.public_key())
        record = self.process_checkpoint()
        self.process_checkpoint()   # the same notary must be reported only once
        self.new_notary("second")
        second = trust.fingerprint(self.key.public_key())
        self.process_checkpoint()
        self.publish()
        self.new_notary("current")
        current = trust.fingerprint(self.key.public_key())
        file = f"anchors/{record['ledger_id'][:12]}.jsonl"
        for scope in (False, True):
            with self.subTest(scope=scope):
                problems = self.remote(this_ledger_only=scope)
                self.assertEqual([p for p in problems if p.startswith("notary changed")],
                                 [f"notary changed: {file} has anchors signed by {fp}, current OBS is {current}"
                                  for fp in (old, second)])

    def test_other_ledgers_can_keep_their_own_notaries_without_new_problems(self):
        old = self.process_checkpoint()
        self.publish()
        self.ledger = self.enterContext(Ledger(self.root / "new-epoch.sqlite"))
        self.path = self.root / "new-anchors.jsonl"
        self.new_notary("new-epoch")
        new = self.process_checkpoint()
        self.publish()
        self.assertNotEqual(old["ledger_id"], new["ledger_id"])
        self.assertNotEqual(old["receipt"]["body"]["notary"], new["receipt"]["body"]["notary"])
        for scope in (False, True):
            with self.subTest(scope=scope):
                self.assertEqual(self.remote(this_ledger_only=scope), [])

    def fresh(self, name):
        self.ledger = self.enterContext(Ledger(self.root / f"{name}.sqlite"))
        self.log.unlink()
        self.path.unlink()

    def test_v2_binds_checkpoint_event_not_the_later_receipt_event(self):
        record = self.checkpoint()
        events = self.ledger.events()
        self.assertEqual(set(record), {"v", "ts", "ledger_id", "seq", "hash", "note", "receipt", "prev"})
        self.assertEqual(record["v"], 2)
        self.assertEqual((record["seq"], record["hash"]), (events[-2].seq, events[-2].hash))
        self.assertEqual(record["receipt"]["body"]["kind"], "anchor.checkpoint")
        self.assertEqual(anchor.check(self.path, self.ledger, self.public, self.log), [])
        self.assertEqual(self.publish(), self.git("rev-parse", "HEAD"))
        self.assertEqual(self.remote(), [])

    def test_signed_wrong_key_and_wrong_ledger_binding_are_reported(self):
        original = self.checkpoint()
        other, public = trust.generate_keypair()
        variants = [trust.sign(original["receipt"]["body"] | {"notary": trust.fingerprint(other.public_key())}, other)]
        for field, value in (("ledger_id", "a" * 64), ("ledger_seq", 999), ("ledger_hash", "b" * 64),
                             ("kind", "verdict")):
            variants.append(trust.sign(original["receipt"]["body"] | {field: value}, self.key))
        for receipt in variants:
            with self.subTest(receipt=receipt["body"]):
                self.path.write_bytes(trust.canonical(original | {"receipt": receipt}) + b"\n")
                self.assertTrue(anchor.check(self.path, self.ledger, self.public, self.log))

    def test_log_position_requires_identical_receipt_and_previous_raw_hash(self):
        self.checkpoint(2)
        original = self.log.read_bytes()
        row = json.loads(original.splitlines()[-1])
        row["receipt"] = trust.sign(row["receipt"]["body"] | {"ts": "different"}, self.key)
        self.log.write_bytes(original.splitlines(keepends=True)[0] + trust.canonical(row) + b"\n")
        self.assertEqual(obs.check_log(self.log, self.public), [])
        self.assertTrue(anchor.check(self.path, self.ledger, self.public, self.log))
        self.log.write_bytes(original.replace(b'"ts":"', b'"ts":"edited', 1))
        self.assertTrue(anchor.check(self.path, self.ledger, self.public, self.log))

    def test_rebuilt_ledger_and_log_are_self_consistent_but_published_checkpoint_differs(self):
        self.checkpoint(3)
        self.publish()
        self.fresh("rewritten")
        self.checkpoint(3)
        self.assertEqual(self.ledger.verify_chain(), (True, None))
        self.assertEqual(obs.check_log(self.log, self.public), [])
        self.assertTrue(any("OBS log replaced or truncated" in p for p in self.remote()))

    def test_new_log_past_old_checkpoint_cannot_hide_old_receipt_and_is_read_once(self):
        old = self.checkpoint(10)
        self.publish()
        old_file = f"anchors/{old['ledger_id'][:12]}.jsonl"
        self.fresh("fresh")
        new = self.checkpoint(13)
        self.assertGreater(new["receipt"]["body"]["obs_seq"], 11)
        self.publish()
        reads, read_bytes = [], Path.read_bytes

        def read(path):
            if path == self.log:
                reads.append(path)
            return read_bytes(path)

        with patch.object(Path, "read_bytes", read), patch("m2k.obs.trust.loads_strict", wraps=trust.loads_strict) as parse:
            problems = self.remote()
        self.assertEqual(reads, [self.log])
        self.assertEqual(sum('"request"' in call.args[0] for call in parse.call_args_list), 13)
        self.assertIn(f"OBS log replaced or truncated: obs_seq 10 in {old_file}", problems)
        self.assertEqual(self.remote(this_ledger_only=True), [])

    def test_same_obs_sequence_in_two_fresh_logs_conflicts_except_in_this_ledger_scope(self):
        old = self.checkpoint()
        self.publish()
        self.fresh("second")
        new = self.checkpoint()
        self.publish()
        self.assertEqual(old["receipt"]["body"]["obs_seq"], new["receipt"]["body"]["obs_seq"])
        self.assertNotEqual(old["receipt"], new["receipt"])
        self.assertTrue(any("OBS log replaced or truncated: obs_seq 1" in p for p in self.remote()))
        self.assertEqual(self.remote(this_ledger_only=True), [])

    def test_published_prefix_is_checked_against_local_bytes_and_fetched_remote_not_checkout(self):
        record = self.checkpoint()
        self.publish()
        self.path.write_bytes(trust.canonical(record | {"note": "local changed"}) + b"\n")
        self.assertTrue(any("not a prefix" in p for p in self.remote()))
        target = self.repo / "anchors" / f"{record['ledger_id'][:12]}.jsonl"
        self.path.write_bytes(target.read_bytes())
        target.unlink()
        self.assertEqual(self.remote(), [])

    def test_append_only_history_is_clean_in_both_scopes(self):
        for _ in range(2):
            self.checkpoint()
            self.publish()
        for scope in (False, True):
            with self.subTest(scope=scope), patch("m2k.anchor.git", wraps=anchor.git) as git:
                self.assertEqual(self.remote(this_ledger_only=scope), [])
            calls = [call.args for call in git.call_args_list if call.args[1] == "log"]
            self.assertEqual(len(calls), 1)
            self.assertIn("--numstat", calls[0])
            self.assertIn("--no-renames", calls[0])
            self.assertIn("refs/remotes/origin/ledger-anchors", calls[0])

    def assert_history_problem_after_push(self, file):
        self.git("commit", "-m", "ordinary history change")
        commit = self.git("rev-parse", "HEAD")
        self.git("push", "origin", "ledger-anchors")
        for scope in (False, True):
            with self.subTest(scope=scope):
                problems = self.remote(this_ledger_only=scope)
                self.assertTrue(any("history is not append-only" in p and commit in p and file in p
                                    for p in problems), problems)

    def test_normal_push_deleting_anchor_file_is_reported_in_both_scopes(self):
        record = self.checkpoint()
        self.publish()
        file = f"anchors/{record['ledger_id'][:12]}.jsonl"
        self.git("rm", "--", file)
        self.assert_history_problem_after_push(file)

    def test_normal_push_removing_last_anchor_line_is_reported_in_both_scopes(self):
        record = self.checkpoint()
        self.publish()
        self.checkpoint()
        self.publish()
        file = f"anchors/{record['ledger_id'][:12]}.jsonl"
        target = self.repo / file
        target.write_bytes(target.read_bytes().splitlines(keepends=True)[0])
        self.git("add", "--", file)
        self.assert_history_problem_after_push(file)

    def test_normal_push_renaming_anchor_file_is_reported_in_both_scopes(self):
        record = self.checkpoint()
        self.publish()
        file = f"anchors/{record['ledger_id'][:12]}.jsonl"
        self.git("mv", file, "anchors/renamed.jsonl")
        self.assert_history_problem_after_push(file)

    def test_deleted_empty_file_is_reported_in_both_scopes(self):
        self.checkpoint()
        self.publish()
        file = "anchors/empty.jsonl"
        (self.repo / file).touch()
        self.git("add", "--", file)
        self.git("commit", "-m", "empty file")
        self.git("push", "origin", "ledger-anchors")
        self.git("rm", "--", file)
        self.assert_history_problem_after_push(file)

    def test_missing_own_file_is_reported_only_after_published_event_in_both_scopes(self):
        record = self.checkpoint()
        for scope in (False, True):
            self.assertEqual(self.remote(this_ledger_only=scope), [])
        self.ledger.append("task", "anchor.published", {"commit": self.git("rev-parse", "HEAD"), "seq": record["seq"]})
        for scope in (False, True):
            with self.subTest(scope=scope):
                self.assertIn(f"anchors/{record['ledger_id'][:12]}.jsonl: published anchor file missing",
                              self.remote(this_ledger_only=scope))

    def test_force_push_branch_deletion_and_unreachable_activity_are_problems_in_both_scopes(self):
        self.checkpoint()
        self.publish()
        for scope in (False, True):
            for kind in ("force_push", "branch_deletion"):
                self.api.return_value = [{"activity_type": kind, "actor": {"login": "owner"}, "timestamp": "2026-10-17T01:02:03Z"}]
                with self.subTest(kind=kind, scope=scope):
                    self.assertIn(f"{kind} by owner at 2026-10-17T01:02:03Z", self.remote(this_ledger_only=scope))
            self.api.side_effect = OSError("API unreachable")
            self.assertTrue(any("API unreachable" in p for p in self.remote(this_ledger_only=scope)))
            self.api.side_effect = None

    def test_missing_remote_branch_or_repository_is_a_problem(self):
        self.git("remote", "set-url", "origin", str(self.root / "missing.git"))
        self.assertTrue(any("git fetch failed" in p for p in self.remote()))


class ActivityApiTest(unittest.TestCase):
    def response(self, entries, link=""):
        result = io.BytesIO(json.dumps(entries).encode("utf-8"))
        result.headers = {"Link": link}
        return result

    def test_env_token_and_pagination_use_only_github_without_credentials_in_url(self):
        entries = [{"activity_type": "force_push", "actor": {"login": "owner"}, "timestamp": "now"}]
        first = self.response(entries, '<https://api.github.com/repos/owner/repo/activity?before=cursor>; rel="next"')
        second, third = self.response([]), self.response([])
        with patch("m2k.anchor.git", return_value="git@github.com:owner/repo.git"), \
                patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}), \
                patch("m2k.anchor.subprocess.run") as credential, \
                patch("m2k.anchor.urllib.request.urlopen", side_effect=[first, second, third]) as fetch:
            self.assertEqual(anchor.activity(Path("clone")), entries)
        credential.assert_not_called()
        self.assertEqual(fetch.call_count, 3)
        self.assertIn("activity_type=force_push", fetch.call_args_list[0].args[0].full_url)
        self.assertIn("activity_type=branch_deletion", fetch.call_args_list[2].args[0].full_url)
        for call in fetch.call_args_list:
            request = call.args[0]
            self.assertEqual(request.headers["Authorization"], "Bearer test-token")
            self.assertTrue(request.full_url.startswith("https://api.github.com/repos/owner/repo/activity?"))
            self.assertNotIn("test-token", request.full_url)

    def test_git_credential_fallback_never_prints_or_records_token(self):
        credential = subprocess.CompletedProcess([], 0, "username=owner\npassword=test-secret\n", "")
        output, error = io.StringIO(), io.StringIO()
        with patch("m2k.anchor.git", return_value="https://github.com/owner/repo.git"), \
                patch.dict(os.environ, {"GITHUB_TOKEN": ""}), \
                patch("m2k.anchor.subprocess.run", return_value=credential) as fill, \
                patch("m2k.anchor.urllib.request.urlopen", side_effect=[self.response([]), self.response([])]) as fetch, \
                redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(anchor.activity(Path("clone")), [])
        self.assertEqual(output.getvalue() + error.getvalue(), "")
        self.assertEqual(fill.call_args.args[0], ["git", "credential", "fill"])
        self.assertEqual(fill.call_args.kwargs["input"], "protocol=https\nhost=github.com\n\n")
        self.assertTrue(all(call.args[0].headers["Authorization"] == "Bearer test-secret" for call in fetch.call_args_list))

    def test_unavailable_and_malformed_api_responses_are_not_a_pass_or_secret_disclosure(self):
        for response in (OSError("test-secret"), self.response({"message": "test-secret"})):
            with self.subTest(response=type(response).__name__), \
                    patch("m2k.anchor.git", return_value="https://github.com/owner/repo"), \
                    patch.dict(os.environ, {"GITHUB_TOKEN": "test-secret"}), \
                    patch("m2k.anchor.urllib.request.urlopen", side_effect=[response]), \
                    self.assertRaisesRegex(ValueError, "^GitHub activity API unavailable$"):
                anchor.activity(Path("clone"))


if __name__ == "__main__":
    unittest.main()
