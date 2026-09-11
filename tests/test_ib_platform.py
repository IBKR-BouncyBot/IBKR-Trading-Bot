from app.ib_platform import (
    DEFAULT_CONNECTION_PROFILES,
    GATEWAY_PLATFORM,
    TWS_PLATFORM,
    default_port,
    platform_label,
    profile_key_for,
)
from app.models import ConnectionSettings
from app.storage import BotStorage


def test_default_connection_profiles_include_tws_and_gateway_modes():
    keys = {profile.key for profile in DEFAULT_CONNECTION_PROFILES}
    assert {"tws_paper", "tws_live", "gateway_paper", "gateway_live"}.issubset(keys)


def test_default_ports_match_selected_platform_and_mode():
    assert default_port(TWS_PLATFORM, "paper") == 7497
    assert default_port(TWS_PLATFORM, "live") == 7496
    assert default_port(GATEWAY_PLATFORM, "paper") == 4002
    assert default_port(GATEWAY_PLATFORM, "live") == 4001


def test_profile_key_returns_custom_for_non_profile_port():
    assert profile_key_for(TWS_PLATFORM, "paper", "127.0.0.1", 7497) == "tws_paper"
    assert profile_key_for(GATEWAY_PLATFORM, "live", "127.0.0.1", 4001) == "gateway_live"
    assert profile_key_for(GATEWAY_PLATFORM, "live", "127.0.0.1", 9999) == "custom"


def test_connection_settings_accept_gateway_and_reject_unknown_platform():
    good = ConnectionSettings(platform=GATEWAY_PLATFORM, trading_mode="paper", port=4002)
    assert good.validate() == []
    bad = ConnectionSettings(platform="rest", trading_mode="paper", port=4002)
    assert any("Platform" in item for item in bad.validate())


def test_connection_settings_roundtrip_gateway_fields(tmp_path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    cfg = ConnectionSettings(
        host="127.0.0.1",
        port=4002,
        client_id=22,
        trading_mode="paper",
        platform=GATEWAY_PLATFORM,
        platform_path=r"C:\\Jts\\ibgateway\\ibgateway.exe",
        market_data_type=0,
    )
    storage.save_connection_settings(cfg)
    loaded = storage.load_connection_settings()

    assert loaded.platform == GATEWAY_PLATFORM
    assert loaded.port == 4002
    assert loaded.platform_path.endswith("ibgateway.exe")
    assert platform_label(loaded.platform) == "IB Gateway"
