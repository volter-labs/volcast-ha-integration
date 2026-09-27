import pytest

from custom_components.volcast.core.modbus.blocks import plan_blocks, read_plan, spec_addresses, split_block
from custom_components.volcast.core.profile import load_builtin

GW = load_builtin("goodwe-et")
DEYE = load_builtin("deye-sg")
# Zakresy odczytu Boxa (implementacja referencyjna) dla GoodWe ET.
GW_REFERENCE = [(35100, 125), (37000, 8), (36000, 19), (47509, 4), (45356, 1), (47760, 1)]


def _covered(blocks):
    return {a for start, n in blocks for a in range(start, start + n)}


def _needed(profile, *, write=True):
    out = set()
    for spec in profile.raw["read"].values():
        out |= set(spec_addresses(spec))
    if write:
        for key, spec in profile.raw["write"].items():
            if key not in ("tou_program", "tou_enable"):
                out.add(spec["addr"])
    return out


def test_spec_addresses():
    assert spec_addresses({"addr": 10, "type": "u16"}) == [10]
    assert spec_addresses({"addr": 10, "type": "f32"}) == [10, 11]
    assert spec_addresses({"addr": 10, "type": "ascii", "len": 3}) == [10, 11, 12]
    assert spec_addresses({"sum": [{"addr": 5, "type": "u32"}, {"ref": "x"}, {"addr": 9, "type": "i16"}]}) == [5, 6, 9]
    assert spec_addresses({"addr": 47511, "type": "u16", "encode": "mode"}) == [47511]


def test_goodwe_plan_matches_reference_blocks():
    blocks = read_plan(GW)
    assert _needed(GW) <= _covered(blocks)
    for start, n in blocks:
        assert 1 <= n <= 125
        assert any(r0 <= start and start + n <= r0 + rn for r0, rn in GW_REFERENCE), (start, n)
    assert not _covered(blocks) & set(range(35003, 35011))          # bez rejestrów seryjnych
    assert 47760 in _covered(blocks)                                  # klucz zapisu bez odczytu w mapie read


def test_goodwe_plan_without_write_keys_skips_write_only_registers():
    assert 47760 not in _covered(read_plan(GW, include_write_keys=False))


def test_identify_reads_only_on_request():
    assert set(range(35000, 35033)) <= _covered(read_plan(GW, include_identify=True))
    assert not _covered(read_plan(GW)) & set(range(35000, 35033))


def test_plan_merges_within_gap_and_splits_over_max():
    assert plan_blocks([1, 2, 3, 20], 125) == [(1, 20)]                # dziura 16 słów — scalone
    assert plan_blocks([1, 2, 3, 21], 125) == [(1, 3), (21, 1)]        # dziura 17 słów — osobno
    assert plan_blocks([1, 5], 125, max_gap=2) == [(1, 1), (5, 1)]
    assert plan_blocks(range(0, 300), 125) == [(0, 125), (125, 125), (250, 50)]
    assert plan_blocks([7, 3, 3, 5], 4) == [(3, 3), (7, 1)]
    assert plan_blocks([], 125) == []
    with pytest.raises(ValueError):
        plan_blocks([1], 0)


def test_deye_plan_covers_tou_block_and_respects_max():
    blocks = read_plan(DEYE)
    cov = _covered(blocks)
    assert set(range(146, 178)) - {147} - set(range(160, 166)) <= cov
    assert _needed(DEYE) <= cov
    assert all(n <= 100 for _, n in blocks)
    assert not cov & set(range(3, 8))                                 # numer seryjny
    no_tou = _covered(read_plan(DEYE, include_tou=False))
    assert not no_tou & set(range(146, 178))


def test_split_block_per_key():
    parts = split_block((47509, 4), GW)
    assert parts == [(47509, 1), (47510, 1), (47511, 1), (47512, 1)]
    running = split_block((35100, 125), GW)
    assert (35105, 2) in running and (35182, 2) in running and (35180, 1) in running
    assert all(35100 <= s and s + n <= 35225 for s, n in running)
    tou = split_block((146, 32), DEYE)
    assert (148, 6) in tou and (172, 6) in tou and (146, 1) in tou
    assert split_block((1000, 5), GW) == []
