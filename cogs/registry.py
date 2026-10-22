import discord
from discord.ext import commands
from discord import option
import easyocr
import numpy as np
from PIL import Image
import io
import re
import math
import warnings
warnings.filterwarnings("ignore", message=".*quantize_per_tensor.*")

from core.database import (
    add_account,
    get_account_by_ingame_id,
    get_accounts_page,
    get_account_count,
    get_accounts_by_discord_id,
    search_accounts,
    find_exact_account,
    delete_account,
    transfer_account,
    add_blacklist,
    get_blacklist_entry,
)
from core.permissions_check import check_and_respond

# Initialise EasyOCR once at startup — loads the ML model into memory
# gpu=False since the host won't have a GPU; still plenty fast for this use case
print("Loading EasyOCR model...")
_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
print("EasyOCR ready.")

ACCOUNTS_PER_PAGE = 5

# UI chrome noise common to Bio screenshots
_BIO_NOISE = re.compile(
    r"^(ralter|alter|day|basic|standard|jump|isk|skill|tech|level|station|wallet|"
    r"balance|position|employment|combat|medal|bio|novice|guidance|corporation|"
    r"executor|alliance|go\s+to|exclusive|store|base|total|sp$)",
    re.IGNORECASE,
)

# UI chrome noise specific to corp panel screenshots
_CORP_NOISE = re.compile(
    r"^(send|message|team|invitation|add|contact|duel|more|personal|employment|"
    r"points|online|active|upper|limit|adverts|recruit|applications|announcements|"
    r"corporation|news|private|major|captain|admiral|pirate|lord|members|"
    r"rest\s+in\s+peace|left\s+your|old\s+member|leaving|usual\s+suspects|"
    r"black\s+flag|pirating|co\.|executor|alliance|ralter|alter)",
    re.IGNORECASE,
)

# Landmark strings that only appear in corp panel screenshots
_CORP_LANDMARKS = re.compile(
    r"send message|team.up invitation|add contact|duel invitation|"
    r"usual suspects|black flag pirating",
    re.IGNORECASE,
)

# Landmark strings that only appear in Bio screenshots
_BIO_LANDMARKS = re.compile(
    r"base station|skill points|tech level|wallet balance|novice guidance",
    re.IGNORECASE,
)


# ---------- OCR helpers ----------

def strip_corp_tag(name):
    if not name:
        return name
    return re.sub(r"^\[.*?\]\s*", "", name).strip()


def _fix_bracket(text):
    """
    EasyOCR consistently misreads the closing bracket in corp tags as J.
    e.g. '[BFPCJDPS' → '[BFPC]DPS'
    """
    return re.sub(r"(\[[A-Z0-9?]+)J([^\[])", r"\1]\2", text)


def _extract_id(results):
    """
    Scan detections for an Eve Echoes character ID.
    All character IDs start with 100 and are 10-13 digits long.
    Works for both 'ID 10007001448' (bio) and 'ID: 10007001448' (corp panel).
    """
    for _, text, confidence in results:
        if confidence < 0.2:
            continue
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 9:
            # Strip leading "10" artifact if EasyOCR glued the label to the number
            if digits.startswith("10") and len(digits) > 11:
                digits = digits[2:]
            if digits.startswith("100") and 10 <= len(digits) <= 13:
                return digits
    return None


def _detect_screenshot_type(full_text):
    """
    Given all raw text from the full image, determine which screenshot type it is.
    Returns 'bio', 'corp', or 'unknown'.
    """
    combined = " ".join(full_text)
    if _CORP_LANDMARKS.search(combined):
        return "corp"
    if _BIO_LANDMARKS.search(combined):
        return "bio"
    return "unknown"


def _parse_bio(results):
    """
    Extract name and ID from a Bio page screenshot.
    Name has corp tag e.g. '[BFPC]Mineral Miner' — strip it after extraction.
    """
    name_candidate = None
    id_candidate = _extract_id(results)

    # Sort by confidence, pick highest-confidence non-noise block as name
    sorted_results = sorted(results, key=lambda x: x[2], reverse=True)
    for _, text, confidence in sorted_results:
        text = text.strip()
        if not text or confidence < 0.3:
            continue
        if _BIO_NOISE.search(text):
            continue
        if len(re.sub(r"[^a-zA-Z]", "", text)) < 2:
            continue
        if re.match(r"^[\d\s.,:]+$", text):
            continue
        name_candidate = strip_corp_tag(_fix_bracket(text))
        break

    return name_candidate, id_candidate


