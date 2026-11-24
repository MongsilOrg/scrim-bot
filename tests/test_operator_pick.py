import asyncio
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from commands import operator_pick
from config.settings import settings
from models import schedule_manager
from models.schedule_manager import ScheduleManager
from services.discord_service import DiscordService
from utils import operator_log
from utils.helpers import KST

MONDAY = datetime(2026, 11, 23, tzinfo=KST)
TUESDAY = date(2026, 11, 24)
ADMIN_ROLE = 777


def _manager(availability=None):
    mgr = ScheduleManager()
    mgr.week_label = "test"
    mgr.week_start = MONDAY
    mgr.availability = availability or {uid: {0, 1, 2, 3, 4} for uid in "abcdef"}
    mgr.admin_names = {uid: uid.upper() for uid in mgr.availability}
    return mgr


def _text(view):
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _admin(user_id=1, admin=True):
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.roles = [SimpleNamespace(id=ADMIN_ROLE if admin else -1)]
    return member


class OperatorLogTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, "log.jsonl")
        self.now = datetime(2026, 11, 24, 20, tzinfo=KST)
        for target in (
            patch.object(settings, "ADMIN_ROLE_IDS", [ADMIN_ROLE]),
            patch.object(operator_log, "LOG_FILE", self.path),
            patch.object(operator_log, "get_current_kst_time", side_effect=lambda: self.now),
        ):
            target.start()
            self.addCleanup(target.stop)

    def test_one_round_in_a_group_counts_as_deployed(self):
        lead, helper = _admin(1), _admin(2)
        operator_log.record(lead, "a", 1, "room_code")
        operator_log.record(lead, "a", 1, "weather")
        operator_log.record(lead, "a", 2, "room_code")
        operator_log.record(helper, "b", 1, "csv")

        self.assertEqual(operator_log.deployed(TUESDAY), {"A": ["1"], "B": ["2"]})

    def test_after_midnight_belongs_to_the_day_it_started(self):
        member = _admin(1)
        operator_log.record(member, "A", 4, "room_code")
        self.now = datetime(2026, 11, 25, 0, 30, tzinfo=KST)
        operator_log.record(member, "A", 5, "room_code")

        self.assertEqual(operator_log.deployed(TUESDAY), {"A": ["1"]})
        self.assertEqual(operator_log.deployed(date(2026, 11, 25)), {})

    def test_ignores_non_admin_and_broken_lines(self):
        operator_log.record(_admin(3, admin=False), "A", 1, "room_code")
        self.assertFalse(os.path.exists(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{broken\n")
            for round_number in (1, 2):
                f.write(json.dumps({"date": "2026-11-24", "group": "A", "round": round_number, "user_id": "9"}) + "\n")

        self.assertEqual(operator_log.deployed(TUESDAY), {"A": ["9"]})


@patch.object(ScheduleManager, "save_backup")
class RankingTest(unittest.TestCase):
    def test_fewer_season_deployments_come_first(self, _save):
        mgr = _manager({"a": {0}, "b": {0}, "c": {0}})
        mgr.deploy_history = {"2026-11-16": ["a", "b"], "2026-11-17": ["a", "b"], "2026-11-18": ["a", "c"]}

        self.assertEqual(mgr.ranked_for_day(0), ["c", "b", "a"])

    def test_tie_goes_to_the_one_deployed_longer_ago(self, _save):
        mgr = _manager({"a": {0}, "b": {0}})
        mgr.deploy_history = {"2026-11-16": ["b"], "2026-11-17": ["a"]}

        self.assertEqual(mgr.ranked_for_day(0), ["b", "a"])

    def test_counts_only_since_season_start(self, _save):
        mgr = _manager({"a": {0}, "b": {0}})
        mgr.deploy_history = {"2026-11-10": ["b"], "2026-11-11": ["b"], "2026-11-23": ["a"]}
        mgr.count_since = "2026-11-23"

        self.assertEqual(mgr.season_counts(), {"a": 1})
        self.assertEqual(mgr.ranked_for_day(0), ["b", "a"])

    def test_confirm_applies_once_and_updates_week(self, _save):
        mgr = _manager()

        self.assertTrue(mgr.confirm_deployment(TUESDAY, "A", ["a"]))
        self.assertEqual(mgr.actual_deployments[1], ["a"])
        with patch.object(schedule_manager, "get_current_kst_time", return_value=datetime(2026, 11, 24, 21, tzinfo=KST)):
            mgr.set_deployment(TUESDAY, [])
        self.assertFalse(mgr.confirm_deployment(TUESDAY, "A", ["a"]))

        self.assertNotIn(1, mgr.actual_deployments)
        self.assertNotIn("2026-11-24", mgr.deploy_history)

    def test_set_deployment_replaces_the_day_and_reports_change(self, _save):
        mgr = _manager()
        mgr.confirm_deployment(TUESDAY, "A", ["a", "b"])

        self.assertTrue(mgr.set_deployment(TUESDAY, ["b", "c"]))
        self.assertFalse(mgr.set_deployment(TUESDAY, ["c", "b"]))
        self.assertEqual(mgr.deploy_history["2026-11-24"], ["b", "c"])
        self.assertEqual(mgr.actual_deployments[1], ["b", "c"])

        self.assertTrue(mgr.set_deployment(TUESDAY, []))
        self.assertNotIn("2026-11-24", mgr.deploy_history)
        self.assertNotIn(1, mgr.actual_deployments)

    def test_operator_dropped_on_labs_is_not_added_back_by_later_confirm(self, _save):
        mgr = _manager()
        mgr.confirm_deployment(TUESDAY, "B", ["a"])

        with patch.object(schedule_manager, "get_current_kst_time", return_value=datetime(2026, 11, 24, 21, tzinfo=KST)):
            mgr.set_deployment(TUESDAY, ["b"])
        mgr.confirm_deployment(TUESDAY, "A", ["a", "c"])
        self.assertEqual(mgr.deploy_history["2026-11-24"], ["b", "c"])

        # 화면에서 다시 넣으면 뺀 기록을 지움
        with patch.object(schedule_manager, "get_current_kst_time", return_value=datetime(2026, 11, 24, 22, tzinfo=KST)):
            mgr.set_deployment(TUESDAY, ["a", "b", "c"])
        self.assertEqual(mgr.dropped, {})

    def test_same_labs_edit_is_applied_once_and_survives_restart(self, save):
        mgr = _manager()
        now = patch.object(schedule_manager, "get_current_kst_time", return_value=datetime(2026, 11, 24, 21, tzinfo=KST))
        with now:
            self.assertTrue(mgr.set_deployment(TUESDAY, ["b"], 5))
            mgr.confirm_deployment(TUESDAY, "A", ["c"])
            # 변경 번호가 오르지 않아 같은 수정을 다시 받으면 그 사이의 조 확정을 지우지 않음
            self.assertFalse(mgr.set_deployment(TUESDAY, ["b"], 5))
            self.assertEqual(mgr.deploy_history["2026-11-24"], ["b", "c"])
            self.assertEqual(mgr.applied, {"2026-11-24": 5})
            # 명단이 같아도 새 번호는 저장해 재시작 뒤에 다시 반영하지 않음
            saved = save.call_count
            self.assertFalse(mgr.set_deployment(TUESDAY, ["b", "c"], 6))
            self.assertEqual(save.call_count, saved + 1)
            self.assertTrue(mgr.set_deployment(TUESDAY, ["b"], 7))
        self.assertEqual(mgr.deploy_history["2026-11-24"], ["b"])

    def test_set_deployment_outside_the_week_keeps_history_only(self, _save):
        mgr = _manager()

        self.assertTrue(mgr.set_deployment(date(2026, 11, 10), ["a"]))

        self.assertEqual(mgr.deploy_history, {"2026-11-10": ["a"]})
        self.assertEqual(mgr.actual_deployments, {})

    def test_confirm_after_week_change_keeps_history_only(self, _save):
        mgr = _manager()
        mgr.week_start = datetime(2026, 11, 30, tzinfo=KST)

        mgr.confirm_deployment(date(2026, 11, 27), "A", ["a"])

        self.assertEqual(mgr.deploy_history, {"2026-11-27": ["a"]})
        self.assertEqual(mgr.actual_deployments, {})


class BackupTest(unittest.TestCase):
    def test_old_backup_fills_history_from_this_week(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "schedule.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "week_label": "test", "week_start": MONDAY.isoformat(),
                    "availability": {"a": [0, 1]}, "actual_deployments": {"0": ["a"]},
                }, f)
            with patch("models.schedule_manager.BACKUP_PATH", path):
                mgr = ScheduleManager()
                self.assertTrue(mgr.load_backup())
                self.assertEqual(mgr.deploy_history, {"2026-11-23": ["a"]})

                mgr.open_pick(TUESDAY, ["A"])
                mgr.count_since = "2026-11-23"
                mgr.save_backup()
                restored = ScheduleManager()
                restored.load_backup()

            self.assertEqual(restored.pick["targets"], ["a"])
            self.assertEqual(restored.count_since, "2026-11-23")
            self.assertEqual(restored.deploy_history, {"2026-11-23": ["a"]})

    def test_restart_keeps_picks_and_lists(self):
        with tempfile.TemporaryDirectory() as folder, patch("models.schedule_manager.BACKUP_PATH", os.path.join(folder, "schedule.json")):
            mgr = _manager()
            mgr.deploy_history = {f"2026-11-{10 + i:02d}": list("abcdef"[i:]) for i in range(1, 6)}
            mgr.open_pick(TUESDAY, ["A", "B", "C"])
            mgr.pick_group("e", "A")
            mgr.decline("b")
            restored = ScheduleManager()
            restored.load_backup()

            self.assertEqual(restored.pick, mgr.pick)
            self.assertEqual(restored.pick["targets"], ["e", "a", "c"])
            self.assertEqual(restored.pick_group("f", "B")["demoted"], ["c"])
            self.assertEqual(restored.pick_group("e", "A")["promoted"], ["c"])

    def test_backup_without_order_uses_listed_order(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "schedule.json")
            pick = {
                "date": "2026-11-24", "message_id": 5, "groups": ["A", "B"], "targets": ["b", "a"], "reserves": ["c"],
                "choices": {"A": "b"}, "declined": ["d"], "closed": False, "notice_ids": [],
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"week_label": "test", "week_start": MONDAY.isoformat(), "availability": {}, "pick": pick}, f)
            with patch("models.schedule_manager.BACKUP_PATH", path):
                mgr = ScheduleManager()
                mgr.load_backup()

                self.assertEqual(mgr.pick_group("c", "B")["demoted"], ["a"])
                self.assertEqual(mgr.pick["reserves"], ["a"])


@patch.object(ScheduleManager, "save_backup")
class PickStateTest(unittest.TestCase):
    def _open(self, letters=("A", "B", "C")):
        mgr = _manager()
        # 투입 횟수로 순위를 a < b < ... < f 로 고정
        mgr.deploy_history = {f"2026-11-{10 + i:02d}": list("abcdef"[i:]) for i in range(1, 6)}
        mgr.open_pick(TUESDAY, list(letters))
        return mgr

    def test_targets_are_top_ranked_and_rest_are_reserves(self, _save):
        mgr = self._open()

        self.assertEqual(mgr.pick["targets"], ["a", "b", "c"])
        self.assertEqual(mgr.pick["reserves"], ["d", "e", "f"])

    def test_pick_move_cancel_and_taken(self, _save):
        mgr = self._open()

        self.assertEqual(mgr.pick_group("a", "B")["status"], "picked")
        self.assertEqual(mgr.pick_group("b", "B")["status"], "taken")
        self.assertEqual(mgr.pick_group("a", "C")["status"], "picked")
        self.assertEqual(mgr.pick["choices"], {"C": "a"})
        self.assertEqual(mgr.pick_group("a", "C")["status"], "cancelled")
        self.assertEqual(mgr.pick["choices"], {})
        self.assertEqual(mgr.pick["targets"], ["a", "b", "c"])

    def test_reserve_pick_moves_lowest_waiting_target_to_reserves(self, _save):
        mgr = self._open()

        self.assertEqual(mgr.pick_group("b", "B"), {"status": "picked", "promoted": [], "demoted": []})
        result = mgr.pick_group("e", "A")

        self.assertEqual(result, {"status": "picked", "promoted": [], "demoted": ["c"]})
        self.assertEqual(mgr.pick["targets"], ["b", "e", "a"])
        self.assertEqual(mgr.pick["reserves"], ["c", "d", "f"])

        self.assertEqual(mgr.pick_group("e", "A"), {"status": "cancelled", "promoted": ["c"], "demoted": []})
        self.assertEqual(mgr.pick["targets"], ["b", "a", "c"])
        self.assertEqual(mgr.pick["reserves"], ["d", "e", "f"])

    def test_no_more_picks_when_every_group_is_taken(self, _save):
        mgr = self._open()
        for uid, letter in (("d", "A"), ("e", "B"), ("f", "C")):
            mgr.pick_group(uid, letter)

        self.assertEqual(mgr.pick["targets"], ["d", "e", "f"])
        self.assertEqual(mgr.pick["reserves"], ["a", "b", "c"])
        self.assertEqual(mgr.pick_group("a", "A")["status"], "full")
        self.assertEqual(mgr.pick_group("d", "B")["status"], "full")

        result = mgr.decline("e")

        self.assertEqual((result["status"], result["promoted"]), ("declined", "a"))
        self.assertEqual(mgr.pick_group("c", "B")["demoted"], ["a"])
        self.assertEqual(mgr.close_pick(), [])

    def test_same_group_goes_to_whoever_picked_first(self, _save):
        mgr = self._open()

        results = [mgr.pick_group(uid, "A")["status"] for uid in ("d", "a")]

        self.assertEqual(results, ["picked", "taken"])
        self.assertEqual(mgr.pick["choices"], {"A": "d"})

    def test_others_may_pick_only_spare_groups(self, _save):
        mgr = self._open()

        self.assertEqual(mgr.pick_group("z", "A")["status"], "not_allowed")
        for uid in "cdef":
            mgr.decline(uid)
        self.assertEqual(mgr.pick_group("z", "A")["status"], "picked")
        self.assertEqual(mgr.pick["targets"], ["z", "a", "b"])
        self.assertEqual(mgr.pick_group("z", "B")["status"], "picked")
        self.assertEqual(mgr.pick["choices"], {"B": "z"})
        mgr.pick_group("z", "B")
        self.assertNotIn("z", mgr.pick["targets"] + mgr.pick["reserves"])

    def test_decline_promotes_first_reserve(self, _save):
        mgr = self._open()
        mgr.pick_group("a", "A")

        result = mgr.decline("a")

        self.assertEqual((result["status"], result["promoted"], result["assigned"]), ("declined", "d", None))
        self.assertEqual(mgr.pick["targets"], ["b", "c", "d"])
        self.assertEqual(mgr.pick["choices"], {})
        self.assertEqual(mgr.decline("e")["status"], "reserve_removed")
        self.assertEqual(mgr.pick["reserves"], ["f"])
        self.assertEqual(mgr.decline("z")["status"], "not_target")

    def test_close_fills_remaining_groups_in_rank_order(self, _save):
        mgr = self._open()
        mgr.pick_group("b", "A")

        self.assertEqual(mgr.close_pick(), [("a", "B"), ("c", "C")])
        self.assertEqual(mgr.close_pick(), [])
        self.assertEqual(mgr.pick_group("a", "B")["status"], "closed")
        self.assertEqual(mgr.pick["choices"]["B"], "a")
        result = mgr.decline("c")
        self.assertEqual((result["promoted"], result["assigned"]), ("d", "C"))

    def test_reopen_same_day_keeps_choices_of_remaining_groups(self, _save):
        mgr = self._open()
        mgr.pick_group("c", "A")
        mgr.pick_group("a", "C")
        mgr.decline("b")

        mgr.open_pick(TUESDAY, ["A", "B"])

        self.assertEqual(mgr.pick["choices"], {"A": "c"})
        self.assertEqual(mgr.pick["targets"], ["c", "a"])
        self.assertNotIn("b", mgr.pick["targets"] + mgr.pick["reserves"])


class FakeRole(SimpleNamespace):
    def __hash__(self):
        return hash(self.name)


class GroupRoleTest(unittest.TestCase):
    def test_operator_outside_teams_loses_group_role(self):
        role = FakeRole(name="A조")
        operator = SimpleNamespace(id=11, display_name="운영", name="op", nick=None, global_name=None, roles=[role])
        guild = SimpleNamespace(members=[operator])

        updates = DiscordService._build_role_updates(guild, {"A": role}, {"A": set()})

        self.assertEqual(updates, [(operator, {role}, set())])


class PickFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mgr = _manager({"11": {1}, "12": {1}, "13": {1}})
        self.mgr.save_backup = MagicMock()
        self.now = datetime(2026, 11, 24, 17, 1, tzinfo=KST)
        self.holidays = set()
        self.roles = {letter: FakeRole(name=f"{letter}조") for letter in "AB"}
        self.members = {}
        for uid in (11, 12, 13):
            member = _admin(uid)
            member.display_name = str(uid)
            member.add_roles = AsyncMock()
            member.remove_roles = AsyncMock()
            self.members[uid] = member
        self.sent = []

        async def send(**kwargs):
            self.sent.append(kwargs)
            return SimpleNamespace(id=100 + len(self.sent))

        self.channel = MagicMock()
        self.channel.send = AsyncMock(side_effect=send)
        self.channel.get_partial_message.return_value.delete = AsyncMock()
        self.channel.get_partial_message.return_value.edit = AsyncMock()
        self.guild = MagicMock()
        self.guild.roles = list(self.roles.values())
        self.guild.get_member.side_effect = self.members.get
        self.guild.get_channel.return_value = self.channel
        self.client = MagicMock()
        self.client.get_guild.return_value = self.guild

        bot_manager = MagicMock()
        bot_manager.get_instance.return_value.get_schedule_manager.return_value = self.mgr
        for target in (
            patch.object(settings, "ADMIN_ROLE_IDS", [ADMIN_ROLE]),
            patch.object(operator_pick, "BotManager", bot_manager),
            patch.object(operator_pick, "refresh_dashboard", new=AsyncMock()),
            patch.object(operator_pick, "schedule_members", side_effect=lambda guild: list(self.members.values())),
            patch.object(operator_pick.holidays_api, "get_rest_day_info", new=AsyncMock(side_effect=self._rest_info)),
            patch.object(operator_pick, "start_close_task"),
            patch.object(operator_pick.holidays_api, "regular_season_start", new=AsyncMock(return_value=MONDAY)),
            patch.object(operator_pick.labs_api, "load_state", return_value={}),
            patch.object(operator_log, "get_current_kst_time", side_effect=lambda: self.now),
            patch.object(operator_log, "deployed", return_value={"A": ["11"]}),
        ):
            target.start()
            self.addCleanup(target.stop)

    async def _rest_info(self, day):
        return {"is_rest_day": day.weekday() >= 5 or day in self.holidays}

    def _interaction(self, uid, message_id):
        interaction = MagicMock()
        interaction.user = self.members[uid]
        interaction.guild = self.guild
        interaction.channel = self.channel
        interaction.message = SimpleNamespace(id=message_id)
        interaction.response.edit_message = AsyncMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        return interaction

    async def test_post_then_pick_leaves_roles_alone(self):
        await operator_pick.post_pick(self.client, self.guild, ["A", "B"])

        self.assertEqual(self.mgr.count_since, "2026-11-23")
        self.assertEqual(self.mgr.pick["message_id"], 101)
        self.assertEqual(len(self.mgr.pick["targets"]), 2)
        self.assertTrue(self.sent[0]["allowed_mentions"].users)

        target = int(self.mgr.pick["targets"][0])
        interaction = self._interaction(target, 101)
        await operator_pick._handle(interaction, "B")

        interaction.response.edit_message.assert_awaited_once()
        self.assertEqual(self.mgr.pick["choices"], {"B": str(target)})

        await operator_pick._handle(self._interaction(target, 101), "B")
        self.assertEqual(self.mgr.pick["choices"], {})
        self.members[target].add_roles.assert_not_awaited()
        self.members[target].remove_roles.assert_not_awaited()

    async def test_old_message_is_rejected(self):
        await operator_pick.post_pick(self.client, self.guild, ["A", "B"])

        interaction = self._interaction(11, 999)
        await operator_pick._handle(interaction, "A")

        interaction.response.edit_message.assert_not_awaited()
        self.assertEqual(self.mgr.pick["choices"], {})

    async def test_decline_notifies_promoted_reserve(self):
        await operator_pick.post_pick(self.client, self.guild, ["A", "B"])
        target, reserve = self.mgr.pick["targets"][0], self.mgr.pick["reserves"][0]

        await operator_pick._handle(self._interaction(int(target), 101), operator_pick.DECLINE)

        self.assertIn(reserve, self.mgr.pick["targets"])
        self.assertEqual(self.mgr.pick["notice_ids"], [102])
        self.assertTrue(self.sent[1]["allowed_mentions"].users)

    async def test_reserve_pick_notifies_the_operator_moved_to_reserves(self):
        await operator_pick.post_pick(self.client, self.guild, ["A", "B"])
        waiting, reserve = self.mgr.pick["targets"][1], self.mgr.pick["reserves"][0]

        await operator_pick._handle(self._interaction(int(reserve), 101), "A")

        self.assertEqual(self.mgr.pick["reserves"], [waiting])
        self.assertEqual(self.mgr.pick["notice_ids"], [102])
        text = _text(self.sent[1]["view"])
        self.assertIn(f"<@{waiting}>", text)
        self.assertNotIn(f"<@{reserve}>", text)
        self.assertIn("님이 A조를 선택해 예비로 옮겨졌습니다.", text)
        self.assertIn("남은 조를 선택하면 다시 진행으로 바뀝니다.", text)
        self.assertNotIn("조 선택", text)

        await operator_pick._handle(self._interaction(int(reserve), 101), "A")

        self.assertEqual(self.mgr.pick["reserves"], [reserve])
        self.assertIn(f"<@{waiting}>", _text(self.sent[2]["view"]))

    async def test_two_operators_pressing_the_same_group_at_once(self):
        await operator_pick.post_pick(self.client, self.guild, ["A", "B"])
        first, second = self._interaction(11, 101), self._interaction(12, 101)

        await asyncio.gather(operator_pick._handle(first, "A"), operator_pick._handle(second, "A"))

        self.assertEqual(self.mgr.pick["choices"], {"A": "11"})
        first.response.edit_message.assert_awaited_once()
        second.response.edit_message.assert_not_awaited()

    async def test_close_assigns_and_next_post_confirms_previous_day(self):
        await operator_pick.post_pick(self.client, self.guild, ["A", "B"])

        await operator_pick.close_pick(self.client)

        self.assertTrue(self.mgr.pick["closed"])
        self.assertEqual(set(self.mgr.pick["choices"]), {"A", "B"})
        self.assertEqual(len(self.sent), 2)
        await operator_pick.close_pick(self.client)
        self.assertEqual(len(self.sent), 2)

        self.now = datetime(2026, 11, 25, 17, 1, tzinfo=KST)
        await operator_pick.post_pick(self.client, self.guild, ["A"])

        self.assertEqual(self.mgr.deploy_history, {"2026-11-24": ["11"]})
        self.assertEqual(self.mgr.confirmed, ["2026-11-24:A", "2026-11-24:B"])
        self.assertEqual(self.mgr.pick["date"], "2026-11-25")

    async def test_rest_day_removes_message_without_new_pick(self):
        await operator_pick.post_pick(self.client, self.guild, ["A", "B"])
        self.now = datetime(2026, 11, 28, 17, 1, tzinfo=KST)

        await operator_pick.post_pick(self.client, self.guild, ["A"])

        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(self.mgr.pick["message_id"])

    async def test_holiday_posts_no_pick(self):
        self.holidays = {TUESDAY}

        await operator_pick.post_pick(self.client, self.guild, ["A", "B"])

        self.assertEqual(self.sent, [])
        self.assertIsNone(self.mgr.pick)

    async def test_rest_day_lookup_failure_still_posts_on_a_weekday(self):
        with patch.object(operator_pick.holidays_api, "get_rest_day_info", new=AsyncMock(side_effect=RuntimeError("down"))):
            await operator_pick.post_pick(self.client, self.guild, ["A", "B"])

        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.mgr.pick["message_id"], 101)

    async def test_pick_message_has_no_footer_and_names_waiting_row(self):
        await operator_pick.post_pick(self.client, self.guild, ["A", "B"])

        view = self.sent[0]["view"]
        text = _text(view)
        self.assertIn("**미선택** ", text)
        self.assertNotIn("**진행**", text)
        self.assertNotIn(settings.EMBED_FOOTER_TEXT, text)
        self.assertFalse([item for item in view.walk_children() if isinstance(item, discord.ui.Separator)])
        labels = [item.item.label for item in view.walk_children() if isinstance(item, operator_pick.PickButton)]
        self.assertEqual(labels, ["A조", "B조", "오늘 불참"])

    async def test_close_with_an_empty_group_mentions_operators_without_one(self):
        self.mgr.availability = {"11": {1}}
        await operator_pick.post_pick(self.client, self.guild, ["A", "B"])

        await operator_pick.close_pick(self.client)

        self.assertEqual(self.mgr.pick["choices"], {"A": "11"})
        text = _text(self.sent[-1]["view"])
        self.assertIn("<@12> <@13>\nB조를 맡을 운영진이 없습니다.\n위 메시지에서 조를 선택해주세요.", text)
        self.assertNotIn("<@11>", text)
        self.assertTrue(self.sent[-1]["allowed_mentions"].users)
        self.assertEqual(len(self.mgr.pick["notice_ids"]), 2)
        # 마감 뒤에는 명단에 없던 운영진도 빈 조를 선택
        await operator_pick._handle(self._interaction(12, 101), "B")
        self.assertEqual(self.mgr.pick["choices"], {"A": "11", "B": "12"})

    async def test_pick_message_is_deleted_once_every_group_is_confirmed(self):
        await operator_pick.post_pick(self.client, self.guild, ["A", "B"])
        delete = self.channel.get_partial_message.return_value.delete

        await operator_pick.confirm_group(self.client, "2026-11-24", "A")
        delete.assert_not_awaited()
        self.assertEqual(self.mgr.pick["message_id"], 101)

        await operator_pick.confirm_group(self.client, "2026-11-24", "B")
        delete.assert_awaited_once()
        self.assertIsNone(self.mgr.pick["message_id"])



if __name__ == "__main__":
    unittest.main()
