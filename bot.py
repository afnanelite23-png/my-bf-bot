import asyncio
from datetime import datetime, timezone
import json
import os
import discord
from discord.ext import commands
from flask import Flask

# --- Flask Server for Render/UptimeRobot 24/7 Uptime ---
app = Flask(__name__)


@app.route("/")
def home():
  return "Bot is alive and running!"


def run_flask():
  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)


# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix=">", intents=intents)

CONFIG_FILE = "config.json"


def load_data():
  if not os.path.exists(CONFIG_FILE):
    return {
        "guilds": {},
        "modstats": {},
        "warns": {},
        "appeal_cooldowns": {},
        "jail_info": {},  # Format: {guild_id: {user_id: {"reason": str, "staff": str}}}
    }
  with open(CONFIG_FILE, "r") as f:
    data = json.load(f)
    if "warns" not in data:
      data["warns"] = {}
    if "appeal_cooldowns" not in data:
      data["appeal_cooldowns"] = {}
    if "jail_info" not in data:
      data["jail_info"] = {}
    return data


def save_data(data):
  with open(CONFIG_FILE, "w") as f:
    json.dump(data, f, indent=4)


def get_config(guild_id, key):
  data = load_data()
  g_id = str(guild_id)
  if g_id not in data["guilds"]:
    return None
  return data["guilds"][g_id].get(key)


def set_config(guild_id, key, value):
  data = load_data()
  g_id = str(guild_id)
  if g_id not in data["guilds"]:
    data["guilds"][g_id] = {}
  data["guilds"][g_id][key] = value
  save_data(data)


async def send_log(guild, embed):
  log_channel_id = get_config(guild.id, "log_channel")
  if log_channel_id:
    channel = guild.get_channel(int(log_channel_id))
    if channel:
      try:
        await channel.send(embed=embed)
      except Exception:
        pass


def add_stat(guild_id, staff_id, action_type):
  data = load_data()
  g_id = str(guild_id)
  s_id = str(staff_id)
  if g_id not in data["modstats"]:
    data["modstats"][g_id] = {}
  if s_id not in data["modstats"][g_id]:
    data["modstats"][g_id][s_id] = {"jails": 0, "mutes": 0, "warns": 0}
  if action_type in data["modstats"][g_id][s_id]:
    data["modstats"][g_id][s_id][action_type] += 1
  save_data(data)


def add_warn(guild_id, user_id, reason, staff_name):
  data = load_data()
  g_id = str(guild_id)
  u_id = str(user_id)
  if g_id not in data["warns"]:
    data["warns"][g_id] = {}
  if u_id not in data["warns"][g_id]:
    data["warns"][g_id][u_id] = []
  data["warns"][g_id][u_id].append({"reason": reason, "staff": str(staff_name)})
  save_data(data)


# --- UI Views & Modals ---


class AppealModal(discord.ui.Modal, title="Submit Your Jail Appeal"):
  reason = discord.ui.TextInput(
      label="Why should your jail be revoked?",
      style=discord.TextStyle.long,
      placeholder="Explain your case clearly...",
      required=True,
      max_length=1000,
  )

  def __init__(self, guild_id):
    super().__init__()
    self.guild_id = guild_id

  async def on_submit(self, interaction: discord.Interaction):
    data = load_data()
    appeal_channel_id = get_config(self.guild_id, "appeal_channel")
    if not appeal_channel_id:
      await interaction.response.send_message(
          "Appeal channel is not configured on this server.", ephemeral=True
      )
      return

    g_id_str = str(self.guild_id)
    u_id_str = str(interaction.user.id)
    if g_id_str not in data["appeal_cooldowns"]:
      data["appeal_cooldowns"][g_id_str] = {}
    data["appeal_cooldowns"][g_id_str][u_id_str] = discord.utils.utcnow().isoformat()
    save_data(data)

    guild = bot.get_guild(self.guild_id)
    channel = guild.get_channel(int(appeal_channel_id))

    proof_url = data.get("temp_proof", {}).get(u_id_str)
    j_info = data.get("jail_info", {}).get(g_id_str, {}).get(u_id_str, {"reason": "Not specified", "staff": "Unknown"})

    embed = discord.Embed(
        title="New Jail Appeal Submitted",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="User", value=f"{interaction.user} ({interaction.user.id})", inline=False)
    embed.add_field(name="Jailed By", value=j_info["staff"], inline=True)
    embed.add_field(name="Jail Reason", value=j_info["reason"], inline=True)
    embed.add_field(name="Appeal Reason", value=self.reason.value, inline=False)
    if proof_url:
      embed.set_image(url=proof_url)

    view = SeniorReviewView(interaction.user.id, self.guild_id)
    await channel.send(embed=embed, view=view)
    await interaction.response.send_message(
        "Your appeal has been successfully sent to the senior staff team!", ephemeral=True
    )


