import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from commands import role_request
from config.settings import settings
from models import role_requests
from models.role_requests import APPROVED, PENDING, REJECTED
from models.team_data import TeamData
from services.discord_service import DiscordService
from utils.helpers import KST

ADMIN_ROLE = 777
EARLY = datetime(2026, 11, 25, 19, 30, tzinfo=KST)
LATE = datetime(2026, 11, 25, 19, 30, 1, tzinfo=KST)


def _text(view):
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _labels(view):
    return [item.item.label for item in view.walk_children() if isinstance(item, discord.ui.DynamicItem)]


class FakeRole(SimpleNamespace):
    def __hash__(self):
        return hash(self.name)


def _member(user_id, name="", *, admin=False, bot=False):
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.bot = bot
    member.display_name = name or f"user{user_id}"
    member.global_name = None
    member.name = f"user{user_id}"
    member.roles = [SimpleNamespace(id=ADMIN_ROLE if admin else -1)]
    member.add_roles = AsyncMock()
    return member


def _request(kind=role_request.SUB, **changes):
    request = {
        'kind': kind, 'date': '2026-11-25', 'letter': 'A', 'team': 'Team ER', 'applicant': 10,
        'members': [20], 'out': ['가', '나'] if kind == role_request.SUB else [],
        'sanction': '주의' if kind == role_request.SUB else None, 'status': PENDING,
    }
    request.update(changes)
    return request


def _interaction(user, message_id=500):
    interaction = MagicMock()
    interaction.user = user
    interaction.message = SimpleNamespace(id=message_id)
    done = []
    interaction.response.is_done = MagicMock(side_effect=lambda: bool(done))
    interaction.response.send_message = AsyncMock(side_effect=lambda **_: done.append(True))
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock(side_effect=lambda **_: done.append(True))
    interaction.response.defer = AsyncMock(side_effect=lambda **_: done.append(True))
    interaction.original_response = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _sent_text(interaction):
    return _text(interaction.response.send_message.await_args.kwargs['view'])


class StoreCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = EARLY
        for target in (
            patch.object(settings, "ADMIN_ROLE_IDS", [ADMIN_ROLE]),
            patch.object(role_requests, "PATH", os.path.join(folder.name, "role_requests.json")),
            patch.object(role_requests, "_state", None),
            patch.object(role_requests, "get_current_kst_time", side_effect=lambda: self.now),
            patch.object(role_request, "get_current_kst_time", side_effect=lambda: self.now),
        ):
            target.start()
            self.addCleanup(target.stop)


class StoreTest(StoreCase):
    def test_request_closes_only_once(self):
        role_requests.add(1, _request())

        closed = role_requests.close(1, APPROVED, sanction='주의')

        self.assertEqual((closed['status'], closed['sanction']), (APPROVED, '주의'))
        self.assertIsNone(role_requests.close(1, REJECTED, sanction=None))

    def test_survives_restart(self):
        role_requests.set_panel_id(9)
        role_requests.add(1, _request())
        role_requests._state = None

        self.assertEqual(role_requests.panel_id(), 9)
        self.assertEqual(role_requests.get(1)['team'], 'Team ER')

    def test_past_day_requests_are_not_handled_and_get_removed(self):
        role_requests.add(1, _request(date='2026-11-24'))
        self.assertIsNone(role_requests.get(1))

        role_requests.add(2, _request())
        self.assertEqual(list(role_requests._load()['requests']), ['2'])

    def test_granted_ids_cover_only_approved_requests_of_the_group(self):
        role_requests.add(1, _request(members=[20, 21]))
        role_requests.add(2, _request(members=[22]))
        role_requests.add(3, _request(members=[23], letter='B'))
        role_requests.close(1, APPROVED, sanction=None)
        role_requests.close(3, APPROVED, sanction=None)

        self.assertEqual(role_requests.granted_ids('A'), {20, 21})


