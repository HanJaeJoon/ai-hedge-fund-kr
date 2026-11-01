"""Validated fund configurations, strategy composition, and model construction."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, TypeAlias

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
    ValidationError,
)

from hedge_fund.brokers.costs import TradingCosts
from hedge_fund.risk.limits import RiskLimits
from hedge_fund.signals import ALPHA_MODEL_REGISTRY, LLMAgent, get_horizon_days, get_investment_approach
from hedge_fund.signals.base import AlphaModel

PortfolioMode: TypeAlias = Literal["long_only", "long_short", "dollar_neutral"]
Cadence: TypeAlias = Literal["minute", "daily", "weekly", "monthly"]

# The shortest interval a minute fund may run at: a session key is stamped
# to the second, so one second is the finest bar there is. A snapshot round
# trip is about a second per name, so at the fastest settings the runner
# will miss bars (logged as gaps) rather than fall behind its clock.
MIN_INTERVAL_SECONDS = 1

# US extended hours: the earliest and latest a trading window may reach.
EARLIEST_WINDOW = "04:00"
LATEST_WINDOW = "20:00"


def _clock_seconds(hhmm: str) -> int:
    """Seconds since midnight for an HH:MM string; ValueError otherwise."""
    try:
        hours, minutes = hhmm.split(":")
        h, m = int(hours), int(minutes)
    except (AttributeError, ValueError):
        raise ValueError(f"{hhmm!r}은(는) 올바른 시각(HH:MM)이 아닙니다") from None
    if not (0 <= h < 24 and 0 <= m < 60 and len(hours) == 2 and len(minutes) == 2):
        raise ValueError(f"{hhmm!r}은(는) 올바른 시각(HH:MM)이 아닙니다")
    return h * 3600 + m * 60


class TradingWindow(BaseModel):
    """When a minute fund trades, in New York time. Both ends are sessions:
    the window opens with a bar at `start` and always closes with one at `end`."""

    model_config = ConfigDict(extra="forbid")

    start: str = "09:30"
    end: str = "16:00"

    @field_validator("start", "end")
    @classmethod
    def _within_extended_hours(cls, value: str) -> str:
        seconds = _clock_seconds(value)
        if not _clock_seconds(EARLIEST_WINDOW) <= seconds <= _clock_seconds(LATEST_WINDOW):
            raise ValueError(f"{value}은(는) 미국 확장 거래 시간({EARLIEST_WINDOW}–{LATEST_WINDOW} ET)을 벗어납니다")
        return value

    @model_validator(mode="after")
    def _opens_before_it_closes(self) -> TradingWindow:
        if self.start_seconds >= self.end_seconds:
            raise ValueError(f"거래 시간은 시작이 종료보다 빨라야 합니다({self.start}–{self.end} ET)")
        return self

    @property
    def start_seconds(self) -> int:
        return _clock_seconds(self.start)

    @property
    def end_seconds(self) -> int:
        return _clock_seconds(self.end)

    @property
    def label(self) -> str:
        return f"{self.start}–{self.end} ET"


class ModelSpec(BaseModel):
    """One signal model in a strategy — an LLM agent or a quant model."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="key into ALPHA_MODEL_REGISTRY, e.g. 'buffett'")
    weight: float = Field(default=1.0, gt=0, description="blend weight")
    params: dict[str, Any] = Field(default_factory=dict, description="constructor kwargs for the model")

    @field_validator("name")
    @classmethod
    def _declared_profile(cls, name: str) -> str:
        get_investment_approach(name)
        get_horizon_days(name)
        return name


