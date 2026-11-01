"""Paper trading rows and plans, rendered from a fund directory without a screen."""

import os
from datetime import datetime, timedelta

from hedge_fund import paths
from hedge_fund.data.sessions import NEW_YORK
from hedge_fund.paper import deploy, Ledger, PID_FILE
from hedge_fund.tui import paper_view, report
from hedge_fund.tui.fixtures import (
    executed_and_decided,
    make_minute_spec,
    make_pending,
    make_quote,
    make_session,
    make_spec,
    make_state,
    render,
)
from hedge_fund.tui.palette import MUTED


def test_run_plan_says_exactly_what_the_run_will_do():
    spec = make_spec(["pead"], "alpha")  # weekly by default
    # Pending decision, and the due session opens a new week: execute, then decide.
    plan = paper_view.run_plan("alpha", make_state(last_session="2025-01-17", pending=make_pending()), "2025-01-21", spec).plain
    assert "2025-01-17 결정 실행: 2025-01-21 종가" in plan
    assert "AAPL +25%  ·  MSFT +25%" in plan and "XOM" not in plan
    assert "비중 산정 전 분석 갱신" in plan
    assert "새 결정" in plan and "주의 첫 세션" in plan
    # Pending decision mid-week: execute, then only mark the book.
    plan = paper_view.run_plan("alpha", make_state(last_session="2025-01-20", pending=make_pending("2025-01-20")), "2025-01-21", spec).plain
    assert "2025-01-20 결정 실행" in plan and "그다음 보유 현황 평가." in plan
    assert "새 결정" not in plan
    # Nothing pending, mid-week: a mark only.
    plan = paper_view.run_plan("alpha", make_state(last_session="2025-01-20"), "2025-01-21", spec).plain
    assert plan.startswith("보유 현황 평가: 2025-01-21 종가.  거래 없음.")
    assert "결정" not in plan
    # A brand-new fund: mark, then its first decision.
    plan = paper_view.run_plan("alpha", make_state(), "2025-01-21", spec).plain
    assert "보유 현황 평가: 2025-01-21 종가" in plan and "첫 결정" in plan
    # A pending decision that went flat says so rather than listing nothing,
    # and says why when the blender recorded a reason: a dollar-neutral desk
    # with conviction but no short to balance it is not "no conviction".
    flat = make_pending("2025-01-20", {})
    plan = paper_view.run_plan("alpha", make_state(last_session="2025-01-20", pending=flat), "2025-01-21", spec).plain
    assert "포지션 없음: 기준을 넘은 확신도 없음" in plan
    flat.strategies[0].flat_reason = "missing_short_side"
    plan = paper_view.run_plan("alpha", make_state(last_session="2025-01-20", pending=flat), "2025-01-21", spec).plain
    assert "포지션 없음: 롱을 상쇄할 편입 가능한 숏 없음" in plan
    overview = render(report.session_overview(make_session("2025-01-20", decision=flat)))
    assert "포지션 없음: 롱을 상쇄할 편입 가능한 숏 없음" in overview
    # Nothing due: when the next close is, Friday rolling to Monday.
    plan = paper_view.run_plan("alpha", make_state(last_session="2025-01-17"), None, spec).plain
    assert plan.startswith("2025-01-17까지 최신 상태입니다.  다음 세션: 2025-01-20 종가.")
    assert "2025-01-17 세션을 다시 실행할까요?" in plan and "기록된 세션은 교체" in plan
    friday = datetime(2025, 1, 17, 18, 0, tzinfo=NEW_YORK)
    assert paper_view.run_plan("alpha", make_state(), None, spec, now=friday).plain == (
        "아직 실행할 완료된 SPY 세션이 없습니다.  다음: 2025-01-20 종가.")


def test_run_plan_on_a_bar_names_the_print_and_how_late_it_is_taken():
    spec = make_minute_spec()  # every 5m, 13:00–13:10
    at = lambda h, m, s=0: datetime(2025, 1, 21, h, m, s, tzinfo=NEW_YORK)  # noqa: E731
    state = make_state(last_session="2025-01-21T13:00:00", pending=make_pending("2025-01-21T13:00:00"))
    plan = paper_view.run_plan("fast", state, "2025-01-21T13:05:00", spec, now=at(13, 5, 20)).plain
    assert "2025-01-21 13:00:00 ET 결정 실행: 2025-01-21 13:05:00 ET 체결가, 지금 조회\n" in plan
    assert "봉 이후" not in plan and "새 결정  (매 봉)" in plan
    late = paper_view.run_plan("fast", state, "2025-01-21T13:05:00", spec, now=at(13, 7, 30)).plain
    assert "체결가, 지금 조회 (봉 이후 2분)" in late
    plan = paper_view.run_plan("fast", make_state(), "2025-01-21T13:00:00", spec, now=at(13, 0, 5)).plain
    assert plan.startswith("보유 현황 평가: 2025-01-21 13:00:00 ET 체결가, 지금 조회.  거래 없음.")
    # Nothing due between bars, and before the first bar of the day.
    plan = paper_view.run_plan("fast", make_state(last_session="2025-01-21T13:05:00"), None, spec, now=at(13, 7)).plain
    assert plan.startswith("2025-01-21 13:05:00 ET까지 최신 상태입니다.  다음 세션: 13:10:00 ET.")
    assert "그때 본 체결가로" in plan
    assert paper_view.run_plan("fast", make_state(), None, spec, now=at(12, 0)).plain == (
        "아직 실행할 봉이 없습니다.  다음: 13:00:00 ET.")


