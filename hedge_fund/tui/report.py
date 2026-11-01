"""Rich renderables for what a fund recorded: signals, the book, orders, fills.

Pure functions from records to renderables, shared by the paper screens and
the backtest board. The one Textual import is `Option`, the row type the
report rail is built from; everything else here renders with Rich alone, so
these functions test with a Console and nothing else.
"""

from __future__ import annotations

from rich import box
from rich.cells import cell_len
from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.widgets.option_list import Option

from hedge_fund.brokers import Fill
from hedge_fund.data.bars import is_intraday
from hedge_fund.llm import ThesisStream
from hedge_fund.models import Signal
from hedge_fund.pipeline import CycleRecord, DecisionRecord, SessionRecord
from hedge_fund.tui.palette import BRIGHT, CYAN, GREEN, MUTED, RED, TEXT
from hedge_fund.tui.shared import DISPLAY_NAMES, horizon_label, SHORT_NAMES

# One glyph and one colour per call, in one place: the live board knows only
# the word the stream has decoded, the report has a whole Signal, and they must
# never disagree about what bearish looks like.
_VERDICT_STYLE = {
    "bullish": ("▲", GREEN),
    "bearish": ("▼", RED),
    "neutral": ("–", "yellow"),
    "abstain": ("·", MUTED),
}


_VERDICT_LABEL = {"bullish": "강세", "bearish": "약세", "neutral": "중립", "abstain": "기권"}

_SIDE_LABEL = {"buy": "매수", "sell": "매도"}


_FLAT_REASONS = {
    "no_eligible_positions": "편입 가능한 포지션 없음",
    "missing_long_side": "숏을 상쇄할 편입 가능한 롱 없음",
    "missing_short_side": "롱을 상쇄할 편입 가능한 숏 없음",
}


# One session may carry two records: the decision executed at this close and
# the decision made at it. The rail shows both, namespaced by these prefixes.
_EXECUTED = "x:"
_DECIDED = "d:"


_TAPE_ROWS = 12


def _pad(text: str, width: int) -> str:
    """Left-justify by terminal cells, not characters: a Hangul glyph is two."""
    return text + " " * max(0, width - cell_len(text))


def verdict(signal: Signal) -> tuple[str, str, str]:
    """A signal's verdict as (glyph, word, colour) — one vocabulary, used by
    both the browser's list and its detail pane."""
    if signal.metadata.get("abstained") is True:
        word = "abstain"
    elif signal.metadata.get("signal") in ("bullish", "bearish", "neutral"):
        word = signal.metadata["signal"]
    elif signal.value > 0:
        word = "bullish"
    elif signal.value < 0:
        word = "bearish"
    else:
        word = "neutral"
    glyph, colour = _VERDICT_STYLE[word]
    return (glyph, _VERDICT_LABEL[word], colour)


def weights_line(decision: DecisionRecord, limit: int = 6) -> Text:
    """A decision's non-zero targets on one line, largest first."""
    targets = sorted(((t, w) for t, w in decision.final_weights.items() if w),
                     key=lambda x: -abs(x[1]))
    if not targets:
        return Text(_flat_line(decision), style=MUTED)
    line = Text()
    for i, (ticker, weight) in enumerate(targets[:limit]):
        if i:
            line.append("  ·  ", style=MUTED)
        line.append(ticker, style=TEXT)
        line.append(f" {weight:+.0%}", style=GREEN if weight > 0 else RED)
    if len(targets) > limit:
        line.append(f"  ·  외 {len(targets) - limit}개", style=MUTED)
    return line


class Desk:
    """One analyst's live state on the run board.

    Written by that analyst's own worker thread, read by the repaint timer.
    Every write is a single attribute assignment, so the worst a frame can show
    is the previous ticker's line — never a torn one. That is why the worker
    does not marshal each token onto the UI thread: at token rate it would
    flood the message pump, and the timer already paints faster than an eye.
    """

    def __init__(self, who: str) -> None:
        self.who = who
        self.status = "queued"
        self.ticker: str | None = None
        self.stream: ThesisStream | None = None
        self.signal: Signal | None = None

    def begin(self, ticker: str) -> None:
        self.status = "working"
        self.ticker = ticker
        self.signal = None
        self.stream = ThesisStream()

    def feed(self, text: str) -> None:
        if self.stream is not None:
            self.stream.feed(text)

    def settle(self, signal: Signal) -> None:
        """The real Signal, once predict() has returned. It supersedes the
        streamed reading — and it is the *only* thing a cache hit produces,
        since a cached answer never goes to the provider and streams nothing.
        """
        self.signal = signal

    def finish(self) -> None:
        self.status = "done"
        self.ticker = None
        self.stream = None
        self.signal = None


