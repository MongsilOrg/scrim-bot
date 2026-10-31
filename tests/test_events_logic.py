import unittest

from utils.helpers import is_csv_filename
from utils.validators import normalize_team_name


class EventsLogicTest(unittest.TestCase):
    def test_is_csv_filename(self):
        self.assertTrue(is_csv_filename("result.csv"))
        self.assertTrue(is_csv_filename("RESULT.CSV"))
        self.assertFalse(is_csv_filename("result.txt"))

    def test_normalize_team_name(self):
        self.assertEqual(normalize_team_name(" DM "), normalize_team_name("dm"))
        self.assertEqual(normalize_team_name("Team  Alpha"), normalize_team_name("team alpha"))
        self.assertNotEqual(normalize_team_name("DM"), normalize_team_name("VGX"))
        self.assertEqual(normalize_team_name(""), "")


if __name__ == "__main__":
    unittest.main()
