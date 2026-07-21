# -*- coding: utf-8 -*-
"""
SK하이닉스 투자 모니터링 대시보드 (Streamlit)
실행: streamlit run streamlit_app.py
"""

import datetime as dt
import json
import os
import re
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf
from bs4 import BeautifulSoup

KST = ZoneInfo("Asia/Seoul")


def now_kst():
    return dt.datetime.now(KST)

# ========================= CONFIG =========================

TOTAL_PLAN = 10
EARNINGS_DATE = "2026-07-24"

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
# "관련주 신호 발생" 등락률 알림은 최초 요청대로 이 4종목 평균만 사용
US_RELATED_ALERT_SET = ["마이크론(MU)", "샌디스크(SNDK)", "웨스턴디지털(WDC)", "SK하이닉스 ADR"]

GAP_ALERT_THRESHOLD = 3  # 시간외 괴리 평균 알림 기준(%p)
GAP_HIGHLIGHT_THRESHOLD = 3  # 표 하이라이트 기준(%p)

INDICES = {
    "코스피": "^KS11",
    "코스닥": "^KQ11",
    "닛케이225": "^N225",
    "상해종합": "000001.SS",
    "나스닥종합": "^IXIC",
    "S&P500": "^GSPC",
    "필라델피아반도체(SOX)": "^SOX",
}
ASIA_INDEX_NAMES = ["코스피", "코스닥", "닛케이225", "상해종합"]

RISK_INDICATORS = {
    "VIX": "^VIX",
    "WTI 국제유가": "CL=F",
    "원달러 환율": "USDKRW=X",
}

TARGET_PRICE_DEFAULT = 2_500_000
STOP_LOSS_RATIO = 0.85  # 손절가 기본값 = 평단가 -15%
STOP_LOSS_NEAR_PCT = 2  # 손절가 근접 기준(%)

SCHEDULE = [
    {"날짜": "2026-07-20", "계획": "2주", "실제": "2주", "매수가": "1,776,000원", "상태": "완료"},
    {"날짜": "2026-07-21", "계획": "2주", "실제": "1주", "매수가": "1,817,000원", "상태": "진행중"},
    {"날짜": "2026-07-22", "계획": "나머지 1주", "실제": "-", "매수가": "-", "상태": "예정"},
    {"날짜": "2026-07-23", "계획": "0주(관망)", "실제": "-", "매수가": "-", "상태": "예정"},
    {"날짜": "2026-07-24", "계획": "0주(실적일 매수금지)", "실제": "-", "매수가": "-", "상태": "예정"},
    {"날짜": "2026-07-25~28", "계획": "4주", "실제": "-", "매수가": "-", "상태": "예정"},
    {"날짜": "잔여", "계획": "2주(하방리스크 대비)", "실제": "-", "매수가": "-", "상태": "예정"},
]

NEWS_QUERIES = ["SK하이닉스", "삼성전자 반도체"]
NEWS_TIME_PATTERN = re.compile(r"^(\d+(분|시간|일)\s*전|\d{4}\.\d{2}\.\d{2}\.?)$")

SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "latest_snapshot.json")
HISTORY_PATH = os.path.join(os.path.dirname(__file__), "history_log.csv")
HISTORY_COLUMNS = ["시각", "SK하이닉스가격", "평단가", "손익률", "손익금액"]

TRADES_PATH = os.path.join(os.path.dirname(__file__), "trades.csv")
TRADES_COLUMNS = ["날짜", "수량", "매수가"]
SEED_TRADES = [
    {"날짜": "2026-07-20", "수량": 2, "매수가": 1_776_000},
    {"날짜": "2026-07-21", "수량": 1, "매수가": 1_817_000},
]

ALERT_LOG_PATH = os.path.join(os.path.dirname(__file__), "alert_history.csv")

st.set_page_config(page_title="SK하이닉스 투자 모니터링", layout="wide")


