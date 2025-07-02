# sophia_feed_monitor_cog.py
import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import xml.etree.ElementTree as ET
import logging
from typing import Dict, List, Any, Optional, Set
import asyncio
import config
from rpg_utils import transaction
from bs4 import BeautifulSoup

logger = logging.getLogger('SophiaBot.FeedMonitorCog')

# デフォルトで登録するソース (Google Newsベース)
DEFAULT_SOURCES = [
    {"name": "テクノロジー", "feed_url": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZU0JXVnVMVWRDR2dKSlRpZ0FQAQ?hl=ja&gl=JP&ceid=JP:ja", "feed_type": "rss"},
    {"name": "AI", "feed_url": "https://news.google.com/rss/search?q=AI&hl=ja&gl=JP&ceid=JP:ja", "feed_type": "rss"},
    {"name": "PCハードウェア", "feed_url": "https://news.google.com/rss/search?q=PC%E3%83%8F%E3%83%BC%E3%83%89%E3%82%A6%E3%82%A7%E3%82%A2&hl=ja&gl=JP&ceid=JP:ja", "feed_type": "rss"},
    {"name": "PCゲーム", "feed_url": "https://news.google.com/rss/search?q=PC%E3%82%B2%E3%83%BC%E3%83%A0&hl=ja&gl=JP&ceid=JP:ja", "feed_type": "rss"},
]


class FeedMonitorCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self.initial_setup_done: Set[int] = set()
        self.failed_feeds: Set[str] = set() # 404エラーになったフィードURLを記録
        self.feed_check_task.start()
        logger.info("フィード監視Cogが初期化されました。")

    async def cog_load(self):
        """Cogがロード���れたときにデータベースのテーブルを初期化し、古いデータを移行する"""
        if self.bot.db:
            async with transaction(self.bot.db) as conn:
                # 新しいテーブルを作成
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS feed_sources (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        channel_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        feed_url TEXT NOT NULL,
                        feed_type TEXT NOT NULL,
                        last_notified_url TEXT,
                        UNIQUE(guild_id, feed_url)
                    )
                ''')
                logger.info("feed_sourcesテーブルの準備ができました。")

                # 古いテーブル(news_sources)が存在するか確認
                cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='news_sources'")
                old_table_exists = await cursor.fetchone()

                if old_table_exists:
                    logger.info("古いテーブル 'news_sources' を検出しました。データ移行を開始します...")
                    try:
                        # データを新しいテーブルにコピー (重複は無視)
                        await conn.execute('''
                            INSERT OR IGNORE INTO feed_sources (guild_id, channel_id, name, feed_url, feed_type, last_notified_url)
                            SELECT guild_id, channel_id, name, feed_url, feed_type, last_notified_url
                            FROM news_sources
                        ''')
                        # 古いテーブルを削除
                        await conn.execute("DROP TABLE news_sources")
                        logger.info("データ移行が完了し、古いテーブル 'news_sources' を削除しました。")
                    except Exception as e:
                        logger.error(f"データ移行中にエラーが発生しました: {e}", exc_info=True)
        else:
            logger.error("データベース接続が利用できません。フィード監視���能は動作しません。")

    async def _setup_default_sources(self, guild: discord.Guild):
        """ギルドにデフォルトのソースをセットアップする"""
        if guild.id in self.initial_setup_done or not self.bot.db:
            return

        target_channel = discord.utils.find(lambda c: c.name in ['news', 'ニュース', 'feed', 'フィード', 'general', '雑談'] and c.permissions_for(guild.me).send_messages, guild.text_channels)
        if not target_channel:
            target_channel = next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)

        if not target_channel:
            logger.warning(f"ギルド '{guild.name}' に通知可能なチャンネルが見つからなかったため、デフォルトソースのセットアップをスキップします。")
            return

        logger.info(f"ギルド '{guild.name}' のデフォルトソースをチャンネル '{target_channel.name}' にセットアップします。")
        try:
            async with transaction(self.bot.db):
                for source in DEFAULT_SOURCES:
                    await self.bot.db.execute(
                        """
                        INSERT INTO feed_sources (guild_id, channel_id, name, feed_url, feed_type)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(guild_id, feed_url) DO NOTHING
                        """,
                        (guild.id, target_channel.id, source["name"], source["feed_url"], source["feed_type"])
                    )
            self.initial_setup_done.add(guild.id)
            logger.info(f"ギルド '{guild.name}' のデフォルトソースのセットアップが完了しました。")
        except Exception as e:
            logger.error(f"ギルド '{guild.name}' のデフォルトソースセットアップ中にエラー: {e}", exc_info=True)


    def cog_unload(self):
        self.feed_check_task.cancel()
        if self.session and not self.session.closed:
            self.bot.loop.create_task(self.session.close())
        logger.info("フィード監視Cogのタスクがキャンセルされました。")

    async def get_thumbnail_from_url(self, url: str) -> Optional[str]:
        """記事URLからOGP画像を取得する"""
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
            async with self.session.get(url, timeout=10, headers=headers) as response:
                if response.status == 200:
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    og_image = soup.find('meta', property='og:image')
                    if og_image and og_image.get('content'):
                        return og_image.get('content')
        except Exception as e:
            logger.warning(f"サムネイル取得中にエラー ({url}): {e}")
        return None

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
    async def feed_check_task(self):
        await self.bot.wait_until_ready()
        if not self.bot.db:
            return
        
        for guild in self.bot.guilds:
            await self._setup_default_sources(guild)

        try:
            async with self.bot.db.execute("SELECT id, name, feed_url, channel_id, feed_type, last_notified_url FROM feed_sources") as cursor:
                sources = await cursor.fetchall()
        except Exception as e:
            logger.error(f"データベースからのソース取得に失敗しました: {e}")
            return

        for source_id, name, feed_url, channel_id, feed_type, last_notified_url in sources:
            if feed_url in self.failed_feeds:
                continue

            try:
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
                async with self.session.get(feed_url, headers=headers) as response:
                    if response.status != 200:
                        if feed_url not in self.failed_feeds:
                            self.failed_feeds.add(feed_url)
                        continue
                    
                    content = await response.text()
                    articles = await self.parse_feed(content, feed_type)

                    if not articles:
                        continue

                    latest_article = articles[0]
                    latest_url = latest_article['link']

                    if latest_url != last_notified_url:
                        if last_notified_url is not None:
                            channel = self.bot.get_channel(channel_id)
                            if channel:
                                prompt = config.NEWS_COMMENT_PROMPT_TEMPLATE.format(news_title=latest_article['title'])
                                comment = await self.bot.generate_text_with_specific_model(prompt, "gemini-2.5-flash")
                                
                                embed = discord.Embed(
                                    title=f"【{name}】{latest_article['title']}",
                                    url=latest_article['link'],
                                    description=f">>> {comment}" if comment else None,
                                    color=discord.Color.blue()
                                )
                                thumbnail_url = await self.get_thumbnail_from_url(latest_article['link'])
                                if thumbnail_url:
                                    embed.set_image(url=thumbnail_url)

                                await channel.send(embed=embed)
                                logger.info(f"新着情報を通知しました: {name} - {latest_article['title']}")
                            else:
                                logger.error(f"チャンネルが見つかりません: {channel_id}")
                            logger.info(f"{name} の最終通知URLを更新しました。")
                        else:
                            logger.info(f"{name} の初回URLを記録しました (通知はスキップ)。")
                        
                        async with transaction(self.bot.db):
                            await self.bot.db.execute("UPDATE feed_sources SET last_notified_url = ? WHERE id = ?", (latest_url, source_id))

            except Exception as e:
                if feed_url not in self.failed_feeds:
                    logger.error(f"{name} のチェック中にエラーが発生しました: {e}", exc_info=True)
                    self.failed_feeds.add(feed_url)
            
            await asyncio.sleep(3)

    @feed_check_task.before_loop
    async def before_feed_check_task(self):
        await self.bot.wait_until_ready()
        logger.info("フィード監視タスクのループを開始します。")

    q_group = app_commands.Group(name="q", description="フィード監視ソースの管理コマンド")

    @q_group.command(name="add", description="新しいフィードを監視リストに追加します。")
    @app_commands.describe(name="ソースの分かりやすい名前", feed_url="RSSまたはAtomフィードのURL", channel="通知を送信するチャンネル")
    async def add_source(self, interaction: discord.Interaction, name: str, feed_url: str, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=False)

        feed_type = None
        try:
            async with self.session.get(feed_url) as response:
                if response.status == 200:
                    content = await response.text()
                    if '<rss' in content.lower():
                        feed_type = 'rss'
                    elif '<feed' in content.lower():
                        feed_type = 'atom'
        except Exception as e:
            logger.error(f"フィードURLの形式確認中にエラー: {e}")
            await interaction.followup.send(f"フィードURLの確認中にエラーが起きちゃった…URLが正しいか確認してみてね！\n`{e}`", ephemeral=True)
            return

        if not feed_type:
            await interaction.followup.send("うーん、そのURLはRSSかAtomフィードじゃないみたい…もう一度確認してくれる？", ephemeral=True)
            return

        try:
            async with transaction(self.bot.db):
                await self.bot.db.execute(
                    "INSERT INTO feed_sources (guild_id, channel_id, name, feed_url, feed_type) VALUES (?, ?, ?, ?, ?)",
                    (interaction.guild_id, channel.id, name, feed_url, feed_type)
                )
            
            if feed_url in self.failed_feeds:
                self.failed_feeds.remove(feed_url)

            logger.info(f"ギルド {interaction.guild_id} に新しいソース '{name}' ({feed_url}) を追加しました。")
            embed = discord.Embed(
                title="ソース追加完了！",
                description=f"**{name}** を監視リストに追加したよ！\nこれから新しい情報があったら {channel.mention} で教えるね！",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed, ephemeral=False)
        except Exception as e:
            logger.error(f"ソースのDB追加中にエラー: {e}")
            await interaction.followup.send(f"データベースへの登録中にエラーが起きちゃったみたい…\nもしかして、そのURLはもう登録されてないかな？\n`{e}`", ephemeral=True)

    @q_group.command(name="list", description="このサーバーで監視中のソース一覧を表示します。")
    async def list_sources(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        try:
            async with self.bot.db.execute("SELECT id, name, feed_url, channel_id FROM feed_sources WHERE guild_id = ?", (interaction.guild_id,)) as cursor:
                sources = await cursor.fetchall()
            
            if not sources:
                await interaction.followup.send("このサーバーではまだ何もソースを監視してないみたいだよ！", ephemeral=True)
                return

            embed = discord.Embed(title=f"{interaction.guild.name}のフィード監視リスト", color=discord.Color.blue())
            description = ""
            for source_id, name, feed_url, channel_id in sources:
                channel = self.bot.get_channel(channel_id)
                channel_mention = channel.mention if channel else f"ID:{channel_id}"
                status = "❌" if feed_url in self.failed_feeds else "✅"
                description += f"**ID: {source_id}** | **{name}** {status}\n"
                description += f"  URL: <{feed_url}>\n"
                description += f"  通知先: {channel_mention}\n\n"
            
            embed.description = description
            embed.set_footer(text="✅: 正常 / ❌: このセッションでエラーが発生")
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"ソース一覧の取得中にエラー: {e}")
            await interaction.followup.send("一覧の表示中にエラーが起きちゃった…ごめんね！", ephemeral=True)

    @q_group.command(name="remove", description="監視リストからソースを削除します。")
    @app_commands.describe(source_ids="削除したいソースのIDをスペース区切りで入力 (`/q list`で確認)")
    async def remove_source(self, interaction: discord.Interaction, source_ids: str):
        await interaction.response.defer(ephemeral=False)

        try:
            ids_to_remove_str = source_ids.split()
            ids_to_remove_int = [int(id_str) for id_str in ids_to_remove_str]
            if not ids_to_remove_int:
                raise ValueError("削除するIDが指定されていません。")
        except ValueError:
            embed = discord.Embed(title="入力エラー", description="ソースIDは半角数字で、スペース区切りで入力してください。", color=discord.Color.red())
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        removed_ids = []
        failed_ids = []
        
        try:
            async with transaction(self.bot.db) as conn:
                for source_id in ids_to_remove_int:
                    # 削除前にfeed_urlを取得してfailed_feedsから削除
                    async with conn.execute("SELECT feed_url FROM feed_sources WHERE id = ? AND guild_id = ?", (source_id, interaction.guild_id)) as cursor:
                        row = await cursor.fetchone()
                        if row and row[0] in self.failed_feeds:
                            self.failed_feeds.remove(row[0])

                    # データを削除
                    cursor = await conn.execute(
                        "DELETE FROM feed_sources WHERE id = ? AND guild_id = ?",
                        (source_id, interaction.guild_id)
                    )
                    if cursor.rowcount > 0:
                        removed_ids.append(str(source_id))
                        logger.info(f"ギルド {interaction.guild_id} のソースID {source_id} を削除しました。")
                    else:
                        failed_ids.append(str(source_id))
            
            # 結果を報告
            if removed_ids:
                embed = discord.Embed(
                    title="ソース削除完了！",
                    description=f"ID: `{', '.join(removed_ids)}` のソースを監視リストから削除したよ！",
                    color=discord.Color.green()
                )
                if failed_ids:
                    embed.add_field(name="削除失敗", value=f"ID: `{', '.join(failed_ids)}` は見つからなかったか、このサーバーのじゃなかったみたい。", inline=False)
                await interaction.followup.send(embed=embed)
            else:
                embed = discord.Embed(
                    title="あれれ？",
                    description=f"ID: `{', '.join(failed_ids)}` のソースは見つからなかったみたい…",
                    color=discord.Color.orange()
                )
                await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"ソースの削除中にエラー: {e}", exc_info=True)
            await interaction.followup.send("削除中にエラーが起きちゃった…ごめんね！", ephemeral=True)


async def setup(bot):
    if not hasattr(bot, 'generate_text_from_prompt'):
        logger.error("Botオブジェクトに `generate_text_from_prompt` メソッドが存在しません。FeedMonitorCogをロードできません。")
        return
    await bot.add_cog(FeedMonitorCog(bot))
