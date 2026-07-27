# -*- coding: utf-8 -*-
"""
SK하이닉스 투자 모니터링 대시보드 (Streamlit)
핵심 목적: 관련주 + 지수의 정규장/시간외 시세와 관련 뉴스를 한 화면에서 확인
실행: streamlit run streamlit_app.py
"""

import datetime as dt
import json
import os
import re
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import yfinance as yf
from bs4 import BeautifulSoup

KST = ZoneInfo("Asia/Seoul")
NY_TZ = ZoneInfo("America/New_York")


def now_kst():
    return dt.datetime.now(KST)


# ========================= CONFIG =========================

TOTAL_PLAN = 10
EARNINGS_DATE = "2026-07-29"  # 확정: 오전 9시 Conference Call

BUY_REVIEW_PRICE = 1_760_000
RISK_WARNING_PRICE = 1_500_000
KOSPI_SUPPORT = 6500
VIX_WARNING = 20
EARNINGS_DDAY_WARNING = 3

DOMESTIC = {
    "SK하이닉스": "000660",
    "삼성전자": "005930",
}

US_RELATED = {
    "마이크론(MU)": "MU",
    "샌디스크(SNDK)": "SNDK",
    "웨스턴디지털(WDC)": "WDC",
    "SK하이닉스 ADR": "SKHY",  # 2026-07-10 나스닥 상장 (구 OTC 티커 HXSCL 폐지)
    "AMD": "AMD",
    "엔비디아(NVDA)": "NVDA",
    "마벨(MRVL)": "MRVL",
    "테라다인(TER)": "TER",
}
US_RELATED_ALERT_SET = ["마이크론(MU)", "샌디스크(SNDK)", "웨스턴디지털(WDC)", "SK하이닉스 ADR"]
GAP_ALERT_THRESHOLD = 3  # 시간외 괴리 평균 알림 기준(%p)
GAP_HIGHLIGHT_THRESHOLD = 3  # 표 하이라이트 기준(%p)

INDICES = {
    "코스피": "^KS11",
    "코스닥": "^KQ11",
    "나스닥종합": "^IXIC",
    "S&P500": "^GSPC",
    "필라델피아반도체(SOX)": "^SOX",
}

RISK_INDICATORS = {
    "VIX": "^VIX",
    "WTI 국제유가": "CL=F",
}

TARGET_PRICE_DEFAULT = 2_500_000
STOP_LOSS_RATIO = 0.85  # 손절가 기본값 = 평단가 -15%
STOP_LOSS_NEAR_PCT = 2  # 손절가 근접 기준(%)

# 시간외 세션 예상 시간대 (참고용 — 실제 거래소 상태는 API 응답이 우선)
DOMESTIC_OVERTIME_START = dt.time(16, 0)
DOMESTIC_OVERTIME_END = dt.time(18, 0)
US_PREMARKET_START = dt.time(4, 0)
US_PREMARKET_END = dt.time(9, 30)
US_REGULAR_START = dt.time(9, 30)
US_REGULAR_END = dt.time(16, 0)
US_AFTERHOURS_START = dt.time(16, 0)
US_AFTERHOURS_END = dt.time(20, 0)

NEWS_QUERIES = ["SK하이닉스", "삼성전자 반도체"]
NEWS_TIME_PATTERN = re.compile(r"^(\d+(분|시간|일)\s*전|\d{4}\.\d{2}\.\d{2}\.?)$")

SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "latest_snapshot.json")

TRADES_PATH = os.path.join(os.path.dirname(__file__), "trades.csv")
SEED_TRADES = [
    {"ID": 1, "날짜": "2026-07-20", "수량": 2, "매수가": 1_776_000},
    {"ID": 2, "날짜": "2026-07-21", "수량": 1, "매수가": 1_817_000},
]

# 국내는 로그인 없이 조회 가능한 과거 시간외 이력 소스가 없어(네이버 실시간 API는
# 정규장 중엔 어제 시간외 정보를 전혀 돌려주지 않음, 직접 확인됨), 이 앱이 실제로
# 라이브 시간외가를 관측할 때마다 여기 캐시해두고 없을 때 최근값으로 재사용한다.
OVERTIME_CACHE_PATH = os.path.join(os.path.dirname(__file__), "overtime_cache.json")

st.set_page_config(page_title="SK하이닉스 투자 모니터링", layout="wide")


# ========================= 데이터 수집: 국내 =========================

def fetch_naver(code):
    """네이버 폴링 API. 정규장가/등락 + 시간외 단일가(있으면) 반환.
    실패 시 예외 발생 -> 호출부에서 pykrx로 폴백."""
    url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    res = requests.get(url, headers=headers, timeout=5)
    res.raise_for_status()
    item = res.json()["datas"][0]

    def to_num(s):
        return float(str(s).replace(",", ""))

    price = to_num(item["closePrice"])
    change_amt = to_num(item["compareToPreviousClosePrice"])
    change_pct = to_num(item["fluctuationsRatio"])
    if item.get("compareToPreviousPrice", {}).get("name") == "FALLING":
        change_amt = -abs(change_amt)
        change_pct = -abs(change_pct)

    overtime_price = None
    overtime_pct = None
    overtime_traded_at = None
    over = item.get("overMarketPriceInfo")
    if over is None:
        overtime_status = "시간외 정보 없음(API 응답에 필드 자체가 없음)"
    else:
        status = over.get("overMarketStatus")
        session_type = over.get("tradingSessionType", "-")
        traded_at = over.get("localTradedAt", "")
        traded_date = traded_at[:10] if traded_at else None
        today_str = now_kst().strftime("%Y-%m-%d")

        # 네이버 API는 정규장 중에도 overMarketPriceInfo를 채워서 주는데, 이때
        # tradingSessionType이 REGULAR_MARKET이라 시간외가 아니라 정규장가와 동일하다.
        # 실제 시간외(장전/장후 단일가)만 골라내려면 세션 종류를 반드시 확인해야 한다.
        is_overtime_session = session_type in ("AFTER_MARKET", "BEFORE_MARKET")

        if over.get("overPrice") and traded_date == today_str and is_overtime_session:
            # 세션이 끝난(CLOSE) 뒤에도 API가 마지막 시간외 체결가를 계속 제공하므로
            # OPEN 여부와 무관하게 값을 보여주되, 오늘 날짜 체결이 아니면(전일 잔여 데이터)
            # 장중에 잘못 섞이지 않도록 제외한다.
            overtime_price = to_num(over["overPrice"])
            overtime_pct = to_num(over.get("fluctuationsRatio", 0))
            if over.get("compareToPreviousPrice", {}).get("name") == "FALLING":
                overtime_pct = -abs(overtime_pct)
            overtime_traded_at = traded_at
            if status == "OPEN":
                overtime_status = f"진행중({session_type}, {traded_at})"
            else:
                overtime_status = f"마감({session_type}, 마지막 체결 {traded_at})"
        elif over.get("overPrice") and not is_overtime_session:
            overtime_status = f"정규장 데이터라 시간외 아님(세션타입={session_type}, 제외함)"
        elif over.get("overPrice") and traded_date != today_str:
            overtime_status = f"오늘자 시간외 데이터 아님(제외함, 마지막 체결 {traded_at})"
        else:
            overtime_status = f"시간외 가격 없음(overMarketStatus={status})"

    return price, change_pct, change_amt, "naver", overtime_price, overtime_pct, overtime_status, overtime_traded_at