def desk_table(desks: list[Desk]) -> Table:
    """The live run board: one row per analyst, thesis typing itself out.

    The call arrives before the prose does — agents answer signal and
    confidence first — so a row shows its verdict the moment it is decodable
    and fills the reasoning in after. Rows move in parallel, which is the point
    of the view: two analysts reaching opposite calls on the same name, at once.
    """
    table = Table(show_header=False, box=None, padding=(0, 1),
                  expand=True, pad_edge=False)
    # Only the thesis flexes. `ratio` is what makes that true: given plain
    # widths and expand=True, rich shrinks every column proportionally when the
    # terminal is narrow, and the verdict degrades to "▲…" — which is the one
    # cell that must always be legible.
    table.add_column(width=1)                                     # status glyph
    table.add_column(width=20, no_wrap=True)                      # analyst
    table.add_column(width=5, no_wrap=True)                       # ticker
    table.add_column(width=14, no_wrap=True)                      # verdict
    table.add_column(ratio=1, overflow="ellipsis", no_wrap=True)  # thesis, live

    for desk in desks:
        if desk.status == "done":
            table.add_row(Text("✓", f"bold {GREEN}"), Text(desk.who, "bold"),
                          Text(""), Text("완료", GREEN), Text(""))
        elif desk.status == "queued":
            table.add_row(Text("⋯", MUTED), Text(desk.who, MUTED), Text(""),
                          Text(""), Text("대기", MUTED))
        else:
            table.add_row(
                Text("⋯", "yellow"),
                Text(desk.who, "bold"),
                Text(desk.ticker or "", CYAN),
                _live_verdict(desk),
                _live_thesis(desk),
            )
    return table


def roster_table(order: list[str], state: dict[str, tuple[str, str | None]]) -> Table:
    """The v1 roster look: one row per agent, working (yellow) → done (green).

    A working row's label is "TICKER" or "TICKER · date"; the date, when
    present, is tinted red as the point-in-time cursor (used by the backtest
    warm; the run-today roster has no date).
    """
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column()
    for name in order:
        status, label = state[name]
        row = Text()
        if status == "done":
            row.append("✓ ", f"bold {GREEN}")
            row.append(_pad(name, 24), "bold")
            row.append("완료", GREEN)
        elif status == "working":
            row.append("⋯ ", "yellow")
            row.append(_pad(name, 24), "bold")
            symbol, _, as_of = (label or "").partition(" · ")
            row.append("[", CYAN)
            row.append(symbol, CYAN)
            if as_of:
                row.append(" · ", CYAN)
                row.append(as_of, RED)
            row.append("] ", CYAN)
            row.append("분석 중", "yellow")
        else:
            row.append("⋯ ", MUTED)
            row.append(_pad(name, 24), MUTED)
            row.append("대기", MUTED)
        table.add_row(row)
    return table