def test_live_rows_value_the_book_at_the_latest_prints():
    spec = make_spec(["pead"], "alpha")
    directory = deploy("alpha", spec, ["TEST"], root=paths.PAPER_DIR)
    ledger = Ledger(directory)
    record, _ = executed_and_decided()
    ledger.append(make_session("2025-01-20", executed=record, nav=101000, positions={"TEST": 10}, marks={"TEST": 100}))
    snap = paper_view.PaperSnapshot(directory)
    # A daily fund has the same next-run row as a minute fund: when, and whether a runner will take it.
    rows = dict(paper_view.next_run_rows(snap, now=datetime(2025, 1, 21, 12, 0, tzinfo=NEW_YORK)))
    assert render(rows["다음 종가"]).strip() == "2025-01-21   ○ 대기 · space로 러너 시작"
    assert "○ 대기" in render(snap.badge())  # a session recorded, no runner up
    # Every held name needs a print; cash alone has nothing live to say.
    assert paper_view.live_rows(snap, {}, today="2025-01-21") == []
    label, line = paper_view.live_rows(snap, {"TEST": make_quote("TEST", 110)}, today="2025-01-21")[0]
    text = render(line)
    assert label == "실시간" and "$101,100" in text and "+0.10%" in text and "10:05 ET 기준" in text
    # A print from before today is shown, but muted and dated: not a live gain.
    _, line = paper_view.live_rows(snap, {"TEST": make_quote("TEST", 110, "2025-01-20T21:00:00Z")}, today="2025-01-21")[0]
    assert "1월 20일 16:00 ET 기준" in render(line)
    assert all(span.style == MUTED for span in line.spans)
    assert "실시간" in render(paper_view.paper_detail(snap, quotes={"TEST": make_quote("TEST", 110)}))
    assert "실시간" not in render(paper_view.paper_detail(snap))


def test_minute_fund_rows_are_pure(monkeypatch):
    directory = deploy("fast", make_minute_spec(), ["TEST"], root=paths.PAPER_DIR)
    ledger = Ledger(directory)
    noon = datetime(2025, 1, 21, 12, 0, tzinfo=NEW_YORK)
    snap = paper_view.PaperSnapshot(directory)
    assert not snap.running and snap.intraday
    rows = dict(paper_view.next_run_rows(snap, now=noon))
    assert render(rows["다음 봉"]).strip() == "13:00:00 ET   ○ 대기 · space로 러너 시작"
    assert rows["먼저"] == "첫 봉의 체결가로 보유 현황 평가" and rows["그다음"] == "첫 결정"
    assert paper_view.today_rows(snap, now=noon) == []
    assert "● 신규" in render(paper_view.paper_slot(0, snap)) and "● 신규" in render(snap.badge())
    assert render(snap.status()).startswith("● 신규  아직 세션 없음")

    record, decided = executed_and_decided()
    first = make_session("2025-01-21T13:00:00", decision=decided, nav=100000)
    second = make_session("2025-01-21T13:05:00", executed=record, nav=101000, positions={"TEST": 10},
                      marks={"TEST": 100}, prev_hash=first.hash)
    ledger.append(first)
    ledger.append(second)
    (directory / PID_FILE).write_text(str(os.getpid()))
    snap = paper_view.PaperSnapshot(directory)
    assert snap.running
    rows = dict(paper_view.next_run_rows(snap, now=noon.replace(hour=13, minute=6)))
    assert render(rows["다음 봉"]).strip() == "13:10:00 ET   ● 실행 중 · space로 중지"
    assert rows["그다음"] == "새 결정"
    # After the day's last bar the next one is tomorrow's open, named by weekday.
    assert render(dict(paper_view.next_run_rows(snap, now=noon.replace(hour=15)))["다음 봉"]).startswith("수요일 13:00:00 ET")
    today = dict(paper_view.today_rows(snap, now=noon.replace(hour=13, minute=6)))
    assert render(today["봉"]).strip() == "2 / 3   마지막 13:05:00 ET"
    assert render(today["시가 대비"]).strip() == "+1.00%   체결 1건"
    assert paper_view.today_rows(snap, now=noon + timedelta(days=1)) == []  # yesterday's bars are not today's
    detail = render(paper_view.paper_detail(snap, now=noon.replace(hour=13, minute=6)))
    assert "5분마다 · 13:00–13:10 ET" in detail and "오늘" in detail and "● 실행 중" in detail
    assert "세션" in detail and "2025-01-21T13:05:00" in detail
    assert "+1.0%  ● 실행 중" in render(paper_view.paper_slot(0, snap))
    assert snap.metrics().n_cycles == 1  # bar keys do not trip the metrics' date math
