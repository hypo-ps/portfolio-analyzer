from __future__ import annotations

import datetime as dt
import io
import zipfile

import pytest

from portfolio_analyzer.scanner import bhavcopy as bc

UDIFF_HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,"
    "XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,"
    "LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
    "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
    "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)


def _row(
    *, date: str = "2026-04-22", fin_tp: str = "STK", isin: str = "INE009A01021",
    symbol: str = "INFY", series: str = "EQ", name: str = "INFOSYS LIMITED",
    o: str = "1295.00", h: str = "1297.70", l: str = "1255.90", c: str = "1268.60",
    prev: str = "1313.20", vol: str = "20088378", turnover: str = "25498272367.70",
    trades: str = "477139",
) -> str:
    return (
        f"{date},{date},CM,NSE,{fin_tp},1594,{isin},{symbol},{series},,,,,"
        f"{name},{o},{h},{l},{c},{c},{prev},,{c},,,{vol},{turnover},{trades},F1,1,,,,,"
    )


def _csv(rows: list[str]) -> str:
    return UDIFF_HEADER + "\n" + "\n".join(rows) + "\n"


def test_bhavcopy_url_formats_date():
    url = bc.bhavcopy_url(dt.date(2026, 4, 22))
    assert url.endswith("BhavCopy_NSE_CM_0_0_0_20260422_F_0000.csv.zip")


def test_parse_keeps_eq_and_be_stk_only():
    csv_text = _csv([
        _row(symbol="INFY", series="EQ"),
        _row(symbol="YESBANK", series="BE", isin="INE528G01035"),
        _row(symbol="OPTSYM", fin_tp="OPTSTK", series="EQ", isin="INE000000001"),
        _row(symbol="SMEONE", series="SM", isin="INE000000002"),
    ])
    rows = bc.parse_bhavcopy(csv_text)
    assert {r.symbol for r in rows} == {"INFY", "YESBANK"}
    assert all(r.series in {"EQ", "BE"} for r in rows)


def test_parse_populates_fields():
    rows = bc.parse_bhavcopy(_csv([_row()]))
    assert len(rows) == 1
    row = rows[0]
    assert row.trade_date == dt.date(2026, 4, 22)
    assert row.isin == "INE009A01021"
    assert row.symbol == "INFY"
    assert row.name == "INFOSYS LIMITED"
    assert row.series == "EQ"
    assert row.open == 1295.00
    assert row.high == 1297.70
    assert row.low == 1255.90
    assert row.close == 1268.60
    assert row.prev_close == 1313.20
    assert row.volume == 20088378
    assert row.turnover == 25498272367.70
    assert row.trades == 477139


def test_parse_skips_rows_missing_isin_or_symbol():
    rows = bc.parse_bhavcopy(_csv([
        _row(isin="", symbol="NOISIN"),
        _row(isin="INE000000099", symbol=""),
        _row(),
    ]))
    assert [r.symbol for r in rows] == ["INFY"]


def test_parse_skips_rows_with_non_numeric_price():
    rows = bc.parse_bhavcopy(_csv([_row(o="abc")]))
    assert rows == []


def test_parse_raises_on_missing_required_columns():
    csv_text = "TradDt,ISIN,TckrSymb\n2026-04-22,INE009A01021,INFY\n"
    with pytest.raises(ValueError, match="missing required columns"):
        bc.parse_bhavcopy(csv_text)


def test_unzip_bhavcopy_extracts_single_csv():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("BhavCopy_NSE_CM_0_0_0_20260422_F_0000.csv", _csv([_row()]))
    text = bc.unzip_bhavcopy(buf.getvalue())
    assert "INFY" in text and text.startswith("TradDt,")


def test_unzip_bhavcopy_rejects_no_csv():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("notes.txt", "hello")
    with pytest.raises(ValueError, match="no CSV"):
        bc.unzip_bhavcopy(buf.getvalue())