def report_nav(record: CycleRecord | DecisionRecord, prefix: str = "") -> list[Option]:
    """The report's left rail: every signal as ONE line, then the book.

    A thesis runs paragraphs — rendering them inline made a table where a
    single signal filled the screen. Here each signal is a scannable row and
    the full thesis goes to the detail pane, so the fund's thinking stays
    navigable on a default-size terminal.

    *prefix* namespaces the option ids when one rail carries two records
    (a session that executed one decision and made the next).
    """
    options: list[Option] = []
    spec_by_name = {s.name: s for s in record.spec.strategies}
    for si, sr in enumerate(record.strategies):
        strategy = spec_by_name[sr.name]
        # Title and capital slice, nothing else — the strategy's kind and
        # neutrality are on the mandate, not worth a line in a 38-cell rail.
        # A blank row above every group but the first separates it from the
        # signals of the strategy before it.
        if si:
            options.append(Option(Text(""), disabled=True))
        options.append(Option(
            Text.assemble((strategy.title, f"bold {BRIGHT}"),
                          (f" ({sr.slice:.0%})", MUTED)),
            disabled=True,
        ))
        for sj, s in enumerate(sr.signals):
            glyph, _, tone = verdict(s)
            confidence = s.metadata.get("confidence")
            row = Text()
            row.append(f" {s.ticker:<6}", style=f"bold {CYAN}")
            row.append(_pad(SHORT_NAMES.get(s.model_name, s.model_name), 15),
                       style=TEXT)
            row.append(f"{glyph} ", style=f"bold {tone}")
            row.append(f"{confidence:.0f}%" if confidence is not None else "  —",
                       style=tone)
            options.append(Option(row, id=f"{prefix}sig:{si}:{sj}"))

    options.append(Option(Text(""), disabled=True))
    options.append(Option(Text("보유 현황", style=f"bold {BRIGHT}"), disabled=True))
    if record.clamps:
        options.append(Option(
            Text(f" 리스크 한도({len(record.clamps)})", style=TEXT), id=f"{prefix}sec:risk"))
    if isinstance(record, CycleRecord):
        options.append(Option(
            Text(f" 주문({len(record.orders)})", style=TEXT), id=f"{prefix}sec:orders"))
    options.append(Option(Text(" 포트폴리오" if isinstance(record, CycleRecord) else " 제안 배분", style=TEXT), id=f"{prefix}sec:portfolio"))
    return options


def session_nav(record: SessionRecord) -> list[Option]:
    """The rail for a whole session: the executed cycle first (that is what
    moved the book), then the decision made at this close, then — when the
    session did neither — just the marked book."""
    options: list[Option] = []
    if record.executed is not None:
        options.append(Option(Text.assemble(
            ("실행됨", f"bold {GREEN}"),
            (f"  {record.executed.as_of} 결정", MUTED)), disabled=True))
        options.extend(report_nav(record.executed, _EXECUTED))
    if record.decision is not None:
        if options:
            options.append(Option(Text(""), disabled=True))
        options.append(Option(Text.assemble(
            ("결정", f"bold {CYAN}"),
            ("  다음 세션에 실행", MUTED)), disabled=True))
        options.extend(report_nav(record.decision, _DECIDED))
    if not options:
        options.append(Option(Text("보유 현황", style=f"bold {BRIGHT}"), disabled=True))
        options.append(Option(Text(" 포트폴리오", style=TEXT), id="book"))
    return options


def session_detail(record: SessionRecord, oid: str) -> Group | None:
    if oid.startswith(_EXECUTED) and record.executed is not None:
        return _report_detail(record.executed, oid[len(_EXECUTED):])
    if oid.startswith(_DECIDED) and record.decision is not None:
        return _report_detail(record.decision, oid[len(_DECIDED):])
    if oid == "book":
        return _session_book(record)
    return None


def signal_detail(record: CycleRecord | DecisionRecord, si: int, sj: int) -> Group:
    """One analyst's full view: who, what, and the whole written thesis."""
    sr = record.strategies[si]
    signal = sr.signals[sj]
    _, word, tone = verdict(signal)
    confidence = signal.metadata.get("confidence")
    jev = signal.metadata.get("provider_metadata", {}).get("jev")
    header = Text()
    header.append(signal.ticker, style=f"bold {CYAN}")
    header.append("  ·  ", style=MUTED)
    header.append(DISPLAY_NAMES.get(signal.model_name, signal.model_name),
                  style=f"bold {BRIGHT}")
    if signal.horizon_days is not None:
        header.append(f"  ·  보유 기간 {horizon_label(signal.horizon_days)}", style=MUTED)
    facts = Text()
    facts.append(word, style=f"bold {tone}")
    if confidence is not None:
        label = "투자 확신도" if jev else "확신도"
        facts.append(f"  ·  {label} {confidence:.0f}%", style=MUTED)
    facts.append(f"  ·  시그널 값 {signal.value:+.2f}", style=MUTED)
    facts.append(f"  ·  {sr.name}", style=MUTED)
    return Group(
        header,
        facts,
        Text(""),
        Text(signal.reasoning or "기록된 논거 없음",
             style=TEXT if signal.reasoning else MUTED),
        *([Text(""), _jev_details(jev)] if jev else []),
    )


