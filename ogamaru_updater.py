#!/usr/bin/env python3
"""
おがさわら丸・ははじま丸 公式情報自動取得 & 通知スクリプト (Ver.1.3)
----------------------------------------------------------------
機能:
1. 小笠原海運（公式サイト・小笠原種まきブログAtom）および東京宝島うみそら便（母島）から最新情報を取得
2. フェイルセーフ設計: 抽出失敗・構造変更時はデフォルトで「通常運航」を返さず「確認不能」を返す
3. 確定した時刻変更を persisted_overrides として永続保持し、公式お知らせ消去後も先祖返りしない
4. ブログAtomフィード取得時のHTTPステータス・パースエラーを詳細ログ出力
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))

SOURCES = {
    "ogamaru_info": "https://www.ogasawarakaiun.co.jp/info/",
    "ogamaru_next": "https://www.ogasawarakaiun.co.jp/info/?page_id=9",
    "ogamaru_blog_feed": "https://blog.livedoor.jp/ogasawarakaiun/atom.xml",
    "ogamaru_blog_feed_http": "http://blog.livedoor.jp/ogasawarakaiun/atom.xml",
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
USER_AGENT = "Mozilla/5.0 (compatible; OgamaruScheduleBot/1.3; +https://ogasawara-app.github.io/)"


def fetch_html(url: str, timeout: int = 15) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def parse_ogamaru_next(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("article, .entry-content, main, #main") or soup.body
    text = main.get_text(" ", strip=True) if main else soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)[:400]


def parse_ogamaru_info(html: str) -> List[Dict[str, Any]]:
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


def parse_ogamaru_blog_atom(xml_text: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """小笠原種まきブログのAtomフィード(XML)から最新記事を抽出し、エラーがあれば理由も返す。"""
    items: List[Dict[str, Any]] = []
    cleaned = xml_text.strip()
    if not cleaned:
        return items, "empty_response"
    if not cleaned.startswith("<"):
        return items, f"not_xml_starts_with={cleaned[:40]!r}"

    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as e:
        return items, f"xml_parse_error: {e}"

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall("atom:entry", ns) or root.findall("entry")
    if not entries:
        return items, f"no_entry_elements_found (root_tag={root.tag})"

    def find_el(parent, *paths):
        for p in paths:
            el = parent.find(p, ns)
            if el is not None:
                return el
        return None

    for entry in entries[:5]:
        title_el = find_el(entry, "atom:title", "title")
        link_el = find_el(entry, "atom:link[@rel='alternate']", "atom:link", "link")
        summary_el = find_el(entry, "atom:summary", "atom:content", "summary")

        title = (title_el.text or "").strip() if title_el is not None else ""
        if not title:
            continue
        url = link_el.attrib.get("href", SOURCES["ogamaru_blog"]) if link_el is not None else SOURCES["ogamaru_blog"]
        summary_text = (summary_el.text or "") if summary_el is not None else ""
        is_office_notice = "営業所" in title and "休業" in title and "出港" not in title and "入港" not in title
        matched = [] if is_office_notice else [kw for kw in ALERT_KEYWORDS if kw in title or kw in summary_text]
        items.append({
            "title": title,
            "url": url,
            "is_alert": len(matched) > 0,
            "matched_keywords": matched,
        })
    return items, None


def parse_hahajima_status(html: str) -> Dict[str, str]:
    """
    東京宝島うみそら便（母島）のステータスを抽出する（フェイルセーフ徹底版）。
    ・抽出失敗・HTML構造変更時はデフォルトで「通常運航」を返さず「確認不能」を返す。
    ・明示的に「通常運航」または「運航」が確認できた場合のみ「通常運航」を返す。
    ・画像alt属性による欠航・条件付表示にも対応する。
    """
    if not html or not html.strip():
        return {"status": "確認不能", "reason": "empty_html", "source_url": SOURCES["hahajima_status"]}

    soup = BeautifulSoup(html, "html.parser")
    # Convert image alt texts into inline tokens so icon-only status badges are detected
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        if alt:
            img.replace_with(f" [{alt}] ")

    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    # 「【母島】」または「母島 沖港」以降を切り出し、フッターの注意書きより前だけを見る
    m = re.search(r"(【母島】|母島\s*沖港)(.*?)(本ページの情報に基づく|大型客船は|$)", text)
    if not m:
        return {"status": "確認不能", "reason": "hahajima_section_not_found", "source_url": SOURCES["hahajima_status"]}

    port_section = m.group(2)

    # 凡例そのものが混入していた場合は除去
    for legend_phrase in [
        "[運航(予定)]",
        "[欠航・運休]",
        "[条件付就航・時間変更・ダイヤ変更・その他情報あり]",
        "運航(予定)",
        "欠航・運休",
        "条件付就航・時間変更・ダイヤ変更・その他情報あり",
    ]:
        port_section = port_section.replace(legend_phrase, " ")

    port_section = re.sub(r"\s+", " ", port_section).strip()
    if not port_section:
        return {"status": "確認不能", "reason": "empty_port_section", "source_url": SOURCES["hahajima_status"]}

    if re.search(r"(全便欠航|本日欠航|欠航決定|欠航|運休)", port_section):
        status = "欠航・運休情報あり"
    elif re.search(r"(時刻変更|時間変更|繰り上げ|繰り下げ|条件付)", port_section):
        status = "時刻変更・条件付情報あり"
    elif re.search(r"(通常運航|運航)", port_section):
        status = "通常運航"
    else:
        status = "確認不能"

    return {
        "status": status,
        "source_url": SOURCES["hahajima_status"],
    }


def extract_schedule_override(text: str, now_jst: datetime) -> Optional[Dict[str, str]]:
    """
    変更前後の時刻を誤認しないよう、明確なパターンがある場合のみ時刻を抽出する。
    """
    if not any(kw in text for kw in ["変更", "遅延", "繰り上げ", "繰り下げ", "延発", "早め"]):
        return None

    m_date = re.search(r"(\d{1,2})[/月](\d{1,2})日?", text)
    if not m_date:
        return None

    m_after = re.search(r"(?:から|→|⇒|へ)\s*(\d{1,2})[:時](\d{2})分?", text)
    all_times = re.findall(r"(\d{1,2})[:時](\d{2})分?", text)

    if m_after:
        hour, minute = int(m_after.group(1)), int(m_after.group(2))
    elif len(all_times) == 1:
        hour, minute = int(all_times[0][0]), int(all_times[0][1])
    else:
        return None

    month, day = int(m_date.group(1)), int(m_date.group(2))
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

    if (evt_type == "arr" and new_time == "11:00") or (evt_type == "dep" and new_time == "15:00"):
        return None

    return {
        "target_date": target_date,
        "type": evt_type,
        "new_time": new_time,
    }


def merge_persisted_overrides(
    existing: List[Dict[str, str]],
    new_override: Optional[Dict[str, str]],
    today_str: str,
) -> List[Dict[str, str]]:
    """
    確定した運航時刻変更を永続保存し、公式サイトから速報が消えても変更前のダイヤに戻らないようにする。
    """
    by_key: Dict[Tuple[str, str], Dict[str, str]] = {}
    for item in existing:
        if isinstance(item, dict) and item.get("target_date") and item.get("type") and item.get("new_time"):
            by_key[(item["target_date"], item["type"])] = {
                "target_date": item["target_date"],
                "type": item["type"],
                "new_time": item["new_time"],
            }
    if new_override:
        by_key[(new_override["target_date"], new_override["type"])] = dict(new_override)

    # Sort chronologically and retain recent/future records
    merged = sorted(by_key.values(), key=lambda x: (x["target_date"], x["type"]))
    return merged[-50:]


def compute_fingerprint(data: Dict[str, Any]) -> str:
    comparable = {
        "next_summary": data.get("next_summary"),
        "alerts": data.get("alerts"),
        "hahajima_status": data.get("hahajima_status"),
        "parsed_override": data.get("parsed_override"),
        "persisted_overrides": data.get("persisted_overrides"),
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
    lines = ["【おが丸・はは丸 入出港カレンダー（非公式）】運航情報に更新がありました"]
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
    now_jst = datetime(2026, 11, 27, 16, 0, tzinfo=JST)

    # 1) 凡例付きの実サイトに近いHTMLで通常運航と判定されること
    sample_haha_html = """
    <html><body>
      <div class="legend">
        <img alt="運航(予定)" src="a.png"> 運航(予定)
        <img alt="欠航・運休" src="b.png"> 欠航・運休
        <img alt="条件付就航・時間変更・ダイヤ変更・その他情報あり" src="c.png"> 条件付就航・時間変更・ダイヤ変更・その他情報あり
      </div>
      <div class="port">【母島】母島 沖港 運航</div>
      <footer>本ページの情報に基づく損害については責任を負いかねます。</footer>
    </body></html>
    """
    assert parse_hahajima_status(sample_haha_html)["status"] == "通常運航"

    # 2) 構造変更・情報消失時は「通常運航」ではなく「確認不能」を返すこと（フェイルセーフ）
    broken_html = "<html><body><div>メンテナンス中です</div></body></html>"
    assert parse_hahajima_status(broken_html)["status"] == "確認不能"

    # 3) 画像altのみで欠航表示されている場合も検知できること
    img_cancel_html = """
    <html><body>
      <div class="port">【母島】母島 沖港 <img alt="欠航" src="cancel.png"></div>
      <footer>本ページの情報に基づく</footer>
    </body></html>
    """
    assert parse_hahajima_status(img_cancel_html)["status"] == "欠航・運休情報あり"

    # 4) 「15時00分から13時00分に変更」で変更後の13:00を正しく取得すること
    ov1 = extract_schedule_override("12月1日の父島出港時刻を15時00分から13時00分に変更します。", now_jst)
    assert ov1 == {"target_date": "2026-12-01", "type": "dep", "new_time": "13:00"}, f"ov1={ov1}"

    # 5) 曖昧な複数時刻は誤上書きせず None を返すこと
    ov2 = extract_schedule_override("12月1日の11:00入港、15:00出港は変更の可能性があります。", now_jst)
    assert ov2 is None, f"ov2={ov2}"

    # 6) 確定した時刻変更が永続保持されること
    merged = merge_persisted_overrides([ov1], None, "2026-11-28")
    assert len(merged) == 1 and merged[0]["new_time"] == "13:00"

    # 7) Atomフィードからブログ記事が取得できること
    sample_atom = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>11月24日～27日は休業となります（父島営業所）</title>
        <link rel="alternate" href="http://blog.livedoor.jp/ogasawarakaiun/archives/1.html" />
      </entry>
      <entry>
        <title>おがさわら丸は11月3日父島発に変更になります</title>
        <link rel="alternate" href="http://blog.livedoor.jp/ogasawarakaiun/archives/2.html" />
      </entry>
    </feed>"""
    blog_items, err = parse_ogamaru_blog_atom(sample_atom)
    assert err is None and len(blog_items) == 2
    assert blog_items[0]["is_alert"] is False
    assert blog_items[1]["is_alert"] is True

    print("ALL STRICT TESTS PASSED!")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="おがさわら丸・ははじま丸 公式情報更新スクリプト")
    parser.add_argument("--test", action="store_true", help="テストを実行する")
    parser.add_argument("--output", default=str(OUTPUT_JSON), help="出力JSONファイルパス")
    args = parser.parse_args()

    if args.test:
        return run_test_mode()

    now_jst = datetime.now(JST)
    out_path = Path(args.output)
    prev_fingerprint: Optional[str] = None
    existing_overrides: List[Dict[str, str]] = []
    if out_path.exists():
        try:
            prev_data = json.loads(out_path.read_text(encoding="utf-8"))
            prev_fingerprint = prev_data.get("fingerprint")
            existing_overrides = prev_data.get("persisted_overrides") or []
        except Exception:
            prev_fingerprint = None

    source_status: Dict[str, str] = {}

    try:
        next_html = fetch_html(SOURCES["ogamaru_next"])
        source_status["ogamaru_next"] = "ok"
        info_html = fetch_html(SOURCES["ogamaru_info"])
        source_status["ogamaru_info"] = "ok"
    except (URLError, HTTPError) as e:
        print(f"[ERROR] 小笠原海運公式サイトの取得に失敗しました: {e}", file=sys.stderr)
        return 1

    try:
        haha_html = fetch_html(SOURCES["hahajima_status"])
        haha_status = parse_hahajima_status(haha_html)
        source_status["hahajima_status"] = "ok" if haha_status["status"] != "確認不能" else "parse_uncertain"
    except Exception as e:
        print(f"[WARN] ははじま丸運航ページの取得に失敗しました: {e}", file=sys.stderr)
        haha_status = {"status": "確認不能", "reason": str(e), "source_url": SOURCES["hahajima_status"]}
        source_status["hahajima_status"] = f"error: {e}"

    blog_items: List[Dict[str, Any]] = []
    blog_debug = ""
    for feed_key in ("ogamaru_blog_feed", "ogamaru_blog_feed_http"):
        try:
            atom_xml = fetch_html(SOURCES[feed_key])
            parsed_items, parse_err = parse_ogamaru_blog_atom(atom_xml)
            if parse_err:
                blog_debug = f"{feed_key}: {parse_err}"
                print(f"[WARN] 公式ブログAtom解析警告 ({blog_debug})", file=sys.stderr)
            else:
                blog_items = parsed_items
                blog_debug = f"{feed_key}: ok ({len(blog_items)} items)"
                break
        except Exception as e:
            blog_debug = f"{feed_key}: fetch_error={e}"
            print(f"[WARN] 公式ブログフィード取得失敗 ({blog_debug})", file=sys.stderr)
    source_status["ogamaru_blog_feed"] = blog_debug

    next_summary = parse_ogamaru_next(next_html)
    info_items = parse_ogamaru_info(info_html) + blog_items
    alerts = [item for item in info_items if item["is_alert"]]
    parsed_override = extract_schedule_override(next_summary, now_jst)
    persisted_overrides = merge_persisted_overrides(existing_overrides, parsed_override, now_jst.strftime("%Y-%m-%d"))

    new_data = {
        "updated_at": now_jst.strftime("%Y-%m-%d %H:%M JST"),
        "updated_iso": now_jst.isoformat(),
        "next_summary": next_summary,
        "alerts": alerts,
        "recent_info": info_items,
        "hahajima_status": haha_status,
        "parsed_override": parsed_override,
        "persisted_overrides": persisted_overrides,
        "source_status": source_status,
        "sources": SOURCES,
    }
    new_fingerprint = compute_fingerprint(new_data)
    new_data["fingerprint"] = new_fingerprint

    changed = new_fingerprint != prev_fingerprint
    out_path.write_text(json.dumps(new_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[INFO] {out_path} を保存しました (changed={changed}, alerts={len(alerts)}, blog={len(blog_items)}, haha={haha_status['status']})")

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
