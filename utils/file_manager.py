"""
파일 관리 유틸리티 모듈
"""
import atexit
import os
import shutil
import tempfile
from contextlib import contextmanager
from typing import List, Optional, Set

from config.logging_config import get_logger

logger = get_logger('file_manager')


class TempFileManager:
    """임시 파일 관리자"""
    
    def __init__(self):
        self._temp_files: Set[str] = set()
        self._temp_dirs: Set[str] = set()
        # 프로그램 종료 시 자동 정리
        atexit.register(self.cleanup_all)
    
    def register_temp_file(self, file_path: str) -> None:
        """임시 파일을 등록합니다."""
        self._temp_files.add(file_path)
    
    def register_temp_dir(self, dir_path: str) -> None:
        """임시 디렉토리를 등록합니다."""
        self._temp_dirs.add(dir_path)
    
    def cleanup_file(self, file_path: str) -> bool:
        """단일 임시 파일을 정리합니다."""
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                return True
            else:
                return False
        except Exception as e:
            logger.error(f"[파일관리] 임시 파일 삭제 실패 - 파일: {file_path}: {e}", exc_info=True)
            return False
        finally:
            # 등록 목록에서 제거
            self._temp_files.discard(file_path)
    
    def cleanup_dir(self, dir_path: str) -> bool:
        """단일 임시 디렉토리를 정리합니다."""
        try:
            if os.path.exists(dir_path):
                shutil.rmtree(dir_path)
                return True
            else:
                return False
        except Exception as e:
            logger.error(f"[파일관리] 임시 디렉토리 삭제 실패 - 디렉토리: {dir_path}: {e}", exc_info=True)
            return False
        finally:
            # 등록 목록에서 제거
            self._temp_dirs.discard(dir_path)
    
    def cleanup_all(self) -> None:
        """모든 임시 파일과 디렉토리를 정리합니다."""
        
        # 임시 파일들 정리
        for file_path in list(self._temp_files):
            self.cleanup_file(file_path)
        
        # 임시 디렉토리들 정리
        for dir_path in list(self._temp_dirs):
            self.cleanup_dir(dir_path)
        
    
    def get_registered_files(self) -> List[str]:
        """등록된 임시 파일 목록을 반환합니다."""
        return list(self._temp_files)
    
    def get_registered_dirs(self) -> List[str]:
        """등록된 임시 디렉토리 목록을 반환합니다."""
        return list(self._temp_dirs)


# 전역 임시 파일 관리자 인스턴스
temp_manager = TempFileManager()


@contextmanager
def temp_file(suffix: str = '', prefix: str = 'scrimbot_', delete: bool = True):
    """임시 파일 컨텍스트 매니저"""
    temp_file = None
    try:
        # 임시 파일 생성
        fd, temp_file = tempfile.mkstemp(suffix=suffix, prefix=prefix)
        os.close(fd)  # 파일 디스크립터 닫기
        
        # 자동 삭제가 활성화된 경우 등록
        if delete:
            temp_manager.register_temp_file(temp_file)
        
        yield temp_file
        
    finally:
        # 컨텍스트 종료 시 파일 정리
        if temp_file and delete:
            temp_manager.cleanup_file(temp_file)


@contextmanager
def temp_dir(prefix: str = 'scrimbot_', delete: bool = True):
    """임시 디렉토리 컨텍스트 매니저"""
    temp_dir = None
    try:
        # 임시 디렉토리 생성
        temp_dir = tempfile.mkdtemp(prefix=prefix)
        
        # 자동 삭제가 활성화된 경우 등록
        if delete:
            temp_manager.register_temp_dir(temp_dir)
        
        yield temp_dir
        
    finally:
        # 컨텍스트 종료 시 디렉토리 정리
        if temp_dir and delete:
            temp_manager.cleanup_dir(temp_dir)


def safe_remove_file(file_path: str) -> bool:
    """안전하게 파일을 삭제합니다."""
    return temp_manager.cleanup_file(file_path)


def safe_remove_dir(dir_path: str) -> bool:
    """안전하게 디렉토리를 삭제합니다."""
    return temp_manager.cleanup_dir(dir_path)


def cleanup_old_files(directory: str, pattern: str = '*', max_age_hours: int = 24) -> int:
    """오래된 파일들을 정리합니다."""
    import glob
    import time
    
    try:
        # 패턴에 맞는 파일들 찾기
        file_pattern = os.path.join(directory, pattern)
        files = glob.glob(file_pattern)
        
        current_time = time.time()
        max_age_seconds = max_age_hours * 3600
        removed_count = 0
        
        for file_path in files:
            try:
                # 파일 수정 시간 확인
                file_mtime = os.path.getmtime(file_path)
                age = current_time - file_mtime
                
                if age > max_age_seconds:
                    os.remove(file_path)
                    removed_count += 1
                    logger.debug(f"오래된 파일 삭제: {file_path}")
                    
            except Exception as e:
                logger.warning(f"[파일관리] 파일 삭제 실패 - 파일: {file_path}: {e}")
        
        if removed_count > 0:
        
        return removed_count
        
    except Exception as e:
        logger.error(f"[파일관리] 오래된 파일 정리 실패 - 디렉토리: {directory}: {e}", exc_info=True)
        return 0


def get_file_size_mb(file_path: str) -> float:
    """파일 크기를 MB 단위로 반환합니다."""
    try:
        if os.path.exists(file_path):
            size_bytes = os.path.getsize(file_path)
            return size_bytes / (1024 * 1024)
        return 0.0
    except Exception as e:
        logger.error(f"[파일관리] 파일 크기 조회 실패 - 파일: {file_path}: {e}", exc_info=True)
        return 0.0


def ensure_directory_exists(directory: str) -> bool:
    """디렉토리가 존재하는지 확인하고, 없으면 생성합니다."""
    try:
        if not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
        return True
    except Exception as e:
        logger.error(f"[파일관리] 디렉토리 생성 실패 - 디렉토리: {directory}: {e}", exc_info=True)
        return False
