import os, re, json, time, random, sqlite3, logging, asyncio, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("creator-bot")
TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN") or "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
DB_FILE = os.getenv("DB_FILE") or os.getenv("DB_PATH") or "data/bot.sqlite3"
os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row

def init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS settings(guild_id INTEGER,key TEXT,value TEXT,PRIMARY KEY(guild_id,key));
    CREATE TABLE IF NOT EXISTS rules(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,title TEXT,body TEXT);
    CREATE TABLE IF NOT EXISTS role_panels(guild_id INTEGER,channel_id INTEGER,message_id INTEGER,role_ids TEXT,PRIMARY KEY(guild_id,message_id));
    CREATE TABLE IF NOT EXISTS voice_rooms(guild_id INTEGER,voice_id INTEGER PRIMARY KEY,text_id INTEGER,owner_id INTEGER);
    CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,channel_id INTEGER UNIQUE,user_id INTEGER,status TEXT,created_at INTEGER);
    CREATE TABLE IF NOT EXISTS warnings(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,user_id INTEGER,moderator_id INTEGER,reason TEXT,created_at INTEGER);
    CREATE TABLE IF NOT EXISTS giveaways(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,channel_id INTEGER,message_id INTEGER,prize TEXT,winners INTEGER,end_at INTEGER,ended INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS giveaway_entries(giveaway_id INTEGER,user_id INTEGER,PRIMARY KEY(giveaway_id,user_id));
    CREATE TABLE IF NOT EXISTS youtube_feeds(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,channel_id INTEGER,feed_url TEXT,last_video_id TEXT);
    CREATE TABLE IF NOT EXISTS twitch_channels(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,channel_id INTEGER,username TEXT,last_stream_id TEXT);
    CREATE TABLE IF NOT EXISTS applications(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,channel_id INTEGER,message_id INTEGER,user_id INTEGER,position TEXT,about TEXT,experience TEXT,status TEXT DEFAULT 'pending');
    CREATE TABLE IF NOT EXISTS suggestions(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,channel_id INTEGER,message_id INTEGER,user_id INTEGER,body TEXT);
    CREATE TABLE IF NOT EXISTS suggestion_votes(suggestion_id INTEGER,user_id INTEGER,vote INTEGER,PRIMARY KEY(suggestion_id,user_id));
    CREATE TABLE IF NOT EXISTS levels(guild_id INTEGER,user_id INTEGER,xp INTEGER DEFAULT 0,level INTEGER DEFAULT 0,coins INTEGER DEFAULT 0,messages INTEGER DEFAULT 0,last_xp INTEGER DEFAULT 0,PRIMARY KEY(guild_id,user_id));
    CREATE TABLE IF NOT EXISTS shop_items(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,role_id INTEGER,cost INTEGER);
    CREATE TABLE IF NOT EXISTS daily_claims(guild_id INTEGER,user_id INTEGER,claimed_at INTEGER,PRIMARY KEY(guild_id,user_id));
    CREATE TABLE IF NOT EXISTS role_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER UNIQUE,
        user_id INTEGER NOT NULL,
        role_id INTEGER NOT NULL,
        platform TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'awaiting_proof',
        created_at INTEGER NOT NULL,
        review_message_id INTEGER
    );
    """)
    db.commit()

def run(sql, params=()):
    cur = db.execute(sql, params); db.commit(); return cur

def one(sql, params=()): return db.execute(sql, params).fetchone()
def many(sql, params=()): return db.execute(sql, params).fetchall()
def setting(gid, key, default=None):
    row = one("SELECT value FROM settings WHERE guild_id=? AND key=?", (gid, key))
    return row["value"] if row else default
def set_setting(gid, key, value):
    run("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value",
        (gid, key, str(value) if value is not None else ""))
def num(v, default=0):
    try: return int(v)
    except (TypeError, ValueError): return default
def ids(v):
    return list(dict.fromkeys(int(x) for x in re.split(r"[,;\\s]+", v.strip()) if x.isdigit()))
def embed(title, description="", color=discord.Color.blurple()):
    e = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    e.set_footer(text="Сообщество YouTube • Twitch")
    return e
async def say(inter, content=None, *, emb=None, view=None):
    kw = {"ephemeral": True}
    if content is not None: kw["content"] = content
    if emb is not None: kw["embed"] = emb
    if view is not None: kw["view"] = view
    if inter.response.is_done(): await inter.followup.send(**kw)
    else: await inter.response.send_message(**kw)
async def admin(inter):
    if inter.guild is None:
        await say(inter, "Команда доступна только на сервере."); return False
    p = inter.user.guild_permissions
    if not (p.administrator or p.manage_guild):
        await say(inter, "⛔ Панель доступна только администрации."); return False
    return True
async def log_to(guild, text):
    if not guild: return
    ch = guild.get_channel(num(setting(guild.id, "logs_channel_id")))
    if ch:
        try: await ch.send(embed=embed("📜 Журнал действий", text, discord.Color.dark_grey()))
        except discord.HTTPException: pass
def dt(value):
    d = datetime.fromisoformat(value.strip())
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

# ---------- Role panel ----------

class RoleSetupModal(discord.ui.Modal, title="Настройка панели ролей"):
    channel_id = discord.ui.TextInput(label="ID канала для панели")
    role_ids = discord.ui.TextInput(label="ID ролей через запятую", style=discord.TextStyle.paragraph, max_length=1500)
    async def on_submit(self, i):
        ch = i.guild.get_channel(num(self.channel_id.value))
        role_list = [r for r in ids(self.role_ids.value)[:25] if i.guild.get_role(r)]
        if not isinstance(ch, discord.TextChannel) or not role_list:
            return await say(i, "Проверь ID канала и ролей. Нужно минимум одну роль.")
        set_setting(i.guild.id, "role_channel_id", ch.id)
        set_setting(i.guild.id, "role_ids", json.dumps(role_list))
        await say(i, f"✅ Сохранено. Ролей: {len(role_list)}.")

class RoleSelect(discord.ui.Select):
    def __init__(self, gid, role_ids, guild):
        opts = [discord.SelectOption(label=guild.get_role(r).name[:100], value=str(r))
                for r in role_ids if guild.get_role(r) and not guild.get_role(r).is_default()]
        super().__init__(placeholder="Выбери роли сообщества…", min_values=0, max_values=max(1,len(opts)),
                         options=opts, custom_id=f"selfroles:{gid}")
        self.role_ids = [int(o.value) for o in opts]
    async def callback(self, i):
        selected = {int(v) for v in self.values}; added=[]; removed=[]; failed=[]
        for rid in self.role_ids:
            role = i.guild.get_role(rid)
            if not role: continue
            try:
                if rid in selected and role not in i.user.roles:
                    await i.user.add_roles(role, reason="Самостоятельный выбор роли"); added.append(role.name)
                elif rid not in selected and role in i.user.roles:
                    await i.user.remove_roles(role, reason="Изменение выбранных ролей"); removed.append(role.name)
            except discord.Forbidden: failed.append(role.name)
        msg = []
        if added: msg.append("Выданы: " + ", ".join(added))
        if removed: msg.append("Сняты: " + ", ".join(removed))
        if failed: msg.append("Не удалось изменить: " + ", ".join(failed) + ". Проверь иерархию ролей бота.")
        await say(i, "\\n".join(msg) if msg else "Роли не изменены.")
class RoleSelectView(discord.ui.View):
    def __init__(self, gid, role_ids, guild):
        super().__init__(timeout=None); self.add_item(RoleSelect(gid, role_ids, guild))
class RoleAdminView(discord.ui.View):
    def __init__(self): super().__init__(timeout=180)
    @discord.ui.button(label="Настроить роли и канал", emoji="⚙️", style=discord.ButtonStyle.primary)
    async def setup(self,i,b): await i.response.send_modal(RoleSetupModal())
    @discord.ui.button(label="Опубликовать панель", emoji="📢", style=discord.ButtonStyle.success)
    async def publish(self,i,b):
        ch=i.guild.get_channel(num(setting(i.guild.id,"role_channel_id")))
        try: role_list=json.loads(setting(i.guild.id,"role_ids","[]"))
        except ValueError: role_list=[]
        if not isinstance(ch,discord.TextChannel) or not role_list: return await say(i,"Сначала настрой роли и канал.")
        view=RoleSelectView(i.guild.id,role_list,i.guild)
        msg=await ch.send(embed=embed("🎭 ВЫБОР РОЛЕЙ","Выбери роли в меню. Повторный выбор позволяет снять роль."),view=view)
        run("INSERT OR REPLACE INTO role_panels VALUES(?,?,?,?)",(i.guild.id,ch.id,msg.id,json.dumps(role_list)))
        bot.add_view(view,message_id=msg.id)
        await say(i,f"✅ Панель опубликована в {ch.mention}.")

# ---------- Welcome ----------

class WelcomeSetupModal(discord.ui.Modal, title="Настройка приветствия"):
    channel_id=discord.ui.TextInput(label="ID канала приветствия")
    rules_id=discord.ui.TextInput(label="ID канала правил",required=False)
    roles_id=discord.ui.TextInput(label="ID канала выбора ролей",required=False)
    newbie_role=discord.ui.TextInput(label="ID роли новичка",required=False)
    message=discord.ui.TextInput(label="Текст ({user}, {server}, {count})",style=discord.TextStyle.paragraph,required=False,
        default="Привет, {user}! Добро пожаловать на **{server}** ❤️")
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(self.channel_id.value))
        if not isinstance(ch,discord.TextChannel): return await say(i,"Канал приветствия не найден.")
        for k,v in [("welcome_channel_id",ch.id),("welcome_rules_id",num(self.rules_id.value)),
                    ("welcome_roles_id",num(self.roles_id.value)),("welcome_role_id",num(self.newbie_role.value))]:
            set_setting(i.guild.id,k,v)
        if self.message.value.strip(): set_setting(i.guild.id,"welcome_text",self.message.value.strip())
        await say(i,f"✅ Приветствие настроено: {ch.mention}")
class WelcomeLinks(discord.ui.View):
    def __init__(self,gid,rules,roles):
        super().__init__(timeout=None)
        if rules: self.add_item(discord.ui.Button(label="📜 Правила",style=discord.ButtonStyle.link,url=f"https://discord.com/channels/{gid}/{rules}"))
        if roles: self.add_item(discord.ui.Button(label="🎭 Выбрать роли",style=discord.ButtonStyle.link,url=f"https://discord.com/channels/{gid}/{roles}"))
class WelcomeView(discord.ui.View):
    def __init__(self): super().__init__(timeout=180)
    @discord.ui.button(label="Настроить",emoji="⚙️",style=discord.ButtonStyle.primary)
    async def setup(self,i,b): await i.response.send_modal(WelcomeSetupModal())
    @discord.ui.button(label="Включить / выключить",emoji="🔔",style=discord.ButtonStyle.secondary)
    async def toggle(self,i,b):
        old=setting(i.guild.id,"welcome_enabled","1")=="1"; set_setting(i.guild.id,"welcome_enabled","0" if old else "1")
        await say(i,"🔕 Приветствия выключены." if old else "🔔 Приветствия включены.")
    @discord.ui.button(label="Тест",emoji="🧪",style=discord.ButtonStyle.success)
    async def test(self,i,b):
        ch=i.guild.get_channel(num(setting(i.guild.id,"welcome_channel_id")))
        if not isinstance(ch,discord.TextChannel): return await say(i,"Сначала настрой канал приветствия.")
        await send_welcome(ch,i.user,True); await say(i,f"🧪 Тест отправлен в {ch.mention}.")
async def send_welcome(ch,member,test=False):
    g=ch.guild; template=setting(g.id,"welcome_text","Привет, {user}! Добро пожаловать на **{server}** ❤️")
    text=template.replace("{user}",member.mention).replace("{server}",g.name).replace("{count}",str(g.member_count or 0))
    e=embed("🎉 ДОБРО ПОЖАЛОВАТЬ!",text,discord.Color.from_rgb(145,70,255))
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="👥 Участник",value=member.mention,inline=True)
    e.add_field(name="📊 Всего участников",value=str(g.member_count or "—"),inline=True)
    await ch.send(content=f"✨ Рады видеть тебя, {member.mention}!" if not test else "🧪 Тестовое приветствие",
                  embed=e,view=WelcomeLinks(g.id,num(setting(g.id,"welcome_rules_id")),num(setting(g.id,"welcome_roles_id"))))

# ---------- Rules ----------

class RuleAdd(discord.ui.Modal,title="Добавить правило"):
    title_input=discord.ui.TextInput(label="Название правила",max_length=100)
    body=discord.ui.TextInput(label="Текст правила",style=discord.TextStyle.paragraph,max_length=1800)
    async def on_submit(self,i):
        run("INSERT INTO rules(guild_id,title,body) VALUES(?,?,?)",(i.guild.id,self.title_input.value,self.body.value))
        await say(i,"✅ Правило добавлено. Нажми «Опубликовать», чтобы обновить канал.")
class RuleEdit(discord.ui.Modal,title="Изменить правило"):
    rule_id=discord.ui.TextInput(label="Номер правила")
    title_input=discord.ui.TextInput(label="Новое название")
    body=discord.ui.TextInput(label="Новый текст",style=discord.TextStyle.paragraph,max_length=1800)
    async def on_submit(self,i):
        rid=num(self.rule_id.value,-1)
        if not one("SELECT id FROM rules WHERE id=? AND guild_id=?",(rid,i.guild.id)): return await say(i,"Правило не найдено.")
        run("UPDATE rules SET title=?,body=? WHERE id=? AND guild_id=?",(self.title_input.value,self.body.value,rid,i.guild.id))
        await say(i,"✅ Правило обновлено.")
class RuleDelete(discord.ui.Modal,title="Удалить правило"):
    rule_id=discord.ui.TextInput(label="Номер правила")
    async def on_submit(self,i):
        cur=run("DELETE FROM rules WHERE id=? AND guild_id=?",(num(self.rule_id.value),i.guild.id))
        await say(i,"🗑️ Правило удалено." if cur.rowcount else "Правило не найдено.")
class RuleChannel(discord.ui.Modal,title="Канал публикации правил"):
    channel_id=discord.ui.TextInput(label="ID текстового канала")
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(self.channel_id.value))
        if not isinstance(ch,discord.TextChannel): return await say(i,"Канал не найден.")
        set_setting(i.guild.id,"rules_channel_id",ch.id); await say(i,f"✅ Канал правил: {ch.mention}")
async def publish_rules(g):
    ch=g.get_channel(num(setting(g.id,"rules_channel_id")))
    rows=many("SELECT id,title,body FROM rules WHERE guild_id=? ORDER BY id",(g.id,))
    if not isinstance(ch,discord.TextChannel): return None,"Сначала выбери канал правил."
    if not rows: return None,"Список правил пуст."
    e=embed("📜 ПРАВИЛА СЕРВЕРА","Ознакомься с правилами сообщества. Незнание правил не освобождает от ответственности.")
    for r in rows[:25]: e.add_field(name=f"{r['id']}. {r['title']}",value=r["body"][:1024],inline=False)
    old=num(setting(g.id,"rules_message_id"))
    if old:
        try:
            msg=await ch.fetch_message(old); await msg.edit(embed=e); return msg,None
        except discord.HTTPException: pass
    msg=await ch.send(embed=e); set_setting(g.id,"rules_message_id",msg.id); return msg,None
class RulesView(discord.ui.View):
    def __init__(self): super().__init__(timeout=180)
    @discord.ui.button(label="Добавить",emoji="➕",style=discord.ButtonStyle.success)
    async def add(self,i,b): await i.response.send_modal(RuleAdd())
    @discord.ui.button(label="Изменить",emoji="✏️",style=discord.ButtonStyle.primary)
    async def edit(self,i,b): await i.response.send_modal(RuleEdit())
    @discord.ui.button(label="Удалить",emoji="🗑️",style=discord.ButtonStyle.danger)
    async def delete(self,i,b): await i.response.send_modal(RuleDelete())
    @discord.ui.button(label="Список",emoji="📋",style=discord.ButtonStyle.secondary)
    async def listing(self,i,b):
        rows=many("SELECT id,title,body FROM rules WHERE guild_id=? ORDER BY id",(i.guild.id,))
        text="\\n".join(f"**{r['id']}. {r['title']}** — {r['body'][:160]}" for r in rows) or "Правил пока нет."
        await say(i,text[:1900])
    @discord.ui.button(label="Канал",emoji="📢",style=discord.ButtonStyle.secondary)
    async def channel(self,i,b): await i.response.send_modal(RuleChannel())
    @discord.ui.button(label="Опубликовать",emoji="🚀",style=discord.ButtonStyle.success)
    async def publish(self,i,b):
        msg,err=await publish_rules(i.guild)
        await say(i,err or f"✅ Правила опубликованы или обновлены в {msg.channel.mention}.")

# ---------- Temporary voice rooms ----------

class VoiceSetup(discord.ui.Modal,title="Настройка временных комнат"):
    trigger_id=discord.ui.TextInput(label="ID голосового канала-триггера")
    category_id=discord.ui.TextInput(label="ID категории для комнат",required=False)
    limit=discord.ui.TextInput(label="Лимит участников (0 = без лимита)",default="0")
    async def on_submit(self,i):
        trigger=i.guild.get_channel(num(self.trigger_id.value)); cat=i.guild.get_channel(num(self.category_id.value)) if self.category_id.value.strip() else None
        if not isinstance(trigger,discord.VoiceChannel) or (cat and not isinstance(cat,discord.CategoryChannel)):
            return await say(i,"Проверь ID голосового канала и категории.")
        set_setting(i.guild.id,"voice_trigger_id",trigger.id); set_setting(i.guild.id,"voice_category_id",cat.id if cat else 0)
        set_setting(i.guild.id,"voice_default_limit",max(0,min(99,num(self.limit.value))))
        await say(i,f"✅ Триггер настроен: {trigger.mention}")
class VoiceView(discord.ui.View):
    def __init__(self): super().__init__(timeout=180)
    @discord.ui.button(label="Настроить комнаты",emoji="⚙️",style=discord.ButtonStyle.primary)
    async def setup(self,i,b): await i.response.send_modal(VoiceSetup())
    @discord.ui.button(label="Проверить",emoji="🔎",style=discord.ButtonStyle.secondary)
    async def check(self,i,b):
        t=i.guild.get_channel(num(setting(i.guild.id,"voice_trigger_id")))
        c=i.guild.get_channel(num(setting(i.guild.id,"voice_category_id")))
        await say(i,f"Канал-триггер: {t.mention if t else 'не настроен'}\\nКатегория: {c.name if c else 'не задана'}")
class RoomModal(discord.ui.Modal):
    def __init__(self,action):
        titles={"rename":"Название комнаты","limit":"Лимит участников","transfer":"Передать владельца","invite":"Пригласить участника","kick":"Исключить участника"}
        super().__init__(title=titles[action]); self.action=action
        self.value=discord.ui.TextInput(label="Новое название" if action=="rename" else ("Лимит 0–99" if action=="limit" else "ID пользователя"))
        self.add_item(self.value)
    async def on_submit(self,i):
        row=one("SELECT * FROM voice_rooms WHERE text_id=?",(i.channel.id,))
        if not row: return await say(i,"Комната не найдена.")
        if i.user.id!=row["owner_id"] and not i.user.guild_permissions.manage_channels: return await say(i,"Управлять может владелец комнаты или администрация.")
        voice=i.guild.get_channel(row["voice_id"])
        if not voice: return await say(i,"Голосовая комната уже удалена.")
        if self.action=="rename":
            await voice.edit(name=self.value.value[:95]); return await say(i,"✅ Название изменено.")
        if self.action=="limit":
            limit=num(self.value.value,-1)
            if not 0<=limit<=99: return await say(i,"Укажи число от 0 до 99.")
            await voice.edit(user_limit=limit); return await say(i,"✅ Лимит изменён.")
        member=i.guild.get_member(num(self.value.value))
        if not member: return await say(i,"Участник не найден на сервере.")
        if self.action=="invite":
            await voice.set_permissions(member,view_channel=True,connect=True)
            text=i.guild.get_channel(row["text_id"])
            if text: await text.set_permissions(member,view_channel=True,send_messages=True,read_message_history=True)
            return await say(i,f"✅ {member.mention} приглашён.")
        if self.action=="kick":
            if member.voice and member.voice.channel and member.voice.channel.id==voice.id: await member.move_to(None)
            await voice.set_permissions(member,connect=False); return await say(i,f"✅ {member.mention} исключён.")
        if self.action=="transfer":
            run("UPDATE voice_rooms SET owner_id=? WHERE voice_id=?",(member.id,voice.id))
            await voice.set_permissions(member,view_channel=True,connect=True)
            return await say(i,f"👑 Новый владелец: {member.mention}")
async def room_action(i,action):
    row=one("SELECT * FROM voice_rooms WHERE text_id=?",(i.channel.id,))
    if not row: return await say(i,"Эта панель не привязана к комнате.")
    if i.user.id!=row["owner_id"] and not i.user.guild_permissions.manage_channels: return await say(i,"Управлять может владелец комнаты или администрация.")
    voice=i.guild.get_channel(row["voice_id"])
    if not voice: return await say(i,"Комната не найдена.")
    if action in ("rename","limit","transfer","invite","kick"): return await i.response.send_modal(RoomModal(action))
    everyone=i.guild.default_role
    if action=="lock": await voice.set_permissions(everyone,connect=False); msg="🔒 Комната закрыта."
    elif action=="unlock": await voice.set_permissions(everyone,connect=True); msg="🔓 Комната открыта."
    elif action=="hide": await voice.set_permissions(everyone,view_channel=False); msg="🙈 Комната скрыта."
    elif action=="show": await voice.set_permissions(everyone,view_channel=True); msg="👀 Комната видна."
    elif action=="delete":
        text=i.guild.get_channel(row["text_id"]); run("DELETE FROM voice_rooms WHERE voice_id=?",(voice.id,))
        if text: await text.delete(reason="Удаление временной комнаты")
        await voice.delete(reason="Удаление временной комнаты"); msg="🗑️ Комната удалена."
    await say(i,msg)
class RoomControls(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Название",emoji="📝",custom_id="room:rename",style=discord.ButtonStyle.primary)
    async def rename(self,i,b): await room_action(i,"rename")
    @discord.ui.button(label="Лимит",emoji="👥",custom_id="room:limit",style=discord.ButtonStyle.primary)
    async def limit(self,i,b): await room_action(i,"limit")
    @discord.ui.button(label="Закрыть",emoji="🔒",custom_id="room:lock")
    async def lock(self,i,b): await room_action(i,"lock")
    @discord.ui.button(label="Открыть",emoji="🔓",custom_id="room:unlock")
    async def unlock(self,i,b): await room_action(i,"unlock")
    @discord.ui.button(label="Скрыть",emoji="🙈",custom_id="room:hide")
    async def hide(self,i,b): await room_action(i,"hide")
    @discord.ui.button(label="Показать",emoji="👀",custom_id="room:show")
    async def show(self,i,b): await room_action(i,"show")
    @discord.ui.button(label="Пригласить",emoji="➕",custom_id="room:invite",style=discord.ButtonStyle.success)
    async def invite(self,i,b): await room_action(i,"invite")
    @discord.ui.button(label="Исключить",emoji="➖",custom_id="room:kick",style=discord.ButtonStyle.danger)
    async def kick(self,i,b): await room_action(i,"kick")
    @discord.ui.button(label="Передать владельца",emoji="👑",custom_id="room:transfer",style=discord.ButtonStyle.primary)
    async def transfer(self,i,b): await room_action(i,"transfer")
    @discord.ui.button(label="Удалить",emoji="🗑️",custom_id="room:delete",style=discord.ButtonStyle.danger)
    async def delete(self,i,b): await room_action(i,"delete")

async def make_room(member):
    g=member.guild; cat=g.get_channel(num(setting(g.id,"voice_category_id"))) if num(setting(g.id,"voice_category_id")) else None
    safe=re.sub(r"[^a-zA-Zа-яА-Я0-9_-]+","-",member.display_name).strip("-")[:65] or "участник"
    perms={g.default_role:discord.PermissionOverwrite(view_channel=True,connect=True),member:discord.PermissionOverwrite(view_channel=True,connect=True)}
    voice=await g.create_voice_channel(f"🎙️・{safe}",category=cat,overwrites=perms,user_limit=max(0,min(99,num(setting(g.id,"voice_default_limit")))))
    text_perms={g.default_role:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True),
                member:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)}
    text=await g.create_text_channel(f"💬・комната-{safe}"[:95],category=cat,overwrites=text_perms)
    run("INSERT OR REPLACE INTO voice_rooms VALUES(?,?,?,?)",(g.id,voice.id,text.id,member.id))
    await text.send(embed=embed("🎙️ Управление вашей комнатой",f"Владелец: {member.mention}\\nНастрой комнату кнопками ниже."),view=RoomControls())
    await member.move_to(voice)

# ---------- Tickets ----------

class TicketSetup(discord.ui.Modal,title="Настройка тикетов"):
    category_id=discord.ui.TextInput(label="ID категории")
    staff_role=discord.ui.TextInput(label="ID роли поддержки")
    panel_channel=discord.ui.TextInput(label="ID канала панели")
    async def on_submit(self,i):
        cat=i.guild.get_channel(num(self.category_id.value)); role=i.guild.get_role(num(self.staff_role.value)); ch=i.guild.get_channel(num(self.panel_channel.value))
        if not isinstance(cat,discord.CategoryChannel) or not role or not isinstance(ch,discord.TextChannel): return await say(i,"Проверь ID категории, роли и канала.")
        for k,v in [("ticket_category_id",cat.id),("ticket_staff_role_id",role.id),("ticket_panel_channel_id",ch.id)]: set_setting(i.guild.id,k,v)
        await say(i,"✅ Настройки тикетов сохранены.")
class TicketPanel(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Создать обращение",emoji="🎫",style=discord.ButtonStyle.success,custom_id="ticket:create")
    async def create(self,i,b):
        g=i.guild; old=one("SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status='open'",(g.id,i.user.id))
        if old and g.get_channel(old["channel_id"]): return await say(i,f"У тебя уже есть тикет: <#{old['channel_id']}>")
        cat=g.get_channel(num(setting(g.id,"ticket_category_id"))); role=g.get_role(num(setting(g.id,"ticket_staff_role_id")))
        if not isinstance(cat,discord.CategoryChannel) or not role: return await say(i,"Тикеты ещё не настроены.")
        ow={g.default_role:discord.PermissionOverwrite(view_channel=False),i.user:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True),
            g.me:discord.PermissionOverwrite(view_channel=True,send_messages=True,manage_channels=True,read_message_history=True),
            role:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)}
        ch=await g.create_text_channel(f"ticket-{i.user.name}"[:95],category=cat,overwrites=ow)
        run("INSERT INTO tickets(guild_id,channel_id,user_id,status,created_at) VALUES(?,?,?,'open',?)",(g.id,ch.id,i.user.id,int(time.time())))
        await ch.send(content=f"{i.user.mention} {role.mention}",embed=embed("🎫 Новое обращение","Опиши проблему. Поддержка скоро ответит."),view=TicketClose())
        await say(i,f"✅ Тикет создан: {ch.mention}"); await log_to(g,f"🎫 {i.user.mention} создал тикет {ch.mention}.")
class TicketClose(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Закрыть обращение",emoji="🔒",style=discord.ButtonStyle.danger,custom_id="ticket:close")
    async def close(self,i,b):
        row=one("SELECT * FROM tickets WHERE channel_id=? AND status='open'",(i.channel.id,))
        if not row: return await say(i,"Тикет уже закрыт.")
        role=i.guild.get_role(num(setting(i.guild.id,"ticket_staff_role_id")))
        staff=i.user.guild_permissions.manage_guild or (role and role in i.user.roles)
        if i.user.id!=row["user_id"] and not staff: return await say(i,"Закрыть тикет может автор или поддержка.")
        run("UPDATE tickets SET status='closed' WHERE channel_id=?",(i.channel.id,))
        opener=i.guild.get_member(row["user_id"])
        if opener: await i.channel.set_permissions(opener,send_messages=False)
        await i.channel.edit(name=f"closed-{i.channel.name}"[:95]); await say(i,"🔒 Тикет закрыт.")
        await log_to(i.guild,f"🔒 Тикет закрыт: {i.channel.mention}.")
class TicketAdmin(discord.ui.View):
    def __init__(self): super().__init__(timeout=180)
    @discord.ui.button(label="Настроить",emoji="⚙️",style=discord.ButtonStyle.primary)
    async def setup(self,i,b): await i.response.send_modal(TicketSetup())
    @discord.ui.button(label="Опубликовать панель",emoji="📢",style=discord.ButtonStyle.success)
    async def publish(self,i,b):
        ch=i.guild.get_channel(num(setting(i.guild.id,"ticket_panel_channel_id")))
        if not isinstance(ch,discord.TextChannel): return await say(i,"Сначала настрой тикеты.")
        msg=await ch.send(embed=embed("🎫 ПОДДЕРЖКА СООБЩЕСТВА","Нажми кнопку ниже, чтобы создать приватное обращение."),view=TicketPanel())
        bot.add_view(TicketPanel(),message_id=msg.id); await say(i,f"✅ Панель опубликована в {ch.mention}.")

# ---------- Moderation ----------

class ModModal(discord.ui.Modal):
    def __init__(self,action):
        super().__init__(title={"warn":"Предупреждение","timeout":"Тайм-аут","kick":"Исключение","ban":"Блокировка"}[action])
        self.action=action; self.uid=discord.ui.TextInput(label="ID участника"); self.reason=discord.ui.TextInput(label="Причина",style=discord.TextStyle.paragraph,max_length=500)
        self.add_item(self.uid); self.add_item(self.reason)
        if action=="timeout": self.duration=discord.ui.TextInput(label="Длительность в минутах",default="10"); self.add_item(self.duration)
    async def on_submit(self,i):
        uid=num(self.uid.value); member=i.guild.get_member(uid); reason=self.reason.value
        try:
            if self.action=="warn":
                if not member: return await say(i,"Участник не найден.")
                run("INSERT INTO warnings(guild_id,user_id,moderator_id,reason,created_at) VALUES(?,?,?,?,?)",(i.guild.id,uid,i.user.id,reason,int(time.time())))
                try: await member.send(f"⚠️ Предупреждение на сервере {i.guild.name}. Причина: {reason}")
                except discord.HTTPException: pass
                result="Предупреждение выдано."
            elif self.action=="timeout":
                if not member: return await say(i,"Участник не найден.")
                mins=max(1,min(40320,num(self.duration.value,10)))
                await member.timeout(datetime.now(timezone.utc)+timedelta(minutes=mins),reason=reason); result=f"Тайм-аут на {mins} минут."
            elif self.action=="kick":
                if not member: return await say(i,"Участник не найден.")
                await member.kick(reason=reason); result="Участник исключён."
            else:
                await i.guild.ban(discord.Object(id=uid),reason=reason,delete_message_seconds=0); result="Участник заблокирован."
            await say(i,"✅ "+result); await log_to(i.guild,f"🛡️ {i.user.mention}: {self.action} для <@{uid}>. Причина: {reason}")
        except discord.Forbidden: await say(i,"⛔ Недостаточно прав. Проверь права и иерархию ролей бота.")
        except discord.HTTPException as e: await say(i,f"Ошибка Discord: {e}")
class ModPanel(discord.ui.View):
    def __init__(self): super().__init__(timeout=180)
    @discord.ui.button(label="Предупреждение",emoji="⚠️")
    async def warn(self,i,b): await i.response.send_modal(ModModal("warn"))
    @discord.ui.button(label="Тайм-аут",emoji="🔇",style=discord.ButtonStyle.primary)
    async def timeout(self,i,b): await i.response.send_modal(ModModal("timeout"))
    @discord.ui.button(label="Исключить",emoji="👢",style=discord.ButtonStyle.danger)
    async def kick(self,i,b): await i.response.send_modal(ModModal("kick"))
    @discord.ui.button(label="Заблокировать",emoji="🔨",style=discord.ButtonStyle.danger)
    async def ban(self,i,b): await i.response.send_modal(ModModal("ban"))

# ---------- Giveaways ----------

class GiveawayCreate(discord.ui.Modal,title="Создать розыгрыш"):
    channel_id=discord.ui.TextInput(label="ID канала")
    prize=discord.ui.TextInput(label="Приз",max_length=200)
    duration=discord.ui.TextInput(label="Длительность в минутах",default="60")
    winners=discord.ui.TextInput(label="Количество победителей",default="1")
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(self.channel_id.value)); mins=max(1,min(43200,num(self.duration.value,60))); count=max(1,min(20,num(self.winners.value,1)))
        if not isinstance(ch,discord.TextChannel): return await say(i,"Канал не найден.")
        end=int(time.time())+mins*60
        cur=run("INSERT INTO giveaways(guild_id,channel_id,prize,winners,end_at) VALUES(?,?,?,?,?)",(i.guild.id,ch.id,self.prize.value,count,end))
        msg=await ch.send(embed=embed("🎁 РОЗЫГРЫШ",f"**Приз:** {self.prize.value}\\n**Победителей:** {count}\\n**Завершение:** <t:{end}:R>\\n\\nНажми кнопку, чтобы участвовать!",discord.Color.gold()),view=GiveawayEntry())
        run("UPDATE giveaways SET message_id=? WHERE id=?",(msg.id,cur.lastrowid))
        await say(i,f"✅ Розыгрыш опубликован в {ch.mention}.")
class GiveawayEntry(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Участвовать",emoji="🎉",style=discord.ButtonStyle.success,custom_id="giveaway:enter")
    async def enter(self,i,b):
        row=one("SELECT id FROM giveaways WHERE message_id=? AND ended=0",(i.message.id,))
        if not row: return await say(i,"Розыгрыш завершён.")
        try: run("INSERT INTO giveaway_entries VALUES(?,?)",(row["id"],i.user.id)); await say(i,"🎉 Ты участвуешь!")
        except sqlite3.IntegrityError: await say(i,"Ты уже участвуешь.")
class GiveawayPanel(discord.ui.View):
    def __init__(self): super().__init__(timeout=180)
    @discord.ui.button(label="Создать розыгрыш",emoji="🎁",style=discord.ButtonStyle.success)
    async def create(self,i,b): await i.response.send_modal(GiveawayCreate())

# ---------- Events ----------

class EventCreate(discord.ui.Modal,title="Создать мероприятие"):
    channel_id=discord.ui.TextInput(label="ID канала анонсов")
    name=discord.ui.TextInput(label="Название",max_length=100)
    description=discord.ui.TextInput(label="Описание",style=discord.TextStyle.paragraph,max_length=900)
    start=discord.ui.TextInput(label="Начало (2026-12-31T19:00)")
    end=discord.ui.TextInput(label="Окончание (2026-12-31T20:00)")
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(self.channel_id.value))
        if not isinstance(ch,discord.TextChannel): return await say(i,"Канал анонсов не найден.")
        try:
            start=dt(self.start.value); end=dt(self.end.value)
            if end<=start: return await say(i,"Окончание должно быть позже начала.")
            location="Discord-сервер"
            ev=await i.guild.create_scheduled_event(name=self.name.value,start_time=start,end_time=end,
                privacy_level=discord.PrivacyLevel.guild_only,entity_type=discord.EntityType.external,
                location=location,description=self.description.value)
            e=embed("🎉 НОВОЕ МЕРОПРИЯТИЕ",f"**{self.name.value}**\\n{self.description.value}\\n\\n🕒 <t:{int(start.timestamp())}:F>\\n📍 {location}\\n[Открыть событие]({ev.url})",discord.Color.green())
            await ch.send(embed=e); await say(i,"✅ Событие создано и опубликовано.")
        except (ValueError,discord.HTTPException) as exc: await say(i,f"Не удалось создать событие. Проверь дату и права бота. {exc}")
class EventPanel(discord.ui.View):
    def __init__(self): super().__init__(timeout=180)
    @discord.ui.button(label="Создать мероприятие",emoji="🎉",style=discord.ButtonStyle.success)
    async def create(self,i,b): await i.response.send_modal(EventCreate())

# ---------- Applications ----------

class ApplicationSetup(discord.ui.Modal,title="Настройка заявок"):
    channel_id=discord.ui.TextInput(label="ID канала заявок")
    staff_role=discord.ui.TextInput(label="ID роли проверяющих")
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(self.channel_id.value)); role=i.guild.get_role(num(self.staff_role.value))
        if not isinstance(ch,discord.TextChannel) or not role: return await say(i,"Проверь ID канала и роли.")
        set_setting(i.guild.id,"applications_channel_id",ch.id); set_setting(i.guild.id,"applications_staff_role_id",role.id)
        await say(i,"✅ Настройки заявок сохранены.")
class ApplicationAdmin(discord.ui.View):
    def __init__(self): super().__init__(timeout=180)
    @discord.ui.button(label="Настроить",emoji="⚙️",style=discord.ButtonStyle.primary)
    async def setup(self,i,b): await i.response.send_modal(ApplicationSetup())
    @discord.ui.button(label="Опубликовать панель",emoji="📢",style=discord.ButtonStyle.success)
    async def publish(self,i,b):
        ch=i.guild.get_channel(num(setting(i.guild.id,"applications_channel_id")))
        if not isinstance(ch,discord.TextChannel): return await say(i,"Сначала настрой канал заявок.")
        msg=await ch.send(embed=embed("📝 ЗАЯВКИ В КОМАНДУ","Хочешь стать частью команды? Нажми кнопку и заполни анкету."),view=ApplicationApply())
        bot.add_view(ApplicationApply(),message_id=msg.id); await say(i,f"Панель опубликована в {ch.mention}.")
class ApplicationApply(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Подать заявку",emoji="📝",style=discord.ButtonStyle.success,custom_id="application:apply")
    async def apply(self,i,b): await i.response.send_modal(ApplicationForm())
class ApplicationForm(discord.ui.Modal,title="Заявка в команду"):
    position=discord.ui.TextInput(label="Должность",max_length=100)
    about=discord.ui.TextInput(label="Расскажи о себе",style=discord.TextStyle.paragraph,max_length=800)
    experience=discord.ui.TextInput(label="Опыт / примеры работ",style=discord.TextStyle.paragraph,max_length=800,required=False)
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(setting(i.guild.id,"applications_channel_id")))
        if not isinstance(ch,discord.TextChannel): return await say(i,"Канал заявок не настроен.")
        e=embed("📝 НОВАЯ ЗАЯВКА",f"**Кандидат:** {i.user.mention}\\n**Должность:** {self.position.value}\\n\\n**О себе:**\\n{self.about.value}\\n\\n**Опыт:**\\n{self.experience.value or 'Не указан'}")
        msg=await ch.send(embed=e,view=ApplicationReview())
        run("INSERT INTO applications(guild_id,channel_id,message_id,user_id,position,about,experience) VALUES(?,?,?,?,?,?,?)",
            (i.guild.id,ch.id,msg.id,i.user.id,self.position.value,self.about.value,self.experience.value))
        await say(i,"✅ Заявка отправлена.")
class ApplicationReview(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    async def decide(self,i,status):
        row=one("SELECT * FROM applications WHERE message_id=?",(i.message.id,))
        if not row or row["status"]!="pending": return await say(i,"Заявка уже рассмотрена.")
        role=i.guild.get_role(num(setting(i.guild.id,"applications_staff_role_id")))
        if not (i.user.guild_permissions.manage_guild or (role and role in i.user.roles)): return await say(i,"Нет доступа к рассмотрению заявок.")
        run("UPDATE applications SET status=? WHERE id=?",(status,row["id"]))
        member=i.guild.get_member(row["user_id"])
        if member:
            try: await member.send(f"Твоя заявка на **{row['position']}**: {'одобрена ✅' if status=='approved' else 'отклонена ❌'}.")
            except discord.HTTPException: pass
        e=i.message.embeds[0]; e.add_field(name="Решение",value=f"{'Одобрена ✅' if status=='approved' else 'Отклонена ❌'} — {i.user.mention}")
        await i.message.edit(embed=e,view=None); await say(i,"Решение сохранено.")
    @discord.ui.button(label="Одобрить",emoji="✅",style=discord.ButtonStyle.success,custom_id="application:approve")
    async def approve(self,i,b): await self.decide(i,"approved")
    @discord.ui.button(label="Отклонить",emoji="❌",style=discord.ButtonStyle.danger,custom_id="application:reject")
    async def reject(self,i,b): await self.decide(i,"rejected")

# ---------- Suggestions and polls ----------

class SuggestionSetup(discord.ui.Modal,title="Настройка предложений"):
    channel_id=discord.ui.TextInput(label="ID канала предложений")
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(self.channel_id.value))
        if not isinstance(ch,discord.TextChannel): return await say(i,"Канал не найден.")
        set_setting(i.guild.id,"suggestions_channel_id",ch.id); await say(i,f"Канал предложений: {ch.mention}")
class SuggestionAdmin(discord.ui.View):
    def __init__(self): super().__init__(timeout=180)
    @discord.ui.button(label="Настроить канал",emoji="⚙️")
    async def setup(self,i,b): await i.response.send_modal(SuggestionSetup())
    @discord.ui.button(label="Опубликовать панель",emoji="📢",style=discord.ButtonStyle.success)
    async def publish(self,i,b):
        ch=i.guild.get_channel(num(setting(i.guild.id,"suggestions_channel_id")))
        if not isinstance(ch,discord.TextChannel): return await say(i,"Сначала настрой канал.")
        msg=await ch.send(embed=embed("💡 ПРЕДЛОЖЕНИЯ","Нажми кнопку, чтобы предложить идею для сервера или канала."),view=SuggestionCreate())
        bot.add_view(SuggestionCreate(),message_id=msg.id); await say(i,f"Панель опубликована в {ch.mention}.")
class SuggestionCreate(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Предложить идею",emoji="💡",style=discord.ButtonStyle.success,custom_id="suggestion:create")
    async def create(self,i,b): await i.response.send_modal(SuggestionForm())
class SuggestionForm(discord.ui.Modal,title="Новое предложение"):
    body=discord.ui.TextInput(label="Твоё предложение",style=discord.TextStyle.paragraph,max_length=1500)
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(setting(i.guild.id,"suggestions_channel_id")))
        if not isinstance(ch,discord.TextChannel): return await say(i,"Канал предложений не настроен.")
        msg=await ch.send(embed=embed("💡 ПРЕДЛОЖЕНИЕ",f"**Автор:** {i.user.mention}\\n\\n{self.body.value}"),view=SuggestionVote())
        run("INSERT INTO suggestions(guild_id,channel_id,message_id,user_id,body) VALUES(?,?,?,?,?)",(i.guild.id,ch.id,msg.id,i.user.id,self.body.value))
        await say(i,"✅ Предложение отправлено.")
class SuggestionVote(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    async def vote(self,i,val):
        row=one("SELECT id FROM suggestions WHERE message_id=?",(i.message.id,))
        if not row: return await say(i,"Предложение не найдено.")
        old=one("SELECT vote FROM suggestion_votes WHERE suggestion_id=? AND user_id=?",(row["id"],i.user.id))
        if old and old["vote"]==val:
            run("DELETE FROM suggestion_votes WHERE suggestion_id=? AND user_id=?",(row["id"],i.user.id)); text="Голос убран."
        else:
            run("INSERT INTO suggestion_votes VALUES(?,?,?) ON CONFLICT(suggestion_id,user_id) DO UPDATE SET vote=excluded.vote",(row["id"],i.user.id,val)); text="Голос учтён."
        up=one("SELECT COUNT(*) n FROM suggestion_votes WHERE suggestion_id=? AND vote=1",(row["id"],))["n"]
        down=one("SELECT COUNT(*) n FROM suggestion_votes WHERE suggestion_id=? AND vote=-1",(row["id"],))["n"]
        e=i.message.embeds[0]; e.set_footer(text=f"За: {up} • Против: {down}"); await i.message.edit(embed=e); await say(i,text)
    @discord.ui.button(label="За",emoji="👍",style=discord.ButtonStyle.success,custom_id="suggestion:up")
    async def up(self,i,b): await self.vote(i,1)
    @discord.ui.button(label="Против",emoji="👎",style=discord.ButtonStyle.danger,custom_id="suggestion:down")
    async def down(self,i,b): await self.vote(i,-1)

class PollForm(discord.ui.Modal,title="Создать опрос"):
    channel_id=discord.ui.TextInput(label="ID канала")
    question=discord.ui.TextInput(label="Вопрос",max_length=200)
    options=discord.ui.TextInput(label="Варианты через | (2–10)",placeholder="Первый | Второй | Третий",max_length=800)
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(self.channel_id.value)); opts=[x.strip() for x in self.options.value.split("|") if x.strip()][:10]
        if not isinstance(ch,discord.TextChannel) or len(opts)<2: return await say(i,"Проверь канал и укажи минимум два варианта через |.")
        nums_emoji=["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
        msg=await ch.send(embed=embed("📊 ОПРОС: "+self.question.value,"\\n".join(f"{nums_emoji[n]} {x}" for n,x in enumerate(opts))))
        for em in nums_emoji[:len(opts)]: await msg.add_reaction(em)
        await say(i,f"Опрос опубликован в {ch.mention}.")
class PollAdmin(discord.ui.View):
    def __init__(self): super().__init__(timeout=180)
    @discord.ui.button(label="Создать опрос",emoji="📊",style=discord.ButtonStyle.success)
    async def create(self,i,b): await i.response.send_modal(PollForm())

# ---------- Levels, achievements, shop and games ----------

def level_row(gid,uid):
    row=one("SELECT * FROM levels WHERE guild_id=? AND user_id=?",(gid,uid))
    if not row:
        run("INSERT OR IGNORE INTO levels(guild_id,user_id) VALUES(?,?)",(gid,uid))
        row=one("SELECT * FROM levels WHERE guild_id=? AND user_id=?",(gid,uid))
    return row
class LevelsPanel(discord.ui.View):
    def __init__(self): super().__init__(timeout=180)
    @discord.ui.button(label="Мой профиль",emoji="👤")
    async def profile(self,i,b):
        r=level_row(i.guild.id,i.user.id)
        e=embed(f"⭐ {i.user.display_name}",f"Уровень: **{r['level']}**\\nОпыт: **{r['xp']} XP**\\nСообщений: **{r['messages']}**\\nМонеты: **{r['coins']}**")
        e.set_thumbnail(url=i.user.display_avatar.url); await say(i,emb=e)
    @discord.ui.button(label="Топ участников",emoji="🏆",style=discord.ButtonStyle.success)
    async def top(self,i,b):
        rows=many("SELECT user_id,level,xp FROM levels WHERE guild_id=? ORDER BY level DESC,xp DESC LIMIT 10",(i.guild.id,))
        text="\\n".join(f"**{n}.** <@{r['user_id']}> — уровень {r['level']} ({r['xp']} XP)" for n,r in enumerate(rows,1)) or "Статистики пока нет."
        await say(i,emb=embed("🏆 ТОП УЧАСТНИКОВ",text))
class AchievementsPanel(discord.ui.View):
    def __init__(self): super().__init__(timeout=180)
    @discord.ui.button(label="Мои достижения",emoji="🏅")
    async def mine(self,i,b):
        r=level_row(i.guild.id,i.user.id); a=[]
        if r["messages"]>=1:a.append("💬 Первое сообщение")
        if r["messages"]>=100:a.append("💬 100 сообщений")
        if r["messages"]>=500:a.append("🔥 500 сообщений")
        if r["level"]>=5:a.append("⭐ Уровень 5")
        if r["level"]>=10:a.append("🌟 Уровень 10")
        if r["coins"]>=500:a.append("💰 500 монет")
        await say(i,emb=embed("🏅 ДОСТИЖЕНИЯ","\\n".join(a) or "Пока нет достижений. Будь активнее!"))
class ShopAdd(discord.ui.Modal,title="Добавить роль в магазин"):
    role_id=discord.ui.TextInput(label="ID роли"); cost=discord.ui.TextInput(label="Цена в монетах")
    async def on_submit(self,i):
        role=i.guild.get_role(num(self.role_id.value)); price=num(self.cost.value,-1)
        if not role or role.is_default() or price<=0:return await say(i,"Проверь ID роли и цену.")
        run("INSERT INTO shop_items(guild_id,role_id,cost) VALUES(?,?,?)",(i.guild.id,role.id,price)); await say(i,f"Роль {role.mention} добавлена за {price} монет.")
class ShopSelect(discord.ui.Select):
    def __init__(self,g):
        rows=many("SELECT role_id,cost FROM shop_items WHERE guild_id=?",(g.id,)); opts=[]
        for r in rows[:25]:
            role=g.get_role(r["role_id"])
            if role:opts.append(discord.SelectOption(label=role.name[:90],value=str(role.id),description=f"{r['cost']} монет"))
        super().__init__(placeholder="Выбери роль…",min_values=1,max_values=1,options=opts or [discord.SelectOption(label="Магазин пуст",value="0")])
    async def callback(self,i):
        rid=num(self.values[0]); item=one("SELECT cost FROM shop_items WHERE guild_id=? AND role_id=?",(i.guild.id,rid))
        if not item:return await say(i,"Магазин пока пуст.")
        role=i.guild.get_role(rid); r=level_row(i.guild.id,i.user.id)
        if not role:return await say(i,"Роль больше не существует.")
        if role in i.user.roles:return await say(i,"У тебя уже есть эта роль.")
        if r["coins"]<item["cost"]:return await say(i,f"Не хватает монет. Нужно {item['cost']}, у тебя {r['coins']}.")
        try:
            await i.user.add_roles(role,reason="Покупка в магазине")
            run("UPDATE levels SET coins=coins-? WHERE guild_id=? AND user_id=?",(item["cost"],i.guild.id,i.user.id))
            await say(i,f"🛒 Куплена роль {role.mention}!")
        except discord.Forbidden:await say(i,"Бот не может выдать роль. Подними его роль выше.")
class ShopSelectPanel(discord.ui.View):
    def __init__(self,g):super().__init__(timeout=120);self.add_item(ShopSelect(g))
class ShopPanel(discord.ui.View):
    def __init__(self):super().__init__(timeout=180)
    @discord.ui.button(label="Купить роль",emoji="🛒",style=discord.ButtonStyle.success)
    async def buy(self,i,b):await say(i,"Выбери товар:",view=ShopSelectPanel(i.guild))
    @discord.ui.button(label="Добавить товар",emoji="➕",style=discord.ButtonStyle.primary)
    async def add(self,i,b):
        if not (i.user.guild_permissions.manage_guild or i.user.guild_permissions.administrator):return await say(i,"Только администрация может добавлять товары.")
        await i.response.send_modal(ShopAdd())
class GamesPanel(discord.ui.View):
    def __init__(self):super().__init__(timeout=180)
    @discord.ui.button(label="Ежедневная награда",emoji="🎁",style=discord.ButtonStyle.success)
    async def daily(self,i,b):
        old=one("SELECT claimed_at FROM daily_claims WHERE guild_id=? AND user_id=?",(i.guild.id,i.user.id)); now=int(time.time())
        if old and now-old["claimed_at"]<86400:
            left=86400-(now-old["claimed_at"]);return await say(i,f"Награда уже получена. Осталось {left//3600} ч. {(left%3600)//60} мин.")
        reward=random.randint(40,100);level_row(i.guild.id,i.user.id)
        run("INSERT INTO daily_claims VALUES(?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET claimed_at=excluded.claimed_at",(i.guild.id,i.user.id,now))
        run("UPDATE levels SET coins=coins+? WHERE guild_id=? AND user_id=?",(reward,i.guild.id,i.user.id));await say(i,f"🎁 Ты получил **{reward} монет**!")
    @discord.ui.button(label="Орёл или решка",emoji="🪙")
    async def coin(self,i,b):
        won=random.choice([True,False]);reward=random.choice([20,40]) if won else -20;level_row(i.guild.id,i.user.id)
        run("UPDATE levels SET coins=MAX(0,coins+?) WHERE guild_id=? AND user_id=?",(reward,i.guild.id,i.user.id))
        await say(i,f"🪙 Выпало **{'орёл' if won else 'решка'}**. Изменение баланса: **{reward:+} монет**.")

# ---------- YouTube and Twitch notifications ----------

class YTAdd(discord.ui.Modal,title="Добавить YouTube-канал"):
    notify_channel=discord.ui.TextInput(label="ID канала Discord для уведомлений")
    youtube_id=discord.ui.TextInput(label="ID YouTube-канала (UC...)")
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(self.notify_channel.value)); cid=self.youtube_id.value.strip()
        if not isinstance(ch,discord.TextChannel) or not cid.startswith("UC"):return await say(i,"Проверь ID Discord-канала и YouTube ID (начинается с UC).")
        run("INSERT INTO youtube_feeds(guild_id,channel_id,feed_url) VALUES(?,?,?)",(i.guild.id,ch.id,f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"))
        await say(i,f"✅ YouTube добавлен. Уведомления: {ch.mention}.")
class YTRemove(discord.ui.Modal,title="Удалить YouTube-канал"):
    feed_id=discord.ui.TextInput(label="ID записи из списка")
    async def on_submit(self,i):
        cur=run("DELETE FROM youtube_feeds WHERE id=? AND guild_id=?",(num(self.feed_id.value),i.guild.id));await say(i,"Удалено." if cur.rowcount else "Запись не найдена.")
class YouTubePanel(discord.ui.View):
    def __init__(self):super().__init__(timeout=180)
    @discord.ui.button(label="Добавить канал",emoji="➕",style=discord.ButtonStyle.success)
    async def add(self,i,b):await i.response.send_modal(YTAdd())
    @discord.ui.button(label="Список каналов",emoji="📋")
    async def listing(self,i,b):
        rows=many("SELECT id,channel_id,feed_url FROM youtube_feeds WHERE guild_id=?",(i.guild.id,))
        await say(i,"\\n".join(f"ID {r['id']} → <#{r['channel_id']}> • `{r['feed_url'].split('channel_id=')[-1]}`" for r in rows) or "Список пуст.")
    @discord.ui.button(label="Удалить канал",emoji="🗑️",style=discord.ButtonStyle.danger)
    async def remove(self,i,b):await i.response.send_modal(YTRemove())
class TwitchAdd(discord.ui.Modal,title="Добавить Twitch-канал"):
    notify_channel=discord.ui.TextInput(label="ID канала Discord для уведомлений")
    username=discord.ui.TextInput(label="Имя Twitch-канала")
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(self.notify_channel.value)); user=self.username.value.strip().lower().lstrip("@")
        if not isinstance(ch,discord.TextChannel) or not re.fullmatch(r"[a-z0-9_]{3,25}",user):return await say(i,"Проверь ID канала и имя Twitch.")
        run("INSERT INTO twitch_channels(guild_id,channel_id,username) VALUES(?,?,?)",(i.guild.id,ch.id,user));await say(i,f"✅ Twitch {user} добавлен.")
class TwitchRemove(discord.ui.Modal,title="Удалить Twitch-канал"):
    entry_id=discord.ui.TextInput(label="ID записи из списка")
    async def on_submit(self,i):
        cur=run("DELETE FROM twitch_channels WHERE id=? AND guild_id=?",(num(self.entry_id.value),i.guild.id));await say(i,"Удалено." if cur.rowcount else "Не найдено.")
class TwitchPanel(discord.ui.View):
    def __init__(self):super().__init__(timeout=180)
    @discord.ui.button(label="Добавить канал",emoji="➕",style=discord.ButtonStyle.success)
    async def add(self,i,b):await i.response.send_modal(TwitchAdd())
    @discord.ui.button(label="Список каналов",emoji="📋")
    async def listing(self,i,b):
        rows=many("SELECT id,channel_id,username FROM twitch_channels WHERE guild_id=?",(i.guild.id,))
        await say(i,"\\n".join(f"ID {r['id']} → **{r['username']}** в <#{r['channel_id']}>" for r in rows) or "Список пуст.")
    @discord.ui.button(label="Удалить канал",emoji="🗑️",style=discord.ButtonStyle.danger)
    async def remove(self,i,b):await i.response.send_modal(TwitchRemove())

# ---------- Logs and settings ----------

class LogSetup(discord.ui.Modal,title="Настройка логов"):
    channel_id=discord.ui.TextInput(label="ID канала логов")
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(self.channel_id.value))
        if not isinstance(ch,discord.TextChannel):return await say(i,"Канал не найден.")
        set_setting(i.guild.id,"logs_channel_id",ch.id);await say(i,f"Логи будут отправляться в {ch.mention}.")
class LogsPanel(discord.ui.View):
    def __init__(self):super().__init__(timeout=180)
    @discord.ui.button(label="Настроить канал",emoji="⚙️")
    async def setup(self,i,b):await i.response.send_modal(LogSetup())
    @discord.ui.button(label="Тест логов",emoji="🧪",style=discord.ButtonStyle.success)
    async def test(self,i,b):await log_to(i.guild,f"Тестовое сообщение от {i.user.mention}.");await say(i,"Проверка отправлена, если канал настроен.")
class SettingsPanel(discord.ui.View):
    def __init__(self):super().__init__(timeout=180)
    @discord.ui.button(label="Показать настройки",emoji="📋")
    async def show(self,i,b):
        keys=["welcome_channel_id","rules_channel_id","role_channel_id","logs_channel_id","ticket_category_id","ticket_panel_channel_id","voice_trigger_id","voice_category_id","applications_channel_id","suggestions_channel_id"]
        await say(i,emb=embed("⚙️ НАСТРОЙКИ","\\n".join(f"**{k}:** `{setting(i.guild.id,k) or 'не настроено'}`" for k in keys)))
    @discord.ui.button(label="Статус бота",emoji="🤖",style=discord.ButtonStyle.success)
    async def status(self,i,b):await say(i,f"✅ Бот работает. Задержка: **{round(bot.latency*1000)} мс**. База данных: SQLite.")


# ---------- Extra community tools: FAQ, clips, reminders, birthdays, automod ----------

def init_extra_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS faqs(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,question TEXT,answer TEXT);
    CREATE TABLE IF NOT EXISTS clips(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,channel_id INTEGER,user_id INTEGER,url TEXT,note TEXT,created_at INTEGER);
    CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,channel_id INTEGER,message TEXT,send_at INTEGER,sent INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS birthdays(guild_id INTEGER,user_id INTEGER,day INTEGER,month INTEGER,last_year INTEGER DEFAULT 0,PRIMARY KEY(guild_id,user_id));
    """)

