import discord
from discord.ext import commands
from discord import option

from core.database import set_permission, remove_permission, get_all_permissions
from core.permissions_check import check_and_respond


# These are the internal command_name keys stored in the DB and checked at runtime.
# Format is "group subcommand" to match how check_and_respond() is called in each cog.
COMMAND_CHOICES = [
    "account add",
    "account remove",
    "account show",
    "account find",
    "account transfer",
    "whois",
    "blacklist",
    "permission add",
    "permission remove",
    "permission show",
]


class ConfirmActionView(discord.ui.View):
    def __init__(self, requester, on_confirm, success_message):
        super().__init__(timeout=60)
        self.requester = requester
        self.on_confirm = on_confirm
        self.success_message = success_message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.requester.id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.on_confirm()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=self.success_message, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.gray)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled.", view=self)


permission_group = discord.SlashCommandGroup("permission", "Manage command permissions")


class Permissions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    permission = permission_group

    @permission_group.command(name="add", description="Grant a role access to a command")
    @option("command", str, description="Command name", choices=COMMAND_CHOICES)
    @option("role", discord.Role, description="Role to grant access to")
    async def permission_add(self, ctx, command: str, role: discord.Role):
        if not await check_and_respond(ctx, "permission add"):
            return

        def confirm_action():
            set_permission(command, role.id, role.name)

        view = ConfirmActionView(
            ctx.author,
            confirm_action,
            f"**{role.name}** now has access to `/{command}`",
        )
        await ctx.respond(
            f"Grant **{role.name}** access to `/{command}`?", view=view
        )

    @permission_group.command(name="remove", description="Remove a role's access to a command")
    @option("command", str, description="Command name", choices=COMMAND_CHOICES)
    @option("role", discord.Role, description="Role to remove access from")
    async def permission_remove(self, ctx, command: str, role: discord.Role):
        if not await check_and_respond(ctx, "permission remove"):
            return

        def confirm_action():
            remove_permission(command, role.id)

        view = ConfirmActionView(
            ctx.author,
            confirm_action,
            f"**{role.name}** no longer has access to `/{command}`",
        )
        await ctx.respond(
            f"Remove **{role.name}**'s access to `/{command}`?", view=view
        )

    @permission_group.command(name="show", description="View current role permissions for all commands")
    async def permission_show(self, ctx):
        if not await check_and_respond(ctx, "permission show"):
            return

        all_perms = get_all_permissions()

        embed = discord.Embed(title="Command Permissions", color=discord.Color.from_rgb(0, 0, 0))
        embed.description = "Server admins always have access to every command, regardless of what's listed below."

        if not all_perms:
            embed.add_field(
                name="No custom permissions set",
                value="Only admins can use commands right now.",
                inline=False,
            )
        else:
            grouped = {}
            for row in all_perms:
                grouped.setdefault(row["command_name"], []).append(row["role_name"])

            for command, roles in grouped.items():
                embed.add_field(name=f"/{command}", value=", ".join(roles), inline=False)

        await ctx.respond(embed=embed)


def setup(bot):
    bot.add_cog(Permissions(bot))