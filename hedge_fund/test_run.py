"""The CLI: configuration preflight, the backtest command, and the paper flow."""

import json
import sys
from datetime import datetime
from unittest.mock import Mock

import pytest
import yaml

from hedge_fund import paths, run
from hedge_fund.data import sessions
from hedge_fund.fund import custom_strategy, FundSpec


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """No credentials, no HTTP, user dirs under tmp, fakes for the engine."""
    from hedge_fund.backtesting.test_fund import FakeAnalyst, FakeDataClient
    from hedge_fund.fund import Fund
    import importlib
    tick_module = importlib.import_module("hedge_fund.paper.tick")

    class OfflineClient(FakeDataClient):
        def __init__(self):
            super().__init__({ticker: {"2025-01-06": 100, "2025-01-07": 100, "2025-01-08": 110, "2025-01-13": 110}
                              for ticker in ("AAPL", "MSFT", "SPY")})
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def build_fund(spec, blind=False):
        return Fund(spec, models={s.name: [FakeAnalyst(m.name, {"AAPL": .8, "MSFT": -.6}) for m in s.models]
                                  for s in spec.strategies})

    monkeypatch.setattr(run, "apply_credentials", lambda: None)
    monkeypatch.setattr(run, "ensure_mandates_dir", lambda: tmp_path)
    monkeypatch.setattr(run, "Fund", build_fund)
    monkeypatch.setattr(tick_module, "Fund", build_fund)
    monkeypatch.setattr(run, "FDClient", OfflineClient)
    monkeypatch.setattr(run, "CachedDataClient", lambda client, **kwargs: client)
    monkeypatch.setattr(run, "PAPER_DIR", tmp_path / "paper")
    monkeypatch.setattr(paths, "RESEARCH_DIR", tmp_path / "research")

    def set_today(day):
        y, m, d = map(int, day.split("-"))
        class Clock:
            @staticmethod
            def now(tz):
                return datetime(y, m, d, 12, tzinfo=tz)
        monkeypatch.setattr(sessions, "datetime", Clock)
    set_today("2025-01-14")
    return set_today


def _mandate(tmp_path, mode="long_short", name="mixed"):
    spec = FundSpec(schema_version=2, name=name, strategies=[custom_strategy(["buffett", "druckenmiller"])],
                    risk={"max_position_pct": .25, "max_gross_exposure": 1})
    spec.strategies[0].blend.mode = mode
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(spec.model_dump()))
    return path


def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["aihf", *argv])
    run.main()


@pytest.mark.parametrize("command", ["backtest", "create"])
@pytest.mark.parametrize("unversioned", [False, True])
def test_cli_rejects_invalid_configuration_before_clients(tmp_path, monkeypatch, capsys, command, unversioned):
    spec = FundSpec(schema_version=2, name="mixed", strategies=[custom_strategy(["buffett", "druckenmiller"])],
                    risk={"max_position_pct": .25, "max_gross_exposure": 1})
    data = spec.model_dump()
    if unversioned:
        data.pop("schema_version")
    else:
        data["strategies"][0]["blend"]["mode"] = "invalid"
    path = tmp_path / "fund.yaml"
    path.write_text(yaml.safe_dump(data))
    original = path.read_bytes()
    forbidden = Mock(side_effect=AssertionError("external activity"))
    monkeypatch.setattr(run, "apply_credentials", lambda: None)
    monkeypatch.setattr(run, "ensure_mandates_dir", lambda: tmp_path)
    monkeypatch.setattr(run, "Fund", forbidden)
    monkeypatch.setattr(run, "FDClient", forbidden)
    monkeypatch.setattr(run, "deploy", forbidden)
    monkeypatch.setattr(run, "PAPER_DIR", tmp_path / "paper")
    argv = (["backtest", str(path), "--universe", "AAPL"] if command == "backtest"
            else ["paper", "create", "alpha", "--mandate", str(path), "--universe", "AAPL"])
    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, *argv)
    assert exc.value.code == 2
    assert forbidden.call_count == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert ("이전 형식" if unversioned else "blend.mode") in output.err
    assert path.read_bytes() == original
    assert not (tmp_path / "paper").exists()


