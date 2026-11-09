#!/usr/bin/env python3
"""
割安チェッカー - ローカルAPIサーバー
【起動.command をダブルクリックで起動してください】

- 銘柄リストは watchlist.json に保存（画面から追加・削除できます）
- 株価データは Yahoo Finance（yfinance）から取得し、cache.json に保存
  （取得に失敗したときは前回の値を表示します）
"""
import json, os, re, threading, time, math, unicodedata
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
import urllib.request

PORT = 5070
DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(DIR, "watchlist.json")
CACHE_PATH = os.path.join(DIR, "cache.json")
CACHE_TTL = 60 * 30  # 30分はキャッシュを使う

_lock = threading.Lock()
_cache: dict = {}

DEFAULT_WATCHLIST = [
    {"code": "8593", "memo": ""},  # 三菱HCキャピタル
    {"code": "9433", "memo": ""},  # KDDI
    {"code": "1928", "memo": ""},  # 積水ハウス
    {"code": "3231", "memo": ""},  # 野村不動産HD
    {"code": "4540", "memo": ""},  # ツムラ
    {"code": "7994", "memo": ""},  # オカムラ
    {"code": "8584", "memo": ""},  # ジャックス
    {"code": "9882", "memo": ""},  # イエローハット
    {"code": "7931", "memo": ""},  # 未来工業
    {"code": "4042", "memo": ""},  # 東ソー
    {"code": "9364", "memo": ""},  # 上組
    {"code": "2124", "memo": ""},  # JACリクルートメント
    {"code": "6381", "memo": ""},  # アネスト岩田
]

SECTOR_JA = {
    "Financial Services": "金融", "Real Estate": "不動産", "Consumer Cyclical": "一般消費財",
    "Consumer Defensive": "生活必需品", "Healthcare": "ヘルスケア", "Industrials": "資本財・産業",
    "Basic Materials": "素材", "Technology": "テクノロジー", "Communication Services": "通信",
    "Utilities": "公益", "Energy": "エネルギー",
}

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# -----------------------------------------------
# ファイル入出力
# -----------------------------------------------
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_watchlist():
    wl = load_json(WATCHLIST_PATH, None)
    if wl is None:
        wl = DEFAULT_WATCHLIST
        save_json(WATCHLIST_PATH, wl)
    return wl


# -----------------------------------------------
# 銘柄コード → yfinance ティッカー
#   4桁（130A のような英字入りも可）→ 東証「.T」
#   それ以外（AAPL など）はそのまま
# -----------------------------------------------
def to_ticker(code: str) -> str:
    code = code.strip().upper()
    if re.fullmatch(r"\d{3}[0-9A-Z]", code):
        return f"{code}.T"
    return code


def is_jp(code: str) -> bool:
    return to_ticker(code).endswith(".T")


