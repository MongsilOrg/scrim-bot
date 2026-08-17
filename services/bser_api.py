"""
BSER API 클라이언트 서비스

캐시 전략 (클래스 속성으로 일회용 인스턴스 간 공유):
- 닉네임 → 유저ID 매칭: 장기 캐시 (변경되지 않는 데이터)
- 유저ID → MMR 조회: 단기 캐시 (주기 갱신 시 API 부하 감소)
  조편성 시에는 MMR 캐시를 클리어하여 실시간 데이터 사용
- 캐시가 상한을 초과하면 저장 시점에 만료 항목을 청소
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
    
    BSER API와의 통신을 담당하며, 닉네임-유저ID 매칭과 MMR을 캐싱합니다.
    캐시는 클래스 속성이라 매번 새로 만드는 일회용 인스턴스 간에도 공유됩니다.
    """

    # API 관련 상수
    MAX_RETRIES = 4  # 과도한 백오프 방지
    INITIAL_WAIT = 1  # 초기 대기 시간 (초)
    MAX_WAIT = 30  # 최대 대기 시간 (초)

    # 캐시 TTL 설정
    NICKNAME_CACHE_TTL = 86400  # 닉네임-유저ID 매칭: 24시간 (장기 캐시)
    MMR_CACHE_TTL = 60  # MMR 캐시: 60초 (단기 캐시)
    CACHE_MAX_ENTRIES = 2000  # 초과 시 만료 항목 청소

    # 공유 캐시 (클래스 속성, 인스턴스에서 재바인딩 금지: 항목 변경만 할 것)
    _nickname_cache: Dict[str, Dict[str, Any]] = {}
    _mmr_cache: Dict[str, Dict[str, Any]] = {}
    # 404 에러 로깅 추적 (같은 닉네임에 대한 반복 로그 방지, 닉네임 -> 마지막 로그 시간)
    _failed_nicknames: Dict[str, float] = {}

    def __init__(self):
        self.api_key = settings.BSER_API_KEY
        self.base_url = "https://open-api.bser.io/v1"
        self.session: Optional[aiohttp.ClientSession] = None
        self.request_timeout = aiohttp.ClientTimeout(total=10)
        self._headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json"
        }

    async def __aenter__(self):
        await self.initialize_session()
        return self

    async def __aexit__(self, exc_type: Optional[type[BaseException]],
                       exc_val: Optional[BaseException],
                       exc_tb: Optional[TracebackType]) -> None:
        await self.close_session()

    async def initialize_session(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(headers=self._headers, timeout=self.request_timeout)
        elif self.session.closed:
            self.session = aiohttp.ClientSession(headers=self._headers, timeout=self.request_timeout)

    async def close_session(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    def __del__(self) -> None:
        """소멸자에서 세션이 남아있다면 경고를 기록합니다."""
        if self.session is not None and not self.session.closed:
            logger.warning("[API] 클라이언트가 적절히 종료되지 않음")
    
    def _get_cache_key(self, endpoint: str, params: Dict[str, Any] = None) -> str:
        if params:
            param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            return f"{endpoint}?{param_str}"
        return endpoint
    
    def _is_nickname_cache_valid(self, cache_entry: Dict[str, Any]) -> bool:
        """닉네임 캐시 유효성 검사"""
        return time.time() - cache_entry.get('timestamp', 0) < self.NICKNAME_CACHE_TTL
    
    def _get_from_nickname_cache(self, cache_key: str) -> Optional[Any]:
        if cache_key in self._nickname_cache:
            cache_entry = self._nickname_cache[cache_key]
            if self._is_nickname_cache_valid(cache_entry):
                return cache_entry['data']
            else:
                # 만료된 캐시 제거
                del self._nickname_cache[cache_key]
        return None
    
    @classmethod
    def _prune_expired(cls, cache: Dict[str, Dict[str, Any]], ttl: float) -> None:
        """캐시가 상한을 넘으면 만료 항목을 제거합니다 (장수 클래스 캐시의 무한 성장 방지)."""
        if len(cache) <= cls.CACHE_MAX_ENTRIES:
            return
        now = time.time()
        for key in [k for k, v in cache.items() if now - v.get('timestamp', 0) >= ttl]:
            del cache[key]

    def _set_nickname_cache(self, cache_key: str, data: Any) -> None:
        self._prune_expired(self._nickname_cache, self.NICKNAME_CACHE_TTL)
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
    
    def _set_mmr_cache(self, cache_key: str, mmr: float) -> None:
        self._prune_expired(self._mmr_cache, self.MMR_CACHE_TTL)
        self._mmr_cache[cache_key] = {'data': mmr, 'timestamp': time.time()}

    @classmethod
    def clear_mmr_cache(cls) -> None:
        """공유 MMR 캐시만 클리어 (조편성 직전 실시간 데이터 보장용)"""
        cls._mmr_cache.clear()

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
        # 닉네임 캐시 확인 (장기 캐시)
        # 원본 닉네임 그대로 사용 (대소문자 구분)
        cache_key = self._get_cache_key("user/nickname", {"query": user_nickname})
        cached_result = self._get_from_nickname_cache(cache_key)
        if cached_result is not None:
            return cached_result

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
                if len(self._failed_nicknames) > self.CACHE_MAX_ENTRIES:
                    for k in [k for k, t in self._failed_nicknames.items() if current_time - t > 300]:
                        del self._failed_nicknames[k]
                self._failed_nicknames[user_nickname] = current_time
        else:
            logger.warning(f"[API] 닉네임 조회 API 응답 코드 오류 - 닉네임: '{user_nickname}', 코드: {data.get('code')}, 메시지: {data.get('message')}")
        
        return None
    
    
    async def get_user_rank(self, uid: str) -> Optional[Dict]:
        """사용자 UID로 랭크 정보를 조회합니다."""
        url = f"{self.base_url}/rank/uid/{uid}/41/3"
        data = await self._request("GET", url)
        if not data:
            return None

        if data.get("code") == 200:
            user_rank = data.get("userRank")
            if user_rank:
                return {"userRank": user_rank}
            logger.warning(f"[API] userRank 데이터 없음 - UID: {uid}")
            return {"userRank": {"mmr": 0}}

        if data.get('code') == 404:
            logger.warning(f"[API] 사용자 MMR 조회 실패 (404) - UID: {uid}, 존재하지 않는 사용자 또는 랭크 데이터 없음")
        else:
            logger.warning(f"[API] 사용자 MMR 조회 API 응답 코드 오류 - UID: {uid}, 코드: {data.get('code')}, 메시지: {data.get('message')}")
        return None
    
    async def get_user_mmr(self, uid: str) -> Optional[float]:
        """사용자 MMR 조회 (단기 캐시 적용)

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
            mmr = rank_data["userRank"].get("mmr", 0.0)
            self._set_mmr_cache(cache_key, mmr)
            return mmr

        except Exception as e:
            logger.error(f"[API] 사용자 MMR 조회 실패 - UID: {uid}: {e}", exc_info=True)
            return None