class ViewTest(unittest.TestCase):
    def test_sanction_turns_into_warning_after_thirty_minutes_before_start(self):
        self.assertEqual(role_request.sanction_kind(EARLY), '주의')
        self.assertEqual(role_request.sanction_kind(LATE), '경고')

    def test_panel_has_one_button_per_request_kind(self):
        view = role_request.panel_view()

        self.assertEqual(_text(view), "## 권한 신청")
        self.assertEqual(_labels(view), ["대타", "스태프"])

    def test_pending_sub_card(self):
        view = role_request.request_view(_request(), operators=[30])

        self.assertEqual(_text(view), "\n".join([
            "<@30>",
            "## 대타 신청",
            "**A조** `Team ER`",
            "**대타** <@20>",
            "**출전하지 않는 선수** 가, 나",
            "**제재** 주의",
            "**신청** <@10>",
        ]))
        self.assertEqual(_labels(view), ["승인", "제재 없이 승인", "거절"])

    def test_pending_staff_card(self):
        view = role_request.request_view(_request(role_request.STAFF, members=[20, 21]))

        self.assertEqual(_text(view), "\n".join([
            "## 스태프 권한 신청",
            "**A조** `Team ER`",
            "**스태프** <@20> <@21>",
            "**신청** <@10>",
        ]))
        self.assertEqual(_labels(view), ["승인", "거절"])

    def test_closed_card_has_no_buttons_or_operator_mention(self):
        view = role_request.request_view(_request(status=APPROVED, sanction=None), operators=[30])

        self.assertEqual(_text(view), "\n".join([
            "## 대타 승인",
            "**A조** `Team ER`",
            "**대타** <@20>",
            "**출전하지 않는 선수** 가, 나",
            "**신청** <@10>",
        ]))
        self.assertEqual(_labels(view), [])
        self.assertIn("## 대타 거절", _text(role_request.request_view(_request(status=REJECTED, sanction=None))))


class OpenTest(StoreCase):
    def _manager(self, *, started=True, team="Team ER"):
        roster = TeamData(name="Team ER", players=["가", "나", "다", "라"])
        other = TeamData(name="Other", players=["마", "바", "사"])
        manager = SimpleNamespace(
            is_team_assignment_started=started,
            groups=[[("Other", other, 0.0)], [("Team ER", roster, 0.0)]] if started else None,
            find_user_team=MagicMock(return_value=team),
        )
        target = patch.object(
            role_request.BotManager, "get_instance",
            return_value=SimpleNamespace(get_team_data_manager=lambda: manager),
        )
        target.start()
        self.addCleanup(target.stop)

    async def test_before_assignment(self):
        self._manager(started=False)
        interaction = _interaction(_member(10))

        await role_request._open(interaction, role_request.SUB)

        self.assertEqual(_sent_text(interaction), f"## 오류\n{role_request.NOT_ASSIGNED_TEXT}")
        interaction.response.send_modal.assert_not_awaited()

    async def test_only_members_of_an_assigned_team_can_request(self):
        self._manager(team=None)
        interaction = _interaction(_member(10))

        await role_request._open(interaction, role_request.STAFF)

        self.assertEqual(_sent_text(interaction), f"## 오류\n{role_request.NOT_IN_TEAM_TEXT}")

    async def test_sub_modal_lists_the_roster_of_the_applicants_team(self):
        self._manager()
        interaction = _interaction(_member(10))

        await role_request._open(interaction, role_request.SUB)

        modal = interaction.response.send_modal.await_args.args[0]
        self.assertEqual((modal.letter, modal.team_name), ("B", "Team ER"))
        self.assertEqual([option.label for option in modal.out_select.options], ["가", "나", "다", "라"])
        self.assertEqual((modal.out_select.min_values, modal.out_select.max_values), (2, 3))

    async def test_staff_modal(self):
        self._manager()
        interaction = _interaction(_member(10))

        await role_request._open(interaction, role_request.STAFF)

        self.assertIsInstance(interaction.response.send_modal.await_args.args[0], role_request.StaffModal)


