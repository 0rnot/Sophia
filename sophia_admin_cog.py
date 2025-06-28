# sophia_admin_cog.py
import discord
from discord import app_commands
from discord.ext import commands
import logging
from typing import Literal

# ロガー設定
logger = logging.getLogger('SophiaBot.AdminCog')

class AdminCog(commands.Cog, name="AdminCog"):
    """管理者用のコマンドを管理するCog"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("AdminCogが正常にロードされました。")

    @app_commands.command(name="switch_model", description="ソフィアのAI思考モデルを切り替えます。")
    @app_commands.describe(model_name="切り替え先のAIモデル名を選択してください。")
    @app_commands.choices(model_name=[
        app_commands.Choice(name="Gemini 2.5 Pro (論理的思考・推論モデル)", value="gemini-2.5-pro"),
        app_commands.Choice(name="Gemini 2.5 Flash (適応的思考・高速モデル)", value="gemini-2.5-flash"),
        app_commands.Choice(name="Gemini 2.5 Flash-Lite (最軽量・高速モデル)", value="gemini-2.5-flash-lite-preview-06-17"),
        app_commands.Choice(name="Gemini 1.5 Pro (高性能・安定モデル)", value="gemini-1.5-pro"),
        app_commands.Choice(name="Gemini 2.0 Flash (高速・安定モデル)", value="gemini-2.0-flash"),
        app_commands.Choice(name="Gemini 2.0 Flash Lite (最軽量・高速モデル)", value="gemini-2.0-flash-lite"),
        app_commands.Choice(name="Gemini 1.5 Flash (高速・バランスモデル)", value="gemini-1.5-flash"),
        app_commands.Choice(name="Gemini 1.5 Flash 8B (大規模向けモデル)", value="gemini-1.5-flash-8b"),
    ])
    async def switch_model(self, interaction: discord.Interaction, model_name: app_commands.Choice[str]):
        """AIモデルを切り替えるコマンド"""
        # --- 変更点: ephemeral=Trueを削除し、誰でも見れるように応答を待機 ---
        await interaction.response.defer()

        new_model_name = model_name.value
        try:
            # SophiaBotクラスに実装されたメソッドを呼び出す
            await self.bot.switch_gemini_model(new_model_name)
            embed = discord.Embed(
                title="AIモデル切り替え完了！",
                description=f"{interaction.user.mention} がAIモデルを **{model_name.name}** (`{new_model_name}`) に切り替えたよ！\nこれからの会話は新しいモデルでお話しするね！",
                color=discord.Color.green()
            )
            logger.info(f"ユーザー {interaction.user.name} により、AIモデルが {new_model_name} に切り替えられました。")
            # --- 変更点: followup.sendはdeferの設定を引き継ぐため、公開メッセージとして送信される ---
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"AIモデルの切り替え中にエラーが発生しました: {e}", exc_info=True)
            embed = discord.Embed(
                title="エラー！",
                description=f"モデルの切り替え中にエラーが発生しちゃった…\nごめんね。\n```{e}```",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    """Cogをボットに追加する"""
    await bot.add_cog(AdminCog(bot))
