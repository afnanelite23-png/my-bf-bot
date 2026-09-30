import asyncio
from datetime import datetime, timezone
import json
import os
import random
import discord
from discord.ext import commands, tasks
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

# Changed prefix from ">" to "!"
bot = commands.Bot(command_prefix="!", intents=intents)

CONFIG_FILE = "config.json"


def load_data():
  if not os.path.exists(CONFIG_FILE):
    return {
        "guilds": {},
        "modstats": {},
        "warns": {},
        "appeal_cooldowns": {},
        "jail_info": {},
        "giveaways": {},
        "levels": {},  # Added for leveling system
    }
  with open(CONFIG_FILE, "r") as f:
    data = json.load(f)
    if "warns" not in data:
      data["warns"] = {}
    if "appeal_cooldowns" not in data:
      data["appeal_cooldowns"] = {}
    if "jail_info" not in data:
      data["jail_info"] = {}
    if "giveaways" not in data:
      data["giveaways"] = {}
    if "modstats" not in data:
      data["modstats"] = {}
    if "levels" not in data:
      data["levels"] = {}
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


# --- Ticket Views & Claim System ---


class TicketControlView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.primary, custom_id="claim_ticket_btn")
  async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
    guild = interaction.guild
    staff_role_id = get_config(guild.id, "ticket_staff_role")
    senior_role_id = get_config(guild.id, "senior_role")
    staff_role_id_gen = get_config(guild.id, "staff_role")

    is_staff = False
    if interaction.user.guild_permissions.manage_messages:
      is_staff = True
    else:
      role_ids = [r.id for r in interaction.user.roles]
      if staff_role_id and int(staff_role_id) in role_ids:
        is_staff = True
      if senior_role_id and int(senior_role_id) in role_ids:
        is_staff = True
      if staff_role_id_gen and int(staff_role_id_gen) in role_ids:
        is_staff = True

    if not is_staff:
      await interaction.response.send_message("You do not have permission to claim tickets.", ephemeral=True)
      return

    channel = interaction.channel

    if staff_role_id:
      staff_role = guild.get_role(int(staff_role_id))
      if staff_role:
        await channel.set_permissions(staff_role, view_channel=True, send_messages=False, read_message_history=True)

    if staff_role_id_gen and staff_role_id_gen != staff_role_id:
      gen_role = guild.get_role(int(staff_role_id_gen))
      if gen_role:
        await channel.set_permissions(gen_role, view_channel=True, send_messages=False, read_message_history=True)

    await channel.set_permissions(interaction.user, view_channel=True, send_messages=True, read_message_history=True)

    button.disabled = True
    button.label = f"Claimed by {interaction.user.name}"
    button.style = discord.ButtonStyle.secondary

    await interaction.message.edit(view=self)
    await interaction.response.send_message(
        f"🔒 Ticket claimed by {interaction.user.mention}. Other staff can view this channel history, but **only you** can send messages."
    )

  @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket_btn")
  async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.send_message("Closing ticket in 5 seconds...")
    await asyncio.sleep(5)
    try:
      await interaction.channel.delete()
    except Exception:
      pass


class TicketSelect(discord.ui.Select):

  def __init__(self):
    options = [
        discord.SelectOption(label="Support", description="General assistance and questions", emoji="🛠️"),
        discord.SelectOption(label="Report", description="Report a user or staff member", emoji="🚨"),
        discord.SelectOption(label="Giveaway Claim", description="Claim a won giveaway prize", emoji="🎁"),
        discord.SelectOption(label="Giveaway Host", description="Coordinate hosting a giveaway", emoji="🎉"),
        discord.SelectOption(label="Ads/Partnerships", description="Inquiries regarding advertisements or partnerships", emoji="🤝"),
    ]
    super().__init__(placeholder="Select a ticket category...", min_values=1, max_values=1, options=options, custom_id="ticket_dropdown")

  async def callback(self, interaction: discord.Interaction):
    guild = interaction.guild
    category_id = get_config(guild.id, "ticket_category")
    staff_role_id = get_config(guild.id, "ticket_staff_role")

    category = guild.get_channel(int(category_id)) if category_id else None
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }

    if staff_role_id:
      staff_role = guild.get_role(int(staff_role_id))
      if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    ticket_channel = await guild.create_text_channel(
        name=f"ticket-{interaction.user.name}-{self.values[0].lower().replace('/', '-')}",
        category=category,
        overwrites=overwrites,
    )

    embed = discord.Embed(
        title=f"Ticket: {self.values[0]}",
        description=f"Welcome {interaction.user.mention}!\nSupport will be with you shortly.\nPlease describe your issue or request in detail.",
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow(),
    )

    view = TicketControlView()
    staff_ping = f"<@&{staff_role_id}>" if staff_role_id else ""
    
    await ticket_channel.send(content=f"{interaction.user.mention} {staff_ping}", embed=embed, view=view)
    await interaction.response.send_message(f"Your ticket has been created: {ticket_channel.mention}", ephemeral=True)