def portfolio_detail(record: CycleRecord) -> Group:
    target_net = record.equity_before * sum(record.final_weights.values())
    actual_net = sum(shares * record.marks[ticker] for ticker, shares in record.positions.items())
    details = [
        Text("포트폴리오", style=f"bold {BRIGHT}"),
        Text(f"목표 순노출 ${target_net:+,.2f} · 실제 순노출 ${actual_net:+,.2f} · 차이 ${actual_net - target_net:+,.2f}", style=MUTED),
    ]
    if any(s.blend.mode == "dollar_neutral" for s in record.spec.strategies):
        details.append(Text("달러 중립 규칙은 전략의 목표 비중에 적용됩니다. 주식 수가 정수라 실제 보유는 다를 수 있고, 다른 전략이 순노출을 더할 수 있습니다.", style=MUTED))
    if record.risk_scale_factor is not None and record.risk_scale_factor < 1:
        details.append(Text(f"리스크 한도 때문에 모든 전략의 목표 비중을 요청 노출의 {record.risk_scale_factor:.1%}로 줄였습니다.", style=MUTED))
    for strategy in record.strategies:
        if strategy.flat_reason:
            details.append(Text(f"{strategy.name}: 노출 없음({_FLAT_REASONS[strategy.flat_reason]}). 배정된 자본은 쓰지 않습니다.", style=MUTED))
    if not record.positions:
        return Group(*details, Text("보유 포지션 없음", style=MUTED))
    table = Table(box=box.SQUARE, header_style="bold", border_style="#1f2b25")
    table.add_column("종목", style=f"bold {CYAN}")
    table.add_column("방향", justify="center")
    table.add_column("주식 수", justify="right")
    table.add_column("평가액", justify="right")
    table.add_column("비중", justify="right")
    for ticker in sorted(record.positions):
        shares = record.positions[ticker]
        value = shares * record.marks[ticker]
        side = (Text("롱", style=f"bold {GREEN}") if shares > 0
                else Text("숏", style=f"bold {RED}"))
        tone = GREEN if value >= 0 else RED
        table.add_row(
            ticker, side, f"{shares:+d}",
            Text(f"${value:+,.0f}", style=tone),
            Text(f"{value / record.nav:+.1%}", style=tone),
        )
    return Group(*details, Text(""), table)


def book_summary(record: CycleRecord | SessionRecord) -> Text:
    """The always-visible bottom strip: what the book looks like after."""
    long_val = sum(max(s * record.marks[t], 0.0)
                   for t, s in record.positions.items())
    short_val = sum(min(s * record.marks[t], 0.0)
                    for t, s in record.positions.items())
    gross = (long_val - short_val) / record.nav
    net = (long_val + short_val) / record.nav
    summary = Text()
    summary.append(f"NAV ${record.nav:,.2f}", style=f"bold {BRIGHT}")
    summary.append(f"   현금 ${record.cash:,.0f}", style=CYAN)
    summary.append(f"   총노출 {gross:.0%}   순노출 {net:+.0%}", style=MUTED)
    return summary


def session_headline(name: str, record: SessionRecord) -> Text:
    """The report header for one session: what happened at this close."""
    facts = [f"세션 {record.session}"]
    if record.executed is not None:
        x = record.executed
        refreshed = x.refreshed_assessment.as_of if x.refreshed_assessment else x.as_of
        facts.append(f"{x.as_of} 결정 실행(갱신 {refreshed})")
    if record.decision is not None:
        n_signals = sum(len(sr.signals) for sr in record.decision.strategies)
        facts.append(f"결정 · 시그널 {n_signals}개 · 다음 세션에 실행")
    if record.executed is None and record.decision is None:
        facts.append("평가만 수행")
    return Text.assemble((name, f"bold {BRIGHT}"), ("  ·  " + "  ·  ".join(facts), MUTED))


