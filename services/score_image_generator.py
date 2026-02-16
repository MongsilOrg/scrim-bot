"""
점수표 이미지 생성 서비스
"""
from io import BytesIO
from typing import Dict, List, Optional

from config.logging_config import get_logger
from services.image_generator import _render_html_to_image

logger = get_logger('score_image')


class ScoreImageGenerator:
    """점수표 이미지 생성기 (HTML/CSS 기반)"""

    def generate_score_table_image(self, team_data: List[Dict]) -> Optional[BytesIO]:
        """점수표 이미지 생성 (BytesIO 반환)"""
        if not team_data:
            return self._create_empty_image()

        html_str = self._build_score_html(team_data)
        return _render_html_to_image(html_str, width=900)

    def _build_score_html(self, team_data: List[Dict]) -> str:
        """점수표 HTML 생성"""
        rows_html = ''
        for team in team_data:
            rank = team.get('rank', 0)
            team_name = team.get('teamName', 'Unknown')
            kill_score = team.get('tournament kill score', 0)
            total_score = team.get('tournament total score', 0)

            rank_class = ''
            if rank == 1:
                rank_class = 'rank-gold'
            elif rank == 2:
                rank_class = 'rank-silver'
            elif rank == 3:
                rank_class = 'rank-bronze'

            row_class = 'row-even' if rank % 2 == 0 else 'row-odd'

            # HTML 이스케이프
            safe_name = team_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

            # 점수 포맷 (정수면 정수, 소수면 소수점 유지)
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
        padding: 15px;
    }}
    .card {{
        background-color: #2d2d2d;
        border-radius: 12px;
        overflow: hidden;
        border: 1px solid #404040;
    }}
    table {{
        width: 100%;
        border-collapse: collapse;
    }}
    th, td {{
        padding: 18px 16px;
        text-align: center;
        font-size: 32px;
        line-height: 1.3;
    }}
    thead th {{
        background-color: #3a3a3a;
        font-size: 36px;
        font-weight: bold;
        padding: 22px 16px;
        border-bottom: 2px solid #404040;
    }}
    .row-even td {{
        background-color: #2d2d2d;
    }}
    .row-odd td {{
        background-color: #353535;
    }}
    td {{
        border-bottom: 1px solid #404040;
    }}
    .col-rank {{
        width: 15%;
        font-weight: bold;
    }}
    .col-team {{
        width: 50%;
        text-align: left;
        padding-left: 24px;
    }}
    .col-ks {{
        width: 17.5%;
        color: #b0b0b0;
    }}
    .col-ts {{
        width: 17.5%;
    }}
    .accent {{
        color: #4a9eff;
        font-weight: bold;
    }}
    .rank-gold {{
        color: #ffd700;
    }}
    .rank-silver {{
        color: #c0c0c0;
    }}
    .rank-bronze {{
        color: #cd7f32;
    }}
    /* 세로 구분선 */
    td, th {{
        border-right: 1px solid #404040;
    }}
    td:last-child, th:last-child {{
        border-right: none;
    }}
</style>
</head>
<body>
<div class="card">
    <table>
        <thead>
            <tr>
                <th class="col-rank">Rank</th>
                <th class="col-team">Team</th>
                <th class="col-ks">KS</th>
                <th class="col-ts">TS</th>
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>
</div>
</body>
</html>"""
        return html

    def _create_empty_image(self) -> Optional[BytesIO]:
        """빈 데이터용 이미지 생성"""
        html = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
    body {
        font-family: 'NanumGothic', 'Nanum Gothic', 'Malgun Gothic', sans-serif;
        background-color: #1a1a1a;
        color: #b0b0b0;
        display: flex;
        justify-content: center;
        align-items: center;
        height: 280px;
        margin: 0;
    }
    .msg { font-size: 24px; }
</style>
</head>
<body>
<div class="msg">점수 데이터가 없습니다</div>
</body>
</html>"""
        return _render_html_to_image(html, width=900)
