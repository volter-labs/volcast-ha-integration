from custom_components.volcast.core.discovery.report import _MAX_TEXT, _mask_value, _serial_pattern

SERIAL = "9010KETU225W0123"


def test_serial_across_cut_boundary_leaves_no_fragment():
    text = "x" * (_MAX_TEXT - 6) + SERIAL + "y" * 50
    out = _mask_value(text, _serial_pattern({SERIAL}))
    assert SERIAL[:6] not in out and out.endswith("…")


def test_many_serials_before_the_cut_still_mask_a_serial_near_it():
    # Dużo podstawień PRZED miejscem cięcia skraca tekst (każdy serial → "<SN>",
    # -12 znaków) — stały bufor liczony od oryginalnej długości (poprzednia wersja)
    # mógł wtedy przesunąć niedomaskowany fragment KOLEJNEGO serialu z powrotem do
    # zachowanej części wyniku. Teraz maskujemy CAŁY tekst przed cięciem, więc tego
    # przesunięcia już nie ma.
    prefix = SERIAL * 50                          # 50 dopasowań, każde skraca o 12 znaków
    text = prefix + "x" * (2554 - len(prefix)) + SERIAL + "y" * 50
    out = _mask_value(text, _serial_pattern({SERIAL}))
    # Maskowanie skraca tekst na tyle, że po cięciu nic już nie trzeba obcinać —
    # to jest POPRAWNE: liczy się to, że żaden serial (i żaden jego fragment) nie
    # przetrwał, nie to, czy na końcu jest wielokropek.
    assert SERIAL not in out and SERIAL[:6] not in out and "<SN>" in out
