"""
BSER API 클라이언트 서비스

캐시 전략:
- 닉네임 → 유저ID 매칭: 24시간 장기 캐시 (변경되지 않는 데이터)
- 유저ID → MMR 조회: 60초 단기 캐시 (5분 주기 갱신 시 API 부하 감소)
  조편성 시에는 MMR 캐시를 클리어하여 실시간 데이터 사용
"""
import asyncio
import random
import time
from types import TracebackType
from typing import Any, Dict, Optional

import aiohttp

from config.logging_config import get_logger
from config.settings import settings

logger = get_logger('bser_api')


class BSERAPIClient:
    """
    BSER API 클라이언트
    
    BSER API와의 통신을 담당하며, 닉네임-유저ID 매칭을 캐싱합니다.
    MMR 조회는 실시간 데이터를 사용합니다.
    
    Attributes:
        api_key: BSER API 키
        base_url: API 기본 URL
        session: aiohttp 클라이언트 세션
        _headers: API 요청 헤더
        _nickname_cache: 닉네임-유저ID 매칭 캐시
    
    Constants:
        MAX_RETRIES: 최대 재시도 횟수 (기본값: 3)
        INITIAL_WAIT: 초기 대기 시간 (초, 기본값: 1)
        MAX_WAIT: 최대 대기 시간 (초, 기본값: 8)
        NICKNAME_CACHE_TTL: 닉네임 캐시 TTL (초, 기본값: 86400 = 24시간)
    """
    
    # API 관련 상수
    MAX_RETRIES = 4  # 과도한 백오프 방지
    INITIAL_WAIT = 1  # 초기 대기 시간 (초)
    MAX_WAIT = 30  # 최대 대기 시간 (초)
    
    # 캐시 TTL 설정
    NICKNAME_CACHE_TTL = 86400  # 닉네임-유저ID 매칭: 24시간 (장기 캐시)
    MMR_CACHE_TTL = 60  # MMR 캐시: 60초 (단기 캐시)
    
    def __init__(self):
        self.api_key = settings.BSER_API_KEY
        self.base_url = "https://open-api.bser.io/v1"
        self.session: Optional[aiohttp.ClientSession] = None
        self.request_timeout = aiohttp.ClientTimeout(total=10)
        self._headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json"
        }
        # 닉네임-유저ID 매칭 캐시 (장기 캐시)
        self._nickname_cache: Dict[str, Dict[str, Any]] = {}
        # MMR 캐시 (단기 캐시)
        self._mmr_cache: Dict[str, Dict[str, Any]] = {}
        # 404 에러 로깅 추적 (같은 닉네임에 대한 반복 로그 방지)
        self._failed_nicknames: Dict[str, float] = {}  # 닉네임 -> 마지막 로그 시간
    
    async def __aenter__(self):
        """비동기 컨텍스트 매니저 진입"""
        await self.initialize_session()
        return self
    
    async def __aexit__(self, exc_type: Optional[type[BaseException]], 
                       exc_val: Optional[BaseException], 
                       exc_tb: Optional[TracebackType]) -> None:
        """비동기 컨텍스트 매니저 종료"""
        await self.close_session()
    
    async def initialize_session(self) -> None:
        """세션을 초기화합니다."""
        if self.session is None:
            self.session = aiohttp.ClientSession(headers=self._headers, timeout=self.request_timeout)
        elif self.session.closed:
            self.session = aiohttp.ClientSession(headers=self._headers, timeout=self.request_timeout)

    async def close_session(self) -> None:
        """세션을 종료합니다."""
        if self.session is not None:
            await self.session.close()
            self.session = None

    def __del__(self) -> None:
        """소멸자에서 세션이 남아있다면 경고를 기록합니다."""
        if self.session is not None and not self.session.closed:
            logger.warning("[API] 클라이언트가 적절히 종료되지 않음")
    
    def _get_cache_key(self, endpoint: str, params: Dict[str, Any] = None) -> str:
        """캐시 키 생성"""
        if params:
            param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            return f"{endpoint}?{param_str}"
        return endpoint
    
    def _is_nickname_cache_valid(self, cache_entry: Dict[str, Any]) -> bool:
        """닉네임 캐시 유효성 검사 (24시간)"""
        return time.time() - cache_entry.get('timestamp', 0) < self.NICKNAME_CACHE_TTL
    
    def _get_from_nickname_cache(self, cache_key: str) -> Optional[Any]:
        """닉네임 캐시에서 데이터 조회"""
        if cache_key in self._nickname_cache:
            cache_entry = self._nickname_cache[cache_key]
            if self._is_nickname_cache_valid(cache_entry):
                return cache_entry['data']
            else:
                # 만료된 캐시 제거
                del self._nickname_cache[cache_key]
        return None
    
    def _set_nickname_cache(self, cache_key: str, data: Any) -> None:
        """닉네임 캐시에 데이터 저장"""
        self._nickname_cache[cache_key] = {
            'data': data,
            'timestamp': time.time()
        }

    async def _request(self, method: str, url: str, *, params: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
        """공통 요청 래퍼 (재시도/백오프/타임아웃/429 대응)"""
        if not self.session:
            await self.initialize_session()

        retries = 0
        wait_time = self.INITIAL_WAIT

        while retries <= self.MAX_RETRIES:
            try:
                async with self.session.request(method, url, params=params, timeout=self.request_timeout) as response:
                    status = response.status
                    data = await response.json(content_type=None)

                    # 429 처리
                    if status == 429:
                        retry_after = response.headers.get('Retry-After')
                        if retries < self.MAX_RETRIES:
                            if retry_after:
                                try:
                                    wait_time = float(retry_after) + 1
                                except (ValueError, TypeError):
                                    wait_time = min(wait_time * 2, self.MAX_WAIT)
                            else:
                                wait_time = min(wait_time * 2, self.MAX_WAIT)
                            await asyncio.sleep(wait_time + random.random())
                            retries += 1
                            continue
                        logger.warning(f"[API] 429 재시도 횟수 초과 ({self.MAX_RETRIES}회)")
                        return None

                    # 5xx 재시도
                    if 500 <= status < 600 and retries < self.MAX_RETRIES:
                        await asyncio.sleep(wait_time + random.random())
                        wait_time = min(wait_time * 2, self.MAX_WAIT)
                        retries += 1
                        continue

                    return data

            except aiohttp.ClientError as e:
                if retries < self.MAX_RETRIES:
                    await asyncio.sleep(wait_time + random.random())
                    wait_time = min(wait_time * 2, self.MAX_WAIT)
                    retries += 1
                    continue
                logger.error(f"[API] HTTP 요청 실패 - URL: {url}: {e}", exc_info=True)
                return None
            except asyncio.TimeoutError:
                if retries < self.MAX_RETRIES:
                    await asyncio.sleep(wait_time + random.random())
                    wait_time = min(wait_time * 2, self.MAX_WAIT)
                    retries += 1
                    continue
                logger.warning(f"[API] HTTP 타임아웃 - URL: {url}")
                return None

        return None
    
    def clear_cache(self) -> None:
        """전체 캐시 클리어"""
        self._nickname_cache.clear()
        self._mmr_cache.clear()

    def clear_nickname_cache(self) -> None:
        """닉네임 캐시만 클리어"""
        self._nickname_cache.clear()

    def clear_mmr_cache(self) -> None:
        """MMR 캐시만 클리어"""
        self._mmr_cache.clear()

    def get_cache_stats(self) -> Dict[str, Any]:
        """캐시 통계 반환"""
        return {
            "nickname_cache_size": len(self._nickname_cache),
            "nickname_cache_ttl": self.NICKNAME_CACHE_TTL,
            "mmr_cache_size": len(self._mmr_cache),
            "mmr_cache_ttl": self.MMR_CACHE_TTL
        }
    
    async def check_server_maintenance(self) -> bool:
        """BSER 서버 점검 여부를 확인합니다.

        Returns:
            True: 점검 중 (API 응답이 200이 아닌 경우)
            False: 정상 운영 중
        """
        url = "https://open-api.bser.io/v2/data/Season"
        try:
            data = await self._request("GET", url)
            if data is None:
                return True
            return data.get("code") != 200
        except Exception:
            return True

    async def get_user_uid(self, user_nickname: str) -> Optional[str]:
        """사용자 닉네임으로 사용자 UID를 조회합니다."""
        # 닉네임 캐시 확인 (24시간 장기 캐시)
        # 원본 닉네임 그대로 사용 (대소문자 구분)
        cache_key = self._get_cache_key("user/nickname", {"query": user_nickname})
        cached_result = self._get_from_nickname_cache(cache_key)
        if cached_result is not None:
            return cached_result
        
        if not self.session:
            await self.initialize_session()

        url = f"{self.base_url}/user/nickname"
        data = await self._request("GET", url, params={"query": user_nickname})
        if not data:
            return None

        if data.get("code") == 200:
            user_data = data.get("user", {})
            uid = user_data.get("userId") or user_data.get("uid")
            if uid:
                self._set_nickname_cache(cache_key, uid)
                return uid
            logger.warning(f"[API] UID 필드를 찾을 수 없음 - 닉네임: '{user_nickname}'")
        elif data.get("code") == 404:
            current_time = time.time()
            last_log_time = self._failed_nicknames.get(user_nickname, 0)
            if current_time - last_log_time > 300:
                logger.warning(f"[API] 닉네임 조회 실패 (404) - 닉네임: '{user_nickname}'")
                self._failed_nicknames[user_nickname] = current_time
        else:
            logger.warning(f"[API] 닉네임 조회 API 응답 코드 오류 - 닉네임: '{user_nickname}', 코드: {data.get('code')}, 메시지: {data.get('message')}")
        
        return None
    
    
    async def get_user_rank(self, uid: str) -> Optional[Dict]:
        """사용자 UID로 랭크 정보를 조회합니다."""
        url = f"{self.base_url}/rank/uid/{uid}/37/3"
        data = await self._request("GET", url)
        if not data:
            return None

        if data.get("code") == 200:
            user_rank = data.get("userRank")
            if user_rank:
                mmr_value = user_rank.get("mmr", 0)
                # MMR 0은 정상적인 경우일 수 있으므로 로그 제거
                return {"userRank": user_rank}
            logger.warning(f"[API] userRank 데이터 없음 - UID: {uid}")
            return {"userRank": {"mmr": 0}}

        if data.get('code') == 404:
            logger.warning(f"[API] 사용자 MMR 조회 실패 (404) - UID: {uid}, 존재하지 않는 사용자 또는 랭크 데이터 없음")
        else:
            logger.warning(f"[API] 사용자 MMR 조회 API 응답 코드 오류 - UID: {uid}, 코드: {data.get('code')}, 메시지: {data.get('message')}")
        return None
    
    async def get_user_stats(self, uid: str) -> Optional[Dict[str, Any]]:
        """사용자 통계 조회"""
        if not self.session:
            return None
        
        try:
            url = f"{self.base_url}/user/stats/{uid}/rank"

            async with self.session.get(url, headers=self._headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("code") == 200:
                        return data.get("userStats", [])
                    else:
                        logger.warning(f"[API] 사용자 통계를 찾을 수 없음 - UID: {uid}")
                        return None
                else:
                    logger.warning(f"[API] API 요청 실패 - 상태: {response.status}")
                    return None
                    
        except Exception as e:
            logger.error(f"[API] 사용자 통계 조회 실패: {e}", exc_info=True)
            return None
    
    async def get_user_mmr(self, uid: str) -> Optional[float]:
        """사용자 MMR 조회 (60초 캐시 적용)

        Returns:
            float: MMR 값 (0.0 포함, 랭크 데이터가 없는 정상 케이스)
            None: API 오류, 네트워크 오류 등 조회 실패
        """
        # MMR 캐시 확인
        cache_key = f"mmr:{uid}"
        if cache_key in self._mmr_cache:
            entry = self._mmr_cache[cache_key]
            if time.time() - entry.get('timestamp', 0) < self.MMR_CACHE_TTL:
                return entry['data']
            else:
                del self._mmr_cache[cache_key]

        try:
            rank_data = await self.get_user_rank(uid)
            if rank_data is None:
                # API 오류 또는 네트워크 오류 → None 반환
                return None
            if isinstance(rank_data, dict) and rank_data.get("userRank"):
                user_rank = rank_data["userRank"]
                if isinstance(user_rank, dict):
                    mmr = user_rank.get("mmr", 0.0)
                else:
                    mmr = getattr(user_rank, "mmr", 0.0) if hasattr(user_rank, "mmr") else 0.0
                # 캐시에 저장
                self._mmr_cache[cache_key] = {'data': mmr, 'timestamp': time.time()}
                return mmr
            logger.warning(f"[API] rank_data에 userRank가 없음 - UID: {uid}")
            mmr = 0.0
            self._mmr_cache[cache_key] = {'data': mmr, 'timestamp': time.time()}
            return mmr

        except Exception as e:
            logger.error(f"[API] 사용자 MMR 조회 실패 - UID: {uid}: {e}", exc_info=True)
            return None
