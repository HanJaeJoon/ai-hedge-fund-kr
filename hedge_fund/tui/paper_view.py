"""What the paper screens show about a deployed fund, computed from its directory.

`PaperSnapshot` reads a fund once and answers questions about it; the
functions below turn a snapshot (and, for the approval step, a ledger state)
into Rich renderables. No screens here: everything renders with Rich, so
every row and detail pane tests with a Console and nothing else. The screens
that compose these live in paper.py.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from rich.console import Group
from rich.table import Table
from rich.text import Text

from hedge_fund.backtesting import performance_metrics
from hedge_fund.data import FDClient, FDClientError, PriceSnapshot
from hedge_fund.data.krx import open_data_client
from hedge_fund.data.bars import bar_time, bars_per_day, is_intraday
from hedge_fund.data.sessions import NEW_YORK
from hedge_fund.fund import FundSpec
from hedge_fund.paper import following_session, is_running, Ledger, LedgerError, load_deployed
from hedge_fund.pipeline import (
    DecisionRecord,
    FundState,
    is_rebalance_session,
    SessionRecord,
)
from hedge_fund.tui.palette import BRIGHT, CYAN, GREEN, MUTED, RED, TEXT
from hedge_fund.tui.report import session_row, weights_line
from hedge_fund.tui.shared import cadence_identity, money, render_area_chart, session_label

_PERIOD_WORD = {"daily": "날", "weekly": "주", "monthly": "달"}


# The reason the app's `h` writes to the kill switch. The ledger wants one for
# `aihf paper status` and the event log; the app does not ask, so it is not shown.
APP_HALT = "halted in the app"


LIVE_REFRESH = 30  # seconds between quote refreshes while the paper list is up


class PaperSnapshot:
    """Everything the paper screens show about one deployed fund, read once
    from its directory. `error` is set when the ledger cannot be trusted
    (broken chain, altered record); the fund is then display-only."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.deployed = load_deployed(directory)
        self.ledger = Ledger(directory)
        self.records: list[SessionRecord] = []
        self.state: FundState | None = None
        self.error: str | None = None
        self.running = is_running(directory)  # a runner process holds the fund
        try:
            self.records = self.ledger.records()
            self.state = self.ledger.replay(self.deployed.spec.capital)
        except LedgerError as exc:
            self.error = str(exc)

    @property
    def spec(self) -> FundSpec:
        return self.deployed.spec

    @property
    def intraday(self) -> bool:
        return self.spec.is_intraday

    def today(self, today: str | None = None) -> list[SessionRecord]:
        """A minute fund's records from *today* (NY date; default: now)."""
        today = today or datetime.now(NEW_YORK).date().isoformat()
        return [r for r in self.records if r.session[:10] == today and is_intraday(r.session)]

    @property
    def halted(self) -> str | None:
        return self.state.halted if self.state is not None else None

    @property
    def pending(self) -> DecisionRecord | None:
        return self.state.pending if self.state is not None else None

    @property
    def nav(self) -> float:
        return self.records[-1].nav if self.records else self.spec.capital

    @property
    def last_session(self) -> str | None:
        return self.records[-1].session if self.records else None

    @property
    def runnable(self) -> bool:
        return self.error is None and self.halted is None

    def curves(self) -> tuple[list[str], list[float], list[float]]:
        """Dates, NAV, and the benchmark scaled to the fund's capital."""
        dates = [r.session for r in self.records]
        nav = [r.nav for r in self.records]
        first = self.records[0].benchmark_close if self.records else 1.0
        bench = [self.spec.capital * r.benchmark_close / first for r in self.records]
        return dates, nav, bench

    def metrics(self):
        dates, nav, bench = self.curves()
        cycles = [r.executed for r in self.records if r.executed is not None]
        per_year = bars_per_day(self.spec.interval_seconds, self.spec.window) * 252 if self.intraday else 252
        return performance_metrics(self.spec.capital, dates, nav, bench, cycles, periods_per_year=per_year)

    def _state_word(self) -> tuple[str, str, str]:
        """Glyph, word, tone, the same vocabulary at every cadence: a runner
        up is running; otherwise new until the first session, then idle."""
        if self.error:
            return "✗", "원장 오류", RED
        if self.halted:
            return "■", "거래 중지됨", RED
        if self.running:
            return "●", "실행 중", GREEN
        if self.last_session is None:
            return "●", "신규", CYAN
        return "○", "대기", MUTED

    def status(self) -> Text:
        """One line: the same facts as `aihf paper status`."""
        glyph, word, tone = self._state_word()
        line = Text()
        line.append(f"{glyph} {word.upper()}  ", style=f"bold {tone}")
        if self.error:
            line.append(self.error, style=RED)
        elif self.halted:
            line.append("앱에서 거래 중지함" if self.halted == APP_HALT else self.halted, style=RED)
        elif self.last_session is None:
            line.append("아직 세션 없음", style=MUTED)
        else:
            line.append(f"마지막 세션 {self.last_session}", style=TEXT)
        return line

    def badge(self) -> Text:
        """The state as a colored glyph and a quiet word, for a header line.
        The reason behind a halt or an error is told where there is room."""
        glyph, word, tone = self._state_word()
        return Text.assemble((f"{glyph} ", tone), (word, MUTED))


