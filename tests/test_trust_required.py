import io
import json
import os
import tempfile
import tomllib
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from m2k import tools
from m2k.cli import main
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from tests.helpers import review, step


REFUSAL = ('拒绝启动：没有信任链（[trust]）。两个选择：1) 运行 m2k trust init-dev --out keys 生成测试链，'
           '并把它打印的 [trust] 段加进 m2k.toml；2) 只这一次不授权运行：m2k run --untrusted <task>（会记入账本）。')
WARNING = '警告：未授权运行（没有信任链），已记入账本。'
BANNER = '未授权运行：这个任务没有信任链（Root → 授权书 → 凭证）'
NOTICE = 'TEST_ONLY：只用于开发和测试。正式运行需要用真 Root 离线签发的授权书。'


class TrustRequiredTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.config = self.root / 'm2k.toml'
        self.config.write_text('ledger = "ledger.sqlite"\n[sandbox]\nbackend = "none"\n'
                               '[models.worker]\n[models.reviewer]\n', encoding='utf-8')
        self.task = self.root / 'task.toml'
        self.task.write_text('[task]\nid = "t20"\ngoal = "verify trust startup"\nworkspace = "ws"\n'
                             '[acceptance]\ncommand = "{python} check.py"\nfiles = ["check.py"]\n'
                             '[budget]\nmax_tokens = 1000\n', encoding='utf-8')
        workspace = self.root / 'ws'
        workspace.mkdir()
        (workspace / 'check.py').write_text('print("ok")\n', encoding='utf-8')
        self.ledger_path = self.root / 'ledger.sqlite'

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(['--config', str(self.config), *map(str, args)])
        return code, stdout.getvalue(), stderr.getvalue()

    def init_dev(self, directory=None):
        directory = directory or self.root / 'keys'
        code, output, error = self.cli('trust', 'init-dev', '--out', directory)
        self.assertEqual(code, 0, error)
        self.assertEqual(error, '')
        self.assertEqual(output.splitlines()[-1], NOTICE)
        table = output[output.index('[trust]'):output.index(NOTICE)]
        section = tomllib.loads(table)['trust']
        self.config.write_text(self.config.read_text(encoding='utf-8') + table, encoding='utf-8')
        code, _, error = self.cli('trust', 'sign-release', '--root-key', directory / 'root.pem',
                                  '--domain', 'TEST_ONLY', '--code', 'dev', '--slot', 'dev', '--version', '1', '--out', section['release'])
        self.assertEqual(code, 0, error)
        return directory, output, section

    def run_scripted(self, *flags):
        worker = ScriptedModel([step('finish')], 'worker')
        reviewer = ScriptedModel([review('GO')], 'reviewer')
        with patch('m2k.cli.build_model', side_effect=[worker, reviewer]):
            code, output, error = self.cli('run', *flags, self.task)
        self.assertEqual(code, 0, output + error)
        self.assertTrue(worker.calls)
        self.assertTrue(reviewer.calls)
        with Ledger(self.ledger_path) as ledger:
            submitted = ledger.events('t20', 'task.submitted')[0].payload
            self.assertTrue(ledger.verify_chain()[0])
        return submitted, error

    def report(self):
        path = self.root / 'report.html'
        with Ledger(self.ledger_path) as ledger:
            before = ledger.events()
        code, _, error = self.cli('report', 't20', '--out', path)
        self.assertEqual(code, 0, error)
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.events(), before)
        return path.read_text(encoding='utf-8')

    def test_no_trust_refuses_before_sandbox_ledger_and_models(self):
        with patch('m2k.cli.build_model') as model, patch('m2k.cli.sandboxes.choose') as sandbox:
            code, output, error = self.cli('run', self.task)
        self.assertEqual(code, 2)
        self.assertEqual(output, '')
        self.assertEqual(error, REFUSAL + '\n')
        from tests.test_startup_refusals import assert_refusal
        assert_refusal(self, self.ledger_path, 't20', 'NO_TRUST')
        model.assert_not_called()
        sandbox.assert_not_called()

    def test_invalid_contract_error_precedes_no_trust(self):
        self.task.write_text('', encoding='utf-8')
        with patch('m2k.cli.build_model') as model, patch('m2k.cli.sandboxes.choose') as sandbox:
            with self.assertRaisesRegex(SystemExit, r'invalid task contract: missing \[task\] table'):
                self.cli('run', self.task)
        self.assertFalse(self.ledger_path.exists())
        model.assert_not_called()
        sandbox.assert_not_called()

    def test_untrusted_completes_and_records_null_with_warning(self):
        submitted, error = self.run_scripted('--untrusted')
        self.assertIsNone(submitted['trust'])
        self.assertIn(WARNING + '\n', error)
        html = self.report()
        self.assertIn(f'<div class="untrusted">{BANNER}</div>', html)
        self.assertIn('.untrusted {', html)
        self.assertIn('color: #991b1b', html)
        self.assertLess(html.index(BANNER), html.index('<header>'))

    def test_configured_trust_and_untrusted_are_mutually_exclusive(self):
        # Even an incomplete configured chain must never be silently bypassed.
        with self.config.open('a', encoding='utf-8') as f:
            f.write('[trust]\n')
        with patch('m2k.cli.build_model') as model, patch('m2k.cli.sandboxes.choose') as sandbox:
            code, output, error = self.cli('run', '--untrusted', self.task)
        self.assertEqual(code, 2)
        self.assertEqual(output, '')
        self.assertEqual(error, '已配置 [trust]，不能同时用 --untrusted\n')
        from tests.test_startup_refusals import assert_refusal
        assert_refusal(self, self.ledger_path, 't20', 'CONFIG')
        model.assert_not_called()
        sandbox.assert_not_called()

    def test_init_dev_files_caps_status_fingerprint_and_no_overwrite(self):
        directory, output, section = self.init_dev()
        files = {'root.pem', 'root-pin.json', 'issuer.pem', 'issuer-pin.json', 'caps.toml',
                 'charter.json', 'revocations.json', 'release.json', 'obs'}
        self.assertEqual({p.name for p in directory.iterdir()}, files)
        self.assertEqual({p.name for p in (directory / 'obs').iterdir()}, {'obs-key.pem'})
        pin = json.loads((directory / 'root-pin.json').read_text(encoding='utf-8'))
        self.assertEqual(output.splitlines()[0], 'Root fingerprint: ' + pin['fingerprint'])
        self.assertEqual(section['domain'], 'TEST_ONLY')
        for field, filename in (('root_pin', 'root-pin.json'), ('charter', 'charter.json'),
                                ('issuer_key', 'issuer.pem'), ('revocations', 'revocations.json')):
            self.assertEqual(section[field], (directory / filename).as_posix())
        caps = tomllib.loads((directory / 'caps.toml').read_text(encoding='utf-8'))
        self.assertEqual(caps, {'tools': sorted(tools.TOOLS), 'max_rounds': 100,
                               'max_model_calls': 120, 'max_tokens': 10000000,
                               'max_commands': 100, 'network': False})
        charter = json.loads((directory / 'charter.json').read_text(encoding='utf-8'))['body']
        self.assertEqual(charter['release'], 'dev')
        self.assertEqual(charter['caps'], caps)
        revocations = json.loads((directory / 'revocations.json').read_text(encoding='utf-8'))['body']
        self.assertEqual(revocations['version'], 0)
        self.assertEqual(revocations['revoked_charters'], [])
        self.assertEqual(revocations['revoked_credentials'], [])
        code, status, error = self.cli('trust', 'status')
        self.assertEqual(code, 0, error)
        self.assertIn(pin['fingerprint'], status)
        if os.name == 'posix':
            for filename in ('root.pem', 'issuer.pem'):
                self.assertEqual((directory / filename).stat().st_mode & 0o777, 0o600)
        before = {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
        code, _, error = self.cli('trust', 'init-dev', '--out', directory)
        self.assertEqual(code, 1)
        self.assertIn('output exists', error)
        self.assertEqual({p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob('*') if p.is_file()}, before)

    def test_init_dev_accepts_existing_empty_relative_directory(self):
        directory = self.root / '测试 keys'
        directory.mkdir()
        with chdir(self.root):
            self.init_dev(Path(directory.name))
        self.assertEqual(self.cli('trust', 'status')[0], 0)

    def test_init_dev_refuses_file_without_changing_it(self):
        path = self.root / 'occupied'
        path.write_bytes(b'preserve')
        code, _, error = self.cli('trust', 'init-dev', '--out', path)
        self.assertEqual(code, 1)
        self.assertIn('output exists', error)
        self.assertEqual(path.read_bytes(), b'preserve')

    def test_generated_chain_runs_and_report_shows_chain_identifiers(self):
        self.init_dev()
        submitted, error = self.run_scripted()
        self.assertNotIn(WARNING, error)
        self.assertEqual(submitted['trust']['domain'], 'TEST_ONLY')
        html = self.report()
        self.assertNotIn(BANNER, html)
        for field in ('domain', 'root_fingerprint', 'charter_id', 'credential_id'):
            self.assertIn(submitted['trust'][field], html.split('<table>')[0])
        self.assertIn('信任链：', html)


if __name__ == '__main__':
    unittest.main()
