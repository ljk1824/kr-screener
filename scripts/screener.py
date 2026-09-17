#!/usr/bin/env python3
"""
국내주식 가치 스캐너 - 데이터 수집기

모드
  financials : OpenDART에서 전 종목 재무(매출, 순이익, 영업CF, CAPEX) 수집 -> data/financials.json
  checks     : 후보 종목의 1층 즉시탈락 검사용 공시·감사의견 수집 -> data/checks.json
  prices     : 시가총액 수집 + 재무·검사 결합 -> docs/data/screen.json (웹앱이 읽는 파일)
  all        : financials -> checks -> prices

1층 즉시탈락 검사 (하나라도 걸리면 탈락)
  감사의견    : 직전 사업보고서 감사의견이 적정이 아님
  CB·BW      : 최근 2년 전환사채·신주인수권부사채 발행결정 2건 이상 (1건은 주의)
  최대주주    : 최근 2년 최대주주 변경 공시
  이익의 질   : TTM 순이익이 영업이익의 1.5배 초과, 또는 영업적자
  현금 전환   : TTM 영업현금흐름 / 순이익 0.5 미만
  FCF 지속성  : 최근 3개 사업연도 중 2년 이상 FCF 적자
  시장 조치   : 관리·환기종목 소속, 또는 최근 1년 관리종목·환기종목·실질심사·불성실공시 지정 공시

계산 기준
  PER       = 시가총액 / TTM 지배주주순이익 (적자면 PER 없음)
  FCF       = TTM 영업활동현금흐름 - TTM 유형자산 취득액
  매출성장률 = 최신 보고서 누적 매출 / 전년 동기 누적 매출 - 1
  TTM       = 직전 사업연도 + 올해 누적 - 전년 동기 누적
"""
import argparse
import io
import json
import os
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIN_PATH = os.path.join(ROOT, "data", "financials.json")
CORP_CACHE = os.path.join(ROOT, "data", "corp_codes.json")
CHECK_PATH = os.path.join(ROOT, "data", "checks.json")
FIN_VERSION = 2  # 재무 레코드 구조가 바뀌면 올려서 재수집
OUT_PATH = os.path.join(ROOT, "docs", "data", "screen.json")
DART = "https://opendart.fss.or.kr/api"
SLEEP = float(os.environ.get("DART_SLEEP", "0.15"))
REPORT_NAME = {"11013": "1분기", "11012": "반기", "11014": "3분기", "11011": "사업보고서"}

