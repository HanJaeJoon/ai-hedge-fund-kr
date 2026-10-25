# AI Hedge Fund - Korea (ai-hedge-fund-kr)

[virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund)의 포크입니다. 원본 엔진은 그대로 두고 **한국 주식(KRX) 데이터 소스**를 추가했습니다. 가격은 네이버 금융, 재무제표는 DART(전자공시) 오픈 API를 쓰므로 데이터 비용이 들지 않습니다.

## 한국 주식으로 돌리기

1. DART 오픈 API 인증키를 발급받습니다 (https://opendart.fss.or.kr, 무료, 즉시 발급).
2. LLM API 키 하나를 준비합니다 (Anthropic, OpenAI 등 원본과 동일).
3. `~/.hedge-fund/.env`에 키를 넣습니다:

   ```
   DART_API_KEY=발급받은 키
   ANTHROPIC_API_KEY=...
   ```

4. `HEDGE_FUND_DATA=krx`로 실행합니다. 종목은 6자리 종목코드, 벤치마크는 ETF 코드(기본 069500 = KODEX 200), 금액은 원화입니다.

   ```bash
   poetry install
   HEDGE_FUND_DATA=krx poetry run aihf backtest hedge_fund/fund/korea.yaml        --universe 005930,000660 --start 2026-07-01 --end 2026-10-23
   ```

   PowerShell에서는 `$env:HEDGE_FUND_DATA="krx"`를 먼저 실행합니다.

`hedge_fund/fund/korea.yaml`은 Graham/Buffett/Munger 에이전트의 롱온리 가치투자 mandate입니다. 복사해서 비중과 에이전트를 바꿔 쓰면 됩니다.

## 한국 데이터의 범위와 한계

- 가격: 네이버 금융 일봉(비공식 엔드포인트). 분봉 펀드(`rebalance: minute`)는 지원하지 않습니다.
- 재무: DART 연결재무제표를 공시 접수일 기준으로 TTM 지표(P/E, ROE, 마진, 부채비율, 주당 지표 등)로 가공합니다. 백테스트는 해당 시점에 공시된 데이터만 봅니다.
- 없음: 뉴스, 내부자 거래, 실적 컨센서스. 어닝 드리프트(`pead`) 모델은 한국 데이터로는 동작하지 않습니다.
- 시가총액 규모 백분위는 미국 기준이라 한국 종목에서는 표시하지 않습니다.
- DART 원문 응답은 `~/.hedge-fund/cache/dart/`에 캐시되어 재실행은 API 호출 없이 동작합니다.

이 저장소도 원본과 같이 **학습/연구 목적**이며 투자 조언이 아닙니다.

---

# AI Hedge Fund

This is a proof of concept for an AI-powered hedge fund. The goal of this project is to explore the use of AI to make trading decisions. This project is for **educational** purposes only and is not intended for real trading or investment.

<img width="2400" height="1460" alt="image" src="https://github.com/user-attachments/assets/e3985623-c226-4c1a-a587-e03fb4eca31e" />


Note: the system does not actually make any trades.

[![Twitter Follow](https://img.shields.io/twitter/follow/virattt?style=social)](https://twitter.com/virattt)

## Disclaimer

This project is for **educational and research purposes only**.

- Not intended for real trading or investment
- No investment advice or guarantees provided
- Creator assumes no liability for financial losses
- Consult a financial advisor for investment decisions
- Past performance does not indicate future results

By using this software, you agree to use it solely for learning purposes.

## How to Install

```bash
pipx install aihf
```

(or `uv tool install aihf`, or `pip install aihf` into an environment of your choice)

Then run it from anywhere:

```bash
aihf
```

### API keys

The app asks for keys the first time it needs them and saves them to `~/.hedge-fund/.env` — nothing to configure up front. It needs:

- A [Financial Datasets](https://financialdatasets.ai) API key, for prices, fundamentals, and earnings.
- One model API key for the investor agents. Supported providers: Anthropic, OpenAI, DeepSeek, Google, xAI, Kimi, TypeSafe (Jev).

Keys exported in your shell always win over the saved file.

## How to Run

### Interactive app

```bash
aihf
```

With no arguments, this launches the interactive terminal app. It has two modes:

- **Paper trading** — your funds, each with a ledger of real market days and fake money. Highlight a fund to see what its next run will do; press enter to run it through the next completed session (an approval step shows the exact decision about to execute before anything trades), `s` for its full session history, `h`/`r` for the kill switch. Build a new fund from the same list: strategies, capital, cadence, tickers, and it is live. Every session is appended to a hash-chained ledger in `~/.hedge-fund/paper/<name>/`, so NAV is a track record, not a reset.
- **Backtesting** — replay a fund over history and watch its equity curve draw against its benchmark. Results are saved to `~/.hedge-fund/research/`. Fund definitions live in `~/.hedge-fund/mandates/` and carry no tickers; you pick the universe per backtest.



## Development

```bash
git clone https://github.com/virattt/ai-hedge-fund.git
cd ai-hedge-fund
poetry install
poetry run aihf
poetry run pytest hedge_fund
```

## How to Contribute

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

**Important**: Please keep your pull requests small and focused. This will make it easier to review and merge.

## Feature Requests

If you have a feature request, please open an [issue](https://github.com/virattt/ai-hedge-fund/issues) and make sure it is tagged with `enhancement`.

## License

This project is licensed under the MIT License - see the LICENSE file for details.