def _parse_corp_panel(results):
    """
    Extract name and ID from a corp member panel screenshot.
    Name has no corp tag — it appears cleanly near the top of the info panel.
    ID appears as 'ID: 10007001448' just below the name.
    """
    id_candidate = _extract_id(results)
    name_candidate = None

    # In corp panel the name appears near the top, sort by vertical position (bbox y)
    # bbox is [[top-left], [top-right], [bottom-right], [bottom-left]]
    position_sorted = sorted(results, key=lambda x: x[0][0][1])

    for bbox, text, confidence in position_sorted:
        text = text.strip()
        if not text or confidence < 0.3:
            continue
        if _CORP_NOISE.search(text):
            continue
        # Skip the ID line itself
        if re.search(r"ID\s*:", text, re.IGNORECASE):
            continue
        # Skip anything that's mostly digits
        if re.match(r"^[\d\s.,:]+$", text):
            continue
        # Must have at least 3 real letters (filters out lone symbols/noise)
        if len(re.sub(r"[^a-zA-Z]", "", text)) < 3:
            continue
        name_candidate = text
        break

    return name_candidate, id_candidate


def parse_character_screenshot(image_bytes):
    """
    Auto-detects whether this is a Bio page or corp member panel screenshot
    and routes to the appropriate parser. Works with any screen resolution.
    """
    img = Image.open(io.BytesIO(image_bytes))
    width, height = img.size

    # Run OCR on the full image first — used for both type detection and parsing
    # Slightly cropped to cut the very edges which are usually empty border
    full_crop = img.crop((
        int(width * 0.03),
        int(height * 0.03),
        int(width * 0.97),
        int(height * 0.97),
    ))
    full_results = _reader.readtext(np.array(full_crop))
    all_text = [text for _, text, _ in full_results]

    screenshot_type = _detect_screenshot_type(all_text)

    if screenshot_type == "corp":
        # For corp panel, focus on the right-side info panel where name and ID live
        # Portrait is on the left (~40% width), info panel is the rest
        info_crop = img.crop((
            int(width * 0.35),   # left: skip the portrait
            int(height * 0.20),  # top: just above the name
            int(width * 0.85),   # right: before the action buttons
            int(height * 0.60),  # bottom: below the ID line
        ))
        panel_results = _reader.readtext(np.array(info_crop))
        ingame_name, ingame_id = _parse_corp_panel(panel_results)
    else:
        # Bio page (or unknown — fall back to bio parser)
        # Use full image results so varying sidebar widths don't affect the crop.
        # Filter to the right half only to skip the sidebar nav on wide/tablet screens.
        right_half_results = [
            (bbox, text, conf)
            for bbox, text, conf in full_results
            if bbox[0][0] > (full_crop.width * 0.30)
        ]
        ingame_name, ingame_id = _parse_bio(right_half_results)

    return ingame_name, ingame_id


# ---------- /account add views ----------

