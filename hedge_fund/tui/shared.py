"""Presentation constants and renderables shared by the app and the CLI.

Textual-free by design: the interactive app (hedge_fund/tui/app.py) and the
non-interactive CLI (hedge_fund/run.py) both import from here, and the CLI must not
pay to import Textual. Everything here is plain data, small pure helpers, or
rich renderables (which Textual Statics render natively).
"""

from __future__ import annotations

from datetime import date as _date
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text

from hedge_fund.data.bars import is_intraday
from hedge_fund.data.krx import krx_selected
from hedge_fund.fund import FundSpec, PortfolioMode, StrategySpec
from hedge_fund.signals import ALPHA_MODEL_REGISTRY, get_investment_approach, LLMAgent

try:
    VERSION = _version("aihf")
except PackageNotFoundError:  # running from source without an install
    VERSION = "dev"

# Strategy libraries live in hedge_fund/strategies/ (code, inside the package).
# Mandates, paper funds and backtest results the app writes are user data
# and live in ~/.hedge-fund/ (see paths.py).
STRATEGY_DIR = Path(__file__).resolve().parent.parent / "strategies"

UNIVERSE_PRESETS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
                    "META", "TSLA", "JPM", "UNH", "XOM"]
KRX_UNIVERSE_PRESETS = ["005930", "000660", "035420", "005380", "035720",
                        "051910", "068270", "105560", "207940", "373220"]
# A new fund under krx: KODEX 200, won-sized capital, the korea.yaml costs (no per-share commission).
KRX_FUND_DEFAULTS = {"benchmark": "069500", "costs": {"spread_bps": 20, "commission_per_share": 0}}
KRX_DEFAULT_CAPITAL = 100_000_000.0

DISPLAY_NAMES = {
    "buffett": "Warren Buffett",
    "munger": "Charlie Munger",
    "graham": "Benjamin Graham",
    "lynch": "Peter Lynch",
    "wood": "Cathie Wood",
    "druckenmiller": "Stanley Druckenmiller",
    "pead": "실적 발표 후 드리프트",
}

SHORT_NAMES = {
    "buffett": "Buffett",
    "munger": "Munger",
    "graham": "Graham",
    "lynch": "Lynch",
    "wood": "Wood",
    "druckenmiller": "Druckenmiller",
    "pead": "PEAD",
}

MODE_LABELS: dict[PortfolioMode, str] = {"long_only": "롱온리", "long_short": "롱숏", "dollar_neutral": "달러 중립"}


def horizon_label(days: int) -> str:
    """`60일`, `1년`, `2년`: a horizon in trading days, as a desk says it."""
    if days % 252 == 0:
        return f"{days // 252}년"
    return f"{days}일"


# The LLM the investor agents reason with. Picked once, upfront; make_llm()
# reads HEDGE_FUND_LLM_MODEL (hedge_fund/llm/client.py) and routes to the right provider, so
# setting that env var steers every agent instance — the warm roster AND the
# Fund — with no threading. Quant models (PEAD) carry no LLM and ignore it.
DEFAULT_MODEL_LABEL = "Opus 5.5"

DEFAULT_RISK = {"max_position_pct": 0.25, "max_gross_exposure": 1.0, "max_drawdown_pct": 0.20}
DEFAULT_CAPITAL = 100_000.0
BACKTEST_WEEKS = 78  # ~18 months of history for the default backtest window
BOARD_REFRESH = 1 / 12  # seconds between run-board repaints — smooth, not frantic
WARM_CHUNK = 10      # dates per warm task — small enough that one stock still fans out

_CHART_HEIGHT = 8


def strategy_description(strategy: StrategySpec) -> str:
    """Explain permissions without treating every negative view as a short."""
    label = MODE_LABELS[strategy.blend.mode]
    short_names = [SHORT_NAMES.get(m.name, m.name) for m in strategy.models
                   if get_investment_approach(m.name) == "long_short"]
    if strategy.blend.mode == "long_only":
        return f"{label}: 주식을 매수하거나 현금으로 보유하며, 공매도는 하지 않습니다."
    prefix = ("롱과 숏의 금액 노출을 같게 맞춥니다(시장 위험 기준이 아님). "
              if strategy.blend.mode == "dollar_neutral" else "주식을 매수하고 공매도합니다. ")
    support = (f"숏 아이디어는 {', '.join(short_names)}의 지지가 있어야 합니다."
               if short_names else "숏 아이디어를 지지할 수 있는 애널리스트가 선택되지 않았습니다.")
    if len(short_names) < len(strategy.models):
        support += " 롱온리 애널리스트는 보유 의견만 냅니다."
    return f"{label}: {prefix}{support}"


