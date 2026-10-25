import discord
from discord.ext import commands

BLACK = discord.Color.from_rgb(0, 0, 0)


class ShutdownConfirmView(discord.ui.View):
    def __init__(self, bot, requester, done_embed):
        super().__init__(timeout=60)
        self.bot = bot
        self.requester = requester
        self.done_embed = done_embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                "Only the person who ran this command can confirm or cancel it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.red)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=self.done_embed, view=self)

        # bot.py reads this after bot.run() ends, so py-cord's shutdown error is not shown
        self.bot.exiting = True
        await self.bot.close()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.gray)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        embed = discord.Embed(title="Cancelled", color=discord.Color.red())
        await interaction.response.edit_message(embed=embed, view=self)


admin_group = discord.SlashCommandGroup(
    "admin",
    "Bot administration",
    default_member_permissions=discord.Permissions(administrator=True),
)


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    admin = admin_group

    async def _owner_ok(self, ctx):
        if ctx.author.id == 715509664296599633:
            return True
        await ctx.respond("This command can only be used by the bot owner.", ephemeral=True)
        return False

    @admin_group.command(name="shutdown", description="Turn the bot off completely")
    async def admin_shutdown(self, ctx):
        if not await self._owner_ok(ctx):
            return

        prompt = discord.Embed(
            title="Confirm Shutdown",
            description=(
                "The bot will go offline and **will not come back** until someone starts it again."
            ),
            color=discord.Color.red(),
        )
        done = discord.Embed(
            title="Shutting down…",
            description="The bot is going offline. It stays off until it is started again on.",
            color=BLACK,
        )
        view = ShutdownConfirmView(self.bot, ctx.author, done)
        await ctx.respond(embed=prompt, view=view)


def setup(bot):
    bot.add_cog(Admin(bot))