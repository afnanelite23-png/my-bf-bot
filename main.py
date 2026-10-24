"""Proof-based jail and moderation bot for Discord."""
import asyncio
import json
import os
import re
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID", "").strip()
DATA_FILE = Path(os.getenv("DATA_FILE", "guild-data.json"))
JAILED_ROLE_NAME = "Jailed"
INFO, OK, WARN, ERROR = 0x5865F2, 0x35A86B, 0xE4A62B, 0xD64545
state = {}


def save_data():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="guild-data-", suffix=".tmp", dir=DATA_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
        os.replace(temp, DATA_FILE)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def get_data(guild_id):
    data = state.setdefault(str(guild_id), {"config": {}, "jails": {}, "appeals": {}, "warnings": []})
    for key, default in (("config", {}), ("jails", {}), ("appeals", {}), ("warnings", [])):
        data.setdefault(key, default)
    return data


def make_embed(title, description, color=INFO):
    embed = discord.Embed(title=title, description=str(description)[:4000], color=color)
    embed.timestamp = discord.utils.utcnow()
    return embed


def staff_ok(member, config):
    if member.guild_permissions.administrator:
        return True
    role_ids = {config.get("staff_role_id"), config.get("senior_mod_role_id")}
    return any(role.id in role_ids for role in member.roles)


def senior_ok(member, config):
    role_id = config.get("senior_mod_role_id")
    return bool(role_id and any(role.id == role_id for role in member.roles))


async def staff_check(interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "Use this in a server."), ephemeral=True)
        return False
    if not staff_ok(interaction.user, get_data(interaction.guild_id)["config"]):
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "You need the configured staff role to use this command."), ephemeral=True)
        return False
    return True


async def admin_check(interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "Use this in a server."), ephemeral=True)
        return False
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "Only server administrators can change bot settings."), ephemeral=True)
        return False
    return True


async def log_action(guild, data, title, text, color=INFO):
    channel_id = data["config"].get("logs_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel:
        try:
            await channel.send(embed=make_embed(title, text, color))
        except (discord.Forbidden, discord.HTTPException):
            pass


async def dm_user(user, text=None, embed=None, view=None):
    try:
        if text and embed is None:
            embed = make_embed("Moderation Bot", text)
            text = None
        await user.send(content=text, embed=embed, view=view)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


def jail_overwrite():
    return discord.PermissionOverwrite(
        send_messages=False, add_reactions=False, connect=False, speak=False,
        create_public_threads=False, create_private_threads=False,
        send_messages_in_threads=False, use_application_commands=False,
    )


async def restrict_channel(channel, role):
    if not hasattr(channel, "set_permissions"):
        return
    try:
        await channel.set_permissions(role, overwrite=jail_overwrite(), reason="Jail role restrictions")
    except (discord.Forbidden, discord.HTTPException) as exc:
        print(f"Could not restrict {getattr(channel, 'name', 'channel')}: {exc}")


async def jailed_role(guild):
    role = discord.utils.get(guild.roles, name=JAILED_ROLE_NAME)
    if role is None:
        role = await guild.create_role(name=JAILED_ROLE_NAME, colour=discord.Colour.dark_grey(),
                                       reason="Created automatically for jail moderation")
    await asyncio.gather(*(restrict_channel(ch, role) for ch in guild.channels))
    return role


async def restore_roles(guild, user_id, record):
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None, [], False
    restored = []
    if record:
        for rid in record.get("previous_role_ids", []):
            role = guild.get_role(int(rid))
            if role and not role.managed and role not in member.roles:
                try:
                    await member.add_roles(role, reason="Jail released; restoring saved roles")
                    restored.append(role.id)
                except (discord.Forbidden, discord.HTTPException):
                    pass
    jail_role = discord.utils.get(guild.roles, name=JAILED_ROLE_NAME)
    if jail_role and jail_role in member.roles:
        try:
            await member.remove_roles(jail_role, reason="Jail released")
        except (discord.Forbidden, discord.HTTPException):
            return member, restored, False
    return member, restored, True


def duration_seconds(value):
    match = re.fullmatch(r"([1-9]\d*)(s|m|h|d)", value.strip(), re.I)
    if not match:
        return None
    seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2).lower()]
    return seconds if seconds <= 28 * 86400 else None


