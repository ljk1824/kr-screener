# 국내 가치 스캐너

PER · FCF · 매출 성장률 조건으로 코스피/코스닥 전 종목을 거르는 개인용 모바일 웹앱.
운영 비용 0원 (GitHub Actions + GitHub Pages + OpenDART 무료 API).

## 구조
| 파일 | 역할 |
|---|---|
| `scripts/screener.py` | DART 재무 수집, 시가총액 결합, 지표 계산 |
| `.github/workflows/screener.yml` | 평일 16:40 KST 주가 갱신, 일요일 03:00 KST 재무·1층 검사 갱신 |
| `docs/index.html` | 폰에서 보는 화면 (GitHub Pages) |
| `docs/data/screen.json` | 화면이 읽는 결과 파일 (자동 생성) |
| `data/financials.json` | 재무 수집 캐시 (자동 생성) |
| `data/checks.json` | 1층 검사용 공시·감사의견 캐시 (자동 생성) |

## 설치 (약 20분)
1. **OpenDART 인증키 발급**: opendart.fss.or.kr → 인증키 신청 (무료, 즉시~1일)
2. **GitHub 저장소 생성**: 새 저장소를 **Public**으로 만들고 이 폴더 전체를 업로드
   - 무료 계정의 GitHub Pages는 Public 저장소에서만 동작
   - `.github` 폴더가 숨김 폴더라 누락되기 쉬우니 꼭 확인
3. **인증키 등록**: 저장소 Settings → Secrets and variables → Actions → New repository secret
   - Name: `DART_API_KEY` / Value: 발급받은 키
4. **Actions 쓰기 권한**: Settings → Actions → General → Workflow permissions → Read and write 선택
5. **Pages 켜기**: Settings → Pages → Branch: `main`, 폴더: `/docs` → Save
6. **첫 수집 실행**: Actions 탭 → 스크리너 데이터 갱신 → Run workflow (mode: all)
   - 전 종목 첫 수집은 약 30분~1시간 소요
7. **폰 홈 화면에 추가**: `https://<아이디>.github.io/<저장소명>/` 접속
   - 아이폰: 사파리 공유 버튼 → 홈 화면에 추가
   - 안드로이드: 크롬 메뉴 → 홈 화면에 추가

## 로컬(WSL) 실행
```bash
pip install -r requirements.txt
export DART_API_KEY=발급받은키
python scripts/screener.py --mode all --limit 30   # 30개만 시험 수집 (재무 → 1층 검사 → 화면 데이터)
python scripts/screener.py --mode all              # 전체
cd docs && python -m http.server 8000              # http://localhost:8000
```

## 계산 기준
- PER = 시가총액 ÷ 최근 4분기(TTM) 지배주주순이익 (적자 기업은 제외)
- FCF = TTM 영업활동현금흐름 − TTM 유형자산 취득액
- 매출 성장률 = 최신 보고서 누적 매출 ÷ 전년 동기 누적 매출 − 1
- TTM = 직전 사업보고서 + 올해 누적 − 전년 동기 누적
- 연결재무제표 우선, 없으면 별도재무제표

## 1층 즉시탈락 검사
흑자, PER 30배 미만, TTM FCF 플러스 종목만 매주 검사 (DART 호출 절약).
하나라도 탈락이면 화면에서 기본으로 숨김 (세부 조건에서 전체/탈락 제외/통과만 선택).

| 항목 | 탈락 기준 | 데이터 |
|---|---|---|
| 감사의견 | 직전 사업보고서 감사의견이 적정 아님 | DART 감사의견 API |
| CB·BW | 최근 2년 발행결정 2건 이상 (1건은 주의 표시만) | DART 공시목록 |
| 최대주주 | 최근 2년 최대주주변경 공시 | DART 공시목록 |
| 이익의 질 | TTM 순이익 > 영업이익 × 1.5, 또는 영업적자 | 재무 |
| 현금 전환 | TTM 영업현금흐름 ÷ 순이익 < 0.5 | 재무 |
| FCF 지속성 | 최근 3개 사업연도 중 2년 이상 FCF 적자 | 사업보고서 당기·전기·전전기 |
| 시장 조치 | 관리·환기종목 소속, 또는 1년 내 관리종목·환기종목·실질심사·불성실공시 지정 공시 | 종목목록 소속부 + DART 공시목록 |

공시를 못 읽은 항목은 "확인 필요"로 표시되고, "통과만" 필터에서는 제외됨.

## 알려진 한계
- 계정명이 비표준인 기업은 매출·CAPEX가 누락될 수 있음 (화면에 경고 표시)
- CAPEX는 유형자산 취득만 반영 (무형자산 취득 제외)
- 금융·지주 구분은 종목명 키워드 추정
- OpenDART는 일일 호출 한도가 있음. 한도 도달 시 저장 후 멈추고 다음 실행 때 이어서 수집
- 주가 수집 라이브러리(FinanceDataReader, pykrx)는 KRX 정책 변경 시 막힐 수 있음.
  GitHub 서버(해외)에서 실패하면 WSL에서 로컬 실행 후 결과를 push하는 방식으로 전환
- 저장소에 60일간 활동이 없으면 예약 실행이 자동 중지될 수 있음 → Actions 탭에서 다시 활성화
- 1층 검사: 공시 제목 키워드로 판정하므로 제목 형식이 다르면 누락 가능. 정정·철회 공시는 집계에서 제외
- 1층 검사: 관리종목 소속부 판정은 FinanceDataReader의 소속부 정보가 있을 때만 동작 (없으면 공시로만 판정)
- 투자 판단 전 반드시 공시 원문 확인