def valid_date(text: str):
    try:
        _date.fromisoformat(text.strip())
        return True
    except ValueError:
        return "YYYY-MM-DD 형식으로 입력하세요."


def fund_label(spec: FundSpec) -> str:
    """One aligned line per fund: name, staff, cadence."""
    staff = ", ".join(f"{s.title} ({MODE_LABELS[s.blend.mode]})" for s in spec.strategies[:3])
    if len(spec.strategies) > 3:
        staff += ", …"
    return f"{spec.name}{' ' * max(0, 18 - cell_len(spec.name))} {staff} · {spec.cadence_label}"


def agent_names(spec: FundSpec) -> list[str]:
    """Unique model names across strategies, in first-appearance order."""
    names: list[str] = []
    for strategy in spec.strategies:
        for m in strategy.models:
            if m.name not in names:
                names.append(m.name)
    return names


def strategy_kind(strategy: StrategySpec) -> str:
    """Discretionary pods are staffed entirely by LLM agents; anything with a
    quant model in the mix is systematic. Derived, never declared."""
    if all(issubclass(ALPHA_MODEL_REGISTRY[m.name], LLMAgent)
           for m in strategy.models):
        return "discretionary"
    return "systematic"


def universe_presets() -> list[str]:
    return KRX_UNIVERSE_PRESETS if krx_selected() else UNIVERSE_PRESETS


def money(value: float, spec: str = ",.0f") -> str:
    """An amount in the data source's currency: `$1,234.50`, or `1,235원` under krx."""
    # shortcut: currency follows HEDGE_FUND_DATA, not the fund; store it on the spec if one process ever mixes markets.
    if krx_selected():
        return f"{value:{spec.replace('.2f', '.0f')}}원"
    return f"${value:{spec}}"


def _money(value: float) -> str:
    if krx_selected():
        return f"{value / 10_000:,.0f}만원" if abs(value) >= 100_000_000 else money(value)
    if abs(value) >= 10_000:
        return f"${value / 1000:,.0f}k"
    return f"${value:,.0f}"


def _render_chart(
    fund: list[float],
    benchmark: list[float],
    baseline: float,
    width: int,
) -> list[Text]:
    """Two-series unicode line chart, tearsheet-style: the fund against its
    benchmark on one set of axes, dollar labels in a left gutter. Lines are
    box-drawing polylines (asciichart-style), not filled areas — the gap
    between the two lines is the point. The fund draws last, so where the
    lines collide the fund wins the cell.
    """
    lo = min(min(fund), min(benchmark))
    hi = max(max(fund), max(benchmark))
    span = (hi - lo) or 1.0

    labels = {
        _CHART_HEIGHT - 1: _money(hi),
        _CHART_HEIGHT // 2: _money(lo + span * (_CHART_HEIGHT // 2) / (_CHART_HEIGHT - 1)),
        0: _money(lo),
    }
    gutter = max(len(label) for label in labels.values())
    plot_width = max(20, width - gutter - 1)

    def resample(values: list[float]) -> list[float]:
        if len(values) == 1:
            return values * plot_width
        step = (len(values) - 1) / (plot_width - 1)
        return [values[round(i * step)] for i in range(plot_width)]

    # grid[row][col] = (char, style); row 0 is the top
    grid = [[(" ", "") for _ in range(plot_width)] for _ in range(_CHART_HEIGHT)]

    def draw(values: list[float], style_of) -> None:
        cols = resample(values)
        level = [round((v - lo) / span * (_CHART_HEIGHT - 1)) for v in cols]
        for x in range(plot_width - 1):
            y0, y1 = level[x], level[x + 1]
            if y0 == y1:
                grid[_CHART_HEIGHT - 1 - y0][x] = ("─", style_of(cols[x]))
            else:
                # Rising: turn up (╯) at the low level, arrive (╭) at the high.
                # Falling: turn down (╮) at the high, arrive (╰) at the low.
                grid[_CHART_HEIGHT - 1 - y0][x] = (
                    "╯" if y1 > y0 else "╮", style_of(cols[x]))
                grid[_CHART_HEIGHT - 1 - y1][x] = (
                    "╭" if y1 > y0 else "╰", style_of(cols[x]))
                for y in range(min(y0, y1) + 1, max(y0, y1)):
                    grid[_CHART_HEIGHT - 1 - y][x] = ("│", style_of(cols[x]))
        grid[_CHART_HEIGHT - 1 - level[-1]][-1] = ("─", style_of(cols[-1]))

    draw(benchmark, lambda v: "cyan")
    draw(fund, lambda v: "bold green" if v >= baseline else "bold red")

    rows: list[Text] = []
    for row in range(_CHART_HEIGHT):
        level = _CHART_HEIGHT - 1 - row
        line = Text()
        if level in labels:
            line.append(f"{labels[level]:>{gutter}}┤", style="dim")
        else:
            line.append(f"{'':>{gutter}}│", style="dim")
        for char, style in grid[row]:
            line.append(char, style=style)
        rows.append(line)
    return rows


