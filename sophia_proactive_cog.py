import discord
from discord.ext import commands, tasks
import asyncio
import random
from datetime import datetime, time, timedelta
import logging
from typing import List, Optional
import config  # config.pyをインポート

logger = logging.getLogger('SophiaBot.ProactiveCog')

class ProactiveCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.daily_post_times: List[time] = []
        self.last_checked_day: Optional[int] = None
        self.proactive_chat_task.start()
        logger.info("自発会話Cogが初期化されました。")

    def cog_unload(self):
        self.proactive_chat_task.cancel()
        logger.info("自発会話Cogのタスクがキャンセルされました。")

    def _schedule_daily_posts(self):
        """1日のランダムな投稿時刻を5つスケジュールする"""
        today = datetime.now().date()
        # 9時から26時（翌2時）の間でランダムな時刻を5つ生成
        self.daily_post_times = [
            (datetime.combine(today, time(9, 0)) + timedelta(seconds=random.randint(0, 17 * 3600))).time()
            for _ in range(5)
        ]
        self.daily_post_times.sort()
        self.last_checked_day = today.day
        logger.info(f"本日の自発会話の時刻をスケジュールしました: {[t.strftime('%H:%M:%S') for t in self.daily_post_times]}")

    async def _find_most_active_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """サーバーで最もアクティブなテキストチャンネルを見つける"""
        most_active_channel = None
        max_messages = -1
        # 検索範囲を過去7日間に広げ、より安定したアクティブチャンネルを見つける
        after_time = datetime.utcnow() - timedelta(days=7)

        text_channels = [c for c in guild.text_channels if c.permissions_for(guild.me).read_message_history and c.permissions_for(guild.me).send_messages]

        # チャンネルの履歴を並行して取得
        histories = await asyncio.gather(
            *[c.history(limit=200, after=after_time).flatten() for c in text_channels],
            return_exceptions=True
        )

        for i, channel_history in enumerate(histories):
            if isinstance(channel_history, Exception):
                logger.warning(f"チャンネル {text_channels[i].name} の履歴読み取りに失敗しました: {channel_history}")
                continue

            message_count = len(channel_history)
            if message_count > max_messages:
                max_messages = message_count
                most_active_channel = text_channels[i]
        
        if most_active_channel:
            logger.info(f"最もアクティブなチャンネル: {most_active_channel.name} (直近7dのメッセージ数(上限200): {max_messages})")
        else:
            # アクティブなチャンネルが見つからない場合、フォールバック
            most_active_channel = guild.system_channel or next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
            logger.warning("アクティブなチャンネルが見つからなかったため、フォールバックチャンネルを使用します。")

        return most_active_channel

    @tasks.loop(minutes=1)
    async def proactive_chat_task(self):
        await self.bot.wait_until_ready()

        now = datetime.now()
        current_time = now.time()

        if self.last_checked_day != now.day:
            self._schedule_daily_posts()

        if not self.daily_post_times or current_time < self.daily_post_times[0]:
            return

        scheduled_time = self.daily_post_times.pop(0)
        logger.info(f"スケジュール時刻 {scheduled_time.strftime('%H:%M:%S')} を過ぎたため、自発会話を試みます。")
        
        try:
            # config.pyからプロンプトを取得
            topic_prompt = config.PROACTIVE_PROMPT_TEMPLATE
            
            for guild in self.bot.guilds:
                target_channel = await self._find_most_active_channel(guild)
                if target_channel:
                    logger.info(f"ギルド '{guild.name}' のチャンネル '{target_channel.name}' に投稿します。")
                    
                    # sophia_bot.py の trigger_ai_response_for_system を呼び出す
                    # このメソッドは会話履歴を維持しつつ、システムからのプロンプトでAIを駆動する
                    await self.bot.trigger_ai_response_for_system(target_channel.id, topic_prompt)

        except Exception as e:
            logger.error(f"自発会話タスクの実行中にエラーが発生しました: {e}", exc_info=True)

    @proactive_chat_task.before_loop
    async def before_proactive_chat_task(self):
        # ボットが完全に準備できるまで待つ
        await self.bot.wait_until_ready()
        logger.info("自発会話タスクのループを開始します。")


async def setup(bot):
    # botインスタンスに `trigger_ai_response_for_system` が存在することを確認
    if not hasattr(bot, 'trigger_ai_response_for_system'):
        logger.error("Botオブジェクトに `trigger_ai_response_for_system` ���ソッドが存在しません。ProactiveCogをロードできません。")
        return
    await bot.add_cog(ProactiveCog(bot))

