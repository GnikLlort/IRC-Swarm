"""Tests for configuration loading, defaults and validation."""

import json

import pytest

import config as config_mod
from models import ConfigError, ProxyMode


def _cfg(**kw):
    base = {
        "bot_count": 3,
        "irc": {"server": "irc.example.net", "port": 6697, "channels": ["#test"]},
    }
    base.update(kw)
    return config_mod.load_config(base)


def test_basic_load():
    c = _cfg()
    assert c.bot_count == 3
    assert c.irc.server == "irc.example.net"
    assert c.irc.port == 6697
    assert c.irc.channels == ["#test"]
    assert c.proxy_mode == ProxyMode.ROTATE  # default


def test_bot_ids_format():
    c = _cfg(bot_count=20)
    assert c.bot_ids()[:3] == ["bot001", "bot002", "bot003"]
    assert c.bot_ids()[-1] == "bot020"


def test_nickname_generation():
    c = _cfg(bot_count=20, nickname_prefix="TestBot")
    assert c.nick_for("bot001") == "TestBot001"
    assert c.nick_for("bot020") == "TestBot020"


def test_default_nickname_prefix_is_bot():
    c = _cfg(bot_count=3)
    assert c.nick_for("bot002") == "Bot002"


def test_wider_id_width():
    c = _cfg(bot_count=150)
    assert c.bot_ids()[0] == "bot001"
    assert c.bot_ids()[-1] == "bot150"
    assert c.nick_for("bot010") == "Bot010"


def test_channels_auto_prefixed():
    c = _cfg(bot_count=1)
    c2 = config_mod.load_config({
        "bot_count": 1,
        "irc": {"server": "x", "channels": ["test", "#already"]},
    })
    assert c2.irc.channels == ["#test", "#already"]


def test_channel_ordering_and_dedup():
    c = _cfg()
    c2 = config_mod.load_config({
        "bot_count": 1,
        "irc": {"server": "x", "channels": ["#a", "#a", "#b"]},
    })
    assert c2.irc.channels == ["#a", "#b"]


def test_proxy_modes_parsed():
    for m in ("unique", "rotate", "direct"):
        c = _cfg(proxy_mode=m)
        assert c.proxy_mode == ProxyMode(m)


def test_groups_load():
    c = _cfg(groups={"g1": ["bot001", "bot002"]})
    assert c.display_groups()["g1"] == ["bot001", "bot002"]


def test_group_unknown_bot_rejected():
    with pytest.raises(ConfigError):
        _cfg(groups={"bad": ["bot999"]})


def test_invalid_bot_count():
    with pytest.raises(ConfigError):
        _cfg(bot_count=0)
    with pytest.raises(ConfigError):
        _cfg(bot_count=-1)


def test_missing_server_rejected():
    with pytest.raises(ConfigError):
        config_mod.load_config({"bot_count": 1, "irc": {}})


def test_invalid_port_rejected():
    with pytest.raises(ConfigError):
        config_mod.load_config({"bot_count": 1, "irc": {"server": "x", "port": 70000}})


def test_invalid_proxy_mode_rejected():
    with pytest.raises(ConfigError):
        _cfg(proxy_mode="banana")


def test_bad_rate_limit_rejected():
    with pytest.raises(ConfigError):
        _cfg(rate_limit={"messages_per_second": -1, "burst": 3})


def test_bad_reconnect_rejected():
    with pytest.raises(ConfigError):
        _cfg(reconnect={"initial_delay": 10, "max_delay": 5})


def test_rate_limit_defaults():
    c = _cfg()
    assert c.rate_limit.messages_per_second == 2.0
    assert c.rate_limit.burst == 3.0


def test_load_from_json_string(tmp_path):
    raw = json.dumps({"bot_count": 1, "irc": {"server": "h", "channels": []}})
    c = config_mod.load_config(raw)
    assert c.bot_count == 1


def test_load_from_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"bot_count": 4, "irc": {"server": "h"}}))
    c = config_mod.load_config_file(str(p))
    assert c.bot_count == 4


def test_non_object_rejected():
    with pytest.raises(ConfigError):
        config_mod.load_config("[1,2,3]")


def test_ssl_settings():
    c = config_mod.load_config({
        "bot_count": 1,
        "irc": {"server": "x", "verify_tls": False},
    })
    assert c.irc.verify_tls is False