def session_row(record: SessionRecord, prev_nav: float, *, time_only: bool = False) -> Text:
    """One session in the fund's history rail: date, day change, what happened.
    *time_only* shows a bar as its clock time, for a list already grouped by day."""
    change = record.nav / prev_nav - 1 if prev_nav else 0.0
    row = Text()
    label = record.session[11:] if time_only and is_intraday(record.session) else record.session
    row.append(f" {label}", style=TEXT)
    row.append(f"  {change:+.2%}", style=GREEN if change >= 0 else RED)
    if record.executed is not None:
        n = len(record.executed.fills)
        row.append(f"  체결 {n}건", style=MUTED)
    if record.decision is not None:
        row.append("  손절" if record.decision.is_stopped_out else "  결정", style=MUTED)
    return row


def session_overview(record: SessionRecord) -> Group:
    """The detail pane for a highlighted session: what it did to the book,
    in a few lines. Enter opens the full report."""
    parts: list = [
        Text.assemble((record.session, f"bold {BRIGHT}"),
                      ("  ·  리밸런싱 세션" if record.rebalance else "  ·  평가만 수행", MUTED)),
        book_summary(record),
        Text(""),
    ]
    if record.executed is not None:
        x = record.executed
        costs = f" · 비용 ${x.trading_cost:,.2f}" if x.trading_cost else ""
        parts.append(Text.assemble(
            ("실행됨  ", f"bold {GREEN}"),
            (f"{x.as_of} 결정 · 주문 {len(x.orders)}건 · 체결 {len(x.fills)}건{costs}", MUTED)))
        for fill in x.fills[:8]:
            tone = GREEN if fill.side == "buy" else RED
            parts.append(Text.assemble(
                (f"  {_SIDE_LABEL[fill.side]} ", f"bold {tone}"),
                (f"{fill.quantity:,} {fill.ticker}", TEXT),
                (f" @ ${fill.price:,.2f}", MUTED)))
        if len(x.fills) > 8:
            parts.append(Text(f"  외 {len(x.fills) - 8}건", style=MUTED))
        parts.append(Text(""))
    if record.decision is not None and record.decision.is_stopped_out:
        d = record.decision
        stop = next(c for c in d.clamps if c.limit == "max_drawdown_pct")
        parts.append(Text.assemble(
            ("손절  ", f"bold {RED}"),
            (f"낙폭 {stop.before:.1%}가 한도 {d.spec.risk.max_drawdown_pct:.1%}를 넘음 · "
             f"다음 {'봉' if is_intraday(record.session) else '세션'} 종가에 전량 매도",
             MUTED)))
        parts.append(Text(""))
    elif record.decision is not None:
        d = record.decision
        n_signals = sum(len(sr.signals) for sr in d.strategies)
        parts.append(Text.assemble(
            ("결정  ", f"bold {CYAN}"),
            (f"시그널 {n_signals}개 · 다음 "
             f"{'봉' if is_intraday(record.session) else '세션'} 종가에 실행", MUTED)))
        targets = sorted(((t, w) for t, w in d.final_weights.items() if w), key=lambda x: -abs(x[1]))
        for ticker, weight in targets[:8]:
            parts.append(Text.assemble(
                (f"  {ticker:<6}", f"bold {CYAN}"),
                (f"{weight:+.1%}", GREEN if weight > 0 else RED)))
        if not targets:
            parts.append(Text(f"  {_flat_line(d)}", style=MUTED))
        parts.append(Text(""))
    parts.append(Text("Enter로 전체 리포트 열기", style=MUTED))
    return Group(*parts)


def cost_line(trading_cost: float, traded_notional: float) -> str:
    """What the fills cost, as a fraction of what was traded: the number a
    desk compares against its alpha. Shared by the backtest header and CLI."""
    if traded_notional == 0:
        return "거래 없음"
    bps = trading_cost / traded_notional * 10_000
    return f"비용 ${trading_cost:,.0f} · 거래대금 ${traded_notional:,.0f}의 {bps:.1f} bps"