class FAQAdd(discord.ui.Modal,title="Добавить вопрос и ответ"):
    question=discord.ui.TextInput(label="Вопрос",max_length=200)
    answer=discord.ui.TextInput(label="Ответ",style=discord.TextStyle.paragraph,max_length=1500)
    async def on_submit(self,i):
        run("INSERT INTO faqs(guild_id,question,answer) VALUES(?,?,?)",(i.guild.id,self.question.value,self.answer.value))
        await say(i,"✅ Вопрос добавлен. Нажми «Опубликовать FAQ».")
class FAQDelete(discord.ui.Modal,title="Удалить вопрос"):
    item_id=discord.ui.TextInput(label="ID вопроса")
    async def on_submit(self,i):
        cur=run("DELETE FROM faqs WHERE id=? AND guild_id=?",(num(self.item_id.value),i.guild.id))
        await say(i,"🗑️ Вопрос удалён." if cur.rowcount else "Вопрос не найден.")
class FAQSetup(discord.ui.Modal,title="Настройка FAQ"):
    channel_id=discord.ui.TextInput(label="ID канала FAQ")
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(self.channel_id.value))
        if not isinstance(ch,discord.TextChannel):return await say(i,"Канал не найден.")
        set_setting(i.guild.id,"faq_channel_id",ch.id);await say(i,f"FAQ будет опубликован в {ch.mention}.")
