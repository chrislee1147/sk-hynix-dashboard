# -*- coding: utf-8 -*-
"""
SK하이닉스 투자 모니터링 대시보드 (Streamlit)
실행: streamlit run streamlit_app.py
"""

import datetime as dt
import json
import os

import pandas as pd
import requests
import streamlit as st
import yfinance as yf
from bs4 import BeautifulSoup

# ========================= CONFIG =========================

AVG_PRICE = 1789666
SHARES_OWNED = 3
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

US_RELATED_CORE = {
    "마이크론(MU)": "MU",
    "샌디스크(SNDK)": "SNDK",
    "웨스턴디지털(WDC)": "WDC",
    "SK하이닉스 ADR": "SKHY",  # 2026-07-10 나스닥 상장 (구 OTC 티커 HXSCL 폐지)
}

US_RELATED_EXTRA = {
    "AMD": "AMD",
    "엔비디아(NVDA)": "NVDA",
}

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
    "원달러 환율": "USDKRW=X",
}

SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "latest_snapshot.json")

st.set_page_config(page_title="SK하이닉스 투자 모니터링", layout="wide")


# ========================= 데이터 수집 =========================

def fetch_naver(code):
    """네이버 증권 페이지 크롤링. 실패 시 예외 발생 -> 호출부에서 pykrx로 폴백."""
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    res = requests.get(url, headers=headers, timeout=5)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    today = soup.find("p", class_="no_today")
    price = int(today.find("span", class_="blind").text.replace(",", ""))

    exday = soup.find("p", class_="no_exday")
    blinds = exday.find_all("span", class_="blind")
    change_amt = float(blinds[0].text.replace(",", ""))
    change_pct = float(blinds[1].text.replace("%", ""))

    em = exday.find("em")
    em_classes = em.get("class") or [] if em else []
    is_down = any("dn" in c or "down" in c for c in em_classes)
    if is_down:
        change_amt = -abs(change_amt)
        change_pct = -abs(change_pct)

    return float(price), change_pct, change_amt, "naver"


def fetch_pykrx(code):
    """pykrx 일별 데이터 폴백 (실시간 아님, 최근 종가 기준)."""
    from pykrx import stock

    today = dt.date.today()
    fromdate = (today - dt.timedelta(days=15)).strftime("%Y%m%d")
    todate = today.strftime("%Y%m%d")
    df = stock.get_market_ohlcv_by_date(fromdate, todate, code)
    if df is None or len(df) < 2:
        raise ValueError("pykrx 데이터 부족")
    close = float(df["종가"].iloc[-1])
    prev_close = float(df["종가"].iloc[-2])
    change_amt = close - prev_close
    change_pct = change_amt / prev_close * 100
    return close, change_pct, change_amt, "pykrx(일별)"


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


# ========================= 전체 데이터 수집 + 알림 판정 =========================