def fetch_pykrx(code):
    """pykrx 일별 데이터 폴백 (실시간 아님, 최근 종가 기준. 시간외 데이터 없음)."""
    from pykrx import stock

    today = now_kst().date()
    fromdate = (today - dt.timedelta(days=15)).strftime("%Y%m%d")
    todate = today.strftime("%Y%m%d")
    df = stock.get_market_ohlcv_by_date(fromdate, todate, code)
    if df is None or len(df) < 2:
        raise ValueError("pykrx 데이터 부족")
    close = float(df["종가"].iloc[-1])
    prev_close = float(df["종가"].iloc[-2])
    change_amt = close - prev_close
    change_pct = change_amt / prev_close * 100
    return close, change_pct, change_amt, "pykrx(일별)", None, None, "pykrx 폴백 사용(시간외 데이터 미제공)", None


def fetch_domestic(code):
    try:
        return fetch_naver(code)
    except Exception:
        return fetch_pykrx(code)


def load_overtime_cache():
    if not os.path.exists(OVERTIME_CACHE_PATH):
        return {}
    try:
        with open(OVERTIME_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_overtime_cache(cache):
    try:
        with open(OVERTIME_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 클라우드 등 쓰기 불가 환경에서는 조용히 무시


def format_iso_to_kst(iso_str):
    """ISO 타임스탬프 문자열을 'YYYY-MM-DD HH:MM' KST 표기로 변환. 실패 시 원문 반환."""
    if not iso_str:
        return "시각 불명"
    try:
        d = dt.datetime.fromisoformat(iso_str)
        if d.tzinfo is None:
            d = d.replace(tzinfo=KST)
        return d.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str


def fetch_investor_flows(code):
    """네이버 증권 페이지의 외국인/기관 순매매수량(주, 가장 최근 거래일). 로그인 불필요."""
    url = f"https://finance.naver.com/item/frgn.naver?code={code}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    res = requests.get(url, headers=headers, timeout=5)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    tables = soup.find_all("table", class_="type2")
    if len(tables) < 2:
        raise ValueError("네이버 수급 테이블을 찾을 수 없음(페이지 구조 변경 가능성)")

    date_pattern = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")
    for tr in tables[1].find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all("td")]
        if len(cells) >= 7 and date_pattern.match(cells[0]):
            inst = int(cells[5].replace(",", "").replace("+", "") or 0)
            foreign = int(cells[6].replace(",", "").replace("+", "") or 0)
            basis_date = cells[0].replace(".", "-")
            return {"기관": inst, "외국인": foreign}, basis_date

    raise ValueError("네이버 수급 데이터 파싱 실패(최근 거래일 행을 찾지 못함)")


# ========================= 데이터 수집: 해외 =========================

def fetch_yf(ticker):
    """정규장 등락(fast_info 기준). 지수/리스크 지표 및 시간외 폴백에 사용."""
    t = yf.Ticker(ticker)
    price = None
    prev = None
    try:
        fi = t.fast_info
        price = getattr(fi, "last_price", None)
        prev = getattr(fi, "previous_close", None)
    except Exception:
        pass

    if price is None or prev is None:
        hist = t.history(period="5d")
        if hist is None or len(hist) < 2:
            raise ValueError("데이터 없음")
        price = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])

    price = float(price)
    prev = float(prev)
    change_amt = price - prev
    change_pct = change_amt / prev * 100
    return price, change_pct, change_amt


def _fetch_extended_via_info(ticker):
    """1차 방법: yfinance Ticker.info의 marketState/preMarketPrice/postMarketPrice 사용.
    지금 당장 진행 중인 시간외가만 반환한다(과거 값 조회는 _fetch_extended_via_history 담당)."""
    t = yf.Ticker(ticker)
    info = t.info

    price = info.get("regularMarketPrice")
    prev = info.get("regularMarketPreviousClose") or info.get("previousClose")
    if price is None or prev is None:
        raise ValueError("info 응답에 regularMarketPrice/previousClose 없음")

    price = float(price)
    prev = float(prev)
    change_amt = price - prev
    change_pct = change_amt / prev * 100

    ext_price = None
    ext_pct = None
    ext_traded_at = None
    state = info.get("marketState")

    if state == "PRE":
        if info.get("preMarketPrice") is not None:
            ext_price = float(info["preMarketPrice"])
            ext_pct = info.get("preMarketChangePercent")
            ext_label = "프리마켓(info, 진행중)"
            epoch = info.get("preMarketTime")
            if epoch:
                ext_traded_at = dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).astimezone(NY_TZ).isoformat()
        else:
            ext_label = "프리마켓(info, 가격 데이터 없음)"
    elif state in ("POST", "POSTPOST"):
        if info.get("postMarketPrice") is not None:
            ext_price = float(info["postMarketPrice"])
            ext_pct = info.get("postMarketChangePercent")
            ext_label = "애프터마켓(info, 진행중)"
            epoch = info.get("postMarketTime")
            if epoch:
                ext_traded_at = dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).astimezone(NY_TZ).isoformat()
        else:
            ext_label = "애프터마켓(info, 가격 데이터 없음)"
    elif state == "REGULAR":
        ext_label = "정규장 중(info, 시간외 없음)"
    elif state == "CLOSED":
        ext_label = "휴장(info, 시간외 없음)"
    elif state is None:
        ext_label = "상태 정보 없음(info, marketState 필드 없음)"
    else:
        ext_label = f"상태불명(info, marketState={state})"

    return price, change_pct, change_amt, ext_price, ext_pct, ext_label, ext_traded_at


