"""Offline release guard tests; never call podman or read real credentials."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('registration_deploy', Path(__file__).with_name('deploy.py'))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class ReleaseGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'turnstile.secret.env'
        self.patch = patch.object(deploy, 'SECRET_FILE', self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def put(self, text, mode=0o600):
        self.path.write_text(text)
        self.path.chmod(mode)

    def test_missing_secret_cannot_prepare_production(self):
        with patch.object(deploy, 'verify_baseline'), patch.object(deploy, 'clone') as clone:
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                deploy.prepare()
            clone.assert_not_called()

    def test_missing_secret_is_reported_without_value(self):
        self.assertEqual(deploy.load_secret(required=False), '')

    def test_world_readable_secret_is_rejected(self):
        self.put('TURNSTILE_SECRET=unit-secret-do-not-use\n', 0o644)
        with self.assertRaisesRegex(RuntimeError, '0600'):
            deploy.load_secret()

    def test_secret_symlink_is_rejected(self):
        target = self.root / 'target'
        target.write_text('TURNSTILE_SECRET=unit-secret-do-not-use\n')
        target.chmod(0o600)
        self.path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, 'regular file'):
            deploy.load_secret()

    def test_empty_placeholder_and_cloudflare_test_keys_are_rejected(self):
        for value in ('', '<REPLACE>', 'YOUR_SECRET_KEY', '1x0000000000000000000000000000000AA'):
            with self.subTest(value=value):
                self.put('TURNSTILE_SECRET=' + value + '\n')
                with self.assertRaises(RuntimeError):
                    deploy.load_secret()

    def test_other_variables_cannot_be_injected_through_secret_file(self):
        self.put('TURNSTILE_SECRET=unit-secret-do-not-use\nREGISTRATION_TURNSTILE_ALLOW_TEST_KEYS=1\n')
        with self.assertRaisesRegex(RuntimeError, 'only TURNSTILE_SECRET'):
            deploy.load_secret()

    def test_config_fingerprint_changes_after_rotation(self):
        self.put('TURNSTILE_SECRET=unit-secret-one\n')
        first = deploy.effective_config_fingerprint()
        self.put('TURNSTILE_SECRET=unit-secret-two\n')
        self.assertNotEqual(first, deploy.effective_config_fingerprint())

    def test_multiline_environment_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'multiline'):
            deploy.write_env(self.root / 'output.env', {'KEY': 'value\nINJECTION=1'})

    def test_cutover_cannot_stop_service_without_approval(self):
        with patch.object(deploy.sys, 'argv', ['deploy.py', 'cutover-pt']), patch.object(deploy, 'run') as run:
            with self.assertRaisesRegex(SystemExit, 'explicit user deployment approval'):
                deploy.cutover_pt()
            run.assert_not_called()

    def test_store_secret_requires_interactive_terminal(self):
        with patch.object(deploy.sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'interactive terminal'):
                deploy.store_secret()
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
