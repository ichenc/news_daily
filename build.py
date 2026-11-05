# -*- coding: utf-8 -*-
"""每日新闻联播文字版（GitHub Actions 版）。

这次重写只解决一件事：让「当天该推的那期」在正确的时间、以正确的日期送达。

关键设计
--------
1. 全流程使用北京时间（固定 UTC+8），不再依赖 runner 的本地时区（runner 是 UTC）。
2. 目标日期由「运行那一刻的北京时间」推导：19:40 之后归档当天，19:40 之前归档昨天。
   因此 GitHub cron 延迟 7 小时、甚至隔夜补跑，归档的仍然是正确那一期。
3. 当日页面可能晚于 19:40 才上线，改为在窗口内轮询等待；等不到就标记 pending，
   交给后面的兜底 cron 重跑，绝不回退到前几天去推旧新闻。
4. state/delivered.json 记录每个日期是否已推送，跨多次触发幂等，避免重复推飞书。
5. 把运行结论写入 GITHUB_OUTPUT，workflow 据此决定是否提交，并生成「延迟 X 分钟」报告。

退出码 / 输出
-------------
* exit 0：正常（status=ok / rebuilt / dry / skip / pending）
* exit 1：未捕获异常（status=error）
* GITHUB_OUTPUT：status, target_date, count, bj_time, delay_minutes, reason
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
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# 北京时间固定 UTC+8，不受 runner 时区和夏令时影响
BJ_TZ = timezone(timedelta(hours=8))
DUE = dtime(19, 50)          # 期望送达时刻（北京时间）
PAGE_READY = (19, 40)        # 央视网当日节目单预计上线时刻

STATE_PATH = REPO_ROOT / "state" / "delivered.json"
INDEX_PATH = REPO_ROOT / "index.html"
README_PATH = REPO_ROOT / "README.md"

# 默认走证书校验；只有目标站证书链有问题时才降级，避免全程关闭校验
CTX = ssl.create_default_context()
CTX_UNSAFE = ssl.create_default_context()
CTX_UNSAFE.check_hostname = False
CTX_UNSAFE.verify_mode = ssl.CERT_NONE

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def log(*a):
    print(f"[{now_bj().strftime('%H:%M:%S')}]", *a, flush=True)


def now_bj():
    return datetime.now(BJ_TZ)


def emit(**kv):
    """写 GitHub Actions 的 step output（本地运行时静默跳过）。"""
    path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not path:
        return
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for k, v in kv.items():
            f.write(f"{k}={str(v).replace(chr(10), ' ')}\n")


def open_url(req, timeout):
    """GET/POST 统一入口：证书链异常时才回退到不校验证书。"""
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=CTX)
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            log("  [WARN] 目标站证书链校验失败，本次请求降级为不校验")
            return urllib.request.urlopen(req, timeout=timeout, context=CTX_UNSAFE)
        raise


# ---------- 目标日期 ----------

def resolve_target_date(now=None, override=None):
    """决定本次运行该归档哪一天。支持 TARGET_DATE / dispatch 入参显式覆盖。"""
    raw = (override if override is not None else os.environ.get("TARGET_DATE", "")).strip()
    if raw:
        return datetime.strptime(raw, "%Y-%m-%d").date(), True

    now = now or now_bj()
    # 19:40 之前：当天《新闻联播》还没播完/页面还没上线，应归档「昨天」。
    # 19:40 之后（含深夜延迟、隔夜补跑）：归档最近一期，也就是运行日所在的那一天，
    # 但如果已经跨过午夜，则归档昨天。下面统一用「时刻」判断，避免 UTC/北京混用。
    if (now.hour, now.minute) < PAGE_READY:
        return (now - timedelta(days=1)).date(), False
    return now.date(), False


def due_moment(target):
    return datetime.combine(target, DUE, tzinfo=BJ_TZ)


# ---------- 抓新闻联播节目单 ----------

def http_get(url, timeout=25, retries=3):
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with open_url(req, timeout) as r:
                return r.read()
        except Exception as e:
            last_err = e
            log(f"  请求第 {i+1} 次失败: {e}")
            if i < retries - 1:
                time.sleep(2.0 * (i + 1))
    raise last_err


def fetch_xwlb_items(target: date):
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

    # 备用正则：主正则没匹配到时用更宽松的
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


def wait_for_items(target, wait_minutes):
    """当日页可能晚于 19:40 上线：窗口内轮询，等不到就返回 pending，不推旧新闻。

    返回 (full_link, items, err)。err 为 None 表示「页面还没有节目单」，
    非 None 表示网络/解析出现了实质故障。
    """
    deadline = time.monotonic() + max(0.0, wait_minutes) * 60
    attempt = 0
    last_err = None
    while True:
        attempt += 1
        try:
            fl, its = fetch_xwlb_items(target)
            if its:
                return fl, its, None
            last_err = None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            log(f"  抓取异常: {last_err}")

        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        nap = min(90.0, remain)
        log(f"  当日节目单尚未就绪，{nap:.0f}s 后重试（第 {attempt + 1} 次）")
        time.sleep(nap)
    return None, [], last_err


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
<div class="meta">来源: 央视网 · 每日 19:50(北京时间) 自动更新 · 共 {len(entries)} 期</div>
<table><thead><tr><th>日期</th><th style="width:100px;text-align:center">条数</th></tr></thead>
<tbody>{chr(10).join(rows)}</tbody></table>
</body></html>"""