def _fetch_extended_via_history(ticker):
    """최근 5일치 1분봉(prepost=True)에서 정규장 시간대(09:30~16:00 ET, 평일)가 아닌
    가장 최근 봉을 찾아 시간외가로 사용한다. 지금 당장 진행 중인 세션이 아니어도
    '최근 기준'으로 반환하며, 그 봉의 체결 시각(NY 기준)을 항상 함께 반환한다."""
    price, change_pct, change_amt = fetch_yf(ticker)

    t = yf.Ticker(ticker)
    hist = t.history(period="5d", interval="1m", prepost=True)
    if hist is None or hist.empty:
        raise ValueError("1분봉 데이터 없음(history)")

    idx = hist.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    idx_ny = idx.tz_convert(NY_TZ)

    last_i = None
    for i in range(len(idx_ny) - 1, -1, -1):
        t_ny = idx_ny[i]
        if t_ny.weekday() >= 5:
            continue
        tm = t_ny.time()
        if tm < US_REGULAR_START or tm >= US_REGULAR_END:
            last_i = i
            break

    if last_i is None:
        return price, change_pct, change_amt, None, None, "시간외 데이터 없음(최근 5일 이내 프리/애프터마켓 봉 없음)", None

    t_ny = idx_ny[last_i]
    last_close = float(hist["Close"].iloc[last_i])
    ext_pct = (last_close - price) / price * 100 if price else None

    now_ny = dt.datetime.now(NY_TZ)
    is_live = t_ny.date() == now_ny.date() and (now_ny - t_ny.to_pydatetime()).total_seconds() < 300
    session_name = "프리마켓" if t_ny.time() < US_REGULAR_START else "애프터마켓"
    ts_display = t_ny.strftime("%Y-%m-%d %H:%M ET")
    if is_live:
        label = f"{session_name}(history, 진행중, {ts_display})"
    else:
        label = f"{session_name}(history, 최근 기준 {ts_display})"

    return price, change_pct, change_amt, last_close, ext_pct, label, t_ny.isoformat()


def fetch_yf_extended(ticker):
    """정규장 등락 + 시간외(프리마켓/애프터마켓) 가격, 그 체결 시각, 캐시(과거값) 여부.
    지금 당장 진행 중인 시간외가 있으면 그 값을(is_cached=False), 없으면 최근 5일 이내
    가장 최근 프리/애프터마켓 값을 '최근 기준'으로(is_cached=True) 반환한다.
    반환: price, change_pct, change_amt, ext_price, ext_pct, ext_label, ext_traded_at, is_cached"""
    info_error = None
    price = change_pct = change_amt = ext_price = ext_pct = ext_traded_at = None
    ext_label = None
    try:
        price, change_pct, change_amt, ext_price, ext_pct, ext_label, ext_traded_at = _fetch_extended_via_info(ticker)
    except Exception as e:
        info_error = e

    if ext_price is not None:
        return price, change_pct, change_amt, ext_price, ext_pct, ext_label, ext_traded_at, False

    try:
        h_price, h_change_pct, h_change_amt, h_ext_price, h_ext_pct, h_label, h_traded_at = _fetch_extended_via_history(ticker)
    except Exception as e_hist:
        if price is None:
            raise ValueError(f"info 실패({info_error}) / history 실패({e_hist})")
        return price, change_pct, change_amt, None, None, f"{ext_label}(최근 시간외 조회도 실패: {e_hist})", None, False

    final_price = price if price is not None else h_price
    final_change_pct = change_pct if price is not None else h_change_pct
    final_change_amt = change_amt if price is not None else h_change_amt
    return final_price, final_change_pct, final_change_amt, h_ext_price, h_ext_pct, h_label, h_traded_at, (h_ext_price is not None)


def fetch_naver_news(query, count=5):
    """네이버 뉴스 검색 결과 상위 N개. 제목/링크/상대시각만 반환 (요약/논평 없음)."""
    url = f"https://search.naver.com/search.naver?where=news&query={quote(query)}&sort=1"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    res = requests.get(url, headers=headers, timeout=5)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    spans = soup.select("span.sds-comps-text-type-headline1")[:count]
    texts = soup.find_all(string=True)

    results = []
    for sp in spans:
        a = sp.find_parent("a")
        title = sp.get_text(strip=True)
        href = a.get("href") if a else None

        time_text = "-"
        try:
            idx = texts.index(sp.string)
            for t in texts[idx + 1: idx + 15]:
                s = t.strip()
                if NEWS_TIME_PATTERN.match(s):
                    time_text = s
                    break
        except ValueError:
            pass

        results.append({"제목": title, "링크": href, "시각": time_text})

    return results


# ========================= 전체 데이터 수집 + 알림 판정 =========================

