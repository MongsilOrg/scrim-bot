import re

# 숫자 패턴의 정규 표현식
pattern = r"^[0-9]+(\.[0-9]+)?$"

def is_number(tag):
  return re.match(pattern, tag) is not None