def test_no_arguments_launches_the_app(monkeypatch):
    launched = Mock()
    monkeypatch.setattr(run, "apply_credentials", lambda: None)
    monkeypatch.setattr(run, "ensure_mandates_dir", lambda: None)
    monkeypatch.setitem(sys.modules, "hedge_fund.tui.app", Mock(HedgeFundApp=lambda: launched))
    _main(monkeypatch)
    launched.run.assert_called_once()


@pytest.mark.parametrize("mode", ["long_only", "long_short", "dollar_neutral"])
def test_backtest_prints_json_and_files_a_research_copy(tmp_path, monkeypatch, capsys, offline, mode):
    path = _mandate(tmp_path, mode)
    _main(monkeypatch, "backtest", str(path), "--universe", "AAPL,MSFT", "--start", "2025-01-06", "--end", "2025-01-13")
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["dates"] == ["2025-01-06", "2025-01-07", "2025-01-08", "2025-01-13"]
    # Monday decides, Tuesday executes; the next Monday decides again.
    assert [r["executed"] is not None for r in result["records"]] == [False, True, False, False]
    assert result["records"][-1]["decision"] is not None
    cycle = result["records"][1]["executed"]
    assert cycle["positions"]["AAPL"] > 0
    assert (cycle["positions"].get("MSFT", 0) < 0) == (mode != "long_only")
    if mode == "dollar_neutral":
        assert sum(cycle["final_weights"].values()) == pytest.approx(0)
    assert "리밸런싱 1회" in output.err and "세션" in output.err
    saved = list((tmp_path / "research").glob("mixed-2025-01-06-2025-01-13-*.json"))
    assert len(saved) == 1 and json.loads(saved[0].read_text()) == result
    assert not (tmp_path / "paper").exists()  # research never touches the paper funds


def test_backtest_defaults_end_to_the_latest_completed_session(tmp_path, monkeypatch, capsys, offline):
    path = _mandate(tmp_path)
    _main(monkeypatch, "backtest", str(path), "--universe", "AAPL", "--start", "2025-01-06")
    result = json.loads(capsys.readouterr().out)
    assert result["end"] == "2025-01-13"


def test_paper_flow_create_tick_status_halt_resume(tmp_path, monkeypatch, capsys, offline):
    set_today = offline
    path = _mandate(tmp_path)

    set_today("2025-01-07")  # deployed Tuesday morning: Monday 01-06 is the latest close
    _main(monkeypatch, "paper", "create", "alpha", "--mandate", str(path), "--universe", "AAPL, MSFT")
    err = capsys.readouterr().err
    assert "alpha" in err and "배포 완료" in err
    with pytest.raises(SystemExit):
        _main(monkeypatch, "paper", "create", "alpha", "--mandate", str(path), "--universe", "AAPL")
    assert "이미 있습니다" in capsys.readouterr().err

    _main(monkeypatch, "paper", "list")
    listed = capsys.readouterr().out
    assert "alpha" in listed and "시작 전" in listed

    _main(monkeypatch, "paper", "tick", "alpha")
    output = capsys.readouterr()
    record = json.loads(output.out)
    assert record["session"] == "2025-01-06" and record["decision"] is not None and record["executed"] is None
    assert "결정" in output.err and "다음 종가에 체결" in output.err

    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, "paper", "tick", "alpha")  # nothing new has closed
    assert exc.value.code == 1
    assert "2025-01-06 이후 완료된 SPY 세션이 없습니다" in capsys.readouterr().err

    set_today("2025-01-08")
    _main(monkeypatch, "paper", "tick", "alpha")
    output = capsys.readouterr()
    record = json.loads(output.out)
    assert record["session"] == "2025-01-07" and record["executed"]["positions"]["AAPL"] > 0
    assert "체결" in output.err

    _main(monkeypatch, "paper", "status", "alpha")
    status = capsys.readouterr().out
    assert "alpha" in status and "세션 2개" in status and "마지막 2025-01-07" in status
    assert "AAPL:" in status and "보유 현황:" in status and "거래 중지됨" not in status

    _main(monkeypatch, "paper", "halt", "alpha", "--reason", "operator review")
    _main(monkeypatch, "paper", "status", "alpha")
    assert "거래 중지됨: operator review" in capsys.readouterr().out
    set_today("2025-01-09")
    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, "paper", "tick", "alpha")
    assert exc.value.code == 1 and "거래 중지됨" in capsys.readouterr().err

    _main(monkeypatch, "paper", "resume", "alpha")
    _main(monkeypatch, "paper", "tick", "alpha")
    assert json.loads(capsys.readouterr().out)["session"] == "2025-01-08"
    _main(monkeypatch, "paper", "list")
    assert "세션 3개" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, "paper", "status", "nobody")
    assert exc.value.code == 2 and "'nobody' 모의투자 펀드가 없습니다" in capsys.readouterr().err


