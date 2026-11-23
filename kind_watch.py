"""KIND '오늘의 공시'를 읽어 영문번역 서비스 신청 기업의 의무공시 대상만 골라 저장한다.

실행:
    python kind_watch.py            # 1회 조회 후 docs/data/results.json 갱신
    python kind_watch.py --loop     # 30분마다 반복 (사내 PC에서 직접 돌릴 때)
    python kind_watch.py --date 2026-11-23   # 특정 날짜 조회

판정 규칙 (기존 english-kospi Streamlit 앱과 동일):
  1) 회사코드가 config/{market}_company.csv 에 있을 것
  2) 공시제목에 config/{market}_format.csv 의 서식명 중 하나가 포함될 것
  3) 제목이 '추가상장'·'변경상장'으로 시작하면 제외
  4) 제목에 '종속회사의 주요경영사항'이 있으면 제외
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config"
OUTPUT = ROOT / "docs" / "data" / "results.json"
ARCHIVE = ROOT / "docs" / "data" / "archive"
KEEP_DAYS = 730         # 이전 기록 보관 기간(약 2년)
PAGE_SIZE = 100

KIND_URL = "https://kind.krx.co.kr/disclosure/todaydisclosure.do"
VIEWER_URL = "https://kind.krx.co.kr/common/disclsviewer.do?method=search&acptno={}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Referer": f"{KIND_URL}?method=searchTodayDisclosureMain",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
MARKET_NAMES = {"kospi": "코스피", "kosdaq": "코스닥"}


def now_kst() -> datetime:
    return datetime.now(KST).replace(microsecond=0)


def clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def norm_company_code(value) -> str:
    """기준자료 회사코드(예: 9181, 00766, USA11)를 KIND 5자리 회사코드로 맞춘다."""
    code = clean(value).upper()
    if code.endswith(".0") and code[:-2].isdigit():
        code = code[:-2]
    return code.zfill(5) if code.isdigit() else code


# ---------------------------------------------------------------- 기준자료
def read_csv(path: Path) -> list[dict]:
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            with path.open(encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path.name} 인코딩을 읽지 못했습니다.")


def load_markets() -> dict:
    """config 폴더에 company/format 짝이 있는 시장만 불러온다."""
    markets = {}
    for market in MARKET_NAMES:
        cpath, fpath = CONFIG / f"{market}_company.csv", CONFIG / f"{market}_format.csv"
        if not (cpath.exists() and fpath.exists()):
            continue
        companies = {norm_company_code(r.get("회사코드")): clean(r.get("회사명"))
                     for r in read_csv(cpath) if clean(r.get("회사코드"))}
        forms = [clean(r.get("서식명")) for r in read_csv(fpath) if clean(r.get("서식명"))]
        if not companies or not forms:
            raise ValueError(f"{market} 기준자료의 회사코드·서식명 열을 확인해주세요.")
        markets[market] = {"companies": companies, "forms": forms}
    if not markets:
        raise ValueError("config 폴더에 기준자료(kospi_company.csv / kospi_format.csv)가 없습니다.")
    return markets


# ---------------------------------------------------------------- KIND 조회
def fetch_page(session: requests.Session, date: str, page: int) -> str:
    data = {
        "method": "searchTodayDisclosureSub", "currentPageSize": str(PAGE_SIZE),
        "pageIndex": str(page), "orderMode": "0", "orderStat": "D",
        "forward": "todaydisclosure_sub", "chose": "S", "todayFlag": "Y",
        "selDate": date, "marketType": "", "disclosureType": "",
    }
    last_error = None
    for attempt in range(3):
        try:
            r = session.post(KIND_URL, data=data, headers=HEADERS, timeout=(10, 40))
            r.raise_for_status()
            r.encoding = r.encoding or "utf-8"
            return r.text
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"KIND 조회 실패 ({date} {page}페이지): {last_error}")


def parse_rows(html: str, date: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table.list tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        title_a = tds[2].find("a", onclick=re.compile(r"openDisclsViewer"))
        comp_a = tds[1].find("a", onclick=re.compile(r"companysummary_open"))
        if not title_a:
            continue
        acpt = re.search(r"openDisclsViewer\('(\d{14})'", title_a["onclick"])
        if not acpt:
            continue
        code = re.search(r"companysummary_open\('([^']+)'", comp_a["onclick"]) if comp_a else None
        market_img = tds[1].find("img")
        rows.append({
            "acptno": acpt.group(1),
            "date": date,
            "time": clean(tds[0].get_text()),
            "market_label": clean(market_img.get("alt")) if market_img else "",
            "company_code": norm_company_code(code.group(1)) if code else "",
            "company": clean(comp_a.get_text()) if comp_a else clean(tds[1].get_text()),
            # 링크 텍스트에는 [정정] 등 머리말이 포함된다(title 속성에는 빠져 있음).
            "title": clean(title_a.get_text()),
            "submitter": clean(tds[3].get_text()),
            "url": VIEWER_URL.format(acpt.group(1)),
        })
    return rows


def total_count(html: str) -> int:
    m = re.search(r"전체\s*<em>\s*([\d,]+)\s*</em>", html)
    return int(m.group(1).replace(",", "")) if m else 0


def fetch_day(date: str) -> list[dict]:
    session = requests.Session()
    first = fetch_page(session, date, 1)
    rows = parse_rows(first, date)
    pages = max(1, math.ceil(total_count(first) / PAGE_SIZE))
    for page in range(2, min(pages, 50) + 1):
        time.sleep(1)  # KIND 부하를 줄이기 위해 천천히
        rows.extend(parse_rows(fetch_page(session, date, page), date))
    unique = {r["acptno"]: r for r in rows}
    return list(unique.values())


# ---------------------------------------------------------------- 판정
def match(rows: list[dict], markets: dict) -> list[dict]:
    out = []
    for row in rows:
        title = row["title"]
        if title.startswith(("추가상장", "변경상장")) or "종속회사의 주요경영사항" in title:
            continue
        for market, ref in markets.items():
            if row["company_code"] not in ref["companies"]:
                continue
            # 서식명과 제목의 띄어쓰기 차이(예: '현금ㆍ현물 배당 결정' vs '현금ㆍ현물배당결정')는 무시한다.
            compact = re.sub(r"\s+", "", title)
            forms = [f for f in ref["forms"] if re.sub(r"\s+", "", f) in compact]
            if forms:
                out.append({**row, "market": market, "market_name": MARKET_NAMES[market],
                            "matched_form": max(forms, key=len)})
                break
    return out


# ---------------------------------------------------------------- 저장
# results.json          : 오늘(KST) 공시분만. 자정이 지나 첫 실행 때 전날 목록은 보관함으로 옮겨진다.
# archive/YYYY-MM.json  : 전날까지의 누적 기록 (월별 파일)
# archive/index.json    : 보관함에 있는 날짜별 건수 목록
def read_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_results() -> dict:
    return read_json(OUTPUT, {"items": [], "runs": []})


def sort_items(items) -> list[dict]:
    return sorted(items, key=lambda i: (i["date"], i["time"], i["acptno"]), reverse=True)


def archive_items(items: list[dict]) -> None:
    """전날 이전 공시를 월별 보관함에 합치고 날짜별 목록(index.json)을 갱신한다."""
    if not items:
        return
    by_month: dict[str, list[dict]] = {}
    for item in items:
        by_month.setdefault(item["date"][:7], []).append(item)
    for month, rows in by_month.items():
        path = ARCHIVE / f"{month}.json"
        stored = {i["acptno"]: i for i in read_json(path, {"items": []})["items"]}
        for row in rows:
            first_seen = stored.get(row["acptno"], {}).get("first_seen")
            stored[row["acptno"]] = {**row, "first_seen": first_seen or row.get("first_seen")}
        write_json(path, {"month": month, "items": sort_items(stored.values())})

    # 날짜별 건수 목록 재작성 + 보관 기간이 지난 월 파일 삭제
    cutoff = (now_kst() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m")
    dates: dict[str, int] = {}
    for path in sorted(ARCHIVE.glob("????-??.json")):
        if path.stem < cutoff:
            path.unlink()
            continue
        for item in read_json(path, {"items": []})["items"]:
            dates[item["date"]] = dates.get(item["date"], 0) + 1
    write_json(ARCHIVE / "index.json", {
        "months": sorted({d[:7] for d in dates}, reverse=True),
        "dates": dict(sorted(dates.items(), reverse=True)),
    })


def save_results(state: dict, matched: list[dict], scanned: dict, error: str | None) -> int:
    now = now_kst()
    stamp, today = now.isoformat(), now.strftime("%Y-%m-%d")

    items = {i["acptno"]: i for i in state.get("items", [])}
    new = 0
    for row in matched:
        if row["acptno"] not in items:
            items[row["acptno"]] = {**row, "first_seen": stamp}
            new += 1
        else:  # 제목·시각 등은 최신값으로, 최초 발견 시각은 유지
            items[row["acptno"]].update(row)

    # 일 단위 리셋: 오늘 공시만 results.json에 남기고 나머지는 보관함으로
    archive_items([i for i in items.values() if i["date"] < today])
    today_items = sort_items(i for i in items.values() if i["date"] >= today)

    run = {"at": stamp, "scanned": scanned, "matched": len(matched), "new": new,
           "status": "error" if error else "ok", "error": error}
    write_json(OUTPUT, {
        "date": today,
        "updated_at": stamp if not error else state.get("updated_at"),
        "last_run": run,
        "runs": ([run] + state.get("runs", []))[:30],
        "items": today_items,
    })
    return new


def run_once(dates: list[str] | None = None) -> int:
    markets = load_markets()
    now = now_kst()
    if not dates:
        dates = [now.strftime("%Y-%m-%d")]
        if now.hour < 2:  # 자정 직후에는 전날 늦은 공시도 한 번 더 확인
            dates.insert(0, (now - timedelta(days=1)).strftime("%Y-%m-%d"))

    state = load_results()
    matched, scanned, error = [], {}, None
    try:
        for date in dates:
            rows = fetch_day(date)
            scanned[date] = len(rows)
            matched.extend(match(rows, markets))
    except Exception as exc:  # 조회 실패도 기록해 화면에 표시
        error = str(exc)[:300]
    new = save_results(state, matched, scanned, error)
    print(f"[{now.isoformat()}] 조회 {scanned} / 대상 {len(matched)}건 / 신규 {new}건"
          + (f" / 오류: {error}" if error else ""))
    return 1 if error else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="30분마다 반복 실행")
    parser.add_argument("--interval", type=int, default=30, help="반복 간격(분)")
    parser.add_argument("--date", action="append", help="조회 날짜 YYYY-MM-DD (여러 번 지정 가능)")
    args = parser.parse_args()
    if not args.loop:
        sys.exit(run_once(args.date))
    while True:
        run_once(args.date)
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
