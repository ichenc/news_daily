# -*- coding: utf-8 -*-
"""
每日新闻联播文字版脚本（GitHub Actions 版）

"""

import base64
import hashlib
import hmac
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

def gen_feishu_sign(secret, timestamp):
    """飞书自定义机器人「签名校验」算法: key = timestamp + "\\n" + secret, 空消息体."""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def post_feishu(webhook, payload, max_retry=3):
    """发送 payload 并校验飞书业务返回码。返回 (ok, reason)。

    重试策略（对齐 FXmonkey/workbuddy_auto_checkin 的做法）:
      - 网络异常 / HTTP 5xx → 退避重试，瞬时故障自愈
      - 飞书业务拒绝（code != 0，如 19001 地址无效、签名校验失败）→ 不重试，
        原因带改法返回（重试无用）
    """
    data = json.dumps(payload).encode("utf-8")
    attempt = 0
    while True:
        attempt += 1
        try:
            req = urllib.request.Request(
                webhook, data=data, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": UA["User-Agent"]},
            )
            with urllib.request.urlopen(req, timeout=15, context=CTX) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
                code = body.get("code", body.get("StatusCode", 0))
                if code == 0:
                    return True, ""
                return False, (
                    "飞书返回码 %s：%s（19001 多为 webhook 地址不对；"
                    "若机器人开了签名校验，请在仓库 Secret 配置 FEISHU_SECRET，"
                    "或重建机器人时不勾选签名校验）" % (code, body.get("msg", ""))
                )
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < max_retry:
                log(f"  [飞书] HTTP {e.code}, 退避后重试 ({attempt}/{max_retry})")
                time.sleep(2.0 * attempt)
                continue
            return False, f"HTTP {e.code}"
        except Exception as e:
            if attempt < max_retry:
                log(f"  [飞书] 请求异常, 退避后重试 ({attempt}/{max_retry}): {e}")
                time.sleep(2.0 * attempt)
                continue
            return False, f"{type(e).__name__}: {e}"


def _feishu_secret():
    secret = os.environ.get("FEISHU_SECRET", "").strip()
    return secret or None


def send_feishu(target, items, full_link):
    """推送到飞书自定义机器人（interactive 卡片消息）。"""
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        log("[WARN] 未设置 FEISHU_WEBHOOK, 跳过飞书推送")
        return

    cn = f"{target.year}年{target.month}月{target.day}日"
    md = [f"**共 {len(items)} 条**"]
    for i, it in enumerate(items, 1):
        md.append(f"{i}. [{it['title']}]({it['url']})")

    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(md)}}]
    if full_link:
        elements.append({"tag": "hr"})
        # 注意: note 元素内只允许 plain_text / lark_md / img 标签,
        # 不能直接用 {"tag": "a"}, 否则飞书报 11246 parse card json err
        elements.append({
            "tag": "note",
            "elements": [
                {"tag": "lark_md", "content": f"完整节目视频: [央视网点播页]({full_link})"},
            ],
        })

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "red",
                "title": {"tag": "plain_text", "content": f"📺 新闻联播 · {cn}"},
            },
            "elements": elements,
        },
    }
    secret = _feishu_secret()
    if secret:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = gen_feishu_sign(secret, ts)

    ok, reason = post_feishu(webhook, payload)
    if ok:
        log("【飞书推送】成功")
    else:
        log(f"【飞书推送】失败: {reason}")


def send_feishu_error(err_msg):
    """抓取失败时也发飞书通知"""
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        return
    payload = {"msg_type": "text",
               "content": {"text": f"⚠️ 新闻联播抓取失败\n{err_msg}"}}
    secret = _feishu_secret()
    if secret:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = gen_feishu_sign(secret, ts)
    ok, reason = post_feishu(webhook, payload, max_retry=2)
    if not ok:
        log(f"[WARN] 错误通知推送失败: {reason}")


# ---------- 主流程 ----------

def main():
    # 只盯「今天」：新闻联播当天 19:00 播出、约 19:40 后央视网当日页面才上线。
    # 配合高频定时（Cloudflare 每 2 分钟轮询 19:35-21:30 北京）即可做到
    # 「联播一结束、页面一上线，就在 2 分钟内推送」，且绝不会误推往期内容。
    # 当天页面还没好（items 为空）→ 直接结束，等下一轮定时再来；
    # 不归档、不推送、不回退到昨天，避免把旧日期当成今天发出去。
    target = datetime.now()
    try:
        full_link, items = fetch_xwlb_items(target)
    except Exception as e:
        err = f"抓取今日({target:%Y-%m-%d})节目单异常: {e}"
        log(f"[FATAL] {err}")
        send_feishu_error(err)
        sys.exit(1)

    if not items:
        log(f"今日({target:%Y-%m-%d})节目单尚未发布/暂无可解析条目, 暂不推送, 等待下次定时重试")
        log("✅ 完成（等待节目单上线）")
        return

    date_cn = f"{target.year}年{target.month}月{target.day}日"
    rel_path = f"{target.year}年/{target.month}月/{target.day:02d}.html"
    log(f"归档日期: {target:%Y-%m-%d}, {len(items)} 条, 路径: {rel_path}")

    # 当日 HTML（幂等保护: 已归档则跳过, 避免备份时段重复生成/重复推送;
    # 手动 workflow_dispatch 触发时 FORCE_RUN=true 可强制重跑）
    day_file = REPO_ROOT / rel_path
    force = os.environ.get("FORCE_RUN", "").strip().lower() == "true"
    if day_file.exists() and not force:
        log(f"{rel_path} 已存在, 跳过重复归档与推送")
        log("✅ 完成（幂等跳过）")
        return
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
        send_feishu(target, items, full_link)
    except Exception as e:
        log(f"【飞书推送】失败: {e}")

    log("✅ 完成")


if __name__ == "__main__":
    main()
