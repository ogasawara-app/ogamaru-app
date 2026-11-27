#!/usr/bin/env python3
"""
おがさわら丸・ははじま丸 公式情報自動取得 & 通知スクリプト
----------------------------------------------------------------
機能:
1. 小笠原海運（公式サイト・小笠原種まきブログ）および東京宝島うみそら便（母島）から最新情報を取得
2. 凡例テキストを除外したうえで、当日の運航状況・緊急変更キーワードを正確に検知
3. 変更対象の日付・新時刻を構造化して schedule_data.json に出力（画面側と自動連動）
4. 差分があれば schedule_history.csv へ自動追記し、LINE / Webhook へ通知を送信
"""

import argparse
import csv
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
    "ogamaru_blog": "https://blog.livedoor.jp/ogasawarakaiun/",
    "ogamaru_service": "https://www.ogasawarakaiun.co.jp/service/",
    "hahajima_service": "https://www.ogasawarakaiun.co.jp/service/hahajima.html",
    "hahajima_status": "https://www.islandaccess.metro.tokyo.lg.jp/traffic/hahajima/",
}

ALERT_KEYWORDS = [
    "変更", "遅延", "欠航", "繰り上げ", "繰り下げ", "延発",
    "代船", "台風", "抜港", "運休", "時間変更", "臨時"
]

OUTPUT_JSON = Path("schedule_data.json")
HISTORY_CSV = Path("schedule_history.csv")
USER_AGENT = "OgamaruScheduleBot/1.1 (+https://www.ogasawarakaiun.co.jp/)"


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
    main = soup.select_one("article, .entry-content, main, #main") or soup.body
    text = main.get_text(" ", strip=True) if main else soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)[:400]


