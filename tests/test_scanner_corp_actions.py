from __future__ import annotations

import datetime as dt

from portfolio_analyzer.scanner import corp_actions as ca


def _rec(**overrides) -> dict:
    base = {
        "symbol": "XYZ", "series": "EQ", "comp": "XYZ Limited",
        "isin": "INE000A01001", "subject": "Bonus 1:1",
        "exDate": "15-Jun-2024", "recDate": "16-Jun-2024", "faceVal": "10",
    }
    base.update(overrides)
    return base


def test_ca_url_formats_dates_and_index():
    url, params = ca.ca_url(dt.date(2024, 1, 1), dt.date(2024, 12, 31))
    assert url.endswith("/corporates-corporateActions")
    assert params == {"index": "equities", "from_date": "01-01-2024", "to_date": "31-12-2024"}


def test_parse_bonus_one_to_one_halves_historical_prices():
    [action] = ca.parse_ca_records([_rec(subject="Bonus 1:1")])
    assert action.action_type == ca.ACTION_BONUS
    assert action.ratio_num == 1.0 and action.ratio_den == 1.0
    assert action.price_factor == 0.5


def test_parse_bonus_four_to_one():
    [action] = ca.parse_ca_records([_rec(subject="Bonus 4:1")])
    assert action.action_type == ca.ACTION_BONUS
    assert action.price_factor == 0.2  # 1 / (4 + 1)


def test_parse_bonus_with_dash_separator():
    [action] = ca.parse_ca_records([_rec(subject="Bonus- 1:2")])
    assert action.action_type == ca.ACTION_BONUS
    assert action.ratio_num == 1.0 and action.ratio_den == 2.0


def test_parse_ncrps_bonus_falls_through_to_other():
    # "Bonus Ncrps 1:116" — preference-share bonus, not an equity ratio event.
    [action] = ca.parse_ca_records([_rec(subject="Bonus Ncrps 1:116")])
    assert action.action_type == ca.ACTION_OTHER
    assert action.price_factor == 1.0


def test_parse_split_rs_to_rs():
    [action] = ca.parse_ca_records([_rec(
        subject="Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share",
    )])
    assert action.action_type == ca.ACTION_SPLIT
    assert action.price_factor == 0.5


def test_parse_split_rs_to_re_lowercase_variant():
    [action] = ca.parse_ca_records([_rec(
        subject="Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share",
    )])
    assert action.action_type == ca.ACTION_SPLIT
    assert action.price_factor == 0.1


def test_parse_split_no_per_share_suffix():
    [action] = ca.parse_ca_records([_rec(subject="Face Value Split From Rs 10 To Rs 2")])
    assert action.action_type == ca.ACTION_SPLIT
    assert action.price_factor == 0.2


def test_parse_compound_bonus_and_split_emits_two_rows():
    rows = ca.parse_ca_records([_rec(
        subject=(
            "Bonus 4:1/Face Value Split (Sub-Division) - "
            "From Rs 10/- Per Share To Rs 5/- Per Share"
        ),
    )])
    assert len(rows) == 2
    types = {r.action_type for r in rows}
    assert types == {ca.ACTION_BONUS, ca.ACTION_SPLIT}
    bonus = next(r for r in rows if r.action_type == ca.ACTION_BONUS)
    split = next(r for r in rows if r.action_type == ca.ACTION_SPLIT)
    assert bonus.price_factor == 0.2
    assert split.price_factor == 0.5


def test_parse_dividend_is_metadata_only():
    [action] = ca.parse_ca_records([_rec(subject="Interim Dividend - Rs 24 Per Share")])
    assert action.action_type == ca.ACTION_DIVIDEND
    assert action.price_factor == 1.0
    assert action.ratio_num is None and action.ratio_den is None


def test_parse_buyback_rights_merger_interest_classify_correctly():
    subjects = {
        "Buy Back": ca.ACTION_BUYBACK,
        "Rights 1:5": ca.ACTION_RIGHTS,
        "Scheme Of Amalgamation": ca.ACTION_MERGER,
        "Interest Payment": ca.ACTION_INTEREST,
        "Consolidation Of Shares": ca.ACTION_CONSOLIDATION,
        "Annual General Meeting": ca.ACTION_OTHER,
    }
    for subj, expected in subjects.items():
        [row] = ca.parse_ca_records([_rec(subject=subj)])
        assert row.action_type == expected, f"{subj} -> {row.action_type}"
        assert row.price_factor == 1.0


def test_parse_skips_missing_isin_or_subject_or_exdate():
    rows = ca.parse_ca_records([
        _rec(isin=""),
        _rec(subject=""),
        _rec(exDate="-"),
        _rec(exDate=""),
    ])
    assert rows == []


def test_parse_ex_date_formats():
    [action] = ca.parse_ca_records([_rec(exDate="03-Jan-2025")])
    assert action.ex_date == dt.date(2025, 1, 3)


def test_parse_preserves_raw_subject_symbol_name():
    subj = "Bonus 1:2"
    [action] = ca.parse_ca_records([_rec(subject=subj, symbol="FOO", comp="Foo Ltd")])
    assert action.raw_subject == subj
    assert action.symbol == "FOO"
    assert action.name == "Foo Ltd"


def test_fetch_ca_records_raises_on_non_list_payload(mocker):
    class FakeResp:
        status_code = 200
        def raise_for_status(self): return None
        def json(self): return {"not": "a list"}
    mocker.patch("portfolio_analyzer.scanner.corp_actions.requests.get", return_value=FakeResp())
    import pytest
    with pytest.raises(ValueError):
        ca.fetch_ca_records(dt.date(2024, 1, 1), dt.date(2024, 1, 31))
