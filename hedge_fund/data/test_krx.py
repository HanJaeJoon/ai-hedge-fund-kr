import pytest

from hedge_fund.data import krx

LISTINGS = [("005930", "c1", "삼성전자"), ("006400", "c2", "삼성SDI"), ("000660", "c3", "SK하이닉스"),
            ("001500", "c4", "현대차증권"), ("005380", "c5", "현대자동차")]


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(krx, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(krx.KRXClient, "_listings", lambda self: LISTINGS)
    return krx.KRXClient(api_key="x")


def test_names_resolve_exactly_then_by_a_unique_substring(client):
    assert client.resolve("005930") == ("005930", "005930")
    assert client.resolve("0001A0") == ("0001A0", "0001A0")
    assert client.resolve("삼성전자") == ("005930", "삼성전자")
    assert client.resolve("하이닉스") == ("000660", "SK하이닉스")
    assert client.resolve("현대차") == ("005380", "현대자동차")  # alias, not 현대차증권
    with pytest.raises(ValueError, match="여러 회사가 해당됩니다. 삼성전자\(005930\), 삼성SDI\(006400\)"):
        client.resolve("삼성")
    with pytest.raises(ValueError, match="일치하는 상장사가 없습니다"):
        client.resolve("KODEX")


def test_resolve_universe_only_looks_names_up_under_krx(monkeypatch, client):
    monkeypatch.delenv("HEDGE_FUND_DATA", raising=False)
    assert krx.resolve_universe(["AAPL", "MSFT"]) == (["AAPL", "MSFT"], [])
    monkeypatch.setenv("HEDGE_FUND_DATA", "krx")
    monkeypatch.setattr(krx, "KRXClient", lambda: client)
    assert krx.resolve_universe(["삼성전자", "000660"]) == (["005930", "000660"], ["삼성전자 005930"])