# 계정 매칭 규칙: 표준 account_id 우선, 없으면 계정명(공백 제거) 완전일치
ACCOUNTS = {
    "revenue": {
        "sj": ("IS", "CIS"),
        "ids": ["ifrs-full_Revenue", "ifrs_Revenue"],
        "names": ["매출액", "영업수익", "수익(매출액)", "매출", "매출액(영업수익)", "영업수익(매출액)"],
    },
    "net_parent": {
        "sj": ("IS", "CIS"),
        "ids": ["ifrs-full_ProfitLossAttributableToOwnersOfParent",
                "ifrs_ProfitLossAttributableToOwnersOfParent"],
        "names": ["지배기업의소유주에게귀속되는당기순이익", "지배기업의소유주에게귀속되는당기순이익(손실)",
                  "지배기업소유주지분", "지배기업의소유주지분", "지배기업소유주"],
    },
    "net": {
        "sj": ("IS", "CIS"),
        "ids": ["ifrs-full_ProfitLoss", "ifrs_ProfitLoss"],
        "names": ["당기순이익", "당기순이익(손실)", "분기순이익", "분기순이익(손실)",
                  "반기순이익", "반기순이익(손실)", "당기순손익"],
    },
    "op": {
        "sj": ("IS", "CIS"),
        "ids": ["dart_OperatingIncomeLoss", "ifrs-full_OperatingIncomeLoss"],
        "names": ["영업이익", "영업이익(손실)", "영업손실", "영업손익", "영업손실(이익)"],
    },
    "ocf": {
        "sj": ("CF",),
        "ids": ["ifrs-full_CashFlowsFromUsedInOperatingActivities",
                "ifrs_CashFlowsFromUsedInOperatingActivities"],
        "names": ["영업활동현금흐름", "영업활동으로인한현금흐름", "영업활동으로부터의현금흐름",
                  "영업활동순현금흐름"],
    },
    "capex": {
        "sj": ("CF",),
        "ids": ["ifrs-full_PurchaseOfPropertyPlantAndEquipment",
                "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                "ifrs_PurchaseOfPropertyPlantAndEquipment"],
        "names": ["유형자산의취득", "유형자산취득", "유형자산의증가"],
    },
}

FIN_KEYWORDS = ["금융", "지주", "홀딩스", "은행", "증권", "보험", "생명", "화재", "캐피탈",
                "카드", "리츠", "인베스트", "파이낸셜", "Holdings", "HOLDINGS"]


MARKET_KW = ["관리종목지정", "투자주의환기종목지정", "상장적격성실질심사", "불성실공시법인지정"]
CHECK_LABELS = ["감사의견", "CB·BW", "최대주주", "이익의 질", "현금 전환", "FCF 지속성", "시장 조치"]


class DartLimitError(Exception):
    pass


def now_kst():
    return datetime.now(KST)


def log(msg):
    print(f"[{now_kst():%H:%M:%S}] {msg}", flush=True)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 종목 목록/시총
def load_listing():
    """상장 보통주 목록과 시가총액. FinanceDataReader 우선, 실패 시 pykrx."""
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing("KRX")
        out = {}
        for _, r in df.iterrows():
            code = str(r["Code"]).zfill(6)
            out[code] = {
                "name": str(r["Name"]),
                "market": str(r["Market"]),
                "close": float(r.get("Close") or 0),
                "marcap": float(r.get("Marcap") or 0),
                "dept": str(r.get("Dept") or "") if "Dept" in df.columns else "",
            }
        price_date = now_kst().strftime("%Y-%m-%d")
        log(f"FinanceDataReader 목록 {len(out)}개")
        return out, price_date
    except Exception as e:  # noqa: BLE001
        log(f"FinanceDataReader 실패: {e} -> pykrx 시도")

    from pykrx import stock
    day = stock.get_nearest_business_day_in_a_week()
    out = {}
    for market in ("KOSPI", "KOSDAQ"):
        cap = stock.get_market_cap(day, market=market)
        for code, r in cap.iterrows():
            out[code] = {
                "name": stock.get_market_ticker_name(code),
                "market": market,
                "close": float(r["종가"]),
                "marcap": float(r["시가총액"]),
                "dept": "",
            }
    price_date = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    log(f"pykrx 목록 {len(out)}개 ({price_date})")
    return out, price_date


def normalize_market(m):
    m = m.upper()
    if m.startswith("KOSPI"):
        return "KOSPI"
    if m.startswith("KOSDAQ"):
        return "KOSDAQ"
    return None


def is_target(code, info):
    """보통주, 코스피/코스닥, 스팩 제외"""
    if not code.isdigit() or code[-1] != "0":
        return False  # 우선주 등
    if normalize_market(info["market"]) is None:
        return False  # 코넥스 등
    name = info["name"]
    if "스팩" in name or "SPAC" in name.upper():
        return False
    return True


def is_fin(name):
    return any(k in name for k in FIN_KEYWORDS)


# ---------------------------------------------------------------- DART
def dart_get(path, params):
    params = dict(params, crtfc_key=os.environ["DART_API_KEY"])
    for attempt in range(3):
        try:
            r = requests.get(f"{DART}/{path}", params=params, timeout=30)
            r.raise_for_status()
            time.sleep(SLEEP)
            return r
        except requests.RequestException as e:
            log(f"네트워크 오류 재시도 {attempt + 1}/3: {e}")
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"DART 요청 실패: {path}")


