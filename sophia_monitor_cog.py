# sophia_monitor_cog.py
import discord
from discord.ext import commands, tasks
import logging
import aiohttp
from typing import Dict

# 必要なモジュールと設定をインポート
from switchbot_api import SwitchBotAPI
from config import DEVICE_IDS, MONITOR_CHANNEL_ID, MONITOR_WEBHOOK_URL, THRESHOLDS, AI_PROMPT_TEMPLATE, MONITOR_INTERVAL_SECONDS

logger = logging.getLogger('SophiaBot.MonitorCog')

class MonitorCog(commands.Cog, name="MonitorCog"):
    """環境センサーを定期的に監視し、しきい値を超えたら通知するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.switchbot_api = SwitchBotAPI()
        self.first_run = True  # 初回実行フラグ
        
        self.last_alert_states: Dict[str, str] = {
            "temperature": "Normal",
            "humidity": "Normal",
            "co2": "Normal",
            "power": "Normal"
        }

        if not self.switchbot_api.token:
            logger.error("SwitchBotの認証情報が見つかりません。監視Cogは起動しません。")
        elif not MONITOR_WEBHOOK_URL:
            logger.error("config.pyにMONITOR_WEBHOOK_URLが設定されていません。監視Cogは機能しません。")
        else:
            self.check_environment_status.start()
            logger.info("MonitorCogが正常にロードされ、監視ループを開始しました。")

    def cog_unload(self):
        self.check_environment_status.cancel()
        logger.info("MonitorCogがアンロードされ、監視ループを停止しました。")

    @tasks.loop(seconds=MONITOR_INTERVAL_SECONDS)
    async def check_environment_status(self):
        await self.bot.wait_until_ready()

        # ### 👇 ここから修正 👇 ###
        # --- 初回起動時のみ通知を送信するロジック ---
        if self.first_run:
            logger.info("環境センサーの監視を開始しました。初回起動通知を送信します。")
            startup_message = "環境センサーの監視を開始しました！これから部屋の状態をチェックしていくよ！"
            # 初回通知はWebhookで送信するが、AIの応答は不要なため直接呼び出す
            try:
                async with aiohttp.ClientSession() as session:
                    webhook = discord.Webhook.from_url(MONITOR_WEBHOOK_URL, session=session)
                    embed = discord.Embed(
                        title="【システム起動通知】",
                        description=startup_message,
                        color=discord.Color.blue(),
                        timestamp=discord.utils.utcnow()
                    )
                    await webhook.send(embed=embed, username="HomeSystem")
                    logger.info(f"Webhook経由で初回起動通知を送信しました。")
            except Exception as e:
                logger.error(f"初回起動通知のWebhook送信中にエラー: {e}", exc_info=True)
            self.first_run = False
        # ### 👆 ここまで修正 👆 ###

        # --- デバイスIDの取得 ---
        meter_device_id = DEVICE_IDS.get("co2_meter")
        plug_device_id = DEVICE_IDS.get("plug_mini")

        current_values = {}

        # --- 環境メーターの状態を取得 ---
        if meter_device_id:
            logger.info(f"環境センサー(ID: {meter_device_id})の状態を確認しています...")
            status_data = self.switchbot_api.get_device_status(meter_device_id)
            if status_data and status_data.get("statusCode") == 100 and "body" in status_data:
                body = status_data["body"]
                co2_value = body.get("CO2", body.get("co2", body.get("co2Value")))
                current_values["temperature"] = body.get("temperature")
                current_values["humidity"] = body.get("humidity")
                current_values["co2"] = co2_value
            else:
                logger.error(f"センサー(ID: {meter_device_id})の状態取得に失敗しました。応答: {status_data}")
        else:
            logger.warning("設定に 'co2_meter' のデバイスIDが見つからないため、環境監視をスキップします。")

        # --- プラグミニの状態を取得 ---
        if plug_device_id:
            logger.info(f"プラグミニ(ID: {plug_device_id})の状態を確認しています...")
            status_data = self.switchbot_api.get_device_status(plug_device_id)
            if status_data and status_data.get("statusCode") == 100 and "body" in status_data:
                body = status_data["body"]
                # 'weight' が電力消費量(W)
                current_values["power"] = body.get("weight")
            else:
                logger.error(f"プラグミニ(ID: {plug_device_id})の状態取得に失敗しました。応答: {status_data}")
        else:
            logger.warning("設定に 'plug_mini' のデバイスIDが見つからないため、電力監視をスキップします。")


        if not current_values:
            logger.warning("監視対象のデバイスが一つも設定されていないか、全ての状態取得に失敗しました。")
            return

        for key, value in current_values.items():
            if value is None:
                logger.warning(f"センサーから {key} の値が取得できませんでした。")
                continue

            thresholds_for_key = THRESHOLDS.get(key)
            if not thresholds_for_key:
                continue

            # 現在の状態を判定
            current_state = "Normal"
            if thresholds_for_key.get("HH") is not None and value >= thresholds_for_key["HH"]:
                current_state = "HH"
            elif thresholds_for_key.get("H") is not None and value >= thresholds_for_key["H"]:
                current_state = "H"
            elif thresholds_for_key.get("LL") is not None and value <= thresholds_for_key["LL"]:
                current_state = "LL"
            elif thresholds_for_key.get("L") is not None and value <= thresholds_for_key["L"]:
                current_state = "L"
            
            last_state_for_key = self.last_alert_states.get(key, "Normal")
            if current_state != last_state_for_key:
                alert_message = None
                embed_color = discord.Color.blue()
                value_str = f"{value:.1f}" if isinstance(value, float) else str(value)

                # --- 状態遷移に応じたメッセージ生成ロジック ---
                if last_state_for_key == "Normal":
                    if current_state == "H":
                        alert_message = f"【注意】部屋の電力消費が **{value_str}W** に**上昇**し、設定された上限({thresholds_for_key['H']}W)を超えました。注意してください。" if key == "power" else f"【注意】部屋の{key}が **{value_str}** に**上昇**し、設定された上限({thresholds_for_key['H']})を超えました。注意してください。"
                        embed_color = discord.Color.red()
                    elif current_state == "HH":
                        alert_message = f"【警告】部屋の電力消費が **{value_str}W** に**急上昇**し、設定された上限({thresholds_for_key['HH']}W)を大幅に超えました。**極めて危険な状態です！**" if key == "power" else f"【警告】部屋の{key}が **{value_str}** に**急上昇**し、設定された上限({thresholds_for_key['HH']})を大幅に超えました。**極めて危険な状態です！**"
                        embed_color = discord.Color.dark_red()
                    elif current_state == "L":
                        alert_message = f"【注意】部屋の{key}が **{value_str}** に**低下**し、設定された下限({thresholds_for_key['L']})を下回りました。注意してください。"
                        embed_color = discord.Color.orange()
                    elif current_state == "LL":
                        alert_message = f"【警告】部屋の{key}が **{value_str}** まで**急低下**し、設定された下限({thresholds_for_key['LL']})を大幅に下回りました。**極めて危険な状態です！**"
                        embed_color = discord.Color.dark_orange()
                elif (last_state_for_key == "H" and current_state == "HH") or \
                     (last_state_for_key == "L" and current_state == "LL"):
                    change_word = "上昇" if current_state == "HH" else "低下"
                    alert_message = f"【悪化】部屋の電力消費が **{value_str}W** にさらに**{change_word}**しました。状況が悪化しています！" if key == "power" else f"【悪化】部屋の{key}が **{value_str}** にさらに**{change_word}**しました。状況が悪化しています！"
                    embed_color = discord.Color.dark_red() if current_state == "HH" else discord.Color.dark_orange()
                elif (last_state_for_key == "HH" and current_state == "H") or \
                     (last_state_for_key == "LL" and current_state == "L"):
                    change_word = "下がりました" if current_state == "H" else "上がりました"
                    alert_message = f"【改善】部屋の電力消費が **{value_str}W** まで**{change_word}**が、まだ注意が必要です。" if key == "power" else f"【改善】部屋の{key}が **{value_str}** まで**{change_word}が**、まだ注意が必要です。"
                    embed_color = discord.Color.red() if current_state == "H" else discord.Color.orange()
                elif current_state == "Normal":
                    alert_message = f"【正常化】部屋の電力消費が **{value_str}W** になり、正常な範囲に戻りました。" if key == "power" else f"【正常化】部屋の{key}が **{value_str}** になり、正常な範囲に戻りました。"
                    embed_color = discord.Color.green()

                if alert_message:
                    logger.info(f"状態変化を検知 ({key}: {last_state_for_key} -> {current_state}): {alert_message}")
                    # 修正: send_notificationsに現在の状態を渡す
                    await self.send_notifications(alert_message, embed_color, current_state)
                
                self.last_alert_states[key] = current_state

    async def send_notifications(self, alert_message: str, embed_color: discord.Color, current_state: str):
        """Webhook経由でシステム通知を送信し、その後AIの応答をトリガーする"""
        try:
            # --- 1. Webhookでシステム通知を送信 ---
            async with aiohttp.ClientSession() as session:
                webhook = discord.Webhook.from_url(MONITOR_WEBHOOK_URL, session=session)
                embed = discord.Embed(
                    title="【環境変化通知】",
                    description=alert_message,
                    color=embed_color,
                    timestamp=discord.utils.utcnow()
                )
                await webhook.send(embed=embed, username="HomeSystem")
                logger.info(f"Webhook経由でシステム通知を送信しました: {alert_message}")

            # --- 2. SophiaのAI応答を生成・送信（HHまたはLLの場合のみ） ---
            if current_state in ["HH", "LL"]:
                target_channel = self.bot.get_channel(MONITOR_CHANNEL_ID)
                if not target_channel:
                     logger.error(f"AI応答用のチャンネル(ID:{MONITOR_CHANNEL_ID})が見つかりません。")
                     return

                if hasattr(self.bot, 'trigger_ai_response_for_system'):
                    prompt_for_ai = AI_PROMPT_TEMPLATE.format(alert_message=alert_message)
                    await self.bot.trigger_ai_response_for_system(target_channel.id, prompt_for_ai) # type: ignore
                    logger.info(f"チャンネル(ID:{MONITOR_CHANNEL_ID})へのSophiaの応答生成をトリガーしました (状態: {current_state})。")
                else:
                    logger.error("botオブジェクトに trigger_ai_response_for_system メソ��ドが見つかりません。")
            else:
                logger.info(f"AI応答はスキップされました (状態: {current_state})。")

        except Exception as e:
            logger.error(f"通知処理全体でエラーが発生しました: {e}", exc_info=True)

async def setup(bot: commands.Bot):
    """Cogをボットに追加する"""
    await bot.add_cog(MonitorCog(bot))
