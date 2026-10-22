import re
from datetime import datetime, timedelta, timezone

import discord
from discord import option
from discord.ext import commands, tasks

from core.database import (
    get_siege_systems,
    add_siege_system,
    remove_siege_system,
    set_siege_cleared,
    reset_siege_systems,
    get_siege_state,
    set_siege_state,
)

# ======================================================================
# SIEGE SETTINGS — edit these, then restart the bot for them to apply
# ======================================================================
SIEGE_CHANNEL_ID = 1497297713908158577   # #siege-fleets, the only channel regular members can use
SIEGE_RESET_WEEKDAY = 2                  # Monday=0, Tuesday=1, Wednesday=2 ... Sunday=6
SIEGE_RESET_HOUR_UTC = 0                 # 0-23, in UTC
SIEGE_REMINDER_HOURS = 48                # hours after each reset to post the "still pending" list
# ======================================================================

BLACK = discord.Color.from_rgb(0, 0, 0)


# ---------- Time helpers ----------

def now_utc():
    return datetime.now(timezone.utc)


def current_cycle_start(now=None):
    """Most recent reset moment (weekday + hour in UTC) at or before now."""
    now = now or now_utc()
    days_back = (now.weekday() - SIEGE_RESET_WEEKDAY) % 7
    start = (now - timedelta(days=days_back)).replace(
        hour=SIEGE_RESET_HOUR_UTC, minute=0, second=0, microsecond=0
    )
    if start > now:
        start -= timedelta(days=7)
    return start


def next_reset(now=None):
    return current_cycle_start(now) + timedelta(days=7)


def parse_ts(value):
    """Parse a timestamp string from Supabase into an aware datetime."""
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    # Older Pythons need exactly 3 or 6 fractional digits, so pad to 6
    match = re.match(r"^(.*?\.)(\d+)(.*)$", value)
    if match:
        value = match.group(1) + match.group(2).ljust(6, "0")[:6] + match.group(3)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------- Board helpers ----------

def is_cleared(system, cycle_start):
    cleared_at = parse_ts(system.get("cleared_at"))
    return cleared_at is not None and cleared_at >= cycle_start


def split_systems(systems, cycle_start):
    pending = sorted(
        (s for s in systems if not is_cleared(s, cycle_start)), key=lambda s: s["name"].lower()
    )
    cleared = sorted(
        (s for s in systems if is_cleared(s, cycle_start)), key=lambda s: s["name"].lower()
    )
    return pending, cleared


def all_cleared(systems):
    if not systems:
        return False
    pending, _ = split_systems(systems, current_cycle_start())
    return not pending


def find_system(systems, name):
    wanted = (name or "").strip().lower()
    for s in systems:
        if s["name"].lower() == wanted:
            return s
    return None


def build_board_embed(systems, title="Siege Fleets", color=BLACK):
    pending, cleared = split_systems(systems, current_cycle_start())
    reset_ts = int(next_reset().timestamp())

    embed = discord.Embed(
        title=title,
        description=(
            f"**{len(cleared)}/{len(systems)} cleared this week**\n"
            f"Resets <t:{reset_ts}:R> (<t:{reset_ts}:F>)"
        ),
        color=color,
    )
    embed.add_field(
        name=f"Pending ({len(pending)})",
        value="\n".join(f"❌ {s['name']}" for s in pending) or "None 🎉",
        inline=True,
    )
    embed.add_field(
        name=f"Cleared ({len(cleared)})",
        value="\n".join(f"✅ {s['name']}" for s in cleared) or "None yet",
        inline=True,
    )
    return embed


# ---------- Autocomplete ----------

def _names_matching(ctx, want_cleared):
    try:
        systems = get_siege_systems()
    except Exception:
        return []

    if want_cleared is not None:
        cycle_start = current_cycle_start()
        systems = [s for s in systems if is_cleared(s, cycle_start) == want_cleared]

    query = (ctx.value or "").lower()
    names = sorted((s["name"] for s in systems), key=str.lower)
    return [n for n in names if query in n.lower()][:25]


async def pending_autocomplete(ctx: discord.AutocompleteContext):
    return _names_matching(ctx, False)


async def cleared_autocomplete(ctx: discord.AutocompleteContext):
    return _names_matching(ctx, True)