def collect_all(avg_price, shares_owned):
    collected_at = now_kst().strftime("%Y-%m-%d %H:%M:%S (KST)")
    items = []
    alerts = []

    sk_price = None
    kospi_price = None
    us_related_pct = {}
    vix_price = None

    # 국내 (정규장 + 시간외, 라이브 시간외가 없으면 이 앱이 관측해둔 최근값으로 대체)
    overtime_cache = load_overtime_cache()
    overtime_cache_updated = False
    for name, code in DOMESTIC.items():
        try:
            price, pct, amt, source, ot_price, ot_pct, ot_status, ot_traded_at = fetch_domestic(code)
            if code == "000660":
                sk_price = price

            is_cached_ot = False
            if ot_price is not None:
                overtime_cache[name] = {
                    "price": ot_price, "pct": ot_pct,
                    "traded_at": ot_traded_at or now_kst().isoformat(),
                }
                overtime_cache_updated = True
            else:
                cached = overtime_cache.get(name)
                if cached:
                    ot_price = cached["price"]
                    ot_pct = cached["pct"]
                    ts_display = format_iso_to_kst(cached["traded_at"])
                    ot_status = f"최근 기준({ts_display} 마지막 체결) — {ot_status}"
                    is_cached_ot = True

            items.append({
                "구분": "국내", "항목": name, "현재가": round(price, 2),
                "등락률(%)": round(pct, 2), "등락폭": round(amt, 2),
                "시간외상태": ot_status,
                "시간외가": round(ot_price, 2) if ot_price is not None else None,
                "시간외등락률(%)": round(ot_pct, 2) if ot_pct is not None else None,
                "시간외캐시여부": is_cached_ot,
                "출처": source, "알림": "",
            })
        except Exception as e:
            items.append({
                "구분": "국내", "항목": name, "현재가": None,
                "등락률(%)": None, "등락폭": None,
                "시간외상태": f"수집실패: {e}",
                "시간외가": None, "시간외등락률(%)": None,
                "시간외캐시여부": False,
                "출처": "실패", "알림": f"수집실패: {e}",
            })
    if overtime_cache_updated:
        save_overtime_cache(overtime_cache)

    # 투자자별 매매동향 (외국인/기관 순매매수량, 주 — 네이버 증권, 로그인 불필요)
    investor_flows = {}
    for name, code in DOMESTIC.items():
        try:
            flows, basis_date = fetch_investor_flows(code)
            investor_flows[name] = {"basis_date": basis_date, "values": flows, "error": None}
        except Exception as e:
            investor_flows[name] = {"basis_date": None, "values": {}, "error": str(e)}

    # 관련주 (정규장 + 시간외 + 괴리, 라이브 시간외가 없으면 최근 5일 이내 최근값을 '최근 기준'으로 표시)
    related_gaps = {}
    related_gaps_any_cached = False
    for name, ticker in US_RELATED.items():
        try:
            price, pct, amt, ext_price, ext_pct, ext_label, ext_traded_at, ext_is_cached = fetch_yf_extended(ticker)
            if name in US_RELATED_ALERT_SET:
                us_related_pct[name] = pct
            gap = None
            if ext_pct is not None:
                gap = round(ext_pct - pct, 2)
                # 8개 종목 전체 평균(avg_gap)에는 캐시(최근 기준) 값도 포함한다 — 주말/장
                # 마감 직후처럼 8종목 모두 비실시간일 때 평균이 통째로 비지 않도록 한다.
                # 대신 캐시 값이 하나라도 섞이면 아래 알림 문구에 그 사실을 명시한다.
                related_gaps[name] = gap
                if ext_is_cached:
                    related_gaps_any_cached = True
            items.append({
                "구분": "관련주", "항목": name, "현재가": round(price, 2),
                "등락률(%)": round(pct, 2), "등락폭": round(amt, 2),
                "시간외구분": ext_label,
                "시간외가": round(ext_price, 2) if ext_price is not None else None,
                "시간외등락률(%)": round(ext_pct, 2) if ext_pct is not None else None,
                "괴리(%p)": gap,
                "시간외캐시여부": ext_is_cached,
                "출처": "yfinance", "알림": "",
            })
        except Exception as e:
            items.append({
                "구분": "관련주", "항목": name, "현재가": None,
                "등락률(%)": None, "등락폭": None,
                "시간외구분": f"수집실패: {e}", "시간외가": None, "시간외등락률(%)": None,
                "괴리(%p)": None, "시간외캐시여부": False,
                "출처": "데이터 없음", "알림": f"수집실패: {e}",
            })

    # 지수
    for name, ticker in INDICES.items():
        try:
            price, pct, amt = fetch_yf(ticker)
            if name == "코스피":
                kospi_price = price
            items.append({
                "구분": "지수", "항목": name, "현재가": round(price, 2),
                "등락률(%)": round(pct, 2), "등락폭": round(amt, 2),
                "출처": "yfinance", "알림": "",
            })
        except Exception as e:
            items.append({
                "구분": "지수", "항목": name, "현재가": None,
                "등락률(%)": None, "등락폭": None, "출처": "데이터 없음",
                "알림": f"수집실패: {e}",
            })

    # 리스크 지표
    for name, ticker in RISK_INDICATORS.items():
        try:
            price, pct, amt = fetch_yf(ticker)
            if name == "VIX":
                vix_price = price
            items.append({
                "구분": "리스크", "항목": name, "현재가": round(price, 2),
                "등락률(%)": round(pct, 2), "등락폭": round(amt, 2),
                "출처": "yfinance", "알림": "",
            })
        except Exception as e:
            items.append({
                "구분": "리스크", "항목": name, "현재가": None,
                "등락률(%)": None, "등락폭": None, "출처": "데이터 없음",
                "알림": f"수집실패: {e}",
            })

    # ---- 알림 조건 판정 ----
    def mark(item_name, tag):
        for it in items:
            if it["항목"] == item_name:
                it["알림"] = (it["알림"] + "/" if it["알림"] else "") + tag

    if sk_price is not None:
        if avg_price:
            diff_pct = (sk_price - avg_price) / avg_price * 100
            if abs(diff_pct) >= 3:
                alerts.append(f"평단가 대비 {diff_pct:+.2f}% (평단가 {avg_price:,}원 / 현재가 {sk_price:,.0f}원)")
                mark("SK하이닉스", "평단가±3%")
        if sk_price <= RISK_WARNING_PRICE:
            alerts.append(f"하방 리스크 경고: SK하이닉스 {sk_price:,.0f}원 (기준 {RISK_WARNING_PRICE:,}원 이하)")
            mark("SK하이닉스", "하방리스크경고")
        elif sk_price <= BUY_REVIEW_PRICE:
            alerts.append(f"매수 검토 구간: SK하이닉스 {sk_price:,.0f}원 (기준 {BUY_REVIEW_PRICE:,}원 이하)")
            mark("SK하이닉스", "매수검토구간")

    if kospi_price is not None and kospi_price <= KOSPI_SUPPORT:
        alerts.append(f"지지선 이탈: 코스피 {kospi_price:,.2f} (기준 {KOSPI_SUPPORT:,})")
        mark("코스피", "지지선이탈")

    if us_related_pct:
        avg_pct = sum(us_related_pct.values()) / len(us_related_pct)
        if abs(avg_pct) >= 3:
            detail = ", ".join(f"{k} {v:+.2f}%" for k, v in us_related_pct.items())
            alerts.append(f"관련주 신호 발생, 지수보다 우선 참고: 평균 {avg_pct:+.2f}% ({detail})")
            for k in us_related_pct:
                mark(k, "관련주신호")

    avg_gap = None
    if related_gaps:
        avg_gap = sum(related_gaps.values()) / len(related_gaps)
        if abs(avg_gap) >= GAP_ALERT_THRESHOLD:
            direction = "강세" if avg_gap > 0 else "약세"
            detail = ", ".join(f"{k} {v:+.2f}%p" for k, v in related_gaps.items())
            caveat = " (일부 종목은 실시간이 아닌 최근 관측값 포함)" if related_gaps_any_cached else ""
            alerts.append(
                f"관련주 시간외 {direction}, 내일 개장 참고 신호{caveat}: "
                f"평균 괴리 {avg_gap:+.2f}%p ({detail})"
            )
            for k in related_gaps:
                mark(k, "시간외괴리")

    if vix_price is not None and vix_price >= VIX_WARNING:
        alerts.append(f"변동성 경고: VIX {vix_price:.2f} (기준 {VIX_WARNING} 이상)")
        mark("VIX", "변동성경고")

    earnings_date = dt.date.fromisoformat(EARNINGS_DATE)
    dday = (earnings_date - now_kst().date()).days
    if 0 <= dday <= EARNINGS_DDAY_WARNING:
        alerts.append(f"매수 자제 구간 안내: 실적발표({EARNINGS_DATE})까지 D-{dday}")

    pnl_pct = None
    pnl_amt = None
    if sk_price is not None and avg_price:
        pnl_pct = (sk_price - avg_price) / avg_price * 100
        pnl_amt = (sk_price - avg_price) * shares_owned

    # ---- 관련 뉴스 ----
    news = {}
    for q in NEWS_QUERIES:
        try:
            news[q] = fetch_naver_news(q, count=5)
        except Exception:
            news[q] = []

    return {
        "collected_at": collected_at,
        "sk_price": sk_price,
        "pnl_pct": pnl_pct,
        "pnl_amt": pnl_amt,
        "dday": dday,
        "alerts": alerts,
        "items": items,
        "news": news,
        "avg_gap": avg_gap,
        "investor_flows": investor_flows,
    }