def parse_level_roles(value):
    """Parse comma-separated Fruit:Level entries; Flame | Lv. 1 is always included."""
    roles = [("Flame", 1)]
    seen = {"flame"}
    if not value.strip():
        return roles
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"Use Fruit:Level format for {item!r}, for example Lightning:30.")
        fruit, level_text = item.rsplit(":", 1)
        fruit = fruit.strip()
        if not fruit or len(fruit) > 80:
            raise ValueError("Fruit names must contain 1 to 80 characters.")
        if not level_text.strip().isdigit() or int(level_text.strip()) < 1:
            raise ValueError(f"Level for {fruit} must be a positive whole number.")
        if fruit.casefold() in seen:
            if fruit.casefold() == "flame" and int(level_text.strip()) == 1:
                continue
            raise ValueError(f"{fruit} appears more than once.")
        seen.add(fruit.casefold())
        roles.append((fruit, int(level_text.strip())))
    return sorted(roles, key=lambda item: (item[1], item[0].casefold()))


async def create_level_roles(guild, entries):
    existing = {role.name.casefold() for role in guild.roles}
    created, already = [], []
    for fruit, level in entries:
        role_name = f"{fruit} | Lv. {level}"
        if role_name.casefold() in existing:
            already.append(role_name)
            continue
        role = await guild.create_role(name=role_name, reason="Level roles created by administrator command")
        created.append(role.name)
        existing.add(role_name.casefold())
    return created, already


class AppealStartView(discord.ui.View):
    def __init__(self, guild_id, user_id):
        super().__init__(timeout=None)
        self.guild_id, self.user_id = guild_id, user_id
        button = discord.ui.Button(label="Appeal this jail", style=discord.ButtonStyle.primary,
                                   custom_id=f"appeal:start:{guild_id}:{user_id}")
        button.callback = self.start
        self.add_item(button)

    async def start(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=make_embed("Moderation Bot", "Only the jailed member can use this button."), ephemeral=True)
            return
        data = get_data(self.guild_id)
        if str(self.user_id) not in data["jails"]:
            await interaction.response.send_message(embed=make_embed("Moderation Bot", "There is no active jail record for you."), ephemeral=True)
            return
        if any(a.get("user_id") == self.user_id and a.get("status") == "pending" for a in data["appeals"].values()):
            await interaction.response.send_message(embed=make_embed("Moderation Bot", "You already have an appeal awaiting review."), ephemeral=True)
            return
        await interaction.response.send_modal(AppealModal(self.guild_id, self.user_id))


class AppealModal(discord.ui.Modal, title="Appeal your jail"):
    reason = discord.ui.TextInput(label="Why should staff review this jail?",
                                  style=discord.TextStyle.paragraph, min_length=10, max_length=1500)

    def __init__(self, guild_id, user_id):
        super().__init__(timeout=300, custom_id=f"appeal:modal:{guild_id}:{user_id}")
        self.guild_id, self.user_id = guild_id, user_id

    async def on_submit(self, interaction):
        data = get_data(self.guild_id)
        jail = data["jails"].get(str(self.user_id))
        if interaction.user.id != self.user_id or not jail:
            await interaction.response.send_message(embed=make_embed("Moderation Bot", "This appeal is no longer available."), ephemeral=True)
            return
        if any(a.get("user_id") == self.user_id and a.get("status") == "pending" for a in data["appeals"].values()):
            await interaction.response.send_message(embed=make_embed("Moderation Bot", "You already have an appeal awaiting review."), ephemeral=True)
            return
        guild = bot.get_guild(self.guild_id)
        channel_id = data["config"].get("appeal_channel_id")
        channel = guild.get_channel(channel_id) if guild and channel_id else None
        if channel is None:
            await interaction.response.send_message(embed=make_embed("Moderation Bot", "Appeal channel unavailable. Ask staff to run /setappealchan."), ephemeral=True)
            return
        appeal_id = f"{int(time.time() * 1000):x}-{os.urandom(3).hex()}"
        reason = str(self.reason.value)
        data["appeals"][appeal_id] = {"user_id": self.user_id, "reason": reason,
                                      "status": "pending", "submitted_at": int(time.time())}
        save_data()
        details = (f"Member: {self.user_id}\nOriginal reason: {str(jail.get('reason', 'Not provided'))[:900]}\n"
                   f"Appeal: {reason}\nProof: {jail.get('proof_url') or 'Not available'}\nAppeal ID: {appeal_id}")
        try:
            await channel.send(embed=make_embed("Jail appeal", details, WARN),
                               view=AppealDecisionView(self.guild_id, appeal_id),
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            data["appeals"][appeal_id]["status"] = "delivery_failed"
            save_data()
            await interaction.response.send_message(embed=make_embed("Moderation Bot", "I could not send the appeal. Please contact staff."), ephemeral=True)
            return
        await log_action(guild, data, "Jail appeal submitted", f"Member: {self.user_id}\nAppeal ID: {appeal_id}")
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "Your appeal was sent to senior moderators for review."), ephemeral=True)


