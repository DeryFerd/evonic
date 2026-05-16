"""Regression tests for backend.zip_validator."""

import os
import tempfile
import unittest
import zipfile

from backend import zip_validator as zv


class TestZipValidator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_zip(self, name: str, entries: dict) -> str:
        path = os.path.join(self.tmp, name)
        with zipfile.ZipFile(path, 'w') as zf:
            for arcname, content in entries.items():
                zf.writestr(arcname, content)
        return path

    def test_accepts_valid_plugin_like_zip(self):
        path = self._write_zip('ok.zip', {
            'my-skill/skill.json': '{}',
            'my-skill/tool.py': 'print("hi")',
        })
        ok, err = zv.validate_upload_zip(path)
        self.assertTrue(ok, err)

    def test_rejects_path_traversal(self):
        path = self._write_zip('traversal.zip', {'../evil.py': 'x'})
        ok, err = zv.validate_upload_zip(path)
        self.assertFalse(ok)
        self.assertIn('traversal', err.lower())

    def test_rejects_absolute_paths(self):
        path = self._write_zip('abs.zip', {'/etc/passwd': 'x'})
        ok, err = zv.validate_upload_zip(path)
        self.assertFalse(ok)
        self.assertIn('absolute', err.lower())

    def test_rejects_disallowed_extension(self):
        path = self._write_zip('exe.zip', {'payload.exe': 'MZ'})
        ok, err = zv.validate_upload_zip(path)
        self.assertFalse(ok)
        self.assertIn('not permitted', err)

    def test_rejects_empty_zip(self):
        path = self._write_zip('empty.zip', {})
        ok, err = zv.validate_upload_zip(path)
        self.assertFalse(ok)
        self.assertIn('empty', err.lower())

    def test_rejects_non_zip_file(self):
        path = os.path.join(self.tmp, 'not.zip')
        with open(path, 'w') as f:
            f.write('not a zip')
        ok, err = zv.validate_upload_zip(path)
        self.assertFalse(ok)
        self.assertIn('zip', err.lower())


if __name__ == '__main__':
    unittest.main()