def save_snapshot(data):
    try:
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 클라우드 등 쓰기 불가 환경에서는 조용히 무시


def load_trades():
    if not os.path.exists(TRADES_PATH):
        seed_df = pd.DataFrame(SEED_TRADES)
        try:
            seed_df.to_csv(TRADES_PATH, index=False, encoding="utf-8-sig")
        except Exception:
            pass
        return seed_df
    try:
        df = pd.read_csv(TRADES_PATH)
    except Exception:
        return pd.DataFrame(SEED_TRADES)

    if "ID" not in df.columns:
        # 이전 버전(ID 없이 저장된) trades.csv 마이그레이션: 행 순서대로 ID 부여 후 재저장
        df.insert(0, "ID", range(1, len(df) + 1))
        try:
            df.to_csv(TRADES_PATH, index=False, encoding="utf-8-sig")
        except Exception:
            pass
    return df


def next_trade_id(trades_df):
    if trades_df is None or trades_df.empty or "ID" not in trades_df.columns or trades_df["ID"].isna().all():
        return 1
    return int(trades_df["ID"].max()) + 1


def save_trade(date_str, qty, price):
    """매수 기록 추가 폼에서 사용: 새 ID를 부여해 한 행 append."""
    existing = load_trades()
    new_id = next_trade_id(existing)
    row = pd.DataFrame([{"ID": new_id, "날짜": date_str, "수량": qty, "매수가": price}])
    header = not os.path.exists(TRADES_PATH)
    row.to_csv(TRADES_PATH, mode="a", header=header, index=False, encoding="utf-8-sig")


def save_trades_df(edited_df):
    """수정/삭제 표(st.data_editor)에서 사용: 전체 표를 통째로 검증 후 덮어쓴다.
    새로 추가된 행(ID 비어있음)에는 새 ID를 부여하고, 필수값이 빈 행(작성 중인 신규 행 등)은 제외한다."""
    df = edited_df.copy()
    if "ID" not in df.columns:
        df.insert(0, "ID", pd.NA)

    missing_id_mask = df["ID"].isna()
    if missing_id_mask.any():
        next_id = next_trade_id(df)
        for idx in df.index[missing_id_mask]:
            df.loc[idx, "ID"] = next_id
            next_id += 1

    df = df.dropna(subset=["날짜", "수량", "매수가"])
    if df.empty:
        df = pd.DataFrame(columns=["ID", "날짜", "수량", "매수가"])
    else:
        df["ID"] = df["ID"].astype(int)
        df["수량"] = df["수량"].astype(int)
        df["매수가"] = df["매수가"].astype(int)

    df.to_csv(TRADES_PATH, index=False, encoding="utf-8-sig")
    return df


def compute_position(trades_df):
    if trades_df is None or trades_df.empty:
        return 0, 0
    shares = int(trades_df["수량"].sum())
    if shares <= 0:
        return 0, 0
    avg = (trades_df["수량"] * trades_df["매수가"]).sum() / shares
    return round(avg), shares


