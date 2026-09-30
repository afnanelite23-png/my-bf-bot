import asyncio
import json
import os
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

# Database file to save configs and mod stats persistently
CONFIG_FILE = "config.json"


def load_data():
  if not os.path.exists(CONFIG_FILE):
    return {
        "guilds": {},
        "modstats": {},  # Format: {guild_id: {staff_id: {"jails": 0, "mutes": 0, ...}}}
    }
  with open(CONFIG_FILE, "r") as f:
    return json.load(f)


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

    guild = bot.get_guild(self.guild_id)
    channel = guild.get_channel(int(appeal_channel_id))

    # Retrieve cached proof if available
    proof_url = data.get("temp_proof", {}).get(str(interaction.user.id))

    embed = discord.Embed(
        title="New Jail Appeal Submitted",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="User", value=f"{interaction.user} ({interaction.user.id})", inline=False)
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
    await interaction.response.send_modal(AppealModal(self.guild_id))


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

    guild = bot.get_guild(self.guild_id)
    member = guild.get_member(self.target_user_id)
    jail_role_id = get_config(self.guild_id, "jail_role")

    if member and jail_role_id:
      jail_role = guild.get_role(int(jail_role_id))
      if jail_role:
        await member.remove_roles(jail_role)

    for child in self.children:
      child.disabled = True
    embed = interaction.message.embeds[0]
    embed.color = discord.Color.green()
    embed.add_field(name="Status", value=f"Accepted by {interaction.user.mention}", inline=False)
    await interaction.message.edit(embed=embed, view=self)
    await interaction.response.send_message("Appeal accepted and user unjailed.", ephemeral=True)

  @discord.ui.button(label="Deny", style=discord.ButtonStyle.red, custom_id="deny_appeal")
  async def deny_appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
    senior_role_id = get_config(self.guild_id, "senior_role")
    if not senior_role_id or not any(r.id == int(senior_role_id) for r in interaction.user.roles):
      await interaction.response.send_message("Only Senior Moderators can use this.", ephemeral=True)
      return

    for child in self.children:
      child.disabled = True
    embed = interaction.message.embeds[0]
    embed.color = discord.Color.red()
    embed.add_field(name="Status", value=f"Denied by {interaction.user.mention}", inline=False)
    await interaction.message.edit(embed=embed, view=self)
    await interaction.response.send_message("Appeal denied.", ephemeral=True)


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

  # Grab attachment proof if provided with the message
  proof_url = ctx.message.attachments[0].url if ctx.message.attachments else None

  # Save temporary proof mapping for the appeal embed
  if proof_url:
    data = load_data()
    if "temp_proof" not in data:
      data["temp_proof"] = {}
    data["temp_proof"][str(member.id)] = proof_url
    save_data(data)

  try:
    await member.add_roles(jail_role, reason=reason)
    add_stat(ctx.guild.id, ctx.author.id, "jails")
  except Exception as e:
    await ctx.send(f"Failed to apply jail role: {e}")
    return

  # DM the user
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
    await ctx.send(f"Successfully unjailed {member.mention}.")
  else:
    await ctx.send("That member is not currently jailed.")


@bot.command()
async def warn(ctx, member: discord.Member, *, reason: str = "No reason provided"):
  if not await check_staff(ctx):
    await ctx.send("You do not have permission.")
    return

  add_stat(ctx.guild.id, ctx.author.id, "warns")
  try:
    await member.send(f"You were warned in **{ctx.guild.name}** for: {reason}")
  except discord.Forbidden:
    pass
  await ctx.send(f"Warned {member.mention} for: {reason}")


@bot.command()
async def mute(ctx, member: discord.Member, *, reason: str = "No reason provided"):
  if not await check_staff(ctx):
    await ctx.send("You do not have permission.")
    return

  add_stat(ctx.guild.id, ctx.author.id, "mutes")
  # Simple timeout implementation (default 10 mins if no time string parsed)
  duration = discord.utils.utcnow() + discord.timedelta(minutes=10)
  try:
    await member.timeout(duration, reason=reason)
    await member.send(f"You were muted in **{ctx.guild.name}** for: {reason}")
  except Exception as e:
    await ctx.send(f"Failed to mute: {e}")
    return
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
  # Run Flask server in background thread
  import threading

  t = threading.Thread(target=run_flask)
  t.daemon = True
  t.start()

  # Run Discord bot (Replace with your bot token or environment variable)
  TOKEN = os.environ.get("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")
  bot.run(TOKEN)
