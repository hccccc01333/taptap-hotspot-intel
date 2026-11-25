"""Cross-process-safe output lock and atomic file write tests."""
import json
import tempfile
import unittest
from pathlib import Path

from context.file_io import atomic_write_json, output_lock


class FileIoTests(unittest.TestCase):
    def test_output_lock_excludes_other_handles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with output_lock(root):
                with self.assertRaises(TimeoutError):
                    with output_lock(root, timeout=0.05):
                        pass
            with output_lock(root, timeout=0.05):
                pass

    def test_atomic_json_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'nested' / 'value.json'
            atomic_write_json(path, {'ok': True})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'ok': True})
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == '__main__':
    unittest.main()
