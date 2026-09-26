"""phonenaver - 휴대폰으로 지시하면 네이버 블로그 글을 써서 임시저장하는 자동화 도구."""

__version__ = "0.1.0"

import warnings

# 맥 기본 파이썬(LibreSSL)에서 뜨는 urllib3 경고는 동작과 무관하므로 숨긴다
warnings.filterwarnings("ignore", message=".*OpenSSL.*")
