import unittest
from types import SimpleNamespace

from commands import room_code
from tests.test_room_code_logic import _FakeChannel


def _notice(weather_line):
    children = [SimpleNamespace(content="## 📢 스크림 공지 - 1라운드"), SimpleNamespace(content=weather_line)]
    return SimpleNamespace(components=[SimpleNamespace(children=children)], embeds=[])


class SubWeatherFromChannelTest(unittest.IsolatedAsyncioTestCase):
    def test_chosen_sub_weather(self):
        self.assertEqual(room_code._chosen_sub_weather(_notice("날씨: `비`, `강풍`\n시작: `20:00`")), "강풍")
        self.assertIsNone(room_code._chosen_sub_weather(_notice("날씨: `비`, 서브 날씨는 아래 버튼으로 고릅니다")))
        self.assertEqual(room_code._chosen_sub_weather(_notice("🌤️ `비` / `자색 안개`")), "자색 안개")

    async def test_deleted_notice_frees_weather(self):
        notices = [_notice("날씨: `모래바람`, `무풍`"), _notice("날씨: `비`, `벼락`")]
        self.assertEqual(await room_code.get_used_sub_weathers(_FakeChannel(notices)), ["무풍", "벼락"])
        # 두 번째 공지를 지우고 다시 올리는 경우
        self.assertEqual(await room_code.get_used_sub_weathers(_FakeChannel(notices[:1])), ["무풍"])
        self.assertEqual(await room_code.get_round_number(_FakeChannel(notices[:1])), 2)


if __name__ == "__main__":
    unittest.main()