def refresh_data(avg_price, shares_owned):
    data = collect_all(avg_price, shares_owned)
    save_snapshot(data)
    return data


def check_stop_loss(sk_price, stop_loss_price):
    """None | 'breach' | 'near' — 순수 함수, 손절가 이탈/근접 여부만 판정."""
    if sk_price is None:
        return None
    if sk_price <= stop_loss_price:
        return "breach"
    if sk_price <= stop_loss_price * (1 + STOP_LOSS_NEAR_PCT / 100):
        return "near"
    return None


def check_target(sk_price, target_price):
    """None | 'reached' — 순수 함수, 목표가 도달 여부만 판정."""
    if sk_price is None or target_price <= 0:
        return None
    if sk_price >= target_price:
        return "reached"
    return None


def style_df(sub):
    """숫자 컬럼에 천단위 콤마 서식 적용 (금액류 0자리, %류 2자리), 결측치는 '-'로 표시."""
    fmt = {}
    for col in sub.columns:
        if sub[col].dtype.kind not in "if":
            continue
        fmt[col] = "{:,.2f}" if "%" in col else "{:,.0f}"
    return sub.style.format(fmt, na_rep="-")


def get_session_status():
    """시계 기준 예상 시간외 세션 여부 (참고용). 실제 거래소 상태는 API 응답이 우선."""
    now_kr = now_kst()
    kr_overtime = (
        now_kr.weekday() < 5
        and DOMESTIC_OVERTIME_START <= now_kr.time() < DOMESTIC_OVERTIME_END
    )

    now_ny = dt.datetime.now(NY_TZ)
    is_weekday_ny = now_ny.weekday() < 5
    us_pre = is_weekday_ny and US_PREMARKET_START <= now_ny.time() < US_PREMARKET_END
    us_after = is_weekday_ny and US_AFTERHOURS_START <= now_ny.time() < US_AFTERHOURS_END

    return {
        "kr_overtime": kr_overtime,
        "kr_time_str": now_kr.strftime("%H:%M (%a)"),
        "us_pre": us_pre,
        "us_after": us_after,
        "us_time_str": now_ny.strftime("%H:%M (%a)"),
    }


# ========================= UI =========================

st.title("SK하이닉스 투자 모니터링")

# 매수 기록(trades.csv) 기반 평단가/보유주수 계산
trades_df = load_trades()
AVG_PRICE, SHARES_OWNED = compute_position(trades_df)
STOP_LOSS_DEFAULT = round(AVG_PRICE * STOP_LOSS_RATIO) if AVG_PRICE else 0

if "data" not in st.session_state:
    st.session_state["data"] = refresh_data(AVG_PRICE, SHARES_OWNED)

col_btn, col_time = st.columns([1, 3])
with col_btn:
    if st.button("현재 시간 반영", type="primary"):
        st.session_state["data"] = refresh_data(AVG_PRICE, SHARES_OWNED)

data = st.session_state["data"]
sk_price = data["sk_price"]

with col_time:
    st.write(f"현재 시각: {now_kst().strftime('%Y-%m-%d %H:%M:%S')} (KST)")
    st.write(f"마지막 갱신 시각: {data['collected_at']}")

# ---- SK하이닉스 정규장가 / 시간외가 (최우선 표시) ----
sk_item = next((it for it in data["items"] if it["항목"] == "SK하이닉스"), None)

col_reg, col_ot = st.columns(2)
with col_reg:
    if sk_item and sk_item["현재가"] is not None:
        st.metric(
            "SK하이닉스 정규장가",
            f"{sk_item['현재가']:,.0f}원",
            f"{sk_item['등락률(%)']:+.2f}%",
        )
    else:
        st.metric("SK하이닉스 정규장가", "N/A")
with col_ot:
    if sk_item and sk_item.get("시간외가") is not None:
        st.metric(
            "SK하이닉스 시간외가",
            f"{sk_item['시간외가']:,.0f}원",
            f"{sk_item['시간외등락률(%)']:+.2f}%",
        )
    else:
        st.metric("SK하이닉스 시간외가", "데이터 없음")
    st.caption(sk_item.get("시간외상태", "") if sk_item else "")

# ---- 시간외 세션 여부 ----
session = get_session_status()
col_sess1, col_sess2 = st.columns(2)
with col_sess1:
    kr_label = "예" if session["kr_overtime"] else "아니오"
    st.write(
        f"현재 시간외 세션 여부 (국내, 16:00~18:00 KST): **{kr_label}** "
        f"(현재 KST {session['kr_time_str']})"
    )
with col_sess2:
    if session["us_pre"]:
        us_label = "예 (프리마켓)"
    elif session["us_after"]:
        us_label = "예 (애프터마켓)"
    else:
        us_label = "아니오"
    st.write(
        f"현재 시간외 세션 여부 (미국, 프리 04:00~09:30 / 애프터 16:00~20:00 ET): **{us_label}** "
        f"(현재 뉴욕시간 {session['us_time_str']})"
    )
st.caption(
    "위는 시계 기준 예상치입니다. 실제 거래소 상태는 아래 국내/관련주 표의 "
    "\"시간외상태\"/\"시간외구분\" 열에 표시되는 값이 최종 근거입니다."
)

# ---- 상단 요약 ----
# 손익 계산은 '지금 진행 중이거나 오늘 마감된 라이브 시간외가'가 있을 때만 그 값으로
# 전환한다. 화면 상단 메트릭에는 캐시된(며칠 지난) 최근 시간외가도 참고로 보여주지만,
# 그 값으로 손익을 계산하면 과거 가격을 오늘 손익인 것처럼 보여주는 오류가 되므로 제외한다.
sk_overtime_price_live = (
    sk_item.get("시간외가") if sk_item and not sk_item.get("시간외캐시여부") else None
)
pnl_basis_price = sk_overtime_price_live if sk_overtime_price_live is not None else sk_price
pnl_basis_label = "시간외 기준" if sk_overtime_price_live is not None else "정규장 기준"

