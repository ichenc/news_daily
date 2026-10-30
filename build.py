# -*- coding: utf-8 -*-
"""
每日新闻联播文字版脚本（GitHub Actions 版）

"""

import html as html_mod
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}


def log(*a):
    print(f"[{datetime.now().strftime('%H:%M:%S')}]", *a, flush=True)


def http_get(url, timeout=25, retries=3):
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return r.read()
        except Exception as e:
            last_err = e
            log(f"  请求第 {i+1} 次失败: {e}")
            if i < retries - 1:
                time.sleep(3)
    raise last_err


# ---------- 抓新闻联播节目单 ----------

def fetch_xwlb_items(target: datetime):
    ymd = target.strftime("%Y%m%d")
    url = f"https://tv.cctv.com/lm/xwlb/day/{ymd}.shtml"
    log(f"抓取: {url}")
    raw = http_get(url).decode("utf-8", "ignore")
    log(f"  页面长度: {len(raw)}")

    pattern = re.compile(
        r'<a\s+href="(https://tv\.cctv\.com/[^"]+)"[^>]*title="([^"]+)"[^>]*>\s*'
        r'<i class="sql\d+">[^<]*</i>([^<]*)</a>',
        re.S,
    )

    full_link, items = None, []
    for m in pattern.finditer(raw):
        href, title_attr, after_i = m.groups()
        title = (after_i or title_attr).strip() or title_attr.strip()
        title = re.sub(r"^\[视频\]", "", title).strip()
        if "新闻联播" in title and "19:00" in title:
            full_link = href
            continue
        if title:
            items.append({"title": title, "url": href})

    # 备用正则: 如果主正则没匹配到, 用更宽松的
    if not items:
        for m in re.finditer(
            r'<a\s+href="(https://tv\.cctv\.com/20\d\d/[^"]+)"[^>]*title="([^"]+)"', raw
        ):
            href, t = m.group(1), re.sub(r"^\[视频\]", "", m.group(2)).strip()
            if "19:00" in t and "新闻联播" in t:
                full_link = href
                continue
            if t:
                items.append({"title": t, "url": href})

    log(f"  抓到 {len(items)} 条")
    return full_link, items


# ---------- 渲染 ----------