class SubmitTest(StoreCase):
    def _modal(self, players, subs, out):
        modal = role_request.SubModal("A", "Team ER", players)
        modal.sub_select._values = subs
        modal.out_select._values = out
        return modal

    async def test_three_player_roster_gives_up_one_player_per_sub(self):
        modal = self._modal(["가", "나", "다"], [_member(20)], ["나"])
        self.assertEqual((modal.out_select.min_values, modal.out_select.max_values), (1, 2))

        with patch.object(role_request, "_submit", new=AsyncMock()) as submit:
            await modal.on_submit(_interaction(_member(10)))

        self.assertEqual(submit.await_args.args[1], _request(out=["나"]))

    async def test_four_player_roster_gives_up_two_players_for_one_sub(self):
        self.now = LATE
        modal = self._modal(["가", "나", "다", "라"], [_member(20)], ["라", "가"])

        with patch.object(role_request, "_submit", new=AsyncMock()) as submit:
            await modal.on_submit(_interaction(_member(10)))

        self.assertEqual(submit.await_args.args[1], _request(out=["가", "라"], sanction='경고'))

    async def test_wrong_number_of_absent_players_is_rejected(self):
        modal = self._modal(["가", "나", "다", "라"], [_member(20)], ["가", "나", "다"])
        interaction = _interaction(_member(10))

        with patch.object(role_request, "_submit", new=AsyncMock()) as submit:
            await modal.on_submit(interaction)

        submit.assert_not_awaited()
        self.assertEqual(
            _sent_text(interaction), "## 오류\n대타가 1명이면 출전하지 않는 선수는 2명입니다.\n다시 신청해주세요.",
        )

    async def test_roster_player_and_bot_cannot_be_subs(self):
        for sub, text in (
            (_member(20, "가"), "이 팀의 로스터 선수는 대타로 선택할 수 없습니다."),
            (_member(21, bot=True), "봇은 대타로 선택할 수 없습니다."),
        ):
            modal = self._modal(["가", "나", "다"], [sub], ["나"])
            interaction = _interaction(_member(10))

            with patch.object(role_request, "_submit", new=AsyncMock()) as submit:
                await modal.on_submit(interaction)

            submit.assert_not_awaited()
            self.assertEqual(_sent_text(interaction), f"## 오류\n{text}")

    async def test_card_mentions_only_the_operator_and_panel_moves_below_it(self):
        role_requests.set_panel_id(400)
        interaction = _interaction(_member(10))
        channel = interaction.channel
        channel.send = AsyncMock(side_effect=[SimpleNamespace(id=500), SimpleNamespace(id=501)])
        old_panel = channel.get_partial_message.return_value
        old_panel.delete = AsyncMock()
        pick = {'date': '2026-11-25', 'choices': {'A': '30'}, 'targets': ['30', '31']}
        manager = SimpleNamespace(get_schedule_manager=lambda: SimpleNamespace(pick=pick))

        with patch.object(role_request.BotManager, "get_instance", return_value=manager):
            await role_request._submit(interaction, _request())

        card = channel.send.await_args_list[0].kwargs
        self.assertTrue(_text(card['view']).startswith("<@30>\n## 대타 신청"))
        self.assertEqual([user.id for user in card['allowed_mentions'].users], [30])
        self.assertEqual(role_requests.get(500)['status'], PENDING)
        self.assertEqual(_labels(channel.send.await_args_list[1].kwargs['view']), ["대타", "스태프"])
        channel.get_partial_message.assert_called_once_with(400)
        old_panel.delete.assert_awaited_once()
        self.assertEqual(role_requests.panel_id(), 501)

    async def test_operators_of_the_day_are_mentioned_until_the_group_is_picked(self):
        pick = {'date': '2026-11-25', 'choices': {'B': '30'}, 'targets': ['30', '31']}
        manager = SimpleNamespace(get_schedule_manager=lambda: SimpleNamespace(pick=pick))

        with patch.object(role_request.BotManager, "get_instance", return_value=manager):
            self.assertEqual(role_request._operator_ids('A'), [30, 31])
            pick['date'] = '2026-11-24'
            self.assertEqual(role_request._operator_ids('A'), [])


class DecisionTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.role = FakeRole(name="A조")
        self.sub = _member(20)
        self.guild = SimpleNamespace(roles=[self.role], get_member=lambda uid: self.sub if uid == 20 else None)
        self.warnings = AsyncMock(return_value="2명에게 주의를 부여했습니다.")
        self.manager = SimpleNamespace(is_team_assignment_started=True)
        for target in (
            patch.object(role_request, "apply_roster_warnings", new=self.warnings),
            patch.object(
                role_request.BotManager, "get_instance",
                return_value=SimpleNamespace(get_team_data_manager=lambda: self.manager),
            ),
        ):
            target.start()
            self.addCleanup(target.stop)

    def _click(self, *, admin=True):
        interaction = _interaction(_member(1, admin=admin))
        interaction.guild = self.guild
        return interaction

    async def test_only_operators_can_decide(self):
        role_requests.add(500, _request())
        interaction = self._click(admin=False)

        await role_request._decide(interaction, role_request.APPROVE)

        self.assertEqual(role_requests.get(500)['status'], PENDING)
        self.sub.add_roles.assert_not_awaited()
        self.warnings.assert_not_awaited()

    async def test_approval_grants_role_and_sanctions_absent_players_once(self):
        role_requests.add(500, _request())
        first, second = self._click(), self._click()

        await role_request._decide(first, role_request.APPROVE)
        await role_request._decide(second, role_request.APPROVE)

        self.sub.add_roles.assert_awaited_once_with(self.role, reason="권한 신청 승인")
        self.warnings.assert_awaited_once_with(first, 'Team ER', ['가', '나'], "대타", kind='주의')
        card = first.response.edit_message.await_args.kwargs['view']
        self.assertIn("## 대타 승인", _text(card))
        self.assertEqual(_labels(card), [])
        self.assertEqual(
            _text(first.followup.send.await_args.kwargs['view']), "## 대타 승인\n2명에게 주의를 부여했습니다.",
        )
        self.assertEqual(_sent_text(second), f"## 오류\n{role_request.CLOSED_TEXT}")

    async def test_request_is_not_approved_after_the_groups_are_cleared(self):
        role_requests.add(500, _request())
        self.manager.is_team_assignment_started = False
        interaction = self._click()

        await role_request._decide(interaction, role_request.APPROVE)

        self.assertEqual(_sent_text(interaction), f"## 오류\n{role_request.CLOSED_TEXT}")
        self.sub.add_roles.assert_not_awaited()

    async def test_late_request_gives_warning_with_its_reason(self):
        role_requests.add(500, _request(sanction='경고'))
        interaction = self._click()

        await role_request._decide(interaction, role_request.APPROVE)

        self.warnings.assert_awaited_once_with(
            interaction, 'Team ER', ['가', '나'], role_request.LATE_REASON, kind='경고',
        )

    async def test_approval_without_sanction(self):
        role_requests.add(500, _request())
        interaction = self._click()

        await role_request._decide(interaction, role_request.WAIVE)

        self.sub.add_roles.assert_awaited_once()
        self.warnings.assert_not_awaited()
        self.assertNotIn("제재", _text(interaction.response.edit_message.await_args.kwargs['view']))
        interaction.followup.send.assert_not_awaited()

    async def test_rejection_changes_nothing_but_the_card(self):
        role_requests.add(500, _request())
        interaction = self._click()

        await role_request._decide(interaction, role_request.REJECT)

        self.assertIn("## 대타 거절", _text(interaction.response.edit_message.await_args.kwargs['view']))
        self.sub.add_roles.assert_not_awaited()
        self.warnings.assert_not_awaited()

    async def test_operator_is_told_when_a_role_could_not_be_granted(self):
        role_requests.add(500, _request(role_request.STAFF, members=[20, 21]))
        interaction = self._click()

        await role_request._decide(interaction, role_request.APPROVE)

        self.sub.add_roles.assert_awaited_once()
        self.assertEqual(
            _text(interaction.followup.send.await_args.kwargs['view']),
            "## 스태프 권한 승인\n조 역할을 부여하지 못했습니다.\n직접 부여해주세요: <@21>",
        )


class KeptRoleTest(StoreCase):
    async def test_roster_change_keeps_the_role_of_approved_members(self):
        role = FakeRole(name="A조")
        approved, stranger = _member(20), _member(21)
        approved.roles = stranger.roles = [role]
        guild = SimpleNamespace(roles=[role], members=[approved, stranger])
        role_requests.add(500, _request())
        role_requests.close(500, APPROVED, sanction=None)
        service = DiscordService(processor=SimpleNamespace(client=None), team_data_manager=SimpleNamespace())
        service._apply_role_updates = AsyncMock()

        await service.update_group_roles(guild, 'A', [("Team ER", TeamData(name="Team ER", players=["가"]), 0.0)])

        self.assertEqual([update[0] for update in service._apply_role_updates.await_args.args[0]], [stranger])


if __name__ == '__main__':
    unittest.main()
