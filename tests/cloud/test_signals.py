import pytest

from custom_components.volcast.cloud.signals import (LIVE_MAX_S, SIGNALS_VERSION_MAJOR,
                                                     ChannelCfg, Signals, parse_signals,
                                                     redact_topic)

BASE = "https://staging.example.test"
HEX = "0123456789abcdef" * 4
TOPIC = f"ha-sig:{HEX}"
URL = "wss://staging.example.test/realtime/v1/websocket"
KEY = "eyJhbGciOi.payload.sig"


def block(**over):
    chan = {"url": URL, "apikey": KEY, "topic": TOPIC}
    b = {"version": 1, "channel": chan, "live_for_s": 118}
    b.update(over)
    return b


def chan(**over):
    return block(channel={"url": URL, "apikey": KEY, "topic": TOPIC, **over})


def test_constants():
    assert LIVE_MAX_S == 900
    assert SIGNALS_VERSION_MAJOR == 1


def test_valid_block():
    s = parse_signals(block(), base_url=BASE)
    assert s == Signals(1, ChannelCfg(URL, KEY, TOPIC), 118)


def test_host_case_insensitive_and_extra_fields_ignored():
    s = parse_signals(block(channel={"url": "wss://STAGING.example.test/x", "apikey": KEY,
                                     "topic": TOPIC, "zzz": 1}, consent=True), base_url=BASE)
    assert s is not None and s.channel is not None


def test_minor_version_ok():
    assert parse_signals(block(version=1), base_url=BASE) is not None


@pytest.mark.parametrize("raw", [None, [], "x", 1, (1,)])
def test_not_a_dict(raw):
    assert parse_signals(raw, base_url=BASE) is None


@pytest.mark.parametrize("v", [2, 0, True, False, "1", 1.0, None])
def test_bad_version(v):
    assert parse_signals(block(version=v), base_url=BASE) is None


def test_missing_version():
    b = block()
    del b["version"]
    assert parse_signals(b, base_url=BASE) is None


@pytest.mark.parametrize("over", [
    {"url": "wss://evil.example/realtime/v1/websocket"},
    {"url": "wss://staging.example.test.evil.example/x"},
    {"url": "ws://staging.example.test/x"},
    {"url": "https://staging.example.test/x"},
    {"url": "wss://user:pw@staging.example.test/x"},
    {"url": "wss://user@staging.example.test/x"},
    {"url": "wss://staging.example.test/x#frag"},
    {"url": "wss://staging.example.test/x?apikey=1"},
    {"url": "wss://staging.example.test/x?"},
    {"url": "wss://staging.example.test/x y"},
    {"url": "wss://staging.example.test/x\n"},
    {"url": "wss://staging.example.test:8443/x"},
    {"url": "wss://staging.example.test/" + "a" * 600},
    {"url": 5}, {"url": None},
    {"topic": "ha-sig:" + "0" * 63},
    {"topic": "ha-sig:" + "0" * 65},
    {"topic": "ha-sig:" + "A" * 64},
    {"topic": "xx-sig:" + "0" * 64},
    {"topic": HEX},
    {"topic": TOPIC + "\n"},
    {"topic": 7},
    {"apikey": ""}, {"apikey": "a" * 4097}, {"apikey": "a b"}, {"apikey": "a\tb"},
    {"apikey": 1}, {"apikey": None},
])
def test_bad_channel_drops_only_channel(over):
    s = parse_signals(chan(**over), base_url=BASE)
    assert s == Signals(1, None, 118)


def test_port_must_match():
    s = parse_signals(chan(url="wss://staging.example.test:8443/x"),
                      base_url="https://staging.example.test:8443")
    assert s is not None and s.channel is not None
    s = parse_signals(chan(), base_url="https://staging.example.test:8443")
    assert s is not None and s.channel is None
    # jawny :443 == domyślny port https/wss
    s = parse_signals(chan(url="wss://staging.example.test:443/x"), base_url=BASE)
    assert s is not None and s.channel is not None


def test_apikey_boundaries():
    assert parse_signals(chan(apikey="a"), base_url=BASE).channel is not None
    assert parse_signals(chan(apikey="a" * 4096), base_url=BASE).channel is not None


@pytest.mark.parametrize("ch", [None, "x", [], 5, {}, {"url": URL}])
def test_channel_not_usable(ch):
    assert parse_signals(block(channel=ch), base_url=BASE) == Signals(1, None, 118)


def test_channel_missing():
    b = block()
    del b["channel"]
    assert parse_signals(b, base_url=BASE) == Signals(1, None, 118)


@pytest.mark.parametrize("v,exp", [(0, 0), (5, 5), (900, 900), (901, 900), (10**9, 900),
                                   (-1, 0), (True, 0), (False, 0), ("5", 0), (5.0, 0),
                                   (None, 0)])
def test_live_for_s(v, exp):
    assert parse_signals(block(live_for_s=v), base_url=BASE).live_for_s == exp


def test_live_for_s_missing():
    b = block()
    del b["live_for_s"]
    assert parse_signals(b, base_url=BASE).live_for_s == 0


def test_bad_base_url_never_raises():
    assert parse_signals(block(), base_url="") == Signals(1, None, 118)
    assert parse_signals(block(), base_url="not a url") == Signals(1, None, 118)


def test_redact_topic():
    r = redact_topic(TOPIC)
    assert r == "ha-sig:01234567…"
    assert HEX[:9] not in r


@pytest.mark.parametrize("bad", ["", "x", HEX, "ha-sig:", "ha-sig:zz", None, 5,
                                 "ha-sig:" + "A" * 64])
def test_redact_invalid(bad):
    assert redact_topic(bad) == "ha-sig:…"
