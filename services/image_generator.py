"""wkhtmltoimage 렌더는 blocking, async 쪽은 *_async 래퍼 사용."""
import asyncio
import os
import platform
from io import BytesIO
from typing import Dict, List, Optional

from services.notion_api import get_server_info
from services.score_aggregation import (
    COL_KILL_SCORE,
    COL_TEAM_NAME,
    COL_TOTAL_SCORE,
    KEY_RANK,
)

import imgkit

from config.logging_config import get_logger
from config.settings import settings
from utils.helpers import get_current_kst_time

logger = get_logger('image_generator')

TOURNAMENT_COLOR = '#FB9206'

if platform.system() == 'Windows':
    WKHTML_PATH = r'C:\Program Files\wkhtmltopdf\bin\wkhtmltoimage.exe'
else:
    WKHTML_PATH = '/usr/bin/wkhtmltoimage'

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'assets', 'templates')

def _load_template(name: str) -> str:
    path = os.path.join(TEMPLATES_DIR, name)
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

def _render_html_to_image(html_str: str, width: int = 800, height: int = None) -> Optional[BytesIO]:
    try:
        if not os.path.exists(WKHTML_PATH):
            logger.error(f"[이미지생성] wkhtmltoimage를 찾을 수 없음 - 경로: {WKHTML_PATH}")
            return None

        config = imgkit.config(wkhtmltoimage=WKHTML_PATH)

        options = {
            'width': width,
            'quality': 100,
            'format': 'png',
            'encoding': 'UTF-8',
            'enable-local-file-access': '',
        }
        if height:
            options['height'] = height

        img_bytes = imgkit.from_string(html_str, False, config=config, options=options)
        img_io = BytesIO(img_bytes)
        img_io.seek(0)
        return img_io
    except Exception as e:
        logger.error(f"[이미지생성] HTML 이미지 변환 실패: {e}", exc_info=True)
        return None


