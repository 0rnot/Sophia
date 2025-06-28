# RPG_cog.py
import discord
from discord.ext import commands
import random
import logging
import asyncio
import os
import json
from typing import Dict, Optional

# 修正: BattleContinuationViewをインポート
from rpg_data import init_database, SELL_PRICES, RARITY_PROBABILITIES, INVENTORY_LIMIT, DEVELOPER_ID, RARITY_ORDER, RARITY_WEIGHTS, TOTAL_RARITY_WEIGHT
from rpg_views import EquipConfirmView, InventorySwapView, RerollSelectView, InventoryEmbedView, BattleView, GachaSelectView, BattleContinuationView
from rpg_utils import transaction
from gacha_system import GachaSystem, GACHA_SETTINGS

logger = logging.getLogger('SophiaBot.RPGCog')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENEMY_DATA_PATH = os.path.join(SCRIPT_DIR, 'enemy')

class BattleSession:
    def __init__(self, bot, interaction: discord.Interaction, player_stats: dict, enemy_data: dict, rpg_cog: 'RPG'):
        self.bot = bot
        self.interaction = interaction
        self.player_id = interaction.user.id
        self.guild_id = interaction.guild.id
        self.player_name = interaction.user.display_name
        self.player_avatar_url = interaction.user.display_avatar.url
        self.rpg_cog = rpg_cog

        self.player_hp = player_stats["hp"]
        self.player_max_hp = player_stats["hp"]
        self.player_atk = player_stats["atk"]
        self.player_def = player_stats["def"]

        self.enemy_name = enemy_data["name"]
        self.enemy_hp = enemy_data["hp"]
        self.enemy_max_hp = enemy_data["hp"]
        self.enemy_atk = enemy_data["atk"]
        self.enemy_def = enemy_data["def"]
        self.enemy_image_url = enemy_data.get("image_url")
        self.enemy_actions = enemy_data["actions"]
        self.enemy_dialogues = enemy_data.get("dialogues", {})
        
        # --- 修正: gold_dropが単一の数値でもリストとして扱えるように修正 ---
        raw_gold_drop = enemy_data.get("gold_drop", [0, 0])
        if isinstance(raw_gold_drop, int):
            # JSONで単一の数値が指定されている場合、[数値, 数値]のリストに変換
            self.enemy_gold_drop = [raw_gold_drop, raw_gold_drop]
        else:
            # リスト形式、またはキーが存在しない場合はそのまま（デフォルト値を含む）
            self.enemy_gold_drop = raw_gold_drop
        # --- 修正ここまで ---

        self.battle_log = [f"野生の {self.enemy_name} が現れた！ {self.enemy_dialogues.get('encounter', '')}"]
        self.current_turn = "player"
        self.is_battle_over = False
        self.battle_message: Optional[discord.WebhookMessage] = None
        self.view_instance: Optional[discord.ui.View] = None

        self.player_is_defending = False
        self.enemy_current_def_buff = 0
        self.enemy_atk_buff = 0
        self.enemy_buff_durations = {}

    async def start_battle(self):
        logger.info(f"Battle started: {self.player_name} vs {self.enemy_name} in guild {self.guild_id}")
        embed = self._create_battle_embed()
        self.view_instance = BattleView(self)
        try:
            # 「考え中」メッセージを編集して戦闘画面を表示
            self.battle_message = await self.interaction.edit_original_response(content=None, embed=embed, view=self.view_instance)
        except discord.NotFound:
            logger.warning("Original interaction message not found in start_battle. Sending new message.")
            self.battle_message = await self.interaction.channel.send(embed=embed, view=self.view_instance)

        if self.view_instance and hasattr(self.view_instance, 'message'):
            self.view_instance.message = self.battle_message

    def _create_battle_embed(self):
        embed = discord.Embed(title=f"{self.player_name} VS {self.enemy_name}", color=discord.Color.red())
        if self.player_avatar_url:
            embed.set_author(name=self.player_name, icon_url=self.player_avatar_url)
        if self.enemy_image_url:
            embed.set_thumbnail(url=self.enemy_image_url)

        embed.add_field(name=f"{self.player_name} (あなた)", value=f"HP: {self.player_hp}/{self.player_max_hp}\nATK: {self.player_atk} | DEF: {self.player_def}", inline=True)
        embed.add_field(name=self.enemy_name, value=f"HP: {self.enemy_hp}/{self.enemy_max_hp}\nATK: {self.enemy_atk} | DEF: {self.enemy_def}", inline=True)

        log_to_display = self.battle_log[-7:]
        embed.add_field(name="バトルログ", value=">>> " + "\n".join(log_to_display) if log_to_display else "戦闘開始！", inline=False)

        if self.is_battle_over:
            if self.player_hp <= 0:
                embed.description = f"**{self.player_name}は倒れてしまった...**\n{self.enemy_dialogues.get('player_lose', 'あなたの負けだ...')}"
                embed.color = discord.Color.dark_grey()
            elif self.enemy_hp <= 0:
                embed.description = f"**{self.enemy_name}を倒した！**\n{self.enemy_dialogues.get('player_win', 'あなたの勝利だ！')}"
                embed.color = discord.Color.green()
            else:
                embed.description = f"**戦闘終了** - {self.battle_log[-1] if self.battle_log else ''}"
                embed.color = discord.Color.light_grey()
        elif self.current_turn == "player":
            embed.set_footer(text="あなたのターンです。行動を選択してください。")
        else:
            embed.set_footer(text=f"{self.enemy_name}のターンです...")
        return embed

    async def update_battle_message(self, view_override: Optional[discord.ui.View] = None):
        if self.battle_message:
            embed = self._create_battle_embed()
            view_to_set = None

            if view_override is not None:
                view_to_set = view_override
            elif not self.is_battle_over:
                self.view_instance = BattleView(self)
                if hasattr(self.view_instance, 'message'):
                    self.view_instance.message = self.battle_message
                view_to_set = self.view_instance

            if self.is_battle_over and self.view_instance and view_override is None:
                 if hasattr(self.view_instance, 'stop'):
                    self.view_instance.stop()

            try:
                await self.battle_message.edit(embed=embed, view=view_to_set)
            except discord.errors.NotFound:
                logger.warning(f"Battle message (ID: {self.battle_message.id}) not found. Cannot update.")
                self.is_battle_over = True
            except Exception as e:
                logger.error(f"Error updating battle message: {e}", exc_info=True)


    async def player_action(self, action_type: str, interaction_for_action: discord.Interaction):
        if self.current_turn != "player" or self.is_battle_over:
            logger.warning(f"Player action '{action_type}' by {self.player_name} denied: Not player's turn or battle over.")
            return

        log_message = ""
        self.player_is_defending = False

        if action_type == "attack":
            actual_enemy_def = self.enemy_def + self.enemy_current_def_buff
            damage_dealt = max(1, self.player_atk - actual_enemy_def)
            self.enemy_hp = max(0, self.enemy_hp - damage_dealt)
            log_message = f"{self.player_name}の攻撃！ {self.enemy_name}に {damage_dealt} のダメージ！ {self.enemy_dialogues.get('player_attack', '')}"
        elif action_type == "defend":
            heal_amount = round(self.player_def * 0.25)
            self.player_hp = min(self.player_max_hp, self.player_hp + heal_amount)
            log_message = f"{self.player_name}は防御に専念し、HPを {heal_amount} 回復した！"
            self.player_is_defending = True
        elif action_type == "flee":
            self.is_battle_over = True
            log_message = f"{self.player_name}は戦闘から逃げ出した...！ {self.enemy_dialogues.get('player_flee', '')}"
            self.battle_log.append(log_message)
            continuation_view = BattleContinuationView(self.rpg_cog, self.player_id)
            continuation_view.message = self.battle_message
            await self.update_battle_message(view_override=continuation_view)
            if self.rpg_cog and self.player_id in self.rpg_cog.active_battles:
                del self.rpg_cog.active_battles[self.player_id]
            return

        self.battle_log.append(log_message)

        if self.enemy_hp <= 0:
            self.is_battle_over = True
            dropped_gold = random.randint(self.enemy_gold_drop[0], self.enemy_gold_drop[1])
            if dropped_gold > 0:
                try:
                    async with transaction(self.bot.db):
                        await self.bot.db.execute("UPDATE users SET gold = gold + ? WHERE user_id = ? AND guild_id = ?",
                                                  (dropped_gold, self.player_id, self.guild_id))
                    self.battle_log.append(f"{self.enemy_name}は {dropped_gold} ゴールドをドロップした！")
                except Exception as e:
                    logger.error(f"Failed to add gold after battle for user {self.player_id}: {e}", exc_info=True)

            continuation_view = BattleContinuationView(self.rpg_cog, self.player_id)
            continuation_view.message = self.battle_message
            await self.update_battle_message(view_override=continuation_view)
            
            if self.rpg_cog and self.player_id in self.rpg_cog.active_battles:
                del self.rpg_cog.active_battles[self.player_id]
            return

        self.current_turn = "enemy"
        await self.update_battle_message()
        await asyncio.sleep(1.5)
        await self.enemy_turn()

    async def enemy_turn(self):
        if self.current_turn != "enemy" or self.is_battle_over:
            return

        for buff_key in list(self.enemy_buff_durations.keys()):
            self.enemy_buff_durations[buff_key] -= 1
            if self.enemy_buff_durations[buff_key] <= 0:
                if buff_key == "defense_buff": self.enemy_current_def_buff = 0
                elif buff_key == "atk_buff": self.enemy_atk_buff = 0
                self.battle_log.append(f"{self.enemy_name}の{buff_key.replace('_buff','')}効果が切れた。")
                del self.enemy_buff_durations[buff_key]

        action = random.choice(self.enemy_actions)
        log_message = ""
        enemy_turn_action_message = action.get("message", "{enemy_name}の行動！").format(enemy_name=self.enemy_name)

        if action["type"] == "attack":
            effective_enemy_atk = self.enemy_atk + self.enemy_atk_buff
            damage_to_player = max(1, round(effective_enemy_atk * action.get("damage_multiplier", 1.0)) - self.player_def)
            if self.player_is_defending:
                damage_to_player = max(0, damage_to_player // 2)
            self.player_hp = max(0, self.player_hp - damage_to_player)
            log_message = f"{enemy_turn_action_message} {self.player_name}に {damage_to_player} のダメージ！"
        elif action["type"] == "defense_buff":
            self.enemy_current_def_buff = action.get("defense_increase", 0)
            self.enemy_buff_durations["defense_buff"] = action.get("duration", 1) + 1
            log_message = enemy_turn_action_message
        elif action["type"] == "heal":
            heal_amount = action.get("amount", 0)
            self.enemy_hp = min(self.enemy_max_hp, self.enemy_hp + heal_amount)
            log_message = enemy_turn_action_message + f" HPが{heal_amount}回復！"
        elif action["type"] == "buff_self_atk":
            self.enemy_atk_buff += action.get("atk_increase", 0)
            self.enemy_buff_durations["atk_buff"] = action.get("duration", 1) + 1
            log_message = enemy_turn_action_message
        elif action["type"] == "attack_debuff_target_def":
            effective_enemy_atk = self.enemy_atk + self.enemy_atk_buff
            damage_to_player = max(1, round(effective_enemy_atk * action.get("damage_multiplier", 0.5)) - self.player_def)
            self.player_hp = max(0, self.player_hp - damage_to_player)
            log_message = f"{enemy_turn_action_message} {self.player_name}に {damage_to_player} のダメージ！{self.player_name}の防御力が下がったようだ..."
        elif action["type"] == "nothing":
            log_message = enemy_turn_action_message
        else:
            log_message = f"{self.enemy_name}は不思議な行動をとった！ ({action.get('name', '不明な技')})"

        self.battle_log.append(log_message)

        if self.player_hp <= 0:
            self.is_battle_over = True
            continuation_view = BattleContinuationView(self.rpg_cog, self.player_id)
            continuation_view.message = self.battle_message
            await self.update_battle_message(view_override=continuation_view)
            
            if self.rpg_cog and self.player_id in self.rpg_cog.active_battles:
                del self.rpg_cog.active_battles[self.player_id]
            return

        self.current_turn = "player"
        await self.update_battle_message()

class RPG(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.inventory_limit = INVENTORY_LIMIT
        self.developer_id = DEVELOPER_ID
        self.sell_prices = SELL_PRICES
        self.rarity_probabilities = RARITY_PROBABILITIES
        self.rarity_order = RARITY_ORDER
        self.rarity_weights = RARITY_WEIGHTS
        self.total_rarity_weight = TOTAL_RARITY_WEIGHT
        self.active_battles: Dict[int, BattleSession] = {}
        self.gacha_system = GachaSystem(bot, self)
        self.gacha_settings = GACHA_SETTINGS


    async def cog_load(self):
        await init_database(self.bot.db)
        if not os.path.exists(ENEMY_DATA_PATH):
            try:
                os.makedirs(ENEMY_DATA_PATH)
                logger.info(f"Created enemy data directory at: {ENEMY_DATA_PATH}")
                sample_enemy = {
                    "name": "テストスライム",
                    "hp": 20,
                    "atk": 3,
                    "def": 1,
                    "image_url": None,
                    "actions": [
                        {"name": "体当たり", "type": "attack", "damage_multiplier": 1.0, "message": "{enemy_name}がプルプル攻撃！"},
                        {"name": "何もしない", "type": "nothing", "message": "{enemy_name}はボーっとしている。"}
                    ],
                    "dialogues": {
                        "encounter": "プルン！",
                        "player_attack": "ピキィ！",
                        "player_win": "グチャ...",
                        "player_lose": "プルプル！"
                    },
                    "gold_drop": [1, 5]
                }
                with open(os.path.join(ENEMY_DATA_PATH, "test_slime.json"), 'w', encoding='utf-8') as f:
                    json.dump(sample_enemy, f, ensure_ascii=False, indent=4)
                logger.info("Created a sample enemy file: test_slime.json")
            except Exception as e:
                logger.error(f"Could not create enemy directory or sample file: {e}", exc_info=True)


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if message.author.id in self.active_battles:
            return

        user_id = message.author.id
        guild_id = message.guild.id

        async with self.bot.db.execute("SELECT total_characters, level FROM users WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)) as cursor:
            row = await cursor.fetchone()

        if row:
            old_total_chars, old_level = row[0], row[1]
        else:
            old_total_chars, old_level = 0, 0
            try:
                async with transaction(self.bot.db):
                    await self.bot.db.execute("INSERT INTO users (user_id, guild_id, total_characters, level, gold) VALUES (?, ?, ?, ?, ?)",
                                              (user_id, guild_id, 0, 0, 0))
            except Exception as e:
                logger.error(f"Failed to register new user {user_id} in guild {guild_id}: {e}", exc_info=True)
                return

        new_total_chars = old_total_chars + len(message.content)
        chars_per_level = 250
        new_level = new_total_chars // chars_per_level

        try:
            async with transaction(self.bot.db):
                await self.bot.db.execute("UPDATE users SET total_characters = ?, level = ? WHERE user_id = ? AND guild_id = ?",
                                          (new_total_chars, new_level, user_id, guild_id))
        except Exception as e:
            logger.error(f"Failed to update user {user_id} stats in guild {guild_id}: {e}", exc_info=True)
            return

        if new_level > old_level:
            leveled_up_by = new_level - old_level
            level_up_embed_description = f"{message.author.mention} がレベル {new_level} にアップ！ ({leveled_up_by}レベル上昇)"
            level_up_embed = discord.Embed(title="レベルアップ！", description=level_up_embed_description, color=discord.Color.gold())
            level_up_embed.set_thumbnail(url=message.author.display_avatar.url)

            items_dropped_for_embed = []
            choice_payload = None

            for i in range(leveled_up_by):
                logger.info(f"Level up drop attempt {i+1}/{leveled_up_by} for user {user_id}")
                drop_result = await self.drop_item(
                    user_id, guild_id, message.channel,
                    message.author.id, message.author.display_name, message.author.display_avatar.url
                )

                if isinstance(drop_result, dict):
                    choice_payload = drop_result
                    view: InventorySwapView = choice_payload['view']
                    full_item_name = view.new_full_item_name
                    item_type_display = view.new_item_type_display
                    base_item_rarity = view.new_item_base_rarity
                    effect_rarity = view.new_effect_rarity

                    prob_item = (self.rarity_weights.get(base_item_rarity, 0) / self.total_rarity_weight) if self.total_rarity_weight > 0 else 0
                    prob_effect = (self.rarity_weights.get(effect_rarity, 0) / self.total_rarity_weight) if self.total_rarity_weight > 0 else 0
                    combined_prob_percent = prob_item * prob_effect * 100

                    items_dropped_for_embed.append(
                        f"**{full_item_name}** ({item_type_display})\n"
                        f"　┣ 装備レアリティ: {base_item_rarity} ({self.rarity_probabilities.get(base_item_rarity, 'N/A')})\n"
                        f"　┣ 効果レアリティ: {effect_rarity} ({self.rarity_probabilities.get(effect_rarity, 'N/A')})\n"
                        f"　┗ 組み合わせ出現率: {combined_prob_percent:.3f}%"
                    )
                    break
                elif drop_result is None:
                    logger.warning(f"Drop attempt {i+1}/{leveled_up_by} for user {user_id} resulted in None (no item or error).")

            if items_dropped_for_embed:
                level_up_embed.add_field(name="獲得アイテム候補", value="\n\n".join(items_dropped_for_embed), inline=False)

            if choice_payload:
                current_description = level_up_embed.description or ""
                level_up_embed.description = f"{current_description}\n新しいアイテムをどうするか選択肢が表示されています。"
            elif not items_dropped_for_embed and leveled_up_by > 0 :
                 level_up_embed.add_field(name="獲得アイテム", value="今回は新しいアイテムを見つけられなかった...", inline=False)


            try:
                level_up_message = await message.channel.send(embed=level_up_embed)
                if choice_payload:
                    choice_embed = choice_payload['embed']
                    choice_view: InventorySwapView = choice_payload['view']
                    choice_view.level_up_message_to_delete = level_up_message
                    message_with_view = await message.channel.send(embed=choice_embed, view=choice_view)
                    choice_view.message_with_view = message_with_view
            except discord.Forbidden:
                logger.warning(f"Missing permissions to send level up message in {message.channel.name} (guild {guild_id}).")
            except Exception as e:
                logger.error(f"Error sending level up message for user {user_id} in guild {guild_id}: {e}", exc_info=True)

    async def _get_item_stats_from_db(self, base_item_id: int, effect_id: int):
        """Helper to get combined stats of a base item and an effect."""
        async with self.bot.db.execute("SELECT base_attack, base_defense FROM items WHERE item_id = ?", (base_item_id,)) as cur_item:
            item_stats_row = await cur_item.fetchone()
        async with self.bot.db.execute("SELECT attack_bonus, defense_bonus FROM effects WHERE effect_id = ?", (effect_id,)) as cur_effect:
            effect_stats_row = await cur_effect.fetchone()

        if item_stats_row and effect_stats_row:
            return {
                "base_attack": item_stats_row[0], "base_defense": item_stats_row[1],
                "effect_attack_bonus": effect_stats_row[0], "effect_defense_bonus": effect_stats_row[1]
            }
        logger.warning(f"Could not retrieve full stats for base_item_id: {base_item_id} or effect_id: {effect_id}")
        return {"base_attack": 0, "base_defense": 0, "effect_attack_bonus": 0, "effect_defense_bonus": 0}


    async def drop_item(self, user_id: int, guild_id: int, channel: discord.TextChannel,
                        interaction_user_id: int, user_display_name: str, user_avatar_url: str):
        """
        Generates a random item.
        Returns a dictionary with embed and view if inventory is full or item is offered,
        otherwise None if an error occurs.
        """
        new_item_type = random.choice(["weapon", "armor"])
        new_item_type_display = "武器" if new_item_type == "weapon" else "防具"
        new_base_rarity = random.choices(list(self.rarity_weights.keys()), weights=list(self.rarity_weights.values()), k=1)[0]

        async with self.bot.db.execute("SELECT item_id, base_name FROM items WHERE type = ? AND rarity = ? ORDER BY RANDOM() LIMIT 1", (new_item_type, new_base_rarity)) as cursor:
            item_row = await cursor.fetchone()
        if not item_row:
            logger.error(f"No base item found for type {new_item_type} and rarity {new_base_rarity}")
            return None
        new_item_base_id, new_item_base_name = item_row

        new_effect_rarity = random.choices(list(self.rarity_weights.keys()), weights=list(self.rarity_weights.values()), k=1)[0]
        async with self.bot.db.execute("SELECT effect_id, prefix_name FROM effects WHERE rarity = ? ORDER BY RANDOM() LIMIT 1", (new_effect_rarity,)) as cursor:
            effect_row = await cursor.fetchone()
        if not effect_row:
            logger.warning(f"No effect found for rarity {new_effect_rarity}. Assigning 'no effect' (ID 0).")
            async with self.bot.db.execute("SELECT effect_id, prefix_name FROM effects WHERE effect_id = 0") as c_no_effect:
                no_effect_row = await c_no_effect.fetchone()
            if no_effect_row: new_effect_id, new_effect_name_prefix = no_effect_row
            else: new_effect_id, new_effect_name_prefix = 0, ""
            new_effect_rarity = "N/A"
        else:
            new_effect_id, new_effect_name_prefix = effect_row

        async with self.bot.db.execute("SELECT COUNT(*) FROM inventory WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)) as cursor:
            count_row = await cursor.fetchone()
        current_inventory_count = count_row[0] if count_row else 0
        is_inventory_full = current_inventory_count >= self.inventory_limit

        return await self.handle_inventory_full(
            user_id, guild_id, channel, interaction_user_id, user_display_name, user_avatar_url,
            new_item_base_id, new_item_base_name, new_base_rarity, new_item_type_display,
            new_effect_id, new_effect_name_prefix or "", new_effect_rarity, is_inventory_full
        )

    async def handle_inventory_full(self, user_id: int, guild_id: int, channel: discord.TextChannel, interaction_user_id: int, user_display_name: str, user_avatar_url: str,
                                    new_item_base_id: int, new_item_base_name: str, new_base_rarity: str, new_item_type_display: str,
                                    new_effect_id: int, new_effect_name_prefix: str, new_effect_rarity: str, is_inventory_full: bool):
        """
        Prepares the embed and view for the item choice.
        Returns a dictionary: {"embed": discord.Embed, "view": InventorySwapView}
        """
        new_full_item_name = f"{new_effect_name_prefix}{new_item_base_name}"

        async with self.bot.db.execute("""
            SELECT inv.inventory_id, i.base_name, i.type, i.rarity as base_rarity, i.item_id,
                   e.prefix_name, e.rarity as effect_rarity, e.effect_id,
                   i.base_attack, i.base_defense, e.attack_bonus, e.defense_bonus
            FROM inventory inv
            JOIN items i ON inv.item_id = i.item_id
            JOIN effects e ON inv.effect_id = e.effect_id
            WHERE inv.user_id = ? AND inv.guild_id = ? ORDER BY inv.inventory_id ASC
        """, (user_id, guild_id)) as cursor:
            inventory_items_for_view_tuples = await cursor.fetchall()

        title = "新しいアイテムを入手！" if not is_inventory_full else "インベントリが上限です！"
        prob_item = (RARITY_WEIGHTS.get(new_base_rarity, 0) / TOTAL_RARITY_WEIGHT) if TOTAL_RARITY_WEIGHT > 0 else 0
        prob_effect = (RARITY_WEIGHTS.get(new_effect_rarity, 0) / TOTAL_RARITY_WEIGHT) if TOTAL_RARITY_WEIGHT > 0 else 0
        if new_effect_rarity == "N/A": prob_effect = 0

        combined_prob_percent = prob_item * prob_effect * 100

        description = (
            f"レベルアップで **{new_full_item_name}** (装備:{new_base_rarity}/効果:{new_effect_rarity}) を見つけました！\n"
            f"　┣ 装備レアリティ: {new_base_rarity} ({RARITY_PROBABILITIES.get(new_base_rarity, 'N/A')})\n"
            f"　┣ 効果レアリティ: {new_effect_rarity} ({RARITY_PROBABILITIES.get(new_effect_rarity, 'N/A')})\n"
            f"　┗ 組み合わせ出現率: {combined_prob_percent:.3f}%\n\n"
            f"どうしますか？\n\n"
            f"このメッセージへの操作は <@{interaction_user_id}> さんのみ可能です。"
        )
        if is_inventory_full:
            description = (
                f"インベントリが一杯（{self.inventory_limit}個）です。\n"
                f"**{new_full_item_name}** (装備:{new_base_rarity}/効果:{new_effect_rarity}) を見つけましたが、\n"
                f"　┣ 装備レアリティ: {new_base_rarity} ({RARITY_PROBABILITIES.get(new_base_rarity, 'N/A')})\n"
                f"　┣ 効果レアリティ: {new_effect_rarity} ({RARITY_PROBABILITIES.get(new_effect_rarity, 'N/A')})\n"
                f"　┗ 組み合わせ出現率: {combined_prob_percent:.3f}%\n\n"
                f"どうしますか？\n\n"
                f"このメッセージへの操作は <@{interaction_user_id}> さんのみ可能です。"
            )

        embed = discord.Embed(title=title, description=description, color=discord.Color.orange())
        embed.set_thumbnail(url=user_avatar_url)

        preview_items_count = 3
        if inventory_items_for_view_tuples:
            preview_text_parts = [f"ID:{item[0]} | {item[5]}{item[1][:15]} ({item[3]}/{item[6]})" for item in inventory_items_for_view_tuples[:preview_items_count]]
            preview_text = "\n".join(preview_text_parts)
            if len(inventory_items_for_view_tuples) > preview_items_count:
                preview_text += f"\n...他{len(inventory_items_for_view_tuples) - preview_items_count}件"
            if len(preview_text) > 1020:
                preview_text = preview_text[:1020] + "..."
            embed.add_field(name=f"現在のインベントリ (一部表示 - 全{len(inventory_items_for_view_tuples)}件)", value=preview_text if preview_text else "なし", inline=False)
        else:
            embed.add_field(name="現在のインベントリ", value="なし", inline=False)

        embed.set_footer(text="選択肢のタイムアウトは2分です。")

        view = InventorySwapView(
            self.bot, user_id, guild_id,
            new_item_base_id, new_item_base_name, new_base_rarity, new_item_type_display,
            new_effect_id, new_effect_name_prefix, new_effect_rarity,
            inventory_items_for_view_tuples,
            interaction_user_id,
            is_inventory_full
        )
        return {"embed": embed, "view": view}


    async def manage_user_role(self, guild: discord.Guild, user: discord.Member, full_item_name: str, item_type_display: str):
        """Manages RPG equipment roles for a user."""
        roles_to_remove = [role for role in user.roles if role.name.startswith(f"{item_type_display}:")]
        if roles_to_remove:
            try:
                await user.remove_roles(*roles_to_remove, reason="RPG装備変更")
            except discord.Forbidden:
                logger.warning(f"Missing permissions to remove roles from {user.name} in {guild.name}.")
            except Exception as e:
                logger.error(f"Error removing roles from {user.name} in {guild.name}: {e}", exc_info=True)

        new_role_name = f"{item_type_display}: {full_item_name}"
        if len(new_role_name) > 100:
            new_role_name = new_role_name[:97] + "..."

        role_to_assign = discord.utils.get(guild.roles, name=new_role_name)
        if not role_to_assign:
            try:
                role_to_assign = await guild.create_role(name=new_role_name, mentionable=False, reason=f"RPG装備: {full_item_name}")
            except discord.Forbidden:
                logger.warning(f"Missing permissions to create role {new_role_name} in {guild.name}.")
                return
            except Exception as e:
                logger.error(f"Error creating role {new_role_name} in {guild.name}: {e}", exc_info=True)
                return

        if role_to_assign:
            try:
                await user.add_roles(role_to_assign, reason="RPG装備変更")
            except discord.Forbidden:
                logger.warning(f"Missing permissions to add role {role_to_assign.name} to {user.name} in {guild.name}.")
            except Exception as e:
                logger.error(f"Error adding role {role_to_assign.name} to {user.name} in {guild.name}: {e}", exc_info=True)

    @discord.app_commands.command(name="vlevel", description="現在のレベルとゴールドを表示")
    async def level_cmd(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        guild_id = interaction.guild.id
        async with self.bot.db.execute("SELECT level, total_characters, gold FROM users WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)) as cursor:
            row = await cursor.fetchone()

        embed = discord.Embed(title=f"{interaction.user.display_name} のステータス", color=discord.Color.green())
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        show_ephemeral = True
        if row:
            level, total_chars, gold = row
            embed.add_field(name="レベル", value=str(level), inline=True)
            embed.add_field(name="総入力文字数", value=str(total_chars), inline=True)
            embed.add_field(name="ゴールド", value=f"{gold} G", inline=True)
            show_ephemeral = False
        else:
            embed.description = "まだSophiaに認識されていません。何かメッセージを送ってみましょう！"
            embed.color = discord.Color.red()
        await interaction.response.send_message(embed=embed, ephemeral=show_ephemeral)

    @discord.app_commands.command(name="vinventory", description="インベントリを表示します。ソートも可能です。")
    async def inventory_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        guild_id = interaction.guild.id

        async with self.bot.db.execute("""
            SELECT
                inv.inventory_id, i.base_name, i.type AS item_type, i.rarity AS base_item_rarity,
                i.item_id AS base_item_id, e.prefix_name AS effect_name_prefix, e.rarity AS effect_rarity,
                e.effect_id AS effect_id, i.base_attack, i.base_defense,
                e.attack_bonus AS effect_attack_bonus, e.defense_bonus AS effect_defense_bonus
            FROM inventory inv
            JOIN items i ON inv.item_id = i.item_id
            JOIN effects e ON inv.effect_id = e.effect_id
            WHERE inv.user_id = ? AND inv.guild_id = ?
        """, (user_id, guild_id)) as cursor:
            inventory_items_db_tuples = await cursor.fetchall()

        async with self.bot.db.execute("SELECT gold FROM users WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)) as cursor:
            gold_row = await cursor.fetchone()
            gold = gold_row[0] if gold_row else 0

        if not inventory_items_db_tuples:
            embed = discord.Embed(title=f"{interaction.user.display_name} のインベントリ (0/{self.inventory_limit})", color=discord.Color.red())
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            embed.add_field(name="所持ゴールド", value=f"{gold} G", inline=False)
            embed.add_field(name="アイテム", value="インベントリは空です。", inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        view = InventoryEmbedView(
            inventory_items_db_tuples, items_per_page=5, user_name=interaction.user.display_name,
            inventory_limit=self.inventory_limit, gold=gold, rarity_probabilities=self.rarity_probabilities,
            initial_interaction=interaction, current_sort_order="id_asc", is_ephemeral=True,
            user_avatar_url=interaction.user.display_avatar.url
        )
        await view.send_initial_message()

    @discord.app_commands.command(name="vequip", description="インベントリからアイテムを装備")
    @discord.app_commands.describe(inventory_id="装備するアイテムのインベントリID")
    async def equip_cmd(self, interaction: discord.Interaction, inventory_id: int):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        guild_id = interaction.guild.id

        async with self.bot.db.execute("""
            SELECT i.base_name, i.type, i.rarity as base_rarity, e.prefix_name, i.item_id, e.effect_id, e.rarity as effect_rarity
            FROM inventory inv
            JOIN items i ON inv.item_id = i.item_id
            JOIN effects e ON inv.effect_id = e.effect_id
            WHERE inv.inventory_id = ? AND inv.user_id = ? AND inv.guild_id = ?
        """, (inventory_id, user_id, guild_id)) as cursor:
            selected_item_row = await cursor.fetchone()

        if not selected_item_row:
            embed = discord.Embed(title="エラー", description=f"インベントリID {inventory_id} は存在しないか、あなたのアイテムではありません。", color=discord.Color.red())
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        base_name, item_type, base_item_rarity, effect_prefix, _item_id, _effect_id, effect_rarity = selected_item_row
        full_item_name = f"{effect_prefix}{base_name}"
        item_type_display = "武器" if item_type == "weapon" else "防具"
        equip_field_to_update = "equipped_weapon" if item_type == "weapon" else "equipped_armor"

        async with self.bot.db.execute(f"SELECT {equip_field_to_update} FROM users WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)) as cursor:
            currently_equipped_row = await cursor.fetchone()
        currently_equipped_inv_id = currently_equipped_row[0] if currently_equipped_row else None

        if currently_equipped_inv_id == inventory_id:
            embed = discord.Embed(title="情報", description=f"そのアイテム ({full_item_name}) は既に装備中です。", color=discord.Color.blue())
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if currently_equipped_inv_id:
            async with self.bot.db.execute("""
                SELECT i.base_name as c_base_name, i.type as c_type, i.rarity as c_item_rarity, e.prefix_name as c_effect_prefix, e.rarity as c_effect_rarity
                FROM inventory inv
                JOIN items i ON inv.item_id = i.item_id
                JOIN effects e ON inv.effect_id = e.effect_id
                WHERE inv.inventory_id = ? AND inv.user_id = ? AND inv.guild_id = ?
            """, (currently_equipped_inv_id, user_id, guild_id)) as cursor:
                equipped_item_details = await cursor.fetchone()

            if equipped_item_details:
                c_base_name, c_type, c_item_rarity, c_effect_prefix, c_effect_rarity = equipped_item_details
                current_full_name = f"{c_effect_prefix}{c_base_name}"
                current_type_display = "武器" if c_type == "weapon" else "防具"
                prob_str_current = self._calculate_combined_probability_str_for_cog(c_item_rarity, c_effect_rarity)
                prob_str_new = self._calculate_combined_probability_str_for_cog(base_item_rarity, effect_rarity)


                embed = discord.Embed(title="装備の入れ替え確認", color=discord.Color.blue())
                embed.set_thumbnail(url=interaction.user.display_avatar.url)
                embed.description = (
                    f"現在装備中: **{current_full_name}** ({current_type_display})\n"
                    f"　┣ ﾚｱ: {c_item_rarity}/{c_effect_rarity} (出現率: {prob_str_current})\n"
                    f"新しい装備: **{full_item_name}** ({item_type_display})\n"
                    f"　┣ ﾚｱ: {base_item_rarity}/{effect_rarity} (出現率: {prob_str_new})\n\n"
                    "この装備に入れ替えますか？"
                )
                view = EquipConfirmView(self.bot, inventory_id, full_item_name, item_type_display, equip_field_to_update, interaction.user.id)
                await interaction.followup.send(embed=embed, view=view, ephemeral=True)
                return

        try:
            async with transaction(self.bot.db):
                await self.bot.db.execute(f"UPDATE users SET {equip_field_to_update} = ? WHERE user_id = ? AND guild_id = ?", (inventory_id, user_id, guild_id))
            await self.manage_user_role(interaction.guild, interaction.user, full_item_name, item_type_display)
            embed = discord.Embed(title="装備完了", description=f"**{full_item_name}** ({item_type_display}) を装備しました。", color=discord.Color.green())
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error equipping item for user {user_id} in guild {guild_id}: {e}", exc_info=True)
            error_embed = discord.Embed(title="エラー", description="装備処理中にエラーが発生しました。", color=discord.Color.red())
            await interaction.followup.send(embed=error_embed, ephemeral=True)


    @discord.app_commands.command(name="vstats", description="現在の装備アイテムとステータスを表示")
    async def stats_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        user_id = interaction.user.id
        guild_id = interaction.guild.id
        target_user = interaction.user

        async with self.bot.db.execute("SELECT equipped_weapon, equipped_armor, gold, level FROM users WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)) as cursor:
            user_data = await cursor.fetchone()

        embed = discord.Embed(title=f"{target_user.display_name} のステータス", color=discord.Color.purple())
        embed.set_thumbnail(url=target_user.display_avatar.url)
        has_any_info = False

        if user_data:
            has_any_info = True
            equipped_weapon_inv_id, equipped_armor_inv_id, gold, level = user_data
            embed.add_field(name="レベル", value=str(level), inline=True)
            embed.add_field(name="ゴールド", value=f"{gold} G", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)

            total_atk = 0
            total_def = 0

            weapon_emoji = "🗡️"
            if equipped_weapon_inv_id:
                async with self.bot.db.execute("""
                    SELECT i.base_name, i.rarity as item_rarity, i.base_attack, i.base_defense,
                           e.prefix_name, e.attack_bonus, e.defense_bonus, e.rarity as effect_rarity
                    FROM inventory inv
                    JOIN items i ON inv.item_id = i.item_id
                    JOIN effects e ON inv.effect_id = e.effect_id
                    WHERE inv.inventory_id = ? AND inv.user_id = ? AND inv.guild_id = ?
                """, (equipped_weapon_inv_id, user_id, guild_id)) as cur:
                    item_stats = await cur.fetchone()
                    if item_stats:
                        full_name = f"{item_stats[4]}{item_stats[0]}"
                        item_rarity = item_stats[1]; eff_rarity = item_stats[7]
                        item_atk = item_stats[2] + item_stats[5]; item_def = item_stats[3] + item_stats[6]
                        total_atk += item_atk; total_def += item_def
                        prob_str = self._calculate_combined_probability_str_for_cog(item_rarity, eff_rarity)
                        embed.add_field(name=f"{weapon_emoji} 装備中の武器", value=f"**{full_name}**\n　┣ ﾚｱ: {item_rarity}/{eff_rarity} (出現率: {prob_str})\n　┗ ATK: {item_atk} | DEF: {item_def}", inline=False)
                    else:
                        embed.add_field(name=f"{weapon_emoji} 装備中の武器", value="なし (情報取得エラー)", inline=False)
            else:
                embed.add_field(name=f"{weapon_emoji} 装備中の武器", value="なし", inline=False)

            shield_emoji = "<:shield:1237991581006565426>"
            if equipped_armor_inv_id:
                async with self.bot.db.execute("""
                    SELECT i.base_name, i.rarity as item_rarity, i.base_attack, i.base_defense,
                           e.prefix_name, e.attack_bonus, e.defense_bonus, e.rarity as effect_rarity
                    FROM inventory inv
                    JOIN items i ON inv.item_id = i.item_id
                    JOIN effects e ON inv.effect_id = e.effect_id
                    WHERE inv.inventory_id = ? AND inv.user_id = ? AND inv.guild_id = ?
                """, (equipped_armor_inv_id, user_id, guild_id)) as cur:
                    item_stats = await cur.fetchone()
                    if item_stats:
                        full_name = f"{item_stats[4]}{item_stats[0]}"
                        item_rarity = item_stats[1]; eff_rarity = item_stats[7]
                        item_atk = item_stats[2] + item_stats[5]; item_def = item_stats[3] + item_stats[6]
                        total_atk += item_atk; total_def += item_def
                        prob_str = self._calculate_combined_probability_str_for_cog(item_rarity, eff_rarity)
                        embed.add_field(name=f"{shield_emoji} 装備中の防具", value=f"**{full_name}**\n　┣ ﾚｱ: {item_rarity}/{eff_rarity} (出現率: {prob_str})\n　┗ ATK: {item_atk} | DEF: {item_def}", inline=False)
                    else:
                        embed.add_field(name=f"{shield_emoji} 装備中の防具", value="なし (情報取得エラー)", inline=False)
            else:
                embed.add_field(name=f"{shield_emoji} 装備中の防具", value="なし", inline=False)

            embed.add_field(name="合計ステータス", value=f"ATK: {total_atk} | DEF: {total_def}", inline=False)

        if not has_any_info:
            embed.description = "まだSophiaに認識されていません。装備の情報もありません。"
            embed.color = discord.Color.red()
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        await interaction.followup.send(embed=embed, ephemeral=False)

    def _calculate_combined_probability_str_for_cog(self, item_base_rarity, effect_rarity):
        """Calculates and formats the combined probability string for an item."""
        if self.total_rarity_weight == 0: return "計算不可"
        item_weight = self.rarity_weights.get(item_base_rarity, 0)
        effect_weight = self.rarity_weights.get(effect_rarity, 0)

        prob_item = item_weight / self.total_rarity_weight
        prob_effect = effect_weight / self.total_rarity_weight
        combined_prob = prob_item * prob_effect
        return f"{combined_prob * 100:.3f}%"


    @discord.app_commands.command(name="vreroll", description="アイテムの効果を再抽選（同レアリティの装備5個を消費）")
    @discord.app_commands.describe(inventory_id_to_reroll="再抽選するアイテムのインベントリID")
    async def reroll_cmd(self, interaction: discord.Interaction, inventory_id_to_reroll: int):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        guild_id = interaction.guild.id

        async with self.bot.db.execute("""
            SELECT i.rarity as base_rarity, i.base_name, i.type FROM inventory inv
            JOIN items i ON inv.item_id = i.item_id
            WHERE inv.inventory_id = ? AND inv.user_id = ? AND inv.guild_id = ?
        """, (inventory_id_to_reroll, user_id, guild_id)) as cursor:
            target_item_info = await cursor.fetchone()

        if not target_item_info:
            await interaction.followup.send(embed=discord.Embed(title="エラー", description=f"ID {inventory_id_to_reroll} は存在しないかあなたのアイテムではありません。", color=discord.Color.red()), ephemeral=True)
            return

        target_item_base_rarity, target_item_base_name, target_item_type = target_item_info
        target_item_type_display = "武器" if target_item_type == "weapon" else "防具"

        async with self.bot.db.execute("""
            SELECT inv.inventory_id, i.base_name, i.type, i.rarity as base_rarity, i.item_id,
                   e.prefix_name, e.rarity as effect_rarity, e.effect_id
            FROM inventory inv
            JOIN items i ON inv.item_id = i.item_id
            JOIN effects e ON inv.effect_id = e.effect_id
            WHERE inv.user_id = ? AND inv.guild_id = ? AND i.rarity = ? AND inv.inventory_id != ?
            ORDER BY inv.inventory_id ASC
        """, (user_id, guild_id, target_item_base_rarity, inventory_id_to_reroll)) as cursor:
            consumable_items_tuples = await cursor.fetchall()

        if len(consumable_items_tuples) < 5:
            await interaction.followup.send(embed=discord.Embed(title="エラー", description=f"同じベースレアリティ ({target_item_base_rarity}) の装備が他に5個必要です。(現在: {len(consumable_items_tuples)}個)", color=discord.Color.red()), ephemeral=True)
            return

        new_eff_rarity = random.choices(list(self.rarity_weights.keys()), weights=list(self.rarity_weights.values()), k=1)[0]
        async with self.bot.db.execute("SELECT effect_id, prefix_name FROM effects WHERE rarity = ? ORDER BY RANDOM() LIMIT 1", (new_eff_rarity,)) as cursor:
            new_effect_info = await cursor.fetchone()
        if not new_effect_info:
            await interaction.followup.send(embed=discord.Embed(title="エラー", description="新しい効果の抽選に失敗しました。", color=discord.Color.red()), ephemeral=True)
            return
        new_effect_id, new_effect_name_prefix = new_effect_info

        new_full_item_name_preview = f"{new_effect_name_prefix}{target_item_base_name}"

        embed = discord.Embed(title="効果の再抽選", color=discord.Color.blue())
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.description = (
            f"**{target_item_type_display}: {target_item_base_name}** (ベースレアリティ: {target_item_base_rarity}) の効果を再抽選します。\n"
            f"新しい効果候補の接頭辞: **{new_effect_name_prefix}** (効果レアリティ: {new_eff_rarity})\n"
            f"これにより、アイテム名は「{new_full_item_name_preview}」のようになります。\n\n"
            "以下から5個選択して消費してください:"
        )

        consumable_list_str_parts = []
        for item_tuple in consumable_items_tuples[:25]:
            consumable_list_str_parts.append(f"ID:{item_tuple[0]} | {item_tuple[5]}{item_tuple[1][:15]} ({item_tuple[3]}/{item_tuple[6]})")
        consumable_list_str = "\n".join(consumable_list_str_parts)
        if len(consumable_items_tuples) > 25:
            consumable_list_str += f"\n...他{len(consumable_items_tuples) - 25}件（選択肢には最初の25件まで表示）"

        embed.add_field(name="消費候補アイテム", value=consumable_list_str if consumable_list_str else "なし", inline=False)
        embed.set_footer(text=f"この操作は {interaction.user.display_name} さんのみ可能です。タイムアウトは2分です。")

        view = RerollSelectView(
            self.bot, inventory_id_to_reroll, new_effect_id, new_effect_name_prefix, new_eff_rarity,
            consumable_items_tuples, target_item_base_rarity, interaction.user.id
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.app_commands.command(name="vsell", description="インベントリから指定したIDのアイテムを複数売却します (IDをスペース区切りで入力)。")
    @discord.app_commands.describe(inventory_ids="売却するアイテムのインベントリID (スペース区切り)")
    async def sell_cmd(self, interaction: discord.Interaction, inventory_ids: str):
        await interaction.response.defer(thinking=True, ephemeral=True)
        user_id = interaction.user.id
        guild_id = interaction.guild.id

        try:
            try:
                ids_to_sell_str = inventory_ids.split()
                ids_to_sell_int = [int(id_str) for id_str in ids_to_sell_str]
                if not ids_to_sell_int:
                    raise ValueError("売却するアイテムのIDが指定されていません。")
            except ValueError:
                embed = discord.Embed(title="入力エラー", description="アイテムIDは半角数字で、スペース区切りで入力してください。", color=discord.Color.red())
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            sold_items_details = []
            failed_to_sell_ids = []
            total_sell_price = 0

            async with transaction(self.bot.db):
                async with self.bot.db.execute("SELECT gold FROM users WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)) as gold_cursor:
                    gold_row = await gold_cursor.fetchone()
                current_gold = gold_row[0] if gold_row else 0
                accumulated_sell_price_for_tx = 0

                for inv_id in ids_to_sell_int:
                    async with self.bot.db.execute("""
                        SELECT i.base_name, i.rarity as base_rarity, e.prefix_name, e.rarity as effect_rarity
                        FROM inventory inv
                        JOIN items i ON inv.item_id = i.item_id
                        JOIN effects e ON inv.effect_id = e.effect_id
                        WHERE inv.inventory_id = ? AND inv.user_id = ? AND inv.guild_id = ?
                    """, (inv_id, user_id, guild_id)) as cursor:
                        item_to_sell = await cursor.fetchone()

                    if not item_to_sell:
                        failed_to_sell_ids.append(str(inv_id))
                        logger.warning(f"User {user_id} tried to sell non-existent or not owned item (Inv ID: {inv_id})")
                        continue

                    base_name, base_rarity, effect_prefix, effect_rarity = item_to_sell
                    full_item_name = f"{effect_prefix}{base_name}"
                    sell_price = SELL_PRICES.get(base_rarity, 0)

                    async with self.bot.db.execute("SELECT equipped_weapon, equipped_armor FROM users WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)) as equip_cursor:
                        equipped_ids = await equip_cursor.fetchone()
                    if equipped_ids:
                        if inv_id == equipped_ids[0]:
                            await self.bot.db.execute("UPDATE users SET equipped_weapon = NULL WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
                            await self.manage_user_role(interaction.guild, interaction.user, "なし", "武器")
                        elif inv_id == equipped_ids[1]:
                            await self.bot.db.execute("UPDATE users SET equipped_armor = NULL WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
                            await self.manage_user_role(interaction.guild, interaction.user, "なし", "防具")

                    delete_cursor = await self.bot.db.execute("DELETE FROM inventory WHERE inventory_id = ? AND user_id = ? AND guild_id = ?", (inv_id, user_id, guild_id))
                    if delete_cursor.rowcount > 0:
                        sold_items_details.append(f"・ID:{inv_id} {full_item_name} ({base_rarity}/{effect_rarity}) - {sell_price}G")
                        total_sell_price += sell_price
                        accumulated_sell_price_for_tx += sell_price
                        logger.info(f"User {user_id} successfully queued item (Inv ID: {inv_id}) for selling. Price: {sell_price}G")
                    else:
                        failed_to_sell_ids.append(str(inv_id) + " (削除失敗)")
                        logger.warning(f"User {user_id} failed to delete item (Inv ID: {inv_id}) from inventory during selling.")

                if accumulated_sell_price_for_tx > 0:
                    new_gold = current_gold + accumulated_sell_price_for_tx
                    await self.bot.db.execute("UPDATE users SET gold = ? WHERE user_id = ? AND guild_id = ?", (new_gold, user_id, guild_id))

            result_description_parts = []
            if sold_items_details:
                result_description_parts.append(f"{len(sold_items_details)}個のアイテムを合計 {total_sell_price}G で売却しました。")
                result_description_parts.append(f"現在の所持ゴールド: {current_gold + total_sell_price}G")
                result_description_parts.append("\n**売却成功:**\n" + "\n".join(sold_items_details))
                embed_color = discord.Color.green()
            else:
                result_description_parts.append("指定されたアイテムの売却に失敗しました。")
                embed_color = discord.Color.orange()

            if failed_to_sell_ids:
                result_description_parts.append("\n**売却失敗/対象外ID:**\n・" + "\n・".join(failed_to_sell_ids))
                embed_color = discord.Color.orange() if not sold_items_details else discord.Color.yellow()

            embed = discord.Embed(
                title="アイテム売却結果",
                description="\n".join(result_description_parts),
                color=embed_color
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error during sell_cmd for user {user_id}, item_ids '{inventory_ids}': {e}", exc_info=True)
            error_embed = discord.Embed(title="エラー", description=f"アイテム売却処理中に予期せぬエラーが発生しました。\n`{str(e)}`", color=discord.Color.red())
            await interaction.followup.send(embed=error_embed, ephemeral=True)

    @discord.app_commands.command(name="vgacha", description="アイテムが手に入るガチャを引きます。")
    async def gacha_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        guild_id = interaction.guild.id
        async with self.bot.db.execute("SELECT gold FROM users WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)) as cursor:
            user_gold_row = await cursor.fetchone()
        current_gold = user_gold_row[0] if user_gold_row else 0

        embed = discord.Embed(
            title="ガチャへようこそ！",
            description=(
                "やあ、同志。運試しといこうか。\n"
                "何があっても自己責任だからな？\n"
                "引きたいガチャの種類を選んでくれ。支払いはもちろん先払いだ。\n"
            ),
            color=discord.Color.purple()
        )
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)

        embed.add_field(name="現在の所持ゴールド", value=f"{current_gold}G", inline=False)

        for gacha_key, gacha_info in self.gacha_settings.items():
            embed.add_field(
                name=f"{gacha_info['name']} - {gacha_info['cost_single']}G",
                value=gacha_info['description'],
                inline=False
            )
        
        view = GachaSelectView(
            bot=self.bot,
            rpg_cog=self,
            gacha_system=self.gacha_system,
            gacha_settings=self.gacha_settings,
            interaction_user_id=interaction.user.id
        )
        
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


    @discord.app_commands.command(name="vreset_rpg", description="RPGデータをリセット（開発者専用）")
    async def reset_rpg_cmd(self, interaction: discord.Interaction):
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message(embed=discord.Embed(title="エラー", description="このコマンドは開発者専用です！", color=discord.Color.red()), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            logger.info(f"RPG Data reset initiated by developer {interaction.user.id}")
            async with transaction(self.bot.db):
                await self.bot.db.execute("DROP TABLE IF EXISTS inventory")
                await self.bot.db.execute("DROP TABLE IF EXISTS users")
            await init_database(self.bot.db)
            logger.info(f"RPG Data reset completed by developer {interaction.user.id}")
            await interaction.followup.send(embed=discord.Embed(title="RPGデータリセット完了", description="ユーザーとインベントリのデータがリセットされました。\nアイテムと効果の基本データは維持または再初期化されました。", color=discord.Color.green()), ephemeral=True)
        except Exception as e:
            logger.error(f"Error during RPG data reset by developer {interaction.user.id}: {e}", exc_info=True)
            await interaction.followup.send(embed=discord.Embed(title="エラー", description=f"リセット中にエラーが発生しました: {str(e)}", color=discord.Color.red()), ephemeral=True)

    def load_enemy_data(self, enemy_name_base: str) -> Optional[dict]:
        """Loads enemy data from a JSON file."""
        file_path = os.path.join(ENEMY_DATA_PATH, f"{enemy_name_base}.json")
        logger.debug(f"Attempting to load enemy data from: {file_path}")
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    logger.info(f"Successfully loaded enemy data for {enemy_name_base}.")
                    return data
            except json.JSONDecodeError as e:
                logger.error(f"JSONDecodeError loading enemy data for {enemy_name_base} from {file_path}: {e}", exc_info=True)
                return None
            except Exception as e:
                logger.error(f"Unexpected error loading enemy data for {enemy_name_base} from {file_path}: {e}", exc_info=True)
                return None
        else:
            logger.warning(f"Enemy data file not found: {file_path}")
        return None

    def get_random_enemy_filename(self) -> Optional[str]:
        """Gets a random enemy filename (without .json) from the enemy data path."""
        if not os.path.exists(ENEMY_DATA_PATH) or not os.path.isdir(ENEMY_DATA_PATH):
            logger.error(f"Enemy data path not found or not a directory: {ENEMY_DATA_PATH}")
            return None
        enemy_files = [f for f in os.listdir(ENEMY_DATA_PATH) if f.endswith('.json')]
        if not enemy_files:
            logger.warning(f"No enemy JSON files found in {ENEMY_DATA_PATH}")
            return None
        chosen_file = random.choice(enemy_files)
        logger.info(f"Random enemy file chosen: {chosen_file}")
        return chosen_file.replace('.json', '')

    async def get_player_battle_stats(self, user_id: int, guild_id: int) -> Optional[dict]:
        """Retrieves player's battle stats (HP, ATK, DEF) based on level and equipment."""
        async with self.bot.db.execute("SELECT level, equipped_weapon, equipped_armor FROM users WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)) as cursor:
            user_base_stats = await cursor.fetchone()
        if not user_base_stats:
            logger.warning(f"Player battle stats not found for user {user_id} in guild {guild_id}.")
            try:
                 async with transaction(self.bot.db):
                    await self.bot.db.execute("INSERT INTO users (user_id, guild_id, level, total_characters, gold) VALUES (?, ?, ?, ?, ?)", (user_id, guild_id, 0, 0, 0))
                 logger.info(f"Created new user entry for {user_id} in guild {guild_id} from get_player_battle_stats.")
                 return await self.get_player_battle_stats(user_id, guild_id)
            except Exception as e:
                 logger.error(f"Failed to create new user {user_id} in get_player_battle_stats: {e}")
                 return None


        level, equipped_weapon_id, equipped_armor_id = user_base_stats
        player_hp = min(level + 10, 1000)
        player_atk = 0
        player_def = 0

        if equipped_weapon_id:
            async with self.bot.db.execute("""
                SELECT i.base_attack, i.base_defense, e.attack_bonus, e.defense_bonus
                FROM inventory inv
                JOIN items i ON inv.item_id = i.item_id
                JOIN effects e ON inv.effect_id = e.effect_id
                WHERE inv.inventory_id = ?
            """, (equipped_weapon_id,)) as cur:
                weapon_stats = await cur.fetchone()
                if weapon_stats:
                    player_atk += weapon_stats[0] + weapon_stats[2]
                    player_def += weapon_stats[1] + weapon_stats[3]
                else:
                    logger.warning(f"Equipped weapon (inv_id: {equipped_weapon_id}) stats not found for user {user_id}.")


        if equipped_armor_id:
            async with self.bot.db.execute("""
                SELECT i.base_attack, i.base_defense, e.attack_bonus, e.defense_bonus
                FROM inventory inv
                JOIN items i ON inv.item_id = i.item_id
                JOIN effects e ON inv.effect_id = e.effect_id
                WHERE inv.inventory_id = ?
            """, (equipped_armor_id,)) as cur:
                armor_stats = await cur.fetchone()
                if armor_stats:
                    player_atk += armor_stats[0] + armor_stats[2]
                    player_def += armor_stats[1] + armor_stats[3]
                else:
                    logger.warning(f"Equipped armor (inv_id: {equipped_armor_id}) stats not found for user {user_id}.")

        logger.info(f"Player {user_id} battle stats: HP={player_hp}, ATK={player_atk}, DEF={player_def}, Level={level}")
        return {"hp": player_hp, "atk": player_atk, "def": player_def, "level": level}

    async def _start_battle_logic(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        guild_id = interaction.guild.id

        if user_id in self.active_battles:
            logger.warning(f"Starting new battle for {user_id}, clearing previous active session.")
            del self.active_battles[user_id]
        
        player_stats = await self.get_player_battle_stats(user_id, guild_id)
        if not player_stats:
            logger.error(f"Failed to retrieve player stats for user {user_id} in guild {guild_id} for battle.")
            await interaction.edit_original_response(content="戦闘を開始できませんでした。あなたのRPG情報が見つかりません。", embed=None, view=None)
            return

        enemy_filename_base = self.get_random_enemy_filename()
        if not enemy_filename_base:
            logger.error("No enemy files found to start a battle.")
            await interaction.edit_original_response(content="戦闘を開始できませんでした。戦うべき敵が見つかりません！", embed=None, view=None)
            return

        enemy_data = self.load_enemy_data(enemy_filename_base)
        if not enemy_data:
            logger.error(f"Failed to load enemy data for '{enemy_filename_base}'.")
            await interaction.edit_original_response(content=f"戦闘を開始できませんでした。敵「{enemy_filename_base}」のデータの読み込みに失敗しました。", embed=None, view=None)
            return

        battle_session = BattleSession(self.bot, interaction, player_stats, enemy_data, self)
        self.active_battles[user_id] = battle_session
        try:
            await battle_session.start_battle()
        except Exception as e:
            logger.error(f"Error during battle_session.start_battle() for user {user_id}: {e}", exc_info=True)
            try:
                await interaction.edit_original_response(content="戦闘の開始中に予期せぬエラーが発生しました。", embed=None, view=None)
            except discord.NotFound:
                await interaction.channel.send("戦闘の開始中に予期せぬエラーが発生しました。")
            if user_id in self.active_battles:
                del self.active_battles[user_id]

    @discord.app_commands.command(name="vbattle", description="ランダムな敵と戦闘を開始します！")
    async def battle_cmd(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if user_id in self.active_battles:
            await interaction.response.send_message("あなたは既に別の戦闘中です！", ephemeral=True)
            logger.warning(f"User {user_id} tried to start a battle while already in one.")
            return

        bot_name = self.bot.user.display_name if self.bot.user else "ソフィア"
        await interaction.response.send_message(f"… {bot_name} が考え中…", ephemeral=False)
        await self._start_battle_logic(interaction)


async def setup(bot):
    await bot.add_cog(RPG(bot))
