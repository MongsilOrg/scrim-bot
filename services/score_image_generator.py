"""
점수표 이미지 생성 서비스
"""
import os
from typing import Dict, List

from PIL import Image, ImageDraw, ImageFont

from config.logging_config import get_logger

logger = get_logger('score_image')


class ScoreImageGenerator:
    """점수표 이미지 생성기"""
    
    def __init__(self):
        self.base_width = 900  # 너비 줄임
        self.base_height = 600
        self.table_margin = 15  # 여백 더 최소화
        self.row_height = 100  # 행 높이 대폭 증가 (큰 폰트에 맞춤)
        self.header_height = 90  # 헤더 높이 대폭 증가 (큰 폰트에 맞춤)
        
        # 색상 팔레트 (다크모드 기반)
        self.colors = {
            'background': '#1a1a1a',
            'card_bg': '#2d2d2d',
            'header_bg': '#3a3a3a',
            'row_bg_even': '#2d2d2d',
            'row_bg_odd': '#353535',
            'text_primary': '#ffffff',
            'text_secondary': '#b0b0b0',
            'accent': '#4a9eff',
            'border': '#404040',
            'rank_1': '#ffd700',  # 금메달
            'rank_2': '#c0c0c0',  # 은메달
            'rank_3': '#cd7f32'   # 동메달
        }
        
        # 폰트 경로 설정
        self.font_path = os.path.join(os.path.dirname(__file__), '..', 'assets', 'fonts', 'NanumGothic.ttf')
        
    def _get_font(self, size: int) -> ImageFont.FreeTypeFont:
        """폰트 로드"""
        try:
            return ImageFont.truetype(self.font_path, size)
        except OSError:
            logger.warning(f"[이미지생성] 폰트 로드 실패 - 기본 폰트 사용, 경로: {self.font_path}")
            return ImageFont.load_default()
    
    def _draw_rounded_rectangle(self, draw: ImageDraw.Draw, xy: tuple, radius: int, fill: str = None, outline: str = None):
        """둥근 모서리 사각형 그리기"""
        x1, y1, x2, y2 = xy
        
        # 모서리 호 그리기
        draw.ellipse([x1, y1, x1 + radius*2, y1 + radius*2], fill=fill, outline=outline)
        draw.ellipse([x2 - radius*2, y1, x2, y1 + radius*2], fill=fill, outline=outline)
        draw.ellipse([x1, y2 - radius*2, x1 + radius*2, y2], fill=fill, outline=outline)
        draw.ellipse([x2 - radius*2, y2 - radius*2, x2, y2], fill=fill, outline=outline)
        
        # 사각형 그리기
        draw.rectangle([x1 + radius, y1, x2 - radius, y2], fill=fill)
        draw.rectangle([x1, y1 + radius, x2, y2 - radius], fill=fill)
    
    def _get_rank_color(self, rank: int) -> str:
        """순위에 따른 색상 반환"""
        if rank == 1:
            return self.colors['rank_1']
        elif rank == 2:
            return self.colors['rank_2']
        elif rank == 3:
            return self.colors['rank_3']
        else:
            return self.colors['text_primary']
    
    def generate_score_table_image(self, team_data: List[Dict]) -> Image.Image:
        """점수표 이미지 생성"""
        if not team_data:
            return self._create_empty_image()
        
        # 이미지 크기 계산
        num_teams = len(team_data)
        table_height = self.header_height + (num_teams * self.row_height)
        self.base_height = max(200, table_height + (self.table_margin * 2))  # 최소 높이 더 감소
        
        # 이미지 생성
        img = Image.new('RGB', (self.base_width, self.base_height), self.colors['background'])
        draw = ImageDraw.Draw(img)
        
        # 메인 카드 배경 (여백 최소화)
        card_rect = (self.table_margin, self.table_margin, 
                    self.base_width - self.table_margin, self.base_height - self.table_margin)
        self._draw_rounded_rectangle(draw, card_rect, 8, fill=self.colors['card_bg'], outline=self.colors['border'])
        
        # 테이블 영역 (바로 시작)
        table_start_y = self.table_margin
        table_width = self.base_width - (self.table_margin * 2)
        
        # 컬럼 너비 설정 (동적으로 계산)
        available_width = self.base_width - (self.table_margin * 2)
        rank_width = int(available_width * 0.15)  # 15%
        team_width = int(available_width * 0.50)  # 50%
        ks_width = int(available_width * 0.175)   # 17.5%
        ts_width = int(available_width * 0.175)   # 17.5%
        
        # 헤더 그리기
        header_y = table_start_y
        header_rect = (self.table_margin, header_y, self.base_width - self.table_margin, header_y + self.header_height)
        self._draw_rounded_rectangle(draw, header_rect, 8, fill=self.colors['header_bg'])
        
        header_font = self._get_font(36)  # 헤더 폰트 크기 대폭 증가
        
        # 헤더 텍스트 (중앙정렬)
        headers = [
            ("Rank", self.table_margin + rank_width // 2, header_y + self.header_height // 2),
            ("Team", self.table_margin + rank_width + team_width // 2, header_y + self.header_height // 2),
            ("KS", self.table_margin + rank_width + team_width + ks_width // 2, header_y + self.header_height // 2),
            ("TS", self.table_margin + rank_width + team_width + ks_width + ts_width // 2, header_y + self.header_height // 2)
        ]
        
        for text, center_x, center_y in headers:
            bbox = draw.textbbox((0, 0), text, font=header_font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            x = center_x - text_width // 2
            y = center_y - text_height // 2
            draw.text((x, y), text, fill=self.colors['text_primary'], font=header_font)
        
        # 헤더 세로 구분선 그리기
        line_y_start = header_y
        line_y_end = header_y + self.header_height
        draw.line([(self.table_margin + rank_width, line_y_start), (self.table_margin + rank_width, line_y_end)], fill=self.colors['border'], width=2)
        draw.line([(self.table_margin + rank_width + team_width, line_y_start), (self.table_margin + rank_width + team_width, line_y_end)], fill=self.colors['border'], width=2)
        draw.line([(self.table_margin + rank_width + team_width + ks_width, line_y_start), (self.table_margin + rank_width + team_width + ks_width, line_y_end)], fill=self.colors['border'], width=2)
        
        # 데이터 행 그리기
        data_font = self._get_font(32)  # 데이터 폰트 크기 대폭 증가
        team_font = self._get_font(30)  # 팀명 폰트 크기 대폭 증가
        
        for i, team in enumerate(team_data):
            row_y = header_y + self.header_height + (i * self.row_height)
            
            # 행 배경색 (교대로)
            row_bg_color = self.colors['row_bg_even'] if i % 2 == 0 else self.colors['row_bg_odd']
            row_rect = (self.table_margin, row_y, self.base_width - self.table_margin, row_y + self.row_height)
            self._draw_rounded_rectangle(draw, row_rect, 5, fill=row_bg_color)
            
            # 순위 (중앙정렬)
            rank = team.get('rank', i + 1)
            rank_color = self._get_rank_color(rank)
            rank_text = f"{rank}"
            rank_bbox = draw.textbbox((0, 0), rank_text, font=data_font)
            rank_center_x = self.table_margin + rank_width // 2
            rank_center_y = row_y + self.row_height // 2
            rank_x = rank_center_x - (rank_bbox[2] - rank_bbox[0]) // 2
            rank_y = rank_center_y - (rank_bbox[3] - rank_bbox[1]) // 2
            draw.text((rank_x, rank_y), rank_text, fill=rank_color, font=data_font)
            
            # 팀명 (중앙정렬) - 길이에 따라 폰트 사이즈 조정
            team_name = team.get('teamName', 'Unknown')
            adjusted_team_font = self._get_adjusted_team_font(team_name, team_width)
            team_bbox = draw.textbbox((0, 0), team_name, font=adjusted_team_font)
            team_center_x = self.table_margin + rank_width + team_width // 2
            team_center_y = row_y + self.row_height // 2
            team_x = team_center_x - (team_bbox[2] - team_bbox[0]) // 2
            team_y = team_center_y - (team_bbox[3] - team_bbox[1]) // 2
            draw.text((team_x, team_y), team_name, fill=self.colors['text_primary'], font=adjusted_team_font)
            
            # 킬 점수 (KS) (중앙정렬)
            kill_score = team.get('tournament kill score', 0)
            ks_text = f"{kill_score}"
            ks_bbox = draw.textbbox((0, 0), ks_text, font=data_font)
            ks_center_x = self.table_margin + rank_width + team_width + ks_width // 2
            ks_center_y = row_y + self.row_height // 2
            ks_x = ks_center_x - (ks_bbox[2] - ks_bbox[0]) // 2
            ks_y = ks_center_y - (ks_bbox[3] - ks_bbox[1]) // 2
            draw.text((ks_x, ks_y), ks_text, fill=self.colors['text_secondary'], font=data_font)
            
            # 총 점수 (TS) (중앙정렬)
            total_score = team.get('tournament total score', 0)
            ts_text = f"{total_score}"
            ts_bbox = draw.textbbox((0, 0), ts_text, font=data_font)
            ts_center_x = self.table_margin + rank_width + team_width + ks_width + ts_width // 2
            ts_center_y = row_y + self.row_height // 2
            ts_x = ts_center_x - (ts_bbox[2] - ts_bbox[0]) // 2
            ts_y = ts_center_y - (ts_bbox[3] - ts_bbox[1]) // 2
            draw.text((ts_x, ts_y), ts_text, fill=self.colors['accent'], font=data_font)
            
            # 데이터 행 세로 구분선 그리기
            draw.line([(self.table_margin + rank_width, row_y), (self.table_margin + rank_width, row_y + self.row_height)], fill=self.colors['border'], width=1)
            draw.line([(self.table_margin + rank_width + team_width, row_y), (self.table_margin + rank_width + team_width, row_y + self.row_height)], fill=self.colors['border'], width=1)
            draw.line([(self.table_margin + rank_width + team_width + ks_width, row_y), (self.table_margin + rank_width + team_width + ks_width, row_y + self.row_height)], fill=self.colors['border'], width=1)
        
        return img
    
    def _create_empty_image(self) -> Image.Image:
        """빈 데이터용 이미지 생성"""
        img = Image.new('RGB', (self.base_width, 300), self.colors['background'])
        draw = ImageDraw.Draw(img)
        
        # 메시지
        font = self._get_font(24)
        text = "점수 데이터가 없습니다"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        x = (self.base_width - text_width) // 2
        y = (300 - (bbox[3] - bbox[1])) // 2
        
        draw.text((x, y), text, fill=self.colors['text_secondary'], font=font)
        
        return img
    
    def _get_adjusted_team_font(self, team_name: str, max_width: int) -> ImageFont.FreeTypeFont:
        """팀명 길이에 따라 적절한 폰트를 반환합니다."""
        # 임시로 텍스트 너비 측정
        temp_img = Image.new('RGB', (1, 1))
        temp_draw = ImageDraw.Draw(temp_img)
        
        # 일반 팀명 폰트로 먼저 측정
        team_font = self._get_font(30)
        bbox = temp_draw.textbbox((0, 0), team_name, font=team_font)
        text_width = bbox[2] - bbox[0]
        
        # 텍스트가 컬럼 너비를 초과하면 작은 폰트 사용
        if text_width > max_width - 20:  # 20픽셀 여백
            return self._get_font(24)  # 작은 폰트
        elif text_width > max_width - 40:  # 40픽셀 여백
            return self._get_font(26)  # 중간 폰트
        else:
            return team_font  # 기본 폰트