async def all_autocomplete(ctx: discord.AutocompleteContext):
    return _names_matching(ctx, None)


# ---------- Confirmation view (remove / reset) ----------

class SiegeConfirmView(discord.ui.View):
    def __init__(self, requester, on_confirm, after_confirm=None):
        super().__init__(timeout=60)
        self.requester = requester
        self.on_confirm = on_confirm        # sync callable, does the DB work, returns the result embed
        self.after_confirm = after_confirm  # optional async callable, runs after the message is updated

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                "Only the person who ran this command can confirm or cancel it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.red)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        embed = self.on_confirm()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)
        if self.after_confirm:
            await self.after_confirm()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.gray)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        embed = discord.Embed(title="Cancelled", color=discord.Color.red())
        await interaction.response.edit_message(embed=embed, view=self)


# ---------- Cog ----------

siege_group = discord.SlashCommandGroup("siege", "Siege fleet board")


class Siege(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    siege = siege_group

    # ----- lifecycle -----

    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready can fire more than once on reconnects, so only start the loop once
        if not self.pending_check.is_running():
            self.pending_check.start()

    def cog_unload(self):
        self.pending_check.cancel()

    # ----- access checks -----

    async def _channel_ok(self, ctx):
        if ctx.author.guild_permissions.administrator or ctx.channel_id == SIEGE_CHANNEL_ID:
            return True
        await ctx.respond(
            f"Siege Fleet commands can only be used in <#{SIEGE_CHANNEL_ID}>", ephemeral=True
        )
        return False

    async def _admin_ok(self, ctx):
        if ctx.author.guild_permissions.administrator:
            return True
        await ctx.respond("This command is restricted to server admins.", ephemeral=True)
        return False

    # ----- channel messaging -----

    async def _get_siege_channel(self):
        channel = self.bot.get_channel(SIEGE_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(SIEGE_CHANNEL_ID)
            except Exception as e:
                print(f"[siege] Could not find siege channel {SIEGE_CHANNEL_ID}: {e}")
                return None
        return channel

    async def _announce_complete(self):
        channel = await self._get_siege_channel()
        if channel is None:
            return
        reset_ts = int(next_reset().timestamp())
        embed = discord.Embed(
            title="Siege Fleets Cleared 🎉",
            description=(
                "All siege fleets have been cleared for the week. Thank you all!\n"
                f"The board resets <t:{reset_ts}:R>."
            ),
            color=discord.Color.green(),
        )
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[siege] Could not send completion message: {e}")

    # ----- background task: pending post 48h after each reset -----

    @tasks.loop(minutes=1)
    async def pending_check(self):
        try:
            now = now_utc()
            cycle_start = current_cycle_start(now)
            if now < cycle_start + timedelta(hours=SIEGE_REMINDER_HOURS):
                return

            state = get_siege_state()
            last_posted = parse_ts(state.get("last_pending_post_cycle")) if state else None
            if last_posted and last_posted >= cycle_start:
                return  # already handled for this week

            # Mark this week as handled first so a failed send can't repeat every minute
            set_siege_state(cycle_start.isoformat())

            systems = get_siege_systems()
            pending, _ = split_systems(systems, cycle_start)
            if not pending:
                return

            channel = await self._get_siege_channel()
            if channel is None:
                return

            reset_ts = int(next_reset(now).timestamp())
            embed = discord.Embed(
                title="Siege Fleets Still Pending",
                description=(
                    "These systems haven't been cleared yet this week:\n\n"
                    + "\n".join(f"❌ {s['name']}" for s in pending)
                    + f"\n\nThe board resets <t:{reset_ts}:R>."
                ),
                color=discord.Color.red(),
            )
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[siege] pending_check error: {e}")

    # ----- commands -----

    @siege_group.command(name="list", description="Show which siege fleet systems are pending and cleared")
    async def siege_list(self, ctx):
        if not await self._channel_ok(ctx):
            return

        systems = get_siege_systems()
        if not systems:
            embed = discord.Embed(
                title="Siege Fleets",
                description="No systems on the board yet. An admin can add one with `/siege add`.",
                color=BLACK,
            )
            await ctx.respond(embed=embed)
            return

        await ctx.respond(embed=build_board_embed(systems))

    @siege_group.command(name="clear", description="Mark a system as cleared for this week")
    @option("system", str, description="System to mark as cleared", autocomplete=pending_autocomplete)
    async def siege_clear(self, ctx, system: str):
        if not await self._channel_ok(ctx):
            return

        systems = get_siege_systems()
        target = find_system(systems, system)
        if not target:
            await ctx.respond(
                f"`{system}` isn't on the board. Pick one from the list.", ephemeral=True
            )
            return

        if is_cleared(target, current_cycle_start()):
            await ctx.respond(f"**{target['name']}** is already cleared this week.", ephemeral=True)
            return

        stamp = now_utc().isoformat()
        set_siege_cleared(target["id"], stamp)
        target["cleared_at"] = stamp

        await ctx.respond(
            embed=build_board_embed(systems, title=f"{target['name']} cleared", color=discord.Color.green())
        )
        if all_cleared(systems):
            await self._announce_complete()

    @siege_group.command(name="unclear", description="Put a cleared system back to pending")
    @option("system", str, description="System to mark as pending again", autocomplete=cleared_autocomplete)
    async def siege_unclear(self, ctx, system: str):
        if not await self._channel_ok(ctx):
            return

        systems = get_siege_systems()
        target = find_system(systems, system)
        if not target:
            await ctx.respond(
                f"`{system}` isn't on the board. Pick one from the list.", ephemeral=True
            )
            return

        if not is_cleared(target, current_cycle_start()):
            await ctx.respond(f"**{target['name']}** is already pending.", ephemeral=True)
            return

        set_siege_cleared(target["id"], None)
        target["cleared_at"] = None

        await ctx.respond(embed=build_board_embed(systems, title=f"{target['name']} set back to pending"))

    @siege_group.command(name="add", description="Add a system to the siege board (admin only)")
    @option("system", str, description="Name of the system to add")
    async def siege_add(self, ctx, system: str):
        if not await self._admin_ok(ctx):
            return

        name = system.strip()
        if not name or len(name) > 32:
            await ctx.respond("System names must be between 1 and 32 characters.", ephemeral=True)
            return

        systems = get_siege_systems()
        if find_system(systems, name):
            await ctx.respond(f"**{name}** is already on the board.", ephemeral=True)
            return

        add_siege_system(name)
        systems = get_siege_systems()
        await ctx.respond(
            embed=build_board_embed(systems, title=f"Added {name}", color=discord.Color.green())
        )

    @siege_group.command(name="remove", description="Remove a system from the siege board (admin only)")
    @option("system", str, description="System to remove", autocomplete=all_autocomplete)
    async def siege_remove(self, ctx, system: str):
        if not await self._admin_ok(ctx):
            return

        systems = get_siege_systems()
        target = find_system(systems, system)
        if not target:
            await ctx.respond(
                f"`{system}` isn't on the board. Pick one from the list.", ephemeral=True
            )
            return

        def do_remove():
            remove_siege_system(target["id"])
            return discord.Embed(
                title="System Removed",
                description=f"**{target['name']}** has been removed from the board.",
                color=BLACK,
            )

        async def after_remove():
            # Removing the last pending system can complete the board
            if all_cleared(get_siege_systems()):
                await self._announce_complete()

        prompt = discord.Embed(
            title="Confirm Removal",
            description=f"Remove **{target['name']}** from the siege board?",
            color=discord.Color.red(),
        )
        view = SiegeConfirmView(ctx.author, do_remove, after_remove)
        await ctx.respond(embed=prompt, view=view)

    @siege_group.command(name="reset", description="Set every system back to pending (admin only)")
    async def siege_reset(self, ctx):
        if not await self._admin_ok(ctx):
            return

        def do_reset():
            reset_siege_systems()
            return discord.Embed(
                title="Board Reset",
                description="All systems are pending again.",
                color=BLACK,
            )

        prompt = discord.Embed(
            title="Confirm Reset",
            description="Set **every** system back to pending for this week?",
            color=discord.Color.red(),
        )
        view = SiegeConfirmView(ctx.author, do_reset)
        await ctx.respond(embed=prompt, view=view)


def setup(bot):
    bot.add_cog(Siege(bot))
