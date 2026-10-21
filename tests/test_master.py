import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import master


class MasterTests(unittest.TestCase):
    def setup_project(self, base):
        root=base/'master with spaces';root.mkdir()
        cfg=dict(project='Smart Wallet Research',code_version='0.2.4',source_run='runs/old',
                 diagnosis='runs/diagnosis.json',baseline_report='runs/700',followup_run='runs/continued',exports='exports')
        (root/'project.json').write_text(json.dumps(cfg))
        (root/'runs/old').mkdir(parents=True);(root/'runs/old/intake_report.json').write_text('{}')
        (root/'runs/diagnosis.json').write_text('{}');(root/'history_followup.py').write_text('')
        baseline=root/'runs/700';baseline.mkdir()
        report=dict(status='PARTIAL_UNRECONCILED',observation_count=700,accounting_ready=False,
                    side_counts=dict(buy=474),errors=[],audits=[])
        (baseline/'intake_report.json').write_text(json.dumps(report));(baseline/'report.html').write_text('<!doctype html>')
        return root,cfg

    def test_report_opens_absolute_local_uri_without_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,cfg=self.setup_project(Path(tmp))
            with patch('master.webbrowser.open',return_value=True) as browser,contextlib.redirect_stdout(io.StringIO()):
                p=master.open_report(root)
            browser.assert_called_once_with(p.as_uri())
            self.assertEqual(p,root/'runs/700/report.html')

    def test_existing_followup_never_starts_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,cfg=self.setup_project(Path(tmp));(root/'runs/continued').mkdir()
            with patch('master.subprocess.run') as run,contextlib.redirect_stdout(io.StringIO()):
                code=master.continue_history(root)
            self.assertEqual(code,2);run.assert_not_called()

    def test_continue_uses_project_paths_and_hidden_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,cfg=self.setup_project(Path(tmp))
            with patch('master.subprocess.run') as run,contextlib.redirect_stdout(io.StringIO()):
                run.return_value.returncode=0
                self.assertEqual(master.continue_history(root),0)
            args=run.call_args.args[0]
            self.assertEqual(args[0],sys.executable)
            self.assertIn(str(root/'runs/old'),args);self.assertIn(str(root/'runs/diagnosis.json'),args)
            self.assertIn('--prompt',args);self.assertEqual(run.call_args.kwargs['cwd'],str(root))

    def test_review_export_includes_results_and_excludes_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,cfg=self.setup_project(Path(tmp));folder=root/'runs/continued';(folder/'raw').mkdir(parents=True)
            (folder/'intake_report.json').write_text('{"status":"test"}')
            (folder/'raw/history_002.json').write_text('{}')
            (folder/'unrelated.txt').write_text('exclude')
            with contextlib.redirect_stdout(io.StringIO()):p=master.export_review(root)
            with zipfile.ZipFile(p) as z:
                self.assertEqual(set(z.namelist()),{'continued/intake_report.json','continued/raw/history_002.json'})
            self.assertTrue((folder/'raw/history_002.json').exists())

    def test_config_path_cannot_escape_master(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,cfg=self.setup_project(Path(tmp));cfg['exports']='../outside'
            with self.assertRaises(master.ProjectError):master.location(root,cfg,'exports')

    def test_latest_report_prefers_completed_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,cfg=self.setup_project(Path(tmp));folder=root/'runs/continued';folder.mkdir()
            self.assertEqual(master.latest_run(root),root/'runs/700')
            (folder/'intake_report.json').write_text('{}')
            self.assertEqual(master.latest_run(root),folder)


if __name__=='__main__':unittest.main()