class AppealButtonView(discord.ui.View):

  def __init__(self, guild_id):
    super().__init__(timeout=None)
    self.guild_id = guild_id

  @discord.ui.button(label="Appeal Jail", style=discord.ButtonStyle.primary, custom_id="appeal_jail_btn")
  async def appeal_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    data = load_data()
    g_id_str = str(self.guild_id)
    u_id_str = str(interaction.user.id)

    cooldowns = data.get("appeal_cooldowns", {}).get(g_id_str, {})
    last_appeal = cooldowns.get(u_id_str)

    if last_appeal:
      last_time = datetime.fromisoformat(last_appeal)
      now = discord.utils.utcnow()
      elapsed_seconds = (now - last_time).total_seconds()
      cooldown_limit = 6 * 3600  # 6 hours

      if elapsed_seconds < cooldown_limit:
        remaining = int(cooldown_limit - elapsed_seconds)
        hours = remaining // 3600
        minutes = (remaining % 3600) // 60
        await interaction.response.send_message(
            f"You are on an appeal cooldown. You must wait **{hours}h {minutes}m** before submitting another appeal.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(AppealModal(self.guild_id))


class ActionReasonModal(discord.ui.Modal):

  def __init__(self, action_type, target_user_id, guild_id):
    title = "Accept Appeal Reason" if action_type == "accept" else "Deny Appeal Reason"
    super().__init__(title=title)
    self.action_type = action_type
    self.target_user_id = target_user_id
    self.guild_id = guild_id

    self.reason_input = discord.ui.TextInput(
        label="Reason for decision",
        style=discord.TextStyle.long,
        placeholder="Type your reason here...",
        required=True,
        max_length=500,
    )
    self.add_item(self.reason_input)

  async def on_submit(self, interaction: discord.Interaction):
    senior_role_id = get_config(self.guild_id, "senior_role")
    if not senior_role_id or not any(r.id == int(senior_role_id) for r in interaction.user.roles):
      await interaction.response.send_message("Only Senior Moderators can use this.", ephemeral=True)
      return

    guild = bot.get_guild(self.guild_id)
    member = guild.get_member(self.target_user_id)
    reason_text = self.reason_input.value

    if self.action_type == "accept":
      jail_role_id = get_config(self.guild_id, "jail_role")
      if member and jail_role_id:
        jail_role = guild.get_role(int(jail_role_id))
        if jail_role:
          await member.remove_roles(jail_role)

      if member:
        try:
          await member.send(
              f"Your jail appeal in **{guild.name}** has been **ACCEPTED** by {interaction.user.mention}.\n**Reason:** {reason_text}"
          )
        except discord.Forbidden:
          pass

      embed = interaction.message.embeds[0]
      embed.color = discord.Color.green()
      embed.add_field(name="Status", value=f"Accepted by {interaction.user.mention}\n**Reason:** {reason_text}", inline=False)
      
      log_embed = discord.Embed(title="Appeal Accepted", color=discord.Color.green(), timestamp=discord.utils.utcnow())
      log_embed.add_field(name="User", value=f"{member} ({self.target_user_id})", inline=False)
      log_embed.add_field(name="Accepted By", value=str(interaction.user), inline=False)
      log_embed.add_field(name="Reason", value=reason_text, inline=False)
      await send_log(guild, log_embed)

    else:  # Deny
      if member:
        try:
          await member.send(
              f"Your jail appeal in **{guild.name}** has been **DENIED** by {interaction.user.mention}.\n**Reason:** {reason_text}"
          )
        except discord.Forbidden:
          pass

      embed = interaction.message.embeds[0]
      embed.color = discord.Color.red()
      embed.add_field(name="Status", value=f"Denied by {interaction.user.mention}\n**Reason:** {reason_text}", inline=False)

      log_embed = discord.Embed(title="Appeal Denied", color=discord.Color.red(), timestamp=discord.utils.utcnow())
      log_embed.add_field(name="User", value=f"{member} ({self.target_user_id})", inline=False)
      log_embed.add_field(name="Denied By", value=str(interaction.user), inline=False)
      log_embed.add_field(name="Reason", value=reason_text, inline=False)
      await send_log(guild, log_embed)

    view = SeniorReviewView(self.target_user_id, self.guild_id)
    for child in view.children:
      child.disabled = True

    await interaction.message.edit(embed=embed, view=view)
    status_msg = "Appeal accepted, user unjailed, and notified." if self.action_type == "accept" else "Appeal denied and user notified."
    await interaction.response.send_message(status_msg, ephemeral=True)


class SeniorReviewView(discord.ui.View):

  def __init__(self, target_user_id, guild_id):
    super().__init__(timeout=None)
    self.target_user_id = target_user_id
    self.guild_id = guild_id

  @discord.ui.button(label="Accept", style=discord.ButtonStyle.green, custom_id="accept_appeal")
  async def accept_appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
    senior_role_id = get_config(self.guild_id, "senior_role")
    if not senior_role_id or not any(r.id == int(senior_role_id) for r in interaction.user.roles):
      await interaction.response.send_message("Only Senior Moderators can use this.", ephemeral=True)
      return
    await interaction.response.send_modal(ActionReasonModal("accept", self.target_user_id, self.guild_id))

  @discord.ui.button(label="Deny", style=discord.ButtonStyle.red, custom_id="deny_appeal")
  async def deny_appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
    senior_role_id = get_config(self.guild_id, "senior_role")
    if not senior_role_id or not any(r.id == int(senior_role_id) for r in interaction.user.roles):
      await interaction.response.send_message("Only Senior Moderators can use this.", ephemeral=True)
      return
    await interaction.response.send_modal(ActionReasonModal("deny", self.target_user_id, self.guild_id))


# --- Check Helpers ---
async def check_staff(ctx):
  staff_role_id = get_config(ctx.guild.id, "staff_role")
  senior_role_id = get_config(ctx.guild.id, "senior_role")
  if not staff_role_id and not senior_role_id:
    return ctx.author.guild_permissions.manage_messages
  role_ids = [r.id for r in ctx.author.roles]
  return (
      (staff_role_id and int(staff_role_id) in role_ids)
      or (senior_role_id and int(senior_role_id) in role_ids)
      or ctx.author.guild_permissions.manage_messages
  )


# --- Configuration Commands ---
@bot.command()
@commands.has_permissions(administrator=True)
async def setappeal(ctx, channel: discord.TextChannel):
  set_config(ctx.guild.id, "appeal_channel", channel.id)
  await ctx.send(f"Appeal channel set to {channel.mention}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setsenior(ctx, role: discord.Role):
  set_config(ctx.guild.id, "senior_role", role.id)
  await ctx.send(f"Senior Mod role set to {role.name}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setstaff(ctx, role: discord.Role):
  set_config(ctx.guild.id, "staff_role", role.id)
  await ctx.send(f"Staff role set to {role.name}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setjail(ctx, role: discord.Role):
  set_config(ctx.guild.id, "jail_role", role.id)
  await ctx.send(f"Jail role set to {role.name}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setlogs(ctx, channel: discord.TextChannel):
  set_config(ctx.guild.id, "log_channel", channel.id)
  await ctx.send(f"Logs channel set to {channel.mention}")


# --- Moderation Commands ---


@bot.command()
async def jail(ctx, member: discord.Member, *, reason: str = "No reason provided"):
  if not await check_staff(ctx):
    await ctx.send("You do not have permission to use this command.")
    return

  jail_role_id = get_config(ctx.guild.id, "jail_role")
  if not jail_role_id:
    await ctx.send("Jail role is not set! Use `>setjail @Role` first.")
    return

  jail_role = ctx.guild.get_role(int(jail_role_id))
  if not jail_role:
    await ctx.send("Configured jail role no longer exists.")
    return

  proof_url = ctx.message.attachments[0].url if ctx.message.attachments else None

  data = load_data()
  g_id_str = str(ctx.guild.id)
  u_id_str = str(member.id)

  if proof_url:
    if "temp_proof" not in data:
      data["temp_proof"] = {}
    data["temp_proof"][u_id_str] = proof_url

  if g_id_str not in data["jail_info"]:
    data["jail_info"][g_id_str] = {}
  data["jail_info"][g_id_str][u_id_str] = {"reason": reason, "staff": str(ctx.author)}
  save_data(data)

  try:
    await member.add_roles(jail_role, reason=reason)
    add_stat(ctx.guild.id, ctx.author.id, "jails")
  except Exception as e:
    await ctx.send(f"Failed to apply jail role: {e}")
    return

  try:
    dm_embed = discord.Embed(
        title=f"You have been jailed in {ctx.guild.name}",
        description=f"**Reason:** {reason}",
        color=discord.Color.red(),
    )
    view = AppealButtonView(ctx.guild.id)
    await member.send(embed=dm_embed, view=view)
  except discord.Forbidden:
    pass

  # Log embed
  log_embed = discord.Embed(title="Member Jailed", color=discord.Color.red(), timestamp=discord.utils.utcnow())
  log_embed.add_field(name="User", value=f"{member} ({member.id})", inline=False)
  log_embed.add_field(name="Staff", value=str(ctx.author), inline=True)
  log_embed.add_field(name="Reason", value=reason, inline=True)
  if proof_url:
    log_embed.set_image(url=proof_url)
  await send_log(ctx.guild, log_embed)

  await ctx.send(f"Successfully jailed {member.mention}. User has been DM'd.")


@bot.command()
async def unjail(ctx, member: discord.Member):
  if not await check_staff(ctx):
    await ctx.send("You do not have permission to use this command.")
    return

  jail_role_id = get_config(ctx.guild.id, "jail_role")
  if not jail_role_id:
    await ctx.send("Jail role is not set!")
    return

  jail_role = ctx.guild.get_role(int(jail_role_id))
  if jail_role and jail_role in member.roles:
    await member.remove_roles(jail_role)
    
    log_embed = discord.Embed(title="Member Unjailed", color=discord.Color.blue(), timestamp=discord.utils.utcnow())
    log_embed.add_field(name="User", value=f"{member} ({member.id})", inline=False)
    log_embed.add_field(name="Staff", value=str(ctx.author), inline=False)
    await send_log(ctx.guild, log_embed)

    await ctx.send(f"Successfully unjailed {member.mention}.")
  else:
    await ctx.send("That member is not currently jailed.")


@bot.command()
async def warn(ctx, member: discord.Member, *, reason: str = "No reason provided"):
  if not await check_staff(ctx):
    await ctx.send("You do not have permission.")
    return

  add_stat(ctx.guild.id, ctx.author.id, "warns")
  add_warn(ctx.guild.id, member.id, reason, ctx.author)

  try:
    await member.send(f"You were warned in **{ctx.guild.name}** for: {reason}")
  except discord.Forbidden:
    pass

  log_embed = discord.Embed(title="Member Warned", color=discord.Color.yellow(), timestamp=discord.utils.utcnow())
  log_embed.add_field(name="User", value=f"{member} ({member.id})", inline=False)
  log_embed.add_field(name="Staff", value=str(ctx.author), inline=True)
  log_embed.add_field(name="Reason", value=reason, inline=True)
  await send_log(ctx.guild, log_embed)

  await ctx.send(f"Warned {member.mention} for: {reason}")


@bot.command(name="warns")
async def check_warns(ctx, member: discord.Member = None):
  if not await check_staff(ctx):
    await ctx.send("You do not have permission.")
    return

  target = member or ctx.author
  data = load_data()
  user_warns = data.get("warns", {}).get(str(ctx.guild.id), {}).get(str(target.id), [])

  embed = discord.Embed(
      title=f"Warnings for {target}",
      description=f"Total Warnings: **{len(user_warns)}**",
      color=discord.Color.yellow(),
  )

  if user_warns:
    for idx, w in enumerate(user_warns, 1):
      embed.add_field(
          name=f"Warning #{idx} (By: {w['staff']})",
          value=w["reason"],
          inline=False,
      )
  else:
    embed.add_field(name="Record", value="This user has no active warnings.", inline=False)

  await ctx.send(embed=embed)


@bot.command()
async def mute(ctx, member: discord.Member, *, reason: str = "No reason provided"):
  if not await check_staff(ctx):
    await ctx.send("You do not have permission.")
    return

  add_stat(ctx.guild.id, ctx.author.id, "mutes")
  duration = discord.utils.utcnow() + discord.timedelta(minutes=10)
  try:
    await member.timeout(duration, reason=reason)
    await member.send(f"You were muted in **{ctx.guild.name}** for: {reason}")
  except Exception as e:
    await ctx.send(f"Failed to mute: {e}")
    return

  log_embed = discord.Embed(title="Member Muted", color=discord.Color.dark_orange(), timestamp=discord.utils.utcnow())
  log_embed.add_field(name="User", value=f"{member} ({member.id})", inline=False)
  log_embed.add_field(name="Staff", value=str(ctx.author), inline=True)
  log_embed.add_field(name="Reason", value=reason, inline=True)
  await send_log(ctx.guild, log_embed)

  await ctx.send(f"Muted {member.mention} for: {reason}")


@bot.command(name="ms")
async def modstats(ctx, member: discord.Member = None):
  target = member or ctx.author
  data = load_data()
  stats = data.get("modstats", {}).get(str(ctx.guild.id), {}).get(str(target.id), {"jails": 0, "mutes": 0, "warns": 0})

  embed = discord.Embed(title=f"Moderation Statistics for {target}", color=discord.Color.blue())
  embed.add_field(name="Jails Executed", value=stats.get("jails", 0), inline=True)
  embed.add_field(name="Mutes Executed", value=stats.get("mutes", 0), inline=True)
  embed.add_field(name="Warns Issued", value=stats.get("warns", 0), inline=True)
  await ctx.send(embed=embed)


# --- Execution Routine ---
if __name__ == "__main__":
  import threading

  t = threading.Thread(target=run_flask)
  t.daemon = True
  t.start()

  TOKEN = os.environ.get("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")
  bot.run(TOKEN)
