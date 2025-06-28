import discord
from discord.ext import commands
from discord import app_commands
import os
from datetime import datetime
import asyncio
import logging
from collections import deque
from typing import Dict, List, Optional, Tuple, Any
import re
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from spotipy.cache_handler import MemoryCacheHandler
from concurrent.futures import ThreadPoolExecutor
import random

logger = logging.getLogger('SophiaBot.AudioCog')

class HelpView(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, pages: List[discord.Embed]):
        super().__init__(timeout=180)
        self.interaction = interaction
        self.pages = pages
        self.current_page = 0
        self.message: Optional[discord.WebhookMessage] = None
        self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.interaction.user.id:
            await interaction.response.send_message("このヘルプメニューはコマンドを実行したあなただけが操作できるよ！", ephemeral=True)
            return False
        return True

    def _update_buttons(self):
        if not self.children: return
        prev_button = self.children[0]
        page_indicator = self.children[1]
        next_button = self.children[2]
        if isinstance(prev_button, discord.ui.Button): prev_button.disabled = self.current_page == 0
        if isinstance(next_button, discord.ui.Button): next_button.disabled = self.current_page == len(self.pages) - 1
        if isinstance(page_indicator, discord.ui.Button): page_indicator.label = f"{self.current_page + 1}/{len(self.pages)}"

    async def show_page(self, interaction: discord.Interaction):
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    @discord.ui.button(label="◀ 前へ", style=discord.ButtonStyle.grey, custom_id="sophia_help_prev_v3")
    async def prev_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await self.show_page(interaction)
        else: await interaction.response.defer()

    @discord.ui.button(label="1/X", style=discord.ButtonStyle.secondary, disabled=True, custom_id="sophia_help_indicator_v3")
    async def page_indicator_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(label="次へ ▶", style=discord.ButtonStyle.grey, custom_id="sophia_help_next_v3")
    async def next_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            await self.show_page(interaction)
        else: await interaction.response.defer()

    async def on_timeout(self):
        if self.message:
            try:
                for item in self.children:
                    if isinstance(item, discord.ui.Button): item.disabled = True
                await self.message.edit(view=self)
            except discord.NotFound: pass
            except Exception as e: logger.error(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ヘルプタイムアウト時のボタン無効化エラー: {e}")

class AudioCog(commands.Cog, name="AudioCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # キューの型定義を変更: FFmpegPCMAudioオブジェクトの代わりにメタデータの辞書を格納
        # Dict[guild_id, deque[Dict[str, Any]]]
        # 各辞書のキー: 'title', 'duration', 'view_count', 'uploader', 'thumbnail', 'stream_url'
        self.audio_queues: Dict[int, deque[Dict[str, Any]]] = {}
        self.is_playing: Dict[int, bool] = {}
        self.is_looping_song: Dict[int, bool] = {}
        self.is_looping_queue: Dict[int, bool] = {}
        self.current_song_info: Dict[int, Optional[Tuple[str, int, Optional[int], Optional[str], Optional[str]]]] = {}
        self.current_song_title: Dict[int, Optional[str]] = {}
        self.current_audio_url: Dict[int, Optional[str]] = {}
        self.current_ffmpeg_source: Dict[int, Optional[discord.FFmpegPCMAudio]] = {}
        self.music_channels: Dict[int, discord.TextChannel] = {}
        self.executor = self.bot.executor # type: ignore

        self.ffmpeg_path = os.environ.get("FFMPEG_PATH")
        self.ffmpeg_options = {
            'executable': self.ffmpeg_path or 'ffmpeg',
            'options': '-vn -bufsize 1M',
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -timeout 1000000',
        }
        self.spotify_client_id = os.environ.get("SPOTIFY_CLIENT_ID")
        self.spotify_client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")

        if self.spotify_client_id and self.spotify_client_secret:
            try:
                self.sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
                    client_id=self.spotify_client_id,
                    client_secret=self.spotify_client_secret,
                    cache_handler=MemoryCacheHandler()
                ))
                logger.info("Spotify APIをAudioCogで初期化しました。")
            except Exception as e:
                logger.error(f"Spotify APIの初期化に失敗: {e}")
                self.sp = None
        else:
            logger.warning("SPOTIFY_CLIENT_IDまたはSPOTIFY_CLIENT_SECRETが設定されていません。Spotify機能は限定的になります。")
            self.sp = None

    async def _update_bot_presence(self):
        now_playing_song_title = None; active_guild_id_for_log = None
        for guild_id_loop, is_internal_playing_flag in self.is_playing.items():
            if is_internal_playing_flag:
                song_title = self.current_song_title.get(guild_id_loop)
                if song_title: now_playing_song_title = song_title; active_guild_id_for_log = guild_id_loop; break
        current_bot_activity = self.bot.activity; new_activity_name = ""
        new_activity_type: discord.ActivityType
        if now_playing_song_title: new_activity_name = now_playing_song_title; new_activity_type = discord.ActivityType.listening
        else: new_activity_name = "ClichéSystem_ver4.1.0_d6"; new_activity_type = discord.ActivityType.playing
        if not current_bot_activity or current_bot_activity.name != new_activity_name or current_bot_activity.type != new_activity_type:
            activity_to_set = discord.Activity(type=new_activity_type, name=new_activity_name)
            try:
                await self.bot.change_presence(activity=activity_to_set)
                if now_playing_song_title: logger.info(f"ボットプレゼンスを更新: ギルド {active_guild_id_for_log} の「{now_playing_song_title}」を再生中")
                else: logger.info(f"ボットプレゼンスを更新: デフォルト ({new_activity_name})")
            except Exception as e: logger.error(f"ボットプレゼンスの更新中にエラー: {e}")

    async def play_next(self, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            logger.error(f"ギルド {guild_id} が見つからないよ。再生処理を中止するね。"); await self._update_bot_presence(); return
        voice_client = guild.voice_client
        if not voice_client or not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
            logger.info(f"ギルド {guild_id} のボイスクライアントが無効か、接続してないみたい。再生処理を中止するね。")
            if guild_id in self.is_playing: self.is_playing[guild_id] = False
            self.current_song_info[guild_id] = None; self.current_song_title[guild_id] = None; self.current_ffmpeg_source[guild_id] = None; self.current_audio_url[guild_id] = None
            music_channel = self.music_channels.get(guild_id)
            if music_channel:
                embed = discord.Embed(title="あれれ？", description="ボイスチャンネルに接続していないみたいだよ！先に`/join`で呼んでね！", color=discord.Color.red())
                try: await music_channel.send(embed=embed)
                except discord.HTTPException as e: logger.error(f"エラーメッセージ送信失敗 (ギルド {guild_id}): {e}")
            await self._update_bot_presence(); return

        if voice_client.is_playing() and self.is_playing.get(guild_id, False):
            logger.info(f"ギルド {guild_id} で既に再生中みたい。play_nextの処理はスキップするね。"); return

        source_to_play: Optional[discord.FFmpegPCMAudio] = None
        title_to_play: Optional[str] = None
        song_metadata_to_play: Optional[Tuple[str, int, Optional[int], Optional[str], Optional[str]]] = None
        stream_url_to_play : Optional[str] = None

        if self.is_looping_song.get(guild_id, False) and self.current_audio_url.get(guild_id) and self.current_song_info.get(guild_id):
            stream_url_to_play = self.current_audio_url[guild_id]
            song_metadata_to_play = self.current_song_info[guild_id]
            title_to_play = self.current_song_title.get(guild_id)
            logger.info(f"ギルド {guild_id} で曲をループ再生: {title_to_play}")

        if not stream_url_to_play:
            if not self.audio_queues.get(guild_id):
                self.is_playing[guild_id] = False
                self.current_song_info[guild_id] = None
                self.current_song_title[guild_id] = None
                self.current_ffmpeg_source[guild_id] = None
                self.current_audio_url[guild_id] = None
                logger.info(f"ギルド {guild_id} のキューが空だよ。再生を停止するね。")
                music_channel = self.music_channels.get(guild_id)
                if music_channel:
                    embed = discord.Embed(title="再生終了♪", description="再生キューが空になっちゃった！また何かリクエストしてね！", color=discord.Color.blue())
                    try:
                        await music_channel.send(embed=embed)
                    except discord.HTTPException as e:
                        logger.error(f"再生終了メッセージ送信失敗 (ギルド {guild_id}): {e}")
                await self._update_bot_presence()
                return

            if self.is_looping_queue.get(guild_id, False) and self.current_song_info.get(guild_id):
                # 再生済みの曲情報をキューの最後尾に追加
                song_to_re_add = {
                    'title': self.current_song_title.get(guild_id, "不明な曲"),
                    'duration': self.current_song_info[guild_id][1],
                    'view_count': self.current_song_info[guild_id][2],
                    'uploader': self.current_song_info[guild_id][3],
                    'thumbnail': self.current_song_info[guild_id][4],
                    'stream_url': self.current_audio_url.get(guild_id)
                }
                if song_to_re_add['stream_url']:
                    self.audio_queues[guild_id].append(song_to_re_add)
                    logger.info(f"ギルド {guild_id} のキューループ: 「{song_to_re_add['title']}」をキューの最後に追加したよ！")

            # キューから次の曲の情報を取り出す
            next_song_data = self.audio_queues[guild_id].popleft()
            title_to_play = next_song_data.get('title', '不明なタイトル')
            stream_url_to_play = next_song_data.get('stream_url')
            song_metadata_to_play = (
                title_to_play,
                next_song_data.get('duration', 0),
                next_song_data.get('view_count'),
                next_song_data.get('uploader'),
                next_song_data.get('thumbnail')
            )
            logger.info(f"ギルド {guild_id} でキューから「{title_to_play}」を再生準備するね！")

        if not stream_url_to_play or not title_to_play or not song_metadata_to_play:
            logger.error(f"ギルド {guild_id} で再生するストリームURLかタ��トル、メタ情報が見つからなかったみたい…")
            self.is_playing[guild_id] = False
            await self._update_bot_presence()
            # エラーが発生したので、安全のため次の曲へ
            asyncio.run_coroutine_threadsafe(self.play_next_safe(guild_id), self.bot.loop)
            return

        # --- 再生直前にFFmpegオブジェクトを生成 ---
        try:
            source_to_play = discord.FFmpegPCMAudio(stream_url_to_play, **self.ffmpeg_options)
        except Exception as e_ffmpeg:
            logger.error(f"FFmpegPCMAudioの生成に失敗 ({title_to_play}): {e_ffmpeg}", exc_info=True)
            music_channel = self.music_channels.get(guild_id)
            if music_channel:
                embed = discord.Embed(title="再生エラー", description=f"ごめんね、「{title_to_play}」を再生する準備中にエラーが起きちゃったみたい…次の曲にいくね！", color=discord.Color.red())
                try: await music_channel.send(embed=embed)
                except discord.HTTPException: pass
            # エラーが発生したので、安全のため次の曲へ
            asyncio.run_coroutine_threadsafe(self.play_next_safe(guild_id), self.bot.loop)
            return

        # 現在再生中の情報を更新
        self.current_song_info[guild_id] = song_metadata_to_play
        self.current_song_title[guild_id] = title_to_play
        self.current_ffmpeg_source[guild_id] = source_to_play # ループ再生用に保持
        self.current_audio_url[guild_id] = stream_url_to_play

        self.is_playing[guild_id] = True
        try:
            def after_playing_callback(error):
                if error:
                    logger.error(f"ギルド {guild_id} で再生エラー (after_playing): {error}")
                self.is_playing[guild_id] = False
                # is_looping_songがFalseの時だけ再生情報をクリアする
                if not self.is_looping_song.get(guild_id, False):
                    self.current_song_title[guild_id] = None
                    self.current_song_info[guild_id] = None
                    self.current_ffmpeg_source[guild_id] = None
                    self.current_audio_url[guild_id] = None
                
                asyncio.run_coroutine_threadsafe(self._update_bot_presence(), self.bot.loop)
                asyncio.run_coroutine_threadsafe(self.play_next_safe(guild_id), self.bot.loop)

            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()
            
            voice_client.play(source_to_play, after=after_playing_callback)
            
            logger.info(f"ギルド {guild_id} で「{title_to_play}」の再生を開始したよ！")
            await self._update_bot_presence()

            _ts, _ds, _vcs, _us, _ths = song_metadata_to_play
            mins, secs = divmod(int(_ds), 60) if isinstance(_ds, (int, float)) and _ds > 0 else (0, 0)
            f_dur = f"{mins:02d}:{secs:02d}"
            f_vc = f"{int(_vcs):,}" if _vcs and isinstance(_vcs, (int, float)) else "たくさん！"
            udisp = _us if _us else '謎の投稿者さん'
            
            embed = discord.Embed(color=discord.Color.pink())
            embed.add_field(name="再生中だよ♪", value=f"今流れてるのはこれ！ **{title_to_play}**", inline=False)
            embed.add_field(name="曲の情報はこちら！", value=f"投稿者: {udisp}\n曲の長さ: {f_dur}\n再生回数: {f_vc}", inline=False)
            if _ths:
                embed.set_thumbnail(url=_ths)
            
            music_channel = self.music_channels.get(guild_id)
            if music_channel:
                try:
                    await music_channel.send(embed=embed)
                except discord.HTTPException as e:
                    logger.error(f"再生中メッセージ送信失敗 (ギルド {guild_id}): {e}")
        except Exception as e:
            self.is_playing[guild_id] = False
            logger.error(f"ギルド {guild_id} で致命的な再生エラー: {e}", exc_info=True)
            music_channel = self.music_channels.get(guild_id)
            if music_channel:
                embed = discord.Embed(title="大変！", description="ごめんなさい！再生中に予期せぬエラーが起きちゃった…もう一度試してくれると嬉しいな！", color=discord.Color.red())
                try:
                    await music_channel.send(embed=embed)
                except discord.HTTPException as he:
                    logger.error(f"エラーメッセージ送信失敗 (ギルド {guild_id}): {he}")
            await self._update_bot_presence()
            asyncio.run_coroutine_threadsafe(self.play_next_safe(guild_id), self.bot.loop)

    async def play_next_safe(self, guild_id: int):
        try:
            guild = self.bot.get_guild(guild_id); vc = guild.voice_client if guild else None
            if not guild or not vc or not isinstance(vc, discord.VoiceClient) or not vc.is_connected():
                logger.info(f"play_next_safe: ギルド {guild_id} のVCが無効か切断されてるみたい。再生情報をクリアするね。")
                if guild_id in self.is_playing: self.is_playing[guild_id] = False
                if guild_id in self.audio_queues: self.audio_queues[guild_id].clear()
                self.current_song_info[guild_id] = None; self.current_song_title[guild_id] = None; self.current_ffmpeg_source[guild_id] = None; self.current_audio_url[guild_id] = None
                await self._update_bot_presence(); return
            if not self.is_playing.get(guild_id, False) and not vc.is_playing():
                 await self.play_next(guild_id)
            else: logger.info(f"play_next_safe: ギルド {guild_id} でまだ再生中かフラグがTrueのため、play_nextの呼び出しをスキップするね。")
        except Exception as e:
            logger.error(f"play_next_safe (ギルド {guild_id}) で予期せぬエラー: {e}", exc_info=True)
            if guild_id in self.is_playing: self.is_playing[guild_id] = False; await self._update_bot_presence()

    async def load_audio_info(self, query_url: str, is_search: bool = False) -> Optional[Dict[str, Any]]:
        ydl_opts = {
            'format': 'bestaudio/best','noplaylist': True,'quiet': True,
            'default_search': 'ytsearch1' if is_search else None,
            'source_address': '0.0.0.0','skip_download': True,'retries': 3,'socket_timeout': 10,
            'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.45 Safari/537.36','Accept-Language': 'ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7',},
        }
        def extract_info_sync():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(query_url, download=False)
                    if is_search and 'entries' in info and info['entries']: return info['entries'][0]
                    return info
                except yt_dlp.utils.DownloadError as e:
                    logger.warning(f"yt-dlpダウンロードエラー ({query_url}): {e}")
                    if "confirm your age" in str(e).lower(): logger.warning(f"年齢制限の可能性がある動画みたいだよ: {query_url}")
                    return None
                except Exception as e: logger.error(f"yt-dlp汎用エラー ({query_url}): {e}", exc_info=False); return None
        try:
            loop = asyncio.get_running_loop(); info_dict = await loop.run_in_executor(self.executor, extract_info_sync)
            if not info_dict or 'url' not in info_dict:
                logger.error(f"情報取得失敗、または'url'キーなし: {query_url}。返却値: {info_dict}")
                return None
            return info_dict
        except Exception as e: logger.error(f"オーディオ情報読み込み処理エラー ({query_url}): {e}", exc_info=True); return None

    async def load_playlist_entries(self, playlist_url: str) -> List[Tuple[str, str, int, Optional[str], Optional[str]]]:
        ydl_opts = {
            'quiet': True, 'skip_download': True, 'extract_flat': 'in_playlist',
            'forcejson': True, 'retries': 2, 'socket_timeout': 10,
            'http_headers': {'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.45 Safari/537.36'},
        }
        processed_entries: List[Tuple[str, str, int, Optional[str], Optional[str]]] = []
        def extract_flat_playlist_info_sync():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    flat_info = ydl.extract_info(playlist_url, download=False)
                    return flat_info.get('entries', [])
                except Exception as e: logger.error(f"プレイリストのフラット情報取得エラー ({playlist_url}): {e}"); return []
        loop = asyncio.get_running_loop(); flat_entries = await loop.run_in_executor(self.executor, extract_flat_playlist_info_sync)
        if not flat_entries: logger.warning(f"プレイリストからエントリを取得できなかったみたい: {playlist_url}"); return []
        logger.info(f"プレイリスト {playlist_url} から {len(flat_entries)} 曲の基本情報を抽出。これから詳細を取得するね！")
        for entry_summary in flat_entries:
            if not entry_summary or not entry_summary.get('url'): logger.warning(f"プレイリスト内の無効なエントリをスキップするね: {entry_summary}"); continue
            video_url_or_query = entry_summary['url']
            title_guess = entry_summary.get('title', '不明なタイトル')
            duration_guess = entry_summary.get('duration', 0)
            uploader_guess = entry_summary.get('uploader')
            thumbnail_guess = entry_summary.get('thumbnail')
            processed_entries.append((video_url_or_query, title_guess, duration_guess, uploader_guess, thumbnail_guess))
        logger.info(f"プレイリスト {playlist_url} から {len(processed_entries)} 曲の基本情報をリストアップしたよ！")
        return processed_entries

    async def background_playlist_loader(self, guild_id: int, entries: List[Tuple[str, str, int, Optional[str], Optional[str]]], interaction: discord.Interaction):
        added_count = 0; failed_count = 0; music_channel = self.music_channels.get(guild_id)
        if not music_channel: logger.warning(f"ギルド {guild_id} の音楽チャンネルが見つからないから、バックグラウンドローダーを中止するね。"); return
        guild = self.bot.get_guild(guild_id); voice_client = guild.voice_client if guild else None
        for i, entry_tuple in enumerate(entries):
            video_url_query, title_g, duration_g, uploader_g, thumbnail_g = entry_tuple
            is_search_for_item = not str(video_url_query).startswith(('http:', 'https:'))
            detailed_info = await self.load_audio_info(str(video_url_query), is_search=is_search_for_item)
            if detailed_info and 'url' in detailed_info:
                # メタデータを辞書としてキューに追加
                song_data = {
                    'title': detailed_info.get('title', title_g),
                    'duration': detailed_info.get('duration', duration_g),
                    'view_count': detailed_info.get('view_count'),
                    'uploader': detailed_info.get('uploader', uploader_g),
                    'thumbnail': detailed_info.get('thumbnail', thumbnail_g),
                    'stream_url': detailed_info['url']
                }
                self.audio_queues[guild_id].append(song_data)
                added_count += 1
                
                embed = discord.Embed(color=discord.Color.light_grey())
                embed.add_field(name="こっそり追加中…", value=f"**{song_data['title']}** ({i+1}/{len(entries)})", inline=True)
                if song_data['thumbnail']:
                    embed.set_thumbnail(url=song_data['thumbnail'])
                
                try:
                    await music_channel.send(embed=embed, delete_after=10)
                except discord.HTTPException:
                    pass
                logger.info(f"ギルド {guild_id} にバックグラウンドで追加 ({i+1}/{len(entries)}): {song_data['title']}")
            else:
                logger.warning(f"プレイリスト内動画「{title_g}」({video_url_query})の詳細情報取得またはストリームURL取得に失敗しちゃった…")
                failed_count += 1
            
            # 再生中でなければ少し短く、再生中なら長めに待つ
            await asyncio.sleep(0.2 if not (voice_client and voice_client.is_playing()) else 0.8)

        final_message = f"プレイリストの曲、ぜーんぶチェック終わったよ！ {added_count} 曲をキューに入れたからね！"
        if failed_count > 0: final_message += f" ({failed_count} 曲は情報が足りなくてスキップしちゃった…ごめんね！)"
        embed_color = discord.Color.green() if added_count > 0 else discord.Color.orange();
        if added_count == 0 and failed_count > 0: embed_color = discord.Color.red()
        final_embed = discord.Embed(description=final_message, color=embed_color)
        try: await music_channel.send(embed=final_embed)
        except discord.HTTPException: pass
        logger.info(f"ギルド {guild_id} でプレイリストからのバックグラウンドキュー追加完了: 成功 {added_count} 曲、失敗 {failed_count} 曲。")

    async def _ensure_voice_connection(self, interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
        guild = interaction.guild
        if not guild: await interaction.followup.send("このコマンドはサーバー内でのみ使用できるよ！", ephemeral=True); return None
        voice_client = guild.voice_client
        if voice_client and isinstance(voice_client, discord.VoiceClient) and voice_client.is_connected():
            if guild.id not in self.music_channels and interaction.channel and isinstance(interaction.channel, discord.TextChannel): self.music_channels[guild.id] = interaction.channel
            return voice_client
        if not interaction.user.voice or not interaction.user.voice.channel: # type: ignore
            await interaction.followup.send(embed=discord.Embed(title="あれれ？", description="あなたがボイスチャンネルに参加していないみたい！まずは参加してからコマンドを使ってね！", color=discord.Color.red()), ephemeral=True); return None
        voice_channel = interaction.user.voice.channel # type: ignore
        try:
            vc = await voice_channel.connect(timeout=10.0, reconnect=True, self_deaf=True)
            logger.info(f"{voice_channel.name} に自動接続したよ (ギルド {guild.id})。")
            self.music_channels.setdefault(guild.id, interaction.channel); self.audio_queues.setdefault(guild.id, deque()); self.is_playing.setdefault(guild.id, False); self.is_looping_song.setdefault(guild.id, False); self.is_looping_queue.setdefault(guild_id, False) # type: ignore
            return vc
        except asyncio.TimeoutError: logger.error(f"ボイスチャンネル {voice_channel.name} への接続がタイムアウトしちゃった…"); await interaction.followup.send(embed=discord.Embed(title="接続失敗…", description="接続に時間がかかりすぎちゃったみたい…もう一度試してみてくれる？", color=discord.Color.red()), ephemeral=True); return None
        except Exception as e: logger.error(f"ボイスチャンネルへの自動接続に失敗 ({voice_channel.name}): {e}"); await interaction.followup.send(embed=discord.Embed(title="接続エラー！", description=f"ごめんなさい、ボイスチャンネルへの接続中にエラーが発生しました…。({e})", color=discord.Color.red()), ephemeral=True); return None

    @app_commands.command(name="join", description="ソフィアがボイスチャンネルに参加します！")
    async def join(self, interaction: discord.Interaction):
        logger.info(f"{interaction.user.name} が /join を使用 (ギルド {interaction.guild_id})")
        if not interaction.guild: await interaction.response.send_message("このコマンドはサーバー専用だよ！", ephemeral=True); return
        if not interaction.user.voice or not interaction.user.voice.channel: # type: ignore
            embed = discord.Embed(title="参加できません！", description="あなたがボイスチャンネルにいないと、私もどこへ行けばいいかわからないよ！", color=discord.Color.orange()); await interaction.response.send_message(embed=embed, ephemeral=True); return
        voice_channel = interaction.user.voice.channel; guild_id = interaction.guild_id; current_voice_client = interaction.guild.voice_client # type: ignore
        if current_voice_client and isinstance(current_voice_client, discord.VoiceClient):
            if current_voice_client.is_connected():
                if current_voice_client.channel == voice_channel:
                    embed = discord.Embed(title="もういるよ！", description=f"私はもう {voice_channel.name} にいるから大丈夫！いつでも呼んでね！", color=discord.Color.blue()); await interaction.response.send_message(embed=embed, ephemeral=True); return
                try: await current_voice_client.move_to(voice_channel); logger.info(f"ボイスチャンネル {voice_channel.name} に移動したよ (ギルド {guild_id})。")
                except asyncio.TimeoutError: logger.warning(f"ボイスチャンネル {voice_channel.name} への移動がタイムアウトしちゃった…"); await interaction.response.send_message("ごめんなさい、移動に時間がかかりすぎちゃったみたい…もう一度試してみて！", ephemeral=True); return
            else:
                try: await voice_channel.connect(timeout=10.0, reconnect=True, self_deaf=True); logger.info(f"ボイスチャンネル {voice_channel.name} に再接続したよ (ギルド {guild_id})。")
                except Exception as e: logger.error(f"ボイスチャンネルへの再接続エラー: {e}"); await interaction.response.send_message(f"再接続中にエラーが発生しました: {e}", ephemeral=True); return
        else:
            try: await voice_channel.connect(timeout=10.0, reconnect=True, self_deaf=True); logger.info(f"ボイスチャンネル {voice_channel.name} に接続したよ (ギルド {guild_id})。")
            except Exception as e: logger.error(f"ボイスチャンネル接続エラー: {e}"); await interaction.response.send_message(f"接続中にエラーが発生しました: {e}", ephemeral=True); return
        self.music_channels.setdefault(guild_id, interaction.channel); self.audio_queues.setdefault(guild_id, deque()); self.is_playing.setdefault(guild_id, False); self.is_looping_song.setdefault(guild_id, False); self.is_looping_queue.setdefault(guild_id, False); self.current_song_info.setdefault(guild_id, None); self.current_song_title.setdefault(guild_id, None); self.current_ffmpeg_source.setdefault(guild_id, None); self.current_audio_url.setdefault(guild_id, None) # type: ignore
        embed = discord.Embed(title="接続完了！", description=f"{voice_channel.name} に参加したよ！一緒に音楽を楽しもうね！", color=discord.Color.green()); await interaction.response.send_message(embed=embed)
        await self._update_bot_presence()

    @app_commands.command(name="leave", description="ソフィアがボイスチャンネルから退出します！")
    async def leave(self, interaction: discord.Interaction):
        logger.info(f"{interaction.user.name} が /leave を使用 (ギルド {interaction.guild_id})")
        guild = interaction.guild
        if not guild: await interaction.response.send_message("このコマンドはサーバー専用だよ！", ephemeral=True); return
        guild_id = guild.id; voice_client = guild.voice_client
        if voice_client and isinstance(voice_client, discord.VoiceClient) and voice_client.is_connected():
            try:
                if voice_client.is_playing() or voice_client.is_paused(): voice_client.stop()
                await voice_client.disconnect(force=False)
                if guild_id in self.audio_queues: self.audio_queues[guild_id].clear()
                self.is_playing[guild_id] = False; self.is_looping_song[guild_id] = False; self.is_looping_queue[guild_id] = False
                self.current_song_info[guild_id] = None; self.current_song_title[guild_id] = None; self.current_ffmpeg_source[guild_id] = None; self.current_audio_url[guild_id] = None
                if guild_id in self.music_channels: del self.music_channels[guild_id]
                embed = discord.Embed(title="またね！", description="ボイスチャンネルから退出したよ！また呼んでくれると嬉しいな♪", color=discord.Color.blue()); await interaction.response.send_message(embed=embed)
                logger.info(f"ギルド {guild_id} のボイスチャンネルから切断しました。")
            except Exception as e: logger.error(f"ボイスチャンネル切断エラー (ギルド {guild_id}): {e}"); embed = discord.Embed(title="エラー！", description=f"ごめんね、退出中にエラーが起きちゃった… ({e})", color=discord.Color.red()); await interaction.response.send_message(embed=embed, ephemeral=True)
            finally: await self._update_bot_presence()
        else: embed = discord.Embed(title="あれれ？", description="私、まだボイスチャンネルにいないみたいだよ！", color=discord.Color.orange()); await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="play", description="再生したい曲やプレイリストを教えてね！")
    @app_commands.describe(query="YouTubeかSpotifyのURL、または曲名やアーティスト名で検索してね！")
    async def play(self, interaction: discord.Interaction, query: str):
        logger.info(f"{interaction.user.name} が /play を使用 (クエリ: {query}, ギルド {interaction.guild_id})")
        await interaction.response.defer()
        if not interaction.guild_id or not interaction.guild: await interaction.followup.send("このコマンドはサーバーの中で使ってね！", ephemeral=True); return
        voice_client = await self._ensure_voice_connection(interaction)
        if not voice_client: return
        ffmpeg_exe = self.ffmpeg_options.get('executable', 'ffmpeg')
        if not self.ffmpeg_path or not os.path.exists(self.ffmpeg_path):
            if not any(os.access(os.path.join(path, ffmpeg_exe), os.X_OK) for path in os.environ.get("PATH", "").split(os.pathsep)):
                logger.error(f"FFmpegが見つからないみたい… FFMPEG_PATH: {self.ffmpeg_path} またはシステムPATHを確認してね。"); embed = discord.Embed(title="大変！", description="音楽再生に使う大事なプログラムが見つからないみたい…マスター、助けて～！", color=discord.Color.red()); await interaction.followup.send(embed=embed, ephemeral=True); return
        guild_id = interaction.guild_id; self.music_channels[guild_id] = interaction.channel # type: ignore
        is_spotify_playlist = self.sp and "spotify.com/playlist/" in query
        is_youtube_playlist = ("youtube.com/playlist?list=" in query or "youtu.be/playlist?list=" in query) and "watch?v=" not in query
        is_playlist_url = is_spotify_playlist or is_youtube_playlist
        is_spotify_track_url = self.sp and "spotify.com/track/" in query

        initial_message_sent = False
        if is_playlist_url:
            await interaction.followup.send(embed=discord.Embed(description=f"プレイリスト「{query}」から曲をキューに追加するね…どんな曲かな？ちょっと待ってて！", color=discord.Color.light_grey()))
            initial_message_sent = True
            playlist_meta_entries: List[Tuple[str, str, int, Optional[str], Optional[str]]] = [] # (query/url, title_g, dur_g, upl_g, thumb_g)
            if is_spotify_playlist:
                try:
                    playlist_id_match = re.search(r'playlist/([a-zA-Z0-9]+)', query); playlist_id = playlist_id_match.group(1) if playlist_id_match else query.split('/')[-1].split('?')[0]
                    results = self.sp.playlist_items(playlist_id, fields='items(track(name,artists(name),duration_ms,album(images)))') # type: ignore
                    for item in results['items']: # type: ignore
                        if item and item.get('track') and item['track'].get('name'):
                            track = item['track']; artist_names = ", ".join([a['name'] for a in track['artists']]); sqyt = f"{track['name']} {artist_names}"; thumb_url = track['album']['images'][0]['url'] if track.get('album') and track['album'].get('images') else None
                            playlist_meta_entries.append((sqyt, f"{track['name']} by {artist_names}", track.get('duration_ms', 0) // 1000, artist_names, thumb_url))
                    logger.info(f"Spotifyプレイリストから {len(playlist_meta_entries)} 曲の情報を取得したよ。")
                except Exception as e: logger.error(f"Spotifyプレイリスト処理エラー: {e}"); await interaction.edit_original_response(embed=discord.Embed(title="ごめんね…", description="Spotifyのプレイリストがうまく読み込めなかったみたい…", color=discord.Color.red())); return
            else: playlist_meta_entries = await self.load_playlist_entries(query) # YouTubeプレイリスト

            if not playlist_meta_entries:
                embed = discord.Embed(title="あれれ？", description="そのプレイリストに曲がなかったり、私が読めないみたい…", color=discord.Color.orange())
                await interaction.edit_original_response(embed=embed) if initial_message_sent else await interaction.followup.send(embed=embed, ephemeral=True)
                return

            first_entry_processed_successfully = False
            if playlist_meta_entries:
                first_query_url, first_title_g, first_dur_g, first_upl_g, first_thumb_g = playlist_meta_entries[0]
                is_first_search = not str(first_query_url).startswith(('http:', 'https:'))
                info = await self.load_audio_info(str(first_query_url), is_search=is_first_search)
                if info and 'url' in info:
                    stream_url = info['url']
                    title = info.get('title', first_title_g)
                    duration = info.get('duration', first_dur_g)
                    vc = info.get('view_count')
                    up = info.get('uploader', first_upl_g)
                    tn = info.get('thumbnail', first_thumb_g)
                    
                    song_data = {
                        'title': title, 'duration': duration, 'view_count': vc,
                        'uploader': up, 'thumbnail': tn, 'stream_url': stream_url
                    }
                    self.audio_queues[guild_id].append(song_data)
                    
                    embed = discord.Embed(color=discord.Color.pink())
                    embed.add_field(name="キューの先頭、いただきっ！", value=f"まずはこれだね！ **{title}**", inline=True)
                    if tn:
                        embed.set_thumbnail(url=tn)
                    await interaction.edit_original_response(embed=embed)
                    first_entry_processed_successfully = True
                    
                    if not self.is_playing.get(guild_id, False) and voice_client and not voice_client.is_playing():
                        asyncio.run_coroutine_threadsafe(self.play_next_safe(guild_id), self.bot.loop)
                else:
                    logger.warning(f"プレイリストの最初の曲「{first_title_g}」の詳細情報取得に失敗…")

            if not first_entry_processed_successfully and playlist_meta_entries:
                 await interaction.edit_original_response(embed=discord.Embed(title="ごめんね…", description="プレイリストの最初の曲、うまくキューに追加できなかったみたい…。", color=discord.Color.red())); return

            remaining_entries = playlist_meta_entries[1:]
            if remaining_entries: asyncio.create_task(self.background_playlist_loader(guild_id, remaining_entries, interaction))

        else: # Single track or search
            search_q_yt = query; title_h = "リクエストの曲♪"; dur_h = 0; thumb_h = None; upl_h = None
            if is_spotify_track_url:
                try:
                    track_id_match = re.search(r'track/([a-zA-Z0-9]+)', query); track_id = track_id_match.group(1) if track_id_match else query.split('/')[-1].split('?')[0]
                    track = self.sp.track(track_id) # type: ignore
                    if track and track['name']: an = ", ".join([a['name'] for a in track['artists']]); search_q_yt = f"{track['name']} {an}"; title_h = f"{track['name']} by {an}"; dur_h = track.get('duration_ms',0)//1000; # type: ignore
                    if track.get('album') and track['album'].get('images'): thumb_h = track['album']['images'][0]['url'] # type: ignore
                    upl_h = an; logger.info(f"Spotifyトラック情報を取得: {title_h}")
                except Exception as e: logger.warning(f"Spotifyトラック情報取得エラー: {e}。元のクエリ「{query}」で検索するね。"); search_q_yt = query

            is_direct_url_play = search_q_yt.startswith(('http:','https:')) and not is_spotify_track_url
            info = await self.load_audio_info(search_q_yt, is_search=not is_direct_url_play or is_spotify_track_url)

            if not info or 'url' not in info:
                embed = discord.Embed(title="うーん…", description="ごめんね、その曲見つけられなかったり、情報が取れなかったり…別のキーワードでお願い！", color=discord.Color.orange())
                await interaction.edit_original_response(embed=embed)
                return

            song_data = {
                'title': info.get('title', title_h),
                'duration': info.get('duration', dur_h),
                'view_count': info.get('view_count'),
                'uploader': info.get('uploader', upl_h),
                'thumbnail': info.get('thumbnail', thumb_h),
                'stream_url': info['url']
            }
            self.audio_queues[guild_id].append(song_data)
            
            embed = discord.Embed(color=discord.Color.pink())
            embed.add_field(name="おっけー！", value=f"**{song_data['title']}** をキューに入れたよ♪わくわく！", inline=True)
            if song_data['thumbnail']:
                embed.set_thumbnail(url=song_data['thumbnail'])
            
            await interaction.edit_original_response(embed=embed)
            logger.info(f"ギルド {guild_id} のキューに追加: {song_data['title']}")
            
            if not self.is_playing.get(guild_id, False) and voice_client and not voice_client.is_playing():
                asyncio.run_coroutine_threadsafe(self.play_next_safe(guild_id), self.bot.loop)

    @app_commands.command(name="interrupt", description="この曲、すぐに聴きたい！って時に使ってね！")
    @app_commands.describe(query="YouTubeかSpotifyのURL、または曲名で検索してキューの先頭に割り込ませるよ！")
    async def interrupt(self, interaction: discord.Interaction, query: str):
        logger.info(f"{interaction.user.name} が /interrupt を使用 (クエリ: {query}, ギルド {interaction.guild_id})")
        await interaction.response.defer()
        if not interaction.guild_id or not interaction.guild:
            await interaction.followup.send("このコマンドはサーバーの中で使ってね！", ephemeral=True)
            return
        voice_client = await self._ensure_voice_connection(interaction)
        if not voice_client:
            return
        guild_id = interaction.guild_id
        self.music_channels[guild_id] = interaction.channel # type: ignore

        # --- 割り込み曲の情報を先に取得 ---
        search_q_yt_int = query
        title_h_int = "割り込み曲♪"
        dur_h_int = 0
        thumb_h_int = None
        upl_h_int = None
        is_spotify_track_url_int = self.sp and "spotify.com/track/" in query
        if is_spotify_track_url_int:
            try:
                track_id_match = re.search(r'track/([a-zA-Z0-9]+)', query)
                track_id = track_id_match.group(1) if track_id_match else query.split('/')[-1].split('?')[0]
                track = self.sp.track(track_id) # type: ignore
                if track and track['name']:
                    an_int = ", ".join([a['name'] for a in track['artists']])
                    search_q_yt_int = f"{track['name']} {an_int}"
                    title_h_int = f"{track['name']} by {an_int}"
                    dur_h_int = track.get('duration_ms', 0) // 1000
                    if track.get('album') and track['album'].get('images'):
                        thumb_h_int = track['album']['images'][0]['url']
                    upl_h_int = an_int
            except Exception as e:
                logger.warning(f"Spotify割り込みトラック情報取得エラー: {e}")

        is_direct_url_int = search_q_yt_int.startswith(('http:', 'https:')) and not is_spotify_track_url_int
        info = await self.load_audio_info(search_q_yt_int, is_search=not is_direct_url_int or is_spotify_track_url_int)

        # --- 情報取得に失敗した場合は、再生を止めずにエラー通知 ---
        if not info or 'url' not in info:
            embed = discord.Embed(title="ごめんね…", description="割り込ませようとした曲、私見つけられなかった…他のキーワードで試してみて？", color=discord.Color.orange())
            await interaction.followup.send(embed=embed)
            return

        # --- 情報取得成功後、キューに追加して再生を停止 ---
        song_data = {
            'title': info.get('title', title_h_int),
            'duration': info.get('duration', dur_h_int),
            'view_count': info.get('view_count'),
            'uploader': info.get('uploader', upl_h_int),
            'thumbnail': info.get('thumbnail', thumb_h_int),
            'stream_url': info['url']
        }
        self.audio_queues[guild_id].appendleft(song_data)
        
        embed = discord.Embed(color=discord.Color.purple())
        embed.add_field(name="割り込みどうぞ！", value=f"**{song_data['title']}** をキューの先頭に入れたよ！次に再生しちゃうね！お楽しみに♪", inline=True)
        if song_data['thumbnail']:
            embed.set_thumbnail(url=song_data['thumbnail'])
        
        await interaction.followup.send(embed=embed)
        logger.info(f"ギルド {guild_id} でキューの先頭に割り込み: {song_data['title']}")
        
        # 再生中または一時停止中なら、現在の曲を停止して次の曲へ
        if voice_client.is_playing() or voice_client.is_paused():
            self.is_looping_song[guild_id] = False
            voice_client.stop()
        # 停止中なら、新しい再生を開始
        elif not self.is_playing.get(guild_id, False):
            asyncio.run_coroutine_threadsafe(self.play_next_safe(guild_id), self.bot.loop)


    @app_commands.command(name="play_random", description="指定したテキストチャットからランダムなURLを見つけて再生するよ！")
    @app_commands.describe(target_channel="どのテキストチャンネルから探す？")
    async def play_random(self, interaction: discord.Interaction, target_channel: discord.TextChannel):
        logger.info(f"{interaction.user.name} が /play_random を使用 (ターゲット: {target_channel.name}, ギルド {interaction.guild_id})")
        await interaction.response.defer()
        if not interaction.guild_id or not interaction.guild: await interaction.followup.send("このコマンドはサーバーの中で使ってね！", ephemeral=True); return
        voice_client = await self._ensure_voice_connection(interaction)
        if not voice_client: return
        guild_id = interaction.guild_id; self.music_channels[guild_id] = interaction.channel # type: ignore
        all_urls = []
        try:
            async for msg_hist_item in target_channel.history(limit=500): # type: ignore
                found_urls_in_msg = re.findall(r'https?://[^\s<>"\']+', msg_hist_item.content)
                if found_urls_in_msg: all_urls.extend(found_urls_in_msg)
        except discord.Forbidden: await interaction.followup.send(embed=discord.Embed(title="ごめんね…", description=f"{target_channel.mention} を私が見に行けなかったみたい…権限がないのかも？", color=discord.Color.red())); return
        except Exception as e: logger.error(f"チャット履歴からのURL取得中にエラー ({target_channel.name}): {e}"); await interaction.followup.send(embed=discord.Embed(title="大変！", description="チャットからURLを探してたらエラーになっちゃった…", color=discord.Color.red())); return
        if not all_urls: await interaction.followup.send(embed=discord.Embed(title="うーん…", description=f"{target_channel.mention} から再生できそうなURLが見つからなかったよ…", color=discord.Color.orange())); return

        random_url_picked = random.choice(all_urls)
        logger.info(f"ランダムに選択されたURL: {random_url_picked} (from {target_channel.name})")

        is_spotify_link_rand = self.sp and "spotify.com/" in random_url_picked
        search_q_yt_rand = random_url_picked; title_h_rand = "ランダムに見つけた曲"; dur_h_rand = 0; thumb_h_rand = None; upl_h_rand = None; is_search_needed_rand = True

        if is_spotify_link_rand and "track/" in random_url_picked:
            try:
                track_id_match = re.search(r'track/([a-zA-Z0-9]+)', random_url_picked); track_id = track_id_match.group(1) if track_id_match else random_url_picked.split('/')[-1].split('?')[0]
                track = self.sp.track(track_id) # type: ignore
                if track and track['name']: an_rand = ", ".join([a['name'] for a in track['artists']]); search_q_yt_rand = f"{track['name']} {an_rand}"; title_h_rand = f"{track['name']} by {an_rand}"; dur_h_rand = track.get('duration_ms',0)//1000; # type: ignore
                if track.get('album') and track['album'].get('images'): thumb_h_rand = track['album']['images'][0]['url'] # type: ignore
                upl_h_rand = an_rand; logger.info(f"ランダム再生: Spotifyトラック「{title_h_rand}」をYouTubeで「{search_q_yt_rand}」検索。")
            except Exception as e: logger.warning(f"ランダム再生: Spotifyトラック情報取得エラー ({random_url_picked}): {e}")
        else: is_search_needed_rand = not random_url_picked.startswith(('http:','https:')) or is_spotify_link_rand # Spotifyプレイリスト等も検索が必要

        info = await self.load_audio_info(search_q_yt_rand, is_search=is_search_needed_rand)

        if not info or 'url' not in info:
            embed = discord.Embed(title="うーん…", description=f"ごめんね、見つけたURL ({random_url_picked}) から曲の情報が取れなかったみたい…", color=discord.Color.orange())
            await interaction.followup.send(embed=embed)
            return

        song_data = {
            'title': info.get('title', title_h_rand),
            'duration': info.get('duration', dur_h_rand),
            'view_count': info.get('view_count'),
            'uploader': info.get('uploader', upl_h_rand),
            'thumbnail': info.get('thumbnail', thumb_h_rand),
            'stream_url': info['url']
        }
        self.audio_queues[guild_id].append(song_data)
        
        embed = discord.Embed(color=discord.Color.pink())
        embed.add_field(name="チャットから発掘！", value=f"**{song_data['title']}** をキューに入れたよ！({target_channel.mention} で見つけたんだ♪)", inline=True)
        if song_data['thumbnail']:
            embed.set_thumbnail(url=song_data['thumbnail'])
        
        await interaction.followup.send(embed=embed)
        logger.info(f"ギルド {guild_id} のキューに追加 (ランダムチャットより): {song_data['title']}")
        
        if not self.is_playing.get(guild_id, False) and voice_client and not voice_client.is_playing():
            asyncio.run_coroutine_threadsafe(self.play_next_safe(guild_id), self.bot.loop)


    @app_commands.command(name="skip", description="今の曲はここまで！次の曲にいっちゃお！")
    async def skip(self, interaction: discord.Interaction):
        logger.info(f"{interaction.user.name} が /skip を使用 (ギルド {interaction.guild_id})")
        guild = interaction.guild
        if not guild: await interaction.response.send_message("サーバーの中で使ってほしいな！", ephemeral=True); return
        guild_id = guild.id; voice_client = guild.voice_client
        if voice_client and isinstance(voice_client, discord.VoiceClient) and (voice_client.is_playing() or voice_client.is_paused()):
            current_title = self.current_song_title.get(guild_id, "今の曲"); self.is_looping_song[guild_id] = False; voice_client.stop()
            embed = discord.Embed(color=discord.Color.orange()); embed.add_field(name="次いくよー！", value=f"**{current_title}** はバイバイして、次の曲へGO！", inline=True)
            await interaction.response.send_message(embed=embed); logger.info(f"ギルド {guild_id} で曲「{current_title}」をスキップしました。")
        else: embed = discord.Embed(title="あれれ？", description="飛ばす曲がないみたい？まずは何か再生してみてね！", color=discord.Color.orange()); await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="stop", description="再生を止めて、キューも空っぽにするよ！")
    async def stop(self, interaction: discord.Interaction):
        logger.info(f"{interaction.user.name} が /stop を使用 (ギルド {interaction.guild_id})")
        guild = interaction.guild
        if not guild: await interaction.response.send_message("サーバーの中でお願い！", ephemeral=True); return
        guild_id = guild.id; voice_client = guild.voice_client; queue_size = 0
        if guild_id in self.audio_queues: queue_size = len(self.audio_queues[guild_id]); self.audio_queues[guild_id].clear()
        self.is_looping_song[guild_id] = False; self.is_looping_queue[guild_id] = False; self.is_playing[guild_id] = False
        self.current_song_info[guild_id] = None; self.current_song_title[guild_id] = None; self.current_ffmpeg_source[guild_id] = None; self.current_audio_url[guild_id] = None
        embed_desc = "";
        if voice_client and isinstance(voice_client, discord.VoiceClient) and (voice_client.is_playing() or voice_client.is_paused()): voice_client.stop(); embed_desc = f"再生ストーップ！キューも空っぽにしちゃった！({queue_size}曲バイバイしたよん♪) またね！"
        else: embed_desc = f"何も曲は入ってなかったけど、キューは綺麗にしといたよ！({queue_size}曲お片付け♪) いつでも呼んでね！"
        logger.info(f"ギルド {guild_id} で再生を停止し、キューをクリアしました。"); embed = discord.Embed(title="ぴたっ！", description=embed_desc, color=discord.Color.red()); await interaction.response.send_message(embed=embed)
        await self._update_bot_presence()

    @app_commands.command(name="pause", description="ちょっと待った！今の曲を一時停止するね")
    async def pause(self, interaction: discord.Interaction):
        logger.info(f"{interaction.user.name} が /pause を使用 (ギルド {interaction.guild_id})")
        if not interaction.guild_id or not interaction.guild:
            await interaction.response.send_message("サーバーの中で使ってね！", ephemeral=True)
            return
        voice_client = interaction.guild.voice_client # type: ignore
        if voice_client and isinstance(voice_client, discord.VoiceClient) and voice_client.is_playing():
            voice_client.pause()
            logger.info(f"ギルド {interaction.guild_id} で再生を一時停止しました。")
            embed = discord.Embed(title="ちょっと待ったー！", description="再生を一時停止したよ！あなたが `/resume` って言ってくれたら、また続きから再生するね♪", color=discord.Color.blue())
            await interaction.response.send_message(embed=embed)
        elif voice_client and voice_client.is_paused():
            await interaction.response.send_message("もう止まってるよぉ～！続き聴きたくなったら `/resume` だからね！", ephemeral=True)
        else:
            embed = discord.Embed(title="んん？", description="止める曲がないみたい？まずは何か再生してみてほしいな！", color=discord.Color.orange())
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="resume", description="おまたせ！一時停止してた曲を続きから再生するよ！")
    async def resume(self, interaction: discord.Interaction):
        logger.info(f"{interaction.user.name} が /resume を使用 (ギルド {interaction.guild_id})")
        if not interaction.guild_id or not interaction.guild: await interaction.response.send_message("サーバーの中でお願いね！", ephemeral=True); return
        voice_client = interaction.guild.voice_client; guild_id = interaction.guild_id # type: ignore
        if voice_client and isinstance(voice_client, discord.VoiceClient) and voice_client.is_paused():
            voice_client.resume()
            logger.info(f"ギルド {guild_id} で再生を再開しました。")
            embed = discord.Embed(title="おまたせ！", description="さっきの続きから再生するね！ノリノリで行くよー！", color=discord.Color.green()); await interaction.response.send_message(embed=embed)
        elif voice_client and voice_client.is_connected() and not voice_client.is_playing() and not self.is_playing.get(guild_id, False):
            if self.audio_queues.get(guild_id) or self.current_ffmpeg_source.get(guild_id):
                logger.info(f"ギルド {guild_id} で再生を再開（play_nextトリガー）。"); asyncio.run_coroutine_threadsafe(self.play_next_safe(guild_id), self.bot.loop)
                await interaction.response.send_message(embed=discord.Embed(title="よーし、再生するぞー！", description="キューの曲、順番に再生しちゃうね！", color=discord.Color.green()))
            else: await interaction.response.send_message("あれ？止まってる曲も、キューにも何もないみたい…まずは `/play` で何かリクエストしてね！", ephemeral=True)
        elif voice_client and voice_client.is_playing(): await interaction.response.send_message("もうノリノリで再生してるよー！このまま楽しんでね！", ephemeral=True)
        else: embed = discord.Embed(title="あれれ？", description="ボイスチャンネルにいないか、再開できる曲がないみたいだよ！", color=discord.Color.orange()); await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="loop", description="今の曲を無限ループ！もう一回で解除だよ！")
    async def loop(self, interaction: discord.Interaction):
        logger.info(f"{interaction.user.name} が /loop を使用 (ギルド {interaction.guild_id})")
        if not interaction.guild_id or not interaction.guild: await interaction.response.send_message("サーバーの中で使ってね！", ephemeral=True); return
        voice_client = interaction.guild.voice_client; guild_id = interaction.guild_id # type: ignore
        if voice_client and isinstance(voice_client, discord.VoiceClient) and voice_client.is_connected():
            is_song_loaded = (self.is_playing.get(guild_id, False) or (voice_client and voice_client.is_paused())) and self.current_song_title.get(guild_id)
            if not is_song_loaded: embed = discord.Embed(title="あれれ？", description="ループする曲がないみたい。まずは何か再生して、私に教えてね！", color=discord.Color.orange()); await interaction.response.send_message(embed=embed, ephemeral=True); return
            self.is_looping_song[guild_id] = not self.is_looping_song.get(guild_id, False); current_title = self.current_song_title.get(guild_id, "今の曲")
            if self.is_looping_song[guild_id]: self.is_looping_queue[guild_id] = False; embed = discord.Embed(title="無限ループ突入！", description=f"**{current_title}** をずーっとエンドレスリピートしちゃうよ！止めたくなったらもう一回 `/loop` って言ってね♪ (キュー全体のループはオフになったよ！)", color=discord.Color.pink())
            else: embed = discord.Embed(title="ループ解除！", description=f"**{current_title}** のエンドレスからは抜け出したよ！次からは普通に再生するね！", color=discord.Color.blue())
            await interaction.response.send_message(embed=embed); logger.info(f"ギルド {guild_id} で「{current_title}」の曲ループ状態を {self.is_looping_song[guild_id]} に設定しました。キューループ: {self.is_looping_queue.get(guild_id, False)}")
        else: embed = discord.Embed(title="あれれ？", description="ボイスチャンネルにいないみたい！/joinで私を呼んでね！", color=discord.Color.orange()); await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="loop_queue", description="キュー全体をループするよ！もう一回で解除！")
    async def loop_queue(self, interaction: discord.Interaction):
        logger.info(f"{interaction.user.name} が /loop_queue を使用 (ギルド {interaction.guild_id})")
        if not interaction.guild_id or not interaction.guild: await interaction.response.send_message("サーバーの中で使ってね！", ephemeral=True); return
        guild_id = interaction.guild_id; voice_client = interaction.guild.voice_client # type: ignore
        if not (voice_client and isinstance(voice_client, discord.VoiceClient) and voice_client.is_connected()):
            embed = discord.Embed(title="あれれ？", description="ボイスチャンネルにいないみたい！/joinで私を呼んでね！", color=discord.Color.orange()); await interaction.response.send_message(embed=embed, ephemeral=True); return
        if not self.audio_queues.get(guild_id) and not self.current_song_info.get(guild_id):
            embed = discord.Embed(title="あれれ？", description="ループする曲がキューにないみたいだよ。まずは何か再生してね！", color=discord.Color.orange()); await interaction.response.send_message(embed=embed, ephemeral=True); return
        self.is_looping_queue[guild_id] = not self.is_looping_queue.get(guild_id, False)
        if self.is_looping_queue[guild_id]: self.is_looping_song[guild_id] = False; embed = discord.Embed(title="キュー全体ループだー！", description="再生待ちの曲をぜーんぶ、ずーっと繰り返し再生するよ！解除したくなったら、もう一回 `/loop_queue` って言ってね♪ (曲ごとのループはオフになったよ！)", color=discord.Color.pink())
        else: embed = discord.Embed(title="キュー全体ループ解除！", description="キュー全体の繰り返し再生はやめたよ！キューの曲が終わったら、普通に止まるからね！", color=discord.Color.blue())
        await interaction.response.send_message(embed=embed); logger.info(f"ギルド {guild_id} のキューループ状態を {self.is_looping_queue[guild_id]} に設定しました。曲ループ: {self.is_looping_song.get(guild_id, False)}")

    @app_commands.command(name="clear", description="再生待ちはリセット！今の曲はそのまま流れるよ")
    async def clear(self, interaction: discord.Interaction):
        logger.info(f"{interaction.user.name} が /clear を使用 (ギルド {interaction.guild_id})")
        if not interaction.guild_id:
            await interaction.response.send_message("サーバーでお願いね！", ephemeral=True)
            return
        guild_id = interaction.guild_id
        queue_size = 0
        if guild_id in self.audio_queues:
            queue_size = len(self.audio_queues[guild_id])
            self.audio_queues[guild_id].clear()
        logger.info(f"ギルド {guild_id} のキューをクリアしました。{queue_size}曲を削除。")
        embed = discord.Embed(title="お掃除完了！", description=f"再生待ちキュー、空っぽにしといたよ！({queue_size}曲バイバイ♪)\n(今流れてる曲はそのまま続くから安心してね！)", color=discord.Color.blue())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="queue", description="次に再生される曲のキュー、見せてあげる！")
    async def queue(self, interaction: discord.Interaction):
        logger.info(f"{interaction.user.name} が /queue を使用 (ギルド {interaction.guild_id})")
        if not interaction.guild_id or not interaction.guild: await interaction.response.send_message("サーバーの中で使ってね！", ephemeral=True); return
        guild_id = interaction.guild_id; guild_name = interaction.guild.name
        embed = discord.Embed(title=f"{guild_name}の再生待ちキューだよっ♪", color=discord.Color.pink())
        if interaction.user.display_avatar: embed.set_author(name=f"見てるのは {interaction.user.display_name} だね！", icon_url=interaction.user.display_avatar.url) # type: ignore
        else: embed.set_author(name=f"見てるのは {interaction.user.display_name} だね！") # type: ignore
        current_title_q = self.current_song_title.get(guild_id); vc_q = interaction.guild.voice_client # type: ignore
        is_currently_playing_or_paused_q = (self.is_playing.get(guild_id, False) or (vc_q and vc_q.is_paused()))
        loop_status_str = ""
        if self.is_looping_song.get(guild_id, False): loop_status_str = " (この曲ずーっとリピート中！)"
        elif self.is_looping_queue.get(guild_id, False): loop_status_str = " (キュー全体をリピート中！)"
        if is_currently_playing_or_paused_q and current_title_q: embed.add_field(name="▶️ 今流れてるのはコレ！", value=f"**{current_title_q}**{loop_status_str}", inline=False)
        else: embed.add_field(name="▶️ 今流れてるのはコレ！", value=f"今は曲が入ってないみたい…{loop_status_str}", inline=False)
        queue_list_str = ""
        if guild_id in self.audio_queues and self.audio_queues[guild_id]:
            for i, song_data in enumerate(list(self.audio_queues[guild_id])[:10]):
                title_in_queue = song_data.get('title', '不明な曲')
                duration_in_queue = song_data.get('duration', 0)
                
                if duration_in_queue and isinstance(duration_in_queue, (int, float)) and duration_in_queue > 0:
                    mins, secs = divmod(int(duration_in_queue), 60)
                    f_dur = f"{mins:02d}:{secs:02d}"
                    queue_list_str += f"{i+1}. **{title_in_queue}** ({f_dur})\n"
                else:
                    queue_list_str += f"{i+1}. **{title_in_queue}** (時間…ナイショ♪)\n"
            
            if len(self.audio_queues[guild_id]) > 10:
                queue_list_str += f"...あと{len(self.audio_queues[guild_id]) - 10}曲も待ってるよ！わくわく！"
        else:
            queue_list_str = "曲が入ってないみたい…さみしいな"
        embed.add_field(name="次の曲は…？", value=queue_list_str if queue_list_str else "曲が入ってないみたい…さみしいな", inline=False)
        embed.set_footer(text=f"ぜーんぶで {len(self.audio_queues.get(guild_id, []))} 曲がキューで待機ちゅう！"); await interaction.response.send_message(embed=embed)

    @app_commands.command(name="help", description="ソフィアの使い方、教えてあげるね！")
    async def help(self, interaction: discord.Interaction):
        logger.info(f"{interaction.user.name} が /help を使用 (ギルド {interaction.guild_id})")
        bot_name = self.bot.user.name if self.bot.user else "ソフィア" # type: ignore

        rpg_spec_description = (
            "RPG機能の細かいルールとか仕様だよ！\n\n"
            "**レベルアップとアイテムドロップ**\n"
            "- サーバーでメッセージを送信すると経験値が溜まってレベルアップ！\n"
            "- レベルアップすると、新しい装備アイテムが手に入ることがあるよ。\n"
            "- アイテムには「武器」と「防具」があって、「ベース」と「効果」にそれぞれレアリティがあるんだ。\n"
            "  組み合わせで強い効果のアイテムをゲットしよう！(最レアは0.0001%)\n"
            "- インベントリは上限があるから気をつけてね！\n"
            "  いっぱいだと新しいアイテムをどうするか聞かれるよ。\n\n"
            "**ステータス**\n"
            "- **HP（体力）**: レベルに応じて増えるよ。(レベル + 10)\n"
            "- **ATK (攻撃力)**: 装備してる武器と防具のATK合計値だよ。\n"
            "- **DEF (防御力)**: 装備してる武器と防具のDEF合計値だよ。\n\n"
            "**戦闘**\n"
            "- `/vbattle`で始まるよ！敵の強さは色々！\n"
            "- **攻撃**: あなたのATKから敵のDEFを引いた分がダメージ！(被ダメも同様)\n"
            "- **防御**: あなたのDEFの1割ぶん、HPが回復するよ！\n"
            "- **逃走**: 戦いから逃げることもできるけど、何も手に入らないよ。\n"
            "- 敵を倒すとゴールドが手に入ることがあるよ！\n\n"
            "**確率と売値**\n"
            "- common:      33.9% / 500G\n"
            "- uncommon:  30%   / 1000G\n"
            "- rare:               20%   / 2500G\n"
            "- epic:               10%   / 5000G\n"
            "- legendary:     5%    / 10000G\n"
            "- mythic:           1%    / 15000G\n"
            "- unique:         0.1%  / 50000G\n"
        )
        
        pages_content = [
            {"title": f"{bot_name}のヘルプ♪ - 音楽コマンドの使い方！ (1/5)", "description": (
                "私と一緒に音楽を楽しもっ！コマンドはこんな感じだよ！\n```\n"
                "/join          - 私がお部屋にお邪魔するね！\n"
                "/leave         - 私がお部屋からバイバイするよ\n"
                "/play          - この曲再生して！って私にお願いできるよ\n"
                "　　　　　　　　   (URL、曲名・プレイリスト名でもOK！)\n"
                "/play_random   - 指定チャットからランダムにURL再生！\n"
                "/interrupt     - 今すぐ聴きたい曲をキューに割り込ませるよ！\n"
                "/skip          - 今の曲はもういいかな？次の曲に飛ぶよ！\n"
                "/stop          - 再生を止めて、キューもぜーんぶおしまい！\n"
                "/clear         - キューだけを空っぽにするね！\n"
                "/pause         - ちょっと待った！今の曲を一時停止するよ\n"
                "/resume        - さっき止めた曲の続きから、また再生するね！\n"
                "/loop          - ずーっと聴いてたい！曲を無限ループ♪\n"
                "/loop_queue    - キュー全体を無限ループしちゃう！\n"
                "/queue         - 次は何の曲かな？キューを見せてあげる！\n"
                "```"
            )},
            {"title": f"{bot_name}のヘルプ♪ - RPGコマンドとか！ (2/5)", "description": (
                "簡単なRPG機能のコマンドだよ！\n"
                "```\n"
                "/vlevel       - レベルと所持ゴールドを表示！\n"
                "/vinventory   - インベントリを表示！\n"
                "/vequip <ID>  - 指定したIDのアイテムを装備！\n"
                "             　(インベントリでIDを確認してね！)\n"
                "/vstats       - 装備とステータス（ATK/DEF）を表示！\n"
                "/vreroll <ID> - 指定したIDのアイテムの効果を再抽選！\n"
                "             　(同じレア度の装備が他に5個必要だよ！)\n"
                "/vsell <ID>   - 指定したIDのアイテムを売却！\n"
                "             　(複数売却はIDをスペースで区切ってね！)\n"
                "/vbattle      - ランダムな敵とバトル！\n"
                "/vgacha       - ガチャを引くよ！\n"
                "```"
            )},
            {"title": f"{bot_name}のヘルプ♪ - RPGの仕様 (3/5)",
             "description": rpg_spec_description
            },
            {"title": f"{bot_name}のヘルプ♪ - 環境確認コマンド (4/5)", "description": (
                "お部屋の今の状態が気になる？そんな時はこのコマンド！\n"
                "```\n"
                "/hv all       - 温度、湿度、CO2濃度など全部まとめて表示！\n"
                "```\n"
                "自動監視もしてるから、お部屋の環境が悪くなったら私が教えてあげるね！"
            )},
            {"title": f"{bot_name}のヘルプ♪ - 他のコマンドとか色々！ (5/5)", "description": (
                f"{bot_name}とのおしゃべりとか、他のことも教えちゃうね！\n\n"
                "**私とおしゃべり**\n"
                f"- 私にメンション（`@{bot_name}` ってやつね！）して話しかけると、お返事するよ！\n"
                "- 画像と一緒だと、画像についても何か言うかも？試してみてね！\n\n"
                "**便利なコマンド**\n"
                "```\n"
                "/switch_model  - AIの思考モデルを切り替えるよ！\n"
                "```\n"
                "**メッセージを右クリックでできること (コンテキストメニュー)**\n"
                "- `このメッセージをAIで要約して`: 長いメッセージを私が代わりに読んで要約してあげる！\n"
                "- `このメッセージを一番下に表示し続ける`: 大事なメッセージをチャンネルの一番下にずっと表示しておくよ！\n"
                "- `このメッセージを後で消すね`: メッセージを6時間、12時間、24時間後に自動で消せるよ！\n"
                "- `メッセージを埋め込みにする`: 普通のメッセージを埋め込みみたいにするよ！\n"
                "- `メッセージの文字数を数える`: そのメッセージが何文字か数えてあげる！\n\n"
                "**特別なコマンド (開発者専用)**\n"
                "```\n"
                f"/restart4      - {bot_name}を再起動する秘密の呪文！\n"
                f"/vreset_rpg    - RPGのデータを全部リセットしちゃうよ！\n"
                "/h ...         - 家電を操作する秘密のコマンド！\n"
                "```\n\n"
                f"**こまったときは？**\n- 変なことがあったり、「こうだったらもっとイイナ！」って思ったら、\n  マスター(@0rnot)にこっそり教えてあげてね！お願い！\n"
            )}
        ]
        pages: List[discord.Embed] = []
        for page_content in pages_content:
            embed = discord.Embed(title=page_content["title"], description=page_content["description"], color=discord.Color.pink())
            embed.set_footer(text=f"{bot_name}の取扱説明書♪")
            pages.append(embed)
        view = HelpView(interaction, pages)
        await interaction.response.send_message(embed=pages[0], view=view)
        try: view.message = await interaction.original_response()
        except discord.NotFound: logger.warning("ヘルプメッセージ送信直後にoriginal_responseが見つかりませんでした。")
        logger.info(f"{interaction.user.name} にページ付きヘルプメッセージを送信しました。")

# ### 👇 ここから修正 👇 ###
# ファイルの末尾に setup 関数を追加
async def setup(bot: commands.Bot):
    await bot.add_cog(AudioCog(bot))
    logger.info("AudioCog (sophia_audio_cog) が正常にロードされました。")
# ### 👆 ここまで修正 👆 ###