class FAQPanel(discord.ui.View):
    def __init__(self):super().__init__(timeout=180)
    @discord.ui.button(label="Настроить канал",emoji="⚙️")
    async def setup(self,i,b):await i.response.send_modal(FAQSetup())
    @discord.ui.button(label="Добавить вопрос",emoji="➕",style=discord.ButtonStyle.success)
    async def add(self,i,b):await i.response.send_modal(FAQAdd())
    @discord.ui.button(label="Удалить вопрос",emoji="🗑️",style=discord.ButtonStyle.danger)
    async def delete(self,i,b):await i.response.send_modal(FAQDelete())
    @discord.ui.button(label="Список вопросов",emoji="📋")
    async def listing(self,i,b):
        rows=many("SELECT id,question FROM faqs WHERE guild_id=? ORDER BY id",(i.guild.id,))
        await say(i,"\\n".join(f"**{r['id']}.** {r['question']}" for r in rows) or "FAQ пока пуст.")
    @discord.ui.button(label="Опубликовать FAQ",emoji="📢",style=discord.ButtonStyle.success)
    async def publish(self,i,b):
        ch=i.guild.get_channel(num(setting(i.guild.id,"faq_channel_id")));rows=many("SELECT question,answer FROM faqs WHERE guild_id=? ORDER BY id",(i.guild.id,))
        if not isinstance(ch,discord.TextChannel) or not rows:return await say(i,"Настрой канал и добавь вопросы.")
        e=embed("❓ ЧАСТО ЗАДАВАЕМЫЕ ВОПРОСЫ","Ответы на популярные вопросы сообщества.")
        for r in rows[:25]:e.add_field(name=r["question"][:256],value=r["answer"][:1024],inline=False)
        await ch.send(embed=e);await say(i,f"FAQ опубликован в {ch.mention}.")

