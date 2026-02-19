"""
이미지 생성 서비스
"""
import os
from io import BytesIO
from typing import Optional

import imgkit
import pandas as pd

from config.logging_config import get_logger

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

            html_str = ImageGenerator._create_mmr_html_template(sorted_teams, current_time)
            return _render_html_to_image(html_str, width=1000)

        except Exception as e:
            logger.error(f"[이미지생성] MMR 이미지 생성 실패: {e}", exc_info=True)
            return None

    @staticmethod
    def _create_mmr_html_template(sorted_teams: list, current_time: str) -> str:
        """MMR 테이블 HTML 템플릿 생성"""
        num_teams = len(sorted_teams)

        # 팀 행 HTML 생성
        rows_html = []
        for idx, (team_name, team_data) in enumerate(sorted_teams):
            rows_html.append(ImageGenerator._build_team_row_html(idx + 1, team_name, team_data))

        # 8팀 구분선 적용
        body_html = ''
        for i, row_html in enumerate(rows_html):
            actual_rank = i + 1
            if actual_rank % 8 == 0 and i < num_teams - 1:
                row_html = row_html.replace('class="row', 'class="row divider-bottom', 1)
            body_html += row_html

        header_row = """
            <tr class="header-row">
                <th class="col-rank">#</th>
                <th class="col-team">팀명</th>
                <th class="col-mmr">MMR</th>
                <th class="col-members">멤버</th>
            </tr>
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
    .mmr-table {{
        width: 100%;
        border-collapse: collapse;
        table-layout: fixed;
    }}
    .mmr-table th, .mmr-table td {{
        padding: 8px 6px;
        text-align: center;
        font-size: 14px;
        line-height: 1.4;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
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
    .col-team {{ width: 17%; }}
    .col-mmr {{ width: 10%; }}
    .col-members {{ width: 68%; }}
    .separator {{
        color: #555555;
        margin: 0 4px;
    }}
    .staff-badge {{
        display: inline-block;
        background-color: #3a3a4a;
        color: #b8a0e8;
        font-size: 12px;
        padding: 1px 6px;
        border-radius: 3px;
        margin-left: 2px;
    }}
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
<table class="mmr-table">
    <thead>{header_row}</thead>
    <tbody>{body_html}</tbody>
</table>
</body>
</html>"""
        return html

    @staticmethod
    def _build_team_row_html(rank: int, team_name: str, team_data) -> str:
        """한 팀의 HTML 테이블 행을 생성"""
        mmr = team_data.mmr
        players = list(team_data.players)
        staff = list(team_data.staff)

        row_class = 'row-even' if (rank - 1) % 2 == 0 else 'row-odd'

        # HTML 이스케이프
        def _esc(s: str) -> str:
            return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        # 멤버 셀: 선수는 · 구분, 스태프는 배지로 시각 구분
        sep = '<span class="separator">·</span>'
        members_html = ''
        if players:
            members_html = sep.join(_esc(p) for p in players)
        if staff:
            staff_badges = ' '.join(
                f'<span class="staff-badge">{_esc(s)}</span>' for s in staff
            )
            if members_html:
                members_html += f' {staff_badges}'
            else:
                members_html = staff_badges
        if not members_html:
            members_html = '-'

        return f"""<tr class="row {row_class}">
    <td>{rank}</td>
    <td>{_esc(team_name)}</td>
    <td class="mmr-value">{mmr:.2f}</td>
    <td>{members_html}</td>
</tr>
"""