def collect_all():
    collected_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    items = []
    alerts = []

    sk_price = None
    kospi_price = None
    us_related_pct = {}
    vix_price = None

    # 국내
    for name, code in DOMESTIC.items():
        try:
            price, pct, amt, source = fetch_domestic(code)
            if code == "000660":
                sk_price = price
            items.append({
                "구분": "국내", "항목": name, "현재가": round(price, 2),
                "등락률(%)": round(pct, 2), "등락폭": round(amt, 2),
                "출처": source, "알림": "",
            })
        except Exception as e:
            items.append({
                "구분": "국내", "항목": name, "현재가": None,
                "등락률(%)": None, "등락폭": None, "출처": "실패",
                "알림": f"수집실패: {e}",
            })

    # 관련주 (핵심)
    for name, ticker in US_RELATED_CORE.items():
        try:
            price, pct, amt = fetch_yf(ticker)
            us_related_pct[name] = pct
            items.append({
                "구분": "관련주", "항목": name, "현재가": round(price, 2),
                "등락률(%)": round(pct, 2), "등락폭": round(amt, 2),
                "출처": "yfinance", "알림": "",
            })
        except Exception as e:
            items.append({
                "구분": "관련주", "항목": name, "현재가": None,
                "등락률(%)": None, "등락폭": None, "출처": "데이터 없음",
                "알림": f"수집실패: {e}",
            })

    # 관련주 (참고)
    for name, ticker in US_RELATED_EXTRA.items():
        try:
            price, pct, amt = fetch_yf(ticker)
            items.append({
                "구분": "참고", "항목": name, "현재가": round(price, 2),
                "등락률(%)": round(pct, 2), "등락폭": round(amt, 2),
                "출처": "yfinance", "알림": "",
            })
        except Exception as e:
            items.append({
                "구분": "참고", "항목": name, "현재가": None,
                "등락률(%)": None, "등락폭": None, "출처": "데이터 없음",
                "알림": f"수집실패: {e}",
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
        diff_pct = (sk_price - AVG_PRICE) / AVG_PRICE * 100
        if abs(diff_pct) >= 3:
            alerts.append(f"평단가 대비 {diff_pct:+.2f}% (평단가 {AVG_PRICE:,}원 / 현재가 {sk_price:,.0f}원)")
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

    if vix_price is not None and vix_price >= VIX_WARNING:
        alerts.append(f"변동성 경고: VIX {vix_price:.2f} (기준 {VIX_WARNING} 이상)")
        mark("VIX", "변동성경고")

    earnings_date = dt.date.fromisoformat(EARNINGS_DATE)
    dday = (earnings_date - dt.date.today()).days
    if 0 <= dday <= EARNINGS_DDAY_WARNING:
        alerts.append(f"매수 자제 구간 안내: 실적발표({EARNINGS_DATE})까지 D-{dday}")

    pnl_pct = None
    pnl_amt = None
    if sk_price is not None:
        pnl_pct = (sk_price - AVG_PRICE) / AVG_PRICE * 100
        pnl_amt = (sk_price - AVG_PRICE) * SHARES_OWNED

    return {
        "collected_at": collected_at,
        "sk_price": sk_price,
        "pnl_pct": pnl_pct,
        "pnl_amt": pnl_amt,
        "dday": dday,
        "alerts": alerts,
        "items": items,
    }


def save_snapshot(data):
    try:
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 클라우드 등 쓰기 불가 환경에서는 조용히 무시


# ========================= UI =========================

st.title("SK하이닉스 투자 모니터링")

if "data" not in st.session_state:
    st.session_state["data"] = collect_all()
    save_snapshot(st.session_state["data"])

col_btn, col_time = st.columns([1, 3])
with col_btn:
    if st.button("현재 시간 반영", type="primary"):
        st.session_state["data"] = collect_all()
        save_snapshot(st.session_state["data"])

data = st.session_state["data"]

with col_time:
    st.write(f"현재 시각: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    st.write(f"마지막 갱신 시각: {data['collected_at']}")

# ---- 상단 요약 ----
m1, m2, m3, m4 = st.columns(4)
m1.metric("SK하이닉스 현재가", f"{data['sk_price']:,.0f}원" if data['sk_price'] is not None else "N/A")
m2.metric("평단가 대비 손익률", f"{data['pnl_pct']:+.2f}%" if data['pnl_pct'] is not None else "N/A")
m3.metric("평가손익(보유 3주 기준)", f"{data['pnl_amt']:+,.0f}원" if data['pnl_amt'] is not None else "N/A")
m4.metric("실적발표 D-day", f"D-{data['dday']}" if data['dday'] >= 0 else f"D+{-data['dday']}")

st.caption(f"평단가 {AVG_PRICE:,}원 / 보유 {SHARES_OWNED}주 (총 계획 {TOTAL_PLAN}주) / 실적발표일 {EARNINGS_DATE}")

# ---- 알림 배너 ----
if data["alerts"]:
    for a in data["alerts"]:
        st.error(a)
else:
    st.success("조건 충족 알림 없음")

df = pd.DataFrame(data["items"])


def show_section(title, category):
    st.subheader(title)
    sub = df[df["구분"] == category].drop(columns=["구분"])
    if sub.empty:
        st.write("데이터 없음")
    else:
        st.dataframe(sub, width="stretch", hide_index=True)


show_section("국내", "국내")
show_section("관련주 (지수보다 우선 참고)", "관련주")
show_section("참고: 미국 반도체주", "참고")
show_section("지수", "지수")
show_section("변동성 / 리스크 지표", "리스크")

# ---- 스냅샷 다운로드 ----
st.divider()
st.download_button(
    label="latest_snapshot.json 다운로드",
    data=json.dumps(data, ensure_ascii=False, indent=2),
    file_name="latest_snapshot.json",
    mime="application/json",
)
st.caption("다운로드한 JSON 파일을 Claude 대화창에 업로드하면 이 화면 데이터를 바로 파악할 수 있습니다.")
