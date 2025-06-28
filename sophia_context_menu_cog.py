import discord
from discord import app_commands
from discord.ext import commands
import logging
from datetime import datetime, timedelta
import asyncio
from typing import Dict, Optional, Any, List
import sqlite3
import json
import os
import re
import aiohttp
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
import xml.etree.ElementTree
import base64

logger = logging.getLogger('SophiaBot.ContextMenuCog')

class DeleteTimerView(discord.ui.View):
    def __init__(self, message_to_delete: discord.Message, interaction_user: discord.User):
        super().__init__(timeout=180.0)
        self.message_to_delete = message_to_delete
        self.interaction_user = interaction_user
        self.delete_delay_seconds = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.interaction_user.id:
            await interaction.response.send_message("このボタンはあなた専用だよ！他の人は使えないんだ～", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button): item.disabled = True
        logger.debug(f"DeleteTimerView for message {self.message_to_delete.id} timed out for user {self.interaction_user.id}.")

    async def handle_delete_selection(self, interaction: discord.Interaction, hours: int):
        self.delete_delay_seconds = hours * 3600
        try:
            await interaction.response.edit_message(content=f"おっけー！このメッセージ、約{hours}時間後に消しちゃうね！", view=None)
        except discord.NotFound: logger.warning(f"時間指定削除の応答メッセージ編集に失敗: Message not found (interaction user: {interaction.user.id})")
        except discord.HTTPException as e: logger.error(f"時間指定削除の応答メッセージ編集中にHTTPエラー: {e} (interaction user: {interaction.user.id})")
        self.stop()

    @discord.ui.button(label="6時間後", style=discord.ButtonStyle.secondary, custom_id="sophia_ctx_delete_6h_v4")
    async def delete_6h(self, interaction: discord.Interaction, button: discord.ui.Button): await self.handle_delete_selection(interaction, 6)

    @discord.ui.button(label="12時間後", style=discord.ButtonStyle.secondary, custom_id="sophia_ctx_delete_12h_v4")
    async def delete_12h(self, interaction: discord.Interaction, button: discord.ui.Button): await self.handle_delete_selection(interaction, 12)

    @discord.ui.button(label="24時間後", style=discord.ButtonStyle.secondary, custom_id="sophia_ctx_delete_24h_v4")
    async def delete_24h(self, interaction: discord.Interaction, button: discord.ui.Button): await self.handle_delete_selection(interaction, 24)

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.danger, custom_id="sophia_ctx_delete_cancel_v4")
    async def cancel_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.delete_delay_seconds = None
        try:
            await interaction.response.edit_message(content="やっぱり消すのやめるね！うん、それがいいかも！", view=None)
        except discord.NotFound: logger.warning(f"時間指定削除キャンセルの応答メッセージ編集に失敗: Message not found (interaction user: {interaction.user.id})")
        except discord.HTTPException as e: logger.error(f"時間指定削除キャンセルの応答メッセージ編集中にHTTPエラー: {e} (interaction user: {interaction.user.id})")
        self.stop()

