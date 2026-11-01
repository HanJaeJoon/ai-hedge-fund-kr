"""Run the AI hedge fund.

Two modes, one engine. A backtest replays a mandate over history; a paper
fund is that same mandate deployed against a universe and advanced one
completed session at a time, keeping a ledger. Both call `advance`; only
the clock, the broker, and where the state lives differ.

Usage::

    aihf
        No arguments: the interactive app (a Textual TUI). Backtest a
        mandate, deploy and advance a paper fund, or build a new mandate.

    aihf backtest ~/.hedge-fund/mandates/example.yaml --universe AAPL,MSFT
        Replay the mandate over history (default: the last 18 months up to
        the latest completed session). Full result JSON on stdout; a copy
        lands in ~/.hedge-fund/research/. --start/--end/--out/--model.

    aihf paper create alpha --mandate ~/.hedge-fund/mandates/example.yaml --universe AAPL,MSFT
        Deploy a paper fund: snapshot the mandate, fix the universe, open an
        empty ledger and a book with the mandate's capital.

    aihf paper tick alpha
        Advance the fund by exactly one session and append it to the ledger:
        the oldest unrecorded completed close for a daily fund, the current
        bar at prints taken now for a minute fund. Idempotent; what a
        scheduler calls after each close:

            30 18 * * 1-5  aihf paper tick alpha >> ~/.hedge-fund/paper/alpha/tick.log 2>&1

        A missed day is never skipped — the next tick advances the oldest
        unrecorded session. --session names it explicitly.

    aihf paper run alpha
        Stay up and do what tick does whenever a session is due: after each
        midnight for a daily fund, at every bar inside the window for a
        minute fund. One process per fund, found by its runner.pid; Ctrl-C,
        SIGTERM or `aihf paper stop alpha` stop it after the session in
        flight. A minute fund's time down is logged as a gap, never back-filled.

    aihf paper status alpha | list | halt alpha --reason "..." | resume alpha

    aihf paper scorecard alpha
        Grade every analyst (hit rate, slugging, IC) and every strategy
        (P&L between executions) on the fund's ledger. JSON on stdout.

A mandate is the desk — strategies, staff, risk, capital, cadence — and
never names tickers; the universe is fixed when a fund is deployed (paper)
or given per study (backtest).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

from rich.console import Console

from hedge_fund.backtesting import backtest_fund, save_result
from hedge_fund.data import CachedDataClient, FDClient
from hedge_fund.data.krx import open_data_client
from hedge_fund.data.bars import is_intraday
from hedge_fund.data.sessions import completed_through
from hedge_fund.fund import Fund, load_spec
from hedge_fund.paper import (
    deploy,
    is_running,
    Ledger,
    LedgerError,
    list_deployed,
    load_deployed,
    NothingDue,
    redo,
    serve,
    stop_runner,
    tick,
    validate_fund_name,
)
from hedge_fund.paths import ensure_mandates_dir, PAPER_DIR
from hedge_fund.pipeline import FundHalted, SessionRecord
from hedge_fund.scorecard import score
from hedge_fund.tui.keys import apply_credentials
from hedge_fund.tui.report import cost_line
from hedge_fund.tui.scorecard_view import analyst_table, strategy_table
from hedge_fund.tui.shared import BACKTEST_WEEKS, money, parse_universe


def main() -> None:
    apply_credentials()
    ensure_mandates_dir()
    parser = _parser()
    args = parser.parse_args()

    if args.model:
        os.environ["HEDGE_FUND_LLM_MODEL"] = args.model

    if args.command is None:
        # The interactive experience is the Textual app. Import it lazily so
        # the non-interactive paths never pay to load Textual.
        from hedge_fund.tui.app import HedgeFundApp

        HedgeFundApp().run()
        return

    console = Console(stderr=True)  # status + summary on stderr; stdout stays pure JSON
    try:
        if args.command == "backtest":
            _backtest(args, parser, console)
        else:
            _paper(args, parser, console)
    except (FundHalted, NothingDue, LedgerError) as exc:
        console.print(f"[red]{exc}[/]")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aihf",
        description="AI 헤지펀드를 실행합니다. 인자가 없으면 대화형 앱을 엽니다. "
        "`backtest`는 운용 지침을 과거 데이터로 재현하고, `paper`는 모의투자 펀드를 "
        "배포하고 진행합니다.",
    )
    parser.add_argument(
        "--model",
        help="투자자 에이전트가 쓸 LLM, 예: claude-opus-5-5 "
        "(기본값: HEDGE_FUND_LLM_MODEL 환경 변수, 없으면 내장 기본값). 퀀트 "
        "모델은 이 값을 무시합니다",
    )
    commands = parser.add_subparsers(dest="command")

    backtest = commands.add_parser(
        "backtest", help="운용 지침을 과거 데이터로 재현",
        description="운용 지침을 과거 데이터로 재현합니다. --start부터 --end까지 벤치마크의 "
        "모든 세션을 진행하며, 리밸런싱 세션 종가의 결정은 다음 종가에 체결됩니다. "
        "전체 결과 JSON은 stdout으로 출력하고 사본을 research/에 저장합니다.",
    )
    backtest.add_argument("mandate", help="운용 지침 YAML 경로, 예: ~/.hedge-fund/mandates/example.yaml")
    backtest.add_argument("--universe", required=True, help=_UNIVERSE_HELP)
    backtest.add_argument("--start", help=f"첫 세션 YYYY-MM-DD (기본값: --end의 {BACKTEST_WEEKS}주 전)")
    backtest.add_argument("--end", help="마지막 세션 YYYY-MM-DD (기본값: 가장 최근에 끝난 세션)")
    backtest.add_argument("--out", help="결과 JSON을 이 파일에도 저장")

    paper = commands.add_parser("paper", help="모의투자 펀드 배포와 진행")
    actions = paper.add_subparsers(dest="action", required=True)

    create = actions.add_parser("create", help="운용 지침을 모의투자 펀드로 배포")
    create.add_argument("name", help="펀드 이름. ~/.hedge-fund/paper/<name>/ 디렉터리가 됩니다")
    create.add_argument("--mandate", required=True, help="운용 지침 YAML 경로")
    create.add_argument("--universe", required=True, help=_UNIVERSE_HELP)

    advance = actions.add_parser("tick", help="펀드를 세션 하나만큼 진행")
    advance.add_argument("name")
    advance.add_argument("--session", help="기록할 세션(YYYY-MM-DD, 봉이면 YYYY-MM-DDTHH:MM:SS). "
                                           "다음 미기록 세션이어야 합니다")
    advance.add_argument("--again", action="store_true",
                         help="마지막으로 기록된 세션을 다시 실행하고 기록을 교체")

    runner = actions.add_parser("run", help="계속 실행하며 세션이 돌아올 때마다 펀드를 진행")
    runner.add_argument("name")

    stopper = actions.add_parser("stop", help="진행 중인 세션이 끝나면 펀드 러너를 중지")
    stopper.add_argument("name")

    status = actions.add_parser("status", help="NAV, 마지막 세션, 대기 중인 결정, 거래 중지 상태")
    status.add_argument("name")

    actions.add_parser("list", help="배포된 모든 모의투자 펀드")

    scorecard = actions.add_parser("scorecard", help="원장 기준으로 모든 애널리스트와 전략을 평가")
    scorecard.add_argument("name")

    halt = actions.add_parser("halt", help="킬 스위치 설정: 틱이 거래를 거부합니다")
    halt.add_argument("name")
    halt.add_argument("--reason", required=True, help="거래 중지 사유")

    resume = actions.add_parser("resume", help="킬 스위치 해제")
    resume.add_argument("name")
    return parser


_UNIVERSE_HELP = "거래할 종목, 쉼표나 공백으로 구분, 예: AAPL,MSFT,NVDA (krx는 005930 또는 삼성전자)"


def _universe(text: str, parser: argparse.ArgumentParser, console: Console) -> list[str]:
    try:
        universe, resolved = parse_universe(text)
    except ValueError as exc:
        parser.error(str(exc))
    if resolved:
        console.print(f"[dim]종목: {resolved}[/]")
    return universe


def _fund_dir(name: str, parser: argparse.ArgumentParser) -> Path:
    try:
        validate_fund_name(name)
    except ValueError as exc:
        parser.error(str(exc))
    directory = PAPER_DIR / name
    if not directory.is_dir():
        parser.error(f"{PAPER_DIR}에 {name!r} 모의투자 펀드가 없습니다(`aihf paper list`로 확인)")
    return directory


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------

def _backtest(args, parser: argparse.ArgumentParser, console: Console) -> None:
    universe = _universe(args.universe, parser, console)
    try:
        spec = load_spec(args.mandate)
    except ValueError as exc:
        parser.error(str(exc))
    end = args.end or completed_through()
    start = args.start or (_date.fromisoformat(end) - timedelta(weeks=BACKTEST_WEEKS)).isoformat()
    # A backtest blinds the investor agents' prompts (no ticker, industry or
    # calendar dates): the LLM may have been trained on what those companies
    # did over the window, and that memory would otherwise score as skill.
    fund = Fund(spec, blind=True)

    with open_data_client(FDClient) as raw:
        fd = CachedDataClient(raw)
        with console.status(
            f"[cyan]{spec.name}: {start} → {end} 백테스트 중 "
            f"({spec.cadence_label} 리밸런싱, 벤치마크 {spec.benchmark}) "
            f"종목 {', '.join(universe)}...",
            spinner="dots",
        ):
            result = backtest_fund(fund, start, end, fd, universe)

    payload = result.model_dump_json(indent=2)
    print(payload)
    saved = save_result(result)
    if args.out:
        Path(args.out).write_text(payload)
    m = result.metrics
    console.print(
        f"[bold]{spec.name}[/] {result.start} → {result.end}  ·  "
        f"세션 {len(result.dates)}개  ·  리밸런싱 {m.n_cycles}회  ·  주문 {m.n_orders}건  ·  "
        f"{cost_line(m.trading_cost, m.traded_notional)}  ·  "
        f"수익률 {m.total_return_pct:+.1%} (벤치마크 {spec.benchmark} {m.benchmark_return_pct:+.1%})  ·  "
        f"샤프 지수 {m.sharpe_ratio:.2f}  ·  최대 낙폭 {m.max_drawdown_pct:.1%}"
    )
    card = score(result.records)
    console.print(analyst_table(card))
    console.print(strategy_table(card))
    console.print(f"[dim]저장 위치: {saved}[/]")


# ---------------------------------------------------------------------------
# paper
# ---------------------------------------------------------------------------

def _paper(args, parser: argparse.ArgumentParser, console: Console) -> None:
    if args.action == "create":
        universe = _universe(args.universe, parser, console)
        try:
            validate_fund_name(args.name)
            spec = load_spec(args.mandate)
        except ValueError as exc:
            parser.error(str(exc))
        try:
            directory = deploy(args.name, spec, universe, root=PAPER_DIR)
        except FileExistsError as exc:
            parser.error(str(exc))
        cadence = f"{spec.cadence_label}, {spec.window.label}" if spec.is_intraday else spec.cadence_label
        console.print(
            f"[bold]{args.name}[/] 배포 완료: {directory}  ·  운용 지침 {spec.name}  ·  "
            f"{', '.join(universe)}  ·  {money(spec.capital, ',.0f')}  ·  {cadence}"
        )
        console.print(f"[dim]다음: 세션 하나 실행은 `aihf paper tick {args.name}`, 계속 실행은 `aihf paper run "
                      f"{args.name}`, 상태 확인은 언제든 `aihf paper status {args.name}`[/]")
        return

    if args.action == "list":
        for directory in list_deployed(PAPER_DIR):
            print(_status_line(directory))
        return

    directory = _fund_dir(args.name, parser)

    if args.action == "tick":
        deployed = load_deployed(directory)
        with open_data_client(FDClient) as raw:
            fd = CachedDataClient(raw, live=deployed.spec.is_intraday)
            verb = "마지막 세션 다시 실행 중" if args.again else "세션 하나 진행 중"
            with console.status(
                f"[cyan]{deployed.name}: {verb}, 종목 {', '.join(deployed.universe)}...", spinner="dots",
            ):
                record = (redo(directory, fd, build_fund=None) if args.again
                          else tick(directory, fd, session=args.session))
        print(record.model_dump_json(indent=2))
        console.print(_tick_summary(deployed.name, record))
        return

    if args.action == "run":
        deployed = load_deployed(directory)
        spec = deployed.spec
        cadence = f"{spec.cadence_label}, {spec.window.label}" if spec.is_intraday else spec.cadence_label
        console.print(f"[bold]{deployed.name}[/] 실행 중: {cadence}  ·  "
                      f"{', '.join(deployed.universe)}  ·  중지하려면 Ctrl-C 또는 `aihf paper stop {deployed.name}`")

        def _event(kind: str, fields: dict) -> None:
            detail = fields.get("reason") or (f"봉 {fields.get('bars_skipped')}개 건너뜀" if "bars_skipped" in fields else "")
            console.print(f"[yellow]{kind}[/] {fields.get('from') or '시작'} → {fields.get('to')}  {detail}".rstrip())

        with open_data_client(FDClient) as raw:
            try:
                serve(directory, CachedDataClient(raw, live=spec.is_intraday),
                      on_record=lambda record: console.print(_tick_summary(deployed.name, record)),
                      on_event=_event)
            except RuntimeError as exc:  # already running
                console.print(f"[red]{exc}[/]")
                sys.exit(1)
        console.print(f"[dim]{deployed.name} 중지됨[/]")
        return

    if args.action == "stop":
        if stop_runner(directory):
            console.print(f"[green]{args.name}[/] 중지를 요청했습니다. 진행 중인 세션을 마친 뒤 멈춥니다")
        else:
            console.print(f"[dim]{args.name}은(는) 실행 중이 아닙니다[/]")
        return

    if args.action == "status":
        print(_status_line(directory))
        ledger = Ledger(directory)
        latest = ledger.latest()
        if latest is not None and latest.decision is not None:
            when = "봉" if load_deployed(directory).spec.is_intraday else "종가"
            print(f"  대기 중인 결정, 다음 {when}에 체결:")
            for ticker, weight in sorted(latest.decision.final_weights.items()):
                print(f"    {ticker}: {weight:+.2%}")
        if latest is not None and latest.positions:
            print("  보유 현황:")
            for ticker, shares in sorted(latest.positions.items()):
                print(f"    {ticker}: {shares:+d} @ {latest.marks[ticker]:,.2f}")
        return

    if args.action == "scorecard":
        records = Ledger(directory).records()
        if not records:
            console.print(f"[dim]{args.name}에는 아직 기록된 세션이 없습니다[/]")
            return
        card = score(records)
        print(card.model_dump_json(indent=2))
        console.print(analyst_table(card))
        console.print(strategy_table(card))
        return

    ledger = Ledger(directory)
    if args.action == "halt":
        ledger.halt(args.reason)
        console.print(f"[red]{args.name} 거래 중지됨:[/] {args.reason}")
    elif args.action == "resume":
        ledger.resume()
        console.print(f"[green]{args.name} 재개됨[/]")


def _status_line(directory: Path) -> str:
    try:
        deployed = load_deployed(directory)
    except ValueError as exc:
        return f"{directory.name:<18} 불러올 수 없음: {exc}"
    ledger = Ledger(directory)
    latest = ledger.latest()
    halted = ledger.halted()
    if latest is None:
        state = f"시작 전  ·  {money(deployed.spec.capital, ',.0f')}"
    else:
        ret = latest.nav / deployed.spec.capital - 1
        state = (f"세션 {len(ledger.sessions())}개  ·  마지막 {latest.session}  ·  "
                 f"NAV {money(latest.nav, ',.2f')} ({ret:+.2%})")
    flag = f"  ·  거래 중지됨: {halted}" if halted else ""
    running = "  ·  실행 중" if is_running(directory) else ""
    return (f"{deployed.name:<18} {deployed.spec.name}  ·  {' '.join(deployed.universe)}  ·  "
            f"{deployed.spec.cadence_label}{running}  ·  {state}{flag}")


def _tick_summary(name: str, record: SessionRecord) -> str:
    parts = [f"[bold]{name}[/] · 세션 {record.session} · NAV {money(record.nav, ',.2f')}"]
    if record.executed is not None:
        costs = f", 비용 {money(record.executed.trading_cost, ',.2f')}" if record.executed.trading_cost else ""
        parts.append(f"{record.executed.as_of} 결정의 주문 {len(record.executed.orders)}건 체결{costs}")
    if record.decision is not None and record.decision.is_stopped_out:
        stop = next(c for c in record.decision.clamps if c.limit == "max_drawdown_pct")
        parts.append(f"낙폭 {stop.before:.1%}에서 손절, 다음 세션에 전량 매도")
    elif record.decision is not None:
        n_signals = sum(len(sr.signals) for sr in record.decision.strategies)
        when = "봉" if is_intraday(record.session) else "종가"
        parts.append(f"시그널 {n_signals}개, 한도 조정 {len(record.decision.clamps)}건으로 결정, 다음 {when}에 체결")
    if record.executed is None and record.decision is None:
        parts.append("보유 현황 평가, 리밸런싱 없음")
    return "  ·  ".join(parts)


if __name__ == "__main__":
    main()