# README 由脚本每次重写，说明文字必须放在这里，否则下一次运行会被覆盖掉。
README_EXTRA = """
## 定时与延迟

GitHub Actions 的 `schedule` 是**尽力而为**：高负载时整批延迟数小时，甚至静默丢弃。
实测本仓库 11:50/11:55/12:00/12:05 UTC 的 4 个 cron 曾连续两天整体延后约 7 小时
（凌晨 2:40–3:30 才送达），所以「同一窗口内多放几个 cron」并不能互为兜底。

现在的分层：

| 层 | 触发方式 | 作用 |
|---|---|---|
| 准时层 | 外部定时器在 11:47 UTC（19:47 北京）调用 `repository_dispatch` / `workflow_dispatch` | 分钟级准时，不受 GitHub cron 排队影响 |
| 兜底层 | 5 个错峰、跨小时的 `schedule` cron | 外部定时器失效时保证「不漏一天」，不保证准时 |
| 正确性 | 北京时间推导目标日期 + `state/delivered.json` 幂等 | 延迟、隔夜补跑、多次触发都不会推错日期或重复推送 |

外部定时器配置见 [`scheduler/README.md`](./scheduler/README.md)。**没有配置外部定时器时，
GitHub cron 仍可能延迟数小时**——这是平台限制，仓库内无法彻底消除，只能靠兜底保证最终送达。

## 手动运行

Actions 页面选择 `每日新闻联播文字版` → Run workflow：

- `mode=normal`（默认）：正常流程，已推送过的日期自动跳过
- `mode=rebuild`：仅重新生成当日归档，不推飞书
- `mode=repush`：补发飞书（用于上次推送失败后的恢复）
- `date`：留空自动推导，或填 `YYYY-MM-DD` 补某一天（补历史日期需配合 `rebuild`/`repush`，否则会被幂等跳过）
- `dry_run=true`：只抓取和渲染，不推飞书、不提交，用于验证

## 说明

- 每天抓取央视网当日《新闻联播》节目单，生成文字版归档
- 开启 GitHub Pages 后，根目录 `index.html` 即为归档主页
- 飞书群推送通过仓库 Secret `FEISHU_WEBHOOK` 配置；机器人开了签名校验还需 `FEISHU_SECRET`
- 每次运行的 Actions Summary 会显示北京时间、归档日期和「较 19:50 延迟 X 分钟」
"""


def render_readme(entries):
    lines = ["# 每日新闻联播文字版归档", "",
             f"> 来源: 央视网 · 每日 19:50(北京时间) 自动更新 · 共 {len(entries)} 期",
             "", "| 日期 | 条数 | 文字版 |", "|---|---:|---|"]
    for e in entries:
        lines.append(
            f"| {e['date_cn']} | {e['count']} | [查看](./{urllib.parse.quote(e['path'], safe='/')}) |"
        )
    lines.append(README_EXTRA.rstrip())
    lines.append("")
    return "\n".join(lines)


