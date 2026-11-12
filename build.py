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
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}


def beijing_now():
    """GitHub runner 跑在 UTC，统一换算成北京时间（无夏令时，固定 +8）。

    用北京时间定「今天」非常重要：否则 00:00-08:00（北京）之间的运行会把日期算成前一天。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)


def log(*a):
    print(f"[{beijing_now().strftime('%H:%M:%S')} 北京]", *a, flush=True)


def _hhmm(value, default):
    """解析 "HH:MM" 形式的配置，失败时用默认值。"""
    try:
        h, m = str(value).split(":")
        return int(h), int(m)
    except Exception:
        return default


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

    log(f"  抓到 {len(items)} 条（整期视频链接: {'有' if full_link else '无'}）")
    return full_link, items


# ---------- 完整性判定（核心：等联播真的播完再推） ----------
#
# 背景：央视网当日节目单是「边播边传」——整期视频和每条新闻的切片是陆续上线的。
# 2026-11-12 就是 19:30 抓到 6 条（联播还没播完）就推了，之后央视网又上了
# 「晋谒衣冠冢仪式」「联播快讯」2 条，导致推送漏稿。
#
# 判定「已经齐了」的三重条件（全部满足才推）：
#   1) 整期视频链接已出现 —— 《新闻联播》YYYYMMDD 19:00，央视网在**整期播完**
#      之后才发布这条，是最可靠的「节目已结束」信号；
#   2) 当前时间 >= 19:36 —— 兜住「整期链接提前挂出」的极端情况；
#   3) 条目连续 90 秒没有变化 —— 兜住「切片还在陆续上传」。
# 兜底：到 20:20（北京）无论如何都推当前已有条目，宁可晚/少，不能不发。

MIN_READY = os.environ.get("MIN_READY", "19:36")        # 早于此时间绝不判定齐全
HARD_DEADLINE = os.environ.get("HARD_DEADLINE", "20:20")  # 兜底截止时间（北京）
STABLE_SECONDS = int(os.environ.get("STABLE_SECONDS", "90"))   # 条目需稳定多久
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))       # 轮询间隔
MAX_WAIT_MINUTES = int(os.environ.get("MAX_WAIT_MINUTES", "75"))


def wait_for_complete(target):
    """轮询当日节目单，直到「整期视频已上线 + 条目稳定」。

    返回 (full_link, items, reason, complete)：
      complete=True  → 正常齐全，可以放心推送；
      complete=False → 到兜底时间按现有条目推送（或完全没有条目）。
    """
    mh, mm = _hhmm(MIN_READY, (19, 36))
    dh, dm = _hhmm(HARD_DEADLINE, (20, 20))
    now = beijing_now()
    min_ready = now.replace(hour=mh, minute=mm, second=0, microsecond=0)
    hard = now.replace(hour=dh, minute=dm, second=0, microsecond=0)
    give_up = min(hard, now + timedelta(minutes=MAX_WAIT_MINUTES))

    log(f"等待节目单齐全（整期视频上线 + 条目稳定 {STABLE_SECONDS}s），"
        f"兜底截止 {give_up:%H:%M}")

    last_sig, stable_since = None, None
    full_link, items = None, []
    while True:
        try:
            full_link, items = fetch_xwlb_items(target)
        except Exception as e:
            log(f"  抓取异常: {e}")
            items = []

        now = beijing_now()
        if items:
            sig = tuple(it["title"] for it in items)
            if sig != last_sig:
                last_sig, stable_since = sig, now
                log(f"  当前 {len(items)} 条（整期视频: {'有' if full_link else '无'}），重新计时")
            stable = (now - stable_since).total_seconds()

            if full_link and now >= min_ready and stable >= STABLE_SECONDS:
                return (full_link, items,
                        f"联播已播完：整期视频已上线，{len(items)} 条已稳定 {int(stable)}s",
                        True)

        if now >= give_up:
            if not items:
                return None, [], "兜底时间到，仍无可解析条目", False
            return (full_link, items,
                    f"兜底时间 {give_up:%H:%M} 到，按现有 {len(items)} 条推送"
                    f"（整期视频: {'有' if full_link else '无'}）",
                    False)

        time.sleep(POLL_SECONDS)


def read_archived_count(path):
    """读已归档 HTML 里的「共 N 条」。不存在返回 None，存在但解析不出返回 -1。"""
    try:
        if not path.exists():
            return None
        m = re.search(r"共\s*(\d+)\s*条", path.read_text(encoding="utf-8"))
        return int(m.group(1)) if m else -1
    except Exception:
        return None


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


def send_feishu(target, items, full_link, tag=""):
    """推送到飞书自定义机器人（interactive 卡片消息）。tag 用于标题后缀，如「（补全版）」。"""
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
                "title": {"tag": "plain_text", "content": f"📺 新闻联播 · {cn}{tag}"},
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
    # 只盯「今天」（北京时间），绝不回退到昨天：
    # 新闻联播 19:00 播出，央视网当日节目单是「边播边传」的，
    # 所以这里不是「有条目就推」，而是先等「联播播完」再推（见 wait_for_complete）。
    # 这样做的好处：1. 联播一结束（整期视频上线 + 条目稳定 90s）就推，最快；
    #              2. 即使联播超时到 19:50/20:00，也不会提前推半截（11-12 的漏稿事故）。
    force = os.environ.get("FORCE_RUN", "").strip().lower() == "true"
    target = beijing_now()

    date_cn = f"{target.year}年{target.month}月{target.day}日"
    rel_path = f"{target.year}年/{target.month}月/{target.day:02d}.html"
    day_file = REPO_ROOT / rel_path

    archived = None if force else read_archived_count(day_file)
    if archived is not None:
        log(f"{rel_path} 已归档（{archived} 条），本次只检查是否有新增条目")

    full_link, items, reason, complete = wait_for_complete(target)

    if not items:
        log(f"今日({target:%Y-%m-%d}) {reason} —— 不归档、不推送, 等下一轮定时")
        log("✅ 完成（无可用条目）")
        return

    log(f"归档日期: {target:%Y-%m-%d}, {len(items)} 条, 路径: {rel_path}")
    log(f"完整性: {'齐全' if complete else '兜底'} —— {reason}")

    # 幂等保护 + 自愈：
    #   - 已归档且条目数 >= 现在抓到的 → 说明今天已经推全了，跳过（不重复发飞书）；
    #   - 已归档但条目数变多 → 说明上次推的是「半截」（央视网后来又补了稿），
    #     这次更新归档并补推一条带「（补全版）」标记的卡片，避免漏稿。
    #   - 手动 workflow_dispatch 勾选 force → 无条件重跑重推。
    if archived is not None and (archived < 0 or archived >= len(items)):
        log(f"{rel_path} 已存在 {archived} 条, 无新增条目, 跳过归档与推送")
        log("✅ 完成（幂等跳过）")
        return

    tag = ""
    if archived is not None:
        log(f"检测到补全: 归档 {archived} 条 → 现在 {len(items)} 条, 更新归档并补推")
        tag = "（补全版）"

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
        send_feishu(target, items, full_link, tag=tag)
    except Exception as e:
        log(f"【飞书推送】失败: {e}")

    log("✅ 完成")


if __name__ == "__main__":
    main()