class ConfirmEditView(discord.ui.View):
    def __init__(self, ingame_name, ingame_id, target_user, requester):
        super().__init__(timeout=180)
        self.ingame_name = ingame_name
        self.ingame_id = ingame_id
        self.target_user = target_user
        self.requester = requester

    def build_embed(self):
        embed = discord.Embed(title="Confirm Account Details", color=discord.Color.from_rgb(0, 0, 0))
        embed.add_field(name="In-game Name", value=self.ingame_name or "Not detected", inline=False)
        embed.add_field(name="In-game ID", value=self.ingame_id or "Not detected", inline=False)
        embed.add_field(
            name="Discord User",
            value=(
                f"{self.target_user.mention}\n"
                f"Username: {self.target_user.name}\n"
                f"Display Name: {self.target_user.global_name or self.target_user.name}"
            ),
            inline=False,
        )
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                "Only the person who ran this command can confirm or edit it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not self.ingame_name or not self.ingame_id:
            await interaction.response.send_message(
                "Name or ID is still missing, please edit before confirming.", ephemeral=True
            )
            return

        # Re-check duplicate in case ID was manually edited to something already registered
        existing = get_account_by_ingame_id(self.ingame_id)
        if existing:
            embed = discord.Embed(title="Account Already Exists", color=discord.Color.red())
            embed.add_field(name="In-game Name", value=existing["ingame_name"], inline=False)
            embed.add_field(name="In-game ID", value=existing["ingame_id"], inline=False)
            embed.add_field(
                name="Already Registered To",
                value=(
                    f"<@{existing['discord_id']}>\n"
                    f"Username: {existing['discord_username']}\n"
                    f"Display Name: {existing['discord_name']}"
                ),
                inline=False,
            )
            embed.set_footer(text="This in-game ID is already in the registry. No account was added.")
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
            return

        add_account(
            self.target_user.id,
            self.target_user.name,
            self.target_user.global_name or self.target_user.name,
            self.ingame_name,
            self.ingame_id,
        )
        embed = self.build_embed()
        embed.title = "Account Added"
        embed.color = discord.Color.green()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Edit Name", style=discord.ButtonStyle.gray)
    async def edit_name(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(EditModal(self, "name"))

    @discord.ui.button(label="Edit ID", style=discord.ButtonStyle.gray)
    async def edit_id(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(EditModal(self, "id"))

    @discord.ui.button(label="Edit Discord User", style=discord.ButtonStyle.gray)
    async def edit_user(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Mention the correct Discord user in your next message in this channel.",
            ephemeral=True,
        )
        bot = interaction.client

        def check(m):
            return m.author.id == self.requester.id and m.mentions

        msg = await bot.wait_for("message", check=check, timeout=60)
        self.target_user = msg.mentions[0]
        await msg.delete()
        await interaction.message.edit(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        embed = self.build_embed()
        embed.title = "Cancelled"
        embed.color = discord.Color.red()
        await interaction.response.edit_message(embed=embed, view=self)


class EditModal(discord.ui.Modal):
    def __init__(self, parent_view, field):
        super().__init__(title=f"Edit {field}")
        self.parent_view = parent_view
        self.field = field
        self.input = discord.ui.InputText(label=f"New {field}")
        self.add_item(self.input)

    async def callback(self, interaction: discord.Interaction):
        if self.field == "name":
            self.parent_view.ingame_name = strip_corp_tag(self.input.value)
        else:
            self.parent_view.ingame_id = self.input.value
        await interaction.response.edit_message(embed=self.parent_view.build_embed())


# ---------- /account show pagination ----------

class AccountsPageView(discord.ui.View):
    def __init__(self, page, total_count):
        super().__init__(timeout=120)
        self.page = page
        self.total_count = total_count
        self.total_pages = math.ceil(total_count / ACCOUNTS_PER_PAGE)
        self._update_buttons()

    def _update_buttons(self):
        self.prev_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= self.total_pages

    def build_embed(self):
        accounts = get_accounts_page(self.page, ACCOUNTS_PER_PAGE)
        start = (self.page - 1) * ACCOUNTS_PER_PAGE + 1
        end = min(start + ACCOUNTS_PER_PAGE - 1, self.total_count)

        embed = discord.Embed(
            title="Registered Accounts",
            description=f"Showing **{start}–{end}** of **{self.total_count}** accounts",
            color=discord.Color.from_rgb(0, 0, 0),
        )
        for acc in accounts:
            embed.add_field(
                name=acc["ingame_name"],
                value=(
                    f"ID: {acc['ingame_id']}\n"
                    f"Owner: <@{acc['discord_id']}>\n"
                    f"Username: {acc['discord_username']}\n"
                    f"Display Name: {acc['discord_name']}"
                ),
                inline=False,
            )
        embed.set_footer(text=f"Page {self.page} of {self.total_pages}")
        return embed

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.gray)
    async def prev_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.gray)
    async def next_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Go to page…", style=discord.ButtonStyle.blurple)
    async def goto_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(GotoPageModal(self))


class GotoPageModal(discord.ui.Modal):
    def __init__(self, parent_view):
        super().__init__(title="Go to Page")
        self.parent_view = parent_view
        self.input = discord.ui.InputText(
            label=f"Page number (1–{parent_view.total_pages})",
            placeholder=str(parent_view.page),
        )
        self.add_item(self.input)

    async def callback(self, interaction: discord.Interaction):
        try:
            page = int(self.input.value)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)
            return

        if not (1 <= page <= self.parent_view.total_pages):
            await interaction.response.send_message(
                f"Page must be between 1 and {self.parent_view.total_pages}.", ephemeral=True
            )
            return

        self.parent_view.page = page
        self.parent_view._update_buttons()
        await interaction.response.edit_message(
            embed=self.parent_view.build_embed(), view=self.parent_view
        )


