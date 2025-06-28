# gacha_system.py
import discord
import random
import logging
from typing import Dict, List, Tuple, Optional, TYPE_CHECKING

from rpg_data import (
    SELL_PRICES, RARITY_ORDER, RARITY_WEIGHTS, TOTAL_RARITY_WEIGHT,
    INVENTORY_LIMIT, RARITY_PROBABILITIES
)
from rpg_views import GachaResultView
from rpg_utils import transaction

if TYPE_CHECKING:
    from RPG_cog import RPG

logger = logging.getLogger('SophiaBot.GachaSystem')

GACHA_SETTINGS: Dict[str, Dict] = {
    "junk": {
        "name": "ジャンクパーツ詰め合わせ",
        "cost_single": 5000,
        "rarity_pool": {"common": 0.60, "uncommon": 0.29, "rare": 0.10, "epic": 0.01},
        "guaranteed_rarity_above_single": None,
        "description": (
            "```\n"
            "ガラクタの山から掘り出し物が見つかるかも？ハードオフよりはマシ\n\n"
            "ベースアイテム提供割合:\n"
            "コモン:　　　　　60.000%\n"
            "アンコモン:　　　29.000%\n"
            "レア:　　　　　　10.000%\n"
            "エピック:　　 　　1.000%\n\n"
            "(効果のレアリティは上記とは別に抽選されます)\n"
            "```\n\n"
        )
    },
    "influencer": {
        "name": "インフルエンサーおすすめセット",
        "cost_single": 30000,
        "rarity_pool": {"uncommon": 0.50, "rare": 0.33, "epic": 0.15, "legendary": 0.02},
        "guaranteed_rarity_above_single": "uncommon",
        "description": (
            "```\n"
            "あの人も使ってる最新トレンドアイテム！（PR案件）\n\n"
            "ベースアイテム提供割合:\n"
            "アンコモン:　　　50.000%\n"
            "レア:　　　　　　33.000%\n"
            "エピック:　　　　15.000%\n"
            "レジェンダリー:　 2.000%\n\n"
            "(効果のレアリティは上記とは別に抽選されます)\n"
            "```\n\n"
        )
    },
    "vip": {
        "name": "VIP供給品",
        "cost_single": 100000,
        "rarity_pool": {"legendary": 0.70, "mythic": 0.25, "unique": 0.05},
        "guaranteed_rarity_above_single": "legendary",
        "description": (
            "```\n"
            "選ばれし者のみが手にできる至高の逸品。\n\n"
            "ベースアイテム提供割合:\n"
            "レジェンダリー:　70.000%\n"
            "ミシック:　　　　25.000%\n"
            "ユニーク:　　　　 5.000%\n\n"
            "(効果のレアリティは上記とは別に抽選されます)\n"
            "```"
        )
    }
}

