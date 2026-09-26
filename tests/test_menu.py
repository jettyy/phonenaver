from phonenaver import menu
from phonenaver.config import Config


def test_set_env_keeps_other_lines(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# 주석\nA=1\nB=\n", encoding="utf-8")
    monkeypatch.setattr(menu, "ENV", env)
    menu.set_env("B", "값")
    menu.set_env("C", "3")
    assert env.read_text(encoding="utf-8") == "# 주석\nA=1\nB=값\nC=3\n"


def test_setup_telegram_registers_chat_automatically(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=\nALLOWED_CHAT_IDS=\n", encoding="utf-8")
    monkeypatch.setattr(menu, "ENV", env)
    sent = []

    def fake(token, method, **params):
        if method == "getMe":
            return {"username": "my_blog_bot"}
        if method == "getUpdates":
            if params.get("timeout") == 0 and params.get("offset") is None:
                return [{"update_id": 5, "message": {"chat": {"id": 111}}}]  # 예전 메시지 (무시)
            if params.get("offset") == 6 and params.get("timeout") == 25:
                return [{"update_id": 6, "message": {"chat": {"id": 987654}, "text": "hi"}}]
            return []
        if method == "sendMessage":
            sent.append(params["chat_id"])
            return {}
        raise AssertionError(method)

    monkeypatch.setattr(menu, "telegram", fake)
    monkeypatch.setattr("builtins.input", lambda _="": "123:ABC")
    cfg = Config()
    menu.setup_telegram(cfg)
    text = env.read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN=123:ABC" in text
    assert "ALLOWED_CHAT_IDS=987654" in text
    assert sent == [987654] and cfg.allowed_chat_ids == {987654}