class AppealDecisionView(discord.ui.View):
    def __init__(self, guild_id, appeal_id):
        super().__init__(timeout=None)
        self.guild_id, self.appeal_id = guild_id, appeal_id
        yes = discord.ui.Button(label="Accept and unjail", style=discord.ButtonStyle.success,
                                custom_id=f"appeal:accept:{guild_id}:{appeal_id}")
        no = discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger,
                               custom_id=f"appeal:deny:{guild_id}:{appeal_id}")
        yes.callback = self.accept
        no.callback = self.deny
        self.add_item(yes)
        self.add_item(no)

    async def decide(self, interaction, accept):
        if interaction.guild_id != self.guild_id or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(embed=make_embed("Moderation Bot", "This appeal can only be decided in its server."), ephemeral=True)
            return
        data = get_data(self.guild_id)
        if not senior_ok(interaction.user, data["config"]):
            await interaction.response.send_message(embed=make_embed("Moderation Bot", "Only the configured senior mod role can decide appeals."), ephemeral=True)
            return
        appeal = data["appeals"].get(self.appeal_id)
        if not appeal or appeal.get("status") != "pending":
            await interaction.response.send_message(embed=make_embed("Moderation Bot", "This appeal is already decided or unavailable."), ephemeral=True)
            return
        user_id = int(appeal["user_id"])
        guild = interaction.guild
        if accept:
            _, _, released = await restore_roles(guild, user_id, data["jails"].get(str(user_id)))
            if not released:
                await interaction.response.send_message(embed=make_embed("Moderation Bot", "Could not remove the jail role. Check bot role position and permissions."), ephemeral=True)
                return
            data["jails"].pop(str(user_id), None)
        status = "accepted" if accept else "denied"
        appeal["status"] = status
        appeal["decided_by"] = interaction.user.id
        appeal["decided_at"] = int(time.time())
        save_data()
        target = guild.get_member(user_id)
        if target:
            suffix = " Your jail was lifted and saved roles restored." if accept else ""
            await dm_user(target, f"Your jail appeal in {guild.name} was {status} by {interaction.user}.{suffix}")
        old = interaction.message.embeds[0].description if interaction.message.embeds else "Appeal details unavailable."
        await interaction.response.edit_message(
            embed=make_embed(f"Appeal {status}", f"{old}\n\nDecision: {status} by {interaction.user}",
                             OK if accept else ERROR), view=None)
        await log_action(guild, data, f"Appeal {status}", f"Member: {user_id}\nSenior mod: {interaction.user}",
                         OK if accept else ERROR)

    async def accept(self, interaction):
        await self.decide(interaction, True)

    async def deny(self, interaction):
        await self.decide(interaction, False)


class ModerationBot(commands.Bot):
    async def setup_hook(self):
        global state
        try:
            state = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        for guild_key, data in state.items():
            guild_id = int(guild_key)
            for user_id in data.get("jails", {}):
                self.add_view(AppealStartView(guild_id, int(user_id)))
            for appeal_id, appeal in data.get("appeals", {}).items():
                if appeal.get("status") == "pending":
                    self.add_view(AppealDecisionView(guild_id, appeal_id))
        try:
            if TEST_GUILD_ID.isdigit():
                test_guild = discord.Object(id=int(TEST_GUILD_ID))
                self.tree.copy_global_to(guild=test_guild)
                await self.tree.sync(guild=test_guild)
            else:
                await self.tree.sync()
        except discord.HTTPException as exc:
            print(f"Slash command sync failed: {exc}")


intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = ModerationBot(command_prefix=";", intents=intents, help_command=None)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}.")


@bot.event
async def on_guild_channel_create(channel):
    role = discord.utils.get(channel.guild.roles, name=JAILED_ROLE_NAME)
    if role:
        await restrict_channel(channel, role)