def collect_entries():
    """从现有 README 表格里恢复历史归档，保持幂等。"""
    existing = {}
    if README_PATH.exists():
        old = README_PATH.read_text(encoding="utf-8")
        for m in re.finditer(r"\|\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*\|\s*(\d+)\s*\|", old):
            y, mo, d, cnt = m.groups()
            cn = f"{y}年{int(mo)}月{int(d)}日"
            p = f"{y}年/{int(mo)}月/{int(d):02d}.html"
            existing[cn] = {"date_cn": cn, "path": p, "count": int(cnt)}
    return existing


def sort_key(e):
    m = re.match(r"(\d+)年(\d+)月(\d+)日", e["date_cn"])
    return tuple(int(x) for x in m.groups())


def write_archive(target, full_link, items):
    cn = f"{target.year}年{target.month}月{target.day}日"
    rel_path = f"{target.year}年/{target.month}月/{target.day:02d}.html"
    day_file = REPO_ROOT / rel_path
    day_file.parent.mkdir(parents=True, exist_ok=True)
    day_file.write_text(render_day_html(target, full_link, items), encoding="utf-8", newline="\n")

    entries = collect_entries()
    entries[cn] = {"date_cn": cn, "path": rel_path, "count": len(items)}
    ordered = sorted(entries.values(), key=sort_key, reverse=True)
    INDEX_PATH.write_text(render_index_html(ordered), encoding="utf-8", newline="\n")
    README_PATH.write_text(render_readme(ordered), encoding="utf-8", newline="\n")
    return rel_path


# ---------- 送达状态（幂等） ----------

def load_state():
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("days", {})
            return data
    except Exception:
        pass
    return {"days": {}, "error_notified": ""}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )


# ---------- 飞书 ----------