if pnl_basis_price is not None and AVG_PRICE:
    live_pnl_pct = (pnl_basis_price - AVG_PRICE) / AVG_PRICE * 100
    live_pnl_amt = (pnl_basis_price - AVG_PRICE) * SHARES_OWNED
else:
    live_pnl_pct = None
    live_pnl_amt = None

m2, m3, m4 = st.columns(3)
m2.metric(f"평단가 대비 손익률 ({pnl_basis_label})", f"{live_pnl_pct:+.2f}%" if live_pnl_pct is not None else "N/A")
m3.metric(
    f"평가손익 ({pnl_basis_label}, 보유 {SHARES_OWNED}주)",
    f"{live_pnl_amt:+,.0f}원" if live_pnl_amt is not None else "N/A",
)
m4.metric("실적발표 D-day", f"D-{data['dday']}" if data['dday'] >= 0 else f"D+{-data['dday']}")

summary_caption = f"평단가 {AVG_PRICE:,}원 / 보유 {SHARES_OWNED}주 (총 계획 {TOTAL_PLAN}주) / 실적발표일 {EARNINGS_DATE}"
if pnl_basis_price is not None:
    summary_caption += f" / 손익 계산 기준가: {pnl_basis_price:,.0f}원 ({pnl_basis_label})"
st.caption(summary_caption)

# ---- 알림 배너 ----
if data["alerts"]:
    for a in data["alerts"]:
        st.error(a)
else:
    st.success("조건 충족 알림 없음")

# ---- 목표가 / 손절가 ----
st.divider()
st.subheader("목표가 / 손절가")

if "target_price" not in st.session_state:
    st.session_state["target_price"] = TARGET_PRICE_DEFAULT
if "stop_loss_price" not in st.session_state:
    st.session_state["stop_loss_price"] = STOP_LOSS_DEFAULT

col_tp, col_sl = st.columns(2)
with col_tp:
    target_price = st.number_input("목표가(원)", min_value=0, step=10_000, key="target_price")
with col_sl:
    stop_loss_price = st.number_input("손절가(원)", min_value=0, step=10_000, key="stop_loss_price")

if sk_price is not None:
    upside_pct = (target_price - sk_price) / sk_price * 100
    st.write(f"목표가까지 남은 상승률: {upside_pct:+.2f}%")

    target_state = check_target(sk_price, target_price)
    if target_state == "reached":
        st.success(f"목표가 도달: 현재가 {sk_price:,.0f}원 ≥ 목표가 {target_price:,.0f}원")

    stop_state = check_stop_loss(sk_price, stop_loss_price)
    if stop_state == "breach":
        st.error(f"손절가 이탈: 현재가 {sk_price:,.0f}원 ≤ 손절가 {stop_loss_price:,.0f}원")
    elif stop_state == "near":
        st.warning(
            f"손절 검토 구간: 현재가 {sk_price:,.0f}원 "
            f"(손절가 {stop_loss_price:,.0f}원 +{STOP_LOSS_NEAR_PCT}% 이내)"
        )
else:
    st.write("SK하이닉스 현재가 데이터 없음")

# ---- 매수 기록 관리 ----
st.divider()
st.subheader("매수 기록 관리")

with st.form("trade_form", clear_on_submit=True):
    col_d, col_q, col_p = st.columns(3)
    with col_d:
        trade_date = st.date_input("날짜", value=now_kst().date())
    with col_q:
        trade_qty = st.number_input("수량(주)", min_value=1, step=1, value=1)
    with col_p:
        trade_price = st.number_input("매수가(원)", min_value=0, step=1_000, value=0)
    submitted = st.form_submit_button("매수 기록 추가")
    if submitted:
        save_trade(trade_date.strftime("%Y-%m-%d"), int(trade_qty), int(trade_price))
        st.success(f"{trade_date} {trade_qty}주 @ {trade_price:,}원 기록 저장됨")
        st.rerun()

with st.expander("매수 기록 전체 보기 / 수정 / 삭제", expanded=False):
    st.caption("셀을 더블클릭해 값을 고쳐 쓰거나, 행 왼쪽을 선택 후 삭제(휴지통) 아이콘으로 지울 수 있습니다. ID는 자동 부여되며 수정할 수 없습니다.")
    edited_trades_df = st.data_editor(
        trades_df,
        width="stretch",
        hide_index=True,
        num_rows="dynamic",
        disabled=["ID"],
        column_config={
            "ID": st.column_config.NumberColumn("ID"),
            "날짜": st.column_config.TextColumn("날짜", help="YYYY-MM-DD"),
            "수량": st.column_config.NumberColumn("수량(주)", min_value=1, step=1),
            "매수가": st.column_config.NumberColumn("매수가(원)", min_value=0, step=1_000),
        },
        key="trades_editor",
    )
    if st.button("매수 기록 변경사항 저장"):
        saved_df = save_trades_df(edited_trades_df)
        st.success(f"매수 기록 {len(saved_df)}건 저장됨")
        st.rerun()

    st.download_button(
        label="trades.csv 다운로드",
        data=trades_df.to_csv(index=False).encode("utf-8-sig"),
        file_name="trades.csv",
        mime="text/csv",
    )

# ---- 데이터 표 ----
df = pd.DataFrame(data["items"])


def relabel_cache_flag(sub):
    """'시간외캐시여부'(bool, 내부 로직용)를 표시용 한글 라벨 컬럼으로 변환."""
    if "시간외캐시여부" in sub.columns:
        sub = sub.copy()
        sub["시간외가 구분"] = sub["시간외캐시여부"].map({True: "최근 기준(과거)", False: "실시간"})
        sub = sub.drop(columns=["시간외캐시여부"])
    return sub


def show_section(title, category):
    st.subheader(title)
    sub = df[df["구분"] == category].drop(columns=["구분"])
    sub = sub.dropna(axis=1, how="all")
    sub = relabel_cache_flag(sub)
    if sub.empty:
        st.write("데이터 없음")
    else:
        st.dataframe(style_df(sub), width="stretch", hide_index=True)


st.divider()
show_section("국내 (정규장 + 시간외)", "국내")