@bot.tree.command(name="jail", description="Jail a member. Attach proof or the action is cancelled.")
@app_commands.describe(user="Member to jail", reason="Reason for jail", proof="Evidence (required)")
async def jail(interaction: discord.Interaction, user: discord.Member, reason: str,
               proof: discord.Attachment = None):
    if not await staff_check(interaction):
        return
    if proof is None:
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "Jail cancelled: attach proof. No jail was applied."), ephemeral=True)
        return
    data = get_data(interaction.guild_id)
    if str(user.id) in data["jails"]:
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "That member is already jailed."), ephemeral=True)
        return
    if user.id in (interaction.user.id, interaction.guild.owner_id):
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "That member cannot be jailed."), ephemeral=True)
        return
    if user.top_role >= interaction.user.top_role and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "You cannot jail a member with an equal or higher role."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    role, previous = None, []
    try:
        role = await jailed_role(interaction.guild)
        previous = [r.id for r in user.roles if r.id != interaction.guild.id and not r.managed]
        data["jails"][str(user.id)] = {"previous_role_ids": previous, "moderator_id": interaction.user.id,
                                       "reason": reason, "proof_url": proof.url, "created_at": int(time.time())}
        save_data()
        await user.add_roles(role, reason=f"Jailed by {interaction.user}: {reason[:400]}")
        old_roles = [r for r in user.roles if r.id in previous]
        if old_roles:
            await user.remove_roles(*old_roles, reason=f"Jailed by {interaction.user}")
    except (discord.Forbidden, discord.HTTPException) as exc:
        data["jails"].pop(str(user.id), None)
        save_data()
        try:
            if role and role in user.roles:
                await user.remove_roles(role, reason="Rolling back unsuccessful jail action")
            restore = [interaction.guild.get_role(rid) for rid in previous]
            restore = [r for r in restore if r and not r.managed and r not in user.roles]
            if restore:
                await user.add_roles(*restore, reason="Rolling back unsuccessful jail action")
        except (discord.Forbidden, discord.HTTPException):
            pass
        await interaction.followup.send(embed=make_embed("Jail failed", f"Check bot role hierarchy and permissions. ({exc})", ERROR), ephemeral=True)
        return
    dm_ok = await dm_user(
        user,
        embed=make_embed("You have been jailed",
                         f"Server: {interaction.guild.name}\nReason: {reason[:900]}\nProof: {proof.url}\n\nUse the button below to appeal.",
                         WARN),
        view=AppealStartView(interaction.guild_id, user.id),
    )
    await log_action(interaction.guild, data, "Member jailed",
                     f"Member: {user} ({user.id})\nStaff: {interaction.user}\nReason: {reason[:900]}\n"
                     f"Proof: {proof.url}\nDM delivered: {'Yes' if dm_ok else 'No'}", WARN)
    await interaction.followup.send(
        embed=make_embed("Jail applied", f"{user.mention} jailed; proof recorded.{'' if dm_ok else ' DM could not be delivered.'}", OK),
        ephemeral=True,
    )


@bot.tree.command(name="unjail", description="Release a member and restore saved roles.")
async def unjail(interaction: discord.Interaction, user: discord.User, reason: str = None):
    if not await staff_check(interaction):
        return
    data = get_data(interaction.guild_id)
    record = data["jails"].get(str(user.id))
    member, restored, released = await restore_roles(interaction.guild, user.id, record)
    if not released:
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "Could not release member. Check bot role position and permissions."), ephemeral=True)
        return
    if not record and member is None:
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "No active jail record was found."), ephemeral=True)
        return
    data["jails"].pop(str(user.id), None)
    save_data()
    note = reason or "No additional note"
    dm_ok = await dm_user(user, f"Your jail in {interaction.guild.name} was lifted by {interaction.user}.\nNote: {note}")
    await log_action(interaction.guild, data, "Member unjailed",
                     f"Member: {user} ({user.id})\nStaff: {interaction.user}\nNote: {note[:500]}\n"
                     f"Roles restored: {len(restored)}\nDM delivered: {'Yes' if dm_ok else 'No'}", OK)
    await interaction.response.send_message(embed=make_embed("Moderation Bot", 
        f"{user.mention} released; {len(restored)} saved role(s) restored.{'' if dm_ok else ' DM could not be delivered.'}"), ephemeral=True)


@bot.tree.command(name="flame", description="Give a member the configured Level 1 Flame role.")
async def flame(interaction: discord.Interaction, user: discord.Member):
    if not await staff_check(interaction):
        return
    data = get_data(interaction.guild_id)
    role = interaction.guild.get_role(data["config"].get("flame_role_id", 0))
    if role is None:
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "Configure this first with /setflame."), ephemeral=True)
        return
    try:
        await user.add_roles(role, reason=f"Level 1 Flame granted by {interaction.user}")
    except (discord.Forbidden, discord.HTTPException):
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "Could not give role. Check bot role hierarchy and permissions."), ephemeral=True)
        return
    await log_action(interaction.guild, data, "Flame role granted", f"Member: {user}\nStaff: {interaction.user}\nRole: {role}", OK)
    await interaction.response.send_message(embed=make_embed("Moderation Bot", f"Gave {role.mention} to {user.mention}."), ephemeral=True)