# ========================= 데이터 수집 =========================

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
    over = item.get("overMarketPriceInfo")
    if over and over.get("overPrice") and over.get("overMarketStatus") == "OPEN":
        overtime_price = to_num(over["overPrice"])
        overtime_pct = to_num(over.get("fluctuationsRatio", 0))
        if over.get("compareToPreviousPrice", {}).get("name") == "FALLING":
            overtime_pct = -abs(overtime_pct)

    return price, change_pct, change_amt, "naver", overtime_price, overtime_pct


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
    return close, change_pct, change_amt, "pykrx(일별)", None, None


def fetch_investor_flows(code):
    """일별 외국인/기관/개인 순매수 금액(원). KRX_ID/KRX_PW 환경변수 필요."""
    if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
        raise ValueError("KRX_ID/KRX_PW 환경변수 미설정 (data.krx.co.kr 회원가입 후 설정 필요)")

    from pykrx import stock

    today = now_kst().date()
    for i in range(6):
        target = today - dt.timedelta(days=i)
        d_str = target.strftime("%Y%m%d")
        try:
            df = stock.get_market_trading_value_by_investor(d_str, d_str, code)
            if df is None or df.empty:
                continue
            result = {}
            for label, candidates in [
                ("외국인", ["외국인합계", "외국인"]),
                ("기관", ["기관합계", "기관"]),
                ("개인", ["개인"]),
            ]:
                for cand in candidates:
                    if cand in df.index:
                        result[label] = float(df.loc[cand, "순매수"])
                        break
            if result:
                return result, target.strftime("%Y-%m-%d")
        except Exception:
            continue
    raise ValueError("수급 데이터 조회 실패")


def fetch_domestic(code):
    try:
        return fetch_naver(code)
    except Exception:
        return fetch_pykrx(code)


def fetch_yf(ticker):
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


