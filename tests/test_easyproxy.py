import pytest

from autofree import easyproxy, settings


def test_get_master_proxy_url_respects_direct_and_follow_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "get_all",
        lambda: {
            "proxy": "http://127.0.0.1:2323",
            "proxy_master_mode": settings.PROXY_MASTER_MODE_DIRECT,
            "easyproxy": {"enabled": False},
        },
    )
    assert settings.get_master_proxy_url() == ""

    monkeypatch.setattr(
        settings,
        "get_all",
        lambda: {
            "proxy": "http://127.0.0.1:2323",
            "proxy_master_mode": settings.PROXY_MASTER_MODE_FOLLOW,
            "easyproxy": {"enabled": False},
        },
    )
    assert settings.get_master_proxy_url() == "http://127.0.0.1:2323"

    monkeypatch.setattr(
        settings,
        "get_all",
        lambda: {
            "proxy": "http://127.0.0.1:2323",
            "proxy_master_mode": settings.PROXY_MASTER_MODE_DIRECT,
            "easyproxy": {
                "enabled": True,
                "proxy_host": "127.0.0.1",
                "pool_port": 2323,
                "master_mode": settings.EASYPROXY_MASTER_MODE_POOL,
            },
        },
    )
    assert settings.get_master_proxy_url() == "http://127.0.0.1:2323"


def test_select_proxy_assignment_uses_only_selectable_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = easyproxy.normalize_easyproxy_settings(
        {
            "enabled": True,
            "management_url": "http://127.0.0.1:9888",
            "password": "secret",
            "proxy_host": "127.0.0.1",
            "pool_port": 2323,
            "port_min": 24000,
            "port_max": 24005,
            "cooldown_minutes": 60,
            "master_mode": "direct",
            "local_blacklist": {
                "24003": {
                    "reason": "timeout",
                    "tag": "node-3",
                    "name": "node-3",
                    "blacklisted_at": "2099-01-01T00:00:00+00:00",
                    "until": "2099-01-01T01:00:00+00:00",
                }
            },
        },
        existing={},
    )

    class FakeClient:
        def __init__(self, _cfg):
            self.cfg = _cfg

        def get_json(self, _path: str) -> dict:
            return {
                "nodes": [
                    {"port": 24000, "tag": "node-0", "name": "node-0", "available": False, "blacklisted": False},
                    {"port": 24001, "tag": "node-1", "name": "node-1", "available": True, "blacklisted": False},
                    {"port": 24002, "tag": "node-2", "name": "node-2", "available": True, "blacklisted": True},
                    {"port": 24003, "tag": "node-3", "name": "node-3", "available": True, "blacklisted": False},
                ]
            }

    monkeypatch.setattr(easyproxy, "get_easyproxy_config", lambda: cfg)
    monkeypatch.setattr(easyproxy, "_prune_local_blacklist", lambda conf: conf)
    monkeypatch.setattr(easyproxy, "EasyProxyClient", FakeClient)

    assignment = easyproxy.select_proxy_assignment()
    assert assignment == {
        "proxy_url": "http://127.0.0.1:24001",
        "port": 24001,
        "tag": "node-1",
        "name": "node-1",
    }