class ClipSetup(discord.ui.Modal,title="Настройка предложений клипов"):
    channel_id=discord.ui.TextInput(label="ID канала для клипов")
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(self.channel_id.value))
        if not isinstance(ch,discord.TextChannel):return await say(i,"Канал не найден.")
        set_setting(i.guild.id,"clips_channel_id",ch.id);await say(i,f"Клипы будут отправляться в {ch.mention}.")
class ClipPanel(discord.ui.View):
    def __init__(self):super().__init__(timeout=180)
    @discord.ui.button(label="Настроить канал",emoji="⚙️")
    async def setup(self,i,b):await i.response.send_modal(ClipSetup())
    @discord.ui.button(label="Опубликовать панель",emoji="📢",style=discord.ButtonStyle.success)
    async def publish(self,i,b):
        ch=i.guild.get_channel(num(setting(i.guild.id,"clips_channel_id")))
        if not isinstance(ch,discord.TextChannel):return await say(i,"Сначала настрой канал клипов.")
        msg=await ch.send(embed=embed("🎞️ ПРЕДЛОЖИТЬ КЛИП","Отправь ссылку на свой клип, который можно использовать в роликах."),view=ClipSubmit())
        bot.add_view(ClipSubmit(),message_id=msg.id);await say(i,f"Панель опубликована в {ch.mention}.")