class TicketPanelView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)
    self.add_item(TicketSelect())


# --- Leveling Role Milestone Config View & Modal ---

class LevelRoleSelect(discord.ui.RoleSelect):
  def __init__(self, level: int):
    super().__init__(placeholder=f"Select role(s) to give at Level {level}...", min_values=1, max_values=5)
    self.level = level

  async def callback(self, interaction: discord.Interaction):
    guild_id = interaction.guild.id
    data = load_data()
    g_id = str(guild_id)
    if g_id not in data["levels"]:
      data["levels"][g_id] = {"roles": {}, "channel_id": None}

    selected_role_ids = [r.id for r in self.values]
    data["levels"][g_id]["roles"][str(self.level)] = selected_role_ids
    save_data(data)

    role_mentions = ", ".join([r.mention for r in self.values])
    await interaction.response.send_message(f"✅ Successfully linked role(s) {role_mentions} to **Level {self.level}**!", ephemeral=True)


class LevelRoleView(discord.ui.View):
  def __init__(self, level: int):
    super().__init__(timeout=180)
    self.add_item(LevelRoleSelect(level))


# --- Giveaway Views & Logic ---

async def end_giveaway_task_logic(bot, guild_id, message_id):
  data = load_data()
  g_id_str = str(guild_id)
  m_id_str = str(message_id)

  if g_id_str not in data["giveaways"] or m_id_str not in data["giveaways"][g_id_str]:
    return

  g_data = data["giveaways"][g_id_str][m_id_str]
  if g_data.get("ended", False):
    return

  g_data["ended"] = True
  save_data(data)

  guild = bot.get_guild(guild_id)
  if not guild:
    return
  channel = guild.get_channel(int(g_data["channel_id"]))
  if not channel:
    return

  try:
    message = await channel.fetch_message(message_id)
  except Exception:
    return

  participants = g_data.get("participants", [])
  winners_count = int(g_data.get("winners_count", 1))
  prize = g_data.get("prize", "Unknown Prize")

  if len(participants) > 0:
    actual_winners_count = min(winners_count, len(participants))
    winners = random.sample(participants, actual_winners_count)
    winners_mention = ", ".join([f"<@{w}>" for w in winners])
    
    embed = message.embeds[0]
    embed.color = discord.Color.dark_embed()
    embed.title = "🎉 GIVEAWAY ENDED 🎉"
    embed.add_field(name="Winners", value=winners_mention, inline=False)
    
    view = GiveawayView(guild_id, message_id, ended=True)
    await message.edit(embed=embed, view=view)
    await channel.send(f"🎊 Congratulations {winners_mention}! You won the **{prize}**!")
  else:
    embed = message.embeds[0]
    embed.color = discord.Color.dark_embed()
    embed.title = "🎉 GIVEAWAY ENDED (No Valid Entries) 🎉"
    embed.add_field(name="Winners", value="No participants entered.", inline=False)
    
    view = GiveawayView(guild_id, message_id, ended=True)
    await message.edit(embed=embed, view=view)
    await channel.send(f"The giveaway for **{prize}** ended with no participants.")


class GiveawayView(discord.ui.View):

  def __init__(self, guild_id, message_id, ended=False):
    super().__init__(timeout=None)
    self.guild_id = guild_id
    self.message_id = message_id
    if ended:
      self.join_btn.disabled = True
      self.end_btn.disabled = True

  @discord.ui.button(label="🎉 Enter Giveaway", style=discord.ButtonStyle.green, custom_id="enter_giveaway_btn")
  async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
    data = load_data()
    g_id_str = str(self.guild_id)
    m_id_str = str(self.message_id)

    if g_id_str not in data["giveaways"] or m_id_str not in data["giveaways"][g_id_str]:
      await interaction.response.send_message("This giveaway no longer exists in records.", ephemeral=True)
      return

    g_data = data["giveaways"][g_id_str][m_id_str]
    if g_data.get("ended", False):
      await interaction.response.send_message("This giveaway has already ended!", ephemeral=True)
      return

    if "participants" not in g_data:
      g_data["participants"] = []

    user_id = interaction.user.id
    if user_id in g_data["participants"]:
      g_data["participants"].remove(user_id)
      save_data(data)
      await interaction.response.send_message("❌ You have left the giveaway.", ephemeral=True)
    else:
      g_data["participants"].append(user_id)
      save_data(data)
      await interaction.response.send_message("✅ You have successfully entered the giveaway! Good luck!", ephemeral=True)

  @discord.ui.button(label="End Early", style=discord.ButtonStyle.red, custom_id="end_giveaway_early_btn")
  async def end_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not interaction.user.guild_permissions.administrator:
      await interaction.response.send_message("Only Administrators can end giveaways early.", ephemeral=True)
      return

    await interaction.response.send_message("Ending giveaway...", ephemeral=True)
    await end_giveaway_task_logic(interaction.client, self.guild_id, self.message_id)


