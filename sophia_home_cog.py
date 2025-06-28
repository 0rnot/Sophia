# sophia_home_cog.py
import discord
from discord import app_commands
from discord.ext import commands
import logging
import math
from typing import Literal, Optional

# このCogの操作対象となるモジュールをインポート
from switchbot_api import SwitchBotAPI
from rpg_data import DEVELOPER_ID 
from config import DEVICE_IDS # config.pyからデバイスIDをインポート

logger = logging.getLogger('SophiaBot.HomeCog')

class HomeCog(commands.Cog, name="HomeCog"):
    """家電操作や環境確認用のコマンドを管理するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.switchbot_api = SwitchBotAPI()
        self.developer_id = DEVELOPER_ID
        
        if not self.switchbot_api.token:
            logger.error("SwitchBotの認証情報が読み込めませんでした。HomeCogは機能しません。")
        else:
            logger.info("HomeCogが正常にロードされました。")

    # /h グループコマンドの定義
    home_group = app_commands.Group(name="h", description="お家のデバイスを操作するよ！(開発者専用)")

    # /hv グループコマンドの定義 (誰でも利用可能)
    env_group = app_commands.Group(name="hv", description="お部屋の環境センサーの値を確認するよ！")

    async def _send_command_and_reply(self, interaction: discord.Interaction, device_key: str, command_payload: dict, success_message: str):
        """SwitchBot APIにコマンドを送信し、結果をDiscordに返信する共通処理"""
        await interaction.response.defer(thinking=True, ephemeral=False)
        
        if not self.switchbot_api.token:
            embed = discord.Embed(title="エラー", description="SwitchBotの認証情報が設定されていないため、操作できません。", color=discord.Color.red())
            await interaction.followup.send(embed=embed)
            return

        device_id = DEVICE_IDS.get(device_key)
        if not device_id:
            embed = discord.Embed(title="エラー", description=f"設定に '{device_key}' のデバイスIDが見つかりません。", color=discord.Color.red())
            await interaction.followup.send(embed=embed)
            return

        logger.info(f"デバイスID '{device_id}' にコマンド {command_payload} を送信します。")
        response = self.switchbot_api.send_command(device_id, command_payload)

        if response and response.get("statusCode") == 100:
            embed = discord.Embed(title="操作成功！", description=f"{interaction.user.mention} が実行: {success_message}", color=discord.Color.green())
            logger.info(f"デバイス '{device_id}' の操作に成功しました。")
        else:
            error_msg = response.get("message", "不明なエラー") if response else "APIからの応答がありませんでした。"
            embed = discord.Embed(title="操作失敗…", description=f"{interaction.user.mention} が実行: ごめんね、操作に失敗しちゃったみたい…。\n`{error_msg}`", color=discord.Color.red())
            logger.error(f"デバイス '{device_id}' の操作に失敗しました。応答: {response}")
        
        await interaction.followup.send(embed=embed)
    
    # --- /h コマンド群 ---
    @home_group.command(name="pc", description="PCの電源を操作するよ！(開発者専用)")
    @app_commands.describe(action="実行する操作を選んでね！")
    async def pc_control(self, interaction: discord.Interaction, action: Literal["on"]):
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message("ごめんなさい！このコマンドは私のマスター（開発者さん）しか使えないんだ…！", ephemeral=True)
            return
        if action == "on":
            command = {"command": "press", "parameter": "default", "commandType": "command"}
            success_msg = "PCの電源をポチッとしといたよ！"
            await self._send_command_and_reply(interaction, "pc", command, success_msg)

    @home_group.command(name="ac", description="エアコンを操作するよ！(開発者専用)")
    @app_commands.describe(action="実行する操作を選んでね！", temperature="設定したい温度（16～30℃）")
    async def ac_control(self, interaction: discord.Interaction, action: Literal["on", "off", "temp"], temperature: Optional[app_commands.Range[int, 16, 30]] = None):
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message("ごめんなさい！このコマンドは私のマスター（開発者さん）しか使えないんだ…！", ephemeral=True)
            return
        if action == "temp":
            if temperature is None:
                embed = discord.Embed(title="あれれ？", description="温度を設定するときは、何度にするか教えてほしいな！", color=discord.Color.orange())
                await interaction.response.send_message(embed=embed, ephemeral=False)
                return
            command_param = f"{temperature},2,1,on"
            command = {"command": "setAll", "parameter": command_param, "commandType": "command"}
            success_msg = f"エアコンを冷房 **{temperature}℃** に設定したよ！"
        else:
            command_type = "turnOn" if action == "on" else "turnOff"
            command = {"command": command_type, "parameter": "default", "commandType": "command"}
            success_msg = f"エアコンを **{action.upper()}** にしたよ！"
        await self._send_command_and_reply(interaction, "ac", command, success_msg)

    # --- /hv コマンド群 ---
    async def _get_device_status_and_reply(self, interaction: discord.Interaction, device_key: str, create_embed_func):
        """指定されたデバイスの状態を取得し、Embedを作成して返信する共通処理"""
        await interaction.response.defer(ephemeral=False, thinking=True)
        device_id = DEVICE_IDS.get(device_key)
        if not device_id:
            await interaction.followup.send(f"エラー: `{device_key}`のデバイスIDが設定されていません。", ephemeral=True)
            return

        status = self.switchbot_api.get_device_status(device_id)
        if not status or status.get("statusCode") != 100 or "body" not in status:
            error_msg = status.get("message", "不明なエラー") if status else "APIからの応答なし"
            await interaction.followup.send(f"センサー情報の取得に失敗しました…\n`{error_msg}`", ephemeral=True)
            return
        
        embed = create_embed_func(status["body"], interaction.user)
        await interaction.followup.send(embed=embed)

    def _create_meter_embed(self, status_body: dict, user: discord.User) -> discord.Embed:
        """環境メーター用のEmbedを作成する"""
        temp = status_body.get('temperature', 0.0)
        humi = status_body.get('humidity', 0)
        co2 = status_body.get("CO2", status_body.get("co2", status_body.get("co2Value", 0)))

        abs_humi = (6.112 * (10**(7.5 * temp / (237.3 + temp))) * humi * 217) / (temp + 273.15)
        dew_point = (237.3 * (math.log(humi/100) + (17.27 * temp) / (237.3 + temp))) / (17.27 - (math.log(humi/100) + (17.27 * temp) / (237.3 + temp)))
        e_s = 6.1078 * (10**(7.5 * temp / (237.3 + temp)))
        vpd = e_s * (1 - (humi / 100))

        embed = discord.Embed(title="お部屋の環境まるわかりレポート！", color=discord.Color.blue())
        embed.set_author(name=f"{user.display_name}さんからのリクエスト", icon_url=user.display_avatar.url if user.display_avatar else None)
        embed.add_field(name="🌡️ 温度", value=f"**{temp:.1f} ℃**", inline=True)
        embed.add_field(name="💧 湿度", value=f"**{humi} %**", inline=True)
        embed.add_field(name="😮‍💨 CO2濃度", value=f"**{co2} ppm**", inline=True)
        embed.add_field(name="💧 絶対湿度", value=f"{abs_humi:.2f} g/m³", inline=True)
        embed.add_field(name="🧊 露点温度", value=f"{dew_point:.1f} ℃", inline=True)
        embed.add_field(name="💨 VPD", value=f"{vpd:.2f} hPa", inline=True)
        return embed

    def _create_plug_embed(self, status_body: dict, user: discord.User) -> discord.Embed:
        """プラグミニ用のEmbedを作成する"""
        power = status_body.get('power', '不明').upper()
        voltage = status_body.get('voltage', 0.0)
        weight = status_body.get('weight', 0.0)
        electricity_of_day = status_body.get('electricityOfDay', 0)
        electric_current = status_body.get('electricCurrent', 0)

        power_status_emoji = "🟢" if power == "ON" else "⚪"
        
        embed = discord.Embed(title="🔌 プラグミニ電力レポート！", color=discord.Color.purple())
        embed.set_author(name=f"{user.display_name}さんからのリクエスト", icon_url=user.display_avatar.url if user.display_avatar else None)
        embed.add_field(name="🔌 電源状態", value=f"{power_status_emoji} **{power}**", inline=True)
        embed.add_field(name="⚡ 現在の電力", value=f"**{weight:.1f} W**", inline=True)
        embed.add_field(name="⚡ 電圧", value=f"{voltage:.1f} V", inline=True)
        embed.add_field(name="💡 今日の使用量", value=f"{electricity_of_day} Wh", inline=True)
        embed.add_field(name="⚡ 電流", value=f"{electric_current} mA", inline=True)
        return embed

    @env_group.command(name="meter", description="部屋の温度やCO2濃度などを教えるね！")
    async def env_meter(self, interaction: discord.Interaction):
        await self._get_device_status_and_reply(interaction, "co2_meter", self._create_meter_embed)

    @env_group.command(name="plug", description="プラグミニの電力使用状況を教えるね！")
    async def env_plug(self, interaction: discord.Interaction):
        await self._get_device_status_and_reply(interaction, "plug_mini", self._create_plug_embed)

async def setup(bot: commands.Bot):
    """Cogをボットに追加する"""
    await bot.add_cog(HomeCog(bot))