class ClipSubmit(discord.ui.View):
    def __init__(self):super().__init__(timeout=None)
    @discord.ui.button(label="Отправить клип",emoji="🎬",style=discord.ButtonStyle.success,custom_id="clip:submit")
    async def submit(self,i,b):await i.response.send_modal(ClipForm())
class ClipForm(discord.ui.Modal,title="Отправить клип"):
    url=discord.ui.TextInput(label="Ссылка на клип или видео",max_length=500)
    note=discord.ui.TextInput(label="Комментарий",style=discord.TextStyle.paragraph,max_length=500,required=False)
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(setting(i.guild.id,"clips_channel_id")))
        if not isinstance(ch,discord.TextChannel):return await say(i,"Канал клипов не настроен.")
        if not re.match(r"https?://",self.url.value.strip()):return await say(i,"Укажи корректную ссылку, начинающуюся с http:// или https://.")
        e=embed("🎞️ НОВЫЙ КЛИП",f"**Автор:** {i.user.mention}\\n**Ссылка:** {self.url.value}\\n**Комментарий:** {self.note.value or 'Нет'}")
        await ch.send(embed=e);run("INSERT INTO clips(guild_id,channel_id,user_id,url,note,created_at) VALUES(?,?,?,?,?,?)",(i.guild.id,ch.id,i.user.id,self.url.value,self.note.value,int(time.time())))
        await say(i,"✅ Клип отправлен команде.")

class ReminderSetup(discord.ui.Modal,title="Создать напоминание"):
    channel_id=discord.ui.TextInput(label="ID канала")
    send_at=discord.ui.TextInput(label="Дата и время ISO (2026-12-31T19:00)")
    message=discord.ui.TextInput(label="Текст напоминания",style=discord.TextStyle.paragraph,max_length=1500)
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(self.channel_id.value))
        if not isinstance(ch,discord.TextChannel):return await say(i,"Канал не найден.")
        try:when=dt(self.send_at.value)
        except ValueError:return await say(i,"Неверный формат даты. Пример: 2026-12-31T19:00")
        if when.timestamp()<=time.time():return await say(i,"Укажи время в будущем.")
        run("INSERT INTO reminders(guild_id,channel_id,message,send_at) VALUES(?,?,?,?)",(i.guild.id,ch.id,self.message.value,int(when.timestamp())))
        await say(i,f"⏰ Напоминание запланировано для {ch.mention}: <t:{int(when.timestamp())}:F>.")
class ReminderPanel(discord.ui.View):
    def __init__(self):super().__init__(timeout=180)
    @discord.ui.button(label="Создать напоминание",emoji="⏰",style=discord.ButtonStyle.success)
    async def create(self,i,b):await i.response.send_modal(ReminderSetup())
    @discord.ui.button(label="Мои напоминания сервера",emoji="📋")
    async def listing(self,i,b):
        rows=many("SELECT id,channel_id,message,send_at FROM reminders WHERE guild_id=? AND sent=0 ORDER BY send_at LIMIT 15",(i.guild.id,))
        text="\\n".join(f"**#{r['id']}** <t:{r['send_at']}:R> в <#{r['channel_id']}> — {r['message'][:80]}" for r in rows) or "Активных напоминаний нет."
        await say(i,text[:1900])

