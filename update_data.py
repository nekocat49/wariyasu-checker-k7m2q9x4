#!/usr/bin/env python3
"""
割安チェッカー - データ更新スクリプト（Macなしで動かす用）

watchlist.json の銘柄の株価・配当・PER・PBR などを Yahoo Finance から取得し、
data.json に書き出します。index.html と data.json を同じ場所に置けば、
サーバーなしの静的サイトとしてスマホからも閲覧できます。

  使い方:  pip install -r requirements.txt
           python3 update_data.py

推奨実行タイミング: 平日 16:30（日本時間）ごろ = 東証の引け後に1日1回
取得に失敗した銘柄は、前回の data.json の値を残します（"stale": true）。
"""
import json, os, sys, time
from datetime import datetime, timezone, timedelta

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
import server  # fetch_stock などを共用

DATA_PATH = os.path.join(DIR, "data.json")
JST = timezone(timedelta(hours=9))


def main():
    watchlist = server.load_watchlist()
    prev = server.load_json(DATA_PATH, {}).get("stocks", {})
    stocks, ok, ng = {}, 0, 0
    for w in watchlist:
        code = w["code"]
        for attempt in range(3):
            try:
                d = server.fetch_stock(code)
                stocks[code] = d
                ok += 1
                print(f"✅ {code} {d['name']}")
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(3)
                    continue
                ng += 1
                if code in prev:
                    old = dict(prev[code])
                    old["stale"] = True
                    old["error"] = str(e)
                    stocks[code] = old
                    print(f"⚠ {code} 取得失敗 → 前回値を使用: {e}")
                else:
                    stocks[code] = {"code": code, "error": str(e)}
                    print(f"❌ {code} 取得失敗: {e}")
        time.sleep(0.5)  # 取得先への負荷を抑える

    out = {
        "generatedAt": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "watchlist": watchlist,
        "stocks": stocks,
    }
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, DATA_PATH)
    print(f"\n完了: 成功 {ok} / 失敗 {ng} → {DATA_PATH}")
    # 1銘柄も取れなかったら異常終了（定期実行の失敗に気づけるように）
    if ok == 0 and watchlist:
        sys.exit(1)


if __name__ == "__main__":
    main()
