"""T90: many m2k runs on one deployment — shared OBS log, ledger, anchor file and model endpoint."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from m2k import anchor, locks, obs, trust
from m2k.ledger import Ledger
from m2k.models import ModelError, OpenAICompatModel

SERVE = "from m2k import obs; import sys; obs.serve(sys.argv[1], sys.argv[2], sys.stdin, sys.stdout)"


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        key, self.public = trust.generate_keypair()
        self.key, self.log = self.root / "obs.pem", self.root / "obs" / "obs.jsonl"
        self.key.write_bytes(key.private_bytes(trust.serialization.Encoding.PEM, trust.serialization.PrivateFormat.PKCS8,
                                               trust.serialization.NoEncryption()))

    def tearDown(self):
        self._tmp.cleanup()

    def client(self):
        observer = obs.ObsClient([sys.executable, "-m", "m2k", "obs", "serve", "--key", str(self.key), "--log", str(self.log)],
                                 self.public, timeout_s=60, log_path=self.log)
        self.addCleanup(observer.close)
        return observer

    def test_mutex_excludes_other_processes_and_times_out(self):
        target = self.root / "shared.txt"
        holder = subprocess.Popen([sys.executable, "-c", "import sys, time; from m2k import locks\n"
                                   "with locks.mutex(sys.argv[1]):\n print('held', flush=True); time.sleep(3)", str(target)],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with self.assertRaisesRegex(TimeoutError, "still busy"):
            with locks.mutex(target, timeout_s=0.3):
                pass
        started = time.monotonic()
        with locks.mutex(target, timeout_s=30):
            self.assertGreater(time.monotonic() - started, 0.5)
        holder.stdout.close()

    def test_slots_cap_holders(self):
        slots, inside, peak, guard = [self.root / f"s{i}" for i in range(3)], [0], [0], threading.Lock()

        def work():
            with locks.held(slots):
                with guard:
                    inside[0] += 1
                    peak[0] = max(peak[0], inside[0])
                time.sleep(0.05)
                with guard:
                    inside[0] -= 1
        threads = [threading.Thread(target=work) for _ in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(peak[0], 3)

    def test_many_witness_processes_share_one_log_and_follow_ledger_order(self):
        Ledger(self.root / "ledger.sqlite").close()
        errors = []

        def run(i):
            observer = obs.ObsClient([sys.executable, "-c", SERVE, str(self.key), str(self.log)], self.public, 60, self.log)
            try:
                with Ledger(self.root / "ledger.sqlite") as ledger:
                    for _ in range(15):
                        def see(event):
                            try:
                                return observer.witness(event, ledger.ledger_id)
                            except obs.ObsError as e:
                                return e
                        _, result = ledger.append_then(f"t{i}", "verdict", {"n": i}, see)
                        if isinstance(result, Exception):
                            errors.append(str(result))
            finally:
                observer.close()
        threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(obs.check_log(self.log, self.public), [])
        rows = [json.loads(raw) for raw in self.log.read_bytes().splitlines()]
        self.assertEqual(len(rows), 120)
        seqs = [row["request"]["ledger_seq"] for row in rows]
        self.assertEqual(seqs, sorted(seqs))

    def test_witness_reports_a_log_broken_by_someone_else_as_unavailable(self):
        observer = self.client()
        with Ledger(self.root / "ledger.sqlite") as ledger:
            observer.witness(ledger.append("t", "verdict", {}), ledger.ledger_id)
            with self.log.open("ab") as f:
                f.write(b"{}\n")
            event = ledger.append("m2k.startup", "release.checked", {"release": {
                "root": "a" * 16, "domain": "TEST_ONLY", "slot": "dev", "version": 1, "manifest_id": "m",
                "revocations_version": None}})
            with self.assertRaisesRegex(obs.ObsError, "OBS log at byte") as caught:
                observer.witness(event, ledger.ledger_id)
            self.assertNotIsInstance(caught.exception, obs.ObsRefused)

    def test_witness_process_exit_reason_reaches_the_caller(self):
        self.log.parent.mkdir()
        self.log.write_bytes(b"not json\n")
        observer = self.client()
        observer.process.wait(30)   # exited before the first request: writing to it fails before any reply is read
        with Ledger(self.root / "ledger.sqlite") as ledger:
            with self.assertRaisesRegex(obs.ObsError, r"witness process exited \(OBS log at byte 0"):
                observer.witness(ledger.append("t", "verdict", {}), ledger.ledger_id)

    def test_interruption_between_commit_and_witness_leaves_no_orphan_witness(self):
        observer = self.client()
        with Ledger(self.root / "ledger.sqlite") as ledger:
            def crash(event):
                raise KeyboardInterrupt   # the run is killed after the commit, before OBS is asked
            with self.assertRaises(KeyboardInterrupt):
                ledger.append_then("t", "verdict", {}, crash)
            self.assertEqual(ledger.last("t", "verdict").seq, 1)
            self.assertFalse(self.log.exists())
            with self.assertRaises(sqlite3.OperationalError), patch.object(ledger, "_append",
                                                                       side_effect=sqlite3.OperationalError("disk")):
                ledger.append_then("t", "verdict", {}, lambda event: self.fail("witnessed an uncommitted event"))
            event, receipt = ledger.append_then("t", "verdict", {}, lambda e: observer.witness(e, ledger.ledger_id))
            self.assertEqual(receipt["body"]["ledger_seq"], event.seq)
        self.assertEqual(obs.check_log(self.log, self.public), [])

    def test_anchor_appends_from_many_threads_keep_one_chain(self):
        path = self.root / "anchors.jsonl"
        with Ledger(self.root / "ledger.sqlite") as ledger:
            ledger.append("t", "task.submitted", {})

        def run(i):
            with Ledger(self.root / "ledger.sqlite") as ledger:
                for _ in range(10):
                    anchor.append(path, ledger, f"t{i}")
        threads = [threading.Thread(target=run, args=(i,)) for i in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        with Ledger(self.root / "ledger.sqlite") as ledger:
            self.assertEqual(anchor.check(path, ledger), [])
        self.assertEqual(len(path.read_bytes().splitlines()), 60)

    def test_model_max_concurrent_caps_requests_per_endpoint(self):
        url = f"http://slots.invalid/{self.root.name}/v1"
        inside, peak, guard = [0], [0], threading.Lock()

        def post(self_, body, key):
            with guard:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
            time.sleep(0.05)
            with guard:
                inside[0] -= 1
            return {"choices": [{"message": {"content": "ok"}}]}
        models = [OpenAICompatModel(url, name, api_key_file=None, max_concurrent=2) for name in ("w", "r")]
        with patch.dict("os.environ", {"OPENAI_API_KEY": "k"}), patch.object(OpenAICompatModel, "_post", post):
            threads = [threading.Thread(target=models[i % 2].complete, args=([{"role": "user", "content": "x"}],))
                       for i in range(10)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual(peak[0], 2)
        for bad in (0, -1, 1.5, True, "2"):
            with self.assertRaisesRegex(ValueError, "max_concurrent"):
                OpenAICompatModel(url, "w", max_concurrent=bad)
        with patch("m2k.locks.held", side_effect=PermissionError("denied")), patch.dict("os.environ", {"OPENAI_API_KEY": "k"}):
            with self.assertRaisesRegex(ModelError, "model slot lock"):
                models[0].complete([{"role": "user", "content": "x"}])


if __name__ == "__main__":
    unittest.main()
