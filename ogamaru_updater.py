#!/usr/bin/env python3
"""
おがさわら丸・ははじま丸 公式情報自動取得 & 通知スクリプト
----------------------------------------------------------------
機能:
1. 小笠原海運（時刻表・お知らせ・運航予定）および東京宝島うみそら便（母島）から最新情報を取得
2. 緊急の変更キーワード（変更・遅延・欠航・繰り上げ等）を検知
3. 前回保存した schedule_data.json と差分比較し、更新があれば JSON を上書き
4. LINE Messaging API / Webhook（Slack・Discord等）へ即時通知を送信

使い方:
  通常実行: python3 ogamaru_updater.py
  ローカルテスト: python3 ogamaru_updater.py --test
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))

SOURCES = {
    "ogamaru_info": "https://www.ogasawarakaiun.co.jp/info/",
    "ogamaru_next": "https://www.ogasawarakaiun.co.jp/info/?page_id=9",
    "ogamaru_service": "https://www.ogasawarakaiun.co.jp/service/",
    "hahajima_service": "https://www.ogasawarakaiun.co.jp/service/hahajima.html",
    "hahajima_status": "https://www.islandaccess.metro.tokyo.lg.jp/traffic/hahajima/",
}

ALERT_KEYWORDS = [
    "変更", "遅延", "欠航", "繰り上げ", "繰り下げ", "延発",
    "代船", "台風", "抜港", "運休", "時間変更", "臨時"
]

OUTPUT_JSON = Path("schedule_data.json")
USER_AGENT = "OgamaruScheduleBot/1.0 (+https://www.ogasawarakaiun.co.jp/)"


def fetch_html(url: str, timeout: int = 15) -> str:
    """公式サイトからHTMLを取得する。"""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def parse_ogamaru_next(html: str) -> str:
    """次回運航予定ページ (?page_id=9) の本文を抽出する。"""
    soup = BeautifulSoup(html, "html.parser")
    # 本文エリアを優先して取得
    main = soup.select_one("article, .entry-content, main, #main") or soup.body
    text = main.get_text(" ", strip=True) if main else soup.get_text(" ", strip=True)
    # 余分な連続空白を整理
    return re.sub(r"\s+", " ", text)[:400]


def parse_ogamaru_info(html: str) -> List[Dict[str, Any]]:
    """お知らせ・新着情報一覧から直近記事と緊急キーワード有無を抽出する。"""
    soup = BeautifulSoup(html, "html.parser")
    items: List[Dict[str, Any]] = []

    # WordPress等の一般的な記事構造に対応
    articles = soup.select("article, .post, .info-list li, .news-list li")
    if not articles:
        # フォールバック: リンク一覧から抽出
        for a in soup.select("a[href*='?p='], a[href*='/info/']"):
            title = a.get_text(" ", strip=True)
            if len(title) > 8:
                is_alert = any(kw in title for kw in ALERT_KEYWORDS)
                items.append({
                    "title": title,
                    "url": a.get("href", ""),
                    "is_alert": is_alert,
                    "matched_keywords": [kw for kw in ALERT_KEYWORDS if kw in title],
                })
        return items[:10]

    for art in articles[:10]:
        title_el = art.select_one("h2, h3, .entry-title, a")
        title = title_el.get_text(" ", strip=True) if title_el else art.get_text(" ", strip=True)[:80]
        link_el = art.select_one("a[href]")
        url = link_el["href"] if link_el else SOURCES["ogamaru_info"]
        body_text = art.get_text(" ", strip=True)
        matched = [kw for kw in ALERT_KEYWORDS if kw in title or kw in body_text]
        items.append({
            "title": title,
            "url": url,
            "is_alert": len(matched) > 0,
            "matched_keywords": matched,
        })
    return items


def parse_hahajima_status(html: str) -> Dict[str, str]:
    """東京宝島うみそら便（母島）から運航ステータスを抽出する。"""
    soup = BeautifulSoup(html, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    status = "通常運航（確認要）"
    if "欠航" in text or "運休" in text:
        status = "欠航・運休情報あり"
    elif "条件付" in text or "時間変更" in text or "ダイヤ変更" in text:
        status = "条件付・時間変更あり"
    elif "運航" in text:
        status = "運航(予定)"
    return {
        "status": status,
        "source_url": SOURCES["hahajima_status"],
    }


def compute_fingerprint(data: Dict[str, Any]) -> str:
    """更新時刻を除いた実質データのハッシュ値を計算して差分を判定する。"""
    comparable = {
        "next_summary": data.get("next_summary"),
        "alerts": data.get("alerts"),
        "hahajima_status": data.get("hahajima_status"),
    }
    serialized = json.dumps(comparable, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def send_line_broadcast(message: str, token: str) -> bool:
    """LINE Messaging API (Broadcast) で登録ユーザー全員へ通知を送る。"""
    url = "https://api.line.me/v2/bot/message/broadcast"
    payload = json.dumps({
        "messages": [{"type": "text", "text": message}]
    }).encode("utf-8")
    req = Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (URLError, HTTPError) as e:
        print(f"[WARN] LINE通知の送信に失敗しました: {e}", file=sys.stderr)
        return False


def send_webhook(message: str, webhook_url: str) -> bool:
    """Slack / Discord 等のWebhookへ通知を送る。"""
    payload = json.dumps({"text": message, "content": message}).encode("utf-8")
    req = Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (URLError, HTTPError) as e:
        print(f"[WARN] Webhook通知の送信に失敗しました: {e}", file=sys.stderr)
        return False


def build_notification_text(data: Dict[str, Any]) -> str:
    """通知用の読みやすいメッセージを作成する。"""
    lines = ["【おがまる便り】運航情報に更新がありました"]
    if data.get("alerts"):
        lines.append("")
        lines.append("⚠ 変更・注意のお知らせ:")
        for a in data["alerts"][:3]:
            lines.append(f"・{a['title']}")
            if a.get("url"):
                lines.append(f"  {a['url']}")
    if data.get("next_summary"):
        lines.append("")
        lines.append(f"■ 次回運航予定:\n{data['next_summary'][:140]}")
    if data.get("hahajima_status"):
        lines.append("")
        lines.append(f"■ ははじま丸: {data['hahajima_status']['status']}")
    lines.append("")
    lines.append(f"確認時刻: {data['updated_at']}")
    return "\n".join(lines)


def run_test_mode() -> int:
    """ネットワークを使わずにローカルのモックHTMLでパースと差分・通知文生成をテストする。"""
    sample_next_html = """
    <html><body><main>
      <p>次のおがさわら丸の運航は、11/28(土) 父島 14時30分着（遅延）、12/1(火) 父島 10時00分発（繰り上げ）の予定です。</p>
    </main></body></html>
    """
    sample_info_html = """
    <html><body>
      <article>
        <h2><a href="https://www.ogasawarakaiun.co.jp/info/?p=99999">【重要】台風接近に伴う12/1父島発の出港時刻繰り上げについて</a></h2>
      </article>
      <article>
        <h2><a href="https://www.ogasawarakaiun.co.jp/info/?p=13584">【速報】2027年7～12月おがさわら丸時刻表</a></h2>
      </article>
    </body></html>
    """
    sample_haha_html = "<html><body><div>母島 沖港 運航(予定)</div></body></html>"

    next_summary = parse_ogamaru_next(sample_next_html)
    info_items = parse_ogamaru_info(sample_info_html)
    alerts = [item for item in info_items if item["is_alert"]]
    haha_status = parse_hahajima_status(sample_haha_html)

    result = {
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "next_summary": next_summary,
        "alerts": alerts,
        "recent_info": info_items,
        "hahajima_status": haha_status,
        "sources": SOURCES,
    }
    result["fingerprint"] = compute_fingerprint(result)

    print("=== パース結果 (テストモード) ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n=== 生成された通知メッセージ ===")
    print(build_notification_text(result))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="おがさわら丸・ははじま丸 公式情報更新スクリプト")
    parser.add_argument("--test", action="store_true", help="モックHTMLでローカル動作確認を行う")
    parser.add_argument("--output", default=str(OUTPUT_JSON), help="出力JSONファイルパス")
    args = parser.parse_args()

    if args.test:
        return run_test_mode()

    out_path = Path(args.output)
    prev_fingerprint: Optional[str] = None
    if out_path.exists():
        try:
            prev_data = json.loads(out_path.read_text(encoding="utf-8"))
            prev_fingerprint = prev_data.get("fingerprint")
        except Exception:
            prev_fingerprint = None

    try:
        next_html = fetch_html(SOURCES["ogamaru_next"])
        info_html = fetch_html(SOURCES["ogamaru_info"])
        haha_html = fetch_html(SOURCES["hahajima_status"])
    except (URLError, HTTPError) as e:
        print(f"[ERROR] 公式サイトの取得に失敗しました: {e}", file=sys.stderr)
        return 1

    next_summary = parse_ogamaru_next(next_html)
    info_items = parse_ogamaru_info(info_html)
    alerts = [item for item in info_items if item["is_alert"]]
    haha_status = parse_hahajima_status(haha_html)

    new_data = {
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "next_summary": next_summary,
        "alerts": alerts,
        "recent_info": info_items,
        "hahajima_status": haha_status,
        "sources": SOURCES,
    }
    new_fingerprint = compute_fingerprint(new_data)
    new_data["fingerprint"] = new_fingerprint

    changed = new_fingerprint != prev_fingerprint
    out_path.write_text(json.dumps(new_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[INFO] {out_path} を保存しました (changed={changed}, alerts={len(alerts)})")

    # 差分があった場合（特に緊急変更がある場合）に通知を送る
    if changed and (alerts or prev_fingerprint is not None):
        msg = build_notification_text(new_data)
        line_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        webhook_url = os.environ.get("WEBHOOK_URL", "").strip()

        if line_token:
            ok = send_line_broadcast(msg, line_token)
            print(f"[INFO] LINE通知送信: {'成功' if ok else '失敗'}")
        if webhook_url:
            ok = send_webhook(msg, webhook_url)
            print(f"[INFO] Webhook通知送信: {'成功' if ok else '失敗'}")
        if not line_token and not webhook_url:
            print("[INFO] 通知先環境変数 (LINE_CHANNEL_ACCESS_TOKEN / WEBHOOK_URL) が未設定のため通知はスキップしました。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
