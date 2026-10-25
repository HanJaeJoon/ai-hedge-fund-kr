"""Korean market data provider: Naver Finance prices + DART filings.

Select it with HEDGE_FUND_DATA=krx. Tickers are 6-digit KRX codes
(005930 = Samsung Electronics); the benchmark is any ETF code (069500 =
KODEX 200). Amounts are KRW.

Prices come from Naver's daily OHLCV endpoint (no key). Fundamentals come
from DART's full financial statements per report (DART_API_KEY), turned into
trailing-twelve-month metrics dated by the filing's receipt date, so a
backtest only sees what was public. Raw DART responses are cached under
~/.hedge-fund/cache/dart/ for free reruns.

News, insider trades and earnings estimates are not available -> empty.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from pathlib import Path

import requests

from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsRecord,
    FinancialMetrics,
    InsiderTrade,
    Price,
    PriceSnapshot,
)
from hedge_fund.paths import CACHE_DIR

DART = "https://opendart.fss.or.kr/api"
NAVER = "https://api.finance.naver.com/siseJson.naver"
_REPORTS = {"11013": "03-31", "11012": "06-30", "11014": "09-30", "11011": "12-31"}
# IS/CF values are cumulative within the fiscal year; BS values are balances.
_FLOW = {
    "revenue": "ifrs-full_Revenue",
    "gross_profit": "ifrs-full_GrossProfit",
    "operating_income": "dart_OperatingIncomeLoss",
    "net_income": ("ifrs-full_ProfitLossAttributableToOwnersOfParent", "ifrs-full_ProfitLoss"),
    "cfo": "ifrs-full_CashFlowsFromUsedInOperatingActivities",
    "capex": "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
}
_STOCK = {
    "equity": ("ifrs-full_EquityAttributableToOwnersOfParent", "ifrs-full_Equity"),
    "assets": "ifrs-full_Assets",
    "liabilities": "ifrs-full_Liabilities",
    "current_assets": "ifrs-full_CurrentAssets",
    "current_liabilities": "ifrs-full_CurrentLiabilities",
}


class KRXClientError(Exception):
    pass


class KRXClient:
    def __init__(self, api_key: str | None = None, timeout: float = 30.0) -> None:
        self._key = api_key or os.environ.get("DART_API_KEY", "")
        if not self._key:
            raise KRXClientError("DART_API_KEY is not set")
        self._timeout = timeout
        self._session = requests.Session()
        self._dir = CACHE_DIR / "dart"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._prices: dict[str, list[Price]] = {}
        self._corp: dict[str, str] | None = None

    def __enter__(self) -> KRXClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    # ------------------------------------------------------------------
    # Prices (Naver)
    # ------------------------------------------------------------------

    def get_prices(self, ticker: str, start_date: str, end_date: str,
                   interval: str = "day", interval_multiplier: int = 1) -> list[Price]:
        if interval != "day":
            raise KRXClientError("KRX provider has daily bars only")
        return [p for p in self._all_prices(ticker) if start_date <= p.time[:10] <= end_date]

    def _all_prices(self, ticker: str) -> list[Price]:
        if ticker not in self._prices:
            resp = self._session.get(NAVER, params={
                "symbol": ticker, "requestType": 1, "startTime": "20100101",
                "endTime": date.today().strftime("%Y%m%d"), "timeframe": "day",
            }, timeout=self._timeout)
            resp.raise_for_status()
            rows = ast.literal_eval(re.sub(r"\s+", "", resp.text))
            self._prices[ticker] = [
                Price(time=f"{r[0][:4]}-{r[0][4:6]}-{r[0][6:]}", open=r[1], high=r[2],
                      low=r[3], close=r[4], volume=int(r[5]))
                for r in rows[1:] if r[4]
            ]
        return self._prices[ticker]

    def _close_on_or_before(self, ticker: str, day: str) -> float | None:
        bars = [p for p in self._all_prices(ticker) if p.time[:10] <= day]
        return bars[-1].close if bars else None

    def get_snapshot(self, ticker: str) -> PriceSnapshot | None:
        return None

    # ------------------------------------------------------------------
    # DART
    # ------------------------------------------------------------------

    def _dart(self, endpoint: str, **params) -> dict:
        name = "_".join([endpoint, *(str(v) for v in params.values())]) + ".json"
        path = self._dir / name
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        resp = self._session.get(f"{DART}/{endpoint}.json", params={"crtfc_key": self._key, **params},
                                 timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") not in ("000", "013"):  # 013 = no data
            raise KRXClientError(f"DART {endpoint}: {data.get('status')} {data.get('message')}")
        if data.get("status") == "000" or params.get("bsns_year", 9999) < date.today().year:
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def _corp_code(self, ticker: str) -> str:
        if self._corp is None:
            path = self._dir / "corpCode.xml"
            if not path.exists():
                resp = self._session.get(f"{DART}/corpCode.xml", params={"crtfc_key": self._key},
                                         timeout=self._timeout)
                resp.raise_for_status()
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    path.write_bytes(zf.read(zf.namelist()[0]))
            root = ET.parse(path).getroot()
            self._corp = {
                el.findtext("stock_code").strip(): el.findtext("corp_code")
                for el in root.iter("list") if el.findtext("stock_code", "").strip()
            }
        try:
            return self._corp[ticker]
        except KeyError:
            raise KRXClientError(f"{ticker}: not a listed KRX stock code in DART") from None

    def _report(self, corp: str, year: int, code: str) -> dict | None:
        """Flows (cumulative YTD), balances, filing date for one report; None if not filed."""
        data = self._dart("fnlttSinglAcntAll", corp_code=corp, bsns_year=year, reprt_code=code, fs_div="CFS")
        rows = data.get("list") or []
        if not rows:
            return None
        out: dict = {"filed": None}
        for r in rows:
            if r["sj_div"] not in ("BS", "IS", "CIS", "CF"):  # CIS: single-statement filers (e.g. SK hynix)
                continue
            out["filed"] = out["filed"] or f"{r['rcept_no'][:4]}-{r['rcept_no'][4:6]}-{r['rcept_no'][6:8]}"
            for key, tags in _FLOW.items():
                if r["sj_div"] != "BS" and _rank(r["account_id"], tags) < out.get(f"_{key}", 9):
                    out[key] = _num(r.get("thstrm_add_amount") if r["sj_div"] != "CF" else None) or _num(r["thstrm_amount"])
                    out[f"_{key}"] = _rank(r["account_id"], tags)
            for key, tags in _STOCK.items():
                if r["sj_div"] == "BS" and _rank(r["account_id"], tags) < out.get(f"_{key}", 9):
                    out[key] = _num(r["thstrm_amount"])
                    out[f"_{key}"] = _rank(r["account_id"], tags)
        return out if out.get("net_income") is not None and out.get("equity") else None

    def _shares(self, corp: str, year: int) -> float | None:
        for y in (year, year - 1, year - 2):
            data = self._dart("stockTotqySttus", corp_code=corp, bsns_year=y, reprt_code="11011")
            for r in data.get("list") or []:
                if r["se"].startswith("보통주") and _num(r["istc_totqy"]):
                    return _num(r["istc_totqy"])
        return None

    def get_financial_metrics(self, ticker: str, end_date: str, period: str = "ttm",
                              limit: int = 10) -> list[FinancialMetrics]:
        corp = self._corp_code(ticker)
        this_year = int(end_date[:4])
        reports: dict[tuple[int, str], dict] = {}
        for year in range(this_year - (limit // 4) - 2, this_year + 1):
            for code in _REPORTS:
                rep = self._report(corp, year, code)
                if rep and rep["filed"] and rep["filed"] <= end_date:
                    reports[(year, code)] = rep

        def ttm(year: int, code: str, key: str) -> float | None:
            cur = reports.get((year, code), {}).get(key)
            if code == "11011":
                return cur
            prev_fy = reports.get((year - 1, "11011"), {}).get(key)
            prev_q = reports.get((year - 1, code), {}).get(key)
            if None in (cur, prev_fy, prev_q):
                return None
            return prev_fy + cur - prev_q

        rows: list[FinancialMetrics] = []
        for (year, code), rep in sorted(reports.items(), key=lambda kv: kv[1]["filed"], reverse=True):
            ni = ttm(year, code, "net_income")
            rev = ttm(year, code, "revenue")
            if ni is None or rev is None:
                continue
            shares = self._shares(corp, year)
            close = self._close_on_or_before(ticker, rep["filed"])
            mcap = shares * close if shares and close else None
            fcf = _sub(ttm(year, code, "cfo"), ttm(year, code, "capex"))
            eq, assets, liab = rep.get("equity"), rep.get("assets"), rep.get("liabilities")
            prev_rev, prev_ni = ttm(year - 1, code, "revenue"), ttm(year - 1, code, "net_income")
            rows.append(FinancialMetrics(
                ticker=ticker, report_period=f"{year}-{_REPORTS[code]}", period="ttm", currency="KRW",
                filing_date=rep["filed"],
                # shortcut: market_cap left None so the blind prompt skips the US-dollar
                # size percentile, which is meaningless for KRW; fill it once breakpoints
                # know about currency.
                market_cap=None,
                price_to_earnings_ratio=_div(mcap, ni),
                price_to_book_ratio=_div(mcap, eq),
                price_to_sales_ratio=_div(mcap, rev),
                free_cash_flow_yield=_div(fcf, mcap),
                gross_margin=_div(ttm(year, code, "gross_profit"), rev),
                operating_margin=_div(ttm(year, code, "operating_income"), rev),
                net_margin=_div(ni, rev),
                return_on_equity=_div(ni, eq),
                return_on_assets=_div(ni, assets),
                current_ratio=_div(rep.get("current_assets"), rep.get("current_liabilities")),
                debt_to_equity=_div(liab, eq),
                debt_to_assets=_div(liab, assets),
                revenue_growth=_growth(rev, prev_rev),
                earnings_growth=_growth(ni, prev_ni),
                earnings_per_share=_div(ni, shares),
                book_value_per_share=_div(eq, shares),
                free_cash_flow_per_share=_div(fcf, shares),
            ))
            if len(rows) == limit:
                break
        return rows

    def get_company_facts(self, ticker: str) -> CompanyFacts | None:
        data = self._dart("company", corp_code=self._corp_code(ticker))
        if data.get("status") != "000":
            return None
        return CompanyFacts(ticker=ticker, name=data.get("corp_name"), exchange="KRX",
                            industry=data.get("induty_code"), location=data.get("adres"))

    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        shares = self._shares(self._corp_code(ticker), int(end_date[:4]))
        close = self._close_on_or_before(ticker, end_date)
        return shares * close if shares and close else None

    def get_news(self, ticker, end_date, start_date=None, limit=1000) -> list[CompanyNews]:
        return []

    def get_insider_trades(self, ticker, end_date, start_date=None, limit=1000) -> list[InsiderTrade]:
        return []

    def get_earnings(self, ticker: str) -> Earnings | None:
        return None

    def get_earnings_history(self, ticker: str, limit: int = 12) -> list[EarningsRecord]:
        return []


def _rank(account_id: str, tags) -> int:
    """Position of account_id in the preferred-tag list; 9 if absent."""
    tags = (tags,) if isinstance(tags, str) else tags
    return tags.index(account_id) if account_id in tags else 9


def _num(text) -> float | None:
    if text in (None, "", "-"):
        return None
    return float(str(text).replace(",", ""))


def _div(a, b) -> float | None:
    return a / b if a is not None and b else None


def _sub(a, b) -> float | None:
    return a - b if a is not None and b is not None else None


def _growth(cur, prev) -> float | None:
    return cur / prev - 1 if cur is not None and prev else None


def open_data_client():
    """FDClient by default; KRXClient when HEDGE_FUND_DATA=krx."""
    if os.environ.get("HEDGE_FUND_DATA", "").lower() == "krx":
        return KRXClient()
    from hedge_fund.data.client import FDClient
    return FDClient()
