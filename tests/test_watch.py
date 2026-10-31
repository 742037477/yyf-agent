"""T73: framework observations during CLI work and witnessed outside-path checks for every worker."""
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from m2k import external, obs
from m2k.adjudicate import adjudicate
from m2k.external import CliAgent, changes, watch_snapshot, workspace_snapshot
from m2k.kernel import Kernel, KernelError
from m2k.models import ScriptedModel, build_model
from tests.helpers import GOOD_CODE, KernelCase, review, step
from tests.test_external_agent import ENV, FAKE
from tests.test_obs import WitnessFiles
from tests.test_task_tree import TreeCase, good_a, good_b


class WatchCase(KernelCase):
    def agent(self, mode="good", watch_s=5, timeout_s=60):
        import sys
        return CliAgent([sys.executable, FAKE, mode, str(self.root / "seen")], env=ENV,
                        watch_s=watch_s, timeout_s=timeout_s)

    def watched_folder(self):
        folder = self.root / "watched"
        folder.mkdir()
        for name in ("changed.txt", "removed.txt"):
            (folder / name).write_text("before", encoding="utf-8")
        return folder

    def native(self, contract, path, watch):
        script = contract.workspace / "mutate.py"
        script.write_text(f"from pathlib import Path\nPath({str(path)!r}).write_text('after', encoding='utf-8')\n",
                          encoding="utf-8")
        steps = [step("run_command", command="{python} mutate.py"),
                 step("write_file", path="calc.py", content=GOOD_CODE), step("finish")]
        return Kernel(self.ledger, ScriptedModel(steps, "w"), ScriptedModel([review("GO")], "r"), watch=watch).run(contract)


class WorkspaceWatchTest(WatchCase):
    def run_agent(self, mode, watch_s=0.2, timeout_s=60):
        c = self.contract()
        outcome = self.agent(mode, watch_s, timeout_s).run(c, self.ledger, c.workspace, (), {"check.py"})
        return c, outcome

    def observations(self):
        events = self.ledger.events("t1", "workspace.changed")
        intent = self.ledger.events("t1", "effect.intent")[0]
        result = self.ledger.events("t1", "effect.result")[0]
        self.assertEqual((intent.op_id, result.op_id), ("x1", "x1"))
        for event in events:
            self.assertLess(intent.seq, event.seq)
            self.assertLess(event.seq, result.seq)
            self.assertIsNone(event.op_id)
        return events

    def test_slow_agent_is_observed_and_output_pipe_is_drained(self):
        caller, writers = threading.get_ident(), []
        append = self.ledger.append
        def record(*args, **kwargs):
            writers.append(threading.get_ident())
            return append(*args, **kwargs)
        with patch.object(self.ledger, "append", side_effect=record):
            _, outcome = self.run_agent("watch_slow")
        self.assertEqual(outcome.status.value, "FINISHED")
        self.assertEqual(set(writers), {caller})
        events = self.observations()
        self.assertGreaterEqual(len(events), 3)
        lists = {k: [p for e in events for p in e.payload[k]] for k in ("added", "changed", "removed")}
        self.assertEqual(lists, {"added": ["calc.py", "second.txt", "last.txt"],
                                 "changed": ["calc.py"], "removed": ["second.txt"]})
        self.assertEqual(events[-1].payload["files"], 3)
        self.assertEqual(events[-1].payload["added"], ["last.txt"])

    def test_zero_disables_observation_including_the_final_snapshot(self):
        with patch("m2k.external.workspace_snapshot") as snapshot:
            self.run_agent("watch_slow", watch_s=0)
        snapshot.assert_not_called()
        self.assertEqual(self.observations(), [])

    def test_timeout_keeps_its_deadline_and_gets_a_final_comparison(self):
        started = time.monotonic()
        _, outcome = self.run_agent("watch_timeout", watch_s=60, timeout_s=2)
        self.assertLess(time.monotonic() - started, 12)
        self.assertEqual(outcome.status.value, "STALLED")
        self.assertEqual(self.ledger.events("t1", "effect.result")[0].payload["status"], "UNKNOWN")
        self.assertEqual(self.observations()[0].payload["added"], ["calc.py"])

    def test_failed_process_gets_a_final_comparison(self):
        _, outcome = self.run_agent("watch_fail", watch_s=60)
        self.assertEqual(outcome.status.value, "MODEL_ERROR")
        self.assertEqual(self.observations()[0].payload["added"], ["calc.py"])

    def test_all_lists_are_sorted_and_capped_at_fifty(self):
        c = self.contract()
        for prefix in ("change", "remove"):
            for i in range(60):
                (c.workspace / f"{prefix}{i:02}.txt").write_text("before", encoding="utf-8")
        self.agent("watch_many", watch_s=60).run(c, self.ledger, c.workspace, (), {"check.py"})
        payload = self.observations()[0].payload
        for kind, prefix in (("added", "add"), ("changed", "change"), ("removed", "remove")):
            self.assertEqual(payload[kind], [f"{prefix}{i:02}.txt" for i in range(50)])
        self.assertEqual(payload["more"], {"added": 10, "changed": 10, "removed": 10})
        self.assertEqual(payload["files"], 121)

    def test_failed_snapshot_is_skipped_and_last_good_snapshot_is_retained(self):
        baseline = {"check.py": (1, 1)}
        final = baseline | {"calc.py": (2, 2)}
        def execute(root, prompt, timeout_s, observe):
            observe()
            return "COMPLETED", 0, ""
        with patch("m2k.external.workspace_snapshot", side_effect=[baseline, PermissionError("busy"), final]), \
                patch.object(CliAgent, "_exec", side_effect=execute):
            self.run_agent("good")
        self.assertEqual(self.observations()[0].payload, {"added": ["calc.py"], "changed": [], "removed": [], "files": 2})

    def test_workspace_snapshot_uses_the_workspace_file_list_and_errors_are_visible(self):
        c = self.contract()
        for name in external.SKIP_DIRS:
            folder = c.workspace / name
            folder.mkdir()
            (folder / "ignored.txt").write_text("ignored", encoding="utf-8")
        folder = c.workspace / "nested"
        folder.mkdir()
        (folder / "kept.txt").write_text("ok", encoding="utf-8")
        self.assertEqual(set(workspace_snapshot(c.workspace)), {"check.py", "nested/kept.txt"})
        with patch.object(Path, "stat", side_effect=FileNotFoundError("vanished")), self.assertRaises(FileNotFoundError):
            workspace_snapshot(c.workspace)

    def test_probe_does_not_observe_or_write_the_ledger(self):
        with patch("m2k.external.workspace_snapshot") as snapshot:
            self.assertIsNone(self.agent(watch_s=0.01).probe())
        snapshot.assert_not_called()
        self.assertEqual(self.ledger.events(), [])

    def test_config_default_zero_and_invalid_values(self):
        self.assertEqual(build_model({"kind": "cli", "command": ["agent"]}).watch_s, 5)
        for value in (0, 0.2, 7):
            self.assertEqual(build_model({"kind": "cli", "command": ["agent"], "watch_s": value}).watch_s, value)
        for value in (-1, "5", None, True, False, [], {}, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, r"\[models.worker\] watch_s"):
                build_model({"kind": "cli", "command": ["agent"], "watch_s": value})