class BirthdaySet(discord.ui.Modal,title="Установить день рождения"):
    date=discord.ui.TextInput(label="Дата в формате ДД.ММ",placeholder="Например: 25.12")
    async def on_submit(self,i):
        try:day,month=[int(x) for x in self.date.value.split(".")]
        except Exception:return await say(i,"Укажи дату в формате ДД.ММ, например 25.12.")
        if not (1<=day<=31 and 1<=month<=12):return await say(i,"Неверная дата.")
        run("INSERT INTO birthdays(guild_id,user_id,day,month,last_year) VALUES(?,?,?,?,0) ON CONFLICT(guild_id,user_id) DO UPDATE SET day=excluded.day,month=excluded.month",
            (i.guild.id,i.user.id,day,month))
        await say(i,"🎂 День рождения сохранён! Бот поздравит тебя в выбранный день.")
class BirthdaySetup(discord.ui.Modal,title="Канал поздравлений"):
    channel_id=discord.ui.TextInput(label="ID канала поздравлений")
    async def on_submit(self,i):
        ch=i.guild.get_channel(num(self.channel_id.value))
        if not isinstance(ch,discord.TextChannel):return await say(i,"Канал не найден.")
        set_setting(i.guild.id,"birthday_channel_id",ch.id);await say(i,f"Поздравления будут в {ch.mention}.")
class BirthdayPanel(discord.ui.View):
    def __init__(self):super().__init__(timeout=180)
    @discord.ui.button(label="Установить мой день рождения",emoji="🎂",style=discord.ButtonStyle.success)
    async def set_mine(self,i,b):await i.response.send_modal(BirthdaySet())
    @discord.ui.button(label="Настроить канал поздравлений",emoji="⚙️",style=discord.ButtonStyle.primary)
    async def setup(self,i,b):
        if not (i.user.guild_permissions.manage_guild or i.user.guild_permissions.administrator):return await say(i,"Настраивать канал может только администрация.")
        await i.response.send_modal(BirthdaySetup())

class AutomodPanel(discord.ui.View):
    def __init__(self):super().__init__(timeout=180)
    @discord.ui.button(label="Антиспам: вкл/выкл",emoji="🚫",style=discord.ButtonStyle.primary)
    async def spam(self,i,b):
        old=setting(i.guild.id,"automod_spam","0")=="1";set_setting(i.guild.id,"automod_spam","0" if old else "1")
        await say(i,"Антиспам выключен." if old else "Антиспам включён: 6 сообщений за 8 секунд вызовут удаление и тайм-аут.")
    @discord.ui.button(label="Фильтр ссылок: вкл/выкл",emoji="🔗",style=discord.ButtonStyle.primary)
    async def links(self,i,b):
        old=setting(i.guild.id,"automod_links","0")=="1";set_setting(i.guild.id,"automod_links","0" if old else "1")
        await say(i,"Фильтр ссылок выключен." if old else "Фильтр ссылок включён: ссылки будут удаляться у участников без права управления сообщениями.")
    @discord.ui.button(label="Статус автомодерации",emoji="📋")
    async def status(self,i,b):
        await say(i,f"Антиспам: **{'включён' if setting(i.guild.id,'automod_spam','0')=='1' else 'выключен'}**\\nФильтр ссылок: **{'включён' if setting(i.guild.id,'automod_links','0')=='1' else 'выключен'}**")



# ---------- Role requests with screenshot proof ----------

class RoleRequestChannelsModal(discord.ui.Modal, title="Каналы заявок на роли"):
    panel_channel_id = discord.ui.TextInput(label="ID канала публичной панели")
    category_id = discord.ui.TextInput(label="ID категории приватных заявок")
    reviewer_role_id = discord.ui.TextInput(label="ID роли администрации / проверяющих")

    async def on_submit(self, i):
        panel = i.guild.get_channel(num(self.panel_channel_id.value))
        category = i.guild.get_channel(num(self.category_id.value))
        reviewer = i.guild.get_role(num(self.reviewer_role_id.value))
        if not isinstance(panel, discord.TextChannel):
            return await say(i, "Не найден текстовый канал публичной панели.")
        if not isinstance(category, discord.CategoryChannel):
            return await say(i, "Не найдена категория для приватных заявок.")
        if not reviewer:
            return await say(i, "Не найдена роль администрации / проверяющих.")
        set_setting(i.guild.id, "role_request_panel_channel_id", panel.id)
        set_setting(i.guild.id, "role_request_category_id", category.id)
        set_setting(i.guild.id, "role_request_reviewer_role_id", reviewer.id)
        await say(i, f"✅ Каналы настроены. Панель: {panel.mention}; категория заявок: **{category.name}**; проверяющие: {reviewer.mention}.")

class RoleRequestRolesModal(discord.ui.Modal, title="Роли для системы заявок"):
    newbie_role_id = discord.ui.TextInput(label="ID роли «Новичок»")
    viewer_role_id = discord.ui.TextInput(label="ID роли «Зритель»")
    pending_role_id = discord.ui.TextInput(label="ID роли «На проверке» (необязательно)", required=False)

    async def on_submit(self, i):
        newbie = i.guild.get_role(num(self.newbie_role_id.value))
        viewer = i.guild.get_role(num(self.viewer_role_id.value))
        pending_id = num(self.pending_role_id.value)
        pending = i.guild.get_role(pending_id) if pending_id else None
        if not newbie or not viewer:
            return await say(i, "Не найдена роль «Новичок» или «Зритель». Проверь ID.")
        if pending_id and not pending:
            return await say(i, "Не найдена роль «На проверке». Проверь ID или оставь поле пустым.")
        set_setting(i.guild.id, "role_request_newbie_role_id", newbie.id)
        set_setting(i.guild.id, "role_request_viewer_role_id", viewer.id)
        set_setting(i.guild.id, "role_request_pending_role_id", pending.id if pending else 0)
        await say(i, f"✅ Роли сохранены: {newbie.mention}, {viewer.mention}" +
                  (f", {pending.mention}." if pending else ". Роль ожидания не используется."))

class RoleRequestAdminPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Настроить каналы", emoji="🗂️", style=discord.ButtonStyle.primary)
    async def channels(self, i, b):
        await i.response.send_modal(RoleRequestChannelsModal())

    @discord.ui.button(label="Настроить роли", emoji="🎭", style=discord.ButtonStyle.primary)
    async def roles(self, i, b):
        await i.response.send_modal(RoleRequestRolesModal())

    @discord.ui.button(label="Опубликовать панель", emoji="📢", style=discord.ButtonStyle.success)
    async def publish(self, i, b):
        ch = i.guild.get_channel(num(setting(i.guild.id, "role_request_panel_channel_id")))
        if not isinstance(ch, discord.TextChannel):
            return await say(i, "Сначала настрой каналы.")
        newbie = i.guild.get_role(num(setting(i.guild.id, "role_request_newbie_role_id")))
        viewer = i.guild.get_role(num(setting(i.guild.id, "role_request_viewer_role_id")))
        if not newbie or not viewer:
            return await say(i, "Сначала настрой роли.")
        view = RoleRequestPublicView()
        panel = discord.Embed(
            title="🎭  ПОЛУЧЕНИЕ РОЛЕЙ",
            description=(
                "Добро пожаловать в **TomsyYouTube**!\\n"
                "Выбери подходящую роль сообщества с помощью кнопок ниже.\\n\\n"
                "━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.from_rgb(145, 70, 255),
            timestamp=datetime.now(timezone.utc)
        )
        panel.set_author(
            name="TOMSY YOUTUBE  •  СООБЩЕСТВО",
            icon_url=bot.user.display_avatar.url if bot.user else discord.Embed.Empty
        )
        if i.guild.icon:
            panel.set_thumbnail(url=i.guild.icon.url)
        if i.guild.banner:
            panel.set_image(url=i.guild.banner.url)
        panel.add_field(
            name="🆕  НОВИЧОК",
            value=(
                "Для новых участников сервера.\\n"
                "**Нажми кнопку — роль выдастся сразу.**"
            ),
            inline=True
        )
        panel.add_field(
            name="🔴  YOUTUBE",
            value=(
                "Подписан на YouTube?\\n"
                "Отправь скриншот подписки в приватную заявку."
            ),
            inline=True
        )
        panel.add_field(
            name="🟣  TWITCH",
            value=(
                "Подписан на Twitch?\\n"
                "Отправь скриншот подписки в приватную заявку."
            ),
            inline=True
        )
        panel.add_field(
            name="📋  КАК ПОЛУЧИТЬ РОЛЬ ЗРИТЕЛЯ",
            value=(
                "1. Выбери YouTube или Twitch.\\n"
                "2. Бот откроет личный канал заявки.\\n"
                "3. Прикрепи скриншот, где видна подписка.\\n"
                "4. Дождись проверки администрации.\\n\\n"
                "✅ После одобрения бот выдаст роль **Зритель**."
            ),
            inline=False
        )
        panel.set_footer(
            text="TOMSY YOUTUBE  •  Спасибо, что поддерживаешь канал",
            icon_url=bot.user.display_avatar.url if bot.user else discord.Embed.Empty
        )
        msg = await ch.send(embed=panel, view=view)
        bot.add_view(view, message_id=msg.id)
        await say(i, f"✅ Красивая панель запросов ролей опубликована в {ch.mention}.")

    @discord.ui.button(label="Показать настройки", emoji="📋", style=discord.ButtonStyle.secondary)
    async def settings(self, i, b):
        keys = [
            ("Канал панели", "role_request_panel_channel_id"),
            ("Категория заявок", "role_request_category_id"),
            ("Роль проверяющих", "role_request_reviewer_role_id"),
            ("Роль новичка", "role_request_newbie_role_id"),
            ("Роль зрителя", "role_request_viewer_role_id"),
            ("Роль на проверке", "role_request_pending_role_id"),
        ]
        body = "\n".join(f"**{label}:** `{setting(i.guild.id, key) or 'не настроено'}`" for label, key in keys)
        await say(i, emb=embed("⚙️ НАСТРОЙКИ ЗАЯВОК НА РОЛИ", body))

async def create_viewer_role_request(interaction, platform):
    guild = interaction.guild
    viewer_role = guild.get_role(num(setting(guild.id, "role_request_viewer_role_id")))
    category = guild.get_channel(num(setting(guild.id, "role_request_category_id")))
    reviewer_role = guild.get_role(num(setting(guild.id, "role_request_reviewer_role_id")))
    if not viewer_role or not isinstance(category, discord.CategoryChannel) or not reviewer_role:
        return await say(interaction, "Система заявок ещё не настроена. Обратись к администрации.")
    if viewer_role in interaction.user.roles:
        return await say(interaction, "У тебя уже есть роль «Зритель».")
    existing = one(
        "SELECT channel_id,status FROM role_requests WHERE guild_id=? AND user_id=? AND status IN ('awaiting_proof','pending_review')",
        (guild.id, interaction.user.id)
    )
    if existing:
        old_channel = guild.get_channel(existing["channel_id"])
        if old_channel:
            return await say(interaction, f"У тебя уже есть незавершённая заявка: {old_channel.mention}")
        run("UPDATE role_requests SET status='cancelled' WHERE channel_id=?", (existing["channel_id"],))

    safe = re.sub(r"[^a-zA-Zа-яА-Я0-9_-]+", "-", interaction.user.display_name).strip("-")[:45] or "участник"
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True
        ),
        reviewer_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True
        ),
    }
    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_channels=True, manage_messages=True, attach_files=True, embed_links=True
        )
    try:
        channel = await guild.create_text_channel(
            name=f"заявка-зритель-{safe}"[:95],
            category=category,
            overwrites=overwrites,
            reason=f"Заявка на роль зрителя от {interaction.user}"
        )
    except discord.Forbidden:
        return await say(interaction, "Боту не хватает прав Manage Channels. Проверь разрешения.")
    except discord.HTTPException as e:
        return await say(interaction, f"Не удалось создать канал заявки: {e}")

    run(
        "INSERT INTO role_requests(guild_id,channel_id,user_id,role_id,platform,status,created_at) VALUES(?,?,?,?,?,'awaiting_proof',?)",
        (guild.id, channel.id, interaction.user.id, viewer_role.id, platform, int(time.time()))
    )
    pending_role = guild.get_role(num(setting(guild.id, "role_request_pending_role_id")))
    if pending_role:
        try:
            await interaction.user.add_roles(pending_role, reason="Ожидание проверки заявки на роль зрителя")
        except discord.Forbidden:
            await log_to(guild, "⚠️ Не удалось выдать временную роль «На проверке». Проверь иерархию ролей.")

    await channel.send(
        content=f"{interaction.user.mention} {reviewer_role.mention}",
        embed=embed(
            "📸 ПОДТВЕРЖДЕНИЕ ПОДПИСКИ",
            f"Ты запросил роль **Зритель** через **{platform}**.\n\n"
            "Прикрепи сюда **скриншот**, на котором видно, что ты подписан на канал.\n"
            "Отправь изображение обычным сообщением. После загрузки скриншота заявка попадёт администрации на проверку.\n\n"
            "Не отправляй пароли, коды входа или личные данные."
        ),
        allowed_mentions=discord.AllowedMentions(users=True, roles=True)
    )
    await say(interaction, f"✅ Заявка создана: {channel.mention}. Прикрепи скриншот подписки.")