def paper_slot(index: int, snap: PaperSnapshot) -> Text:
    """One fund in the paper rail: number, name, and how it is doing. One
    line; everything else about the fund belongs to the detail pane."""
    card = Text()
    card.append(f" {index + 1:02d}  ", style=MUTED)
    card.append(snap.deployed.name, style=TEXT)
    glyph, word, tone = snap._state_word()
    if snap.error:
        card.append("   ✗ 원장", style=RED)
        return card
    if snap.records and not snap.halted:
        ret = snap.nav / snap.spec.capital - 1
        card.append(f"   {ret:+.1%}", style=GREEN if ret >= 0 else RED)
        card.append("  ", style=MUTED)
    else:
        card.append("   ")
    card.append(f"{glyph} {word}", style=tone)
    return card


def next_run_rows(snap: PaperSnapshot, now: datetime | None = None) -> list[tuple[str, Text | str]]:
    """What the next run will do, from the ledger alone — no network on a
    highlight. When the next session arrives and whether a runner will take
    it; then what happens to the pending decision, then whether a new one is
    made. The exact session is resolved when the user asks to run."""
    if snap.halted:
        rows: list[tuple[str, Text | str]] = [("거래 중지됨", Text("실행하려면 먼저 r로 재개", style=RED))]
        if snap.halted not in (APP_HALT, "halted"):  # a reason given from the CLI
            rows.append(("", Text(snap.halted, style=MUTED)))
        return rows
    moment = now or datetime.now(NEW_YORK)
    rows: list[tuple[str, Text | str]] = [("다음 봉" if snap.intraday else "다음 종가", Text.assemble(
        (following_session(snap.spec, snap.last_session, moment).removesuffix(" 종가"), BRIGHT),
        ("   ● 실행 중 · space로 중지" if snap.running else "   ○ 대기 · space로 러너 시작",
         GREEN if snap.running else MUTED)))]
    if snap.pending is not None:
        rows.append(("먼저", Text.assemble(
            (session_label(snap.pending.as_of), BRIGHT), " 결정 실행")))
        rows.append(("", weights_line(snap.pending)))
    elif snap.last_session is None:
        rows.append(("먼저", "첫 봉의 체결가로 보유 현황 평가" if snap.intraday
                     else "가장 최근 완료된 종가로 보유 현황 평가"))
    else:
        rows.append(("먼저", "보유 현황 평가"))
    cadence = snap.spec.rebalance
    if snap.last_session is None:
        rows.append(("그다음", "첫 결정"))
    elif cadence in ("daily", "minute"):
        rows.append(("그다음", "새 결정"))
    else:
        rows.append(("그다음", f"새 {_PERIOD_WORD[cadence]}의 첫 세션이면 새 결정"))
    return rows


def today_rows(snap: PaperSnapshot, now: datetime | None = None) -> list[tuple[str, Text | str]]:
    """A minute fund's day so far: bars recorded out of the day's grid, the
    last one's time, and the move since the open (the NAV going into the
    day's first bar). Empty before the first bar of the day."""
    now = now or datetime.now(NEW_YORK)
    today = now.astimezone(NEW_YORK).date().isoformat()
    bars = snap.today(today)
    if not bars:
        return []
    spec = snap.spec
    total = bars_per_day(spec.interval_seconds, spec.window)
    first_index = snap.records.index(bars[0])
    opened_at = snap.records[first_index - 1].nav if first_index else spec.capital
    change = bars[-1].nav / opened_at - 1 if opened_at else 0.0
    fills = sum(len(r.executed.fills) for r in bars if r.executed is not None)
    return [
        ("봉", Text.assemble((f"{len(bars)} / {total}", TEXT),
                             (f"   마지막 {bar_time(bars[-1].session):%H:%M:%S} ET", MUTED))),
        ("시가 대비", Text.assemble((f"{change:+.2%}", GREEN if change >= 0 else RED),
                                (f"   체결 {fills}건", MUTED))),
    ]


