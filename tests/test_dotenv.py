import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import okx_reader as m
import master


class DotenvTests(unittest.TestCase):
    def values_file(self, root, text=None):
        path=Path(root)/'.env'
        if text is None:text='OKX_API_KEY=fixture-key\nOKX_SECRET_KEY=fixture-secret\nOKX_PASSPHRASE=fixture-pass\n'
        path.write_text(text,encoding='utf-8-sig')
        return path

    def test_complete_file_skips_prompt_even_without_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=self.values_file(tmp)
            with patch.dict(os.environ,{},clear=True),patch.object(m,'DOTENV_PATH',p),patch('okx_reader.sys.stdin.isatty',return_value=False),patch('okx_reader.getpass.getpass') as prompt:
                self.assertEqual(m.credentials(prompt=True),['fixture-key','fixture-secret','fixture-pass'])
                prompt.assert_not_called()

    def test_nonempty_environment_wins_and_empty_environment_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=self.values_file(tmp)
            with patch.dict(os.environ,{'OKX_API_KEY':'environment-key','OKX_SECRET_KEY':''},clear=True),patch.object(m,'DOTENV_PATH',p):
                self.assertEqual(m.credentials(),['environment-key','fixture-secret','fixture-pass'])

    def test_prompt_only_requests_missing_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=self.values_file(tmp,'OKX_API_KEY=fixture-key\nOKX_SECRET_KEY=fixture-secret\n')
            with patch.dict(os.environ,{},clear=True),patch.object(m,'DOTENV_PATH',p),patch('okx_reader.sys.stdin.isatty',return_value=True),patch('okx_reader.getpass.getpass',return_value='fixture-pass') as prompt:
                self.assertEqual(m.credentials(prompt=True),['fixture-key','fixture-secret','fixture-pass'])
                self.assertEqual(prompt.call_count,1);self.assertIn('OKX_PASSPHRASE',prompt.call_args.args[0])

    def test_quotes_bom_and_special_characters_are_literal(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=self.values_file(tmp,'# comment\nexport OKX_API_KEY = "key#=$HOME%PATH%"\nOKX_SECRET_KEY=\'C:\\literal\\secret\'\nOKX_PASSPHRASE=pass=with#hash\n')
            vals=m.dotenv_values(p)
            self.assertEqual(vals['OKX_API_KEY'],'key#=$HOME%PATH%')
            self.assertEqual(vals['OKX_SECRET_KEY'],'C:\\literal\\secret')
            self.assertEqual(vals['OKX_PASSPHRASE'],'pass=with#hash')

    def test_unknown_keys_are_ignored_and_environment_is_not_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=self.values_file(tmp,'OTHER=ignored\n'+self.values_file_text())
            with patch.dict(os.environ,{},clear=True),patch.object(m,'DOTENV_PATH',p):
                self.assertEqual(m.credentials(),['fixture-key','fixture-secret','fixture-pass'])
                self.assertEqual(dict(os.environ),{})
                self.assertNotIn('OTHER',m.dotenv_values(p))

    def values_file_text(self):
        return 'OKX_API_KEY=fixture-key\nOKX_SECRET_KEY=fixture-secret\nOKX_PASSPHRASE=fixture-pass\n'

    def test_file_location_is_independent_of_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=self.values_file(root);other=root/'elsewhere';other.mkdir()
            previous=Path.cwd()
            try:
                os.chdir(other)
                with patch.dict(os.environ,{},clear=True),patch.object(m,'DOTENV_PATH',p):
                    self.assertEqual(m.credentials()[0],'fixture-key')
            finally:os.chdir(previous)

    def test_invalid_assignment_error_never_echoes_values(self):
        for text in ['OKX_SECRET_KEY="never-echo-this\n','OKX_SECRET_KEY=never-echo-this\nOKX_SECRET_KEY=other\n']:
            with tempfile.TemporaryDirectory() as tmp:
                p=self.values_file(tmp,text)
                with self.assertRaises(m.IntakeError) as cm:m.dotenv_values(p)
                self.assertIn('ENV_FILE_INVALID',str(cm.exception));self.assertNotIn('never-echo-this',str(cm.exception))

    def test_missing_file_and_empty_values_remain_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'.env'
            self.assertEqual(m.dotenv_values(p),{})
            self.values_file(tmp,'OKX_API_KEY=\nOKX_SECRET_KEY=""\nOKX_PASSPHRASE=\n')
            with patch.dict(os.environ,{},clear=True),patch.object(m,'DOTENV_PATH',p):
                with self.assertRaisesRegex(m.IntakeError,'CREDENTIALS_MISSING'):m.credentials()

    def test_unreadable_file_error_does_not_echo_original_exception(self):
        with patch.object(Path,'read_text',side_effect=PermissionError('never-echo-this')):
            with self.assertRaises(m.IntakeError) as cm:m.dotenv_values(Path('ignored'))
        self.assertNotIn('never-echo-this',str(cm.exception))

    def test_existing_redaction_works_with_file_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=self.values_file(tmp)
            with patch.dict(os.environ,{},clear=True),patch.object(m,'DOTENV_PATH',p):creds=m.credentials()
            body=json.dumps(dict(code='0',data={'echo':'fixture-key fixture-secret fixture-pass'})).encode()
            cl=m.Client(creds,transport=lambda u,h:(200,body),interval=0)
            env=cl.get(m.LEADERBOARD,{})
            for value in creds:self.assertNotIn(value,json.dumps(env))

    def test_review_zip_excludes_env_at_root_and_inside_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'runs/continued';folder.mkdir(parents=True)
            (root/'project.json').write_text(json.dumps(dict(followup_run='runs/continued',exports='exports')))
            (root/'.env').write_text('fixture-secret');(folder/'.env').write_text('fixture-secret')
            (folder/'intake_report.json').write_text('{}')
            with contextlib.redirect_stdout(io.StringIO()):archive=master.export_review(root)
            with zipfile.ZipFile(archive) as z:
                self.assertEqual(z.namelist(),['continued/intake_report.json'])
                self.assertNotIn(b'fixture-secret',z.read(z.namelist()[0]))


if __name__=='__main__':unittest.main()