def fetch_yf_extended(ticker):
    """정규장 등락 + 시간외(프리마켓/애프터마켓). 시간외 데이터 없으면 (None, None, None)."""
    t = yf.Ticker(ticker)
    info = t.info

    price = info.get("regularMarketPrice")
    prev = info.get("regularMarketPreviousClose") or info.get("previousClose")
    if price is None or prev is None:
        price, change_pct, change_amt = fetch_yf(ticker)
        return price, change_pct, change_amt, None, None, None

    price = float(price)
    prev = float(prev)
    change_amt = price - prev
    change_pct = change_amt / prev * 100

    ext_price = None
    ext_pct = None
    ext_label = None
    state = info.get("marketState")
    if state == "PRE" and info.get("preMarketPrice") is not None:
        ext_price = float(info["preMarketPrice"])
        ext_pct = info.get("preMarketChangePercent")
        ext_label = "프리마켓"
    elif state in ("POST", "POSTPOST") and info.get("postMarketPrice") is not None:
        ext_price = float(info["postMarketPrice"])
        ext_pct = info.get("postMarketChangePercent")
        ext_label = "애프터마켓"

    return price, change_pct, change_amt, ext_price, ext_pct, ext_label


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

    # 국내 (정규장 + 시간외)
    for name, code in DOMESTIC.items():
        try:
            price, pct, amt, source, ot_price, ot_pct = fetch_domestic(code)
            if code == "000660":
                sk_price = price
            items.append({
                "구분": "국내", "항목": name, "현재가": round(price, 2),
                "등락률(%)": round(pct, 2), "등락폭": round(amt, 2),
                "시간외가": round(ot_price, 2) if ot_price is not None else None,
                "시간외등락률(%)": round(ot_pct, 2) if ot_pct is not None else None,
                "출처": source, "알림": "",
            })
        except Exception as e:
            items.append({
                "구분": "국내", "항목": name, "현재가": None,
                "등락률(%)": None, "등락폭": None,
                "시간외가": None, "시간외등락률(%)": None,
                "출처": "실패", "알림": f"수집실패: {e}",
            })

    # 국내 수급 (외국인/기관/개인 순매수)
    for name, code in DOMESTIC.items():
        try:
            flows, flow_date = fetch_investor_flows(code)
            for investor, amt in flows.items():
                items.append({
                    "구분": "수급", "항목": f"{name} - {investor}({flow_date})",
                    "현재가": round(amt), "등락률(%)": None, "등락폭": None,
                    "출처": "pykrx", "알림": "",
                })
        except Exception as e:
            items.append({
                "구분": "수급", "항목": f"{name} 수급", "현재가": None,
                "등락률(%)": None, "등락폭": None, "출처": "실패",
                "알림": f"수집실패: {e}",
            })

    # 관련주 (정규장 + 시간외 + 괴리)
    related_gaps = {}
    for name, ticker in US_RELATED.items():
        try:
            price, pct, amt, ext_price, ext_pct, ext_label = fetch_yf_extended(ticker)
            if name in US_RELATED_ALERT_SET:
                us_related_pct[name] = pct
            gap = None
            if ext_pct is not None:
                gap = round(ext_pct - pct, 2)
                related_gaps[name] = gap
            items.append({
                "구분": "관련주", "항목": name, "현재가": round(price, 2),
                "등락률(%)": round(pct, 2), "등락폭": round(amt, 2),
                "시간외구분": ext_label or "-",
                "시간외가": round(ext_price, 2) if ext_price is not None else None,
                "시간외등락률(%)": round(ext_pct, 2) if ext_pct is not None else None,
                "괴리(%p)": gap,
                "출처": "yfinance", "알림": "",
            })
        except Exception as e:
            items.append({
                "구분": "관련주", "항목": name, "현재가": None,
                "등락률(%)": None, "등락폭": None,
                "시간외구분": "-", "시간외가": None, "시간외등락률(%)": None,
                "괴리(%p)": None,
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
            alerts.append(
                f"관련주 시간외 {direction}, 내일 개장 참고 신호: "
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
    }


def save_snapshot(data):
    try:
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 클라우드 등 쓰기 불가 환경에서는 조용히 무시


def load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            return pd.read_csv(HISTORY_PATH)
        except Exception:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
    return pd.DataFrame(columns=HISTORY_COLUMNS)


def append_history(collected_at, sk_price, avg_price, pnl_pct, pnl_amt):
    if sk_price is None:
        return
    row = pd.DataFrame([{
        "시각": collected_at.replace(" (KST)", ""),
        "SK하이닉스가격": sk_price,
        "평단가": avg_price,
        "손익률": pnl_pct,
        "손익금액": pnl_amt,
    }])
    try:
        header = not os.path.exists(HISTORY_PATH)
        row.to_csv(HISTORY_PATH, mode="a", header=header, index=False, encoding="utf-8-sig")
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
        return pd.read_csv(TRADES_PATH)
    except Exception:
        return pd.DataFrame(SEED_TRADES)


def save_trade(date_str, qty, price):
    row = pd.DataFrame([{"날짜": date_str, "수량": qty, "매수가": price}])
    header = not os.path.exists(TRADES_PATH)
    row.to_csv(TRADES_PATH, mode="a", header=header, index=False, encoding="utf-8-sig")


def compute_position(trades_df):
    if trades_df is None or trades_df.empty:
        return 0, 0
    shares = int(trades_df["수량"].sum())
    if shares <= 0:
        return 0, 0
    avg = (trades_df["수량"] * trades_df["매수가"]).sum() / shares
    return round(avg), shares


def append_alert_log(collected_at, alerts):
    if not alerts:
        return
    rows = pd.DataFrame([
        {"시각": collected_at.replace(" (KST)", ""), "알림내용": a} for a in alerts
    ])
    try:
        header = not os.path.exists(ALERT_LOG_PATH)
        rows.to_csv(ALERT_LOG_PATH, mode="a", header=header, index=False, encoding="utf-8-sig")
    except Exception:
        pass  # 클라우드 등 쓰기 불가 환경에서는 조용히 무시


def refresh_data(avg_price, shares_owned):
    data = collect_all(avg_price, shares_owned)
    save_snapshot(data)
    append_history(data["collected_at"], data["sk_price"], avg_price, data["pnl_pct"], data["pnl_amt"])
    append_alert_log(data["collected_at"], data["alerts"])
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


# ========================= UI =========================

st.title("SK하이닉스 투자 모니터링")

# 매수 기록(trades.csv) 기반 평단가/보유주수 계산 — 화면 하단 폼에서 갱신 가능
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

# ---- 상단 요약 (평단가/보유주수는 trades.csv 최신값으로 즉시 반영) ----
if sk_price is not None and AVG_PRICE:
    live_pnl_pct = (sk_price - AVG_PRICE) / AVG_PRICE * 100
    live_pnl_amt = (sk_price - AVG_PRICE) * SHARES_OWNED
else:
    live_pnl_pct = None
    live_pnl_amt = None

m1, m2, m3, m4 = st.columns(4)
m1.metric("SK하이닉스 현재가", f"{sk_price:,.0f}원" if sk_price is not None else "N/A")
m2.metric("평단가 대비 손익률", f"{live_pnl_pct:+.2f}%" if live_pnl_pct is not None else "N/A")
m3.metric(f"평가손익(보유 {SHARES_OWNED}주 기준)", f"{live_pnl_amt:+,.0f}원" if live_pnl_amt is not None else "N/A")
m4.metric("실적발표 D-day", f"D-{data['dday']}" if data['dday'] >= 0 else f"D+{-data['dday']}")

st.caption(f"평단가 {AVG_PRICE:,}원 / 보유 {SHARES_OWNED}주 (총 계획 {TOTAL_PLAN}주) / 실적발표일 {EARNINGS_DATE}")

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
    target_price = st.number_input(
        "목표가(원)", min_value=0, step=10_000, key="target_price",
    )
with col_sl:
    stop_loss_price = st.number_input(
        "손절가(원)", min_value=0, step=10_000, key="stop_loss_price",
    )

if sk_price is not None:
    upside_pct = (target_price - sk_price) / sk_price * 100
    st.write(f"목표가까지 남은 상승률: {upside_pct:+.2f}%")

    denom = target_price - AVG_PRICE
    progress = (sk_price - AVG_PRICE) / denom if denom != 0 else 0.0
    progress_clamped = min(max(progress, 0.0), 1.0)
    st.progress(progress_clamped, text=f"평단가→목표가 진행률: {progress * 100:.1f}%")

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

# ---- 주가 / 손익률 추이 차트 ----
st.divider()
st.subheader("수익 현황 추이")

history_df = load_history()
if history_df.empty:
    st.info("차트를 표시할 이력 데이터가 아직 없습니다. '현재 시간 반영'을 눌러 데이터를 쌓아주세요.")
else:
    x = pd.to_datetime(history_df["시각"])

    fig_price = go.Figure()
    fig_price.add_trace(go.Scatter(
        x=x, y=history_df["SK하이닉스가격"], mode="lines+markers", name="SK하이닉스",
    ))
    fig_price.add_hline(y=target_price, line_dash="dash", line_color="green", annotation_text="목표가")
    fig_price.add_hline(y=stop_loss_price, line_dash="dash", line_color="red", annotation_text="손절가")
    fig_price.add_hline(y=AVG_PRICE, line_dash="dash", line_color="gray", annotation_text="평단가")
    fig_price.update_layout(title="SK하이닉스 주가 추이", xaxis_title="시각", yaxis_title="가격(원)", height=400)
    st.plotly_chart(fig_price)

    fig_pnl = go.Figure()
    fig_pnl.add_trace(go.Scatter(
        x=x, y=history_df["손익률"], mode="lines+markers", name="손익률(%)",
    ))
    fig_pnl.update_layout(title="손익률 추이", xaxis_title="시각", yaxis_title="손익률(%)", height=300)
    st.plotly_chart(fig_pnl)

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

with st.expander("매수 기록 전체 보기"):
    st.dataframe(trades_df, width="stretch", hide_index=True)
    st.download_button(
        label="trades.csv 다운로드",
        data=trades_df.to_csv(index=False).encode("utf-8-sig"),
        file_name="trades.csv",
        mime="text/csv",
    )
st.caption(
    "주의: Streamlit Cloud는 재부팅/재배포 시 로컬 파일이 초기화되어 "
    "이 화면에서 추가한 매수 기록이 사라질 수 있습니다. 주기적으로 다운로드해 백업하세요."
)

# ---- 매수 스케줄 (계획) ----
st.divider()
st.subheader("매수 스케줄 (계획)")
st.dataframe(pd.DataFrame(SCHEDULE), width="stretch", hide_index=True)

buy_progress = min(SHARES_OWNED / TOTAL_PLAN, 1.0) if TOTAL_PLAN else 0.0
st.progress(buy_progress, text=f"누적 매수 진행률: {SHARES_OWNED}/{TOTAL_PLAN}주 ({buy_progress * 100:.0f}%)")

df = pd.DataFrame(data["items"])


def show_section(title, category):
    st.subheader(title)
    sub = df[df["구분"] == category].drop(columns=["구분"])
    sub = sub.dropna(axis=1, how="all")
    if sub.empty:
        st.write("데이터 없음")
    else:
        st.dataframe(sub, width="stretch", hide_index=True)


show_section("국내 (정규장 + 시간외)", "국내")
show_section("수급 (외국인/기관/개인 순매수)", "수급")

# ---- 관련주 (정규장 + 시간외 + 괴리) ----
st.subheader("관련주 (지수보다 우선 참고, 정규장 + 시간외 괴리)")
if data["avg_gap"] is not None:
    st.metric("관련주 시간외 괴리 평균", f"{data['avg_gap']:+.2f}%p")
else:
    st.write("시간외 괴리 평균: 데이터 없음")

related_df = df[df["구분"] == "관련주"].drop(columns=["구분"]).dropna(axis=1, how="all")
if related_df.empty:
    st.write("데이터 없음")
else:
    sort_by_gap = st.checkbox("괴리(%p) 절댓값 큰 순으로 정렬")
    if sort_by_gap and "괴리(%p)" in related_df.columns:
        order = related_df["괴리(%p)"].abs().sort_values(ascending=False, na_position="last").index
        related_df = related_df.reindex(order)

    def highlight_gap(row):
        gap = row.get("괴리(%p)")
        if pd.notna(gap) and abs(gap) >= GAP_HIGHLIGHT_THRESHOLD:
            return ["background-color: #fff3b0"] * len(row)
        return [""] * len(row)

    st.dataframe(related_df.style.apply(highlight_gap, axis=1), width="stretch", hide_index=True)

st.caption(
    "국내 시간외등락률·정규장등락률은 전일종가 기준, 해외 시간외등락률은 "
    "정규장 마감가 기준(Yahoo Finance)이라 산출 기준이 다를 수 있습니다."
)

show_section("지수", "지수")
show_section("변동성 / 리스크 지표", "리스크")

# ---- 아시아-미국 순환매 비교 ----
st.divider()
st.subheader("아시아-미국 순환매 비교")
col_asia, col_us = st.columns(2)
with col_asia:
    st.write("아시아 지수 (당일 마감 등락률)")
    asia_df = df[(df["구분"] == "지수") & (df["항목"].isin(ASIA_INDEX_NAMES))][["항목", "현재가", "등락률(%)"]]
    if asia_df.empty:
        st.write("데이터 없음")
    else:
        st.dataframe(asia_df, width="stretch", hide_index=True)
with col_us:
    st.write("미국 관련주 (시간외 등락률)")
    if related_df.empty or "시간외등락률(%)" not in related_df.columns:
        st.write("데이터 없음")
    else:
        st.dataframe(
            related_df[["항목", "시간외구분", "시간외등락률(%)"]],
            width="stretch", hide_index=True,
        )

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

# ---- 알림 로그 ----
st.divider()
st.subheader("알림 로그")
with st.expander("지금까지 발생한 알림 보기"):
    if os.path.exists(ALERT_LOG_PATH):
        try:
            st.dataframe(pd.read_csv(ALERT_LOG_PATH), width="stretch", hide_index=True)
        except Exception:
            st.write("알림 로그를 읽는 데 실패했습니다.")
    else:
        st.write("아직 기록된 알림이 없습니다.")

# ---- 스냅샷 다운로드 ----
st.divider()
st.download_button(
    label="latest_snapshot.json 다운로드",
    data=json.dumps(data, ensure_ascii=False, indent=2),
    file_name="latest_snapshot.json",
    mime="application/json",
)
st.caption("다운로드한 JSON 파일을 Claude 대화창에 업로드하면 이 화면 데이터를 바로 파악할 수 있습니다.")
