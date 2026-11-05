// Cloudflare Worker：每天 11:47 UTC（19:47 北京时间）准时触发 news_daily。
//
// 为什么不直接用 GitHub Actions 的 schedule：
//   GitHub 的 cron 是尽力而为排队，高峰期会整批延迟数小时（本仓库实测 ~7 小时），
//   甚至静默丢弃。repository_dispatch 不经过那套排队，通常秒级入队。
//
// 部署：见同目录 README.md。GH_TOKEN 用 `wrangler secret put GH_TOKEN` 写入，不要写进代码。

const OWNER = 'ichenc';
const REPO = 'news_daily';
const EVENT_TYPE = 'daily-news';

async function dispatch(env, payload) {
  const body = {
    event_type: EVENT_TYPE,
    client_payload: payload || { source: 'cloudflare-cron', date: '' },
  };
  const res = await fetch(`https://api.github.com/repos/${OWNER}/${REPO}/dispatches`, {
    method: 'POST',
    headers: {
      'Accept': 'application/vnd.github+json',
      'Authorization': `Bearer ${env.GH_TOKEN}`,
      'X-GitHub-Api-Version': '2022-11-28',
      'Content-Type': 'application/json',
      'User-Agent': 'news-daily-scheduler',
    },
    body: JSON.stringify(body),
  });
  // 204 = 触发成功；非 2xx 抛错让 Worker 记录失败（Cloudflare 控制台可见）
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`GitHub dispatch 失败: HTTP ${res.status} ${text.slice(0, 300)}`);
  }
  return res.status;
}

export default {
  async scheduled(event, env, ctx) {
    const scheduledTime = new Date(event.scheduledTime).toISOString();
    try {
      const status = await dispatch(env, { source: 'cloudflare-cron', date: '' });
      console.log(`[news_daily] ${scheduledTime} 触发成功 HTTP ${status}`);
    } catch (e) {
      console.error(`[news_daily] ${scheduledTime} 首次触发失败: ${e.message}`);
      // 一次瞬时失败重试；仍失败也没关系，仓库里的 5 个兜底 cron 会补上
      await new Promise((r) => setTimeout(r, 5000));
      try {
        const status = await dispatch(env, { source: 'cloudflare-cron-retry', date: '' });
        console.log(`[news_daily] ${scheduledTime} 重试成功 HTTP ${status}`);
      } catch (e2) {
        console.error(`[news_daily] ${scheduledTime} 重试仍失败: ${e2.message}`);
      }
    }
  },

  // 方便手动验证：访问 Worker 域名即可立刻触发一次（可加 ?date=YYYY-MM-DD 补某天）
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const date = url.searchParams.get('date') || '';
    try {
      const status = await dispatch(env, { source: 'manual', date });
      return new Response(`dispatched HTTP ${status}\n`, { status: 200 });
    } catch (e) {
      return new Response(`error: ${e.message}\n`, { status: 502 });
    }
  },
};
