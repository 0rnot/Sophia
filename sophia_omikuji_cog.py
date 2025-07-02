import discord
from discord.ext import commands
import random

class OmikujiCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.fortunes = ["ざぁこ♡", "カス", "大大吉", "吉", "中吉", "小吉", "末吉", "凶", "大凶","大大凶"]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        
        if "おみくじ" in message.content:
            async with message.channel.typing():
                fortune = random.choice(self.fortunes)

                # AIに生成させるプロンプト
                prompt = f"""ユーザーがおみくじを引いたよ！
結果は「{fortune}」だよ。
この結果に合った、素敵なラッキーカラーとラッキーアイテムを一つずつ考えて、今日1日のアドバイスと一緒に、ソフィアらしく伝えてあげて。

フォーマットは以下の通り厳守してね：
今日のあなたの運勢は「{fortune}」！
ラッキーカラーは「〇〇」
ラッキーアイテムは「〇〇」
[ここにアドバイスメッセージ]
だよ！

"""

                # botのgenerate_text_from_promptを呼び出す
                sophia_response = await self.bot.generate_text_from_prompt(prompt)

                if sophia_response:
                    await message.channel.send(sophia_response)
                else:
                    # フォールバックメッセージ
                    await message.channel.send(f"ごめんなさい、うまく言葉が思い浮かばなかったみたい…。でも、今日のあなたの運勢は【{fortune}】だよ！きっと良いことがあるはず！")

async def setup(bot):
    await bot.add_cog(OmikujiCog(bot))