class RoleRequestPublicView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Новичок", emoji="🆕",
                       style=discord.ButtonStyle.success, custom_id="role_request:newbie")
    async def newbie(self, i, b):
        role = i.guild.get_role(num(setting(i.guild.id, "role_request_newbie_role_id")))
        if not role:
            return await say(i, "Роль «Новичок» ещё не настроена. Обратись к администрации.")
        if role in i.user.roles:
            return await say(i, "У тебя уже есть роль «Новичок».")
        try:
            await i.user.add_roles(role, reason="Самостоятельный запрос роли «Новичок»")
            await say(i, f"✅ Тебе выдана роль {role.mention}.")
            await log_to(i.guild, f"🆕 {i.user.mention} получил роль {role.mention}.")
        except discord.Forbidden:
            await say(i, "Бот не может выдать эту роль. Администрации нужно поднять роль бота выше роли «Новичок».")

    @discord.ui.button(label="Зритель YouTube", emoji="🔴",
                       style=discord.ButtonStyle.danger, custom_id="role_request:viewer_youtube")
    async def youtube(self, i, b):
        await create_viewer_role_request(i, "YouTube")

    @discord.ui.button(label="Зритель Twitch", emoji="🟣",
                       style=discord.ButtonStyle.primary, custom_id="role_request:viewer_twitch")
    async def twitch(self, i, b):
        await create_viewer_role_request(i, "Twitch")

class RoleRequestReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def decide(self, i, approved):
        row = one("SELECT * FROM role_requests WHERE review_message_id=?", (i.message.id,))
        if not row or row["status"] != "pending_review":
            return await say(i, "Эта заявка уже рассмотрена или ожидает скриншот.")

        reviewer_role = i.guild.get_role(num(setting(i.guild.id, "role_request_reviewer_role_id")))
        if not (
            i.user.guild_permissions.administrator
            or i.user.guild_permissions.manage_guild
            or (reviewer_role and reviewer_role in i.user.roles)
        ):
            return await say(i, "⛔ У тебя нет прав на рассмотрение заявок.")

        member = i.guild.get_member(row["user_id"])
        if member is None:
            try:
                member = await i.guild.fetch_member(row["user_id"])
            except (discord.NotFound, discord.HTTPException):
                member = None

        target_role = i.guild.get_role(row["role_id"])
        pending_role = i.guild.get_role(num(setting(i.guild.id, "role_request_pending_role_id")))

        if approved:
            if not member:
                return await say(i, "Участник покинул сервер. Заявку нельзя одобрить.")
            if not target_role:
                return await say(i, "Роль «Зритель» была удалена. Настрой роль заново.")
            try:
                await member.add_roles(target_role, reason=f"Заявка одобрена: {row['platform']}")
                if pending_role and pending_role in member.roles:
                    await member.remove_roles(pending_role, reason="Заявка на роль рассмотрена")
            except discord.Forbidden:
                return await say(i, "Боту не хватает прав на выдачу роли. Подними роль бота выше «Зрителя» и «На проверке».")
            status = "approved"
            decision_label = "ОДОБРЕНА"
            decision_color = discord.Color.from_rgb(46, 204, 113)
            dm_description = (
                "Отличные новости! Твоя заявка успешно прошла проверку.\\n"
                f"Тебе выдана роль {target_role.mention}.\\n\\n"
                "Спасибо, что смотришь и поддерживаешь наше сообщество! 💜"
            )
            staff_text = f"✅ Заявка одобрена. Участнику выдана роль {target_role.mention}."
        else:
            if member and pending_role and pending_role in member.roles:
                try:
                    await member.remove_roles(pending_role, reason="Заявка на роль отклонена")
                except discord.HTTPException:
                    pass
            status = "rejected"
            decision_label = "ОТКЛОНЕНА"
            decision_color = discord.Color.from_rgb(231, 76, 60)
            dm_description = (
                "К сожалению, администрация не смогла одобрить твою заявку.\\n\\n"
                "Проверь, что на скриншоте хорошо видна подписка на нужный канал. "
                "Если считаешь, что произошла ошибка, обратись к администрации сообщества."
            )
            staff_text = "❌ Заявка отклонена. Временная роль «На проверке» снята."

        run("UPDATE role_requests SET status=? WHERE id=?", (status, row["id"]))

        # Beautiful personal notification with the name of the moderator who made the decision.
        dm_sent = False
        if member:
            dm_embed = discord.Embed(
                title=("🎉  ЗАЯВКА ОДОБРЕНА" if approved else "📩  ЗАЯВКА ОТКЛОНЕНА"),
                description=dm_description,
                color=decision_color,
                timestamp=datetime.now(timezone.utc)
            )
            dm_embed.add_field(name="🌐 Платформа", value=row["platform"], inline=True)
            dm_embed.add_field(name="📌 Решение", value=decision_label, inline=True)
            dm_embed.add_field(
                name="🛡️ Рассмотрел",
                value=f"{i.user.mention}\\n**{discord.utils.escape_markdown(i.user.display_name)}**",
                inline=False
            )
            dm_embed.set_author(
                name=f"{i.guild.name} • Проверка заявок",
                icon_url=i.guild.icon.url if i.guild.icon else discord.Embed.Empty
            )
            dm_embed.set_footer(
                text="TomsyYouTube • Система заявок",
                icon_url=bot.user.display_avatar.url if bot.user else discord.Embed.Empty
            )
            try:
                await member.send(embed=dm_embed)
                dm_sent = True
            except discord.Forbidden:
                dm_sent = False
            except discord.HTTPException:
                dm_sent = False

        # Update the review card before removing the private ticket channel.
        try:
            review_embed = i.message.embeds[0]
            review_embed.add_field(
                name=f"{'✅' if approved else '❌'} Решение администрации",
                value=f"**{decision_label}**\\nРассмотрел: {i.user.mention}",
                inline=False
            )
            await i.message.edit(embed=review_embed, view=None)
        except (discord.HTTPException, IndexError):
            pass

        await log_to(
            i.guild,
            f"🎭 Заявка на роль зрителя от <@{row['user_id']}>: "
            f"**{decision_label}**. Рассмотрел: {i.user.mention}. "
            f"Личное сообщение: {'отправлено' if dm_sent else 'не доставлено (возможно, закрыты ЛС)'}."
        )

        # Acknowledge the button interaction before deleting its channel.
        dm_status = "Личное сообщение отправлено участнику." if dm_sent else "Не удалось отправить ЛС участнику (возможно, закрыты личные сообщения)."
        await say(i, f"✅ Заявка {decision_label.lower()}. {dm_status} Канал заявки будет удалён через несколько секунд.")

        channel = i.guild.get_channel(row["channel_id"])
        if channel:
            try:
                await channel.send(
                    embed=embed(
                        "🗑️ ЗАЯВКА ЗАВЕРШЕНА",
                        f"Решение: **{decision_label}**\\nРассмотрел: {i.user.mention}\\n"
                        "Этот приватный канал будет автоматически удалён через 5 секунд.",
                        decision_color
                    )
                )
                await asyncio.sleep(5)
                await channel.delete(reason=f"Заявка {decision_label.lower()}; рассмотрел {i.user}")
            except discord.NotFound:
                pass
            except discord.Forbidden:
                await log_to(i.guild, f"⚠️ Не удалось удалить канал заявки <#{row['channel_id']}>. Проверь право Manage Channels.")
            except discord.HTTPException:
                await log_to(i.guild, f"⚠️ Discord не позволил удалить канал заявки <#{row['channel_id']}>.")

    @discord.ui.button(label="Одобрить", emoji="✅", style=discord.ButtonStyle.success,
                       custom_id="role_request:approve")
    async def approve(self, i, b):
        await self.decide(i, True)

    @discord.ui.button(label="Отклонить", emoji="❌", style=discord.ButtonStyle.danger,
                       custom_id="role_request:reject")
    async def reject(self, i, b):
        await self.decide(i, False)

async def handle_role_request_proof(message):
    if not message.guild or message.author.bot:
        return
    row = one(
        "SELECT * FROM role_requests WHERE channel_id=? AND user_id=? AND status='awaiting_proof'",
        (message.channel.id, message.author.id)
    )
    if not row:
        return
    image_attachments = []
    for attachment in message.attachments:
        content_type = (attachment.content_type or "").lower()
        filename = attachment.filename.lower()
        if content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            image_attachments.append(attachment)
    if not image_attachments:
        if message.attachments:
            await message.reply("Нужен именно скриншот в формате PNG, JPG или WEBP. Отправь изображение файлом.")
        return

    attachment = image_attachments[0]
    viewer_role = message.guild.get_role(row["role_id"])
    e = embed(
        "🔎 ЗАЯВКА НА РОЛЬ «ЗРИТЕЛЬ»",
        f"**Участник:** {message.author.mention}\n"
        f"**Платформа:** {row['platform']}\n"
        f"**Запрошенная роль:** {viewer_role.mention if viewer_role else 'роль удалена'}\n"
        f"**Доказательство:** скриншот прикреплён ниже.\n\n"
        "Проверь, что на изображении видно подписку на нужный канал."
    )
    e.set_image(url=attachment.url)
    review_message = await message.channel.send(embed=e, view=RoleRequestReviewView())
    run(
        "UPDATE role_requests SET status='pending_review',review_message_id=? WHERE id=? AND status='awaiting_proof'",
        (review_message.id, row["id"])
    )
    await message.reply("✅ Скриншот получен и отправлен администрации на проверку. Ожидай решения.")


# ---------- Bot, events and background jobs ----------

intents=discord.Intents.default()
intents.members=True
intents.message_content=True
intents.voice_states=True
intents.guilds=True

