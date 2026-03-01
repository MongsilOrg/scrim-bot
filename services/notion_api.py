import os
import requests

from datetime import datetime, timezone, timedelta
from utils.number_er import is_number


# 1. 설정 정보
NOTION_TOKEN: str = os.getenv('NOTION_TOKEN', '')
NOTION_DATABASE_ID: str = os.getenv('NOTION_DATABASE_ID', '')

def get_date_data(data):
    props = data.get("properties", {})

    date_prop = props.get("날짜", {}).get("date")
            
    start_str = date_prop.get("start")
    end_str = date_prop.get("end") or start_str

    start_date = datetime.strptime(start_str[:10], "%Y-%m-%d").date()
    end_date = datetime.strptime(end_str[:10], "%Y-%m-%d").date()

    return start_date, end_date, props

def get_notion_data():
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    
    # 이거 안쓰면 안감
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    # 날짜에있는 데이터만 가져오기 없으면 안가져옴
    payload = {
        "filter": {
            "property": "날짜",
            "date": { "is_not_empty": True }
        }
    }
    
    res = requests.post(url, headers=headers, json=payload)
    results = res.json().get("results", [])
    return results

def check_notion_for_tags():
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    today = now.date()
    tomorrow = (now + timedelta(days=1)).date()

    results = get_notion_data()
    
    today_numbers = []
    tomorrow_numbers = []

    for result in results:
        if "properties" not in result:
            continue

        start_date, end_date, props = get_date_data(result)
        
        tag_list = props.get("태그", {}).get("multi_select", [])
        
        if start_date <= today <= end_date:
            for tag in tag_list:
                tag_name = tag["name"]
                if is_number(tag_name):
                    today_numbers.append(float(tag_name))

        if start_date <= tomorrow <= end_date:
            for tag in tag_list:
                tag_name = tag["name"]
                if is_number(tag_name):
                    tomorrow_numbers.append(float(tag_name))

    if today_numbers and tomorrow_numbers:
        if min(tomorrow_numbers) < min(today_numbers):
            return True

    return False

