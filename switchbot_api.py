# switchbot_api.py
import os
import time
import uuid
import json
import hashlib
import hmac
import base64
import requests
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger('SophiaBot.SwitchBotAPI')

class SwitchBotAPI:
    """
    SwitchBot API (v1.1) との通信を行うためのクライアントクラス。
    署名付きリクエストをサポートします。
    """
    def __init__(self):
        """APIクライアントを初期化します。"""
        try:
            self.token = os.environ["SWITCH_BOT_TOKEN"]
            self.secret = os.environ["SWITCH_BOT_CLIENT"]
            self.api_host = "https://api.switch-bot.com"
            logger.info("SwitchBot APIクライアントが正常に初期化されました。")
        except KeyError:
            logger.critical("環境変数 SWITCH_BOT_TOKEN または SWITCH_BOT_CLIENT が設定されていません。")
            self.token = None
            self.secret = None

    def _generate_headers(self) -> Optional[Dict[str, str]]:
        """APIリクエスト用のヘッダーを生成します。"""
        if not self.token or not self.secret:
            logger.error("トークンまたはシークレットキーが利用できません。ヘッダーを生成できません。")
            return None

        t = int(round(time.time() * 1000))
        nonce = str(uuid.uuid4())
        string_to_sign = f'{self.token}{t}{nonce}'
        sign = hmac.new(
            self.secret.encode('utf-8'),
            msg=string_to_sign.encode('utf-8'),
            digestmod=hashlib.sha256
        ).digest()
        sign = base64.b64encode(sign)

        return {
            'Authorization': self.token,
            'Content-Type': 'application/json; charset=utf8',
            't': str(t),
            'sign': sign.decode('utf-8'),
            'nonce': nonce
        }

    def get_devices(self) -> Optional[Dict[str, Any]]:
        """デバイス一覧を取得します。"""
        api_path = "/v1.1/devices"
        headers = self._generate_headers()
        if not headers:
            return None
        
        try:
            response = requests.get(f"{self.api_host}{api_path}", headers=headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"デバイス一覧の取得中にAPIリクエストエラーが発生しました: {e}", exc_info=True)
            return {"statusCode": 500, "message": str(e)}

    # ### 👇 ここから修正 👇 ###
    def get_device_status(self, device_id: str) -> Optional[Dict[str, Any]]:
        """指定されたデバイスの状態を取得します。"""
        api_path = f"/v1.1/devices/{device_id}/status"
        headers = self._generate_headers()
        if not headers:
            return None
        
        try:
            response = requests.get(f"{self.api_host}{api_path}", headers=headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"デバイス(ID:{device_id})の状態取得中にAPIリクエストエラーが発生しました: {e}", exc_info=True)
            if e.response is not None:
                try:
                    return e.response.json()
                except json.JSONDecodeError:
                    return {"statusCode": e.response.status_code, "message": e.response.text}
            return {"statusCode": 500, "message": str(e)}
    # ### 👆 ここまで修正 👆 ###

    def send_command(self, device_id: str, command: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """指定されたデバイスにコマンドを送信します。"""
        api_path = f"/v1.1/devices/{device_id}/commands"
        headers = self._generate_headers()
        if not headers:
            return None

        try:
            response = requests.post(
                f"{self.api_host}{api_path}",
                headers=headers,
                data=json.dumps(command)
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"デバイス(ID:{device_id})へのコマンド送信中にAPIリクエストエラーが発生しました: {e}", exc_info=True)
            if e.response is not None:
                try:
                    return e.response.json()
                except json.JSONDecodeError:
                    return {"statusCode": e.response.status_code, "message": e.response.text}
            return {"statusCode": 500, "message": str(e)}