_BLOCKS = " ▁▂▃▄▅▆▇█"


def render_area_chart(
    fund: list[float],
    benchmark: list[float],
    baseline: float,
    width: int,
) -> list[Text]:
    """Filled-area equity curve: the fund as a solid mass of eighth-blocks —
    green above its starting capital, red below — with the benchmark threaded
    through it as a cyan polyline. The fill is the fund's P&L made physical;
    the line is the market it has to beat.
    """
    lo = min(min(fund), min(benchmark), baseline)
    hi = max(max(fund), max(benchmark), baseline)
    # Drop the floor a touch below the true low: the worst column still draws
    # a sliver of fill instead of vanishing into a hole in the mass.
    lo -= ((hi - lo) or 1.0) * 0.06
    span = (hi - lo) or 1.0

    labels = {
        _CHART_HEIGHT - 1: _money(hi),
        _CHART_HEIGHT // 2: _money(lo + span * (_CHART_HEIGHT // 2) / (_CHART_HEIGHT - 1)),
        0: _money(lo),
    }
    gutter = max(len(label) for label in labels.values())
    plot_width = max(20, width - gutter - 1)

    def resample(values: list[float]) -> list[float]:
        """Linear interpolation, not nearest-neighbor: a handful of rebalance
        cycles stretched over the full width should read as slopes, not slabs."""
        if len(values) == 1:
            return values * plot_width
        step = (len(values) - 1) / (plot_width - 1)
        out = []
        for i in range(plot_width):
            pos = i * step
            j = int(pos)
            k = min(j + 1, len(values) - 1)
            frac = pos - j
            out.append(values[j] * (1 - frac) + values[k] * frac)
        return out

    # The fund, as filled columns measured in 1/8 blocks from the floor.
    cols = resample(fund)
    eighths = [(v - lo) / span * _CHART_HEIGHT * 8 for v in cols]
    grid = [[(" ", "") for _ in range(plot_width)] for _ in range(_CHART_HEIGHT)]
    for x, (v, lvl) in enumerate(zip(cols, eighths)):
        style = "green" if v >= baseline else "red"
        for row in range(_CHART_HEIGHT):  # row 0 is the top
            filled = max(0, min(8, round(lvl - (_CHART_HEIGHT - 1 - row) * 8)))
            if filled:
                grid[row][x] = (_BLOCKS[filled], style)

    # The benchmark, as a polyline punched through the fill.
    bench = resample(benchmark)
    level = [round((v - lo) / span * (_CHART_HEIGHT - 1)) for v in bench]
    for x in range(plot_width - 1):
        y0, y1 = level[x], level[x + 1]
        if y0 == y1:
            grid[_CHART_HEIGHT - 1 - y0][x] = ("─", "cyan")
        else:
            grid[_CHART_HEIGHT - 1 - y0][x] = ("╯" if y1 > y0 else "╮", "cyan")
            grid[_CHART_HEIGHT - 1 - y1][x] = ("╭" if y1 > y0 else "╰", "cyan")
            for y in range(min(y0, y1) + 1, max(y0, y1)):
                grid[_CHART_HEIGHT - 1 - y][x] = ("│", "cyan")
    grid[_CHART_HEIGHT - 1 - level[-1]][-1] = ("─", "cyan")

    rows: list[Text] = []
    for row in range(_CHART_HEIGHT):
        lvl = _CHART_HEIGHT - 1 - row
        line = Text()
        if lvl in labels:
            line.append(f"{labels[lvl]:>{gutter}}┤", style="dim")
        else:
            line.append(f"{'':>{gutter}}│", style="dim")
        for char, style in grid[row]:
            line.append(char, style=style)
        rows.append(line)
    return rows


def cadence_identity(spec: FundSpec) -> str:
    """How a fund's cadence reads on one line: `매주`, or `30초마다 · 09:30–16:00 ET`."""
    if not spec.is_intraday:
        return spec.cadence_label
    return f"{spec.cadence_label} · {spec.window.label}"


def session_label(session: str) -> str:
    """A session for a human: a date as it is, a bar as `2024-06-04 13:05:00 ET`."""
    if is_intraday(session):
        return f"{session[:10]} {session[11:]} ET"
    return session