class BlendPolicy(BaseModel):
    """How a strategy's model views combine into one sleeve."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["conviction_weighted"] = "conviction_weighted"
    gross_target: float = Field(default=1.0, gt=0, description="desired sum of |weights| when views exist")
    mode: PortfolioMode


class StrategySpec(BaseModel):
    """A strategy ("pod"): signal models plus the policy that blends them.

    `weight` is the fund's capital slice for this strategy, relative to its
    siblings (normalized at netting time — 2/2 means the same as 1/1). In a
    library file (hedge_fund/strategies/) it stays at the default; slices are a
    fund-assembly decision, not a property of the strategy itself.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = Field(default=None, description="human-facing name, e.g. 'Deep Value'")
    weight: float = Field(default=1.0, gt=0)
    models: list[ModelSpec] = Field(min_length=1)
    blend: BlendPolicy

    @property
    def title(self) -> str:
        """Display name, falling back to a title-cased slug."""
        return self.display_name or self.name.replace("-", " ").title()

    @property
    def model_weights(self) -> dict[str, float]:
        """model_name -> blend weight, as portfolio construction consumes it."""
        return {m.name: m.weight for m in self.models}


class FundSpec(BaseModel):
    """A mandate with fund-level risk limits and no fixed ticker universe.

    Unknown fields are rejected. Risk limits apply to the combined portfolio;
    the ticker universe is supplied separately for each run.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    name: str
    strategies: list[StrategySpec] = Field(min_length=1)
    risk: RiskLimits
    costs: TradingCosts = Field(
        default_factory=TradingCosts,
        description="what every fill costs: half-spread per side and commission per share",
    )
    capital: float = Field(default=100_000.0, gt=0)
    rebalance: Cadence = Field(
        default="weekly",
        description="how often the fund decides: every bar (minute), or the first session of each day/week/month",
    )
    interval: float = Field(
        default=1.0, gt=0,
        description="minutes between sessions; read only when rebalance is minute",
    )
    window: TradingWindow = Field(
        default_factory=TradingWindow,
        description="trading hours in New York time; read only when rebalance is minute",
    )
    benchmark: str = Field(
        default="SPY",
        description="what the fund measures itself against; also the source " "of the backtest's trading-day grid",
    )

    @field_validator("benchmark")
    @classmethod
    def _uppercase_benchmark(cls, ticker: str) -> str:
        return ticker.upper()

    @field_validator("strategies")
    @classmethod
    def _unique_strategy_names(cls, strategies: list[StrategySpec]) -> list[StrategySpec]:
        names = [s.name for s in strategies]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"전략 이름이 중복됩니다: {sorted(duplicates)}")
        return strategies

    @model_validator(mode="after")
    def _interval_and_window_belong_to_minute_funds(self) -> FundSpec:
        if self.rebalance != "minute":
            stray = sorted({"interval", "window"} & self.model_fields_set)
            if stray:
                raise ValueError(f"{', '.join(stray)}: 분 단위 펀드에만 있는 항목입니다. 이 펀드는 {cadence_name(self.rebalance)} 리밸런싱입니다")
            return self
        if self.interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError(
                f"interval: 최소 {MIN_INTERVAL_SECONDS}초입니다. 세션은 초 단위로 기록됩니다"
            )
        if self.interval_seconds > self.window.end_seconds - self.window.start_seconds:
            raise ValueError(
                f"interval: {format_interval(self.interval_seconds)}은(는) 거래 시간 {self.window.label}보다 깁니다"
            )
        return self

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        """Daily/weekly/monthly mandates serialize exactly as they did before
        minute funds existed: `interval` and `window` mean nothing to them,
        and every ledger record carries a serialized spec in its hash. For
        the same reason a mandate at the default costs serializes without a
        `costs` block, as every mandate did before costs existed."""
        data = handler(self)
        if self.rebalance != "minute":
            data.pop("interval", None)
            data.pop("window", None)
        if self.costs == TradingCosts():
            data.pop("costs", None)
        return data

    @property
    def is_intraday(self) -> bool:
        return self.rebalance == "minute"

    @property
    def interval_seconds(self) -> int:
        """The bar length, to the second. Only meaningful for a minute fund."""
        return int(round(self.interval * 60))

    @property
    def cadence_label(self) -> str:
        """How the cadence reads: `매일`, or `30초마다` / `5분마다` / `1시간 30분마다`."""
        if not self.is_intraday:
            return cadence_name(self.rebalance)
        return f"{format_interval(self.interval_seconds)}마다"


def format_interval(seconds: int) -> str:
    """`30초`, `5분`, `1시간`, `1시간 30분`, `2분 30초` — the shortest honest spelling."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if seconds < 60:
        return f"{secs}초"
    parts = []
    if hours:
        parts.append(f"{hours}시간")
    if minutes:
        parts.append(f"{minutes}분")
    if secs:
        parts.append(f"{secs}초")
    return " ".join(parts)


