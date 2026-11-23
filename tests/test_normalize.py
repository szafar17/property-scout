"""Tests for address normalization and cross-MLS dedupe."""

from __future__ import annotations

from property_scout.models import County, Listing, MlsBoard
from property_scout.normalize import dedupe, normalize_address


def test_normalize_expands_suffix_and_strips_punctuation():
    a = normalize_address("412 Example Avenue, Fairview, NJ 07022")
    b = normalize_address("412 Example Ave Fairview NJ 07022")
    assert a == b
    assert "AVE" in a and "AVENUE" not in a


def test_normalize_directionals():
    assert normalize_address("10 North Main St") == normalize_address("10 N Main Street")


def _l(source, mls, address, county, **kw):
    return Listing(source=source, mls_number=mls, address=address, town="Fairview",
                   county=county, **kw)


def test_dedupe_collapses_same_address_across_boards():
    nj = _l(MlsBoard.NJMLS, "24-1001", "412 Example Ave, Fairview, NJ 07022",
            County.BERGEN, list_price=419000, sqft=1320, year_built=1948,
            raw={"description": "estate sale"})
    hc = _l(MlsBoard.HCMLS, "HC-55012", "412 Example Avenue, Fairview, NJ 07022",
            County.HUDSON, list_price=419000)
    result = dedupe([nj, hc])
    assert len(result) == 1
    winner = result[0]
    # Richer NJMLS record (more fields + description) wins.
    assert winner.source is MlsBoard.NJMLS
    assert "HCMLS:HC-55012" in winner.also_seen_mls
    assert "NJMLS:24-1001" not in winner.also_seen_mls


def test_dedupe_keeps_distinct_addresses():
    a = _l(MlsBoard.NJMLS, "1", "1 Oak St, Wayne, NJ 07470", County.PASSAIC)
    b = _l(MlsBoard.NJMLS, "2", "2 Oak St, Wayne, NJ 07470", County.PASSAIC)
    assert len(dedupe([a, b])) == 2


def test_dedupe_is_order_stable():
    a = _l(MlsBoard.NJMLS, "1", "1 Oak St, Wayne, NJ 07470", County.PASSAIC)
    b = _l(MlsBoard.NJMLS, "2", "2 Oak St, Wayne, NJ 07470", County.PASSAIC)
    c = _l(MlsBoard.NJMLS, "3", "3 Oak St, Wayne, NJ 07470", County.PASSAIC)
    out = dedupe([b, a, c])
    assert [x.mls_number for x in out] == ["2", "1", "3"]
