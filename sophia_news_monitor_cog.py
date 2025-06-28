import discord
from discord.ext import commands, tasks
import aiohttp
import xml.etree.ElementTree as ET
import logging
from typing import Dict, List, Any
import asyncio
import config

logger = logging.getLogger('SophiaBot.NewsMonitorCog')

# 監視対象のニュースソース設定
NEWS_SOURCES: List[Dict[str, Any]] = [
    {
        "name": "Publickey",
        "feed_url": "https://www.publickey1.jp/atom.xml",
        "channel_id": 1388365448998944879,
        "feed_type": "atom"
    },
    {
        "name": "ITmedia PC USER",
        "feed_url": "https://rss.itmedia.co.jp/rss/2.0/pcuser.xml",
        "channel_id": 1388365448998944879,
        "feed_type": "rss"
    },
    {
        "name": "ASCII.jp",
        "feed_url": "https://ascii.jp/rss.xml",
        "channel_id": 1388365448998944879,
        "feed_type": "rss"
    }
]

class NewsMonitorCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        # サイトごとに最後に通知した記事のURLを保持
        self.last_notified_urls: Dict[str, str] = {}
        self.news_check_task.start()
        logger.info("ニュース監視Cogが初期化されました。")

    def cog_unload(self):
        self.news_check_task.cancel()
        # セッションを閉じる
        if self.session and not self.session.closed:
            self.bot.loop.create_task(self.session.close())
        logger.info("ニュース監視Cogのタスクがキャンセルされました。")

    async def parse_feed(self, feed_content: str, feed_type: str) -> List[Dict[str, str]]:
        """RSS/Atomフィードを解析して記事のリストを返す"""
        articles = []
        try:
            root = ET.fromstring(feed_content)
            if feed_type == 'atom':
                ns = {'atom': 'http://www.w3.org/2005/Atom'}
                entries = root.findall('atom:entry', ns)
                for entry in entries:
                    title = entry.find('atom:title', ns).text
                    link = entry.find('atom:link', ns).get('href')
                    articles.append({'title': title, 'link': link})
            elif feed_type == 'rss':
                entries = root.findall('.//item')
                for entry in entries:
                    title = entry.find('title').text
                    link = entry.find('link').text
                    articles.append({'title': title, 'link': link})
        except Exception as e:
            logger.error(f"フィードの解析に失敗しました: {e}")
        return articles

    @tasks.loop(minutes=10)
    async def news_check_task(self):
        await self.bot.wait_until_ready()
        
        for source in NEWS_SOURCES:
            name = source["name"]
            feed_url = source["feed_url"]
            channel_id = source["channel_id"]
            feed_type = source["feed_type"]

            try:
                async with self.session.get(feed_url) as response:
                    if response.status != 200:
                        logger.warning(f"{name} のフィード取得に失敗しました。ステータス: {response.status}")
                        continue
                    
                    content = await response.text()
                    articles = await self.parse_feed(content, feed_type)

                    if not articles:
                        logger.info(f"{name}: 記事が見つかりませんでした。")
                        continue

                    # 最新の記事を取得
                    latest_article = articles[0]
                    latest_url = latest_article['link']

                    # 初回チェック時、またはURLが更新されている場合
                    if self.last_notified_urls.get(feed_url) != latest_url:
                        # 初回実行時は通知しない（起動時の大量通知を防ぐため）
                        if feed_url in self.last_notified_urls:
                            channel = self.bot.get_channel(channel_id)
                            if channel:
                                # AIにコメントを生成させる
                                prompt = config.NEWS_COMMENT_PROMPT_TEMPLATE.format(news_title=latest_article['title'])
                                comment = await self.bot.generate_text_from_prompt(prompt)
                                
                                embed = discord.Embed(
                                    title=f"【{name}】{latest_article['title']}",
                                    url=latest_article['link'],
                                    description=f"**ソフィアの一言**\n>>> {comment}" if comment else None,
                                    color=discord.Color.blue()
                                )
                                await channel.send(embed=embed)
                                logger.info(f"新着記事を通知しました: {name} - {latest_article['title']}")
                            else:
                                logger.error(f"チャンネルが見つかりません: {channel_id}")
                        else:
                            logger.info(f"{name} の初回チェック完了。次回から新着記事を通知します。")

                        # 最後に通知したURLを更新
                        self.last_notified_urls[feed_url] = latest_url

            except Exception as e:
                logger.error(f"{name} のチェック中にエラーが発生しました: {e}", exc_info=True)
            
            # 連続リクエストを避けるための短い待機
            await asyncio.sleep(5)

    @news_check_task.before_loop
    async def before_news_check_task(self):
        await self.bot.wait_until_ready()
        logger.info("ニュース監視タスクのループを開始します。")


async def setup(bot):
    if not hasattr(bot, 'generate_text_from_prompt'):
        logger.error("Botオブジェクトに `generate_text_from_prompt` メソッドが存在しません。NewsMonitorCogをロードできません。")
        return
    await bot.add_cog(NewsMonitorCog(bot))
