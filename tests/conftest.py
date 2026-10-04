import pytest


@pytest.fixture(autouse=True)
def _no_youtube_gap(monkeypatch):
    """실제 사용 때는 자막 요청 사이에 20초를 두지만, 테스트에서는 기다리지 않는다."""
    from phonenaver import youtube

    monkeypatch.setattr(youtube, "MIN_GAP_SECONDS", 0)