def render_day_html(target, full_link, items):
    cn = f"{target.year}年{target.month}月{target.day}日"
    rows = []
    for i, it in enumerate(items, 1):
        rows.append(
            f'<li class="item"><span class="num">{i:02d}</span>'
            f'<a href="{html_mod.escape(it["url"])}" target="_blank">{html_mod.escape(it["title"])}</a></li>'
        )
    full = (
        f'<p class="full">完整节目视频: <a href="{html_mod.escape(full_link)}" target="_blank">央视网点播页</a></p>'
        if full_link else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>新闻联播 {cn}</title>
<style>
body{{font-family:-apple-system,"Microsoft YaHei",sans-serif;max-width:820px;margin:40px auto;padding:0 20px;color:#222;line-height:1.7}}
h1{{font-size:22px;border-left:4px solid #c00;padding-left:10px}}
.meta{{color:#888;font-size:13px;margin-bottom:24px}}
.full{{background:#f6f6f6;padding:10px 14px;border-radius:6px;font-size:14px}}
ol{{padding-left:0;list-style:none}}
.item{{padding:10px 0;border-bottom:1px solid #eee;display:flex;gap:12px;align-items:baseline}}
.num{{color:#c00;font-weight:700;font-size:14px;min-width:28px}}
a{{color:#1a5fb4;text-decoration:none}}a:hover{{text-decoration:underline}}
footer{{margin-top:30px;color:#aaa;font-size:12px}}
</style></head><body>
<h1>新闻联播 · {cn}</h1>
<div class="meta">来源: 央视网 tv.cctv.com/lm/xwlb · 自动整理</div>
{full}
<ol>{chr(10).join(rows)}</ol>
<footer>共 {len(items)} 条 · 由 news_daily 自动生成</footer>
</body></html>"""


def render_index_html(entries):
    rows = []
    for e in entries:
        rows.append(
            f'<tr><td><a href="./{urllib.parse.quote(e["path"], safe="/")}">{html_mod.escape(e["date_cn"])}</a></td>'
            f'<td style="text-align:center">{e["count"]} 条</td></tr>'
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日新闻联播文字版归档</title>
<style>
body{{font-family:-apple-system,"Microsoft YaHei",sans-serif;max-width:820px;margin:40px auto;padding:0 20px;color:#222}}
h1{{font-size:22px;border-left:4px solid #c00;padding-left:10px}}
table{{width:100%;border-collapse:collapse;margin-top:20px}}
td{{padding:10px 8px;border-bottom:1px solid #eee}}
a{{color:#1a5fb4;text-decoration:none}}
.meta{{color:#888;font-size:13px}}
</style></head><body>
<h1>每日新闻联播文字版归档</h1>
<div class="meta">来源: 央视网 · 每日 20:00 自动更新 · 共 {len(entries)} 期</div>
<table><thead><tr><th>日期</th><th style="width:100px;text-align:center">条数</th></tr></thead>
<tbody>{chr(10).join(rows)}</tbody></table>
</body></html>"""


def render_readme(entries):
    lines = ["# 每日新闻联播文字版归档", "",
             f"> 来源: 央视网 · 每日 20:00(北京时间) 自动更新 · 共 {len(entries)} 期",
             "", "| 日期 | 条数 | 文字版 |", "|---|---:|---|"]
    for e in entries:
        lines.append(
            f"| {e['date_cn']} | {e['count']} | [查看](./{urllib.parse.quote(e['path'], safe='/')}) |"
        )
    lines += ["", "## 说明", "",
              "- 每天由 GitHub Actions 自动抓取央视网当日《新闻联播》节目单",
              "- 开启 GitHub Pages 后, 根目录 index.html 即为归档主页",
              "- 飞书群推送通过仓库 Secret `FEISHU_WEBHOOK` 配置",
              ""]
    return "\n".join(lines)


# ---------- 飞书 ----------

def send_feishu(target, items):
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        log("[WARN] 未设置 FEISHU_WEBHOOK, 跳过飞书推送")
        return

    cn = f"{target.year}年{target.month}月{target.day}日"
    lines = [[{"tag": "text", "text": f"📺 新闻联播 {cn} 共 {len(items)} 条\n"}]]
    for i, it in enumerate(items, 1):
        lines.append([
            {"tag": "text", "text": f"{i}. "},
            {"tag": "a", "text": it["title"], "href": it["url"]},
        ])
    payload = {"msg_type": "post",
               "content": {"post": {"zh_cn": {"title": f"新闻联播 {cn}", "content": lines}}}}
    req = urllib.request.Request(
        webhook, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA["User-Agent"]},
    )
    with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
        resp = json.loads(r.read().decode("utf-8"))
        log(f"飞书响应: {resp}")


def send_feishu_error(err_msg):
    """抓取失败时也发飞书通知"""
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        return
    try:
        payload = {"msg_type": "text",
                   "content": {"text": f"⚠️ 新闻联播抓取失败\n{err_msg}"}}
        req = urllib.request.Request(
            webhook, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": UA["User-Agent"]},
        )
        urllib.request.urlopen(req, timeout=15, context=CTX)
    except Exception:
        pass


# ---------- 主流程 ----------

def main():
    # 选日期: 优先今天, 失败回退前 3 天
    target, full_link, items = None, None, []
    errors = []
    for delta in range(0, 4):
        d = datetime.now() - timedelta(days=delta)
        try:
            fl, its = fetch_xwlb_items(d)
            if its:
                target, full_link, items = d, fl, its
                break
            else:
                errors.append(f"{d:%Y-%m-%d}: 页面无节目单")
        except Exception as e:
            errors.append(f"{d:%Y-%m-%d}: {e}")
            log(f"{d:%Y-%m-%d} 失败: {e}")

    if not target:
        err = "近 4 天都没抓到节目单\n" + "\n".join(errors)
        log(f"[FATAL] {err}")
        send_feishu_error(err)
        sys.exit(1)

    date_cn = f"{target.year}年{target.month}月{target.day}日"
    rel_path = f"{target.year}年/{target.month}月/{target.day:02d}.html"
    log(f"归档日期: {target:%Y-%m-%d}, {len(items)} 条, 路径: {rel_path}")

    # 当日 HTML
    day_file = REPO_ROOT / rel_path
    day_file.parent.mkdir(parents=True, exist_ok=True)
    day_file.write_text(render_day_html(target, full_link, items), encoding="utf-8")

    # 读现有 README 汇总历史
    existing = {}
    readme_path = REPO_ROOT / "README.md"
    if readme_path.exists():
        old = readme_path.read_text(encoding="utf-8")
        for m in re.finditer(r"\|\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*\|\s*(\d+)\s*\|", old):
            y, mo, d, cnt = m.groups()
            cn = f"{y}年{int(mo)}月{int(d)}日"
            p = f"{y}年/{int(mo)}月/{int(d):02d}.html"
            existing[cn] = {"date_cn": cn, "path": p, "count": int(cnt)}

    existing[date_cn] = {"date_cn": date_cn, "path": rel_path, "count": len(items)}
    entries = sorted(
        existing.values(),
        key=lambda e: (int(re.match(r"(\d+)年(\d+)月(\d+)日", e["date_cn"]).group(1)),
                       int(re.match(r"(\d+)年(\d+)月(\d+)日", e["date_cn"]).group(2)),
                       int(re.match(r"(\d+)年(\d+)月(\d+)日", e["date_cn"]).group(3))),
        reverse=True,
    )

    (REPO_ROOT / "index.html").write_text(render_index_html(entries), encoding="utf-8")
    readme_path.write_text(render_readme(entries), encoding="utf-8")

    # 飞书
    try:
        send_feishu(target, items)
    except Exception as e:
        log(f"[WARN] 飞书失败: {e}")

    log("✅ 完成")


if __name__ == "__main__":
    main()
