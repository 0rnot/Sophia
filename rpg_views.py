# rpg_views.py
import discord
import logging
import asyncio
from typing import Optional, List, TYPE_CHECKING
from rpg_data import SELL_PRICES, RARITY_PROBABILITIES, RARITY_ORDER, RARITY_WEIGHTS, TOTAL_RARITY_WEIGHT
from rpg_utils import transaction

if TYPE_CHECKING:
    from RPG_cog import RPG, BattleSession

logger = logging.getLogger('SophiaBot.RPGViews')

class EquipConfirmView(discord.ui.View):
    def __init__(self, bot, inventory_id, full_item_name: str, item_type_display: str, equip_field: str, interaction_user_id: int):
        super().__init__(timeout=60)
        self.bot = bot
        self.inventory_id = inventory_id
        self.full_item_name = full_item_name
        self.item_type_display = item_type_display
        self.equip_field = equip_field
        self.interaction_user_id = interaction_user_id
        self.rpg_cog: 'RPG' = self.bot.get_cog("RPG")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("この操作はコマンドを実行した本人のみ可能です。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="入れ替える", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        guild_id = interaction.guild.id
        try:
            if not self.rpg_cog:
                logger.error("RPGCog not found in EquipConfirmView confirm.")
                raise Exception("RPGCog not found")

            async with transaction(self.bot.db):
                await self.bot.db.execute(f"UPDATE users SET {self.equip_field} = ? WHERE user_id = ? AND guild_id = ?", (self.inventory_id, user_id, guild_id))

            await self.rpg_cog.manage_user_role(interaction.guild, interaction.user, self.full_item_name, self.item_type_display)

            embed = discord.Embed(title="装備入れ替え完了", description=f"**{self.full_item_name}** を装備しました。", color=discord.Color.green())
            await interaction.edit_original_response(embed=embed, view=None)
        except Exception as e:
            logger.error(f"Error in EquipConfirmView confirm: {e}", exc_info=True)
            embed = discord.Embed(title="エラー", description="装備の入れ替え中にエラーが発生しました。", color=discord.Color.red())
            try:
                await interaction.edit_original_response(embed=embed, view=None)
            except discord.errors.NotFound:
                pass
        finally:
            self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(title="キャンセル", description="装備の入れ替えをキャンセルしました。", color=discord.Color.red())
        try:
            await interaction.edit_original_response(embed=embed, view=None)
        except discord.errors.NotFound:
            pass
        finally:
            self.stop()

class InventorySwapView(discord.ui.View):
    def __init__(self, bot, user_id, guild_id,
                 new_item_base_id, new_item_base_name, new_item_base_rarity, new_item_type_display,
                 new_effect_id, new_effect_name_prefix, new_effect_rarity,
                 inventory_items_tuples,
                 interaction_user_id: int,
                 is_inventory_full: bool):
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id
        self.guild_id = guild_id
        self.new_item_base_id = new_item_base_id
        self.new_item_base_name = new_item_base_name
        self.new_item_base_rarity = new_item_base_rarity
        self.new_item_type_display = new_item_type_display
        self.new_effect_id = new_effect_id
        self.new_effect_name_prefix = new_effect_name_prefix
        self.new_effect_rarity = new_effect_rarity
        self.inventory_items_full_list = inventory_items_tuples
        self.interaction_user_id = interaction_user_id
        self.new_full_item_name = f"{self.new_effect_name_prefix}{self.new_item_base_name}"
        self.message_with_view: Optional[discord.Message] = None
        self.level_up_message_to_delete: Optional[discord.Message] = None

        self.acquire_button = discord.ui.Button(
            label=f"{self.new_full_item_name[:20]} ({self.new_item_base_rarity}/{self.new_effect_rarity}) を取得",
            style=discord.ButtonStyle.green,
            row=0,
            disabled=is_inventory_full
        )
        self.acquire_button.callback = self.acquire_button_callback
        self.add_item(self.acquire_button)

        self.sell_button = discord.ui.Button(
            label=f"{self.new_full_item_name[:20]} ({self.new_item_base_rarity}/{self.new_effect_rarity}) を売却",
            style=discord.ButtonStyle.danger,
            row=1
        )
        self.sell_button.callback = self.sell_button_callback
        self.add_item(self.sell_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message(f"この操作は <@{self.interaction_user_id}> さんのみ可能です。", ephemeral=True)
            return False
        return True

    async def _delete_associated_messages(self):
        """Deletes the view's own message and the associated level-up message if present."""
        if self.message_with_view:
            try:
                await self.message_with_view.delete()
                logger.info(f"InventorySwapView: Successfully deleted view message (ID: {self.message_with_view.id}).")
            except discord.errors.NotFound:
                logger.warning(f"InventorySwapView: View message (ID: {self.message_with_view.id}) not found for deletion.")
            except Exception as e:
                logger.error(f"InventorySwapView: Error deleting view message (ID: {self.message_with_view.id}): {e}", exc_info=True)
            finally:
                self.message_with_view = None
        
        if self.level_up_message_to_delete:
            try:
                await self.level_up_message_to_delete.delete()
                logger.info(f"InventorySwapView: Successfully deleted level up message (ID: {self.level_up_message_to_delete.id}).")
            except discord.errors.NotFound:
                logger.warning(f"InventorySwapView: Level up message (ID: {self.level_up_message_to_delete.id}) not found for deletion.")
            except Exception as e:
                logger.error(f"InventorySwapView: Error deleting level up message (ID: {self.level_up_message_to_delete.id}): {e}", exc_info=True)
            finally:
                self.level_up_message_to_delete = None


    async def acquire_button_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with transaction(self.bot.db):
                await self.bot.db.execute(
                    "INSERT INTO inventory (user_id, guild_id, item_id, effect_id) VALUES (?, ?, ?, ?)",
                    (self.user_id, self.guild_id, self.new_item_base_id, self.new_effect_id)
                )

            prob_item = (RARITY_WEIGHTS.get(self.new_item_base_rarity, 0) / TOTAL_RARITY_WEIGHT) if TOTAL_RARITY_WEIGHT > 0 else 0
            prob_effect = (RARITY_WEIGHTS.get(self.new_effect_rarity, 0) / TOTAL_RARITY_WEIGHT) if TOTAL_RARITY_WEIGHT > 0 else 0
            combined_prob_percent = prob_item * prob_effect * 100

            embed = discord.Embed(
                title="アイテム取得完了",
                description=(
                    f"**{self.new_full_item_name}** (装備:{self.new_item_base_rarity}/効果:{self.new_effect_rarity}) をインベントリに追加しました。\n"
                    f"　┣ 装備レアリティ: {self.new_item_base_rarity} ({RARITY_PROBABILITIES.get(self.new_item_base_rarity, 'N/A')})\n"
                    f"　┣ 効果レアリティ: {self.new_effect_rarity} ({RARITY_PROBABILITIES.get(self.new_effect_rarity, 'N/A')})\n"
                    f"　┗ 組み合わせ出現率: {combined_prob_percent:.3f}%"
                ),
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"InventorySwapView - acquire_button error: {e}", exc_info=True)
            await interaction.followup.send(f"アイテム取得処理中にエラーが発生しました: {str(e)}", ephemeral=True)
        finally:
            await self._delete_associated_messages()
            self.stop()

    async def sell_button_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            sell_price = SELL_PRICES.get(self.new_item_base_rarity, 0)
            async with self.bot.db.execute("SELECT gold FROM users WHERE user_id = ? AND guild_id = ?", (self.user_id, self.guild_id)) as cursor:
                row = await cursor.fetchone()
            current_gold = row[0] if row else 0
            new_gold = current_gold + sell_price

            async with transaction(self.bot.db):
                await self.bot.db.execute("UPDATE users SET gold = ? WHERE user_id = ? AND guild_id = ?", (new_gold, self.user_id, self.guild_id))

            embed = discord.Embed(
                title="アイテム売却完了",
                description=f"**{self.new_full_item_name}** (装備:{self.new_item_base_rarity}/効果:{self.new_effect_rarity}) を売却し、{sell_price} ゴールドを獲得しました。\n"
                            "インベントリに変更はありません。",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"InventorySwapView - sell_button error: {e}", exc_info=True)
            await interaction.followup.send("アイテム売却処理中にエラーが発生しました。", ephemeral=True)
        finally:
            await self._delete_associated_messages()
            self.stop()

class RerollSelectView(discord.ui.View):
    def __init__(self, bot, inventory_id_to_reroll, new_effect_id, new_effect_name_prefix, new_effect_rarity, consumable_items, target_item_base_rarity, interaction_user_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.inventory_id_to_reroll = inventory_id_to_reroll
        self.new_effect_id = new_effect_id
        self.new_effect_name_prefix = new_effect_name_prefix
        self.new_effect_rarity = new_effect_rarity
        self.consumable_items = consumable_items
        self.target_item_base_rarity = target_item_base_rarity
        self.interaction_user_id = interaction_user_id

        options = [
            discord.SelectOption(
                label=f"ID:{item[0]} | {item[5]}{item[1][:20]} ({item[3]}/{item[6]})",
                value=str(item[0])
            ) for item in consumable_items[:25]
        ]
        self.select_menu = discord.ui.Select(
            placeholder=f"{self.target_item_base_rarity}の装備を5個選択してください",
            options=options if options else [discord.SelectOption(label="選択可能なアイテムなし", value="no_op_placeholder")],
            min_values=5,
            max_values=5,
            disabled=not options or len(options) < 5
        )
        self.select_menu.callback = self.select_callback
        self.add_item(self.select_menu)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("この操作はコマンドを実行した本人のみ可能です。", ephemeral=True)
            return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not self.select_menu.values or self.select_menu.values[0] == "no_op_placeholder":
            try:
                await interaction.edit_original_response(content="有効なアイテムが選択されていません。", embed=None, view=None)
            except discord.errors.NotFound:
                pass
            self.stop()
            return

        user_id = interaction.user.id
        guild_id = interaction.guild.id
        selected_ids_to_consume = [int(value) for value in self.select_menu.values]

        try:
            async with transaction(self.bot.db):
                placeholders = ', '.join('?' for _ in selected_ids_to_consume)
                delete_cursor = await self.bot.db.execute(
                    f"DELETE FROM inventory WHERE inventory_id IN ({placeholders}) AND user_id = ? AND guild_id = ?",
                    (*selected_ids_to_consume, user_id, guild_id)
                )
                if delete_cursor.rowcount != 5:
                    logger.warning(f"Reroll: Expected to delete 5 items, but deleted {delete_cursor.rowcount} for user {user_id}.")
                    raise Exception(f"消費アイテムの削除に失敗しました。{delete_cursor.rowcount}個しか削除できませんでした。")

                update_cursor = await self.bot.db.execute(
                    "UPDATE inventory SET effect_id = ? WHERE inventory_id = ? AND user_id = ? AND guild_id = ?",
                    (self.new_effect_id, self.inventory_id_to_reroll, user_id, guild_id)
                )
                if update_cursor.rowcount == 0:
                    logger.warning(f"Reroll: Failed to update effect for item {self.inventory_id_to_reroll} for user {user_id}.")
                    raise Exception("リロール対象のアイテムの効果更新に失敗しました。")

            async with self.bot.db.execute("""
                SELECT i.base_name, e.prefix_name
                FROM inventory inv
                JOIN items i ON inv.item_id = i.item_id
                JOIN effects e ON inv.effect_id = e.effect_id
                WHERE inv.inventory_id = ?
            """, (self.inventory_id_to_reroll,)) as cursor:
                rerolled_item_names = await cursor.fetchone()
            rerolled_full_name = f"{rerolled_item_names[1]}{rerolled_item_names[0]}" if rerolled_item_names else "不明なアイテム"

            embed = discord.Embed(
                title="効果を更新",
                description=f"アイテム「{rerolled_full_name}」の効果を **{self.new_effect_name_prefix}** (効果レアリティ: {self.new_effect_rarity}) に更新しました！\n"
                            f"（{self.target_item_base_rarity}装備5個を消費）",
                color=discord.Color.green()
            )
            await interaction.edit_original_response(embed=embed, view=None)
        except Exception as e:
            logger.error(f"RerollSelectView - select_callback error: {e}", exc_info=True)
            error_embed = discord.Embed(title="エラー", description=f"リロール処理中にエラーが発生しました: {str(e)}", color=discord.Color.red())
            try:
                await interaction.edit_original_response(embed=error_embed, view=None)
            except discord.errors.NotFound:
                await interaction.followup.send(embed=error_embed, ephemeral=True)
        finally:
            self.stop()

class InventoryEmbedView(discord.ui.View):
    def __init__(self, items_data: List, items_per_page: int, user_name: str, inventory_limit: int, gold: int, rarity_probabilities: dict,
                 initial_interaction: discord.Interaction, current_sort_order="id_asc", is_ephemeral=False, user_avatar_url=None):
        super().__init__(timeout=180)
        self.all_items_data_orig = list(items_data)
        self.items_per_page = items_per_page
        self.user_name = user_name
        self.user_avatar_url = user_avatar_url
        self.inventory_limit = inventory_limit
        self.gold = gold
        self.rarity_probabilities = rarity_probabilities
        self.rarity_weights = RARITY_WEIGHTS
        self.total_rarity_weight = TOTAL_RARITY_WEIGHT
        self.interaction_to_edit = initial_interaction
        self.current_sort_order = current_sort_order
        self.is_ephemeral = is_ephemeral
        self.sorted_items_data = []

        self.current_page = 0
        self._sort_items()
        self.total_pages = (len(self.sorted_items_data) - 1) // self.items_per_page + 1
        if self.total_pages == 0: self.total_pages = 1

        self.prev_button = discord.ui.Button(label="◀ 前へ", style=discord.ButtonStyle.grey, disabled=True, row=0)
        self.prev_button.callback = self.prev_page_callback
        self.add_item(self.prev_button)

        self.page_label = discord.ui.Button(label=f"1/{self.total_pages}", style=discord.ButtonStyle.secondary, disabled=True, row=0)
        self.add_item(self.page_label)

        self.next_button = discord.ui.Button(label="次へ ▶", style=discord.ButtonStyle.grey, disabled=self.total_pages <= 1, row=0)
        self.next_button.callback = self.next_page_callback
        self.add_item(self.next_button)

        self.sort_id_button = discord.ui.Button(label="ID順", style=discord.ButtonStyle.primary if current_sort_order=="id_asc" else discord.ButtonStyle.secondary, row=1)
        self.sort_id_button.callback = self.sort_by_id_callback
        self.add_item(self.sort_id_button)

        self.sort_rarity_asc_button = discord.ui.Button(label="レア度昇順", style=discord.ButtonStyle.primary if current_sort_order=="rarity_asc" else discord.ButtonStyle.secondary, row=1)
        self.sort_rarity_asc_button.callback = self.sort_by_rarity_asc_callback
        self.add_item(self.sort_rarity_asc_button)

        self.sort_rarity_desc_button = discord.ui.Button(label="レア度降順", style=discord.ButtonStyle.primary if current_sort_order=="rarity_desc" else discord.ButtonStyle.secondary, row=1)
        self.sort_rarity_desc_button.callback = self.sort_by_rarity_desc_callback
        self.add_item(self.sort_rarity_desc_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.interaction_to_edit.user.id:
            await interaction.response.send_message("このインベントリの操作は表示させた本人のみ可能です。", ephemeral=True)
            return False
        return True

    def _sort_items(self):
        """Sorts the items based on the current_sort_order."""
        if self.current_sort_order == "rarity_asc":
            self.sorted_items_data = sorted(self.all_items_data_orig, key=lambda x: (RARITY_ORDER.get(x[3], 0) + RARITY_ORDER.get(x[6], 0), x[0]))
        elif self.current_sort_order == "rarity_desc":
            self.sorted_items_data = sorted(self.all_items_data_orig, key=lambda x: (RARITY_ORDER.get(x[3], 0) + RARITY_ORDER.get(x[6], 0), -x[0]), reverse=True)
        else:
            self.sorted_items_data = sorted(self.all_items_data_orig, key=lambda x: x[0])
        self.current_page = 0
        self.total_pages = (len(self.sorted_items_data) - 1) // self.items_per_page + 1
        if self.total_pages == 0: self.total_pages = 1

    def _calculate_combined_probability_str(self, item_base_rarity, effect_rarity):
        """Calculates and formats the combined probability string for an item."""
        item_weight = self.rarity_weights.get(item_base_rarity, 0)
        effect_weight = self.rarity_weights.get(effect_rarity, 0)
        if self.total_rarity_weight == 0: return "計算不可"

        prob_item = item_weight / self.total_rarity_weight
        prob_effect = effect_weight / self.total_rarity_weight
        combined_prob = prob_item * prob_effect
        return f"{combined_prob * 100:.3f}%"


    def _create_page_embed(self):
        embed = discord.Embed(
            title=f"{self.user_name} のインベントリ ({len(self.all_items_data_orig)}/{self.inventory_limit}) - ページ {self.current_page + 1}/{self.total_pages}",
            color=discord.Color.blue()
        )
        if self.user_avatar_url:
            embed.set_thumbnail(url=self.user_avatar_url)

        sort_map = {"id_asc": "ID昇順", "rarity_asc": "レア度総合昇順", "rarity_desc": "レア度総合降順"}
        embed.description = f"ソート順: {sort_map.get(self.current_sort_order, 'ID昇順')}"
        embed.add_field(name="所持ゴールド", value=f"{self.gold} ゴールド", inline=False)

        start_index = self.current_page * self.items_per_page
        end_index = start_index + self.items_per_page
        page_items_data = self.sorted_items_data[start_index:end_index]

        if not self.sorted_items_data:
            embed.add_field(name="アイテム", value="インベントリは空です。", inline=False)
        elif not page_items_data:
             embed.add_field(name="アイテム", value="このページにアイテムはありません。", inline=False)
        else:
            field_value = ""
            FIELD_VALUE_LIMIT = 1020
            separator = "\n\n"

            for item_data in page_items_data:
                full_item_name = f"{item_data[5]}{item_data[1]}"
                combined_prob_str = self._calculate_combined_probability_str(item_data[3], item_data[6])
                item_str = (
                    f"**ID: {item_data[0]}** | {full_item_name}\n"
                    f"　種別: {'武器' if item_data[2] == 'weapon' else '防具'} | 装備レアリティ: {item_data[3]}\n"
                    f"　効果レアリティ: {item_data[6]} | 出現確率: {combined_prob_str}\n"
                    f"　ATK: {item_data[8] + item_data[10]} | DEF: {item_data[9] + item_data[11]}"
                )
                if len(field_value) + len(item_str) + (len(separator) if field_value else 0) > FIELD_VALUE_LIMIT:
                    field_value += "\n...（このページの続きは表示しきれません）"
                    break
                if field_value:
                    field_value += separator
                field_value += item_str
            embed.add_field(name=f"アイテム (表示数: {len(page_items_data)})", value=field_value if field_value else "なし", inline=False)
        return embed

    async def _update_view_and_buttons(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=self.is_ephemeral)

        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1
        self.page_label.label = f"{self.current_page + 1}/{self.total_pages}"

        self.sort_id_button.style = discord.ButtonStyle.primary if self.current_sort_order == "id_asc" else discord.ButtonStyle.secondary
        self.sort_rarity_asc_button.style = discord.ButtonStyle.primary if self.current_sort_order == "rarity_asc" else discord.ButtonStyle.secondary
        self.sort_rarity_desc_button.style = discord.ButtonStyle.primary if self.current_sort_order == "rarity_desc" else discord.ButtonStyle.secondary


        embed = self._create_page_embed()
        try:
            if self.is_ephemeral or not hasattr(interaction, 'message') or not interaction.message :
                 await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.message.edit(embed=embed, view=self)
        except discord.errors.NotFound:
            logger.warning(f"InventoryEmbedView: Message to edit not found for user {self.user_name}.")
            self.stop()
        except Exception as e:
            logger.error(f"Error updating inventory view for {self.user_name}: {e}", exc_info=True)


    async def prev_page_callback(self, interaction: discord.Interaction):
        if self.current_page > 0:
            self.current_page -= 1
        await self._update_view_and_buttons(interaction)

    async def next_page_callback(self, interaction: discord.Interaction):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
        await self._update_view_and_buttons(interaction)

    async def sort_by_id_callback(self, interaction: discord.Interaction):
        self.current_sort_order = "id_asc"
        self._sort_items()
        await self._update_view_and_buttons(interaction)

    async def sort_by_rarity_asc_callback(self, interaction: discord.Interaction):
        self.current_sort_order = "rarity_asc"
        self._sort_items()
        await self._update_view_and_buttons(interaction)

    async def sort_by_rarity_desc_callback(self, interaction: discord.Interaction):
        self.current_sort_order = "rarity_desc"
        self._sort_items()
        await self._update_view_and_buttons(interaction)

    async def send_initial_message(self):
        """Sends the initial inventory message with the view."""
        embed = self._create_page_embed()
        try:
            await self.interaction_to_edit.followup.send(embed=embed, view=self, ephemeral=self.is_ephemeral)
        except Exception as e:
            logger.error(f"Error sending initial inventory message for {self.user_name}: {e}", exc_info=True)
            try:
                error_embed = discord.Embed(title="エラー", description="インベントリ表示の開始に失敗しました。", color=discord.Color.red())
                await self.interaction_to_edit.followup.send(embed=error_embed, ephemeral=True)
            except Exception as e2:
                logger.error(f"Failed to send error fallback for initial inventory for {self.user_name}: {e2}", exc_info=True)

class BattleView(discord.ui.View):
    def __init__(self, battle_session: 'BattleSession'):
        super().__init__(timeout=300)
        self.battle_session = battle_session
        self.message: Optional[discord.WebhookMessage] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.battle_session.player_id:
            await interaction.response.send_message("この戦闘はあなたの戦闘ではありません。", ephemeral=True)
            return False
        if self.battle_session.is_battle_over:
            await interaction.response.send_message("この戦闘は既に終了しています。", ephemeral=True)
            return False
        return True

    async def _handle_action(self, interaction: discord.Interaction, action_type: str):
        """Generic handler for player actions."""
        if self.battle_session.current_turn != "player":
            await interaction.response.send_message("あなたのターンではありません。", ephemeral=True)
            return

        await interaction.response.defer()

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                logger.warning(f"BattleView: Message for disabling buttons not found (ID: {self.message.id})")
            except Exception as e_edit:
                 logger.error(f"BattleView: Error disabling buttons on message edit: {e_edit}", exc_info=True)


        await self.battle_session.player_action(action_type, interaction)

    @discord.ui.button(label="攻撃", style=discord.ButtonStyle.danger, custom_id="battle_attack")
    async def attack_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_action(interaction, "attack")

    @discord.ui.button(label="防御", style=discord.ButtonStyle.secondary, custom_id="battle_defend")
    async def defend_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_action(interaction, "defend")

    @discord.ui.button(label="逃走", style=discord.ButtonStyle.grey, custom_id="battle_flee")
    async def flee_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_action(interaction, "flee")
        self.stop()

    async def on_timeout(self):
        if self.message and not self.battle_session.is_battle_over:
            logger.info(f"BattleView timed out for player {self.battle_session.player_id} vs {self.battle_session.enemy_name}. Message ID: {self.message.id}")
            self.battle_session.battle_log.append(f"{self.battle_session.player_name}は時間切れで行動できなかった...")
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            try:
                timeout_embed = self.battle_session._create_battle_embed()
                timeout_embed.description = "時間切れで戦闘が終了しました。"
                timeout_embed.color = discord.Color.light_grey()
                await self.message.edit(embed=timeout_embed, view=None)
            except discord.errors.NotFound:
                logger.warning(f"BattleView timeout: Message (ID: {self.message.id}) not found.")
            except Exception as e:
                logger.error(f"BattleView timeout: Error editing message: {e}", exc_info=True)

            rpg_cog = self.battle_session.bot.get_cog("RPG")
            if rpg_cog and self.battle_session.player_id in rpg_cog.active_battles:
                del rpg_cog.active_battles[self.battle_session.player_id]
                logger.info(f"Battle session for {self.battle_session.player_id} removed from active_battles due to View timeout.")
        self.stop()

class BattleContinuationView(discord.ui.View):
    def __init__(self, rpg_cog: 'RPG', interaction_user_id: int):
        super().__init__(timeout=180)
        self.rpg_cog = rpg_cog
        self.interaction_user_id = interaction_user_id
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("この操作は戦闘を行った本人にしかできません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="連戦する", style=discord.ButtonStyle.success, custom_id="battle_continue")
    async def continue_battle(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 修正: 元のメッセージを削除し、新しい「考え中」メッセージを送信
        if interaction.message:
            try:
                await interaction.message.delete()
            except (discord.NotFound, discord.Forbidden):
                logger.warning(f"Could not delete previous battle message (ID: {interaction.message.id})")

        bot_name = self.rpg_cog.bot.user.display_name if self.rpg_cog.bot.user else "ソフィア"
        await interaction.response.send_message(f"… {bot_name} が考え中…", ephemeral=False)

        await self.rpg_cog._start_battle_logic(interaction)
        self.stop()

    @discord.ui.button(label="戦闘を終了する", style=discord.ButtonStyle.danger, custom_id="battle_end")
    async def end_battle(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="戦闘終了", description="お疲れ様でした！またの挑戦を待ってるよ！", color=discord.Color.blue())
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()
    
    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            try:
                original_embed = self.message.embeds[0]
                original_embed.description = f"{original_embed.description}\n\n選択時間が過ぎたため、戦闘は終了しました。"
                original_embed.set_footer(text="タイムアウト")
                original_embed.color=discord.Color.light_grey()
                await self.message.edit(embed=original_embed, view=self)
            except (discord.NotFound, IndexError, discord.HTTPException) as e:
                logger.warning(f"Could not edit message on BattleContinuationView timeout: {e}")
        self.stop()

class GachaSelectView(discord.ui.View):
    """ユーザーにどのガチャを引くか選択させるView。"""
    def __init__(self, bot, rpg_cog, gacha_system, gacha_settings, interaction_user_id):
        super().__init__(timeout=180)
        self.bot = bot
        self.rpg_cog = rpg_cog
        self.gacha_system = gacha_system
        self.interaction_user_id = interaction_user_id
        self.message: Optional[discord.WebhookMessage] = None

        for key, info in gacha_settings.items():
            button = discord.ui.Button(
                label=f"{info['name']} ({info['cost_single']}G)",
                style=discord.ButtonStyle.primary,
                custom_id=f"gacha_select_{key}"
            )
            button.callback = self.create_callback(key)
            self.add_item(button)

    def create_callback(self, gacha_key: str):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.gacha_system.execute_gacha_draw(interaction, gacha_key, 1)
            self.stop()
        return callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("この操作はコマンドを実行した本人のみ可能です。", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        try:
            if self.message:
                 await self.message.edit(content="時間切れです。もう一度コマンドを実行してください。", view=self)
        except discord.NotFound:
            pass
        self.stop()

class GachaResultView(discord.ui.View):
    """ガチャの結果を表示し、アイテムの保管か売却を選択させるView。"""
    def __init__(self, bot, rpg_cog, user_id, guild_id, drawn_items_details, gacha_name, rarity_probabilities, rarity_weights, total_rarity_weight, sell_prices, inventory_limit):
        super().__init__(timeout=180)
        self.bot = bot
        self.rpg_cog = rpg_cog
        self.user_id = user_id
        self.guild_id = guild_id
        self.gacha_name = gacha_name
        self.rarity_probabilities = rarity_probabilities
        self.rarity_weights = rarity_weights
        self.total_rarity_weight = total_rarity_weight
        self.sell_prices = sell_prices
        self.inventory_limit = inventory_limit
        self.message: Optional[discord.WebhookMessage] = None

        item_details = drawn_items_details[0]
        if item_details is None:
            self.stop()
            return

        (self.new_item_base_id, self.new_item_base_name, self.new_item_base_rarity,
         self.new_item_type_display, self.new_effect_id, self.new_effect_name_prefix,
         self.new_effect_rarity) = item_details

        self.full_item_name = f"{self.new_effect_name_prefix}{self.new_item_base_name}"

        self.acquire_button = discord.ui.Button(label="インベントリに保管", style=discord.ButtonStyle.green)
        self.acquire_button.callback = self.acquire_callback
        self.add_item(self.acquire_button)

        sell_price = self.sell_prices.get(self.new_item_base_rarity, 0)
        self.sell_button = discord.ui.Button(label=f"売却 ({sell_price}G)", style=discord.ButtonStyle.danger)
        self.sell_button.callback = self.sell_callback
        self.add_item(self.sell_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("この操作はガチャを引いた本人のみ可能です。", ephemeral=True)
            return False
        return True

    async def disable_buttons(self, interaction: discord.Interaction):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except discord.NotFound:
            pass
        self.stop()

    async def acquire_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.execute("SELECT COUNT(*) FROM inventory WHERE user_id = ? AND guild_id = ?", (self.user_id, self.guild_id)) as cursor:
            count_row = await cursor.fetchone()
        current_inventory_count = count_row[0] if count_row else 0

        if current_inventory_count >= self.inventory_limit:
            embed = discord.Embed(title="インベントリが満杯！", description="アイテムを保管できませんでした。インベントリがいっぱいです。", color=discord.Color.red())
            await interaction.followup.send(embed=embed, ephemeral=True)
            await self.disable_buttons(interaction)
            return

        try:
            async with transaction(self.bot.db):
                await self.bot.db.execute(
                    "INSERT INTO inventory (user_id, guild_id, item_id, effect_id) VALUES (?, ?, ?, ?)",
                    (self.user_id, self.guild_id, self.new_item_base_id, self.new_effect_id)
                )
            embed = discord.Embed(title="保管完了！", description=f"**{self.full_item_name}** をインベントリに保管しました。", color=discord.Color.green())
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"GachaResultView acquire error: {e}", exc_info=True)
            embed = discord.Embed(title="エラー", description="アイテムの保管中にエラーが発生しました。", color=discord.Color.red())
            await interaction.followup.send(embed=embed, ephemeral=True)
        finally:
            await self.disable_buttons(interaction)

    async def sell_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        sell_price = self.sell_prices.get(self.new_item_base_rarity, 0)

        try:
            async with transaction(self.bot.db):
                await self.bot.db.execute("UPDATE users SET gold = gold + ? WHERE user_id = ? AND guild_id = ?", (sell_price, self.user_id, self.guild_id))

            embed = discord.Embed(title="売却完了！", description=f"**{self.full_item_name}** を売却して **{sell_price}G** を獲得しました。", color=discord.Color.blue())
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"GachaResultView sell error: {e}", exc_info=True)
            embed = discord.Embed(title="エラー", description="アイテムの売却中にエラーが発生しました。", color=discord.Color.red())
            await interaction.followup.send(embed=embed, ephemeral=True)
        finally:
            await self.disable_buttons(interaction)

    async def send_initial_message(self, interaction: discord.Interaction):
        prob_item = (self.rarity_weights.get(self.new_item_base_rarity, 0) / self.total_rarity_weight) if self.total_rarity_weight > 0 else 0
        prob_effect = (self.rarity_weights.get(self.new_effect_rarity, 0) / self.total_rarity_weight) if self.total_rarity_weight > 0 else 0
        combined_prob_percent = prob_item * prob_effect * 100

        embed = discord.Embed(
            title=f"ガチャ結果: {self.gacha_name}",
            description=(
                f"{interaction.user.mention}さん、見てみて！こんなのが出たよ！\n\n"
                f"**{self.full_item_name}**\n"
                f"　┣ 装備タイプ: {self.new_item_type_display}\n"
                f"　┣ 装備レアリティ: {self.new_item_base_rarity} ({self.rarity_probabilities.get(self.new_item_base_rarity, 'N/A')})\n"
                f"　┣ 効果レアリティ: {self.new_effect_rarity} ({self.rarity_probabilities.get(self.new_effect_rarity, 'N/A')})\n"
                f"　┗ 組み合わせ出現率: {combined_prob_percent:.3f}%\n\n"
                "このアイテム、どうする？"
            ),
            color=discord.Color.gold()
        )
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text="選択肢のタイムアウトは3分です。")
        self.message = await interaction.followup.send(embed=embed, view=self, ephemeral=False)
