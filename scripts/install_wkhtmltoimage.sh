#!/bin/bash
# wkhtmltoimage 설치 스크립트 (Ubuntu Docker 컨테이너용)
set -e

echo "=== wkhtmltoimage 설치 시작 ==="

apt-get update -qq
apt-get install -y --no-install-recommends wkhtmltopdf

# 설치 확인
if command -v wkhtmltoimage &> /dev/null; then
    echo "=== 설치 완료 ==="
    wkhtmltoimage --version
    echo "경로: $(which wkhtmltoimage)"
else
    echo "=== 설치 실패: wkhtmltoimage를 찾을 수 없습니다 ==="
    exit 1
fi
