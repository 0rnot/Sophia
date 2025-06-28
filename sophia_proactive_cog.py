import discord
from discord.ext import commands, tasks
import asyncio
import random
from datetime import datetime, time, timedelta
import logging
from typing import List, Optional
import config

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
        """朝と夜に1回ずつ、ランダムな時刻に投稿をスケジュールする"""
        now = datetime.now()
        today = now.date()
        
        # 時間帯を定義
        morning_start = datetime.combine(today, time(7, 0))
        morning_end = datetime.combine(today, time(12, 0))
        night_start = datetime.combine(today, time(20, 0))
        night_end = datetime.combine(today, time(23, 59))

        new_schedule = []

        # 朝のスケジュール
        # 現在時刻が朝の時間帯より前の場合のみスケジュール
        if now < morning_end:
            start = max(now, morning_start)
            if start < morning_end:
                morning_time = start + timedelta(seconds=random.uniform(0, (morning_end - start).total_seconds()))
                new_schedule.append(morning_time.time())

        # 夜のスケジュール
        # 現在時刻が夜の時間帯より前の場合のみスケジュール
        if now < night_end:
            start = max(now, night_start)
            if start < night_end:
                night_time = start + timedelta(seconds=random.uniform(0, (night_end - start).total_seconds()))
                new_schedule.append(night_time.time())
        
        self.daily_post_times = sorted(new_schedule)
        self.last_checked_day = now.day
        logger.info(f"本日の自発会話の時刻をスケジュールしました: {[t.strftime('%H:%M:%S') for t in self.daily_post_times]}")

    async def _find_most_active_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """サーバーで最もアクティブなテキストチャンネルを見つける"""
        most_active_channel = None
        max_messages = -1
        after_time = datetime.utcnow() - timedelta(days=7)

        channel_message_counts = {}

        for channel in guild.text_channels:
            if not channel.permissions_for(guild.me).read_message_history or not channel.permissions_for(guild.me).send_messages:
                continue
            
            try:
                count = 0
                async for _ in channel.history(limit=200, after=after_time):
                    count += 1
                channel_message_counts[channel] = count
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.warning(f"チャンネル {channel.name} の履歴読み取りに失敗しました: {e}")
                continue
        
        if channel_message_counts:
            most_active_channel = max(channel_message_counts, key=channel_message_counts.get)
            max_messages = channel_message_counts[most_active_channel]

        if most_active_channel:
            logger.info(f"最もアクティブなチャンネル: {most_active_channel.name} (直近7dのメッセージ数(上限200): {max_messages})")
        else:
            most_active_channel = guild.system_channel or next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
            logger.warning("アクティブなチャンネルが見つからなかったため、フォールバックチャンネルを使用します。")

        return most_active_channel

    @tasks.loop(minutes=1)
    async def proactive_chat_task(self):
        now = datetime.now()
        current_time = now.time()

        if self.last_checked_day != now.day:
            self._schedule_daily_posts()

        # 実行すべきタスクがないか、またはまだ時間でな��場合は何もしない
        if not self.daily_post_times or current_time < self.daily_post_times[0]:
            return

        # 時間になったタスクを実行し、リストから削除
        scheduled_time = self.daily_post_times.pop(0)
        logger.info(f"スケジュール時刻 {scheduled_time.strftime('%H:%M:%S')} を過ぎたため、自発会話を試みます。")
        
        try:
            # 独り言の内容をAIに生成させる
            monologue = await self.bot.generate_text_from_prompt(config.PROACTIVE_PROMPT_TEMPLATE)
            if not monologue:
                logger.warning("AIによる独り言の生成に失敗しました。")
                return

            # 各ギルドで最もアクティブなチャンネルに投稿
            for guild in self.bot.guilds:
                target_channel = await self._find_most_active_channel(guild)
                if target_channel:
                    logger.info(f"ギルド '{guild.name}' のチャンネル '{target_channel.name}' に独り言を投稿します。")
                    await target_channel.send(monologue)
                    # 複���のギルドに投稿する場合、少し間隔をあける
                    await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"自発会話タスクの実行中にエラーが発生しました: {e}", exc_info=True)

    @proactive_chat_task.before_loop
    async def before_proactive_chat_task(self):
        await self.bot.wait_until_ready()
        logger.info("自発会話タスクのループを開始します。")


async def setup(bot):
    if not hasattr(bot, 'generate_text_from_prompt'):
        logger.error("Botオブジェクトに `generate_text_from_prompt` メソッドが存在しません。ProactiveCogをロードできません。")
        return
    await bot.add_cog(ProactiveCog(bot))