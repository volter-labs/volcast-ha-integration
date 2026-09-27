from custom_components.volcast.core.discovery.report import _MAX_TEXT, _mask_value, _serial_pattern

SERIAL = "9010KETU225W0123"


def test_serial_across_cut_boundary_leaves_no_fragment():
    text = "x" * (_MAX_TEXT - 6) + SERIAL + "y" * 50
    out = _mask_value(text, _serial_pattern({SERIAL}))
    assert SERIAL[:6] not in out and out.endswith("…")
