"""Installer configuration and installed CLI regressions; no installation or network."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import build_installer as builder
import shorts_core as core


class InstallerTests(unittest.TestCase):
    def test_inno_script_preserves_windows_paths_and_non_admin_install(self):
        text = builder.installer_script(Path(r'C:\Build Folder\payload'), Path(r'C:\Output Folder'), '1.2.3')
        self.assertIn(r'C:\Build Folder\payload\*', text)
        self.assertIn('PrivilegesRequired=lowest', text)
        self.assertIn(r'{app}\runtime\pythonw.exe', text)
        self.assertIn('AppVersion=1.2.3', text)
        self.assertNotIn('\x07', text)
        self.assertNotIn('\r', text)
        self.assertNotIn('[UninstallDelete]', text)

    def test_source_allowlist_excludes_secrets_and_user_data(self):
        self.assertIn('shorts_studio.py', builder.SOURCES)
        self.assertIn('scene_generation.py', builder.SOURCES)
        self.assertTrue(all(name.endswith('.py') for name in builder.SOURCES))
        self.assertTrue(all(not name.startswith('test_') for name in builder.SOURCES))
        self.assertNotIn('token.json', builder.SOURCES)
        self.assertNotIn('data', builder.SOURCES)

    def test_installed_task_command_uses_bootstrap_not_user_data_folder(self):
        with patch.object(core, 'INSTALLED', True), patch.object(core, 'RESOURCE_ROOT', Path(r'C:\Installed\app')), patch.object(core.sys, 'frozen', False, create=True):
            command = core.app_command()
        self.assertEqual(command[0], core.sys.executable)
        self.assertEqual(command[1], '-I')
        self.assertEqual(command[2], str(Path(r'C:\Installed\app') / 'launch_studio.py'))

    def test_installer_override_is_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            compiler = Path(folder) / 'ISCC.exe'
            compiler.write_bytes(b'fixture')
            self.assertEqual(builder.find_iscc(compiler), compiler.resolve())
            with self.assertRaises(RuntimeError):
                builder.find_iscc(Path(folder) / 'missing.exe')


if __name__ == '__main__':
    unittest.main()
