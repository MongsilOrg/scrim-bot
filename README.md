# ScrimBot

이터널 리턴 스크림 대회 관리 Discord 봇

## 주요 기능

- 팀 등록/수정/취소
- 17시 자동 조편성 (MMR 기반, 시드팀 분산)
- 방코드 공지 및 날씨 관리
- CSV 전적 처리 및 점수표 생성
- 패널티 시스템 (Google Sheets 연동)

## 설치 및 실행

```bash
git clone https://github.com/MongsilDev/scrimbot.git
cd scrimbot
pip install -r requirements.txt
cp .env.example .env  # 환경변수 설정
python main.py
```

### 필수 요구사항

- Python 3.10+
- wkhtmltoimage (`brew install --cask wkhtmltopdf` / `apt-get install wkhtmltopdf`)
- Discord Bot Token + BSER API Key
- Google Sheets 서비스 계정 (`credentials/google_sheets_credentials.json`)

## 환경변수

`.env.example` 참고. 주요 항목:

| 변수 | 설명 |
|------|------|
| `DISCORD_TOKEN` | Discord 봇 토큰 |
| `GUILD_ID` | Discord 서버 ID |
| `ADMIN_ROLE_IDS` | 관리자 역할 ID (쉼표 구분) |
| `BSER_API_KEY` | 이터널 리턴 API 키 |
| `NOTICE_CHANNEL_ID` | 공지 채널 |
| `GROUP_CHANNEL_IDS` | 조별 채널 (A:id,B:id,...) |
| `GOOGLE_SHEETS_MAIN_SPREADSHEET_ID` | 스프레드시트 ID |
| `NOTION_TOKEN` | Notion API 토큰 |
| `NOTION_DATABASE_ID` | Notion 데이터베이스 ID |

## Discord Intents

Developer Portal에서 활성화 필요:
- Message Content Intent
- Server Members Intent