def live_rows(
    snap: PaperSnapshot, quotes: dict[str, PriceSnapshot], today: str | None = None,
) -> list[tuple[str, Text | str]]:
    """The book at the latest prints: value, change since the last close,
    and when the newest print was. Empty unless every held name has a quote;
    muted all through when the newest print is from before *today*, because
    a stale quote marked green would be a lie."""
    if snap.state is None or not snap.state.positions:
        return []
    positions = snap.state.positions
    if any(t not in quotes for t in positions):
        return []
    value = snap.state.cash + sum(shares * quotes[t].price for t, shares in positions.items())
    change = value / snap.nav - 1 if snap.nav else 0.0
    newest = max(_quote_time(quotes[t]) for t in positions)
    today = today or datetime.now(NEW_YORK).date().isoformat()
    stale = newest.date().isoformat() < today
    tone = MUTED if stale else GREEN if change >= 0 else RED
    when = f"{newest.month}월 {newest.day}일 {newest:%H:%M}" if stale else newest.strftime("%H:%M")
    return [("실시간", Text.assemble(
        (f"{money(value, ',.0f')}", MUTED if stale else TEXT),
        (f"   {change:+.2%}", tone),
        (f"   {when} ET 기준", MUTED)))]


def paper_detail(
    snap: PaperSnapshot, width: int = 60, quotes: dict[str, PriceSnapshot] | None = None,
    now: datetime | None = None,
) -> Group:
    """The right pane, top to bottom: who the fund is, what the next run
    does, (a minute fund's day so far,) how it has done, its last few
    sessions. One bold line (the name); color only where it means something
    — gains, losses, state. *quotes* (the latest prints for the held names)
    adds a live value line; *now* is for tests."""
    spec = snap.spec
    staff = ", ".join(s.title for s in spec.strategies)
    parts: list = [
        Text.assemble((snap.deployed.name, f"bold {BRIGHT}"), "   ", snap.badge()),
        Text(f"{staff} · {cadence_identity(spec)} · {money(spec.capital, ',.0f')} · {' '.join(snap.deployed.universe)}",
             style=MUTED),
        Text(""),
    ]
    if snap.error:
        parts.append(Text(snap.error, style=RED))
        return Group(*parts)
    parts += [_section("다음 실행"), _facts(next_run_rows(snap, now))]
    today = today_rows(snap, now) if snap.intraday else []
    if today:
        parts += [Text(""), _section("오늘"), _facts(today)]
    if snap.records:
        m = snap.metrics()
        _, nav, bench = snap.curves()
        n = len(snap.records)
        ret, bench_ret = m.total_return_pct, m.benchmark_return_pct
        parts += [Text(""), _section("성과"), _facts([
            ("평가액", f"{money(snap.nav, ',.0f')}"),
            ("수익률", Text.assemble((f"{ret:+.1%}", GREEN if ret >= 0 else RED),
                                     (f"   {spec.benchmark} {bench_ret:+.1%}", MUTED))),
            ("시작", Text(f"{snap.records[0].session} · 세션 {n}개 · 주문 {m.n_orders}건", style=MUTED)),
            *(live_rows(snap, quotes) if quotes else []),
        ])]
        if n >= 2:  # one point is a flat block, not a curve
            parts.append(Text(""))
            parts.extend(render_area_chart(nav, bench, spec.capital, width))
        parts += [Text(""), _section("세션")]
        navs = [spec.capital, *(r.nav for r in snap.records)]
        shown = min(n, 3)
        for i in range(n - 1, n - 1 - shown, -1):
            parts.append(Text(" ").append_text(session_row(snap.records[i], navs[i])))
        if n > shown:
            parts.append(Text(f"   ... 외 {n - shown}개 · s로 전체 보기", style=MUTED))
    return Group(*parts)


def fetch_snapshots(tickers: list[str]) -> dict[str, PriceSnapshot]:
    """One real-time quote per ticker, fetched together. Empty when there is
    no key to ask with or the API refuses: the pane then shows the last close
    and nothing else, which is the honest fallback."""
    if not tickers or not os.environ.get("FINANCIAL_DATASETS_API_KEY"):
        return {}
    quotes: dict[str, PriceSnapshot] = {}
    try:
        with open_data_client(FDClient) as fd, ThreadPoolExecutor(max_workers=min(8, len(tickers))) as pool:
            for ticker, snapshot in zip(tickers, pool.map(fd.get_snapshot, tickers)):
                if snapshot is not None:
                    quotes[ticker] = snapshot
    except FDClientError:
        return {}
    return quotes


