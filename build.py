name: 每日新闻联播文字版

on:
  schedule:
    # UTC 11:27 = 北京时间 19:27，错开整点高峰，提前给排队留缓冲
    # 即使排队延迟 1~2 小时，也能在 20:30~21:30 左右跑完
    - cron: '27 11 * * *'
  workflow_dispatch: {}

permissions:
  contents: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: 抓取并生成 HTML
        env:
          FEISHU_WEBHOOK: ${{ secrets.FEISHU_WEBHOOK }}
        run: python build.py

      - name: 提交并推送
        run: |
          git config user.name "news-daily-bot"
          git config user.email "news-daily-bot@users.noreply.github.com"
          git add -A
          if git diff --staged --quiet; then
            echo "无更新"
          else
            git commit -m "auto: 新闻联播 $(date -u '+%Y-%m-%d')"
            git push
          fi