CADENCE_NAMES: dict[str, str] = {"minute": "분 단위", "daily": "매일", "weekly": "매주", "monthly": "매월"}


def cadence_name(cadence: str) -> str:
    """The display name of a rebalance cadence value (`weekly` -> `매주`)."""
    return CADENCE_NAMES.get(cadence, cadence)


def normalize_universe(tickers: list[str]) -> list[str]:
    """Strip, uppercase, and deduplicate tickers while preserving their order.

    Raise ValueError if no nonempty tickers remain.
    """
    universe: list[str] = []
    for ticker in tickers:
        upper = ticker.strip().upper()
        if upper and upper not in universe:
            universe.append(upper)
    if not universe:
        raise ValueError("유니버스가 비어 있습니다. 실행하려면 종목이 하나 이상 필요합니다")
    return universe


def load_spec(path: str | Path) -> FundSpec:
    """Load a YAML mandate, raising ValueError with its path and error details."""
    data = _read_yaml(path)
    if "schema_version" not in data:
        raise ValueError(f"{path}: schema_version: 이전 형식의 펀드입니다. 다시 만들거나 설정을 업데이트해야 합니다.")
    try:
        return FundSpec.model_validate(data)
    except ValidationError as exc:
        raise ValueError(_validation_message(path, exc)) from exc


def load_strategy(path: str | Path) -> StrategySpec:
    """Load one strategy (a library file under hedge_fund/strategies/) from YAML."""
    try:
        return StrategySpec.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise ValueError(_validation_message(path, exc)) from exc


def _validation_message(path: str | Path, exc: ValidationError) -> str:
    errors = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
    return f"{path}: {errors}"


def _read_yaml(path: str | Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(Path(path).read_text())
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: 설정을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 설정은 YAML 매핑이어야 합니다")
    return data


def custom_strategy(names: list[str]) -> StrategySpec:
    """Build a strategy with its mode derived from registered analyst profiles.

    Duplicate names are removed in selection order. Any analyst that permits
    shorting enables long/short mode; dollar neutrality is never inferred.
    Invalid or empty selections raise ValueError.
    """
    models = [ModelSpec(name=n) for n in dict.fromkeys(names)]
    mode: PortfolioMode = "long_short" if any(get_investment_approach(m.name) == "long_short" for m in models) else "long_only"
    return StrategySpec(name="custom", models=models, blend=BlendPolicy(mode=mode))


class Fund:
    """A validated mandate with persistent model instances for each strategy.

    Models are constructed once so their caches survive successive cycles.
    Callers may supply instances keyed by strategy name, including test doubles.

    `blind=True` is for backtests: the LLM agents render prompts without
    the ticker, industry or calendar dates, so the result can't lean on
    what the model remembers about the company. Quant models are unaffected.
    """

    def __init__(
        self,
        spec: FundSpec,
        models: dict[str, list[AlphaModel]] | None = None,
        blind: bool = False,
    ) -> None:
        self.spec = spec
        self.strategies: list[tuple[StrategySpec, list[AlphaModel]]] = []
        for strategy in spec.strategies:
            if models is not None:
                self.strategies.append((strategy, models[strategy.name]))
                continue
            staff = []
            for m in strategy.models:
                if m.name not in ALPHA_MODEL_REGISTRY:
                    raise ValueError(f"전략 {strategy.name!r}에 알 수 없는 모델 {m.name!r}이(가) 있습니다. 사용 가능: {sorted(ALPHA_MODEL_REGISTRY)}")
                cls = ALPHA_MODEL_REGISTRY[m.name]
                params = {**m.params, "blind": True} if blind and issubclass(cls, LLMAgent) else m.params
                staff.append(cls(**params))
            self.strategies.append((strategy, staff))