def load_corp_codes():
    """stock_code -> corp_code 매핑 (하루 1회 갱신)"""
    cache = load_json(CORP_CACHE, {})
    if cache.get("date") == now_kst().strftime("%Y-%m-%d"):
        return cache["map"]
    r = dart_get("corpCode.xml", {})
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        xml = z.read(z.namelist()[0])
    root = ET.fromstring(xml)
    mapping = {}
    for el in root.iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if sc:
            mapping[sc] = el.findtext("corp_code").strip()
    save_json(CORP_CACHE, {"date": now_kst().strftime("%Y-%m-%d"), "map": mapping})
    log(f"DART 기업코드 {len(mapping)}개")
    return mapping


def fetch_statement(corp_code, year, reprt, fs_order=("CFS", "OFS")):
    """전체 재무제표. 연결 우선, 없으면 별도. (rows, fs_div) 또는 (None, None)"""
    for fs in fs_order:
        r = dart_get("fnlttSinglAcntAll.json", {
            "corp_code": corp_code, "bsns_year": str(year),
            "reprt_code": reprt, "fs_div": fs,
        })
        data = r.json()
        status = data.get("status")
        if status == "000":
            return data.get("list", []), fs
        if status == "020":
            raise DartLimitError(data.get("message"))
        if status not in ("013",):
            log(f"  DART 응답 {status}: {data.get('message')}")
    return None, None


