# 准时触发层：外部定时器

> 这一层是「稳定修复」的关键。仅靠 GitHub Actions 的 `schedule`，无法保证 19:50 送达。

## 为什么需要它

GitHub 官方说明：scheduled workflow 属于**尽力而为**，高峰期会延迟数小时，也可能被静默丢弃；
把多个 cron 堆在同一个窗口里没有用，因为 GitHub 会把整批一起排队。本仓库实测：

| 配置 cron (UTC) | 实际启动 (UTC) | 北京时间 | 延迟 |
|---|---|---|---|
| 11:50 / 11:55 | 18:53 / 18:54（次日） | 02:53 / 02:54 | ≈ 7.1 h |
| 12:00 / 12:05 | 19:27 / 19:27（次日） | 03:27 | ≈ 7.6 h |

11 月 3、4 日连续两天，4 个「互为兜底」的 cron 全部一起延后约 7 小时。

`repository_dispatch` 不经过这套排队，通常秒级入队。所以：

- **准时**由外部定时器触发 dispatch 保证；
- **不漏天**由仓库内 5 个错峰 cron 兜底；
- **不推错 / 不重复**由 `build.py` 的北京时间日期推导 + `state/delivered.json` 幂等保证。

## 方案 A：Cloudflare Worker（推荐，免费、常在线）

1. 建一个 GitHub Token（只给 `ichenc/news_daily` 一个仓库）：
   - classic PAT：勾 `repo`（公开仓库 `public_repo` 也可以）
   - fine-grained PAT：`Contents: read & write`
   - 官方要求：`POST /repos/{owner}/{repo}/dispatches` 需要 classic `repo`，或 fine-grained 的 `Contents: write`
2. 部署（约 2 分钟）：

   ```bash
   cd scheduler
   npm i -g wrangler            # 或 npx wrangler
   wrangler login
   wrangler secret put GH_TOKEN # 粘贴第 1 步的 token
   wrangler deploy
   ```

   `wrangler.toml` 里的 `crons = ["47 11 * * *"]` 即 11:47 UTC / 19:47 北京时间。
3. 验证：浏览器打开 Worker 域名会立刻触发一次；或看 Actions 里出现
   `repository_dispatch` 的运行，Summary 里「较 19:50 延迟」应为个位数分钟。

> Worker 的 `fetch` 处理器只是手动验证/补跑入口；想补某一天可加 `?date=2026-11-04`，
> 并在 Actions 手动运行时把 `mode` 设为 `rebuild` 或 `repush`。

## 方案 B：任意 VPS / NAS 的 crontab

```bash
# 每天 19:47 北京时间（系统时区为 UTC+8 时）
47 19 * * * curl -sS -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $GH_TOKEN" \
  https://api.github.com/repos/ichenc/news_daily/dispatches \
  -d '{"event_type":"daily-news"}' >> /var/log/news-daily.log 2>&1
```

## 方案 C：第三方 cron 服务（cron-job.org / EasyCron 等）

新建一个 HTTPS 任务，URL 填 `https://api.github.com/repos/ichenc/news_daily/dispatches`，
方法 POST，请求头加 `Authorization: Bearer <token>`、`Accept: application/vnd.github+json`、
`Content-Type: application/json`，请求体 `{"event_type":"daily-news"}`，时间设为 19:47（UTC+8）。

## 上线检查清单

- [ ] 用 `dry_run=true` 手动跑一次 workflow：Summary 里应出现归档日期、条数、延迟分钟
- [ ] 配置外部定时器并手动触发一次，确认 Actions 里出现 `repository_dispatch` 运行
- [ ] 次日 19:50 前后确认飞书卡片到达，卡片底部会显示「推送时间 / 较 19:50 晚 X 分钟」
- [ ] 检查仓库根目录生成了 `state/delivered.json`，其中当天 `done: true`、且配了 webhook 时 `notified: true`
- [ ] 兜底 cron 后续触发时应输出「已送达（幂等跳过）」，不会重复推送

## 注意

- 外部定时器**不是单点故障**：它挂了还有 5 个兜底 cron，只是可能晚几小时。
- `FEISHU_WEBHOOK` / `FEISHU_SECRET` 仍配置在 GitHub 仓库 Secret，不要放进 Worker 或 crontab。
- `GH_TOKEN` 只用于触发 dispatch，权限最小化，不要给 `admin` / `workflow` 等额外权限。
