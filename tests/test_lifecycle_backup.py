import glob
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from models.team_backup import TeamBackup


class BackupQuarantineTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, 'teams_backup.json')
        self.backup = TeamBackup(SimpleNamespace(BACKUP_FILE=self.path))

    def _write(self, text):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(text)

    def _corrupt_files(self):
        return glob.glob(self.path + '.corrupt-*')

    def test_unparsable_backup_is_renamed_not_deleted(self):
        self._write('{"_meta": {"scrim_day": 6')
        with self.assertLogs('scrim-bot.team_backup', level='ERROR'):
            self.assertFalse(self.backup.should_restore())
        self.assertFalse(os.path.exists(self.path))
        kept = self._corrupt_files()
        self.assertEqual(len(kept), 1)
        with open(kept[0], encoding='utf-8') as f:
            self.assertEqual(f.read(), '{"_meta": {"scrim_day": 6')

    def test_backup_without_meta_is_renamed(self):
        self._write(json.dumps({'teams': {}}))
        self.assertFalse(self.backup.should_restore())
        self.assertEqual(len(self._corrupt_files()), 1)

    def test_valid_backup_stays(self):
        self._write(json.dumps({'_meta': {'scrim_day': 6}}))
        self.assertTrue(self.backup.should_restore())
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(self._corrupt_files(), [])

    def test_load_failure_renames_backup(self):
        from models.team_data_manager import TeamDataManager

        mgr = TeamDataManager()
        mgr.BACKUP_FILE = self.path
        self._write(json.dumps({'_meta': {'scrim_day': 6}, 'teams': {'팀A': 'not-a-dict'}}))
        self.assertFalse(mgr.load_backup())
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(len(self._corrupt_files()), 1)

    def test_missing_backup_is_not_an_error(self):
        self.assertFalse(self.backup.should_restore())
        self.assertEqual(self._corrupt_files(), [])


if __name__ == '__main__':
    unittest.main()