def gen_feishu_sign(secret, timestamp):
    """飞书自定义机器人「签名校验」算法: key = timestamp + "\\n" + secret, 空消息体。"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def webhook_url():
    return os.environ.get("FEISHU_WEBHOOK", "").strip()


def feishu_secret():
    return os.environ.get("FEISHU_SECRET", "").strip() or None


def with_sign(payload):
    secret = feishu_secret()
    if secret:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = gen_feishu_sign(secret, ts)
    return payload


def post_feishu(payload, max_retry=3):
    """发送并校验飞书业务返回码，返回 (ok, reason)。"""
    webhook = webhook_url()
    if not webhook:
        return False, "未设置 FEISHU_WEBHOOK"
    data = json.dumps(payload).encode("utf-8")
    attempt = 0
    while True:
        attempt += 1
        try:
            req = urllib.request.Request(
                webhook, data=data, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": UA["User-Agent"]},
            )
            with open_url(req, 15) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            code = body.get("code", body.get("StatusCode", 0))
            if code == 0:
                return True, ""
            return False, (
                "飞书返回码 %s：%s（19001 多为 webhook 地址不对；若机器人开了签名校验，"
                "请在仓库 Secret 配置 FEISHU_SECRET）" % (code, body.get("msg", ""))
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


def send_feishu(target, items, full_link, now, delay_minutes):
    """推送互动卡片，并在卡片里标注实际送达时间与延迟，便于核对。"""
    if not webhook_url():
        log("[WARN] 未设置 FEISHU_WEBHOOK, 跳过飞书推送")
        return True, "未配置 webhook（视为跳过）"

    cn = f"{target.year}年{target.month}月{target.day}日"
    md = [f"**共 {len(items)} 条**"]
    for i, it in enumerate(items, 1):
        md.append(f"{i}. [{it['title']}]({it['url']})")

    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(md)}}]
    tail = []
    if full_link:
        tail.append(f"完整节目视频: [央视网点播页]({full_link})")
    delay_desc = f"准时（±5 分钟）" if -5 <= delay_minutes <= 5 else (
        f"较 19:50 晚 {delay_minutes} 分钟" if delay_minutes > 0 else f"较 19:50 早 {-delay_minutes} 分钟"
    )
    tail.append(f"推送时间: {now.strftime('%Y-%m-%d %H:%M')} 北京时间 · {delay_desc}")
    elements.append({"tag": "hr"})
    # note 元素内只允许 plain_text / lark_md / img，不能直接用 {"tag": "a"}
    elements.append({
        "tag": "note",
        "elements": [{"tag": "lark_md", "content": "  \n".join(tail)}],
    })

    payload = with_sign({
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "red",
                "title": {"tag": "plain_text", "content": f"📺 新闻联播 · {cn}"},
            },
            "elements": elements,
        },
    })
    return post_feishu(payload)


def send_feishu_error(state, target, err_msg):
    """硬故障时发一条告警，同一天只发一次，避免兜底 cron 反复骚扰。"""
    if not webhook_url():
        return
    iso = target.isoformat()
    if state.get("error_notified") == iso:
        return
    payload = with_sign({
        "msg_type": "text",
        "content": {"text": f"⚠️ 新闻联播抓取失败（{iso}）\n{err_msg[:500]}"},
    })
    ok, _ = post_feishu(payload, max_retry=2)
    if ok:
        state["error_notified"] = iso
        save_state(state)


# ---------- 主流程 ----------

def main():
    now = now_bj()
    target, overridden = resolve_target_date(now)
    iso = target.isoformat()
    delay_minutes = int((now - due_moment(target)).total_seconds() // 60)

    mode = os.environ.get("RUN_MODE", "normal").strip().lower()
    dry = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    try:
        wait_minutes = float(os.environ.get("WAIT_MINUTES", "6") or 6)
    except ValueError:
        wait_minutes = 6.0

    log(f"运行时刻(北京): {now:%Y-%m-%d %H:%M:%S}, 目标日期: {iso}, "
        f"模式: {mode}, 较 19:50 延迟: {delay_minutes} 分钟"
        + (" (日期由入参指定)" if overridden else ""))

    day_file = REPO_ROOT / f"{target.year}年/{target.month}月/{target.day:02d}.html"
    state = load_state()
    rec = state.get("days", {}).get(iso, {})
    # 兼容升级前已有的归档：没有 state 记录但归档文件存在，视为已送达，避免补推旧新闻。
    # done = 这一期已处理完；notified = 飞书确实送达。两者分开，
    # 这样「未配置 webhook」也能正常归档且不会每天重跑。
    legacy_done = day_file.exists() and not rec
    already = bool(rec.get("done") or rec.get("notified")) or legacy_done

    base_out = dict(
        target_date=iso,
        bj_time=now.strftime("%Y-%m-%d %H:%M:%S"),
        delay_minutes=delay_minutes,
        count=0,
    )

    if already and mode not in ("rebuild", "repush"):
        log(f"{iso} 已送达（状态文件或历史归档存在），跳过重复归档与推送")
        log("✅ 完成（幂等跳过）")
        emit(status="skip", reason="已送达", **base_out)
        return

    full_link, items, err = wait_for_items(target, wait_minutes)
    if not items:
        reason = "当日节目单未就绪" if err is None else f"抓取失败: {err}"
        log(f"[PENDING] {reason}，本次不归档、不推送，等待后续兜底运行")
        if err is not None:
            send_feishu_error(state, target, err)
        emit(status="pending", reason=reason, **base_out)
        return

    log(f"归档日期: {iso}, {len(items)} 条, 路径: {day_file.relative_to(REPO_ROOT)}")
    rel_path = write_archive(target, full_link, items)
    base_out["count"] = len(items)

    if dry:
        log("[DRY_RUN] 已渲染文件，但不推飞书、不写状态、不提交")
        emit(status="dry", reason="dry_run", **base_out)
        return

    if mode == "rebuild":
        log("mode=rebuild: 归档已重建，跳过飞书推送")
        emit(status="rebuilt", reason="rebuild", **base_out)
        return

    ok, reason = send_feishu(target, items, full_link, now, delay_minutes)
    if not ok:
        log(f"【飞书推送】失败: {reason}")
        # 不写 done/notified，下一次运行会自动补发；归档文件已生成，不会重复抓取
        emit(status="pending", reason=f"飞书推送失败: {reason}", **base_out)
        return
    if reason:
        log(f"【飞书推送】跳过: {reason}")
    else:
        log("【飞书推送】成功")

    state.setdefault("days", {})[iso] = {
        "archived": True,
        "done": True,
        "notified": not bool(reason),
        "note": reason or "",
        "count": len(items),
        "at": now.isoformat(),
        "delay_minutes": delay_minutes,
    }
    save_state(state)
    log(f"✅ 完成（{rel_path}）")
    emit(status="ok", reason="", **base_out)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        traceback.print_exc()
        log(f"[FATAL] {exc}")
        emit(status="error", reason=str(exc)[:200])
        sys.exit(1)
