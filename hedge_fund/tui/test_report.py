"""Pure renderers: verdicts, the live desk, signal and portfolio details."""

import pytest

from hedge_fund.brokers.models import Fill, Order
from hedge_fund.models import Signal
from hedge_fund.tui import report
from hedge_fund.pipeline.stages import stop_out_decision
from hedge_fund.tui.fixtures import make_cycle, make_session, make_signal, make_spec, render


LABEL = {"bullish": "강세", "bearish": "약세", "neutral": "중립"}


@pytest.mark.parametrize("direction,strength", [("bullish", 3.2), ("bearish", 1.6), ("neutral", 4), ("bullish", 0), ("bearish", 0)])
def test_jev_results_show_stored_direction_and_separate_confidence(direction, strength):
    signal = make_signal(direction, strength)
    detail = render(report.signal_detail(make_cycle(signal), 0, 0))
    assert LABEL[direction] in detail
    assert "투자 확신도" in detail
    assert "No written thesis generated." in detail
    assert "Jev 답변 선택지별 확률" in detail
    assert "투자 수익률이 아닙니다" in detail
    assert "Jev 자체 답변 확신도" in detail
    assert "방향: 60.0%" in detail
    assert "강세 강도: 40.0%" in detail
    assert "약세 강도: 40.0%" in detail
    assert f"{LABEL[direction]} 80.0%" in detail
    cached = signal.model_copy(deep=True)
    cached.metadata["cached"] = True
    assert render(report.signal_detail(make_cycle(cached), 0, 0)) == detail
    desk = report.Desk("Buffett")
    desk.begin("TEST")
    assert report._live_verdict(desk).plain == "생각 중"
    assert report._live_thesis(desk).plain == ""
    desk.settle(signal)
    assert LABEL[direction] in report._live_verdict(desk).plain
    assert report._live_thesis(desk).plain == signal.reasoning
    verdict = report.verdict(signal)
    nav = report.report_nav(make_cycle(signal))
    option = next(option for option in nav if option.id == "sig:0:0")
    assert verdict[0] in render(option.prompt)


def test_abstention_overrides_stored_direction():
    signal = Signal(model_name="buffett", ticker="TEST", date="2025-01-15", value=0, reasoning="abstained: TypeSafe returned HTTP 401", metadata={"abstained": True, "signal": "bullish"})
    text = render(report.signal_detail(make_cycle(signal), 0, 0))
    assert "기권" in text and "HTTP 401" in text
    assert "Jev 자체 답변 확신도" not in text


def test_chat_rendering_and_quantitative_fallback_are_preserved():
    chat = Signal(model_name="buffett", ticker="TEST", date="2025-01-15", value=0.8, reasoning="A durable business.", metadata={"signal": "bullish", "confidence": 80.0})
    text = render(report.signal_detail(make_cycle(chat), 0, 0))
    assert "확신도 80%" in text and "시그널 값 +0.80" in text
    assert "A durable business." in text and "Jev" not in text
    desk = report.Desk("Buffett")
    desk.begin("TEST")
    desk.feed('{"signal":"bullish","confidence":80,"reasoning":"A durable business."}')
    assert "강세 80%" in report._live_verdict(desk).plain
    assert "A durable business." in report._live_thesis(desk).plain
    for value, direction in [(0.5, "강세"), (-0.5, "약세"), (0, "중립")]:
        quant = Signal(model_name="pead", ticker="TEST", date="2025-01-15", value=value)
        assert report.verdict(quant)[1] == direction
        desk.settle(quant)
        assert report._live_thesis(desk).plain == ""


def test_portfolio_report_explains_rounding_scaling_and_flat_strategies():
    record = make_cycle(make_signal())
    record.spec.strategies[0].blend.mode = "dollar_neutral"
    record.equity_before = record.nav = 10_000
    record.final_weights = {"A": .5, "B": -.5}
    record.positions = {"A": 16, "B": -7}
    record.marks = {"A": 300, "B": 700}
    record.risk_scale_factor = .5
    text = render(report.portfolio_detail(record))
    assert "목표 순노출 $+0.00" in text
    assert "실제 순노출 $-100.00" in text
    assert "차이 $-100.00" in text
    assert "요청 노출의 50.0%로 줄였습니다" in text
    assert "주식 수가 정수라 실제 보유는 다를 수 있고" in text
    record.positions = {}
    record.final_weights = {"A": 0, "B": 0}
    record.strategies[0].flat_reason = "missing_short_side"
    text = render(report.portfolio_detail(record))
    assert "롱을 상쇄할 편입 가능한 숏 없음" in text
    assert "배정된 자본은 쓰지 않습니다" in text
    assert "보유 포지션 없음" in text


def test_orders_report_shows_the_fill_beside_the_mark_and_what_it_cost():
    record = make_cycle(make_signal())
    record.orders = [Order(ticker="TEST", side="buy", quantity=100, price=100.0)]
    record.fills = [Fill(ticker="TEST", side="buy", quantity=100, price=100.05)]
    record.cash_before, record.cash = 100_000, 100_000 - 100 * 100.05 - 0.50
    text = render(report.session_detail(_session_with(record), "x:sec:orders"))
    assert "평가가" in text and "체결가" in text
    assert "$100.00" in text and "$100.05" in text
    assert "슬리피지 $5.00 · 수수료 $0.50" in text
    record.fills = [Fill(ticker="TEST", side="buy", quantity=100, price=100.0)]
    record.cash = 100_000 - 100 * 100.0
    assert "슬리피지" not in render(report.session_detail(_session_with(record), "x:sec:orders"))


def test_cost_line_reads_as_bps_of_what_was_traded():
    assert report.cost_line(312.0, 375_000.0) == "비용 $312 · 거래대금 $375,000의 8.3 bps"
    assert report.cost_line(0.0, 0.0) == "거래 없음"


def _session_with(record):
    return make_session(record.as_of, executed=record)


def test_a_stop_out_reads_as_one_in_the_row_and_the_overview():
    spec = make_spec().model_copy(update={"risk": make_spec().risk.model_copy(update={"max_drawdown_pct": 0.2})})
    decision = stop_out_decision(spec, "2025-01-15", ["TEST"], {"TEST": 80.0}, {"TEST": 100}, 0.213)
    session = make_session("2025-01-15", decision=decision, positions={"TEST": 100}, marks={"TEST": 80.0}, nav=80_000)
    assert "손절" in render(report.session_row(session, 100_000))
    text = render(report.session_overview(session))
    assert "손절" in text and "낙폭 21.3%가 한도 20.0%를 넘음" in text