class OutsideWatchTest(WitnessFiles, WatchCase):
    def test_outside_change_is_witnessed_fails_without_testing_repair_or_delivery(self):
        folder = self.watched_folder()
        observer, _, public, _ = self.client()
        c = replace(self.contract(), max_repairs=2, writes=("calc.py",))
        with patch("m2k.kernel.run_test") as test:
            result = Kernel(self.ledger, self.agent("outside"), ScriptedModel([review("GO")], "r"),
                            obs=observer, watch=[folder]).run(c)
        test.assert_not_called()
        self.assertEqual((result.test.verdict.value, result.test.reason), ("FAIL", "OUTSIDE_CHANGED"))
        self.assertEqual(result.verdict.value, "FAIL")
        self.assertIsNone(result.delivered_to)
        self.assertEqual(self.ledger.events("t1", "repair.started"), [])
        event = self.ledger.events("t1", "outside.changed")[0]
        self.assertEqual(event.payload, {"added": [str(folder / "added.txt")], "changed": [str(folder / "changed.txt")],
                                         "removed": [str(folder / "removed.txt")]})
        self.assertEqual(self.ledger.last("t1", "task.submitted").payload["watch"], [str(folder.absolute())])
        receipts = {e.payload["receipt"]["body"]["ledger_seq"]: e.payload["receipt"]
                    for e in self.ledger.events("t1", "obs.receipt")}
        self.assertEqual(obs.receipt_body(receipts[event.seq], public, obs.request(event, self.ledger.ledger_id))["kind"],
                         "outside.changed")
        for observation in self.ledger.events("t1", "workspace.changed"):
            self.assertNotIn(observation.seq, receipts)
        self.assertTrue(any("files changed outside the workspace in watched paths:" in reason for reason in result.reasons))

    def test_same_external_agent_without_watch_passes_and_has_no_watch_metadata(self):
        self.watched_folder()
        result = Kernel(self.ledger, self.agent("outside"), ScriptedModel([review("GO")], "r")).run(self.contract())
        self.assertEqual(result.reported_verdict.value, "PASS", result.reasons)
        self.assertIsNotNone(result.delivered_to)
        self.assertNotIn("watch", self.ledger.last("t1", "task.submitted").payload)
        self.assertEqual(self.ledger.events("t1", "outside.changed"), [])

    def test_unchanged_watched_folder_does_not_change_good_delivery(self):
        folder = self.watched_folder()
        result = Kernel(self.ledger, self.agent(), ScriptedModel([review("GO")], "r"), watch=[folder]).run(self.contract())
        self.assertEqual(result.reported_verdict.value, "PASS", result.reasons)
        self.assertEqual((result.delivered_to / "calc.py").read_text(encoding="utf-8"), GOOD_CODE)
        self.assertEqual(self.ledger.events("t1", "outside.changed"), [])

    def test_native_command_is_checked_too(self):
        folder = self.watched_folder()
        result = self.native(self.contract(), folder / "changed.txt", [folder])
        self.assertEqual((result.test.verdict.value, result.test.reason), ("FAIL", "OUTSIDE_CHANGED"))
        self.assertEqual(self.ledger.last("t1", "outside.changed").payload["changed"], [str(folder / "changed.txt")])
        self.assertEqual(self.ledger.events("t1", "workspace.changed"), [])
        self.assertIsNone(result.delivered_to)

    def test_symlinks_are_recorded_without_following_file_or_directory_targets(self):
        folder = self.watched_folder()
        link = self.root / "file-link"
        directory = self.root / "dir-link"
        try:
            link.symlink_to(folder / "changed.txt")
            directory.symlink_to(folder, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlinks unavailable: {error}")
        before, unread = watch_snapshot([link, directory])
        self.assertEqual(unread, [])
        self.assertEqual(before, {str(link): "link:" + os.readlink(link), str(directory): "link:" + os.readlink(directory)})
        result = self.native(self.contract(), folder / "changed.txt", [link, directory])
        self.assertEqual(result.reported_verdict.value, "PASS", result.reasons)
        self.assertEqual(self.ledger.events("t1", "outside.changed"), [])
        self.assertEqual(watch_snapshot([link, directory])[0], before)

    def test_symlinks_inside_a_watched_tree_are_not_followed(self):
        folder = self.watched_folder()
        target = self.root / "target"
        target.mkdir()
        file = target / "data"
        file.write_text("before", encoding="utf-8")
        link = folder / "directory"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlinks unavailable: {error}")
        before, unread = watch_snapshot([folder])
        file.write_text("after", encoding="utf-8")
        self.assertEqual(watch_snapshot([folder]), (before, unread))
        self.assertIn(str(link), before)
        self.assertNotIn(str(link / "data"), before)

    def test_windows_junction_is_recorded_without_following_its_target(self):
        if os.name != "nt":
            self.skipTest("Windows junctions only")
        import _winapi
        folder = self.watched_folder()
        junction = self.root / "junction"
        try:
            _winapi.CreateJunction(str(folder), str(junction))
        except OSError as error:
            self.skipTest(f"junctions unavailable: {error}")
        before, unread = watch_snapshot([junction])
        self.assertEqual((before, unread), ({str(junction): "link:" + os.readlink(junction)}, []))
        result = self.native(self.contract(), folder / "changed.txt", [junction])
        self.assertEqual(result.reported_verdict.value, "PASS", result.reasons)
        self.assertEqual(watch_snapshot([junction]), (before, []))
        self.assertEqual(self.ledger.events("t1", "outside.changed"), [])

    def test_removed_watched_root_is_a_change_not_an_unverified_snapshot(self):
        folder = self.watched_folder()
        file = folder / "changed.txt"
        before, _ = watch_snapshot([file])
        file.unlink()
        after, unread = watch_snapshot([file])
        self.assertEqual(unread, [])
        self.assertEqual(changes(before, after), {"added": [], "changed": [], "removed": [str(file)]})

    def test_after_snapshot_failure_is_unknown_and_no_acceptance_or_repairs_run(self):
        folder = self.watched_folder()
        before = watch_snapshot([folder])
        with patch("m2k.kernel.watch_snapshot", side_effect=[before, ({}, [str(folder)])]), \
                patch("m2k.kernel.run_test") as test:
            result = Kernel(self.ledger, self.agent(), ScriptedModel([review("GO")], "r"), watch=[folder]).run(
                replace(self.contract(), max_repairs=2))
        test.assert_not_called()
        self.assertEqual((result.test.verdict.value, result.test.reason), ("UNKNOWN", "OUTSIDE_UNVERIFIED"))
        self.assertEqual(self.ledger.last("t1", "outside.changed").payload, {"unverified": [str(folder)]})
        self.assertIsNone(result.delivered_to)
        self.assertEqual(self.ledger.events("t1", "repair.started"), [])

    def test_permission_failure_and_file_limit_return_bounded_unverified_paths(self):
        folder = self.watched_folder()
        with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            self.assertEqual(watch_snapshot([folder]), ({}, sorted(str(p) for p in folder.iterdir())))
        self.assertEqual(external.WATCH_FILE_LIMIT, 20_000)
        with patch("m2k.external.WATCH_FILE_LIMIT", 1):
            files, unread = watch_snapshot([folder])
        self.assertEqual(len(files), 2)
        self.assertEqual(len(unread), 1)
        with patch.object(Path, "lstat", side_effect=PermissionError("denied")):
            self.assertEqual(len(watch_snapshot([folder / str(i) for i in range(60)])[1]), 50)

    def test_exceeding_the_file_limit_during_work_is_unknown(self):
        folder = self.watched_folder()
        with patch("m2k.external.WATCH_FILE_LIMIT", 2):
            result = self.native(self.contract(), folder / "added.txt", [folder])
        self.assertEqual((result.test.verdict.value, result.test.reason), ("UNKNOWN", "OUTSIDE_UNVERIFIED"))
        self.assertEqual(len(self.ledger.last("t1", "outside.changed").payload["unverified"]), 1)
        self.assertIsNone(result.delivered_to)

    def test_outside_check_precedes_write_scope_but_follows_shadow_check(self):
        folder = self.watched_folder()
        for tid, filename, reason in (("scope", "extra.txt", "OUTSIDE_CHANGED"),
                                      ("shadow", "math.py", "SHADOWED_MODULE")):
            c = replace(self.contract(task_id=tid), writes=("allowed.py",))
            # The command bypasses the declared write scope; the shadow case also introduces a stdlib name.
            (c.workspace / "mutate.py").write_text(
                f"from pathlib import Path\nPath({str(folder / 'changed.txt')!r}).write_text({tid!r}, encoding='utf-8')\n"
                f"Path({filename!r}).write_text('data', encoding='utf-8')\n", encoding="utf-8")
            with self.subTest(tid=tid):
                result = Kernel(self.ledger, ScriptedModel([step("run_command", command="{python} mutate.py"), step("finish")], "w"),
                                ScriptedModel([review("GO")], "r"), watch=[folder]).run(c)
                self.assertEqual(result.test.reason, reason)
                self.assertEqual(len(self.ledger.events(tid, "outside.changed")), 1)

    def test_watch_overlap_with_workspace_or_subtask_folder_is_rejected_at_submit(self):
        c = self.contract()
        subtask_folder = c.workspace.parent / f"{c.workspace.name}.subtasks"
        for path in (c.workspace, c.workspace.parent, c.workspace / "check.py", subtask_folder,
                     subtask_folder / "a" / "file"):
            with self.subTest(path=path), self.assertRaisesRegex(KernelError, "watched paths overlap"):
                Kernel(self.ledger, self.agent(), ScriptedModel([], "r"), watch=[path]).run(c)
        self.assertEqual(self.ledger.events(), [])

    def test_each_repair_takes_its_own_before_snapshot(self):
        folder = self.watched_folder()
        c = replace(self.contract(), max_repairs=2)
        calls = 0
        def reply(messages):
            nonlocal calls
            calls += 1
            if calls == 1:
                return step("finish")   # An ordinary acceptance failure offers one repair.
            (folder / "changed.txt").write_text("after", encoding="utf-8")
            return step("finish")
        snapshots = []
        def snapshot(paths):
            result = watch_snapshot(paths)
            snapshots.append(result)
            return result
        with patch("m2k.kernel.watch_snapshot", side_effect=snapshot):
            result = Kernel(self.ledger, ScriptedModel([reply, reply], "w"), ScriptedModel([review("GO")] * 2, "r"),
                            watch=[folder]).run(c)
        self.assertEqual(len(snapshots), 4)
        self.assertEqual(snapshots[1], snapshots[2])
        self.assertNotEqual(snapshots[2], snapshots[3])
        self.assertEqual(result.test.reason, "OUTSIDE_CHANGED")
        self.assertEqual(len(self.ledger.events("t1", "repair.started")), 1)


class SubtaskWatchTest(TreeCase):
    def test_reassigned_success_does_not_clear_an_earlier_outside_change(self):
        self.reassigned_outside_change(False)

    def test_reassigned_success_does_not_clear_an_earlier_unverified_change(self):
        self.reassigned_outside_change(True)

    def reassigned_outside_change(self, unverified):
        folder = self.root / "watched"
        folder.mkdir()
        path = folder / "data"
        script = f"from pathlib import Path\nPath({str(path)!r}).write_text('changed', encoding='utf-8')\n"
        c = self.parent(extra={"mutate.py": script})
        c = replace(c, budget=replace(c.budget, max_rounds=20, max_model_calls=20),
                    subtasks=(replace(c.subtasks[0], max_reassign=1,
                                      budget=replace(c.subtasks[0].budget, max_rounds=8, max_model_calls=8)), c.subtasks[1]))
        steps = [step("run_command", command="{python} mutate.py")] + good_a() + good_a() + good_b()
        snapshots = []
        def snapshot(paths):
            value = watch_snapshot(paths)
            snapshots.append(value)
            return ({}, [str(path)]) if unverified and len(snapshots) == 2 else value
        with patch("m2k.kernel.watch_snapshot", side_effect=snapshot):
            result = Kernel(self.ledger, ScriptedModel(steps, "w"), ScriptedModel([review("GO")] * 4, "r"),
                            watch=[folder]).run(c)
        verdict, reason = ("UNKNOWN", "OUTSIDE_UNVERIFIED") if unverified else ("FAIL", "OUTSIDE_CHANGED")
        first = self.ledger.last("p1.a", "test.result").payload
        self.assertEqual((first["verdict"], first["reason"]), (verdict, reason))
        self.assertEqual(self.ledger.last("p1.a.e2", "test.result").payload["verdict"], "PASS")
        self.assertEqual(self.ledger.last("p1.b", "test.result").payload["verdict"], "PASS")
        self.assertEqual(self.ledger.events("p1.a.e2", "outside.changed"), [])
        self.assertEqual(path.read_text(encoding="utf-8"), "changed")
        self.assertEqual((result.test.verdict.value, result.test.reason), (verdict, reason))
        parent = self.ledger.last("p1", "test.result").payload
        self.assertEqual((parent["verdict"], parent["reason"], parent["exit_code"]), (verdict, reason, None))
        self.assertEqual(result.verdict.value, verdict, result.reasons)
        self.assertEqual(result.reported_verdict.value, verdict, result.reasons)
        self.assertIn(repr(str(path)), " ".join(result.reasons))
        self.assertIsNone(result.delivered_to)
        self.assertFalse(c.deliver_to.exists())
        self.assertEqual(self.ledger.events("p1", "delivery.intent"), [])
        self.assertEqual(self.ledger.events(kind="repair.started"), [])
        adj = adjudicate(self.ledger, "p1")
        self.assertNotEqual(adj.verdict, "PASS")
        self.assertIn(repr(str(path)), " ".join(adj.reasons))

    def test_each_subtask_attempt_is_watched(self):
        folder = self.root / "watched"
        folder.mkdir()
        def changed(messages):
            (folder / "data").write_text("changed", encoding="utf-8")
            return step("write_file", path="b.py", content="B = 2\n")
        c = self.parent()
        c = replace(c, budget=replace(c.budget, max_rounds=20, max_model_calls=20),
                    subtasks=(replace(c.subtasks[0], max_reassign=1,
                                      budget=replace(c.subtasks[0].budget, max_rounds=8, max_model_calls=8)), c.subtasks[1]))
        steps = [step("write_file", path="a.py", content="A = 0\n"), step("finish")] + good_a() + [changed, step("finish")]
        snapshots = []
        def snapshot(paths):
            value = watch_snapshot(paths)
            snapshots.append(value)
            return value
        with patch("m2k.kernel.watch_snapshot", side_effect=snapshot):
            result = Kernel(self.ledger, ScriptedModel(steps, "w"), ScriptedModel([review("GO")] * 4, "r"),
                            watch=[folder]).run(c)
        self.assertEqual(result.reported_verdict.value, "FAIL", result.reasons)
        self.assertEqual(len(snapshots), 6)
        self.assertEqual(len(self.ledger.events("p1.b", "outside.changed")), 1)
        self.assertEqual(self.ledger.events("p1.a.e2", "outside.changed"), [])
        self.assertEqual(self.ledger.last("p1.b", "test.result").payload["reason"], "OUTSIDE_CHANGED")
        self.assertEqual(self.ledger.last("p1.a.e2", "test.result").payload["verdict"], "PASS")