def to_int(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def norm(s):
    return "".join((s or "").split())


def find_row(rows, key):
    rule = ACCOUNTS[key]
    cands = [r for r in rows if r.get("sj_div") in rule["sj"]]
    if key in ("net_parent", "net"):
        cands = [r for r in cands if "Comprehensive" not in (r.get("account_id") or "")]
    for r in cands:
        if r.get("account_id") in rule["ids"]:
            return r
    names = set(rule["names"])
    for r in cands:
        if norm(r.get("account_nm")) in names:
            return r
    return None


def cur_prev(row):
    """(당기 누적, 전년 동기 누적). 분기 손익은 add_amount가 누적값."""
    if row is None:
        return None, None
    cur = to_int(row.get("thstrm_add_amount"))
    if cur is None:
        cur = to_int(row.get("thstrm_amount"))
    prev = to_int(row.get("frmtrm_add_amount"))
    if prev is None:
        prev = to_int(row.get("frmtrm_amount"))
    if prev is None:
        prev = to_int(row.get("frmtrm_q_amount"))
    return cur, prev


def extract(rows):
    out = {}
    for key in ACCOUNTS:
        cur, prev = cur_prev(find_row(rows, key))
        if key == "capex":  # 취득액 부호 표기가 회사/보고서마다 달라 절댓값으로 통일
            cur = abs(cur) if cur is not None else None
            prev = abs(prev) if prev is not None else None
        out[key] = (cur, prev)
    if out["net_parent"][0] is None:  # 별도재무제표 등은 지배주주 구분 없음
        out["net_parent"] = out["net"]
    return out


def ttm(cur, prev, annual):
    if None in (cur, prev, annual):
        return None
    return annual + cur - prev


def report_candidates(today):
    """제출기한 + 여유 5일 기준으로 최신 보고서 후보"""
    y, md = today.year, (today.month, today.day)
    if md >= (11, 20):
        return [(y, "11014"), (y, "11012")]
    if md >= (8, 20):
        return [(y, "11012"), (y, "11013")]
    if md >= (5, 20):
        return [(y, "11013"), (y - 1, "11011")]
    if md >= (4, 5):
        return [(y - 1, "11011"), (y - 1, "11014")]
    return [(y - 1, "11014"), (y - 1, "11012")]


def fcf_history(rows, base_year):
    """사업보고서의 당기·전기·전전기 FCF. [[연도, FCF], ...] 최신순"""
    ocf = find_row(rows, "ocf")
    capex = find_row(rows, "capex")
    out = []
    for i, col in enumerate(("thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount")):
        o = to_int(ocf.get(col)) if ocf else None
        c = to_int(capex.get(col)) if capex else None
        fcf = None if o is None else o - (abs(c) if c is not None else 0)
        out.append([base_year - i, fcf])
    return out


def build_record(corp_code, candidates):
    rows = fs = year = reprt = None
    for year, reprt in candidates:
        rows, fs = fetch_statement(corp_code, year, reprt)
        if rows:
            break
    if not rows:
        return None

    keys = ("revenue", "net_parent", "op", "ocf", "capex")
    latest = extract(rows)
    is_annual = reprt == "11011"
    if is_annual:
        vals = {k: latest[k][0] for k in keys}
        arows, annual_year = rows, year
    else:
        order = (fs, "OFS" if fs == "CFS" else "CFS")
        arows, _ = fetch_statement(corp_code, year - 1, "11011", fs_order=order)
        annual_year = year - 1
        annual = extract(arows) if arows else {k: (None, None) for k in ACCOUNTS}
        vals = {k: ttm(latest[k][0], latest[k][1], annual[k][0]) for k in keys}

    rev_cur, rev_prev = latest["revenue"]
    growth = None
    if rev_cur is not None and rev_prev and rev_prev > 0:
        growth = rev_cur / rev_prev - 1

    capex = vals["capex"] if vals["capex"] is not None else 0  # 취득 내역이 없으면 0
    fcf = vals["ocf"] - capex if vals["ocf"] is not None else None

    return {
        "v": FIN_VERSION,
        "report": f"{year} {REPORT_NAME[reprt]}",
        "fs": "연결" if fs == "CFS" else "별도",
        "annual_year": annual_year,
        "rev_ttm": vals["revenue"],
        "rev_growth": growth,
        "ni_ttm": vals["net_parent"],
        "op_ttm": vals["op"],
        "ocf_ttm": vals["ocf"],
        "capex_ttm": capex,
        "fcf_ttm": fcf,
        "fcf_hist": fcf_history(arows, annual_year) if arows else [],
        "updated": now_kst().strftime("%Y-%m-%d"),
    }


def run_financials(force=False, limit=None):
    if not os.environ.get("DART_API_KEY"):
        sys.exit("DART_API_KEY 환경변수가 없습니다.")
    listing, _ = load_listing()
    corp = load_corp_codes()
    fin = load_json(FIN_PATH, {})
    cands = report_candidates(now_kst())
    target_report = f"{cands[0][0]} {REPORT_NAME[cands[0][1]]}"

    todo = []
    for code, info in listing.items():
        if not is_target(code, info) or code not in corp:
            continue
        rec = fin.get(code)
        if not force and rec and rec.get("report") == target_report and rec.get("v") == FIN_VERSION:
            continue  # 이미 최신 보고서 반영
        todo.append(code)
    if limit:
        todo = todo[:limit]
    log(f"기준 보고서 {target_report}, 수집 대상 {len(todo)}개")

    done = 0
    try:
        for i, code in enumerate(todo, 1):
            try:
                rec = build_record(corp[code], cands)
            except DartLimitError as e:
                log(f"DART 일일 한도 도달: {e}. 저장 후 중단 (다음 실행 때 이어서 수집)")
                break
            except Exception as e:  # noqa: BLE001
                log(f"  {code} 오류: {e}")
                continue
            if rec:
                rec["name"] = listing[code]["name"]
                fin[code] = rec
                done += 1
            if i % 100 == 0:
                save_json(FIN_PATH, fin)
                log(f"진행 {i}/{len(todo)} (저장됨)")
    finally:
        save_json(FIN_PATH, fin)
    log(f"재무 수집 완료 {done}개, 누적 {len(fin)}개")


# ---------------------------------------------------------------- 1층 검사
def fetch_disclosures(corp_code, days=730):
    end = now_kst()
    params = {
        "corp_code": corp_code,
        "bgn_de": (end - timedelta(days=days)).strftime("%Y%m%d"),
        "end_de": end.strftime("%Y%m%d"),
        "page_count": 100,
    }
    items = []
    for page in range(1, 6):
        data = dart_get("list.json", dict(params, page_no=page)).json()
        status = data.get("status")
        if status == "013":
            break
        if status == "020":
            raise DartLimitError(data.get("message"))
        if status != "000":
            raise RuntimeError(f"공시목록 {status}: {data.get('message')}")
        items += data.get("list", [])
        if page >= int(data.get("total_page") or 1):
            break
    return items


def scan_disclosures(items, today):
    one_year_ago = (today - timedelta(days=365)).strftime("%Y%m%d")
    cbbw, owner, market = [], [], []
    for it in items:
        nm = norm(it.get("report_nm"))
        if "정정" in nm or "철회" in nm:
            continue  # 원공시만 집계
        entry = {"d": it.get("rcept_dt"), "nm": (it.get("report_nm") or "").strip(), "no": it.get("rcept_no")}
        if "전환사채권발행결정" in nm or "신주인수권부사채권발행결정" in nm:
            cbbw.append(entry)
        elif "최대주주변경" in nm and "수반" not in nm:
            owner.append(entry)
        elif any(k in nm for k in MARKET_KW) and (it.get("rcept_dt") or "") >= one_year_ago:
            market.append(entry)
    key = lambda e: e["d"] or ""
    return (sorted(cbbw, key=key, reverse=True), sorted(owner, key=key, reverse=True),
            sorted(market, key=key, reverse=True))


def fetch_audit(corp_code, year):
    data = dart_get("accnutAdtorNmNdAdtOpinion.json", {
        "corp_code": corp_code, "bsns_year": str(year), "reprt_code": "11011",
    }).json()
    status = data.get("status")
    if status == "020":
        raise DartLimitError(data.get("message"))
    if status != "000":
        return None
    rows = data.get("list", [])
    for r in rows:
        if "당기" in (r.get("bsns_year") or ""):
            return (r.get("adt_opinion") or "").strip()
    return (rows[0].get("adt_opinion") or "").strip() if rows else None


def total_caps(listing):
    """보통주 시총 + 같은 회사 우선주 시총"""
    caps = {}
    for code, info in listing.items():
        if len(code) == 6:
            base = code[:5] + "0"
            caps[base] = caps.get(base, 0) + (info.get("marcap") or 0)
    return caps


def is_check_candidate(rec, cap):
    """검사 대상: 흑자, PER 30배 미만, TTM FCF 플러스 (API 호출 절약)"""
    ni, fcf = rec.get("ni_ttm"), rec.get("fcf_ttm")
    if not ni or ni <= 0 or fcf is None or fcf <= 0:
        return False
    return cap / ni < 30


def run_checks(force=False, limit=None):
    if not os.environ.get("DART_API_KEY"):
        sys.exit("DART_API_KEY 환경변수가 없습니다.")
    listing, _ = load_listing()
    corp = load_corp_codes()
    fin = load_json(FIN_PATH, {})
    checks = load_json(CHECK_PATH, {})
    caps = total_caps(listing)
    today = now_kst()
    fresh_after = (today - timedelta(days=6)).strftime("%Y-%m-%d")

    todo = []
    for code, rec in fin.items():
        info = listing.get(code)
        if not info or code not in corp or not is_target(code, info):
            continue
        if not is_check_candidate(rec, caps.get(code, info["marcap"])):
            continue
        c = checks.get(code)
        if not force and c and c.get("date", "") > fresh_after:
            continue
        todo.append(code)
    if limit:
        todo = todo[:limit]
    log(f"1층 검사 대상 {len(todo)}개")

    try:
        for i, code in enumerate(todo, 1):
            rec = fin[code]
            chk = {"date": today.strftime("%Y-%m-%d"), "disc_ok": False, "audit": None,
                   "cbbw": [], "owner": [], "market": []}
            try:
                cbbw, owner, market = scan_disclosures(fetch_disclosures(corp[code]), today)
                chk.update(disc_ok=True, cbbw=cbbw[:5], owner=owner[:3], market=market[:3])
                if rec.get("annual_year"):
                    chk["audit"] = fetch_audit(corp[code], rec["annual_year"])
            except DartLimitError as e:
                log(f"DART 일일 한도 도달: {e}. 저장 후 중단")
                break
            except Exception as e:  # noqa: BLE001
                log(f"  {code} 검사 오류: {e}")
            checks[code] = chk
            if i % 100 == 0:
                save_json(CHECK_PATH, checks)
                log(f"검사 진행 {i}/{len(todo)} (저장됨)")
    finally:
        save_json(CHECK_PATH, checks)
    log(f"1층 검사 누적 {len(checks)}개")


def fmt_date(d):
    d = d or ""
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d


def evaluate_layer1(rec, chk, info):
    """반환: (상태, 항목들) 상태 p=통과 f=탈락 u=확인필요 n=미검사
    항목: [라벨, 판정(p/f/w/u), 설명, 공시접수번호 또는 None]"""
    items = []
    miss = "검사 대상 외" if chk is None else "공시 조회 실패"

    # 감사의견
    op = norm((chk or {}).get("audit"))
    if not chk or not op or op == "-":
        items.append([CHECK_LABELS[0], "u", "검사 대상 외" if chk is None else "감사의견 정보 없음", None])
    elif any(k in op for k in ("한정", "부적정", "거절")):
        items.append([CHECK_LABELS[0], "f", chk["audit"], None])
    elif "적정" in op:
        items.append([CHECK_LABELS[0], "p", f"{rec.get('annual_year')}년 적정", None])
    else:
        items.append([CHECK_LABELS[0], "u", chk["audit"], None])

    # CB·BW, 최대주주
    if not chk or not chk.get("disc_ok"):
        items.append([CHECK_LABELS[1], "u", miss, None])
        items.append([CHECK_LABELS[2], "u", miss, None])
    else:
        n = len(chk["cbbw"])
        last = chk["cbbw"][0] if n else None
        if n >= 2:
            items.append([CHECK_LABELS[1], "f", f"2년 내 {n}건, 최근 {fmt_date(last['d'])}", last["no"]])
        elif n == 1:
            items.append([CHECK_LABELS[1], "w", f"2년 내 1건 ({fmt_date(last['d'])})", last["no"]])
        else:
            items.append([CHECK_LABELS[1], "p", "2년 내 없음", None])
        if chk["owner"]:
            o = chk["owner"][0]
            items.append([CHECK_LABELS[2], "f", f"변경 공시 {fmt_date(o['d'])}", o["no"]])
        else:
            items.append([CHECK_LABELS[2], "p", "2년 내 변경 없음", None])

    # 이익의 질
    ni, opi = rec.get("ni_ttm"), rec.get("op_ttm")
    if ni is None or opi is None:
        items.append([CHECK_LABELS[3], "u", "영업이익 계정 확인 불가", None])
    elif ni <= 0:
        items.append([CHECK_LABELS[3], "f", "순손실", None])
    elif opi <= 0:
        items.append([CHECK_LABELS[3], "f", "영업이익 적자", None])
    else:
        ratio = ni / opi
        items.append([CHECK_LABELS[3], "f" if ratio > 1.5 else "p",
                      f"순이익이 영업이익의 {ratio:.2f}배", None])

    # 현금 전환
    ocf = rec.get("ocf_ttm")
    if ocf is None or not ni or ni <= 0:
        items.append([CHECK_LABELS[4], "u", "계산 불가", None])
    else:
        ratio = ocf / ni
        items.append([CHECK_LABELS[4], "f" if ratio < 0.5 else "p",
                      f"영업현금흐름이 순이익의 {ratio:.2f}배", None])

    # FCF 지속성
    hist = [v for _, v in rec.get("fcf_hist", []) if v is not None]
    if len(hist) < 2:
        items.append([CHECK_LABELS[5], "u", "3년 자료 부족", None])
    else:
        neg = sum(1 for v in hist if v < 0)
        items.append([CHECK_LABELS[5], "f" if neg >= 2 else "p",
                      f"{len(hist)}년 중 {len(hist) - neg}년 플러스", None])

    # 시장 조치
    dept = info.get("dept") or ""
    if "관리" in dept or "환기" in dept:
        items.append([CHECK_LABELS[6], "f", dept, None])
    elif chk and chk.get("market"):
        m = chk["market"][0]
        items.append([CHECK_LABELS[6], "f", f"{m['nm']} ({fmt_date(m['d'])})", m["no"]])
    elif not chk or not chk.get("disc_ok"):
        items.append([CHECK_LABELS[6], "u", miss, None])
    else:
        items.append([CHECK_LABELS[6], "p", "해당 없음", None])

    states = [s for _, s, _, _ in items]
    if "f" in states:
        status = "f"
    elif chk is None:
        status = "n"
    elif "u" in states:
        status = "u"
    else:
        status = "p"
    return status, items


# ---------------------------------------------------------------- 결합/출력
def eok(v):
    return None if v is None else round(v / 1e8, 1)


def run_prices():
    listing, price_date = load_listing()
    caps = total_caps(listing)
    fin = load_json(FIN_PATH, {})
    checks = load_json(CHECK_PATH, {})
    fields = ["c", "n", "m", "f", "cap", "per", "rg", "fcf", "ocf", "capex", "ni", "rev", "rep", "fs",
              "op", "l1", "ck"]
    rows = []
    reports = {}
    for code, info in listing.items():
        if not is_target(code, info) or code not in fin:
            continue
        rec = fin[code]
        cap = caps.get(code, info["marcap"])
        if cap <= 0:
            continue
        ni = rec.get("ni_ttm")
        per = round(cap / ni, 2) if ni and ni > 0 else None
        rg = rec.get("rev_growth")
        l1, ck = evaluate_layer1(rec, checks.get(code), info)
        rows.append([
            code, info["name"], normalize_market(info["market"]), 1 if is_fin(info["name"]) else 0,
            eok(cap), per, None if rg is None else round(rg * 100, 1),
            eok(rec.get("fcf_ttm")), eok(rec.get("ocf_ttm")), eok(rec.get("capex_ttm")),
            eok(ni), eok(rec.get("rev_ttm")), rec.get("report"), rec.get("fs"),
            eok(rec.get("op_ttm")), l1, ck,
        ])
        reports[rec.get("report")] = reports.get(rec.get("report"), 0) + 1

    main_report = max(reports, key=reports.get) if reports else None
    out = {
        "meta": {
            "generated_at": now_kst().isoformat(timespec="minutes"),
            "price_date": price_date,
            "main_report": main_report,
            "count": len(rows),
            "checked": sum(1 for r in rows if r[15] != "n"),
            "unit": "억원",
        },
        "fields": fields,
        "rows": rows,
    }
    save_json(OUT_PATH, out, compact=True)
    log(f"화면 데이터 {len(rows)}개 저장 (주가 기준 {price_date}, 주 보고서 {main_report})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["financials", "checks", "prices", "all"], default="prices")
    ap.add_argument("--force", action="store_true", help="이미 수집한 종목도 다시 수집")
    ap.add_argument("--limit", type=int, help="테스트용: 수집 종목 수 제한")
    a = ap.parse_args()
    if a.mode in ("financials", "all"):
        run_financials(force=a.force, limit=a.limit)
    if a.mode in ("checks", "all"):
        run_checks(force=a.force, limit=a.limit)
    if a.mode in ("prices", "all"):
        run_prices()


if __name__ == "__main__":
    main()
