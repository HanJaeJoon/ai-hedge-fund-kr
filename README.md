# AI Hedge Fund - Korea (ai-hedge-fund-kr)

[virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund)의 포크입니다. 원본 엔진은 그대로 두고 **한국 주식(KRX) 데이터 소스**를 추가했습니다. 가격은 네이버 금융, 재무제표는 DART(전자공시) 오픈 API를 쓰므로 데이터 비용이 들지 않습니다.

## 한국 주식으로 돌리기

1. DART 오픈 API 인증키를 발급받습니다 (https://opendart.fss.or.kr, 무료, 즉시 발급).
2. LLM API 키 하나를 준비합니다 (Anthropic, OpenAI 등 원본과 동일).
3. `~/.hedge-fund/.env`에 키를 넣습니다:

   ```
   DART_API_KEY=발급받은 키
   ANTHROPIC_API_KEY=...
   HEDGE_FUND_LANGUAGE=Korean   # 선택: 에이전트 분석(reasoning)을 한국어로 받음
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

# AI Hedge Fund (원본 안내)

AI 기반 헤지펀드의 개념 증명(proof of concept) 프로젝트입니다. AI로 매매 결정을 내리는 방법을 탐구하는 것이 목표입니다. 이 프로젝트는 **교육** 목적으로만 만들었으며, 실제 매매나 투자를 위한 것이 아닙니다.

<img width="2400" height="1460" alt="image" src="https://github.com/user-attachments/assets/e3985623-c226-4c1a-a587-e03fb4eca31e" />


참고: 이 시스템은 실제로 매매하지 않습니다.

[![Twitter Follow](https://img.shields.io/twitter/follow/virattt?style=social)](https://twitter.com/virattt)

## 면책 조항

이 프로젝트는 **교육과 연구 목적으로만** 만들었습니다.

- 실제 매매나 투자를 위한 것이 아닙니다.
- 투자 조언이나 수익 보장을 제공하지 않습니다.
- 제작자는 금전적 손실에 책임지지 않습니다.
- 투자 결정은 금융 전문가와 상담하세요.
- 과거 성과가 미래 수익을 보장하지 않습니다.

이 소프트웨어를 사용하면 학습 목적으로만 쓰는 데 동의한 것으로 봅니다.

## 설치

```bash
pipx install aihf
```

(`uv tool install aihf`로 설치하거나, 원하는 환경에 `pip install aihf`로 설치해도 됩니다.)

설치한 뒤에는 어디서든 실행할 수 있습니다.

```bash
aihf
```

### API 키

앱은 키가 처음 필요할 때 입력을 요청하고 `~/.hedge-fund/.env`에 저장하므로 미리 설정할 것은 없습니다. 필요한 키는 다음과 같습니다.

- 가격, 재무, 실적 데이터를 가져올 [Financial Datasets](https://financialdatasets.ai) API 키
- 투자자 에이전트가 쓸 모델 API 키 하나. 지원하는 제공자: Anthropic, OpenAI, DeepSeek, Google, xAI, Kimi, TypeSafe (Jev)

셸에서 export한 키가 저장된 파일보다 항상 우선합니다.

## 실행

### 대화형 앱

```bash
aihf
```

인자 없이 실행하면 대화형 터미널 앱이 열립니다. 모드는 두 가지입니다.

- **모의투자** - 내 펀드 목록입니다. 펀드마다 실제 거래일과 가상의 돈으로 쌓은 원장이 있습니다. 펀드를 선택하면 다음 실행에서 무엇을 할지 보여 줍니다. enter를 누르면 다음으로 마감된 세션까지 실행하고(매매 전에 실행할 결정을 그대로 보여 주는 승인 단계를 거칩니다), `s`를 누르면 전체 세션 기록을, `h`/`r`을 누르면 킬 스위치를 씁니다. 같은 목록에서 새 펀드도 만들 수 있습니다. 전략, 자본금, 주기, 종목을 정하면 바로 운용이 시작됩니다. 모든 세션은 `~/.hedge-fund/paper/<name>/`의 해시 체인 원장에 이어서 기록되므로, NAV는 초기화되지 않는 실제 운용 실적이 됩니다.
- **백테스트** - 과거 데이터로 펀드를 다시 돌려 보고, 자산 곡선이 벤치마크와 나란히 그려지는 것을 볼 수 있습니다. 결과는 `~/.hedge-fund/research/`에 저장됩니다. 펀드 정의는 `~/.hedge-fund/mandates/`에 있으며 종목을 담고 있지 않습니다. 유니버스는 백테스트할 때마다 고릅니다.



## 개발

```bash
git clone https://github.com/virattt/ai-hedge-fund.git
cd ai-hedge-fund
poetry install
poetry run aihf
poetry run pytest hedge_fund
```

## 기여 방법

1. 저장소를 포크합니다.
2. 기능 브랜치를 만듭니다.
3. 변경 사항을 커밋합니다.
4. 브랜치에 push합니다.
5. Pull Request를 만듭니다.

**중요**: pull request는 작게, 한 가지 목적에 집중해서 만들어 주세요. 그래야 리뷰와 병합이 쉬워집니다.

## 기능 요청

기능 요청이 있으면 [issue](https://github.com/virattt/ai-hedge-fund/issues)를 열고 `enhancement` 태그를 달아 주세요.

## 라이선스

이 프로젝트는 MIT 라이선스를 따릅니다. 자세한 내용은 LICENSE 파일을 참고하세요.