def tape_table(tape: list[tuple[str, Fill, int]]) -> Table:
    """The running trade tape: every fill of the replay so far, newest first.
    Each row is one execution — date, ticker, side, size, price, notional —
    and the book it left behind (LONG/SHORT/FLAT that ticker)."""
    table = Table.grid(expand=True, padding=(0, 1))
    for justify in ("left", "left", "left", "right", "right", "right", "left"):
        table.add_column(justify=justify)

    if not tape:
        table.add_row(Text("아직 거래 없음", style=MUTED))
        return table

    for as_of, fill, shares in list(reversed(tape))[:_TAPE_ROWS]:
        side = (Text("매수", style=f"bold {GREEN}") if fill.side == "buy"
                else Text("매도", style=f"bold {RED}"))
        book = (Text(f"→ 롱 {shares}", style=GREEN) if shares > 0
                else Text(f"→ 숏 {-shares}", style=RED) if shares < 0
                else Text("→ 청산", style=MUTED))
        table.add_row(
            Text(as_of, style=MUTED),
            Text(fill.ticker, style=f"bold {CYAN}"),
            side,
            f"{fill.quantity:,}",
            f"@ ${fill.price:,.2f}",
            Text(f"${fill.quantity * fill.price:,.0f}", style=TEXT),
            book,
        )
    if len(tape) > _TAPE_ROWS:
        table.add_row(Text(f"이전 거래 {len(tape) - _TAPE_ROWS}건", style=MUTED))
    return table


def _flat_line(decision: DecisionRecord) -> str:
    """Why a decision has no targets, in the blender's own words. A
    dollar-neutral desk with conviction but no short to balance it is the
    common case, and "no conviction" would be the wrong story to tell."""
    reasons = [(s.name, _FLAT_REASONS[s.flat_reason]) for s in decision.strategies if s.flat_reason]
    if not reasons:
        return "포지션 없음: 기준을 넘은 확신도 없음"
    if len(decision.strategies) == 1:
        return f"포지션 없음: {reasons[0][1]}"
    return "포지션 없음: " + "; ".join(f"{name}: {why}" for name, why in reasons)


def _live_verdict(desk: Desk) -> Text:
    """The call: from the finished Signal when predict() has returned, else
    from the stream as far as it has decoded, else nothing yet."""
    if desk.signal is not None:
        glyph, word, colour = verdict(desk.signal)
        confidence = desk.signal.metadata.get("confidence")
        label = f"{word} {confidence:.0f}%" if isinstance(confidence, float) else word
        return Text.assemble((f"{glyph} ", colour), (label, f"bold {colour}"))
    stream = desk.stream
    if stream is None or stream.signal is None:
        return Text("생각 중", MUTED)
    glyph, colour = _VERDICT_STYLE.get(stream.signal, ("·", MUTED))
    label = _VERDICT_LABEL.get(stream.signal, stream.signal.upper())
    if stream.confidence is not None:
        label += f" {stream.confidence:.0f}%"
    return Text.assemble((f"{glyph} ", colour), (label, f"bold {colour}"))


def _live_thesis(desk: Desk) -> Text:
    """The reasoning so far, on one line. Whitespace is collapsed because the
    row is a single line and a newline inside the thesis would eat it."""
    if desk.signal is not None:
        text = desk.signal.reasoning or ""
    elif desk.stream is not None:
        text = desk.stream.thesis
    else:
        text = ""
    return Text(" ".join(text.split()), style=TEXT)


def _report_detail(record: CycleRecord | DecisionRecord, oid: str) -> Group | None:
    """The right pane for one rail option (id without any prefix)."""
    if oid.startswith("sig:"):
        _, si, sj = oid.split(":")
        return signal_detail(record, int(si), int(sj))
    if oid == "sec:risk":
        return _risk_detail(record)
    if oid == "sec:orders" and isinstance(record, CycleRecord):
        return _orders_detail(record)
    if oid == "sec:portfolio":
        return (portfolio_detail(record) if isinstance(record, CycleRecord)
                else _proposal_detail(record))
    return None


def _session_book(record: SessionRecord) -> Group:
    """The marked book at a session's close, for sessions that only valued."""
    head = [
        Text("포트폴리오", style=f"bold {BRIGHT}"),
        Text(f"{record.session} 종가 기준 평가 · 이번 세션 주문 없음", style=MUTED),
    ]
    if not record.positions:
        return Group(*head, Text("보유 포지션 없음", style=MUTED))
    table = Table(box=box.SQUARE, header_style="bold", border_style="#1f2b25")
    table.add_column("종목", style=f"bold {CYAN}")
    table.add_column("방향", justify="center")
    table.add_column("주식 수", justify="right")
    table.add_column("평가액", justify="right")
    table.add_column("비중", justify="right")
    for ticker in sorted(record.positions):
        shares = record.positions[ticker]
        value = shares * record.marks[ticker]
        side = (Text("롱", style=f"bold {GREEN}") if shares > 0
                else Text("숏", style=f"bold {RED}"))
        tone = GREEN if value >= 0 else RED
        table.add_row(ticker, side, f"{shares:+d}",
                      Text(f"${value:+,.0f}", style=tone),
                      Text(f"{value / record.nav:+.1%}", style=tone))
    return Group(*head, Text(""), table)