# ---------- /account transfer view ----------

class TransferConfirmView(discord.ui.View):
    def __init__(self, account, new_user, requester):
        super().__init__(timeout=60)
        self.account = account
        self.new_user = new_user
        self.requester = requester

    def build_embed(self):
        embed = discord.Embed(title="Confirm Account Transfer", color=discord.Color.from_rgb(0, 0, 0))
        embed.add_field(
            name="In-game Account",
            value=(
                f"Name: {self.account['ingame_name']}\n"
                f"ID: {self.account['ingame_id']}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Current Owner",
            value=(
                f"<@{self.account['discord_id']}>\n"
                f"Username: {self.account['discord_username']}\n"
                f"Display Name: {self.account['discord_name']}\n"
                f"Discord ID: {self.account['discord_id']}"
            ),
            inline=False,
        )
        embed.add_field(
            name="New Owner",
            value=(
                f"{self.new_user.mention}\n"
                f"Username: {self.new_user.name}\n"
                f"Display Name: {self.new_user.global_name or self.new_user.name}\n"
                f"Discord ID: {self.new_user.id}"
            ),
            inline=False,
        )
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                "Only the person who ran this command can confirm or cancel it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm Transfer", style=discord.ButtonStyle.green)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        transfer_account(
            self.account["ingame_id"],
            self.new_user.id,
            self.new_user.name,
            self.new_user.global_name or self.new_user.name,
        )
        embed = self.build_embed()
        embed.title = "Account Transferred"
        embed.color = discord.Color.green()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        embed = self.build_embed()
        embed.title = "Transfer Cancelled"
        embed.color = discord.Color.red()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)


# ---------- /account remove view ----------

class DeleteConfirmView(discord.ui.View):
    def __init__(self, account, requester):
        super().__init__(timeout=60)
        self.account = account
        self.requester = requester

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.requester.id

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.red)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        delete_account(self.account["ingame_id"])
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Deleted **{self.account['ingame_name']}** ({self.account['ingame_id']})",
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.gray)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled.", view=self)


# ---------- /blacklist views ----------