# -----------------------------------------------
# 日本語の銘柄名（Yahoo!ファイナンス → 株探）
# -----------------------------------------------
def _fetch_html(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _num(s):
    """'1,234.5' → 1234.5 / '－' などは None"""
    s = re.sub(r"<[^>]+>|&nbsp;|,|\s", "", s or "")
    try:
        return float(s)
    except Exception:
        return None


def yahoo_jp_info(code):
    """
    Yahoo!ファイナンス（日本版）から銘柄名と会社予想の1株配当を取る。
    株探が取得できない環境（GitHub Actions など海外IP）向けの代替。
    昨日の実績では Yahoo!ファイナンスは Actions からも取得できた。
    """
    out = {}
    try:
        html = _fetch_html(f"https://finance.yahoo.co.jp/quote/{code}.T", timeout=10)
        html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        m = re.search(r"<title>(.*?)【", html)
        if m:
            nm = re.sub(r"[（(]株[)）]", "", m.group(1)).strip()
            if len(nm) >= 2:
                out["name"] = nm
        m = re.search(r"1株配当</span><span[^>]*>（会社予想）</span>.*?"
                      r"_StyledNumber__value[^\"]*\">([\d,\.]+)</span>", html, re.S)
        if m:
            out["fcDiv"] = _num(m.group(1))
            mp = re.search(r"\((\d{4})/(\d{2})\)", html[m.end():m.end() + 300])
            if mp:
                out["fcPeriod"] = f"{mp.group(1)}.{mp.group(2)}"
        m = re.search(r"配当利回り</span><span[^>]*>（会社予想）</span>.*?"
                      r"_StyledNumber__value[^\"]*\">([\d,\.]+)</span>", html, re.S)
        if m:
            out["yjYield"] = _num(m.group(1))
    except Exception:
        pass
    return out


def kabutan_info(code):
    """
    株探から日本株の「会社予想ベース」の指標を取る。
      - 銘柄名・PER（予想）・PBR・配当利回り（会社予想）… 基本情報ページ
      - 予想1株配当・予想EPS・前期の実績1株配当 … 業績ページの通期表
    Yahoo(米国版)の dividendRate は「直近1回の配当 × 回数」で、
    中間と期末の金額が違う銘柄だと大きくずれるため、こちらを優先する。
    """
    out = {}
    try:
        html = _fetch_html(f"https://kabutan.jp/stock/?code={code}", timeout=10)
        m = re.search(r"<title>([^(\[（【]+)[(\[（【]", html)
        if m and len(m.group(1).strip()) >= 2:
            out["name"] = m.group(1).strip()
        # PER / PBR / 利回り / 信用倍率 の並び
        m = re.search(r'id="stockinfo_i3".*?<tbody>\s*<tr>(.*?)</tr>', html, re.S)
        if m:
            tds = [re.sub(r"<span.*", "", td, flags=re.S) for td in re.findall(r"<td[^>]*>(.*?)</td>", m.group(1), re.S)]
            if len(tds) >= 3:
                out["kPer"], out["kPbr"], out["kYield"] = _num(tds[0]), _num(tds[1]), _num(tds[2])
    except Exception:
        pass
    try:
        html = _fetch_html(f"https://kabutan.jp/stock/finance?code={code}", timeout=10)
        # 通期の業績表（「1株配」を含む最初の表）
        i = html.find("1株配", html.find("fin_year_result_d"))
        tbl = html[i:html.find("</table>", i)] if i > 0 else ""
        actual = forecast = None
        periods = []
        for th, body in re.findall(r"<th scope=\"row\"[^>]*>(.*?)</th>(.*?)</tr>", tbl, re.S):
            if not re.search(r"\d{4}\.\d{2}", th):
                continue
            tds = re.findall(r"<td[^>]*>(.*?)</td>", body, re.S)
            if len(tds) < 6:
                continue
            row = {"period": re.search(r"\d{4}\.\d{2}", th).group(0),
                   "eps": _num(tds[4]), "div": _num(tds[5])}
            y, mo = map(int, row["period"].split("."))
            periods.append(y * 12 + mo)
            if "予" in th:
                forecast = forecast or row
            else:
                actual = row
        # REIT など決算が半年ごとの銘柄は、1期分の分配金を年換算（×2）する
        if len(periods) >= 3 and all(b - a == 6 for a, b in zip(periods[-3:], periods[-2:])):
            for r in (forecast, actual):
                if r:
                    r["div"] = r["div"] * 2 if r["div"] is not None else None
                    r["eps"] = r["eps"] * 2 if r["eps"] is not None else None
            out["semiannual"] = True
        if forecast:
            out["fcPeriod"] = forecast["period"]
            out["fcDiv"] = forecast["div"]
            out["fcEps"] = forecast["eps"]
        if actual:
            out["lastPeriod"] = actual["period"]
            out["lastDiv"] = actual["div"]
            out["lastEps"] = actual["eps"]
    except Exception:
        pass
    return out


# -----------------------------------------------
# 株価・指標の取得
# -----------------------------------------------
_yf = None


def yf():
    global _yf
    if _yf is None:
        import yfinance
        _yf = yfinance
    return _yf


def fnum(v, nd=None):
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return round(x, nd) if nd is not None else x
    except Exception:
        return None


def fetch_stock(code: str) -> dict:
    t = yf().Ticker(to_ticker(code))
    info = t.info or {}

    hist = t.history(period="6y", auto_adjust=False)
    if hist is None or hist.empty:
        raise RuntimeError("株価データが取得できませんでした（コードを確認してください）")
    hist = hist.dropna(subset=["Close"])
    idx = hist.index.tz_localize(None) if hist.index.tz is not None else hist.index

    price = fnum(info.get("currentPrice")) or fnum(info.get("regularMarketPrice")) or fnum(hist["Close"].iloc[-1])
    prev = fnum(info.get("previousClose")) or fnum(info.get("regularMarketPreviousClose"))

    # 配当（分割調整済み・権利落ち日ベース）
    divs = t.dividends
    if divs is not None and len(divs):
        didx = divs.index.tz_localize(None) if divs.index.tz is not None else divs.index
        div_list = [(d.to_pydatetime(), float(v)) for d, v in zip(didx, divs.values)]
    else:
        div_list = []

    now = datetime.now()
    ttm_div = sum(v for d, v in div_list if d > now - timedelta(days=365))

    # 予想配当
    #   日本株：株探の会社予想（今期の予想1株配当）を使う
    #   それ以外／取れないとき：Yahoo の dividendRate（直近1回×回数の概算）→ 直近12ヶ月実績
    kb = kabutan_info(code) if is_jp(code) else {}
    yj = yahoo_jp_info(code) if is_jp(code) and kb.get("fcDiv") is None else {}
    if kb.get("fcDiv") is not None:
        fwd_div, div_source = kb["fcDiv"], f"会社予想（{kb.get('fcPeriod', '')}期）"
    elif yj.get("fcDiv") is not None:
        fwd_div, div_source = yj["fcDiv"], f"会社予想（{yj.get('fcPeriod', '')}期・Yahoo!ファイナンス）"
    elif kb.get("lastDiv") is not None:
        fwd_div, div_source = kb["lastDiv"], f"前期実績（{kb.get('lastPeriod', '')}期・今期予想なし）"
    else:
        fwd_div = fnum(info.get("dividendRate")) or fnum(info.get("trailingAnnualDividendRate")) or (ttm_div or None)
        div_source = "Yahoo概算"

    # 5年平均利回り：過去5年の各月末株価 ÷ その時点の直近12ヶ月配当 の平均
    monthly = hist["Close"].copy()
    monthly.index = idx
    try:
        monthly = monthly.resample("ME").last().dropna()
    except ValueError:
        monthly = monthly.resample("M").last().dropna()
    start5 = now - timedelta(days=365 * 5)
    yields = []
    for d, close in monthly.items():
        if d < start5 or close <= 0:
            continue
        d = d.to_pydatetime()
        s = sum(v for dd, v in div_list if d - timedelta(days=365) < dd <= d)
        if s > 0:
            yields.append(s / close * 100)
    avg5 = round(sum(yields) / len(yields), 2) if len(yields) >= 24 else fnum(info.get("fiveYearAvgDividendYield"))

    # 暦年ごとの配当と「非減配の連続年数」（今年は未確定なので除外）
    by_year = {}
    for d, v in div_list:
        by_year[d.year] = by_year.get(d.year, 0) + v
    years = sorted(y for y in by_year if y < now.year)
    streak = 0
    for i in range(len(years) - 1, 0, -1):
        if years[i] - years[i - 1] != 1:
            break
        if by_year[years[i]] >= by_year[years[i - 1]] * 0.995:
            streak += 1
        else:
            break
    div_years = [{"year": y, "div": round(by_year[y], 2)} for y in years[-8:]]

    # 日足（5年分：日付・終値・高値）※画面側で「高値から何%下落」を自由な期間で計算
    h5 = hist[idx >= start5]
    i5 = idx[idx >= start5]
    series = [[d.strftime("%Y-%m-%d"), round(float(c), 2), round(float(hh), 2)]
              for d, c, hh in zip(i5, h5["Close"].values, h5["High"].values)]

    name = unicodedata.normalize("NFKC", kb.get("name") or yj.get("name") or info.get("shortName") or info.get("longName") or code)

    sector_en = info.get("sector") or ""
    pbr = kb.get("kPbr") or fnum(info.get("priceToBook"), 2)
    bps = fnum(info.get("bookValue"))
    if pbr is None and bps and price:
        pbr = round(price / bps, 2)

    # PER：実績＝前期EPS、予想＝今期予想EPS（株探）。取れなければ Yahoo
    per = round(price / kb["lastEps"], 1) if kb.get("lastEps") and kb["lastEps"] > 0 and price else fnum(info.get("trailingPE"), 1)
    fper = round(price / kb["fcEps"], 1) if kb.get("fcEps") and kb["fcEps"] > 0 and price else (kb.get("kPer") or fnum(info.get("forwardPE"), 1))
    # 配当性向：予想配当 ÷ 予想EPS
    if kb.get("fcDiv") is not None and kb.get("fcEps"):
        payout = round(kb["fcDiv"] / kb["fcEps"] * 100, 1) if kb["fcEps"] > 0 else None
    else:
        payout = fnum(info.get("payoutRatio") * 100, 1) if fnum(info.get("payoutRatio")) is not None else None

    return {
        "code": code.upper(),
        "ticker": to_ticker(code),
        "name": name,
        "jp": is_jp(code),
        "currency": info.get("currency") or ("JPY" if is_jp(code) else "USD"),
        "sector": SECTOR_JA.get(sector_en, sector_en or "その他"),
        "industry": info.get("industry") or "",
        "price": price,
        "prevClose": prev,
        "fwdDiv": fnum(fwd_div, 2),
        "divSource": div_source,
        "lastDiv": kb.get("lastDiv"),
        "kYield": kb.get("kYield") or yj.get("yjYield"),
        "ttmDiv": round(ttm_div, 2) if ttm_div else None,
        "avg5Yield": avg5,
        "per": per,
        "fper": fper,
        "pbr": pbr,
        "payout": payout,
        "high52": fnum(info.get("fiftyTwoWeekHigh")),
        "low52": fnum(info.get("fiftyTwoWeekLow")),
        "marketCap": fnum(info.get("marketCap")),
        "streak": streak,
        "divYears": div_years,
        "series": series,
        "fetchedAt": datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M"),
        "stale": False,
    }


def get_stock(code: str, force=False) -> dict:
    code = code.strip().upper()
    with _lock:
        c = _cache.get(code)
    if c and not force and time.time() - c.get("_ts", 0) < CACHE_TTL:
        return c
    try:
        data = fetch_stock(code)
        data["_ts"] = time.time()
        with _lock:
            _cache[code] = data
            save_json(CACHE_PATH, _cache)
        return data
    except Exception as e:
        if c:  # 失敗したら前回値
            d = dict(c)
            d["stale"] = True
            d["error"] = str(e)
            return d
        raise


# -----------------------------------------------
# HTTP サーバー
# -----------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(DIR, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/health":
            return self._send(200, {"ok": True})
        if u.path == "/api/watchlist":
            return self._send(200, load_watchlist())
        if u.path == "/api/stock":
            code = (q.get("code") or [""])[0]
            force = (q.get("force") or ["0"])[0] == "1"
            if not code:
                return self._send(400, {"error": "code がありません"})
            try:
                return self._send(200, get_stock(code, force))
            except Exception as e:
                return self._send(200, {"code": code.upper(), "error": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"null")
        except Exception:
            return self._send(400, {"error": "JSON が不正です"})
        if u.path == "/api/watchlist":
            if not isinstance(data, list):
                return self._send(400, {"error": "リスト形式で送ってください"})
            clean, seen = [], set()
            for it in data:
                code = str(it.get("code", "")).strip().upper()
                if not code or code in seen or not re.fullmatch(r"[0-9A-Z.\-^=]{1,15}", code):
                    continue
                seen.add(code)
                clean.append({"code": code, "memo": str(it.get("memo", ""))[:200]})
            save_json(WATCHLIST_PATH, clean)
            return self._send(200, clean)
        self._send(404, {"error": "not found"})


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    _cache.update(load_json(CACHE_PATH, {}))
    load_watchlist()
    print(f"割安チェッカー起動: http://127.0.0.1:{PORT}/")
    Server(("127.0.0.1", PORT), Handler).serve_forever()