class ContextMenuCog(commands.Cog, name="ContextMenuCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.logger = logger
        self.db_conn: Optional[sqlite3.Connection] = None
        self.sticky_messages_data: Dict[int, Dict[int, Dict[int, Dict[str, Any]]]] = {}
        self.sticky_channel_locks: Dict[int, asyncio.Lock] = {}
        self.executor = self.bot.executor # type: ignore
        self.db_file_path = self.bot.sticky_db_path # type: ignore
        self.logger.info(f"ContextMenuCog: スティッキーメッセージDBパスを '{self.db_file_path}' に設定しました。")
        self._init_db()
        if self.db_conn: self._load_sticky_messages_from_db()
        else: self.logger.error("データベース接続が確立されていないため、スティッキーメッセージの読み込みをスキップします。")

        self.summarize_message_context_menu = app_commands.ContextMenu(
            name='このメッセージをAIで要約',
            callback=self.summarize_message_callback
        )
        self.bot.tree.add_command(self.summarize_message_context_menu)

        self.sticky_message_context_menu = app_commands.ContextMenu(
            name='このメッセージを一番下に表示し続ける',
            callback=self.toggle_sticky_message_callback
        )
        self.bot.tree.add_command(self.sticky_message_context_menu)

        self.timed_delete_message_context_menu = app_commands.ContextMenu(
            name='このメッセージを後で消す',
            callback=self.timed_delete_message_callback
        )
        self.bot.tree.add_command(self.timed_delete_message_context_menu)

        self.embed_message_context_menu = app_commands.ContextMenu(
            name='メッセージを埋め込みにする',
            callback=self.embed_message_callback
        )
        self.bot.tree.add_command(self.embed_message_context_menu)

        self.count_chars_message_context_menu = app_commands.ContextMenu(
            name='メッセージの文字数を数える',
            callback=self.count_chars_message_callback
        )
        self.bot.tree.add_command(self.count_chars_message_context_menu)

    def _init_db(self):
        try:
            db_dir = os.path.dirname(self.db_file_path)
            if db_dir and not os.path.exists(db_dir):
                try: os.makedirs(db_dir); self.logger.info(f"データベースディレクトリ '{db_dir}' を作成しました。")
                except OSError as e_mkdir: self.logger.error(f"データベースディレクトリ '{db_dir}' の作成に失敗: {e_mkdir}。処理を続行します。")
            self.db_conn = sqlite3.connect(self.db_file_path)
            cursor = self.db_conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS sticky_messages (
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    original_message_id INTEGER NOT NULL,
                    sticky_bot_message_id INTEGER,
                    content TEXT,
                    embed_json TEXT,
                    author_id INTEGER,
                    PRIMARY KEY (guild_id, channel_id, original_message_id))''')
            self.db_conn.commit()
            self.logger.info(f"データベース '{self.db_file_path}' を初期化/接続しました。")
        except sqlite3.Error as e: self.logger.error(f"データベース初期化/接続エラー ({self.db_file_path}): {e}", exc_info=True); self.db_conn = None
        except OSError as e_os: self.logger.error(f"データベースファイル/ディレクトリ操作エラー ({self.db_file_path}): {e_os}", exc_info=True); self.db_conn = None

    def _load_sticky_messages_from_db(self):
        if not self.db_conn: self.logger.warning("DB接続がないため、スティッキーメッセージを読み込めません。"); return
        try:
            cursor = self.db_conn.cursor()
            cursor.execute("SELECT guild_id, channel_id, original_message_id, sticky_bot_message_id, content, embed_json, author_id FROM sticky_messages")
            rows = cursor.fetchall()
            for row in rows:
                gid, cid, orig_mid, sticky_bid, cont, emb_json, auth_id = row
                emb_obj = None
                if emb_json:
                    try:
                        emb_data = json.loads(emb_json)
                        emb_obj = discord.Embed.from_dict(emb_data)
                    except Exception as e_emb:
                        self.logger.error(f"DBからEmbed復元失敗 (G:{gid}, C:{cid}, OrigM:{orig_mid}): {e_emb}")

                if gid not in self.sticky_messages_data:
                    self.sticky_messages_data[gid] = {}
                if cid not in self.sticky_messages_data[gid]:
                    self.sticky_messages_data[gid][cid] = {}

                self.sticky_messages_data[gid][cid][orig_mid] = {
                    "sticky_bot_message_id": sticky_bid,
                    "content": cont,
                    "embed": emb_obj,
                    "author_id": auth_id
                }
            self.logger.info(f"{len(rows)}件のスティッキーメッセージ情報をDBから読み込みました。")
        except sqlite3.Error as e: self.logger.error(f"DBからのスティッキー読み込みエラー: {e}", exc_info=True)
        except Exception as e_gen: self.logger.error(f"スティッキー読み込み中の予期せぬエラー: {e_gen}", exc_info=True)

    async def cog_unload(self):
        if hasattr(self, 'summarize_message_context_menu'):
            self.bot.tree.remove_command(self.summarize_message_context_menu.name, type=self.summarize_message_context_menu.type)
        if hasattr(self, 'sticky_message_context_menu'):
            self.bot.tree.remove_command(self.sticky_message_context_menu.name, type=self.sticky_message_context_menu.type)
        if hasattr(self, 'timed_delete_message_context_menu'):
            self.bot.tree.remove_command(self.timed_delete_message_context_menu.name, type=self.timed_delete_message_context_menu.type)
        if hasattr(self, 'embed_message_context_menu'):
            self.bot.tree.remove_command(self.embed_message_context_menu.name, type=self.embed_message_context_menu.type)
        if hasattr(self, 'count_chars_message_context_menu'):
            self.bot.tree.remove_command(self.count_chars_message_context_menu.name, type=self.count_chars_message_context_menu.type)
        self.logger.info("ContextMenuCog のコンテキストメニューコマンドを削除しました。")

        if self.db_conn:
            try:
                self.db_conn.close()
                self.logger.info(f"データベース '{self.db_file_path}' への接続を閉じました。")
            except sqlite3.Error as e:
                self.logger.error(f"データベース接続クローズエラー: {e}", exc_info=True)

    def _prepare_sticky_content(self, content: Optional[str], original_message_id_log: Any) -> Optional[str]:
        if content is None:
            return None
        MAX_DISCORD_CONTENT_LENGTH = 2000
        TRUNCATION_SUFFIX = " ... (文字数制限のため一部省略)"
        if len(content) > MAX_DISCORD_CONTENT_LENGTH:
            self.logger.warning(
                f"スティッキーメッセージのcontentが長すぎるため ({len(content)}文字)、切り詰めます。"
                f" (関連メッセージID: {original_message_id_log})"
            )
            cut_length = MAX_DISCORD_CONTENT_LENGTH - len(TRUNCATION_SUFFIX)
            if cut_length < 0: cut_length = 0
            return content[:cut_length] + TRUNCATION_SUFFIX
        return content

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild: return
        gid = message.guild.id
        cid = message.channel.id
        if gid in self.sticky_messages_data and cid in self.sticky_messages_data[gid]:
            if cid not in self.sticky_channel_locks:
                self.sticky_channel_locks[cid] = asyncio.Lock()
            async with self.sticky_channel_locks[cid]:
                await asyncio.sleep(0.75)
                channel_stickies = self.sticky_messages_data[gid][cid]
                for original_message_id, sticky_data in list(channel_stickies.items()):
                    old_sticky_bot_message_id = sticky_data.get("sticky_bot_message_id")
                    if old_sticky_bot_message_id:
                        try:
                            old_sticky_msg = await message.channel.fetch_message(old_sticky_bot_message_id)
                            await old_sticky_msg.delete()
                        except discord.NotFound:
                            logger.debug(f"古いスティッキーボットメッセージ {old_sticky_bot_message_id} (元ID: {original_message_id}) が見つかりませんでした。")
                        except discord.Forbidden:
                            logger.warning(f"古いスティッキーボットメッセージ {old_sticky_bot_message_id} (元ID: {original_message_id}) の削除権限がありません。")
                        except Exception as e_del_old:
                            logger.error(f"古いスティッキーボットメッセージ {old_sticky_bot_message_id} (元ID: {original_message_id}) の削除中にエラー: {e_del_old}")
                    try:
                        raw_content_on_repost = sticky_data.get("content")
                        embed_on_repost = sticky_data.get("embed")
                        prepared_content_on_repost = self._prepare_sticky_content(raw_content_on_repost, original_message_id)
                        bot_msg: Optional[discord.Message] = None
                        if embed_on_repost:
                            bot_msg = await message.channel.send(content=prepared_content_on_repost, embed=embed_on_repost)
                        elif prepared_content_on_repost and prepared_content_on_repost.strip():
                            bot_msg = await message.channel.send(content=prepared_content_on_repost)
                        if bot_msg:
                            self.sticky_messages_data[gid][cid][original_message_id]["sticky_bot_message_id"] = bot_msg.id
                            if self.db_conn:
                                try:
                                    cur = self.db_conn.cursor()
                                    cur.execute("UPDATE sticky_messages SET sticky_bot_message_id = ? WHERE guild_id = ? AND channel_id = ? AND original_message_id = ?",
                                                (bot_msg.id, gid, cid, original_message_id))
                                    self.db_conn.commit()
                                except sqlite3.Error as e_dbu:
                                    self.logger.error(f"DB更新エラー (sticky_bot_message_id for original_message_id {original_message_id}): {e_dbu}")
                        else:
                            logger.warning(f"スティッキーメッセージ (元ID: {original_message_id}) の再投稿データが空でした (テキスト内容も埋め込みもなし)。")
                    except discord.Forbidden:
                        logger.error(f"スティッキーメッセージ (元ID: {original_message_id}) 再投稿中に権限エラー。")
                    except discord.HTTPException as e_http_repost:
                        if e_http_repost.code == 50035:
                             self.logger.error(f"スティッキーメッセージ (元ID: {original_message_id}) 再投稿中に文字数制限エラーが再発しました: {e_http_repost.text}")
                        else:
                             self.logger.error(f"スティッキーメッセージ (元ID: {original_message_id}) 再投稿中にHTTPエラー: {e_http_repost}", exc_info=True)
                    except Exception as e_repost:
                        self.logger.error(f"スティッキーメッセージ (元ID: {original_message_id}) 再投稿中に予期せぬエラー: {e_repost}", exc_info=True)

    # ▼▼▼▼▼ ここからが修正・再構築したAI要約機能 v3 ▼▼▼▼▼

    async def _get_http_session(self) -> aiohttp.ClientSession:
        """aiohttpのセッションを管理・取得します。"""
        if not hasattr(self.bot, 'http_session') or self.bot.http_session.closed:
            self.logger.warning("ContextMenuCog用に新しいaiohttp.ClientSessionを作成します。")
            self.bot.http_session = aiohttp.ClientSession()
        return self.bot.http_session

    async def _get_image_data_from_url(self, url: str) -> Dict[str, Any]:
        """URLから画像データを取得し、MIMEタイプとBase64エンコードされたデータを返します。"""
        try:
            session = await self._get_http_session()
            async with session.get(url, timeout=20) as response:
                response.raise_for_status()
                image_bytes = await response.read()
                mime_type = response.content_type
                if not mime_type or not mime_type.startswith('image/'):
                    # MIMEタイプが不明な場合はファイル拡張子から推測
                    ext = os.path.splitext(url.split('?')[0])[-1].lower()
                    if ext in ['.jpg', '.jpeg']: mime_type = 'image/jpeg'
                    elif ext == '.png': mime_type = 'image/png'
                    elif ext == '.gif': mime_type = 'image/gif'
                    elif ext == '.webp': mime_type = 'image/webp'
                    else: raise ValueError(f"サポートされていない画像形式です: {ext}")
                
                return {
                    "mime_type": mime_type,
                    "data": base64.b64encode(image_bytes).decode('utf-8')
                }
        except Exception as e:
            self.logger.error(f"URLからの画像取得に失敗しました: {url}, エラー: {e}")
            raise

    async def _fetch_url_content(self, url: str) -> str:
        """URLからテキストコンテンツを取得します。"""
        if re.match(r'https?:\/\/(twitter\.com|x\.com)\/.+', url):
            self.logger.warning(f"Twitter/XのURLが検出されたため、コンテンツ取得をスキップします: {url}")
            return "（技術的な制限により、Twitter/Xのコンテンツは取得・要約できません。）"
        
        self.logger.info(f"URLからコンテンツの取得を開始: {url}")
        try:
            session = await self._get_http_session()
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
            async with session.get(url, timeout=15, headers=headers) as response:
                response.raise_for_status()
                content_type = response.headers.get('Content-Type', '')
                if 'text/html' not in content_type:
                    return f"（このURLのコンテンツはHTMLページではないため処理できませんでした: {content_type}）"
                html_content = await response.text()
                loop = asyncio.get_running_loop()
                soup = await loop.run_in_executor(self.executor, lambda: BeautifulSoup(html_content, 'html.parser'))
                for element in soup(["script", "style", "nav", "footer", "aside", "header"]):
                    element.decompose()
                text = soup.get_text(separator=' ', strip=True)
                MAX_WEB_CONTENT_LENGTH = 7000
                if len(text) > MAX_WEB_CONTENT_LENGTH:
                    text = text[:MAX_WEB_CONTENT_LENGTH] + " ... (コンテンツが長いため省略されました)"
                return text if text.strip() else "（このURLにはテキストコンテンツが見つかりませんでした。）"
        except Exception as e:
            self.logger.error(f"URL処理中にエラー: {url}: {e}", exc_info=True)
            return f"（URLの処理中にエラーが発生しました: {type(e).__name__}）"

    def _get_youtube_video_id(self, url: str) -> Optional[str]:
        patterns = [
            r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})',
            r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/shorts\/([a-zA-Z0-9_-]{11})'
        ]
        for p in patterns:
            match = re.search(p, url)
            if match: return match.group(1)
        return None

    def _fetch_youtube_transcript_sync(self, video_id: str) -> str:
        try:
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['ja', 'en'])
            text_content = " ".join([part['text'] for part in transcript_list])
            MAX_TRANSCRIPT_LENGTH = 7000
            if len(text_content) > MAX_TRANSCRIPT_LENGTH:
                text_content = text_content[:MAX_TRANSCRIPT_LENGTH] + " ... (字幕が長いため省略されました)"
            self.logger.info(f"YouTubeビデオIDの字幕取得に成功: {video_id}")
            return text_content.strip()
        except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
            self.logger.warning(f"YouTubeビデオ(ID:{video_id})の字幕が利用できません。")
            return "（この動画の字幕は利用できませんでした。）"
        except xml.etree.ElementTree.ParseError:
            return "（字幕データはありましたが、解析に失敗しました。）"
        except Exception as e:
            self.logger.error(f"YouTube字幕取得中に予期せぬエラー(ID: {video_id}): {e}", exc_info=True)
            return f"（字幕取得中に内部エラーが発生しました: {type(e).__name__}）"

    async def _fetch_youtube_transcript(self, video_id: str) -> str:
        if not video_id: return "（無効なYouTubeビデオIDです。）"
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, self._fetch_youtube_transcript_sync, video_id)

    def _extract_text_from_embed(self, embed: discord.Embed) -> str:
        texts = []
        if embed.title: texts.append(embed.title)
        if embed.description: texts.append(embed.description)
        for field in embed.fields: texts.append(f"{field.name}: {field.value}")
        if embed.footer and embed.footer.text: texts.append(embed.footer.text)
        if embed.author and embed.author.name: texts.append(f"作成者: {embed.author.name}")
        return "\n".join(texts)

    async def summarize_message_callback(self, interaction: discord.Interaction, message: discord.Message):
        self.logger.info(f"{interaction.user.name}がメッセージID {message.id}のAI要約を開始 (ギルド {interaction.guild_id})")
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not self.bot.model:
            self.logger.warning(f"AI要約が試行されましたが、Geminiモデルが利用できません (ギルド {interaction.guild_id})")
            await interaction.followup.send("ごめんなさい！AI機能が現在利用できないようです。", ephemeral=True)
            return

        gemini_payload = []
        processed_sources = []
        text_parts_for_prompt = []

        # 1. メッセージ本文
        if message.content:
            text_parts_for_prompt.append(f"--- 元のメッセージのテキスト ---\n{message.content}")
            processed_sources.append("元のメッセージ")

        # 2. 埋め込みメッセージ
        if message.embeds:
            for i, embed in enumerate(message.embeds):
                embed_text = self._extract_text_from_embed(embed)
                if embed_text.strip():
                    text_parts_for_prompt.append(f"\n--- 埋め込みメッセージ {i+1} の内容 ---\n{embed_text}")
                    processed_sources.append(embed.title or f"埋め込み {i+1}")
        
        # 3. 添付画像
        image_attachments = [att for att in message.attachments if att.content_type and att.content_type.startswith("image/")]
        if image_attachments:
            text_parts_for_prompt.append("\n--- 添付画像の解析 ---\n以下の画像を解析し、内容を考察・要約に含めてください。")
            for i, attachment in enumerate(image_attachments):
                try:
                    image_data = await self._get_image_data_from_url(attachment.url)
                    gemini_payload.append({"inline_data": image_data})
                    processed_sources.append(f"添付画像: {attachment.filename}")
                except Exception as e:
                    text_parts_for_prompt.append(f"（画像 '{attachment.filename}' の読み込みに失敗しました: {e}）")

        # 4. URLコンテンツ (YouTube含む)
        urls_found = list(set(re.findall(r'https?://[^\s<>"\']+|www\.[^\s<>"\']+', message.clean_content)))
        youtube_tasks = {}
        other_url_tasks = {}

        for url in urls_found:
            video_id = self._get_youtube_video_id(url)
            if video_id:
                thumbnail_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                youtube_tasks[url] = {
                    "transcript": self._fetch_youtube_transcript(video_id),
                    "thumbnail": self._get_image_data_from_url(thumbnail_url)
                }
            else:
                other_url_tasks[url] = self._fetch_url_content(url)

        # URLコンテンツ取得を実行
        other_url_results = await asyncio.gather(*other_url_tasks.values(), return_exceptions=True)
        for url, result in zip(other_url_tasks.keys(), other_url_results):
            content = result if not isinstance(result, Exception) else f"（コンテンツ取得エラー: {result}）"
            text_parts_for_prompt.append(f"\n--- Webページ「{url}」の内容 ---\n{content}")
            processed_sources.append(f"Webページ: {url.split('//')[-1].split('/')[0]}")
        
        for url, tasks in youtube_tasks.items():
            results = await asyncio.gather(tasks['transcript'], tasks['thumbnail'], return_exceptions=True)
            transcript, thumbnail_data = results
            
            text_parts_for_prompt.append(f"\n--- YouTube動画「{url}」の分析 ---")
            processed_sources.append(f"YouTube動画: {url.split('//')[-1].split('/')[0]}")
            
            if not isinstance(transcript, Exception):
                text_parts_for_prompt.append(f"【字幕情報】\n{transcript}")
            else:
                text_parts_for_prompt.append(f"（字幕の取得に失敗しました: {transcript}）")
            
            if not isinstance(thumbnail_data, Exception):
                text_parts_for_prompt.append("【サムネイル画像】")
                gemini_payload.append({"inline_data": thumbnail_data})
            else:
                 text_parts_for_prompt.append("（サムネイルの取得に失敗しました。）")

        # 最終的なプロンプトを作成
        prompt = (
            "あなたは優秀な多機能アシスタントです。以下のDiscordのメッセージと、そこに含まれるテキスト、画像、URL先のコンテンツ、YouTube動画の字幕とサムネイルを総合的に分析・要約してください。\n"
            "要約は以下の形式で、分かりやすくまとめてください:\n"
            "1. **【総合的な結論】**: 全体を300文字程度で簡潔にまとめる。\n"
            "2. **【詳細な分析・考察】**: 各ソース（テキスト、画像、動画など）の内容を個別に分析し、重要なポイントや考察を箇条書きで説明する。\n\n"
            "--- 分析対象のコンテンツ ---\n"
            + "\n".join(text_parts_for_prompt)
        )
        gemini_payload.insert(0, prompt)
        
        if len(text_parts_for_prompt) == 0 and not image_attachments:
             await interaction.followup.send("うーん、このメッセージには要約できるコンテンツが見当たらないみたい…", ephemeral=True)
             return

        try:
            response = await self.bot.model.generate_content_async(gemini_payload)
            summary_text = response.text
        except Exception as e:
            self.logger.error(f"メッセージ{message.id}のAI要約中にエラー: {e}", exc_info=True)
            await interaction.followup.send("AIでの要約中にエラーが発生しました…もう一度試してみてください！", ephemeral=True)
            return

        embed = discord.Embed(title="AIによる総合分析＆要約だよっ！", description=summary_text, color=discord.Color.purple(), timestamp=datetime.now())
        embed.add_field(name="元のメッセージ", value=f"[ここをクリックしてジャンプ！]({message.jump_url})", inline=False)
        source_list_str = "\n".join(f"- {s}" for s in processed_sources)
        if len(source_list_str) > 1024: source_list_str = source_list_str[:1021] + "..."
        embed.add_field(name="分析に使用したソース", value=source_list_str if source_list_str else "なし", inline=False)
        embed.set_footer(text=f"AI分析 by {self.bot.user.display_name}")
        await interaction.followup.send(embed=embed, ephemeral=True)


    # ▲▲▲▲▲ ここまでが修正・再構築したAI要約機能 v3 ▲▲▲▲▲


    async def toggle_sticky_message_callback(self, interaction: discord.Interaction, message: discord.Message):
        self.logger.info(f"{interaction.user.name} ({interaction.user.id}) がメッセージID {message.id} のスティッキー化/解除コマンドを使用 (チャンネル {interaction.channel_id} @ ギルド {interaction.guild_id})")
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("この機能はサーバーのテキストチャンネル内でのみ利用できるよ！", ephemeral=True)
            return
        gid = interaction.guild.id
        cid = interaction.channel.id
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("ごめんね！この操作をするには「メッセージの管理」権限が必要みたい…！", ephemeral=True)
            return
        if not self.db_conn:
            self.logger.error("DB接続がないため、スティッキー操作を実行できません。")
            await interaction.response.send_message("データベースエラーでスティッキー機能が使えないみたい…ごめんね！管理者に連絡してね！", ephemeral=True)
            return
        if cid not in self.sticky_channel_locks:
            self.sticky_channel_locks[cid] = asyncio.Lock()
        async with self.sticky_channel_locks[cid]:
            if gid not in self.sticky_messages_data: self.sticky_messages_data[gid] = {}
            if cid not in self.sticky_messages_data[gid]: self.sticky_messages_data[gid][cid] = {}
            channel_stickies = self.sticky_messages_data[gid][cid]
            original_message_id_to_operate_on: Optional[int] = None
            action_is_unsticky = False
            message_to_get_content_from = message
            for orig_msg_id_key, sticky_info_val in channel_stickies.items():
                if sticky_info_val.get("sticky_bot_message_id") == message.id:
                    original_message_id_to_operate_on = orig_msg_id_key
                    action_is_unsticky = True
                    self.logger.info(f"コマンド対象はボットのスティッキーメッセージ ({message.id})。操作対象の元メッセージID: {original_message_id_to_operate_on}")
                    break
            if original_message_id_to_operate_on is None:
                if message.id in channel_stickies:
                    original_message_id_to_operate_on = message.id
                    action_is_unsticky = True
                    self.logger.info(f"コマンド対象は現在スティッキー化されている元メッセージ ({message.id})。解除します。")
                else:
                    original_message_id_to_operate_on = message.id
                    action_is_unsticky = False
                    self.logger.info(f"コマンド対象は新しいメッセージ ({message.id})。スティッキー化します。")
            if action_is_unsticky:
                if original_message_id_to_operate_on not in channel_stickies:
                    self.logger.error(f"解除エラー: 対象の元メッセージID {original_message_id_to_operate_on} がメモリに存在しません。")
                    await interaction.response.send_message("あれ？解除しようとしたメッセージ情報が見つからないみたい…もう一度試してみてね。", ephemeral=True)
                    return
                sticky_to_remove = channel_stickies.pop(original_message_id_to_operate_on)
                old_sticky_bot_message_id = sticky_to_remove.get("sticky_bot_message_id")
                if old_sticky_bot_message_id:
                    try:
                        old_bot_msg = await interaction.channel.fetch_message(old_sticky_bot_message_id)
                        await old_bot_msg.delete()
                        self.logger.info(f"スティッキーメッセージ (ボットMSG ID: {old_sticky_bot_message_id}, 元ID: {original_message_id_to_operate_on}) を解除・削除しました。")
                    except discord.NotFound:
                        self.logger.info(f"解除対象のスティッキーボットメッセージ {old_sticky_bot_message_id} が見つかりませんでした。")
                    except discord.Forbidden:
                        self.logger.warning(f"スティッキーボットメッセージ {old_sticky_bot_message_id} の削除権限がありませんでした。")
                    except Exception as e_del_sticky:
                        self.logger.error(f"スティッキーボットメッセージ {old_sticky_bot_message_id} の削除中にエラー: {e_del_sticky}")
                try:
                    cur = self.db_conn.cursor()
                    cur.execute("DELETE FROM sticky_messages WHERE guild_id = ? AND channel_id = ? AND original_message_id = ?",
                                (gid, cid, original_message_id_to_operate_on))
                    self.db_conn.commit()
                except sqlite3.Error as e_db_del:
                    self.logger.error(f"DBからのスティッキー解除エラー (元ID: {original_message_id_to_operate_on}): {e_db_del}")
                await interaction.response.send_message(f"メッセージID {original_message_id_to_operate_on} の常時表示を解除したよ！", ephemeral=True)
                self.logger.info(f"チャンネル {cid} のスティッキーメッセージ (元ID: {original_message_id_to_operate_on}) を解除しました。")
                if not channel_stickies and cid in self.sticky_messages_data.get(gid, {}):
                    del self.sticky_messages_data[gid][cid]
                if not self.sticky_messages_data.get(gid, {}) and gid in self.sticky_messages_data:
                     del self.sticky_messages_data[gid]
            else:
                if message_to_get_content_from.author.id == self.bot.user.id:
                    self.logger.warning(f"ボット自身のメッセージ ({message_to_get_content_from.id}) をスティッキー化しようとしています。これが意図した動作か確認してください。(このメッセージはアクティブなスティッキーボットメッセージではありませんでした)")
                cont_sticky = message_to_get_content_from.content
                emb_obj_sticky: Optional[discord.Embed] = None
                emb_json_db: Optional[str] = None
                image_url_sticky: Optional[str] = None
                if message_to_get_content_from.attachments:
                    for attachment in message_to_get_content_from.attachments:
                        if attachment.content_type and attachment.content_type.startswith("image/"):
                            image_url_sticky = attachment.url
                            break
                if message_to_get_content_from.embeds:
                    emb_obj_sticky = discord.Embed.from_dict(message_to_get_content_from.embeds[0].to_dict())
                elif image_url_sticky:
                    emb_obj_sticky = discord.Embed(timestamp=message_to_get_content_from.created_at)
                    emb_obj_sticky.set_author(name=message_to_get_content_from.author.display_name, icon_url=message_to_get_content_from.author.display_avatar.url if message_to_get_content_from.author.display_avatar else None)
                if emb_obj_sticky and image_url_sticky:
                    emb_obj_sticky.set_image(url=image_url_sticky)
                if not cont_sticky and not emb_obj_sticky:
                    self.logger.warning(f"スティッキーにする表示可能なテキスト内容、埋め込み、または画像がありませんでした。元ID: {original_message_id_to_operate_on}")
                    await interaction.response.send_message("このメッセージには、スティッキーとして表示できるテキスト、埋め込み、または画像がないみたい。\n（例：ファイルやスタンプのみのメッセージなど）", ephemeral=True)
                    return
                if emb_obj_sticky:
                    current_footer = emb_obj_sticky.footer.text if emb_obj_sticky.footer and emb_obj_sticky.footer.text else ""
                    sticky_indicator = "📌 常時表示中"
                    if sticky_indicator not in current_footer:
                        new_footer = f"{current_footer} | {sticky_indicator}".strip(" |")
                        emb_obj_sticky.set_footer(text=new_footer, icon_url=emb_obj_sticky.footer.icon_url if emb_obj_sticky.footer else None)
                    emb_json_db = json.dumps(emb_obj_sticky.to_dict())
                new_sticky_data_entry = {"sticky_bot_message_id": None, "content": cont_sticky, "embed": emb_obj_sticky, "author_id": message_to_get_content_from.author.id}
                channel_stickies[original_message_id_to_operate_on] = new_sticky_data_entry
                try:
                    cur = self.db_conn.cursor()
                    cur.execute("""
                        INSERT INTO sticky_messages
                        (guild_id, channel_id, original_message_id, content, embed_json, author_id, sticky_bot_message_id)
                        VALUES (?, ?, ?, ?, ?, ?, NULL)
                        ON CONFLICT(guild_id, channel_id, original_message_id) DO UPDATE SET
                        content=excluded.content, embed_json=excluded.embed_json, author_id=excluded.author_id, sticky_bot_message_id=NULL
                        """,
                        (gid, cid, original_message_id_to_operate_on, new_sticky_data_entry["content"], emb_json_db, new_sticky_data_entry["author_id"]))
                    self.db_conn.commit()
                except sqlite3.Error as e_db_ins:
                    self.logger.error(f"DBへのスティッキー設定エラー (元ID: {original_message_id_to_operate_on}): {e_db_ins}")
                    await interaction.response.send_message("データベースエラーでスティッキー設定できなかった…ごめんね！", ephemeral=True)
                    self._del_sticky_from_memory_on_fail(gid, cid, original_message_id_to_operate_on)
                    return
                try:
                    bot_msg_sent: Optional[discord.Message] = None
                    raw_content_to_send = new_sticky_data_entry.get("content")
                    embed_to_send = new_sticky_data_entry.get("embed")
                    prepared_content_to_send = self._prepare_sticky_content(raw_content_to_send, original_message_id_to_operate_on)
                    if embed_to_send:
                        bot_msg_sent = await interaction.channel.send(content=prepared_content_to_send, embed=embed_to_send)
                    elif prepared_content_to_send and prepared_content_to_send.strip():
                        bot_msg_sent = await interaction.channel.send(content=prepared_content_to_send)
                    else:
                        self.logger.warning(f"スティッキーにする表示可能なテキスト内容または埋め込みがありませんでした。元ID: {original_message_id_to_operate_on}")
                        await interaction.response.send_message("このメッセージには、スティッキーとして表示できるテキストや埋め込みがないみたい。\n（例：画像やファイルのみのメッセージなど）", ephemeral=True)
                        self._del_sticky_from_db_and_memory_on_fail(gid, cid, original_message_id_to_operate_on)
                        return
                    if bot_msg_sent:
                        channel_stickies[original_message_id_to_operate_on]["sticky_bot_message_id"] = bot_msg_sent.id
                        cur = self.db_conn.cursor()
                        cur.execute("UPDATE sticky_messages SET sticky_bot_message_id = ? WHERE guild_id = ? AND channel_id = ? AND original_message_id = ?",
                                    (bot_msg_sent.id, gid, cid, original_message_id_to_operate_on))
                        self.db_conn.commit()
                        response_content_preview = message_to_get_content_from.content[:30] if message_to_get_content_from.content else '（画像・埋め込み）'
                        await interaction.response.send_message(f"メッセージID {original_message_id_to_operate_on}「{response_content_preview}...」を一番下に表示し続けるね！\n解除はもう一回このメッセージ（または私が送ったメッセージ）でコマンドを実行してね！", ephemeral=True)
                        self.logger.info(f"チャンネル {cid} にスティッキーメッセージを設定 (元ID: {original_message_id_to_operate_on}, ボットMSG ID: {bot_msg_sent.id})")
                except discord.HTTPException as e_http_send:
                    if e_http_send.code == 50035:
                         self.logger.error(f"スティッキーメッセージ初回投稿エラー (元ID: {original_message_id_to_operate_on}): 文字数制限エラー - {e_http_send.text}")
                         await interaction.response.send_message("メッセージが長すぎてスティッキーにできなかったみたい…ごめんね！もっと短いメッセージで試してみて！", ephemeral=True)
                    else:
                         self.logger.error(f"スティッキーメッセージ初回投稿中にHTTPエラー (元ID: {original_message_id_to_operate_on}): {e_http_send}", exc_info=True)
                         await interaction.response.send_message(f"スティッキー設定中にDiscord APIエラーが… ({e_http_send.status} {e_http_send.code})", ephemeral=True)
                    self._del_sticky_from_db_and_memory_on_fail(gid, cid, original_message_id_to_operate_on)
                except discord.Forbidden:
                    await interaction.response.send_message("ごめん！メッセージを投稿する権限がないからスティッキーにできなかった…", ephemeral=True)
                    self._del_sticky_from_db_and_memory_on_fail(gid, cid, original_message_id_to_operate_on)
                except Exception as e_send_sticky:
                    self.logger.error(f"スティッキーメッセージ初回投稿エラー (元ID: {original_message_id_to_operate_on}): {e_send_sticky}", exc_info=True)
                    await interaction.response.send_message(f"スティッキー設定中にエラーが… ({type(e_send_sticky).__name__})", ephemeral=True)
                    self._del_sticky_from_db_and_memory_on_fail(gid, cid, original_message_id_to_operate_on)

    def _del_sticky_from_memory_on_fail(self, guild_id: int, channel_id: int, original_message_id: int):
        if guild_id in self.sticky_messages_data and \
           channel_id in self.sticky_messages_data[guild_id] and \
           original_message_id in self.sticky_messages_data[guild_id][channel_id]:
            del self.sticky_messages_data[guild_id][channel_id][original_message_id]
            if not self.sticky_messages_data[guild_id][channel_id]:
                del self.sticky_messages_data[guild_id][channel_id]
            if not self.sticky_messages_data[guild_id]:
                del self.sticky_messages_data[guild_id]

    def _del_sticky_from_db_and_memory_on_fail(self, guild_id: int, channel_id: int, original_message_id: int):
        self._del_sticky_from_memory_on_fail(guild_id, channel_id, original_message_id)
        if self.db_conn:
            try:
                cur = self.db_conn.cursor()
                cur.execute("DELETE FROM sticky_messages WHERE guild_id = ? AND channel_id = ? AND original_message_id = ?",
                            (guild_id, channel_id, original_message_id))
                self.db_conn.commit()
                self.logger.info(f"設定失敗のため、DBからスティッキー情報 (元ID: {original_message_id}) を削除しました。")
            except sqlite3.Error as e_db_del_fail:
                self.logger.error(f"スティッキー設定失敗時のDB削除エラー (元ID: {original_message_id}): {e_db_del_fail}")

    async def timed_delete_message_callback(self, interaction: discord.Interaction, message: discord.Message):
        self.logger.info(f"{interaction.user.name} がメッセージID {message.id} の時間指定削除コマンドを使用 (ギルド {interaction.guild_id})")
        if not interaction.guild: await interaction.response.send_message("この機能はサーバー内でのみ利用できるよ！", ephemeral=True); return
        can_delete = False
        if message.author == self.bot.user or \
           interaction.user == message.author or \
           (interaction.guild and interaction.user.guild_permissions.manage_messages):
            can_delete = True
        if not can_delete:
            self.logger.warning(f"{interaction.user.name} にはメッセージ {message.id} を削除する権限がないみたい (ギルド {interaction.guild_id})")
            await interaction.response.send_message("ごめんね！そのメッセージを消すのは、メッセージを送った本人か、メッセージ管理権限を持ってる人だけなんだ…！", ephemeral=True); return
        view = DeleteTimerView(message, interaction.user)
        await interaction.response.send_message("いつ消すか選んでね！", view=view, ephemeral=True)
        view_timed_out = await view.wait()
        if view_timed_out and view.delete_delay_seconds is None:
            self.logger.info(f"メッセージ {message.id} の時間指定削除の選択がタイムアウトしちゃった。")
            try: await interaction.edit_original_response(content="時間指定削除の選択はタイムアウトしました。", view=None)
            except discord.NotFound: pass
            return
        if view.delete_delay_seconds is not None:
            self.logger.info(f"メッセージ {message.id} を {view.delete_delay_seconds} 秒後に削除するようスケジュールしたよ。")
            async def delayed_delete():
                await asyncio.sleep(view.delete_delay_seconds)
                try: await message.delete(); self.logger.info(f"メッセージ {message.id} をちゃんと削除したよ（時間指定）。")
                except discord.Forbidden: self.logger.warning(f"メッセージ {message.id} の時間指定削除に失敗: 権限がないみたい。")
                except discord.NotFound: self.logger.warning(f"メッセージ {message.id} が時間指定削除前に見つからなかったみたい。")
                except discord.HTTPException as e_http: self.logger.error(f"メッセージ {message.id} の時間指定削除中にHTTPエラー: {e_http}")
                except Exception as e_gen: self.logger.error(f"メッセージ {message.id} の時間指定削除中に予期せぬエラー: {e_gen}", exc_info=True)
            asyncio.create_task(delayed_delete())
        else:
            self.logger.info(f"メッセージ {message.id} の時間指定削除はキャンセルされたよ。")

    async def embed_message_callback(self, interaction: discord.Interaction, message: discord.Message):
        self.logger.info(f"{interaction.user.name} がメッセージID {message.id} の埋め込み変換コマンドを使用 (ギルド {interaction.guild_id})")
        if not interaction.guild: await interaction.response.send_message("この機能はサーバー内でのみ利用できるよ！", ephemeral=True); return
        try:
            await interaction.response.defer(ephemeral=False, thinking=True)
            embed = discord.Embed(description=message.content or "（本文なし）", color=discord.Color.pink(), timestamp=message.created_at)
            embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url if message.author.display_avatar else None)
            embed.add_field(name="元のメッセージへのリンク", value=f"[ここをクリック]({message.jump_url})", inline=False)
            await interaction.followup.send(embed=embed)
            self.logger.info(f"メッセージ {message.id} を埋め込みに変換して送信しました。")
        except Exception as e:
            self.logger.error(f"メッセージ {message.id} の埋め込み変換中にエラー: {e}", exc_info=True)
            try: await interaction.followup.send("埋め込み変換中にエラーが起きちゃった…ごめんね。", ephemeral=True)
            except discord.HTTPException: pass

    async def count_chars_message_callback(self, interaction: discord.Interaction, message: discord.Message):
        self.logger.info(f"{interaction.user.name} がメッセージID {message.id} の文字数カウントコマンドを使用 (ギルド {interaction.guild_id})")
        content_length = len(message.content)
        embed = discord.Embed(title="文字数カウントしたよ！", description=f"このメッセージは **{content_length}** 文字だったよ！", color=discord.Color.teal())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        self.logger.info(f"メッセージ {message.id} の文字数 ({content_length}文字) をカウントしました。")

async def setup(bot: commands.Bot):
    cog = ContextMenuCog(bot)
    await bot.add_cog(cog)
    logger.info("ContextMenuCog (sophia_context_menu_cog) が正常にロードされました。")