class ImageGenerator:

    @staticmethod
    async def generate_mmr_image_async(teams_data: dict, *, sort_by_mmr: bool = True,
                                       unverified_teams: set = None, server_info: dict = None) -> Optional[BytesIO]:
        return await asyncio.to_thread(
            ImageGenerator.generate_mmr_image, teams_data,
            sort_by_mmr=sort_by_mmr, unverified_teams=unverified_teams, server_info=server_info,
        )

    @staticmethod
    def generate_mmr_image(teams_data: dict, *, sort_by_mmr: bool = True,
                           unverified_teams: set = None, server_info: dict = None) -> Optional[BytesIO]:
        try:
            if unverified_teams is None:
                unverified_teams = set()

            if sort_by_mmr:
                verified = sorted(
                    [(n, d) for n, d in teams_data.items() if n not in unverified_teams],
                    key=lambda x: x[1].mmr,
                    reverse=True
                )
                unverified = [(n, d) for n, d in teams_data.items() if n in unverified_teams]
                ordered_teams = verified + unverified
            else:
                ordered_teams = list(teams_data.items())

            current_time = get_current_kst_time().strftime('%H:%M')

            html_str = ImageGenerator._create_mmr_html_template(ordered_teams, current_time, unverified_teams, server_info=server_info)
            return _render_html_to_image(html_str, width=1000)

        except Exception as e:
            logger.error(f"[이미지생성] MMR 이미지 생성 실패: {e}", exc_info=True)
            return None

    @staticmethod
    def _create_mmr_html_template(sorted_teams: list, current_time: str, unverified_teams: set = None,
                                  server_info: dict = None) -> str:
        if server_info is None:
            server_info = get_server_info()
        is_tournament = server_info['is_tournament']
        num_teams = len(sorted_teams)

        accent_color = TOURNAMENT_COLOR if is_tournament else '#4a9eff'
        border_color = TOURNAMENT_COLOR if is_tournament else '#3a8ee0'

        if unverified_teams is None:
            unverified_teams = set()

        rows_html = []
        for idx, (team_name, team_data) in enumerate(sorted_teams):
            is_unverified = team_name in unverified_teams
            rows_html.append(ImageGenerator._build_team_row_html(idx + 1, team_name, team_data, is_unverified=is_unverified))

        verified_count = sum(1 for name, _ in sorted_teams if name not in unverified_teams)

        body_html = ''
        for i, row_html in enumerate(rows_html):
            actual_rank = i + 1
            if i == verified_count and verified_count > 0 and verified_count < num_teams:
                body_html += row_html.replace('class="row', 'class="row divider-top', 1)
            elif actual_rank % settings.TEAMS_PER_GROUP == 0 and i < num_teams - 1:
                body_html += row_html.replace('class="row', 'class="row divider-bottom', 1)
            else:
                body_html += row_html

        header_row = """
            <tr class="header-row">
                <th class="col-rank">#</th>
                <th class="col-team">팀명</th>
                <th class="col-mmr">MMR</th>
                <th class="col-members">멤버</th>
            </tr>
        """

        template = _load_template('mmr_table.html')
        return template.format(
            accent_color=accent_color,
            border_color=border_color,
            header_row=header_row,
            body_html=body_html,
        )

    @staticmethod
    def _build_team_row_html(rank: int, team_name: str, team_data, *, is_unverified: bool = False) -> str:
        mmr = team_data.mmr
        players = list(team_data.players)
        staff = list(team_data.staff)
        is_seed = getattr(team_data, 'is_seed', False)
        seed_name = getattr(team_data, 'seed_name', None)

        row_class = 'row-unverified' if is_unverified else ('row-even' if (rank - 1) % 2 == 0 else 'row-odd')

        def _esc(s: str) -> str:
            return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        team_main = f'<div class="cell-main">{_esc(team_name)}</div>'
        if is_seed:
            seed_label = f'시드 {_esc(seed_name)}' if seed_name else '시드'
            team_sub = f'<div class="cell-sub seed">{seed_label}</div>'
        else:
            team_sub = ''
        team_cell = team_main + team_sub

        sep = '<span class="separator">,</span>'
        players_text = sep.join(_esc(p) for p in players) if players else '-'
        members_main = f'<div class="cell-main">{players_text}</div>'
        members_sub = ''
        if staff:
            staff_text = ', '.join(_esc(s) for s in staff)
            members_sub = f'<div class="cell-sub staff">{staff_text}</div>'
        members_cell = members_main + members_sub

        mmr_display = '<td class="mmr-unverified">점검</td>' if is_unverified else f'<td class="mmr-value">{mmr:.2f}</td>'

        return f"""<tr class="row {row_class}">
    <td>{rank}</td>
    <td>{team_cell}</td>
    {mmr_display}
    <td>{members_cell}</td>
</tr>
"""

    @staticmethod
    async def generate_score_table_image_async(team_data: List[Dict]) -> Optional[BytesIO]:
        return await asyncio.to_thread(ImageGenerator.generate_score_table_image, team_data)

    @staticmethod
    def generate_score_table_image(team_data: List[Dict]) -> Optional[BytesIO]:
        if not team_data:
            return ImageGenerator._create_empty_score_image()

        html_str = ImageGenerator._build_score_html(team_data)
        return _render_html_to_image(html_str, width=900)

    @staticmethod
    def _build_score_html(team_data: List[Dict]) -> str:
        is_tournament = get_server_info()['is_tournament']
        accent_color = TOURNAMENT_COLOR if is_tournament else '#4a9eff'

        rows_html = ''
        for team in team_data:
            rank = team.get(KEY_RANK, 0)
            team_name = team.get(COL_TEAM_NAME, 'Unknown')
            kill_score = team.get(COL_KILL_SCORE, 0)
            total_score = team.get(COL_TOTAL_SCORE, 0)

            rank_class = ''
            if rank == 1:
                rank_class = 'rank-gold'
            elif rank == 2:
                rank_class = 'rank-silver'
            elif rank == 3:
                rank_class = 'rank-bronze'

            row_class = 'row-even' if rank % 2 == 0 else 'row-odd'

            safe_name = team_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

            ks_display = int(kill_score) if kill_score == int(kill_score) else kill_score
            ts_display = int(total_score) if total_score == int(total_score) else total_score

            rows_html += f"""
            <tr class="{row_class}">
                <td class="col-rank {rank_class}">{rank}</td>
                <td class="col-team">{safe_name}</td>
                <td class="col-ks">{ks_display}</td>
                <td class="col-ts accent">{ts_display}</td>
            </tr>
            """

        template = _load_template('score_table.html')
        return template.format(accent_color=accent_color, rows_html=rows_html)

    @staticmethod
    def _create_empty_score_image() -> Optional[BytesIO]:
        html = _load_template('empty_score.html')
        return _render_html_to_image(html, width=900)