class CreatorBot(commands.Bot):
    async def setup_hook(self):
        init_db(); init_extra_db()
        for view in [TicketPanel(),TicketClose(),RoomControls(),GiveawayEntry(),ApplicationApply(),ApplicationReview(),SuggestionCreate(),SuggestionVote(),ClipSubmit(),RoleRequestPublicView(),RoleRequestReviewView()]:
            self.add_view(view)
        for row in many("SELECT guild_id,message_id,role_ids FROM role_panels"):
            g=self.get_guild(row["guild_id"])
            if g:
                try:self.add_view(RoleSelectView(row["guild_id"],json.loads(row["role_ids"]),g),message_id=row["message_id"])
                except Exception:log.exception("Не удалось восстановить панель ролей")
        self.youtube_task.start();self.twitch_task.start();self.giveaway_task.start(); reminders_loop.start(); birthday_loop.start()
        if GUILD_ID:
            obj=discord.Object(id=GUILD_ID);self.tree.copy_global_to(guild=obj);await self.tree.sync(guild=obj)
        else:await self.tree.sync()
    async def on_ready(self):
        log.info("Запущен как %s (%s)",self.user,self.user.id)
        if not getattr(self,"_role_views_restored",False):
            for row in many("SELECT guild_id,message_id,role_ids FROM role_panels"):
                g=self.get_guild(row["guild_id"])
                if g:
                    try:self.add_view(RoleSelectView(row["guild_id"],json.loads(row["role_ids"]),g),message_id=row["message_id"])
                    except Exception:log.exception("Не удалось восстановить панель ролей")
            self._role_views_restored=True
    async def on_member_join(self,member):
        if setting(member.guild.id,"welcome_enabled","1")=="1":
            ch=member.guild.get_channel(num(setting(member.guild.id,"welcome_channel_id")))
            if isinstance(ch,discord.TextChannel):
                try:await send_welcome(ch,member)
                except discord.HTTPException:log.exception("Не отправилось приветствие")
        role=member.guild.get_role(num(setting(member.guild.id,"welcome_role_id")))
        if role:
            try:await member.add_roles(role,reason="Автоматическая роль новичка")
            except discord.Forbidden:await log_to(member.guild,"Не удалось выдать роль новичка. Проверь иерархию ролей.")
        await log_to(member.guild,f"👋 Новый участник: {member.mention}.")
    async def on_member_remove(self,member):await log_to(member.guild,f"🚪 Сервер покинул **{member}**.")
    async def on_member_update(self,before,after):
        if before.roles!=after.roles:
            add=[r.mention for r in after.roles if r not in before.roles];rem=[r.mention for r in before.roles if r not in after.roles]
            await log_to(after.guild,f"🎭 Изменение ролей у {after.mention}\\nДобавлены: {', '.join(add) or 'нет'}\\nСняты: {', '.join(rem) or 'нет'}")
    async def on_message_delete(self,msg):
        if msg.guild and not msg.author.bot:await log_to(msg.guild,f"🗑️ Удалено сообщение {msg.author.mention} в {msg.channel.mention}: {(msg.content or '[без текста]')[:700]}")
    async def on_voice_state_update(self,member,before,after):
        if member.bot:return
        trigger=num(setting(member.guild.id,"voice_trigger_id"))
        if after.channel and after.channel.id==trigger and (not before.channel or before.channel.id!=trigger):
            try:await make_room(member)
            except discord.Forbidden:await log_to(member.guild,"⛔ Не удалось создать комнату: нужны Manage Channels и Move Members.")
            except discord.HTTPException as e:await log_to(member.guild,f"Ошибка создания комнаты: {e}")
        if before.channel:
            row=one("SELECT * FROM voice_rooms WHERE voice_id=?",(before.channel.id,))
            if row and len(before.channel.members)==0:
                text=member.guild.get_channel(row["text_id"]);run("DELETE FROM voice_rooms WHERE voice_id=?",(before.channel.id,))
                try:
                    if text:await text.delete(reason="Временная комната пуста")
                    await before.channel.delete(reason="Временная комната пуста")
                except discord.HTTPException:pass
    async def on_message(self,msg):
        if msg.guild and not msg.author.bot:
            await handle_role_request_proof(msg)
            r=level_row(msg.guild.id,msg.author.id);now=int(time.time());xp=r["xp"];level=r["level"];coins=r["coins"];last=r["last_xp"]
            if now-last>=60:
                xp+=random.randint(10,20);coins+=1
                while xp>=100+level*50:xp-=100+level*50;level+=1
            run("UPDATE levels SET xp=?,level=?,coins=?,messages=messages+1,last_xp=? WHERE guild_id=? AND user_id=?",
                (xp,level,coins,now if now-last>=60 else last,msg.guild.id,msg.author.id))
        await self.process_commands(msg)
    @tasks.loop(seconds=180)
    async def youtube_task(self):
        rows=many("SELECT * FROM youtube_feeds")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            for r in rows:
                try:
                    async with session.get(r["feed_url"]) as resp:
                        if resp.status!=200:continue
                        root=ET.fromstring(await resp.text())
                    ns={"a":"http://www.w3.org/2005/Atom","yt":"http://www.youtube.com/xml/schemas/2015"}
                    entry=root.find("a:entry",ns)
                    if entry is None:continue
                    vid=entry.findtext("yt:videoId",default="",namespaces=ns);title=entry.findtext("a:title",default="Новое видео",namespaces=ns)
                    link=f"https://www.youtube.com/watch?v={vid}"
                    if not vid:continue
                    if not r["last_video_id"]:run("UPDATE youtube_feeds SET last_video_id=? WHERE id=?",(vid,r["id"]));continue
                    if vid!=r["last_video_id"]:
                        g=self.get_guild(r["guild_id"]);ch=g.get_channel(r["channel_id"]) if g else None
                        if ch:await ch.send(embed=embed("🔴 НОВОЕ ВИДЕО НА YOUTUBE",f"**{title}**\\n{link}",discord.Color.red()))
                        run("UPDATE youtube_feeds SET last_video_id=? WHERE id=?",(vid,r["id"]))
                except Exception as e:log.warning("YouTube feed: %s",e)
    @youtube_task.before_loop
    async def before_youtube(self):await self.wait_until_ready()
    @tasks.loop(seconds=60)
    async def twitch_task(self):
        rows=many("SELECT * FROM twitch_channels")
        if not rows or not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:return
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.post("https://id.twitch.tv/oauth2/token",params={"client_id":TWITCH_CLIENT_ID,"client_secret":TWITCH_CLIENT_SECRET,"grant_type":"client_credentials"}) as rr:token=(await rr.json()).get("access_token")
                if not token:return
                headers={"Client-ID":TWITCH_CLIENT_ID,"Authorization":f"Bearer {token}"}
                for r in rows:
                    async with s.get("https://api.twitch.tv/helix/streams",params={"user_login":r["username"]},headers=headers) as rr:data=await rr.json()
                    streams=data.get("data",[])
                    if not streams:continue
                    stream=streams[0];sid=stream.get("id")
                    if sid and sid!=r["last_stream_id"]:
                        g=self.get_guild(r["guild_id"]);ch=g.get_channel(r["channel_id"]) if g else None
                        if ch:
                            url=f"https://twitch.tv/{r['username']}"
                            await ch.send(embed=embed("🟣 НАЧАЛАСЬ ТРАНСЛЯЦИЯ!",f"**{r['username']}** уже в эфире!\\n**Название:** {stream.get('title','Прямой эфир')}\\n**Категория:** {stream.get('game_name','Twitch')}\\n{url}",discord.Color.from_rgb(145,70,255)))
                        run("UPDATE twitch_channels SET last_stream_id=? WHERE id=?",(sid,r["id"]))
        except Exception as e:log.warning("Twitch API: %s",e)
    @twitch_task.before_loop
    async def before_twitch(self):await self.wait_until_ready()
    @tasks.loop(seconds=30)
    async def giveaway_task(self):
        for r in many("SELECT * FROM giveaways WHERE ended=0 AND end_at<=?",(int(time.time()),)):
            entries=[x["user_id"] for x in many("SELECT user_id FROM giveaway_entries WHERE giveaway_id=?",(r["id"],))]
            winners=random.sample(entries,min(len(entries),r["winners"])) if entries else []
            run("UPDATE giveaways SET ended=1 WHERE id=?",(r["id"],))
            g=self.get_guild(r["guild_id"]);ch=g.get_channel(r["channel_id"]) if g else None
            if ch and r["message_id"]:
                try:
                    msg=await ch.fetch_message(r["message_id"]);text=", ".join(f"<@{u}>" for u in winners) if winners else "Нет участников."
                    await msg.edit(embed=embed("🏁 РОЗЫГРЫШ ЗАВЕРШЁН",f"Приз: **{r['prize']}**\\nПобедители: {text}",discord.Color.gold()),view=None)
                    if winners:await ch.send(f"🎉 Поздравляем: {text}! Приз: **{r['prize']}**.")
                except discord.HTTPException:pass
    @giveaway_task.before_loop
    async def before_giveaway(self):await self.wait_until_ready()

bot=CreatorBot(command_prefix="!",intents=intents)

# ---------- Slash commands: one command opens one panel ----------

async def panel_cmd(inter, title, view):
    if not await admin(inter):return
    await say(inter, f"**{title}**\\nВсе действия доступны через кнопки панели.", view=view)

@bot.tree.command(name="roles",description="Открыть панель ролей")
async def c_roles(i):await panel_cmd(i,"🎭 Панель ролей",RoleAdminView())
@bot.tree.command(name="role_requests",description="Настроить заявки на роли сообщества")
async def c_role_requests(i):await panel_cmd(i,"🎭 Панель заявок на роли",RoleRequestAdminPanel())
@bot.tree.command(name="welcome",description="Открыть панель приветствий")
async def c_welcome(i):await panel_cmd(i,"👋 Панель приветствий",WelcomeView())
@bot.tree.command(name="rules",description="Открыть панель правил")
async def c_rules(i):await panel_cmd(i,"📜 Панель правил",RulesView())
@bot.tree.command(name="voice",description="Открыть панель голосовых комнат")
async def c_voice(i):await panel_cmd(i,"🎙️ Панель голосовых комнат",VoiceView())
@bot.tree.command(name="tickets",description="Открыть панель тикетов")
async def c_tickets(i):await panel_cmd(i,"🎫 Панель тикетов",TicketAdmin())
@bot.tree.command(name="moderation",description="Открыть панель модерации")
async def c_moderation(i):await panel_cmd(i,"🛡️ Панель модерации",ModPanel())
@bot.tree.command(name="giveaway",description="Открыть панель розыгрышей")
async def c_giveaway(i):await panel_cmd(i,"🎁 Панель розыгрышей",GiveawayPanel())
@bot.tree.command(name="events",description="Открыть панель мероприятий")
async def c_events(i):await panel_cmd(i,"🎉 Панель мероприятий",EventPanel())
@bot.tree.command(name="applications",description="Открыть панель заявок в команду")
async def c_applications(i):await panel_cmd(i,"📝 Панель заявок",ApplicationAdmin())
@bot.tree.command(name="suggestions",description="Открыть панель предложений")
async def c_suggestions(i):await panel_cmd(i,"💡 Панель предложений",SuggestionAdmin())
@bot.tree.command(name="polls",description="Открыть панель опросов")
async def c_polls(i):await panel_cmd(i,"📊 Панель опросов",PollAdmin())
@bot.tree.command(name="levels",description="Открыть уровни и рейтинг")
async def c_levels(i):await say(i,"⭐ Уровни и рейтинг участников",view=LevelsPanel())
@bot.tree.command(name="achievements",description="Открыть панель достижений")
async def c_achievements(i):await say(i,"🏅 Достижения участников",view=AchievementsPanel())
@bot.tree.command(name="shop",description="Открыть магазин ролей")
async def c_shop(i):await say(i,"🛒 Магазин ролей за монеты",view=ShopPanel())
@bot.tree.command(name="games",description="Открыть мини-игры")
async def c_games(i):await say(i,"🎲 Мини-игры сообщества",view=GamesPanel())
@bot.tree.command(name="youtube",description="Открыть панель YouTube")
async def c_youtube(i):await panel_cmd(i,"🔴 Панель YouTube",YouTubePanel())
@bot.tree.command(name="twitch",description="Открыть панель Twitch")
async def c_twitch(i):await panel_cmd(i,"🟣 Панель Twitch",TwitchPanel())
@bot.tree.command(name="logs",description="Открыть панель логов")
async def c_logs(i):await panel_cmd(i,"📜 Панель логов",LogsPanel())
@bot.tree.command(name="statistics",description="Показать статистику сервера")
async def c_statistics(i):
    g=i.guild
    humans=sum(not m.bot for m in g.members);bots=sum(m.bot for m in g.members)
    voice=sum(len(c.members) for c in g.voice_channels)
    await say(i,emb=embed("📊 СТАТИСТИКА",f"👥 Участников: **{humans}**\\n🤖 Ботов: **{bots}**\\n🎙️ В голосовых: **{voice}**\\n📁 Каналов: **{len(g.channels)}**\\n🎭 Ролей: **{len(g.roles)}**"))
@bot.tree.command(name="settings",description="Открыть общие настройки бота")
async def c_settings(i):await panel_cmd(i,"⚙️ Настройки бота",SettingsPanel())


# Extra listeners and scheduled jobs
from collections import defaultdict, deque
_spam = defaultdict(deque)

@bot.listen("on_message")
async def automod_listener(message):
    if not message.guild or message.author.bot:return
    member=message.author
    if member.guild_permissions.manage_messages or member.guild_permissions.administrator:return
    if setting(message.guild.id,"automod_links","0")=="1" and re.search(r"https?://",message.content or "",re.I):
        try:
            await message.delete()
            await message.channel.send(f"{member.mention}, ссылки в этом канале запрещены.",delete_after=5)
            await log_to(message.guild,f"🔗 Удалена ссылка от {member.mention}.")
        except discord.HTTPException:pass
    if setting(message.guild.id,"automod_spam","0")=="1":
        key=(message.guild.id,member.id);now=time.time();q=_spam[key];q.append(now)
        while q and now-q[0]>8:q.popleft()
        if len(q)>=6:
            q.clear()
            try:
                await message.channel.purge(limit=6,check=lambda m:m.author.id==member.id)
                await member.timeout(datetime.now(timezone.utc)+timedelta(seconds=60),reason="Автоматический антиспам")
                await log_to(message.guild,f"🚫 Антиспам: {member.mention} получил тайм-аут на 60 секунд.")
            except discord.HTTPException:pass

@tasks.loop(seconds=30)
async def reminders_loop():
    for r in many("SELECT * FROM reminders WHERE sent=0 AND send_at<=?",(int(time.time()),)):
        g=bot.get_guild(r["guild_id"]);ch=g.get_channel(r["channel_id"]) if g else None
        if ch:
            try:await ch.send(embed=embed("⏰ НАПОМИНАНИЕ",r["message"],discord.Color.orange()))
            except discord.HTTPException:pass
        run("UPDATE reminders SET sent=1 WHERE id=?",(r["id"],))
@reminders_loop.before_loop
async def before_reminders():await bot.wait_until_ready()

@tasks.loop(minutes=30)
async def birthday_loop():
    now=datetime.now(timezone.utc)
    if now.hour not in (9,):return
    for r in many("SELECT * FROM birthdays WHERE day=? AND month=? AND last_year<?",(now.day,now.month,now.year)):
        g=bot.get_guild(r["guild_id"]);ch=g.get_channel(num(setting(r["guild_id"],"birthday_channel_id"))) if g else None
        if ch:
            member=g.get_member(r["user_id"])
            if member:
                try:await ch.send(embed=embed("🎂 С ДНЁМ РОЖДЕНИЯ!",f"Поздравляем {member.mention}! Желаем счастья, здоровья и исполнения желаний! 🎉",discord.Color.gold()))
                except discord.HTTPException:pass
        run("UPDATE birthdays SET last_year=? WHERE guild_id=? AND user_id=?",(now.year,r["guild_id"],r["user_id"]))
@birthday_loop.before_loop
async def before_birthdays():await bot.wait_until_ready()

@bot.tree.command(name="faq",description="Открыть панель часто задаваемых вопросов")
async def c_faq(i):await panel_cmd(i,"❓ Панель FAQ",FAQPanel())
@bot.tree.command(name="clips",description="Открыть панель предложений клипов")
async def c_clips(i):await panel_cmd(i,"🎞️ Панель клипов",ClipPanel())
@bot.tree.command(name="reminders",description="Открыть панель напоминаний")
async def c_reminders(i):await panel_cmd(i,"⏰ Панель напоминаний",ReminderPanel())
@bot.tree.command(name="birthday",description="Открыть панель дней рождения")
async def c_birthday(i):await say(i,"🎂 Панель дней рождения",view=BirthdayPanel())
@bot.tree.command(name="automod",description="Открыть панель автомодерации")
async def c_automod(i):await panel_cmd(i,"🚫 Панель автомодерации",AutomodPanel())

if __name__=="__main__":
    if not TOKEN:raise SystemExit("Токен не найден. На хостинге добавь BOT_TOKEN или DISCORD_TOKEN; локально заполни .env.")
    bot.run(TOKEN)