@bot.tree.command(name="warn", description="Issue a warning to a member.")
async def warn(interaction: discord.Interaction, user: discord.User, reason: str):
    if not await staff_check(interaction):
        return
    data = get_data(interaction.guild_id)
    data["warnings"].append({"user_id": user.id, "moderator_id": interaction.user.id,
                             "reason": reason, "created_at": int(time.time())})
    save_data()
    dm_ok = await dm_user(user, f"Warning in {interaction.guild.name}.\nReason: {reason}\nStaff: {interaction.user}")
    await log_action(interaction.guild, data, "Member warned",
                     f"Member: {user} ({user.id})\nStaff: {interaction.user}\nReason: {reason[:900]}\n"
                     f"DM delivered: {'Yes' if dm_ok else 'No'}", WARN)
    await interaction.response.send_message(embed=make_embed("Moderation Bot", f"Warned {user}.{'' if dm_ok else ' DM could not be delivered.'}"), ephemeral=True)


@bot.tree.command(name="mute", description="Temporarily timeout a member (maximum 28 days).")
@app_commands.describe(duration="For example 10m, 2h, or 1d (max 28 days)")
async def mute(interaction: discord.Interaction, user: discord.Member, duration: str, reason: str):
    if not await staff_check(interaction):
        return
    seconds = duration_seconds(duration)
    if seconds is None:
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "Use 10m, 2h, or 1d (maximum 28 days)."), ephemeral=True)
        return
    if user.id in (interaction.user.id, interaction.guild.owner_id):
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "That member cannot be timed out."), ephemeral=True)
        return
    try:
        await user.timeout(discord.utils.utcnow() + timedelta(seconds=seconds), reason=f"{interaction.user}: {reason[:400]}")
    except (discord.Forbidden, discord.HTTPException):
        await interaction.response.send_message(embed=make_embed("Moderation Bot", "Could not timeout member. Check bot role hierarchy and permissions."), ephemeral=True)
        return
    data = get_data(interaction.guild_id)
    dm_ok = await dm_user(user, f"Timeout in {interaction.guild.name} for {duration}.\nReason: {reason}")
    await log_action(interaction.guild, data, "Member timed out",
                     f"Member: {user} ({user.id})\nStaff: {interaction.user}\nDuration: {duration}\n"
                     f"Reason: {reason[:900]}\nDM delivered: {'Yes' if dm_ok else 'No'}", WARN)
    await interaction.response.send_message(embed=make_embed("Moderation Bot", f"Timed out {user.mention} for {duration}."), ephemeral=True)


@bot.tree.command(name="setflame", description="Set the Level 1 Flame role.")
@app_commands.default_permissions(administrator=True)
async def setflame(interaction: discord.Interaction, role: discord.Role):
    if not await admin_check(interaction): return
    get_data(interaction.guild_id)["config"]["flame_role_id"] = role.id
    save_data()
    await interaction.response.send_message(embed=make_embed("Moderation Bot", f"Level 1 Flame role set to {role.mention}."), ephemeral=True)


