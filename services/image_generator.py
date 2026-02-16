"""
이미지 생성 서비스
"""
import os
from io import BytesIO
from typing import Optional

import imgkit
import pandas as pd

from config.logging_config import get_logger
from config.settings import settings

logger = get_logger('image_generator')

# wkhtmltoimage 공통 설정
WKHTML_PATH = '/usr/bin/wkhtmltoimage'


def _render_html_to_image(html_str: str, width: int = 800, height: int = None) -> Optional[BytesIO]:
    """HTML 문자열을 PNG 이미지로 변환하는 공통 유틸리티."""
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
        logger.error(f"[이미지생성] HTML→이미지 변환 실패: {e}", exc_info=True)
        return None


class ImageGenerator:
    """이미지 생성 클래스"""

    @staticmethod
    def generate_result_image(df: pd.DataFrame) -> Optional[BytesIO]:
        """결과 이미지를 생성하는 함수"""
        try:
            html_str = ImageGenerator._create_html_template(df)
            return _render_html_to_image(html_str, width=800, height=600)
        except Exception as e:
            logger.error(f"[이미지생성] 이미지 생성 실패: {e}", exc_info=True)
            return None

    @staticmethod
    def _create_html_template(df: pd.DataFrame) -> str:
        """결과 이미지용 HTML 템플릿 생성"""
        table_html = df.to_html(index=False, escape=False, classes='table')

        html_template = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{
                    font-family: 'Arial', sans-serif;
                    margin: 20px;
                    background-color: #f5f5f5;
                }}
                .header {{
                    text-align: center;
                    margin-bottom: 20px;
                }}
                .header h1 {{
                    color: #333;
                    margin: 0;
                }}
                .table {{
                    width: 100%;
                    border-collapse: collapse;
                    background-color: white;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                }}
                .table th, .table td {{
                    padding: 12px;
                    text-align: left;
                    border-bottom: 1px solid #ddd;
                }}
                .table th {{
                    background-color: #4a9eff;
                    color: white;
                    font-weight: bold;
                }}
                .table tr:nth-child(even) {{
                    background-color: #f9f9f9;
                }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1>스크림 결과</h1>
            </div>
            {table_html}
        </body>
        </html>
        """
        return html_template

    @staticmethod
    def generate_mmr_image(teams_data: dict) -> Optional[BytesIO]:
        """MMR 이미지를 HTML/CSS 기반으로 생성하는 함수"""
        try:
            # 팀 정보를 MMR 순으로 정렬 (TeamData 구조)
            sorted_teams = sorted(
                teams_data.items(),
                key=lambda x: x[1].mmr if hasattr(x[1], 'mmr') else 0,
                reverse=True
            )

            from utils.helpers import get_current_kst_time
            current_time = get_current_kst_time().strftime('%H:%M')

            num_teams = len(sorted_teams)
            use_two_columns = num_teams > 16
            img_width = 1200 if use_two_columns else 1000

            html_str = ImageGenerator._create_mmr_html_template(sorted_teams, current_time)
            return _render_html_to_image(html_str, width=img_width)

        except Exception as e:
            logger.error(f"[이미지생성] MMR 이미지 생성 실패: {e}", exc_info=True)
            return None

    @staticmethod
    def _create_mmr_html_template(sorted_teams: list, current_time: str) -> str:
        """MMR 테이블 HTML 템플릿 생성"""
        num_teams = len(sorted_teams)
        use_two_columns = num_teams > 16

        # 팀 행 HTML 생성
        rows_html = []
        for idx, (team_name, team_data) in enumerate(sorted_teams):
            rows_html.append(ImageGenerator._build_team_row_html(idx + 1, team_name, team_data))

        if use_two_columns:
            mid = (num_teams + 1) // 2
            left_rows = rows_html[:mid]
            right_rows = rows_html[mid:]
            table_width = '48%'
        else:
            left_rows = rows_html
            right_rows = None
            table_width = '100%'

        def _build_table_html(rows: list, rank_offset: int = 0) -> str:
            """테이블 하나의 HTML을 구성"""
            body = ''
            for i, row_html in enumerate(rows):
                actual_rank = rank_offset + i + 1
                # 8팀 구분선: 8의 배수 뒤에 구분선 클래스 추가
                if actual_rank % 8 == 0 and i < len(rows) - 1:
                    # 이 행에 구분선 하단 클래스 추가
                    row_html = row_html.replace('class="row', 'class="row divider-bottom', 1)
                body += row_html
            return body

        left_body = _build_table_html(left_rows, rank_offset=0)
        right_body = _build_table_html(right_rows, rank_offset=(num_teams + 1) // 2) if right_rows else ''

        header_row = """
            <tr class="header-row">
                <th class="col-rank">#</th>
                <th class="col-team">팀명</th>
                <th class="col-mmr">MMR</th>
                <th class="col-player">선수1</th>
                <th class="col-player">선수2</th>
                <th class="col-player">선수3</th>
                <th class="col-player">선수4</th>
                <th class="col-staff">스태프</th>
            </tr>
        """

        right_table_html = ''
        if use_two_columns:
            right_table_html = f"""
            <table class="mmr-table" style="width: {table_width};">
                <thead>{header_row}</thead>
                <tbody>{right_body}</tbody>
            </table>
            """

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
    * {{
        margin: 0;
        padding: 0;
        box-sizing: border-box;
    }}
    body {{
        font-family: 'NanumGothic', 'Nanum Gothic', 'Malgun Gothic', sans-serif;
        background-color: #1a1a1a;
        color: #ffffff;
        padding: 12px;
    }}
    .container {{
        display: {'flex' if use_two_columns else 'block'};
        {'gap: 16px;' if use_two_columns else ''}
        {'justify-content: center;' if use_two_columns else ''}
    }}
    .mmr-table {{
        width: {table_width};
        border-collapse: collapse;
        table-layout: fixed;
    }}
    .mmr-table th, .mmr-table td {{
        padding: 8px 6px;
        text-align: center;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        font-size: 14px;
        line-height: 1.4;
    }}
    .header-row th {{
        background-color: #4a9eff;
        color: #ffffff;
        font-size: 16px;
        font-weight: bold;
        padding: 10px 6px;
        border-bottom: 2px solid #3a8ee0;
    }}
    .col-rank {{ width: 5%; }}
    .col-team {{ width: 14%; }}
    .col-mmr {{ width: 11%; }}
    .col-player {{ width: 14%; }}
    .col-staff {{ width: 14%; }}
    .row-even td {{
        background-color: #2d2d2d;
    }}
    .row-odd td {{
        background-color: #333333;
    }}
    .mmr-value {{
        color: #4a9eff;
        font-weight: bold;
    }}
    .divider-bottom td {{
        border-bottom: 2px solid #ff6b6b !important;
    }}
    td {{
        border-bottom: 1px solid #404040;
    }}
</style>
</head>
<body>
<div class="container">
    <table class="mmr-table" style="width: {table_width};">
        <thead>{header_row}</thead>
        <tbody>{left_body}</tbody>
    </table>
    {right_table_html}
</div>
</body>
</html>"""
        return html

    @staticmethod
    def _build_team_row_html(rank: int, team_name: str, team_data) -> str:
        """한 팀의 HTML 테이블 행을 생성"""
        mmr = team_data.mmr
        players = list(team_data.players)
        staff = list(team_data.staff)

        # 선수 정렬 (닉네임 길이 순)
        def _sort_key(nickname):
            if nickname == "-":
                return (0, 0)
            length = sum(2 if ord(c) >= 128 else 1 for c in nickname)
            first_char_priority = 0 if ord(nickname[0]) >= 128 else 1
            return (length, first_char_priority)

        sorted_players = sorted(players, key=_sort_key, reverse=True)
        while len(sorted_players) < 4:
            sorted_players.append("-")

        sorted_staff = sorted(staff, key=_sort_key, reverse=True)
        staff_str = ', '.join(sorted_staff) if sorted_staff else '-'

        row_class = 'row-even' if (rank - 1) % 2 == 0 else 'row-odd'

        # HTML 이스케이프
        def _esc(s: str) -> str:
            return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        return f"""<tr class="row {row_class}">
    <td>{rank}</td>
    <td>{_esc(team_name)}</td>
    <td class="mmr-value">{mmr:.2f}</td>
    <td>{_esc(sorted_players[0])}</td>
    <td>{_esc(sorted_players[1])}</td>
    <td>{_esc(sorted_players[2])}</td>
    <td>{_esc(sorted_players[3])}</td>
    <td>{_esc(staff_str)}</td>
</tr>
"""