# ---- 투자자별 매매동향 (외국인/기관 순매매수량) ----
st.subheader("투자자별 매매동향 (외국인/기관 순매매수량)")
st.caption(f"수급 데이터 마지막 갱신 시각: {data['collected_at']}")
st.caption("출처: 네이버 증권(finance.naver.com), 로그인 불필요. 단위: 주(순매매수량)")

flow_rows = []
today_str = now_kst().strftime("%Y-%m-%d")
for name in DOMESTIC:
    info = data["investor_flows"].get(name, {})
    if info.get("error"):
        flow_rows.append({
            "종목": name, "외국인(주)": None, "기관(주)": None,
            "기준일": None, "비고": f"수집실패: {info['error']}",
        })
    else:
        vals = info.get("values", {})
        basis_date = info.get("basis_date")
        note = "" if basis_date == today_str else "당일 미반영, 최근 거래일 기준"
        flow_rows.append({
            "종목": name,
            "외국인(주)": vals.get("외국인"),
            "기관(주)": vals.get("기관"),
            "기준일": basis_date,
            "비고": note,
        })

flow_df = pd.DataFrame(flow_rows)


def color_pos_neg(val):
    if pd.isna(val):
        return ""
    if val > 0:
        return "color: #d62728; font-weight: bold"  # 순매수 = 빨강(한국식)
    if val < 0:
        return "color: #1f77b4; font-weight: bold"  # 순매도 = 파랑(한국식)
    return ""


amount_cols = ["외국인(주)", "기관(주)"]
styled_flow = flow_df.style.format({c: "{:,.0f}" for c in amount_cols}, na_rep="-")
styled_flow = styled_flow.map(color_pos_neg, subset=amount_cols)
st.dataframe(styled_flow, width="stretch", hide_index=True)

for name in DOMESTIC:
    vals = data["investor_flows"].get(name, {}).get("values", {})
    if vals:
        max_key = max(vals, key=lambda k: abs(vals[k]))
        st.caption(f"{name}: 절댓값 기준 최대 매매주체 = {max_key} ({vals[max_key]:+,.0f}주)")

with st.expander("확보하지 못한 항목 (개인 / 기관 세부 / 기타법인)"):
    st.markdown(
        "요청된 항목 중 아래는 로그인 없이 가져올 수 있는 데이터 소스가 없어 "
        "**데이터 없음**입니다: 개인, 기타법인, 기관 세부(금융투자·보험·투신·사모펀드·"
        "은행·기타금융·연기금등·국가지자체)\n\n"
        "확인한 내용:\n"
        "- 네이버 증권(finance.naver.com/item/frgn.naver) 페이지는 외국인/기관(합계)만 "
        "제공하며, 위 세부 항목 자체가 이 페이지에 존재하지 않습니다.\n"
        "- pykrx의 `get_market_trading_value_by_investor()`는 위 세부 항목을 반환할 수 "
        "있지만, 실제로 로그인 없이 호출을 테스트한 결과 KRX 서버(data.krx.co.kr)가 "
        "비로그인 요청을 HTTP 400 응답(`LOGOUT`)으로 명시적으로 거부하는 것을 직접 "
        "확인했습니다. 즉 pykrx의 문제가 아니라 KRX 서버 자체가 로그인을 요구합니다."
    )

st.subheader("관련주 (정규장 + 시간외 괴리)")
if data["avg_gap"] is not None:
    st.metric("관련주 시간외 괴리 평균 (8종목)", f"{data['avg_gap']:+.2f}%p")
else:
    st.write("시간외 괴리 평균 (실시간): 데이터 없음")

related_df = df[df["구분"] == "관련주"].drop(columns=["구분"]).dropna(axis=1, how="all")
if related_df.empty:
    st.write("데이터 없음")
else:
    sort_by_gap = st.checkbox("괴리(%p) 절댓값 큰 순으로 정렬")
    if sort_by_gap and "괴리(%p)" in related_df.columns:
        order = related_df["괴리(%p)"].abs().sort_values(ascending=False, na_position="last").index
        related_df = related_df.reindex(order)

    related_df_display = relabel_cache_flag(related_df)

    def highlight_gap(row):
        gap = row.get("괴리(%p)")
        if pd.notna(gap) and abs(gap) >= GAP_HIGHLIGHT_THRESHOLD:
            return ["background-color: #fff3b0"] * len(row)
        return [""] * len(row)

    st.dataframe(style_df(related_df_display).apply(highlight_gap, axis=1), width="stretch", hide_index=True)

st.caption(
    "\"시간외가 구분\"이 '최근 기준(과거)'인 종목은 지금 진행 중인 시간외 세션이 없어 "
    "최근 5일 이내 마지막 프리/애프터마켓 값을 보여준 것입니다. 괴리(%p) 평균에는 이런 "
    "값도 포함되며, 하나라도 섞여 있으면 알림 문구에 \"실시간이 아닌 최근 관측값 포함\"이라고 "
    "표시됩니다. 국내 시간외등락률·정규장등락률은 전일종가 기준, 해외 시간외등락률은 "
    "정규장 마감가 기준(Yahoo Finance)이라 산출 기준이 다를 수 있습니다."
)

show_section("지수", "지수")
show_section("변동성 / 리스크 지표", "리스크")

# ---- 관련 뉴스 ----
st.divider()
st.subheader("관련 뉴스")
for q in NEWS_QUERIES:
    st.markdown(f"**{q}**")
    news_items = data["news"].get(q, [])
    if not news_items:
        st.write("수집 실패 또는 검색 결과 없음")
    else:
        for n in news_items:
            st.markdown(f"- [{n['제목']}]({n['링크']}) &nbsp;·&nbsp; {n['시각']}")

# ---- 스냅샷 다운로드 ----
st.divider()
st.download_button(
    label="latest_snapshot.json 다운로드",
    data=json.dumps(data, ensure_ascii=False, indent=2),
    file_name="latest_snapshot.json",
    mime="application/json",
)
st.caption("다운로드한 JSON 파일을 Claude 대화창에 업로드하면 이 화면 데이터를 바로 파악할 수 있습니다.")