@tasks.loop(seconds=15)
async def check_giveaways():
  data = load_data()
  now = datetime.now(timezone.utc)
  
  if "giveaways" not in data:
    return

  updated = False
  for g_id_str, messages in list(data["giveaways"].items()):
    for m_id_str, g_data in list(messages.items()):
      if g_data.get("ended", False):
        continue
      
      end_time = datetime.fromisoformat(g_data["end_time"])
      if now >= end_time:
        guild_id = int(g_id_str)
        message_id = int(m_id_str)
        await end_giveaway_task_logic(bot, guild_id, message_id)
        updated = True

  if updated:
    data = load_data()


# --- Appeal Views & Modals ---

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
      cooldown_limit = 6 * 3600

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

    else:
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


async def check_senior(ctx):
  senior_role_id = get_config(ctx.guild.id, "senior_role")
  if not senior_role_id:
    return ctx.author.guild_permissions.manage_messages
  role_ids = [r.id for r in ctx.author.roles]
  return (senior_role_id and int(senior_role_id) in role_ids) or ctx.author.guild_permissions.manage_messages


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


# --- Leveling System Config Commands ---
@bot.command()
@commands.has_permissions(administrator=True)
async def setlevelchannel(ctx, channel: discord.TextChannel):
  data = load_data()
  g_id = str(ctx.guild.id)
  if g_id not in data["levels"]:
    data["levels"][g_id] = {"roles": {}, "channel_id": None}
  data["levels"][g_id]["channel_id"] = channel.id
  save_data(data)
  await ctx.send(f"✅ Level-up notification channel set to {channel.mention}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setlevelrole(ctx, level: int):
  view = LevelRoleView(level)
  await ctx.send(f"Select role(s) to give automatically when a member reaches **Level {level}**:", view=view)


# --- Ticket System Setup Commands ---
@bot.command()
@commands.has_permissions(administrator=True)
async def setupticket(ctx, channel: discord.TextChannel):
  embed = discord.Embed(
      title="Support Tickets",
      description="Select a category from the dropdown menu below to open a private ticket with our staff team.",
      color=discord.Color.blurple(),
  )
  view = TicketPanelView()
  await channel.send(embed=embed, view=view)
  await ctx.send(f"Ticket panel successfully sent to {channel.mention}!")


@bot.command()
@commands.has_permissions(administrator=True)
async def setcategory(ctx, category: discord.CategoryChannel):
  set_config(ctx.guild.id, "ticket_category", category.id)
  await ctx.send(f"Ticket category set to **{category.name}**")


@bot.command()
@commands.has_permissions(administrator=True)
async def setstaffrole(ctx, role: discord.Role):
  set_config(ctx.guild.id, "ticket_staff_role", role.id)
  await ctx.send(f"Ticket staff role set to {role.mention}")


# --- Giveaway Admin Commands ---
@bot.command(name="gstart")
@commands.has_permissions(administrator=True)
async def gcreate(ctx, channel: discord.TextChannel, time_str: str, winners_count: int, *, prize: str):
  seconds = 0
  if time_str.endswith("s"):
    seconds = int(time_str[:-1])
  elif time_str.endswith("m"):
    seconds = int(time_str[:-1]) * 60
  elif time_str.endswith("h"):
    seconds = int(time_str[:-1]) * 3600
  elif time_str.endswith("d"):
    seconds = int(time_str[:-1]) * 86400
  else:
    await ctx.send("❌ Invalid time format! Use `s`, `m`, `h`, or `d` (e.g., `10m`, `2h`).", delete_after=10)
    return

  end_time = datetime.now(timezone.utc) + discord.utils.timedelta(seconds=seconds)
  timestamp_unix = int(end_time.timestamp())

  embed = discord.Embed(
      title="🎉 **GIVEAWAY** 🎉",
      description=f"Prize: **{prize}**\nHosted by: {ctx.author.mention}\nWinners: **{winners_count}**\nEnds:  ()",
      color=discord.Color.gold(),
      timestamp=discord.utils.utcnow()
  )

  view = GiveawayView(ctx.guild.id, 0)
  g_msg = await channel.send(embed=embed, view=view)

  data = load_data()
  g_id_str = str(ctx.guild.id)
  m_id_str = str(g_msg.id)

  if g_id_str not in data["giveaways"]:
    data["giveaways"][g_id_str] = {}

  data["giveaways"][g_id_str][m_id_str] = {
      "prize": prize,
      "winners_count": winners_count,
      "end_time": end_time.isoformat(),
      "channel_id": channel.id,
      "participants": [],
      "ended": False
  }
  save_data(data)

  view.message_id = g_msg.id
  await g_msg.edit(view=view)
  await ctx.send(f"✅ Giveaway successfully started in {channel.mention}!", delete_after=5)


# --- Moderation & Purge Commands ---

@bot.command()
async def purge(ctx, amount: int):
  if not await check_senior(ctx):
    await ctx.send("You do not have permission to use this command. Only Senior Moderators can purge messages.", delete_after=5)
    return

  if amount < 1 or amount > 100:
    await ctx.send("Please specify a number between **1 and 100**.", delete_after=5)
    return

  try:
    await ctx.message.delete()
    deleted = await ctx.channel.purge(limit=amount)
    
    await ctx.send(f"Successfully deleted **{len(deleted)}** messages.", delete_after=5)

    log_embed = discord.Embed(title="Messages Purged", color=discord.Color.purple(), timestamp=discord.utils.utcnow())
    log_embed.add_field(name="Senior Mod", value=str(ctx.author), inline=False)
    log_embed.add_field(name="Channel", value=ctx.channel.mention, inline=True)
    log_embed.add_field(name="Count", value=str(len(deleted)), inline=True)
    await send_log(ctx.guild, log_embed)

  except Exception as e:
    await ctx.send(f"Failed to purge messages: {e}", delete_after=5)


@bot.command()
async def jail(ctx, member: discord.Member, *, reason: str = "No reason provided"):
  if not await check_staff(ctx):
    await ctx.send("You do not have permission to use this command.")
    return

  jail_role_id = get_config(ctx.guild.id, "jail_role")
  if not jail_role_id:
    await ctx.send("Jail role is not set! Use `!setjail @Role` first.")
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


# --- FIXED MUTE COMMAND ---
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


# --- FIXED MODSTATS COMMAND ---
@bot.command(name="ms")
async def modstats(ctx, member: discord.Member = None):
  target = member or ctx.author
  data = load_data()
  
  guild_stats = data.get("modstats", {}).get(str(ctx.guild.id), {})
  stats = guild_stats.get(str(target.id), {"jails": 0, "mutes": 0, "warns": 0})

  embed = discord.Embed(title=f"Moderation Statistics for {target}", color=discord.Color.blue())
  embed.add_field(name="Jails Executed", value=stats.get("jails", 0), inline=True)
  embed.add_field(name="Mutes Executed", value=stats.get("mutes", 0), inline=True)
  embed.add_field(name="Warns Issued", value=stats.get("warns", 0), inline=True)
  await ctx.send(embed=embed)


# --- Leveling Event Handler ---
@bot.event
async def on_message(message):
  if message.author.bot or not message.guild:
    await bot.process_commands(message)
    return

  guild_id = str(message.guild.id)
  user_id = str(message.author.id)

  data = load_data()
  if "user_xp" not in data:
    data["user_xp"] = {}
  if guild_id not in data["user_xp"]:
    data["user_xp"][guild_id] = {}

  user_data = data["user_xp"][guild_id].get(user_id, {"xp": 0, "level": 0})
  
  # Add random XP per message
  user_data["xp"] += random.randint(15, 25)
  
  # Calculate required XP for next level (Level * 100 + 100)
  next_level_xp = (user_data["level"] + 1) * 150
  
  if user_data["xp"] >= next_level_xp:
    user_data["level"] += 1
    new_level = user_data["level"]
    
    # Check if level rewards exist
    lvl_config = data.get("levels", {}).get(guild_id, {})
    notif_channel_id = lvl_config.get("channel_id")
    milestone_roles = lvl_config.get("roles", {}).get(str(new_level), [])

    if milestone_roles:
      member = message.guild.get_member(message.author.id)
      if member:
        for r_id in milestone_roles:
          role = message.guild.get_role(int(r_id))
          if role:
            try:
              await member.add_roles(role)
            except Exception:
              pass

    if notif_channel_id:
      channel = message.guild.get_channel(int(notif_channel_id))
      if channel:
        await channel.send(f"🎉 Congratulations {message.author.mention}! You leveled up to **Level {new_level}**!")

  data["user_xp"][guild_id][user_id] = user_data
  save_data(data)

  await bot.process_commands(message)


@bot.event
async def on_ready():
  if not check_giveaways.is_running():
    check_giveaways.start()
  print(f"Logged in as {bot.user} (ID: {bot.user.id})")


# --- Execution Routine ---
if __name__ == "__main__":
  import threading

  t = threading.Thread(target=run_flask)
  t.daemon = True
  t.start()

  TOKEN = os.environ.get("DIS_TOKEN")
  if not TOKEN:
    raise ValueError("No DIS_TOKEN environment variable found. Please set your bot token in Render's Environment settings.")
  bot.run(TOKEN)
