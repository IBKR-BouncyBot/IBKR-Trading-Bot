from app.ib_platform import DEFAULT_CONNECTION_PROFILES, GATEWAY_PLATFORM, normalize_profile_dict, profile_key_for
from app.models import ConnectionSettings


def test_default_connection_settings_use_gateway_live_auto_data():
    settings = ConnectionSettings()
    assert settings.platform == GATEWAY_PLATFORM
    assert settings.trading_mode == "live"
    assert settings.host == "127.0.0.1"
    assert settings.port == 4001
    assert settings.market_data_type == 0


def test_gateway_live_profile_is_first_default():
    first = DEFAULT_CONNECTION_PROFILES[0]
    assert first.key == "gateway_live"
    assert first.platform == GATEWAY_PLATFORM
    assert first.trading_mode == "live"
    assert first.port == 4001


def test_profile_key_for_gateway_live_defaults():
    assert profile_key_for(GATEWAY_PLATFORM, "live", "127.0.0.1", 4001) == "gateway_live"


def test_profile_normalization_fills_safe_defaults():
    normalized = normalize_profile_dict({})
    assert normalized["key"] == "gateway_live"
    assert normalized["platform"] == GATEWAY_PLATFORM
    assert normalized["trading_mode"] == "live"
    assert normalized["port"] == 4001
