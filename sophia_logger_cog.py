import discord
from discord.ext import commands
import csv
import os
from datetime import datetime

# --- 設定項目 ---

# 収集対象のユーザーIDと名前の対応表
# この名前がファイル名の一部になります (例: log_りゅう.csv)
TARGET_USERS = {
    '1033218587676123146': 'りゅうが',
    '1369943889284431936': 'もちだ',
    '598786239289884688': 'ひびき',
    '351985575047200768': 'れいや',
    '561537662536646656': 'やました',
    '961977537557323786': 'いさき',
}

# ログを保存するディレクトリのパス
# このディレクトリ内に log_ユーザー名.csv が作成されます
LOG_DIRECTORY = 'S:/Python/My_LLM_Project/user_logs'


class SophiaLoggerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        print("ログ収集Cogが読み込まれました。(ユーザー別保存モード)")
        print(f"収集対象ユーザー: {list(TARGET_USERS.values())}")
        # ログ保存用ディレクトリの存在を確認・作成
        os.makedirs(LOG_DIRECTORY, exist_ok=True)
        print(f"ログは '{LOG_DIRECTORY}' に保存されます。")

    def write_to_csv(self, file_path, data):
        """指定されたファイルパスにデータを追記する関数"""
        try:
            # ファイルが存在しない場合はヘッダーを書き込む
            file_exists = os.path.isfile(file_path)
            
            with open(file_path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(['timestamp', 'user_id', 'user_name', 'content'])
                writer.writerow(data)
        except Exception as e:
            print(f"CSVファイル '{file_path}' への書き込み中にエラーが発生しました: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # ボット自身のメッセージや、コマンド実行は無視
        if message.author.bot or message.content.startswith(self.bot.command_prefix):
            return

        # 収集対象のユーザーかどうかをIDで確認
        author_id_str = str(message.author.id)
        if author_id_str in TARGET_USERS:
            
            user_name = TARGET_USERS[author_id_str]
            
            # ユーザーごとのファイルパスを生成
            log_file_name = f"log_{user_name}.csv"
            log_file_path = os.path.join(LOG_DIRECTORY, log_file_name)
            
            # 収集するデータを作成
            timestamp = message.created_at.strftime('%Y-%m-%d %H:%M:%S')
            content = message.content
            
            # コンソールにログを表示 (デバッグ用)
            print(f"[Log -> {log_file_name}] [{user_name}] : {content}")
            
            # 対応するユーザーのCSVファイルに書き込み
            self.write_to_csv(log_file_path, [timestamp, author_id_str, user_name, content])

async def setup(bot):
    await bot.add_cog(SophiaLoggerCog(bot))