class BlacklistConfirmView(discord.ui.View):
    def __init__(self, ingame_name, ingame_id, requester):
        super().__init__(timeout=180)
        self.ingame_name = ingame_name
        self.ingame_id = ingame_id
        self.requester = requester

    def build_embed(self):
        embed = discord.Embed(title="Confirm Blacklist Entry", color=discord.Color.from_rgb(0, 0, 0))
        embed.add_field(name="In-game Name", value=self.ingame_name or "Not detected", inline=False)
        embed.add_field(name="In-game ID", value=self.ingame_id or "Not detected", inline=False)
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                "Only the person who ran this command can confirm or edit it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not self.ingame_name or not self.ingame_id:
            await interaction.response.send_message(
                "Name or ID is still missing, please edit before confirming.", ephemeral=True
            )
            return

        add_blacklist(self.ingame_name, self.ingame_id)
        embed = self.build_embed()
        embed.title = "Blacklisted"
        embed.color = discord.Color.red()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Edit Name", style=discord.ButtonStyle.gray)
    async def edit_name(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(BlacklistEditModal(self, "name"))

    @discord.ui.button(label="Edit ID", style=discord.ButtonStyle.gray)
    async def edit_id(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(BlacklistEditModal(self, "id"))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        embed = self.build_embed()
        embed.title = "Cancelled"
        embed.color = discord.Color.red()
        await interaction.response.edit_message(embed=embed, view=self)


class BlacklistEditModal(discord.ui.Modal):
    def __init__(self, parent_view, field):
        super().__init__(title=f"Edit {field}")
        self.parent_view = parent_view
        self.field = field
        self.input = discord.ui.InputText(label=f"New {field}")
        self.add_item(self.input)

    async def callback(self, interaction: discord.Interaction):
        if self.field == "name":
            self.parent_view.ingame_name = strip_corp_tag(self.input.value)
        else:
            self.parent_view.ingame_id = self.input.value
        await interaction.response.edit_message(embed=self.parent_view.build_embed())


# ---------- Slash command groups ----------

account_group = discord.SlashCommandGroup("account", "Manage corp account registry")


# ---------- Cog ----------

class Registry(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    account = account_group

    @account_group.command(name="add", description="Add an Eve Echoes account from a character screenshot")
    @option("screenshot", discord.Attachment, description="Character page screenshot")
    @option("user", discord.Member, description="Discord user this account belongs to")
    async def account_add(self, ctx, screenshot: discord.Attachment, user: discord.Member):
        if not await check_and_respond(ctx, "account add"):
            return

        await ctx.defer()

        image_bytes = await screenshot.read()
        ingame_name, ingame_id = parse_character_screenshot(image_bytes)

        # Blacklist check — hard block
        if ingame_id:
            blacklisted = get_blacklist_entry(ingame_id)
            if blacklisted:
                embed = discord.Embed(title="⛔ Blacklisted Character", color=discord.Color.red())
                embed.add_field(name="In-game Name", value=blacklisted["ingame_name"], inline=False)
                embed.add_field(name="In-game ID", value=blacklisted["ingame_id"], inline=False)
                embed.set_footer(text="This character is blacklisted. No account was added.")
                await ctx.respond(embed=embed)
                return

        # Duplicate ID check
        if ingame_id:
            existing = get_account_by_ingame_id(ingame_id)
            if existing:
                embed = discord.Embed(title="Account Already Exists", color=discord.Color.red())
                embed.add_field(name="In-game Name", value=existing["ingame_name"], inline=False)
                embed.add_field(name="In-game ID", value=existing["ingame_id"], inline=False)
                embed.add_field(
                    name="Already Registered To",
                    value=(
                        f"<@{existing['discord_id']}>\n"
                        f"Username: {existing['discord_username']}\n"
                        f"Display Name: {existing['discord_name']}"
                    ),
                    inline=False,
                )
                embed.set_footer(text="This in-game ID is already in the registry. No account was added.")
                await ctx.respond(embed=embed)
                return

        view = ConfirmEditView(ingame_name, ingame_id, user, ctx.author)
        await ctx.respond(embed=view.build_embed(), view=view)

    @account_group.command(name="remove", description="Remove an account from the registry")
    @option("query", str, description="Exact in-game name or ID")
    async def account_remove(self, ctx, query: str):
        if not await check_and_respond(ctx, "account remove"):
            return

        account = find_exact_account(query)
        if not account:
            await ctx.respond(f"No exact match found for `{query}`.")
            return

        embed = discord.Embed(title="Confirm Deletion", color=discord.Color.red())
        embed.add_field(name="In-game Name", value=account["ingame_name"], inline=False)
        embed.add_field(name="In-game ID", value=account["ingame_id"], inline=False)
        embed.add_field(
            name="Registered To",
            value=(
                f"<@{account['discord_id']}>\n"
                f"Username: {account['discord_username']}\n"
                f"Display Name: {account['discord_name']}"
            ),
            inline=False,
        )

        view = DeleteConfirmView(account, ctx.author)
        await ctx.respond(embed=embed, view=view)

    @account_group.command(name="show", description="List all registered accounts in the corp")
    async def account_show(self, ctx):
        if not await check_and_respond(ctx, "account show"):
            return

        total = get_account_count()
        if not total:
            await ctx.respond("No accounts registered yet.")
            return

        view = AccountsPageView(page=1, total_count=total)
        await ctx.respond(embed=view.build_embed(), view=view)

    @account_group.command(name="find", description="List all accounts belonging to a Discord user")
    @option("user", discord.Member, description="Mention a server member", required=False)
    @option("discord_id", str, description="Discord user ID (use this for ex-members)", required=False)
    async def account_find(self, ctx, user: discord.Member = None, discord_id: str = None):
        if not await check_and_respond(ctx, "account find"):
            return

        # Exactly one input must be provided
        if user and discord_id:
            await ctx.respond("Please provide either a mention or a Discord ID, not both.", ephemeral=True)
            return
        if not user and not discord_id:
            await ctx.respond("Please provide a mention or a Discord ID to search.", ephemeral=True)
            return

        if user:
            resolved_id = str(user.id)
            display = f"{user.mention} ({user.name})"
        else:
            discord_id = discord_id.strip()
            if not discord_id.isdigit():
                await ctx.respond("Discord IDs are numeric only. Please check and try again.", ephemeral=True)
                return
            resolved_id = discord_id
            display = f"Discord ID `{discord_id}`"

        accounts = get_accounts_by_discord_id(resolved_id)

        if not accounts:
            await ctx.respond(f"No accounts found for {display}.")
            return

        embed = discord.Embed(
            title=f"Accounts for {display}" if user else f"Accounts for Discord ID {discord_id}",
            description=f"{len(accounts)} account{'s' if len(accounts) != 1 else ''} registered",
            color=discord.Color.from_rgb(0, 0, 0),
        )

        # Pull Discord info from the first row — all rows share the same owner
        first = accounts[0]
        embed.add_field(
            name="Discord User",
            value=(
                f"<@{first['discord_id']}>\n"
                f"Username: {first['discord_username']}\n"
                f"Display Name: {first['discord_name']}"
            ),
            inline=False,
        )
        embed.add_field(name="\u200b", value="─" * 30, inline=False)

        for acc in accounts:
            embed.add_field(
                name=acc["ingame_name"],
                value=f"ID: {acc['ingame_id']}",
                inline=False,
            )

        await ctx.respond(embed=embed)

    @account_group.command(name="transfer", description="Transfer an account to a different Discord user")
    @option("ingame_id", str, description="In-game ID of the account to transfer")
    @option("user", discord.Member, description="New Discord owner")
    async def account_transfer(self, ctx, ingame_id: str, user: discord.Member):
        if not await check_and_respond(ctx, "account transfer"):
            return

        account = get_account_by_ingame_id(ingame_id.strip())
        if not account:
            await ctx.respond(f"No account found with in-game ID `{ingame_id}`.", ephemeral=True)
            return

        if account["discord_id"] == str(user.id):
            await ctx.respond(
                f"**{account['ingame_name']}** is already registered to {user.mention}.",
                ephemeral=True,
            )
            return

        view = TransferConfirmView(account, user, ctx.author)
        await ctx.respond(embed=view.build_embed(), view=view)

    @discord.slash_command(name="whois", description="Find who owns an Eve Echoes account")
    @option("query", str, description="In-game name or ID")
    async def whois(self, ctx, query: str):
        if not await check_and_respond(ctx, "whois"):
            return

        results = search_accounts(query)
        if not results:
            await ctx.respond(f"No accounts found matching `{query}`.")
            return

        embed = discord.Embed(title=f"Results for '{query}'", color=discord.Color.from_rgb(0, 0, 0))
        for acc in results[:15]:
            embed.add_field(
                name=acc["ingame_name"],
                value=(
                    f"ID: {acc['ingame_id']}\n"
                    f"Owner: <@{acc['discord_id']}>\n"
                    f"Username: {acc['discord_username']}\n"
                    f"Display Name: {acc['discord_name']}"
                ),
                inline=False,
            )
        await ctx.respond(embed=embed)

    @discord.slash_command(name="blacklist", description="Add a character to the blacklist from a screenshot")
    @option("screenshot", discord.Attachment, description="Character page screenshot")
    async def blacklist(self, ctx, screenshot: discord.Attachment):
        if not ctx.author.guild_permissions.administrator:
            await ctx.respond("This command is restricted to server admins.", ephemeral=True)
            return

        await ctx.defer()

        image_bytes = await screenshot.read()
        ingame_name, ingame_id = parse_character_screenshot(image_bytes)

        view = BlacklistConfirmView(ingame_name, ingame_id, ctx.author)
        await ctx.respond(embed=view.build_embed(), view=view)


def setup(bot):
    bot.add_cog(Registry(bot))