def test_paper_minute_fund_ticks_and_runs_like_a_daily_one(tmp_path, monkeypatch, capsys, offline):
    """The CLI verbs are the same at every cadence: `tick` runs the session
    that is due (for a minute fund, the current bar at the print taken now)
    and `run` leaves a runner on the fund."""
    import importlib
    import os
    from hedge_fund.data.sessions import NEW_YORK
    from hedge_fund.paper import PID_FILE
    from hedge_fund.pipeline.test_clock import Tape
    spec = FundSpec(schema_version=2, name="bars", strategies=[custom_strategy(["buffett"])],
                    risk={"max_position_pct": .25, "max_gross_exposure": 1},
                    rebalance="minute", interval=0.5, window={"start": "09:30", "end": "12:00"})
    path = tmp_path / "bars.yaml"
    path.write_text(yaml.safe_dump(spec.model_dump()))

    _main(monkeypatch, "paper", "create", "fast", "--mandate", str(path), "--universe", "AAPL")
    err = " ".join(capsys.readouterr().err.split())  # the console wraps; read it as one line
    assert "30초마다" in err and "09:30–12:00 ET" in err
    assert "세션 하나 실행은 `aihf paper tick fast`" in err and "계속 실행은 `aihf paper run fast`" in err

    class CliTape(Tape):
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    tape = CliTape({"AAPL": {"2025-01-07T09:30:00": 100.0, "2025-01-07T09:30:30": 101.0},
                    "SPY": {"2025-01-07T09:30:00": 500.0}},
                   now=datetime(2025, 1, 7, 9, 30, 40, tzinfo=NEW_YORK))  # 10s into the 09:30:30 bar
    monkeypatch.setattr(run, "FDClient", lambda: tape)
    tick_module = importlib.import_module("hedge_fund.paper.tick")
    class Clock(datetime):  # the tick's wall clock is the tape's; everything else is datetime's
        @classmethod
        def now(cls, tz=None):
            return tape.now.astimezone(tz)
    monkeypatch.setattr(tick_module, "datetime", Clock)

    _main(monkeypatch, "paper", "tick", "fast")
    output = capsys.readouterr()
    record = json.loads(output.out)
    assert record["session"] == "2025-01-07T09:30:30" and record["decision"] is not None
    assert "결정" in output.err and "다음 봉에 체결" in output.err

    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, "paper", "tick", "fast")  # no new bar yet
    assert exc.value.code == 1
    assert "2025-01-07T09:30:30 이후 봉이 없습니다. 다음: 09:31:00 ET" in capsys.readouterr().err

    _main(monkeypatch, "paper", "stop", "fast")
    assert "실행 중이 아닙니다" in capsys.readouterr().err
    _main(monkeypatch, "paper", "list")
    listed = capsys.readouterr().out
    assert "30초마다" in listed and "실행 중" not in listed

    # `run` is the serve loop; stand in for it and check the wiring around it.
    seen = {}
    def fake_serve(directory, data, *, on_record, on_event):
        seen["pid"] = directory / PID_FILE
        on_event("gap", {"from": None, "to": "2025-01-07T09:30:00", "bars_skipped": 3})
    monkeypatch.setattr(run, "serve", fake_serve)
    _main(monkeypatch, "paper", "run", "fast")
    err = capsys.readouterr().err
    assert "fast" in err and "30초마다" in err and "gap 시작 → 2025-01-07T09:30:00  봉 3개 건너뜀" in err
    assert "중지됨" in err

    (seen["pid"]).write_text(str(os.getpid()))
    _main(monkeypatch, "paper", "list")
    assert "실행 중" in capsys.readouterr().out
    monkeypatch.setattr(run, "stop_runner", lambda directory: True)
    _main(monkeypatch, "paper", "stop", "fast")
    assert "중지를 요청했습니다" in capsys.readouterr().err