class GachaSystem:
    def __init__(self, bot, rpg_cog: 'RPG'):
        self.bot = bot
        self.rpg_cog = rpg_cog
        self.gacha_settings = GACHA_SETTINGS

    async def _draw_single_item(self, gacha_type_key: str) -> Optional[Tuple[int, str, str, str, int, str, str]]:
        if gacha_type_key not in self.gacha_settings:
            logger.error(f"Unknown gacha type in _draw_single_item: {gacha_type_key}")
            return None

        gacha_info = self.gacha_settings[gacha_type_key]
        base_item_rarity_pool = gacha_info["rarity_pool"]
        guaranteed_rarity_threshold = gacha_info.get("guaranteed_rarity_above_single")

        chosen_base_rarity: Optional[str] = None
        current_抽選プール: Dict[str, float] = {}

        if guaranteed_rarity_threshold:
            for rarity, weight in base_item_rarity_pool.items():
                if RARITY_ORDER.get(rarity, 0) >= RARITY_ORDER.get(guaranteed_rarity_threshold, 0):
                    current_抽選プール[rarity] = weight
            if not current_抽選プール:
                logger.warning(f"Gacha '{gacha_type_key}' guaranteed threshold '{guaranteed_rarity_threshold}' not met by pool. Using full defined pool.")
                current_抽選プール = base_item_rarity_pool
        else:
            current_抽選プール = base_item_rarity_pool

        total_current_weight = sum(current_抽選プール.values())
        if total_current_weight > 0 and current_抽選プール:
            chosen_base_rarity = random.choices(list(current_抽選プール.keys()), weights=list(current_抽選プール.values()), k=1)[0]
        else:
            logger.error(f"Gacha '{gacha_type_key}' has a total weight of 0 or empty pool for its current rarity pool ({current_抽選プール}). Cannot draw base rarity.")
            if TOTAL_RARITY_WEIGHT > 0 and RARITY_WEIGHTS:
                chosen_base_rarity = random.choices(list(RARITY_WEIGHTS.keys()), weights=list(RARITY_WEIGHTS.values()), k=1)[0]
                logger.warning(f"Fell back to global rarity weights, chose: {chosen_base_rarity}")
            else:
                logger.critical(f"CRITICAL: Global RARITY_WEIGHTS are also invalid. Cannot determine base rarity for '{gacha_type_key}'.")
                return None

        if not chosen_base_rarity:
             logger.critical(f"CRITICAL: Failed to determine chosen_base_rarity for gacha '{gacha_type_key}' after fallbacks.")
             return None

        new_item_type = random.choice(["weapon", "armor"])
        new_item_type_display = "武器" if new_item_type == "weapon" else "防具"
        async with self.bot.db.execute("SELECT item_id, base_name FROM items WHERE type = ? AND rarity = ? ORDER BY RANDOM() LIMIT 1", (new_item_type, chosen_base_rarity)) as cursor:
            item_row = await cursor.fetchone()

        if not item_row:
            logger.error(f"No base item found for gacha '{gacha_type_key}', type: {new_item_type}, chosen_rarity: {chosen_base_rarity}. Attempting fallback to 'common'.")
            async with self.bot.db.execute("SELECT item_id, base_name FROM items WHERE type = ? AND rarity = 'common' ORDER BY RANDOM() LIMIT 1", (new_item_type,)) as fallback_cursor:
                item_row = await fallback_cursor.fetchone()
            if not item_row:
                logger.error(f"Fallback failed: No common {new_item_type} found. Cannot provide item for this draw.")
                return None
            new_item_base_id, new_item_base_name = item_row
            original_chosen_rarity = chosen_base_rarity
            chosen_base_rarity = "common"
            logger.info(f"Fell back to common item: {new_item_base_name} (original intended rarity: {original_chosen_rarity})")
        else:
            new_item_base_id, new_item_base_name = item_row

        effect_rarity_pool = gacha_info["rarity_pool"]
        total_effect_pool_weight = sum(effect_rarity_pool.values())
        chosen_effect_rarity: Optional[str] = None

        if total_effect_pool_weight > 0 and effect_rarity_pool:
            chosen_effect_rarity = random.choices(list(effect_rarity_pool.keys()), weights=list(effect_rarity_pool.values()), k=1)[0]
        else:
            logger.warning(f"Gacha '{gacha_type_key}' has total weight 0 or empty pool for effect rarity. Fallback to global weights.")
            if TOTAL_RARITY_WEIGHT > 0 and RARITY_WEIGHTS:
                chosen_effect_rarity = random.choices(list(RARITY_WEIGHTS.keys()), weights=list(RARITY_WEIGHTS.values()), k=1)[0]
                logger.warning(f"Fell back to global rarity weights for effect, chose: {chosen_effect_rarity}")
            else:
                logger.critical(f"CRITICAL: Global RARITY_WEIGHTS also invalid for effect. Cannot determine effect rarity for '{gacha_type_key}'.")
                chosen_effect_rarity = "N/A" # 効果レアリティ不明

        if not chosen_effect_rarity or chosen_effect_rarity == "N/A":
            logger.warning(f"Effect rarity determination failed for gacha '{gacha_type_key}'. Assigning 'no effect'.")
            async with self.bot.db.execute("SELECT effect_id, prefix_name FROM effects WHERE effect_id = 0") as c_no_effect:
                no_effect_row = await c_no_effect.fetchone()
            if no_effect_row: new_effect_id, new_effect_name_prefix = no_effect_row
            else: new_effect_id, new_effect_name_prefix = 0, ""
            chosen_effect_rarity = "N/A"
        else:
            async with self.bot.db.execute("SELECT effect_id, prefix_name FROM effects WHERE rarity = ? ORDER BY RANDOM() LIMIT 1", (chosen_effect_rarity,)) as cursor:
                effect_row = await cursor.fetchone()
            if not effect_row:
                logger.warning(f"No effect found for chosen rarity '{chosen_effect_rarity}' in gacha '{gacha_type_key}'. Assigning 'no effect'.")
                async with self.bot.db.execute("SELECT effect_id, prefix_name FROM effects WHERE effect_id = 0") as c_no_effect:
                    no_effect_row = await c_no_effect.fetchone()
                if no_effect_row: new_effect_id, new_effect_name_prefix = no_effect_row
                else: new_effect_id, new_effect_name_prefix = 0, ""
                chosen_effect_rarity = "N/A"
            else:
                new_effect_id, new_effect_name_prefix = effect_row

        logger.info(f"Gacha draw successful for '{gacha_type_key}': {new_effect_name_prefix or ''}{new_item_base_name} (Item: {chosen_base_rarity}, Effect: {chosen_effect_rarity})")
        return (new_item_base_id, new_item_base_name, chosen_base_rarity, new_item_type_display,
                new_effect_id, new_effect_name_prefix or "", chosen_effect_rarity)

    async def execute_gacha_draw(self, interaction: discord.Interaction, gacha_type_key: str, num_draws: int):
        """
        ユーザーがガチャの種類を選んだ後に呼び出される実際のガチャ処理。
        interaction は既に ephemeral=True で defer されている想定。
        num_draws は常に1 (単発ガチャのみのため)。
        """
        user_id = interaction.user.id
        guild_id = interaction.guild.id

        if num_draws != 1:
            logger.error(f"User {user_id} attempted gacha with num_draws={num_draws}. This system is single-draw only. Forcing to 1.")
            num_draws = 1 # 強制的に単発にする

        if gacha_type_key not in self.gacha_settings:
            logger.warning(f"User {user_id} attempted to draw unknown gacha type: {gacha_type_key}")
            await interaction.followup.send("あれ？そんな名前のガチャは知らないなぁ…。もしかして異世界のガチャ？", ephemeral=True)
            return

        gacha_info = self.gacha_settings[gacha_type_key]
        cost = gacha_info["cost_single"]
        required_inventory_space = 1

        async with self.bot.db.execute("SELECT gold FROM users WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)) as cursor:
            user_gold_row = await cursor.fetchone()
        current_gold = user_gold_row[0] if user_gold_row else 0

        if current_gold < cost:
            await interaction.followup.send(
                f"おっと、**{gacha_info['name']}** を引くには **{cost}G** 必要だけど、君は今 **{current_gold}G** しか持ってないみたいだね。\n"
                "もう少し懐を温めてから、また挑戦しに来ておくれ！世の中そんなに甘くないのさ。",
                ephemeral=True
            )
            return

        async with self.bot.db.execute("SELECT COUNT(*) FROM inventory WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)) as cursor:
            count_row = await cursor.fetchone()
        current_inventory_count = count_row[0] if count_row else 0
        available_slots = self.rpg_cog.inventory_limit - current_inventory_count

        if available_slots < required_inventory_space:
            await interaction.followup.send(
                f"アイテムを受け取るには、インベントリに少なくとも **1個** の空きが必要みたいだよ。\n"
                f"今の空きは **{available_slots}個** だけだからね。\n"
                "`/vsell` コマンドで持ち物を整理して、スペースを確保してから再挑戦だ！じゃないと、せっかくの戦利品が虚空に消えちゃうよ？",
                ephemeral=True
            )
            return

        try:
            async with transaction(self.bot.db):
                await self.bot.db.execute("UPDATE users SET gold = gold - ? WHERE user_id = ? AND guild_id = ?", (cost, user_id, guild_id))
            logger.info(f"User {user_id} spent {cost}G on gacha: {gacha_type_key} x1")
        except Exception as e:
            logger.error(f"Gacha coin deduction error for user {user_id} (gacha: {gacha_type_key}): {e}", exc_info=True)
            await interaction.followup.send("おっと、コインの処理でエラーが起きちゃったみたいだ…。サーバーが混み合ってるのかも？\nごめんね、もう一度試してみて。", ephemeral=True)
            return

        drawn_items_details: List[Optional[Tuple[int, str, str, str, int, str, str]]] = []
        item_tuple = await self._draw_single_item(gacha_type_key)
        drawn_items_details.append(item_tuple)

        if not any(drawn_items_details):
            logger.warning(f"Gacha for user {user_id} (gacha: {gacha_type_key} x1) resulted in all None items.")
            await interaction.followup.send(
                "うーん、今回は残念ながら何も出なかったみたい…まるで蜃気楼だったね！\n"
                f"（消費した {cost}G は勉強代ってことで！運営の気まぐれかもしれないし、また挑戦してみて！）",
                ephemeral=True
            )
            return

        gacha_result_view = GachaResultView(
            bot=self.bot,
            rpg_cog=self.rpg_cog,
            user_id=user_id,
            guild_id=guild_id,
            drawn_items_details=drawn_items_details,
            gacha_name=gacha_info["name"],
            rarity_probabilities=RARITY_PROBABILITIES,
            rarity_weights=RARITY_WEIGHTS,
            total_rarity_weight=TOTAL_RARITY_WEIGHT,
            sell_prices=SELL_PRICES,
            inventory_limit=INVENTORY_LIMIT
        )
        # interaction (元のコマンドの Interaction オブジェクト) を渡す
        await gacha_result_view.send_initial_message(interaction)