def run_plan(name: str, state: FundState, due: str | None, spec: FundSpec,
             now: datetime | None = None) -> Text:
    """What pressing enter will do, in plain words, from the ledger state and
    the session that is due. Pure: the confirm modal renders it, tests read it.

    Three shapes when a session is due — execute then decide, execute then
    mark, or mark only — at a close for a daily fund, at a bar's print for a
    minute fund (taken now, and the body says how long after the bar that
    is). When nothing is due, enter runs the last session again instead,
    and the body says what that replaces.
    """
    moment = now or datetime.now(NEW_YORK)
    body = Text()
    if due is None:
        if state.last_session is None:
            what = "봉이" if spec.is_intraday else f"완료된 {spec.benchmark} 세션이"
            body.append(f"아직 실행할 {what} 없습니다.  다음: ", style=TEXT)
            body.append(following_session(spec, None, moment), style=f"bold {BRIGHT}")
            body.append(".", style=TEXT)
            return body
        body.append(session_label(state.last_session), style=f"bold {BRIGHT}")
        body.append("까지 최신 상태입니다.  다음 세션: ", style=TEXT)
        body.append(following_session(spec, state.last_session, moment), style=f"bold {BRIGHT}")
        body.append(".\n\n", style=TEXT)
        body.append(session_label(state.last_session), style=f"bold {BRIGHT}")
        body.append(" 세션을 다시 실행할까요?  ", style=TEXT)
        at = "그때 본 체결가" if spec.is_intraday else "같은 종가"
        body.append(f"애널리스트가 다시 분석하고 {at}로 포지션 규모를 다시 정합니다. "
                    "기록된 세션은 교체되고, 이전 기록은 체인 밖에 따로 보관됩니다.",
                    style=MUTED)
        return body
    at = _at_phrase(due, moment)
    if state.pending is not None:
        body.append(session_label(state.pending.as_of), style=f"bold {BRIGHT}")
        body.append(" 결정 실행: ", style=TEXT)
        body.append_text(at)
        body.append("\n  ", style=TEXT)
        body.append_text(weights_line(state.pending))
        body.append("\n  비중 산정 전 분석 갱신\n", style=MUTED)
        then = "그다음 "
    else:
        body.append("보유 현황 평가: ", style=TEXT)
        body.append_text(at)
        body.append(".  거래 없음.\n", style=TEXT)
        then = ""
    if is_rebalance_session(due, state.last_session, spec.rebalance):
        why = ("펀드의 첫 결정" if state.last_session is None
               else "매 봉" if spec.rebalance == "minute"
               else "매 세션" if spec.rebalance == "daily"
               else f"{_PERIOD_WORD[spec.rebalance]}의 첫 세션")
        body.append(f"{then}새 결정", style=TEXT)
        body.append(f"  ({why})", style=MUTED)
    elif then:
        body.append("그다음 보유 현황 평가.", style=TEXT)
    return body


def _at_phrase(due: str, now: datetime) -> Text:
    """Where the session's prices come from: `<date> close`, or `<bar> ET
    print` with how late the print is taken when the bar is behind now."""
    if not is_intraday(due):
        return Text.assemble((due, f"bold {BRIGHT}"), (" 종가", TEXT))
    phrase = Text.assemble((session_label(due), f"bold {BRIGHT}"), (" 체결가, 지금 조회", TEXT))
    late = (now - bar_time(due)).total_seconds()
    if late >= 60:
        minutes = int(late // 60)
        phrase.append(f" (봉 이후 {minutes}분)", style=MUTED)
    return phrase


def day_heading(day: str, records: list[SessionRecord], navs: list[float]) -> Text:
    """A day's line over its bars in the history rail: the date, how many
    bars, and the day's move (its last bar against the NAV going into it)."""
    indices = [i for i, r in enumerate(records) if r.session[:10] == day]
    opened_at, closed_at = navs[indices[0]], navs[indices[-1] + 1]
    change = closed_at / opened_at - 1 if opened_at else 0.0
    n = len(indices)
    return Text.assemble(
        (f" {day}", f"bold {BRIGHT}"),
        (f"  {change:+.2%}", GREEN if change >= 0 else RED),
        (f"  봉 {n}개", MUTED),
    )


def _section(title: str) -> Text:
    """A block title in the detail pane: quiet, so the values carry the weight."""
    return Text(title, style=MUTED)


def _facts(rows: list[tuple[str, Text | str]]) -> Table:
    """Label/value rows with the values in one aligned column — easier to
    scan than a sentence strung together with dots."""
    grid = Table.grid(padding=(0, 3))
    grid.add_column(style=MUTED, no_wrap=True)
    grid.add_column(style=TEXT)
    for label, value in rows:
        grid.add_row(f"  {label}" if label else "", value)
    return grid


def _quote_time(snapshot: PriceSnapshot) -> datetime:
    """The quote's trade time in New York."""
    return datetime.fromisoformat(snapshot.time.replace("Z", "+00:00")).astimezone(NEW_YORK)
