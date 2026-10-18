"""No-paid-API daily RSS briefing. Only public RSS metadata is used."""
import calendar
import datetime as dt
import hashlib
import html
import os
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import feedparser
import requests

KST = ZoneInfo('Asia/Seoul')
NOW = dt.datetime.now(KST)
WEBHOOK = os.getenv('DISCORD_WEBHOOK_URL', '').strip()
if not WEBHOOK.startswith('https://discord.com/api/webhooks/') and not WEBHOOK.startswith('https://discordapp.com/api/webhooks/'):
    raise SystemExit('Missing/invalid DISCORD_WEBHOOK_URL secret')

# Separate searches prevent a single topic from filling the whole digest.
SEARCHES = [
    ('AI·IT', '인공지능 AI 신기술 발표'),
    ('AI·IT', 'OpenAI 구글 엔비디아 생성형 AI'),
    ('J-POP·내한', 'JPOP 일본 가수 내한 콘서트'),
    ('J-POP·내한', '일본 밴드 가수 신곡 앨범'),
    ('한국 인디', '윤마치 신곡 앨범 콘서트'),
    ('한국 인디', '한로로 신곡 앨범 공연'),
    ('한국 인디', '정우 싱어송라이터 신곡 콘서트'),
    ('K-POP', '아이브 IVE 컴백 신곡 콘서트'),
    ('K-POP', '리센느 RESCENE 컴백 신곡'),
]
CATEGORIES = ('AI·IT', 'J-POP·내한', '한국 인디', 'K-POP')
TARGETS = {'AI·IT': 3, 'J-POP·내한': 3, '한국 인디': 2, 'K-POP': 2}
USER_AGENT = 'DailyNewsRSSBot/1.0 (+https://github.com/)'


def clean(text):
    text = re.sub(r'<[^>]+>', ' ', text or '')
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def timestamp(entry):
    for key in ('published_parsed', 'updated_parsed'):
        val = entry.get(key)
        if val:
            return dt.datetime.fromtimestamp(calendar.timegm(val), tz=dt.timezone.utc).astimezone(KST)
    for key in ('published', 'updated'):
        try:
            return parsedate_to_datetime(entry[key]).astimezone(KST)
        except (KeyError, TypeError, ValueError):
            pass
    return None


def fetch(category, query):
    url = 'https://news.google.com/rss/search?q=' + quote(query + ' when:7d') + '&hl=ko&gl=KR&ceid=KR:ko'
    response = requests.get(url, timeout=18, headers={'User-Agent': USER_AGENT})
    response.raise_for_status()
    result = feedparser.parse(response.content)
    out = []
    for entry in result.entries[:35]:
        title = clean(entry.get('title', ''))
        link = entry.get('link', '')
        published = timestamp(entry)
        if not title or not link or not published:
            continue
        age = (NOW - published).total_seconds()
        if age < -3600 or age > 7 * 86400:
            continue
        # News RSS description may contain links/HTML; keep only plain text.
        summary = clean(entry.get('summary', ''))
        if summary == title or summary.startswith(title):
            summary = ''
        summary = summary[:230].strip(' -|')
        normalized = re.sub(r'[^\w가-힣]', '', title.split(' - ')[0].lower())
        if not normalized:
            continue
        out.append(dict(category=category, title=title[:220], link=link,
                        published=published, summary=summary, identity=normalized))
    return out


def collect():
    pool = {category: [] for category in CATEGORIES}
    for category, query in SEARCHES:
        try:
            found = fetch(category, query)
            pool[category].extend(found)
            print(f'{category}: {query}: {len(found)} results')
        except Exception as exc:
            print(f'Feed error for {query}: {exc}')
    for category in CATEGORIES:
        pool[category].sort(key=lambda e: e['published'], reverse=True)
    selected = []
    seen = set()

    def add(article):
        if article['identity'] in seen:
            return False
        # Avoid near-identical headlines from different publishers.
        for old in selected:
            a, b = old['identity'], article['identity']
            if a[:30] == b[:30] or (a in b and len(a) >= 16) or (b in a and len(b) >= 16):
                return False
        selected.append(article)
        seen.add(article['identity'])
        return True

    # Prefer stories from 24 hours, but make room for topic diversity.
    for category in CATEGORIES:
        ordered = sorted(pool[category], key=lambda e: (e['published'] >= NOW - dt.timedelta(hours=24), e['published']), reverse=True)
        n = 0
        for article in ordered:
            if add(article):
                n += 1
            if n == TARGETS[category]:
                break
    # Fill remaining slots from all feeds, prioritizing fresh news.
    rest = sorted((article for values in pool.values() for article in values),
                  key=lambda e: (e['published'] >= NOW - dt.timedelta(hours=24), e['published']), reverse=True)
    for article in rest:
        if len(selected) >= 10:
            break
        add(article)
    return selected[:10]


def send(content):
    # No embeds or mass pings, to keep webhook requests simple and safe.
    response = requests.post(WEBHOOK, json={'content': content, 'allowed_mentions': {'parse': []}}, timeout=25)
    if response.status_code == 429:
        delay = min(float(response.json().get('retry_after', 2)), 30)
        time.sleep(delay)
        response = requests.post(WEBHOOK, json={'content': content, 'allowed_mentions': {'parse': []}}, timeout=25)
    response.raise_for_status()


def main():
    items = collect()
    date = NOW.strftime('%Y년 %m월 %d일')
    if not items:
        send(f'📰 **{date} 뉴스 브리핑**\n오늘은 RSS에서 최근 7일 내 검증 가능한 기사를 확보하지 못했습니다. 잠시 후 피드 상태를 확인해 주세요.')
        return
    send(f'📰 **{date} AI·음악 뉴스 브리핑**\n무료 뉴스 RSS 기반 · {len(items)}개 기사 · 최신 24시간 우선 (부족 시 최근 7일)\n※ 기사의 원문 제목과 공개 메타데이터 기반이며, 사실 여부는 링크에서 확인해 주세요.')
    for idx, item in enumerate(items, 1):
        date_str = item['published'].strftime('%m/%d %H:%M KST')
        snippet = ('\n' + item['summary']) if item['summary'] else ''
        message = f"**{idx:02d}. [{item['category']}] {item['title']}**\n🕒 {date_str}{snippet}\n🔗 {item['link']}"
        send(message[:1950])
        time.sleep(0.45)
    print(f'Sent {len(items)} articles')

if __name__ == '__main__':
    main()
