"""
이미지 생성 서비스
"""
import os
from io import BytesIO
from typing import Optional

import imgkit
import pandas as pd
from PIL import Image, ImageDraw, ImageFont

from config.logging_config import get_logger
from config.settings import settings

logger = get_logger('image_generator')


class ImageGenerator:
    """이미지 생성 클래스"""
    
    @staticmethod
    def generate_result_image(df: pd.DataFrame) -> Optional[BytesIO]:
        """결과 이미지를 생성하는 함수"""
        try:
            # HTML 템플릿 생성
            html_str = ImageGenerator._create_html_template(df)
            
            # 이미지 생성 옵션
            options = {
                'width': 800,
                'height': 600,
                'quality': 100,
                'format': 'png'
            }
            
            # wkhtmltoimage 설정 (경로 미존재 시 안전하게 실패 처리)
            wkhtml_path = '/usr/bin/wkhtmltoimage'
            if not os.path.exists(wkhtml_path):
                logger.error(f"[이미지생성] wkhtmltoimage를 찾을 수 없음 - 경로: {wkhtml_path}")
                return None

            config = imgkit.config(wkhtmltoimage=wkhtml_path)
            
            # 이미지 생성
            img_bytes = imgkit.from_string(html_str, False, config=config, options=options)
            
            # BytesIO로 변환
            img_io = BytesIO(img_bytes)
            img_io.seek(0)
            
            return img_io
            
        except Exception as e:
            logger.error(f"[이미지생성] 이미지 생성 실패: {e}", exc_info=True)
            return None
    
    @staticmethod
    def _create_html_template(df: pd.DataFrame) -> str:
        """HTML 템플릿 생성"""
        # 데이터프레임을 HTML 테이블로 변환
        table_html = df.to_html(index=False, escape=False, classes='table')
        
        html_template = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>Result</title>
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
                .table tr:hover {{
                    background-color: #f5f5f5;
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
        """MMR 이미지를 생성하는 함수"""
        try:
            # 팀 정보를 MMR 순으로 정렬 (TeamData 구조)
            sorted_teams = sorted(
                teams_data.items(),
                key=lambda x: x[1].mmr if hasattr(x[1], 'mmr') else 0,
                reverse=True
            )

            # 현재 시간 (KST)
            from utils.helpers import get_current_kst_time
            current_time = get_current_kst_time().strftime('%H:%M')
            
            # 이미지 크기 설정 (가로 길이 줄이고 세로는 팀 수에 따라 자동 조정)
            img_width = 800  # 가로 길이 줄임
            row_height = 40
            header_height = 50
            
            # 팀 수에 따라 세로 길이 자동 계산
            num_teams = len(sorted_teams)
            if num_teams == 0:
                # 팀이 없는 경우 헤더만 표시
                img_height = header_height
            else:
                img_height = header_height + (num_teams * row_height)
                
                # 8팀의 배수 + @ 팀이 있을 경우 구분선 높이 추가
                if num_teams > 8:
                    divider_count = 0
                    for i in range(8, num_teams, 8):
                        if i < num_teams:
                            divider_count += 1
                    
                    # 구분선 높이 추가 (구분선 두께만)
                    divider_height = 3  # 구분선 두께
                    img_height += divider_count * divider_height
            
            # 최소 높이 보장 (헤더 높이 이상)
            if img_height < header_height:
                img_height = header_height
            
            # 이미지 생성
            img = Image.new('RGB', (img_width, img_height), '#1a1a1a')
            draw = ImageDraw.Draw(img)
            
            # 폰트 설정
            font_path = os.path.join(os.path.dirname(__file__), '..', 'assets', 'fonts', 'NanumGothic.ttf')
            try:
                header_font = ImageFont.truetype(font_path, 16)
                text_font = ImageFont.truetype(font_path, 12)
                small_font = ImageFont.truetype(font_path, 10)
            except OSError:
                header_font = ImageFont.load_default()
                text_font = ImageFont.load_default()
                small_font = ImageFont.load_default()
            
            # 색상 설정
            colors = {
                'background': '#1a1a1a',
                'header_bg': '#4a9eff',
                'row_bg1': '#2d2d2d',
                'row_bg2': '#333333',
                'text_primary': '#ffffff',
                'text_secondary': '#b0b0b0',
                'mmr_color': '#4a9eff',
                'border': '#404040'
            }
            
            # 헤더 그리기
            header_y = 0
            draw.rectangle([0, header_y, img_width, header_y + header_height], fill=colors['header_bg'])
            
            # 헤더 텍스트 및 컬럼 너비 (가로 길이에 맞게 조정)
            headers = ['Rank', 'Team', 'MMR', 'Player1', 'Player2', 'Player3', 'Player4', 'Staff']
            col_widths = [50, 120, 70, 100, 100, 100, 100, 160]  # 가로 길이에 맞게 조정
            col_x = 0
            
            for i, (header, width) in enumerate(zip(headers, col_widths)):
                # 텍스트 중앙 정렬
                bbox = draw.textbbox((0, 0), header, font=header_font)
                text_width = bbox[2] - bbox[0]
                text_x = col_x + (width - text_width) // 2
                text_y = header_y + (header_height - (bbox[3] - bbox[1])) // 2
                
                draw.text((text_x, text_y), header, fill=colors['text_primary'], font=header_font)
                col_x += width
            
            # 데이터 행 그리기 (팀이 있는 경우에만)
            if num_teams > 0:
                current_y = header_height
                # 팀 데이터 표시
                for idx, (team_name, team_data) in enumerate(sorted_teams):
                    # 이미지 높이가 자동으로 조정되므로 높이 제한 제거
                    
                    # MMR 값 및 팀 정보 추출 (TeamData 구조)
                    mmr = team_data.mmr
                    players = team_data.players
                    staff = team_data.staff
                    
                    # 선수들을 닉네임 길이 순으로 정렬
                    def get_nickname_length(nickname):
                        if nickname == "-":
                            return 0
                        length = 0
                        for char in nickname:
                            if ord(char) < 128:
                                length += 1
                            else:
                                length += 2
                        return length
                    
                    def get_sort_key(nickname):
                        if nickname == "-":
                            return (0, 0)
                        length = get_nickname_length(nickname)
                        first_char_priority = 0 if ord(nickname[0]) >= 128 else 1
                        return (length, first_char_priority)
                    
                    sorted_players = sorted(players, key=get_sort_key, reverse=True)
                    while len(sorted_players) < 4:
                        sorted_players.append("-")
                    
                    sorted_staff = sorted(staff, key=get_sort_key, reverse=True)
                    
                    # 행 배경색
                    row_bg = colors['row_bg1'] if idx % 2 == 0 else colors['row_bg2']
                    draw.rectangle([0, current_y, img_width, current_y + row_height], fill=row_bg)
                    
                    # 데이터 텍스트
                    col_x = 0
                    row_data = [
                        str(idx + 1),
                        team_name,
                        f"{mmr:.2f}",
                        sorted_players[0],
                        sorted_players[1],
                        sorted_players[2],
                        sorted_players[3],
                        ', '.join(sorted_staff) if sorted_staff else '-'
                    ]
                    
                    for i, (data, width) in enumerate(zip(row_data, col_widths)):
                        # 팀명 컬럼(인덱스 1)의 경우 폰트 사이즈 조정
                        if i == 1:  # 팀명 컬럼
                            # 팀명 길이에 따라 폰트 사이즈 조정
                            font_to_use = ImageGenerator._get_adjusted_font(data, width, text_font, small_font)
                        else:
                            font_to_use = text_font
                        
                        # 텍스트 중앙 정렬
                        bbox = draw.textbbox((0, 0), data, font=font_to_use)
                        text_width = bbox[2] - bbox[0]
                        text_x = col_x + (width - text_width) // 2
                        text_y = current_y + (row_height - (bbox[3] - bbox[1])) // 2
                        
                        # MMR은 특별한 색상으로
                        text_color = colors['mmr_color'] if i == 2 else colors['text_primary']
                        
                        draw.text((text_x, text_y), data, fill=text_color, font=font_to_use)
                        col_x += width
                    
                    current_y += row_height
                    
                    # 8팀의 배수 + @ 팀이 있을 경우 구분선 추가
                    # 9팀이면 8~9 사이에, 17팀이면 8~9와 16~17 사이에 구분선
                    if num_teams > 8:
                        # 8팀의 배수 다음에 구분선이 필요한지 확인
                        if (idx + 1) % 8 == 0 and (idx + 1) < num_teams:
                            # 구분선 그리기
                            divider_color = '#ff6b6b'  # 빨간색 구분선
                            divider_thickness = 3
                            
                            # 구분선 그리기 (전체 너비)
                            draw.rectangle([
                                0, current_y - divider_thickness // 2,
                                img_width, current_y + divider_thickness // 2
                            ], fill=divider_color)
                            
                            # 구분선 높이만큼 current_y 증가
                            current_y += divider_thickness
            
            # BytesIO로 변환
            img_io = BytesIO()
            img.save(img_io, format='PNG', quality=95)
            img_io.seek(0)
            
            return img_io
            
        except Exception as e:
            logger.error(f"[이미지생성] MMR 이미지 생성 실패: {e}", exc_info=True)
            return None
    
    @staticmethod
    def _get_adjusted_font(text: str, max_width: int, normal_font: ImageFont.FreeTypeFont, small_font: ImageFont.FreeTypeFont) -> ImageFont.FreeTypeFont:
        """텍스트 길이에 따라 적절한 폰트를 반환합니다."""
        # 임시로 텍스트 너비 측정
        temp_img = Image.new('RGB', (1, 1))
        temp_draw = ImageDraw.Draw(temp_img)
        
        # 일반 폰트로 먼저 측정
        bbox = temp_draw.textbbox((0, 0), text, font=normal_font)
        text_width = bbox[2] - bbox[0]
        
        # 텍스트가 컬럼 너비를 초과하면 작은 폰트 사용
        if text_width > max_width - 10:  # 10픽셀 여백
            return small_font
        else:
            return normal_font