def _jev_details(metadata: dict) -> Text:
    """Explain saved typed answers in the existing detail pane."""
    answers = metadata["response"]["answers"]
    probabilities = answers["direction"]["probabilities"]
    details = Text("Jev 답변 선택지별 확률\n", style=MUTED)
    details.append("  ·  ".join(
        f"{_VERDICT_LABEL[direction]} {probabilities[direction]:.1%}"
        for direction in ("bullish", "bearish", "neutral")), style=TEXT)
    details.append("\n제시된 답변 선택지에 대한 확률이며 투자 수익률이 아닙니다.\n\n")
    details.append("Jev 자체 답변 확신도\n")
    details.append("\n".join(
        f"{label}: {answers[name]['confidence']:.1%}"
        for name, label in (
            ("direction", "방향"),
            ("bullish_strength", "강세 강도"),
            ("bearish_strength", "약세 강도"),
        )), style=TEXT)
    return details


def _risk_detail(record: CycleRecord | DecisionRecord) -> Group:
    table = Table(box=box.SQUARE, header_style="bold", border_style="#1f2b25")
    table.add_column("범위", style=f"bold {CYAN}")
    table.add_column("요청", justify="right")
    table.add_column("허용", justify="right")
    table.add_column("한도", style="dim")
    for c in record.clamps:
        table.add_row(
            c.ticker or "포트폴리오 전체",
            Text(f"{c.before:+.2f}", style="yellow"),
            Text(f"{c.after:+.2f}", style="bold"),
            c.limit,
        )
    return Group(
        Text.assemble(("리스크 한도  ", f"bold {BRIGHT}"),
                      ("에이전트가 넘을 수 없는 고정 한도", MUTED)),
        Text(""),
        table,
    )


def _orders_detail(record: CycleRecord) -> Group:
    if not record.orders:
        return Group(
            Text("주문", style=f"bold {BRIGHT}"),
            Text(""),
            Text("없음: 오늘은 기준을 넘은 확신도가 없음", style=MUTED),
        )
    table = Table(box=box.SQUARE, header_style="bold", border_style="#1f2b25")
    table.add_column("구분", justify="center")
    table.add_column("수량", justify="right")
    table.add_column("종목", style=f"bold {CYAN}")
    table.add_column("평가가", justify="right")
    table.add_column("체결가", justify="right")
    for o, fill in zip(record.orders, record.fills):
        tone = f"bold {GREEN}" if o.side == "buy" else f"bold {RED}"
        table.add_row(
            Text(_SIDE_LABEL[o.side], style=tone),
            Text(f"{o.quantity:,}", style=tone),
            o.ticker,
            f"${o.price:,.2f}",
            f"${fill.price:,.2f}",
        )
    if record.slippage or record.commission:
        table.caption = f"슬리피지 ${record.slippage:,.2f} · 수수료 ${record.commission:,.2f}"
        table.caption_style = MUTED
        table.caption_justify = "left"
    return Group(Text("주문", style=f"bold {BRIGHT}"), Text(""), table)


def _proposal_detail(record: DecisionRecord) -> Group:
    return Group(
        Text("제안 배분", style=f"bold {BRIGHT}"),
        Text("목표 비중만 정했고 주문이나 체결은 아직 없습니다.", style=MUTED),
        *(Text(f"{ticker}: {weight:+.2%}", style=TEXT)
          for ticker, weight in sorted(record.final_weights.items())),
        *(Text(f"{strategy.name}: 노출 없음({_FLAT_REASONS[strategy.flat_reason]})", style=MUTED)
          for strategy in record.strategies if strategy.flat_reason),
    )