def parse_ogamaru_info(html: str) -> List[Dict[str, Any]]:
    """お知らせ・新着情報一覧から直近記事と緊急キーワード有無を抽出する。"""
    soup = BeautifulSoup(html, "html.parser")
    items: List[Dict[str, Any]] = []

    articles = soup.select("article, .post, .info-list li, .news-list li")
    if not articles:
        for a in soup.select("a[href*='?p='], a[href*='/info/']"):
            title = a.get_text(" ", strip=True)
            if len(title) > 8:
                matched = [kw for kw in ALERT_KEYWORDS if kw in title]
                items.append({
                    "title": title,
                    "url": a.get("href", ""),
                    "is_alert": len(matched) > 0,
                    "matched_keywords": matched,
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


def parse_ogamaru_blog(html: str) -> List[Dict[str, Any]]:
    """小笠原種まきブログ（Livedoor）の最新記事を抽出する。"""
    soup = BeautifulSoup(html, "html.parser")
    items: List[Dict[str, Any]] = []
    articles = soup.select("article, .article-outer, .article")
    for art in articles[:5]:
        title_el = art.select_one("h1, h2, .article-title, .article-title a")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        if not title:
            continue
        link_el = art.select_one(".article-title a[href], h1 a[href], h2 a[href], a[href]")
        url = link_el["href"] if link_el else SOURCES["ogamaru_blog"]
        body_text = art.get_text(" ", strip=True)
        # 休業案内のみの定例記事は運航変更アラートから除外
        is_office_notice = "営業所" in title and "休業" in title and "出港" not in title and "入港" not in title
        matched = [] if is_office_notice else [kw for kw in ALERT_KEYWORDS if kw in title or kw in body_text]
        items.append({
            "title": title,
            "url": url,
            "is_alert": len(matched) > 0,
            "matched_keywords": matched,
        })
    return items


def parse_hahajima_status(html: str) -> Dict[str, str]:
    """
    東京宝島うみそら便（母島）から実際の運航ステータスを抽出する。
    ページ上部の凡例（「● 欠航・運休」「● 条件付就航・時間変更...」）を誤検知しないよう、
    凡例フレーズを除去したうえで港・便ごとのステータス表記を判定する。
    """
    soup = BeautifulSoup(html, "html.parser")
    # 凡例テキストを除去して誤判定を防ぐ
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    cleaned = text
    for legend_phrase in [
        "運航(予定)",
        "欠航・運休",
        "条件付就航・時間変更・ダイヤ変更・その他情報あり",
    ]:
        cleaned = cleaned.replace(legend_phrase, " ")

    # 港ブロック（母島 沖港周辺）または画像altテキストを確認
    alt_texts = " ".join(img.get("alt", "") for img in soup.select("img[alt]"))
    target_zone = f"{alt_texts} {cleaned}"

    status = "通常運航"
    if re.search(r"(全便欠航|本日欠航|欠航決定|欠航しております|運休しております)", target_zone):
        status = "欠航・運休情報あり"
    elif re.search(r"(時刻変更|時間変更|繰り上げ|繰り下げ|条件付)", target_zone):
        status = "時刻変更・条件付情報あり"
    elif "運航" in target_zone:
        status = "通常運航"
    else:
        status = "公式サイトで確認"

    return {
        "status": status,
        "source_url": SOURCES["hahajima_status"],
    }


def extract_schedule_override(text: str, now_jst: datetime) -> Optional[Dict[str, str]]:
    """
    お知らせや次回運航文から、日付・種別(arr/dep)・変更後時刻を簡易抽出する。
    例: 「12/1(火) 父島 10時00分発（繰り上げ）」「11/28(土) 父島 14:30着（遅延）」
    """
    if not any(kw in text for kw in ["変更", "遅延", "繰り上げ", "繰り下げ", "延発", "早め"]):
        return None

    m_date = re.search(r"(\d{1,2})[/月](\d{1,2})日?", text)
    m_time = re.search(r"(\d{1,2})[:時](\d{2})分?", text)
    if not (m_date and m_time):
        return None

    month = int(m_date.group(1))
    day = int(m_date.group(2))
    hour = int(m_time.group(1))
    minute = int(m_time.group(2))
    year = now_jst.year
    if month < now_jst.month - 6:
        year += 1

    target_date = f"{year}-{month:02d}-{day:02d}"
    new_time = f"{hour:02d}:{minute:02d}"

    evt_type = ""
    if any(k in text for k in ["入港", "着"]):
        evt_type = "arr"
    elif any(k in text for k in ["出港", "発"]):
        evt_type = "dep"

    if not evt_type:
        return None

    # 通常時刻と同じなら変更オーバーライド不要
    if (evt_type == "arr" and new_time == "11:00") or (evt_type == "dep" and new_time == "15:00"):
        return None

    return {
        "target_date": target_date,
        "type": evt_type,
        "new_time": new_time,
    }


def compute_fingerprint(data: Dict[str, Any]) -> str:
    """更新時刻を除いた実質データのハッシュ値を計算して差分を判定する。"""
    comparable = {
        "next_summary": data.get("next_summary"),
        "alerts": data.get("alerts"),
        "hahajima_status": data.get("hahajima_status"),
        "parsed_override": data.get("parsed_override"),
    }
    serialized = json.dumps(comparable, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def send_line_broadcast(message: str, token: str) -> bool:
    url = "https://api.line.me/v2/bot/message/broadcast"
    payload = json.dumps({"messages": [{"type": "text", "text": message}]}).encode("utf-8")
    req = Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (URLError, HTTPError) as e:
        print(f"[WARN] LINE通知の送信に失敗しました: {e}", file=sys.stderr)
        return False


def send_webhook(message: str, webhook_url: str) -> bool:
    payload = json.dumps({"text": message, "content": message}).encode("utf-8")
    req = Request(webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (URLError, HTTPError) as e:
        print(f"[WARN] Webhook通知の送信に失敗しました: {e}", file=sys.stderr)
        return False


def build_notification_text(data: Dict[str, Any]) -> str:
    """公式文章の丸ごと転載を避け、事実の検知と公式トップページへの案内を作成する。"""
    lines = ["【おが丸入出港カレンダー（非公式）】運航情報に更新がありました"]
    if data.get("alerts"):
        kws = sorted({kw for a in data["alerts"] for kw in a.get("matched_keywords", [])})
        kw_text = "・".join(kws) if kws else "運航変更"
        lines.append("")
        lines.append(f"⚠ 検知された変更種別: {kw_text}")
    if data.get("parsed_override"):
        ov = data["parsed_override"]
        kind = "入港" if ov["type"] == "arr" else "出港"
        lines.append(f"■ 検知時刻: {ov['target_date']} {kind} {ov['new_time']}")
    if data.get("hahajima_status"):
        lines.append(f"■ ははじま丸: {data['hahajima_status']['status']}")
    lines.append("")
    lines.append("詳細は公式サイトをご確認ください:")
    lines.append("https://www.ogasawarakaiun.co.jp/")
    lines.append(f"確認時刻: {data['updated_at']}")
    return "\n".join(lines)


def run_test_mode() -> int:
    """凡例誤検知防止・ブログ監視・時刻オーバーライド抽出をテストする。"""
    now_jst = datetime(2026, 11, 27, 15, 45, tzinfo=JST)
    sample_next_html = """
    <html><body><main>
      <p>次のおがさわら丸の運航は、11/28(土) 父島 14時30分着（遅延）の予定です。</p>
    </main></body></html>
    """
    sample_info_html = """
    <html><body>
      <article>
        <h2><a href="https://www.ogasawarakaiun.co.jp/info/?p=13584">【速報】2027年7～12月おがさわら丸時刻表</a></h2>
      </article>
    </body></html>
    """
    sample_blog_html = """
    <html><body>
      <article class="article">
        <h2 class="article-title"><a href="https://blog.livedoor.jp/ogasawarakaiun/archives/1083814561.html">11月24日～27日は休業となります（父島営業所）</a></h2>
      </article>
    </body></html>
    """
    # 凡例（「欠航・運休」等）を含んでいても、実際が「運航」なら誤判定しないことを確認
    sample_haha_html = """
    <html><body>
      <div>凡例: 運航(予定) 欠航・運休 条件付就航・時間変更・ダイヤ変更・その他情報あり</div>
      <div>【母島】母島 沖港 運航</div>
    </body></html>
    """

    next_summary = parse_ogamaru_next(sample_next_html)
    info_items = parse_ogamaru_info(sample_info_html) + parse_ogamaru_blog(sample_blog_html)
    alerts = [item for item in info_items if item["is_alert"]]
    haha_status = parse_hahajima_status(sample_haha_html)
    parsed_override = extract_schedule_override(next_summary, now_jst)

    result = {
        "updated_at": now_jst.strftime("%Y-%m-%d %H:%M JST"),
        "next_summary": next_summary,
        "alerts": alerts,
        "recent_info": info_items,
        "hahajima_status": haha_status,
        "parsed_override": parsed_override,
        "sources": SOURCES,
    }
    result["fingerprint"] = compute_fingerprint(result)

    assert haha_status["status"] == "通常運航", f"Unexpected haha_status: {haha_status}"
    assert parsed_override == {"target_date": "2026-11-28", "type": "arr", "new_time": "14:30"}, f"Unexpected override: {parsed_override}"
    print("ALL TESTS PASSED!")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="おがさわら丸・ははじま丸 公式情報更新スクリプト")
    parser.add_argument("--test", action="store_true", help="モックHTMLでローカル動作確認を行う")
    parser.add_argument("--output", default=str(OUTPUT_JSON), help="出力JSONファイルパス")
    args = parser.parse_args()

    if args.test:
        return run_test_mode()

    now_jst = datetime.now(JST)
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

    blog_items: List[Dict[str, Any]] = []
    try:
        blog_html = fetch_html(SOURCES["ogamaru_blog"])
        blog_items = parse_ogamaru_blog(blog_html)
    except Exception as e:
        print(f"[WARN] 公式ブログの取得をスキップしました: {e}", file=sys.stderr)

    next_summary = parse_ogamaru_next(next_html)
    info_items = parse_ogamaru_info(info_html) + blog_items
    alerts = [item for item in info_items if item["is_alert"]]
    haha_status = parse_hahajima_status(haha_html)
    parsed_override = extract_schedule_override(next_summary, now_jst)

    new_data = {
        "updated_at": now_jst.strftime("%Y-%m-%d %H:%M JST"),
        "next_summary": next_summary,
        "alerts": alerts,
        "recent_info": info_items,
        "hahajima_status": haha_status,
        "parsed_override": parsed_override,
        "sources": SOURCES,
    }
    new_fingerprint = compute_fingerprint(new_data)
    new_data["fingerprint"] = new_fingerprint

    changed = new_fingerprint != prev_fingerprint
    out_path.write_text(json.dumps(new_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[INFO] {out_path} を保存しました (changed={changed}, alerts={len(alerts)}, haha={haha_status['status']})")

    if changed:
        is_new_csv = not HISTORY_CSV.exists()
        kws = "・".join(sorted({kw for a in alerts for kw in a.get("matched_keywords", [])})) if alerts else "通常更新"
        with HISTORY_CSV.open("a", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            if is_new_csv:
                writer.writerow(["記録日時", "変更種別", "ははじま丸状況", "次回運航概要"])
            writer.writerow([
                new_data["updated_at"],
                kws,
                haha_status.get("status", ""),
                next_summary[:160],
            ])

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

    return 0


if __name__ == "__main__":
    sys.exit(main())
