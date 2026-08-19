import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import upgrade


class UpgradeTestCase(unittest.TestCase):
    def test_compare_versions_considers_extra_components(self):
        self.assertEqual(upgrade.compare_versions("1.2.3.1", "1.2.3"), 1)
        self.assertEqual(upgrade.compare_versions("1.2", "1.2.0"), 0)

    def test_validation_rejects_python_syntax_error(self):
        files = {name: b"x = 1\n" for name in upgrade.SOURCE_FILES}
        files["VERSION"] = b"VERSION=9.9.9\n"
        files["manager.py"] = b"def broken(:\n"
        self.assertFalse(upgrade.validate_source_files(files))

    def test_install_rolls_back_when_a_replace_fails(self):
        with tempfile.TemporaryDirectory() as tempdir:
            originals = {}
            source = {}
            for name in upgrade.SOURCE_FILES:
                originals[name] = f"old-{name}".encode()
                source[name] = f"new-{name}".encode()
                with open(os.path.join(tempdir, name), "wb") as handle:
                    handle.write(originals[name])

            real_atomic_write = upgrade._atomic_write

            def flaky_write(path, content):
                if os.path.basename(path) == "monitor.py" and content.startswith(b"new-"):
                    raise OSError("simulated failure")
                return real_atomic_write(path, content)

            with mock.patch.object(upgrade, "BASE_DIR", tempdir), \
                 mock.patch.object(upgrade, "_atomic_write", side_effect=flaky_write):
                self.assertFalse(upgrade.install_source_files(source))

            for name, expected in originals.items():
                with open(os.path.join(tempdir, name), "rb") as handle:
                    self.assertEqual(handle.read(), expected)

    def test_install_can_restore_a_missing_program_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = {name: f"new-{name}".encode() for name in upgrade.SOURCE_FILES}
            missing = "pve.py"
            for name in upgrade.SOURCE_FILES:
                if name == missing:
                    continue
                with open(os.path.join(tempdir, name), "wb") as handle:
                    handle.write(b"old")

            with mock.patch.object(upgrade, "BASE_DIR", tempdir):
                self.assertTrue(upgrade.install_source_files(source))

            with open(os.path.join(tempdir, missing), "rb") as handle:
                self.assertEqual(handle.read(), source[missing])

    def test_database_backup_uses_consistent_sqlite_snapshot(self):
        with tempfile.TemporaryDirectory() as tempdir:
            data_dir = os.path.join(tempdir, "data")
            os.makedirs(data_dir)
            db_path = os.path.join(data_dir, "traffic.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE sample (value INTEGER)")
            conn.execute("INSERT INTO sample VALUES (42)")
            conn.commit()
            conn.close()

            with mock.patch.object(upgrade, "BASE_DIR", tempdir), \
                 mock.patch.object(upgrade, "DATA_DIR", data_dir):
                self.assertTrue(upgrade.backup_data())

            backups = [name for name in os.listdir(tempdir) if name.startswith("data.bak.")]
            self.assertEqual(len(backups), 1)
            backup_db = os.path.join(tempdir, backups[0], "traffic.db")
            conn = sqlite3.connect(backup_db)
            value = conn.execute("SELECT value FROM sample").fetchone()[0]
            conn.close()
            self.assertEqual(value, 42)


if __name__ == "__main__":
    unittest.main()