@bot.tree.command(name="setappealchan", description="Set where jail appeals are sent.")
@app_commands.default_permissions(administrator=True)
async def setappealchan(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await admin_check(interaction): return
    get_data(interaction.guild_id)["config"]["appeal_channel_id"] = channel.id
    save_data()
    await interaction.response.send_message(embed=make_embed("Moderation Bot", f"Appeals will be sent to {channel.mention}."), ephemeral=True)


@bot.tree.command(name="setseniormod", description="Set the role that can decide appeals.")
@app_commands.default_permissions(administrator=True)
async def setseniormod(interaction: discord.Interaction, role: discord.Role):
    if not await admin_check(interaction): return
    get_data(interaction.guild_id)["config"]["senior_mod_role_id"] = role.id
    save_data()
    await interaction.response.send_message(embed=make_embed("Moderation Bot", f"Senior moderator role set to {role.mention}."), ephemeral=True)


@bot.tree.command(name="setstaff", description="Set the role allowed to use moderation commands.")
@app_commands.default_permissions(administrator=True)
async def setstaff(interaction: discord.Interaction, role: discord.Role):
    if not await admin_check(interaction): return
    get_data(interaction.guild_id)["config"]["staff_role_id"] = role.id
    save_data()
    await interaction.response.send_message(embed=make_embed("Moderation Bot", f"Staff role set to {role.mention}."), ephemeral=True)


@bot.tree.command(name="setlogs", description="Set the moderation log channel.")
@app_commands.default_permissions(administrator=True)
async def setlogs(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await admin_check(interaction): return
    get_data(interaction.guild_id)["config"]["logs_channel_id"] = channel.id
    save_data()
    await interaction.response.send_message(embed=make_embed("Moderation Bot", f"Moderation logs will be sent to {channel.mention}."), ephemeral=True)


@bot.tree.command(name="createlevels", description="Create Blox Fruits level roles from fruit:level pairs.")
@app_commands.describe(fruits_and_levels="Comma-separated pairs, for example Lightning:30, Ice:60. Flame | Lv. 1 is added automatically.")
@app_commands.default_permissions(administrator=True)
async def createlevels(interaction: discord.Interaction, fruits_and_levels: str):
    if not await admin_check(interaction):
        return
    try:
        entries = parse_level_roles(fruits_and_levels)
        created, already = await create_level_roles(interaction.guild, entries)
    except ValueError as exc:
        await interaction.response.send_message(
            embed=make_embed("Invalid level list", str(exc), ERROR), ephemeral=True
        )
        return
    except (discord.Forbidden, discord.HTTPException) as exc:
        await interaction.response.send_message(
            embed=make_embed("Role creation failed", f"Check the bot's Manage Roles permission and role position. {exc}", ERROR),
            ephemeral=True,
        )
        return
    summary = []
    if created:
        summary.append("Created:\n" + "\n".join(created))
    if already:
        summary.append("Already existed:\n" + "\n".join(already))
    await interaction.response.send_message(
        embed=make_embed("Level roles ready", "\n\n".join(summary) or "No roles needed to be created.", OK),
        ephemeral=True,
    )


@bot.event
async def on_guild_channel_create(channel):
    role = discord.utils.get(channel.guild.roles, name=JAILED_ROLE_NAME)
    if role:
        await restrict_channel(channel, role)


async def prefix_allowed(ctx, admin=False):
    if ctx.guild is None:
        await ctx.send(embed=make_embed("Command unavailable", "Use this command in a server.", ERROR))
        return False
    if admin:
        if ctx.author.guild_permissions.administrator:
            return True
        await ctx.send(embed=make_embed("Permission denied", "Only server administrators can change bot settings.", ERROR))
        return False
    if staff_ok(ctx.author, get_data(ctx.guild.id)["config"]):
        return True
    await ctx.send(embed=make_embed("Permission denied", "You need the configured staff role to use this command.", ERROR))
    return False


@bot.command(name="jail")
async def prefix_jail(ctx, user: discord.Member, *, reason: str):
    if not await prefix_allowed(ctx):
        return
    if not ctx.message.attachments:
        await ctx.send(embed=make_embed("Jail cancelled", "Attach proof to this message. No jail was applied.", ERROR))
        return
    data = get_data(ctx.guild.id)
    if str(user.id) in data["jails"]:
        await ctx.send(embed=make_embed("Already jailed", "That member already has an active jail record.", WARN))
        return
    if user.id in (ctx.author.id, ctx.guild.owner_id) or user.top_role >= ctx.author.top_role:
        await ctx.send(embed=make_embed("Jail cancelled", "You cannot jail that member due to the target or role hierarchy.", ERROR))
        return
    await ctx.typing()
    role, previous = None, []
    try:
        role = await jailed_role(ctx.guild)
        proof = ctx.message.attachments[0]
        previous = [r.id for r in user.roles if r.id != ctx.guild.id and not r.managed]
        data["jails"][str(user.id)] = {"previous_role_ids": previous, "moderator_id": ctx.author.id,
                                       "reason": reason, "proof_url": proof.url, "created_at": int(time.time())}
        save_data()
        await user.add_roles(role, reason=f"Jailed by {ctx.author}: {reason[:400]}")
        old_roles = [r for r in user.roles if r.id in previous]
        if old_roles:
            await user.remove_roles(*old_roles, reason=f"Jailed by {ctx.author}")
    except (discord.Forbidden, discord.HTTPException) as exc:
        data["jails"].pop(str(user.id), None)
        save_data()
        try:
            if role and role in user.roles:
                await user.remove_roles(role, reason="Rolling back unsuccessful jail action")
            restore = [ctx.guild.get_role(rid) for rid in previous]
            restore = [r for r in restore if r and not r.managed and r not in user.roles]
            if restore:
                await user.add_roles(*restore, reason="Rolling back unsuccessful jail action")
        except (discord.Forbidden, discord.HTTPException):
            pass
        await ctx.send(embed=make_embed("Jail failed", f"Check bot role hierarchy and permissions. {exc}", ERROR))
        return
    proof = ctx.message.attachments[0]
    dm_ok = await dm_user(user, embed=make_embed(
        "You have been jailed",
        f"Server: {ctx.guild.name}\nReason: {reason[:900]}\nProof: {proof.url}\n\nUse the button below to appeal.",
        WARN), view=AppealStartView(ctx.guild.id, user.id))
    await log_action(ctx.guild, data, "Member jailed",
                     f"Member: {user} ({user.id})\nStaff: {ctx.author}\nReason: {reason[:900]}\nProof: {proof.url}", WARN)
    await ctx.send(embed=make_embed("Jail applied", f"{user.mention} was jailed. DM delivered: {'Yes' if dm_ok else 'No'}.", OK))


@bot.command(name="unjail")
async def prefix_unjail(ctx, user: discord.Member, *, reason: str = "No additional note"):
    if not await prefix_allowed(ctx):
        return
    data = get_data(ctx.guild.id)
    record = data["jails"].get(str(user.id))
    member, restored, released = await restore_roles(ctx.guild, user.id, record)
    if not released:
        await ctx.send(embed=make_embed("Unjail failed", "Check bot role position and permissions.", ERROR))
        return
    if not record and member is None:
        await ctx.send(embed=make_embed("No jail record", "No active jail record was found for that member.", WARN))
        return
    data["jails"].pop(str(user.id), None)
    save_data()
    dm_ok = await dm_user(user, f"Your jail in {ctx.guild.name} was lifted by {ctx.author}.\nNote: {reason}")
    await log_action(ctx.guild, data, "Member unjailed", f"Member: {user}\nStaff: {ctx.author}\nNote: {reason}", OK)
    await ctx.send(embed=make_embed("Member unjailed",
                                    f"{user.mention} released; {len(restored)} saved role(s) restored. DM delivered: {'Yes' if dm_ok else 'No'}.", OK))


@bot.command(name="flame")
async def prefix_flame(ctx, user: discord.Member):
    if not await prefix_allowed(ctx):
        return
    data = get_data(ctx.guild.id)
    role = ctx.guild.get_role(data["config"].get("flame_role_id", 0))
    if role is None:
        await ctx.send(embed=make_embed("Flame role not set", "An administrator must run ;setflame first.", WARN))
        return
    try:
        await user.add_roles(role, reason=f"Level 1 Flame granted by {ctx.author}")
    except (discord.Forbidden, discord.HTTPException):
        await ctx.send(embed=make_embed("Role assignment failed", "Check bot role hierarchy and permissions.", ERROR))
        return
    await log_action(ctx.guild, data, "Flame role granted", f"Member: {user}\nStaff: {ctx.author}\nRole: {role}", OK)
    await ctx.send(embed=make_embed("Flame role granted", f"Gave {role.mention} to {user.mention}.", OK))


@bot.command(name="warn")
async def prefix_warn(ctx, user: discord.User, *, reason: str):
    if not await prefix_allowed(ctx):
        return
    data = get_data(ctx.guild.id)
    data["warnings"].append({"user_id": user.id, "moderator_id": ctx.author.id,
                             "reason": reason, "created_at": int(time.time())})
    save_data()
    dm_ok = await dm_user(user, f"Warning in {ctx.guild.name}.\nReason: {reason}\nStaff: {ctx.author}")
    await log_action(ctx.guild, data, "Member warned", f"Member: {user} ({user.id})\nStaff: {ctx.author}\nReason: {reason}", WARN)
    await ctx.send(embed=make_embed("Warning issued", f"Warned {user}. DM delivered: {'Yes' if dm_ok else 'No'}.", OK))


@bot.command(name="mute")
async def prefix_mute(ctx, user: discord.Member, duration: str, *, reason: str):
    if not await prefix_allowed(ctx):
        return
    seconds = duration_seconds(duration)
    if seconds is None:
        await ctx.send(embed=make_embed("Invalid duration", "Use 10m, 2h, or 1d (maximum 28 days).", ERROR))
        return
    if user.id in (ctx.author.id, ctx.guild.owner_id):
        await ctx.send(embed=make_embed("Timeout cancelled", "That member cannot be timed out.", ERROR))
        return
    try:
        await user.timeout(discord.utils.utcnow() + timedelta(seconds=seconds), reason=f"{ctx.author}: {reason[:400]}")
    except (discord.Forbidden, discord.HTTPException):
        await ctx.send(embed=make_embed("Timeout failed", "Check bot role hierarchy and permissions.", ERROR))
        return
    data = get_data(ctx.guild.id)
    dm_ok = await dm_user(user, f"Timeout in {ctx.guild.name} for {duration}.\nReason: {reason}")
    await log_action(ctx.guild, data, "Member timed out", f"Member: {user}\nStaff: {ctx.author}\nDuration: {duration}\nReason: {reason}", WARN)
    await ctx.send(embed=make_embed("Member timed out", f"Timed out {user.mention} for {duration}. DM delivered: {'Yes' if dm_ok else 'No'}.", OK))


@bot.command(name="setflame")
async def prefix_setflame(ctx, role: discord.Role):
    if not await prefix_allowed(ctx, admin=True): return
    get_data(ctx.guild.id)["config"]["flame_role_id"] = role.id
    save_data()
    await ctx.send(embed=make_embed("Setting saved", f"Level 1 Flame role set to {role.mention}.", OK))


@bot.command(name="setappealchan")
async def prefix_setappealchan(ctx, channel: discord.TextChannel):
    if not await prefix_allowed(ctx, admin=True): return
    get_data(ctx.guild.id)["config"]["appeal_channel_id"] = channel.id
    save_data()
    await ctx.send(embed=make_embed("Setting saved", f"Appeals will be sent to {channel.mention}.", OK))


@bot.command(name="setseniormod")
async def prefix_setseniormod(ctx, role: discord.Role):
    if not await prefix_allowed(ctx, admin=True): return
    get_data(ctx.guild.id)["config"]["senior_mod_role_id"] = role.id
    save_data()
    await ctx.send(embed=make_embed("Setting saved", f"Senior moderator role set to {role.mention}.", OK))


@bot.command(name="setstaff")
async def prefix_setstaff(ctx, role: discord.Role):
    if not await prefix_allowed(ctx, admin=True): return
    get_data(ctx.guild.id)["config"]["staff_role_id"] = role.id
    save_data()
    await ctx.send(embed=make_embed("Setting saved", f"Staff role set to {role.mention}.", OK))


@bot.command(name="setlogs")
async def prefix_setlogs(ctx, channel: discord.TextChannel):
    if not await prefix_allowed(ctx, admin=True): return
    get_data(ctx.guild.id)["config"]["logs_channel_id"] = channel.id
    save_data()
    await ctx.send(embed=make_embed("Setting saved", f"Moderation logs will be sent to {channel.mention}.", OK))


@bot.command(name="createlevels")
async def prefix_createlevels(ctx, *, fruits_and_levels: str):
    if not await prefix_allowed(ctx, admin=True):
        return
    try:
        entries = parse_level_roles(fruits_and_levels)
        created, already = await create_level_roles(ctx.guild, entries)
    except ValueError as exc:
        await ctx.send(embed=make_embed("Invalid level list", str(exc), ERROR))
        return
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(embed=make_embed("Role creation failed",
                                        f"Check the bot's Manage Roles permission and role position. {exc}", ERROR))
        return
    summary = []
    if created:
        summary.append("Created:\n" + "\n".join(created))
    if already:
        summary.append("Already existed:\n" + "\n".join(already))
    await ctx.send(embed=make_embed("Level roles ready", "\n\n".join(summary) or "No roles needed to be created.", OK))


@bot.command(name="help")
async def prefix_help(ctx):
    commands_text = (
        ";jail @member reason + attach proof\n"
        ";unjail @member [note]\n;flame @member\n;warn @member reason\n;mute @member 10m reason\n"
        ";setflame @role\n;setappealchan #channel\n;setseniormod @role\n;setstaff @role\n;setlogs #channel\n"
        ";createlevels Lightning:30, Ice:60 (admins; Flame | Lv. 1 is added automatically)\n"
        "Slash commands are also available. Configuration commands require an administrator; moderation commands require staff."
    )
    await ctx.send(embed=make_embed("Moderation Bot Commands", commands_text))


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        message = f"Missing argument: {error.param.name}. Use ;help to see command formats."
    elif isinstance(error, commands.BadArgument):
        message = "I could not find that member, role, or channel. Mention it or provide its ID."
    else:
        print(f"Prefix command error: {error}")
        message = "The command failed. Check bot permissions and role order."
    await ctx.send(embed=make_embed("Command error", message, ERROR))


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your hosting panel's environment variables.")
    bot.run(TOKEN)
