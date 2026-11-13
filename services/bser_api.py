import asyncio
import random
import time
from types import TracebackType
from typing import Any, Dict, Optional, Tuple

import aiohttp

from config.logging_config import get_logger, log_once
from config.settings import settings

logger = get_logger('bser_api')

UID_FOUND = "found"
UID_NOT_FOUND = "not_found"
UID_ERROR = "error"


class BSERAPIClient:
    # 시즌 목록을 받지 못했을 때만 씀. 평소에는 holidays_api.rank_season_ids가 정함
    RANK_SEASON_ID = 41

    MAX_RETRIES = 4
    INITIAL_WAIT = 1
    MAX_WAIT = 30

    NICKNAME_CACHE_TTL = 86400
    MMR_CACHE_TTL = 60
    CACHE_MAX_ENTRIES = 2000

    # 일회용 인스턴스 간 공유 캐시, self에 재할당하면 공유가 끊김
    _nickname_cache: Dict[str, Dict[str, Any]] = {}
    _mmr_cache: Dict[str, Dict[str, Any]] = {}

    def __init__(self, *, quiet: bool = False):
        # quiet는 점검으로 판정한 주기에 선수별 실패 로그를 DEBUG로 내림
        self.quiet = quiet
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
        if self.session is not None and not self.session.closed:
            logger.warning("[API] 클라이언트가 적절히 종료되지 않음")
    
    def _get_cache_key(self, endpoint: str, params: Dict[str, Any] = None) -> str:
        if params:
            param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            return f"{endpoint}?{param_str}"
        return endpoint
    
    def _is_nickname_cache_valid(self, cache_entry: Dict[str, Any]) -> bool:
        return time.time() - cache_entry.get('timestamp', 0) < self.NICKNAME_CACHE_TTL
    
    def _get_from_nickname_cache(self, cache_key: str) -> Optional[Any]:
        if cache_key in self._nickname_cache:
            cache_entry = self._nickname_cache[cache_key]
            if self._is_nickname_cache_valid(cache_entry):
                return cache_entry['data']
            else:
                del self._nickname_cache[cache_key]
        return None
    
    @classmethod
    def _prune_expired(cls, cache: Dict[str, Dict[str, Any]], ttl: float) -> None:
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
        """None은 조회 실패. 4xx 본문은 그대로 돌려줘 호출부가 code로 판단."""
        if not self.session:
            await self.initialize_session()

        retries = 0
        rate_limited = 0
        wait_time = self.INITIAL_WAIT
        try:
            while retries <= self.MAX_RETRIES:
                try:
                    async with self.session.request(method, url, params=params, timeout=self.request_timeout) as response:
                        status = response.status

                        if status == 429:
                            if retries >= self.MAX_RETRIES:
                                return None
                            retry_after = response.headers.get('Retry-After')
                            try:
                                wait_time = float(retry_after) + 1 if retry_after else min(wait_time * 2, self.MAX_WAIT)
                            except (ValueError, TypeError):
                                wait_time = min(wait_time * 2, self.MAX_WAIT)
                            rate_limited += 1
                            retries += 1
                            await asyncio.sleep(wait_time + random.random())
                            continue

                        # 점검이나 장애 때 5xx 본문은 JSON이 아닌 HTML이라 본문보다 상태 코드를 먼저 확인
                        if 500 <= status < 600:
                            if retries < self.MAX_RETRIES:
                                retries += 1
                                await asyncio.sleep(wait_time + random.random())
                                wait_time = min(wait_time * 2, self.MAX_WAIT)
                                continue
                            if log_once(f"api5xx:{status}", 600):
                                logger.warning(f"[API] 서버 오류 {status}, 재시도 {self.MAX_RETRIES}회 후 실패")
                            return None

                        try:
                            return await response.json(content_type=None)
                        except ValueError:
                            if log_once(f"api-nonjson:{status}", 600):
                                logger.warning(f"[API] JSON이 아닌 응답 - 상태: {status}")
                            return None

                except aiohttp.ClientError as e:
                    if retries < self.MAX_RETRIES:
                        retries += 1
                        await asyncio.sleep(wait_time + random.random())
                        wait_time = min(wait_time * 2, self.MAX_WAIT)
                        continue
                    logger.error(f"[API] HTTP 요청 실패 - URL: {url}: {e}", exc_info=True)
                    return None
                except asyncio.TimeoutError:
                    if retries < self.MAX_RETRIES:
                        retries += 1
                        await asyncio.sleep(wait_time + random.random())
                        wait_time = min(wait_time * 2, self.MAX_WAIT)
                        continue
                    logger.warning(f"[API] HTTP 타임아웃 - URL: {url}")
                    return None

            return None
        finally:
            if rate_limited:
                logger.info(f"[API] 429 재시도 {rate_limited}회")

    def _set_mmr_cache(self, cache_key: str, mmr: float) -> None:
        self._prune_expired(self._mmr_cache, self.MMR_CACHE_TTL)
        self._mmr_cache[cache_key] = {'data': mmr, 'timestamp': time.time()}

    @classmethod
    def clear_mmr_cache(cls) -> None:
        cls._mmr_cache.clear()

    def _warn_player(self, key: str, message: str) -> None:
        if self.quiet:
            logger.debug(message)
        elif log_once(key):
            logger.warning(message)

    @classmethod
    def _forget_uid(cls, uid: str) -> None:
        for key in [k for k, v in cls._nickname_cache.items() if v.get('data') == uid]:
            del cls._nickname_cache[key]

    async def rank_seasons(self) -> Tuple[int, Optional[int]]:
        """(랭크 MMR에 쓸 정규 시즌 ID, 그 직전 정규 시즌 ID)"""
        # utils.helpers가 이 모듈을 불러서 위에서 불러오면 순환 import가 됨
        from services import holidays_api
        try:
            ids = await holidays_api.rank_season_ids()
        except Exception as e:
            logger.warning(f"[API] 랭크 시즌 판정 실패, 기본 시즌 사용: {type(e).__name__}")
            ids = None
        if ids is None:
            if log_once("rank-season-default", 3600):
                logger.warning(f"[API] 시즌 목록 없음, 기본 랭크 시즌 사용 - 시즌: {self.RANK_SEASON_ID}")
            return self.RANK_SEASON_ID, None
        return ids

    async def check_server_maintenance(self) -> bool:
        # 패치 점검 중에는 닉네임 검색과 rank/uid가 결과를 돌려주지 않음. 랭킹 1위 닉네임 검색으로 판정하고 판정할 수 없으면 점검으로 간주
        try:
            ranks = []
            seasons = await self.rank_seasons()
            # 랭킹은 닉네임 하나만 얻으면 되므로 랭크 시즌 랭킹이 비면 직전 시즌 랭킹을 씀
            for season_id in seasons:
                if season_id is None:
                    continue
                top = await self._request("GET", f"{self.base_url}/rank/top/{season_id}/3/10")
                # 재시도까지 실패했으면 직전 시즌도 같으므로 다시 묻지 않음
                if top is None:
                    break
                ranks = top.get("topRanks") or []
                if ranks:
                    break
            if not ranks:
                if log_once("maintenance-rank-empty", 600):
                    logger.info(f"[API] 랭킹 조회 실패, 점검으로 간주 - 시즌: {seasons}")
                return True
            probe = await self._request("GET", f"{self.base_url}/user/nickname",
                                        params={"query": ranks[0]["nickname"]})
            return not probe or probe.get("code") != 200
        except Exception as e:
            logger.warning(f"[API] 점검 여부 확인 실패, 점검으로 간주 - 오류: {e}")
            return True

    async def lookup_user_uid(self, user_nickname: str) -> Tuple[str, Optional[str]]:
        """반환: UID_FOUND, UID_NOT_FOUND, UID_ERROR 중 하나와 UID. 점검 중에는 캐시에 없는 닉네임이 모두 UID_NOT_FOUND."""
        # 닉네임 조회 API는 대소문자 구분
        cache_key = self._get_cache_key("user/nickname", {"query": user_nickname})
        cached_result = self._get_from_nickname_cache(cache_key)
        if cached_result is not None:
            return UID_FOUND, cached_result

        url = f"{self.base_url}/user/nickname"
        data = await self._request("GET", url, params={"query": user_nickname})
        if not data:
            return UID_ERROR, None

        code = data.get("code")
        if code == 200:
            user_data = data.get("user", {})
            uid = user_data.get("userId") or user_data.get("uid")
            if uid:
                self._set_nickname_cache(cache_key, uid)
                return UID_FOUND, uid
            logger.warning(f"[API] UID 필드를 찾을 수 없음 - 닉네임: '{user_nickname}'")
            return UID_ERROR, None
        if code == 404:
            self._warn_player(f"nickname404:{user_nickname}",
                              f"[API] 닉네임 조회 결과 없음 404 - 닉네임: '{user_nickname}'")
            return UID_NOT_FOUND, None
        self._warn_player(f"nickname-code:{user_nickname}:{code}",
                          f"[API] 닉네임 조회 API 응답 코드 오류 - 닉네임: '{user_nickname}', 코드: {code}, 메시지: {data.get('message')}")
        return UID_ERROR, None

    async def get_user_uid(self, user_nickname: str) -> Optional[str]:
        _, uid = await self.lookup_user_uid(user_nickname)
        return uid

    async def get_user_rank(self, uid: str) -> Optional[Dict]:
        season_id, previous_id = await self.rank_seasons()
        data = await self._request("GET", f"{self.base_url}/rank/uid/{uid}/{season_id}/3")
        if not data:
            return None

        code = data.get("code")
        if code == 200:
            user_rank = data.get("userRank")
            # 랭크 시즌 기록이 없으면 API가 MMR 0으로 답함. 그 시즌을 아직 하지 않은 선수는 직전 정규 시즌 MMR을 씀
            if not (user_rank or {}).get("mmr") and previous_id is not None:
                previous = await self._request("GET", f"{self.base_url}/rank/uid/{uid}/{previous_id}/3")
                if previous is None:
                    return None
                previous_rank = (previous or {}).get("userRank") if (previous or {}).get("code") == 200 else None
                if (previous_rank or {}).get("mmr"):
                    return {"userRank": previous_rank}
            if user_rank:
                return {"userRank": user_rank}
            logger.warning(f"[API] userRank 데이터 없음 - UID: {uid}")
            return {"userRank": {"mmr": 0}}

        if code == 403:
            # User Mismatch는 그 닉네임을 다른 계정이 쓰게 된 경우라 캐시된 UID를 버려야 다음 조회에서 새 UID를 받음
            self._forget_uid(uid)
            if log_once(f"rank403:{uid}"):
                logger.warning(f"[API] 사용자 MMR 조회 거부 403 - UID: {uid}, 메시지: {data.get('message')}, 닉네임 캐시 삭제")
        elif code == 404:
            self._warn_player(f"rank404:{uid}",
                              f"[API] 사용자 MMR 조회 실패 404 - UID: {uid}, 사용자나 랭크 데이터 없음")
        else:
            self._warn_player(f"rank-code:{uid}:{code}",
                              f"[API] 사용자 MMR 조회 API 응답 코드 오류 - UID: {uid}, 코드: {code}, 메시지: {data.get('message')}")
        return None

    async def get_user_mmr(self, uid: str) -> Optional[float]:
        """0.0은 랭크 데이터 없음, None은 조회 실패."""
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
                return None
            mmr = rank_data["userRank"].get("mmr", 0.0)
            self._set_mmr_cache(cache_key, mmr)
            return mmr

        except Exception as e:
            logger.error(f"[API] 사용자 MMR 조회 실패 - UID: {uid}: {e}", exc_info=True)
            return None
