import os
import re
import json
import random
import asyncio
import traceback
from datetime import datetime, timezone, timedelta
from threading import Thread
from collections import defaultdict, deque
from io import BytesIO

from openai import OpenAI
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from flask import Flask


# --- web (Render) ---

app = Flask(__name__)


@app.route("/")
def home():
    return "ok"


def keep_alive():
    port = int(os.environ.get("PORT", 10000))
    Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False),
        daemon=True,
    ).start()


# --- config ---

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI-KEY") or os.getenv("AI-KEY")
NIGHT_OWL_KEY = os.getenv("NIGHT-OWL")
APPLICATION_ID = os.getenv("APPLICATION_ID", "1348335637656371330")
MEMORY_CHANNEL_ID = os.getenv("MEMORY_CHANNEL_ID")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
AI_MODEL = os.getenv("AI-MODEL", "gemini-2.5-flash")
OPEN_MOUTH = ":open_mouth:"
SEAHORSE = "<:seahorse:1554464159947817091>"

if not TOKEN:
    raise RuntimeError("Falta DISCORD_TOKEN")
if not GEMINI_KEY:
    raise RuntimeError("Falta GEMINI-KEY")

COLOMBIA = timezone(timedelta(hours=-5))
MEMORY_FILE = "memory.json"
HISTORY_LIMIT = 12

TALK_USER_LIMIT = 12
TALK_GLOBAL_LIMIT = 200
TALK_COOLDOWN = 15

OWNER_NAMES = {
    name.strip().lower()
    for name in os.getenv("OWNER_USERNAMES", "crea1e").split(",")
    if name.strip()
}

SHOP = {
    "cookie": (15, "Cookie"),
    "hat": (50, "Cool Hat"),
    "shield": (80, "Rob Shield"),
    "plush": (100, "Seahorse Plush"),
    "title": (200, "Fancy Title"),
}

TEXTS = {
    False: {
        "lang": "Language set to **English**.",
        "pong": "Pong **{ms}ms**",
        "die_bad": "The die needs at least 2 sides.",
        "die": "Rolled **{n}** (d{sides})",
        "heads": "Heads",
        "tails": "Tails",
        "coin": "Coin: **{side}**",
        "choose_bad": "Give at least two options, separated by commas.",
        "choose": "I pick **{choice}**",
        "ball": "Question: {q}\nAnswer: **{a}**",
    },
    True: {
        "lang": "Idioma cambiado a **Español**.",
        "pong": "Pong **{ms}ms**",
        "die_bad": "El dado necesita al menos 2 caras.",
        "die": "Salió **{n}** (d{sides})",
        "heads": "Cara",
        "tails": "Cruz",
        "coin": "Moneda: **{side}**",
        "choose_bad": "Dame al menos dos opciones, separadas por comas.",
        "choose": "Elijo **{choice}**",
        "ball": "Pregunta: {q}\nRespuesta: **{a}**",
    },
}

BALLS = {
    False: ["Yes.", "No.", "Maybe.", "Ask later.", "Definitely.", "Don't count on it."],
    True: ["Sí.", "No.", "Tal vez.", "Pregunta luego.", "Seguro.", "No cuentes con eso."],
}

FACTS = [
    "Honey never spoils",
    "Octopuses have three hearts",
    "A day on Venus is longer than its year",
    "Bananas are berries",
    "Sharks are older than trees",
]


# --- state ---

ai = OpenAI(api_key=GEMINI_KEY, base_url=GEMINI_BASE)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    allowed_contexts=app_commands.AppCommandContext(
        guild=True, dm_channel=True, private_channel=True
    ),
)

spanish: dict[int, bool] = {}
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
economy: dict[str, dict] = {}
usage = {"day": "", "global": 0, "users": {}, "last": {}}
lock: asyncio.Lock | None = None
last_fact = None


def now():
    return datetime.now(COLOMBIA)


def text(user_id: int, key: str) -> str:
    return TEXTS[spanish.get(user_id, False)][key]


def is_owner(user: discord.abc.User) -> bool:
    return (user.name or "").lower() in OWNER_NAMES


def get_lock() -> asyncio.Lock:
    global lock
    if lock is None:
        lock = asyncio.Lock()
    return lock


# --- memory (one channel for the whole bot, not one per user) ---

def snapshot() -> dict:
    return {
        "spanish": {str(k): v for k, v in spanish.items()},
        "history": {str(k): list(v) for k, v in history.items()},
        "economy": economy,
        "usage": usage,
    }


def load_snapshot(data: dict) -> None:
    if not isinstance(data, dict):
        return
    for key, value in (data.get("spanish") or {}).items():
        try:
            spanish[int(key)] = bool(value)
        except ValueError:
            continue
    for key, rows in (data.get("history") or {}).items():
        try:
            channel_id = int(key)
        except ValueError:
            continue
        clean = [
            {"user": str(row.get("user", "?")), "prompt": str(row.get("prompt", ""))[:160]}
            for row in rows
            if isinstance(row, dict) and row.get("prompt")
        ]
        history[channel_id] = deque(clean, maxlen=HISTORY_LIMIT)
    for key, row in (data.get("economy") or {}).items():
        if isinstance(row, dict):
            economy[str(key)] = {
                "coins": int(row.get("coins", 0) or 0),
                "work": float(row.get("work", 0) or 0),
                "rob": float(row.get("rob", 0) or 0),
                "shield": int(row.get("shield", 0) or 0),
                "inv": [str(item) for item in (row.get("inv") or [])][:30],
            }
    saved = data.get("usage")
    if isinstance(saved, dict):
        usage["day"] = str(saved.get("day", ""))
        usage["global"] = int(saved.get("global", 0) or 0)
        usage["users"] = {str(k): int(v) for k, v in (saved.get("users") or {}).items()}
        usage["last"] = {str(k): float(v) for k, v in (saved.get("last") or {}).items()}


def load_file() -> None:
    if not os.path.isfile(MEMORY_FILE):
        return
    try:
        with open(MEMORY_FILE, encoding="utf-8") as handle:
            load_snapshot(json.load(handle))
    except Exception as error:
        print("memory file:", error)


def dump_file() -> None:
    temp = MEMORY_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(snapshot(), handle, ensure_ascii=False)
    os.replace(temp, MEMORY_FILE)


def memory_channel_id() -> int | None:
    if not MEMORY_CHANNEL_ID:
        return None
    try:
        return int(MEMORY_CHANNEL_ID)
    except ValueError:
        print("MEMORY_CHANNEL_ID tiene que ser solo el numero del canal")
        return None


async def load_discord_memory() -> None:
    channel_id = memory_channel_id()
    if channel_id is None:
        return
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    async for message in channel.history(limit=20):
        if message.author.id != bot.user.id:
            continue
        file = next((item for item in message.attachments if item.filename == "memory.json"), None)
        if file is None:
            continue
        load_snapshot(json.loads((await file.read()).decode("utf-8")))
        print("memory loaded from discord")
        return


async def save_discord_memory() -> None:
    channel_id = memory_channel_id()
    if channel_id is None:
        return
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    raw = json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")
    await channel.send(file=discord.File(BytesIO(raw), filename="memory.json"))
    old = []
    async for message in channel.history(limit=20):
        if message.author.id == bot.user.id and any(
            item.filename == "memory.json" for item in message.attachments
        ):
            old.append(message)
    for message in old[1:]:
        try:
            await message.delete()
        except Exception:
            break


async def save_memory() -> None:
    async with get_lock():
        try:
            await asyncio.to_thread(dump_file)
        except Exception as error:
            print("save file:", error)
        try:
            await save_discord_memory()
        except Exception as error:
            print("save discord:", error)


load_file()


# --- talk limits ---

def reset_usage() -> None:
    day = now().strftime("%Y-%m-%d")
    if usage["day"] != day:
        usage["day"] = day
        usage["global"] = 0
        usage["users"] = {}
        usage["last"] = {}


def talk_blocked(user: discord.abc.User) -> str | None:
    reset_usage()
    if is_owner(user):
        return None
    key = str(user.id)
    last = usage["last"].get(key)
    if last is not None:
        wait = TALK_COOLDOWN - (now().timestamp() - float(last))
        if wait > 0:
            return f"Espera **{max(1, int(wait))}s**."
    if int(usage["users"].get(key, 0)) >= TALK_USER_LIMIT:
        return f"Limite de hoy: **{TALK_USER_LIMIT}**. Se reinicia a las 00:00 UTC-5."
    if int(usage["global"]) >= TALK_GLOBAL_LIMIT:
        return "El bot llego al limite global de IA de hoy."
    return None


def use_talk(user: discord.abc.User) -> None:
    reset_usage()
    if is_owner(user):
        return
    key = str(user.id)
    usage["users"][key] = int(usage["users"].get(key, 0)) + 1
    usage["global"] = int(usage["global"]) + 1
    usage["last"][key] = now().timestamp()


def ai_hours_open() -> bool:
    return 6 <= now().hour < 21


def ai_client(night: bool) -> tuple[OpenAI | None, str | None]:
    if ai_hours_open():
        return ai, None
    if not night:
        return None, "La IA esta apagada. Horario: **06:00–21:00 UTC-5**."
    return OpenAI(api_key=NIGHT_OWL_KEY or GEMINI_KEY, base_url=GEMINI_BASE), None


def strip_flags(prompt: str) -> tuple[bool, str]:
    text = prompt.lstrip()
    night = False
    while True:
        match = re.match(r"\[night[-_]?owl\s*=\s*on\]\s*", text, flags=re.I)
        if not match:
            break
        night = True
        text = text[match.end():]
    return night, text.strip()


def answer_text(completion) -> str:
    try:
        content = completion.choices[0].message.content
    except Exception:
        return ""
    return content.strip() if isinstance(content, str) else ""


def close_fences(text: str) -> str:
    if text.count("```") % 2 == 1:
        text = text.rstrip() + "\n```"
    return text.strip()


async def ask_ai(channel_id: int, username: str, prompt: str, client: OpenAI) -> tuple[str, str]:
    recent = "\n".join(
        f"- {row['user']}: {row['prompt']}" for row in list(history[channel_id])[-8:]
    ) or "none"
    creator = "This user is crea1e, the bot owner." if username.lower() == "crea1e" else ""
    system = f"""You are a Discord bot. Be clear and casual.
User: {username}
{creator}
Seahorse emoji, use it exactly if they ask for one: {SEAHORSE}
Recent prompts in this chat:
{recent}
Always answer with visible text. If you write code, close the ``` fence.
"""
    started = asyncio.get_event_loop().time()
    completion = await asyncio.to_thread(
        client.chat.completions.create,
        model=AI_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        max_tokens=900,
    )
    answer = answer_text(completion)
    if not answer:
        completion = await asyncio.to_thread(
            client.chat.completions.create,
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt + "\nAnswer now in one complete message."},
            ],
            max_tokens=900,
        )
        answer = answer_text(completion)
    answer = close_fences(answer) or "No pude completar la respuesta. Intenta de nuevo."
    seconds = max(1, round(asyncio.get_event_loop().time() - started))
    history[channel_id].append({"user": username, "prompt": prompt[:160]})
    await save_memory()
    return f'**"Ö" ahh bot thought for {seconds} seconds**', answer


async def send_answer(send, edit, prompt: str, thought: str, answer: str) -> None:
    body = f"-# {prompt}\n{thought}\n{answer}"
    first = edit or send
    if len(body) <= 2000:
        await first(body)
        return
    await first(f"-# {prompt}\n{thought}")
    for start in range(0, len(answer), 1900):
        await send(answer[start:start + 1900])


# --- economy ---

def wallet(user_id: int) -> dict:
    row = economy.get(str(user_id))
    if row is None:
        row = {"coins": 40, "work": 0.0, "rob": 0.0, "shield": 0, "inv": []}
        economy[str(user_id)] = row
    return row


def wait_left(last: float, seconds: int) -> int:
    return max(0, int(seconds - (now().timestamp() - float(last or 0))))


# --- events ---

@tasks.loop(minutes=8)
async def rotate_status():
    global last_fact
    choices = [fact for fact in FACTS if fact != last_fact] or FACTS
    last_fact = random.choice(choices)
    await bot.change_presence(activity=discord.CustomActivity(name=last_fact))


@rotate_status.before_loop
async def before_status():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print(f"online as {bot.user} ({bot.user.id})")
    if not rotate_status.is_running():
        rotate_status.start()
    try:
        emojis = await bot.fetch_application_emojis()
        global OPEN_MOUTH, SEAHORSE
        for emoji in emojis:
            if emoji.name.lower() == "open_mouth":
                OPEN_MOUTH = str(emoji)
            elif emoji.name.lower() == "seahorse":
                SEAHORSE = str(emoji)
    except Exception as error:
        print("emojis:", error)
    try:
        await load_discord_memory()
    except Exception as error:
        print("load memory:", error)
    try:
        synced = await bot.tree.sync()
        print(f"commands: {len(synced)}")
    except Exception as error:
        print("sync:", error)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)
    if isinstance(message.channel, discord.DMChannel):
        if message.content.strip():
            await run_talk(message, message.content, is_dm=True)
        return
    ids = {bot.user.id}
    try:
        ids.add(int(APPLICATION_ID))
    except ValueError:
        pass
    content = message.content or ""
    if not any(user.id in ids for user in message.mentions) and not any(
        re.search(rf"<@!?{uid}>", content) for uid in ids
    ):
        return
    prompt = content
    for uid in ids:
        prompt = re.sub(rf"<@!?{uid}>", " ", prompt)
    prompt = prompt.strip()
    if not prompt:
        await message.reply("Escribe algo despues del @.", mention_author=False)
        return
    await run_talk(message, prompt, is_dm=False)


async def run_talk(message: discord.Message, raw: str, is_dm: bool) -> None:
    night, prompt = strip_flags(raw)
    if not prompt:
        await message.channel.send("Escribe un mensaje.")
        return
    blocked = talk_blocked(message.author)
    if blocked:
        await message.channel.send(blocked)
        return
    client, error = ai_client(night)
    if error or client is None:
        await message.channel.send(error or "IA no disponible.")
        return
    use_talk(message.author)
    thinking = None
    if not is_dm:
        thinking = await message.reply(f"{OPEN_MOUTH} lemme think for a sec", mention_author=False)

    async def send(content):
        if is_dm:
            await message.channel.send(content)
        else:
            await message.channel.send(content)

    async def edit(content):
        if thinking is not None:
            await thinking.edit(content=content)
        else:
            await send(content)

    try:
        async with message.channel.typing():
            thought, answer = await ask_ai(
                message.channel.id, message.author.display_name, prompt, client
            )
        await send_answer(send, None if is_dm else edit, prompt, thought, answer)
    except Exception as error:
        print("talk:", error)
        traceback.print_exc()
        fail = "La IA no pudo responder ahora."
        if thinking is not None:
            await thinking.edit(content=fail)
        else:
            await message.channel.send(fail)


# --- commands ---

@bot.tree.command(name="talk", description="Habla con la IA. 06:00–21:00 UTC-5.")
@app_commands.describe(prompt="Lo que quieres preguntar.")
async def talk(interaction: discord.Interaction, prompt: str):
    night, clean = strip_flags(prompt)
    if not clean:
        await interaction.response.send_message("Escribe un prompt.", ephemeral=True)
        return
    blocked = talk_blocked(interaction.user)
    if blocked:
        await interaction.response.send_message(blocked, ephemeral=True)
        return
    client, error = ai_client(night)
    if error or client is None:
        await interaction.response.send_message(error or "IA no disponible.", ephemeral=True)
        return
    use_talk(interaction.user)
    is_dm = interaction.guild is None
    if is_dm:
        await interaction.response.defer()
    else:
        await interaction.response.send_message(f"{OPEN_MOUTH} lemme think for a sec")
    try:
        thought, answer = await ask_ai(
            interaction.channel.id if interaction.channel else 0,
            interaction.user.display_name,
            clean,
            client,
        )

        async def send(content):
            await interaction.followup.send(content)

        async def edit(content):
            await interaction.edit_original_response(content=content)

        await send_answer(send, None if is_dm else edit, clean, thought, answer)
    except Exception as error:
        print("slash talk:", error)
        fail = "La IA no pudo responder ahora."
        if is_dm:
            await interaction.followup.send(fail)
        else:
            await interaction.edit_original_response(content=fail)


@bot.tree.command(name="say", description="Repite un mensaje. Usa \\n para saltar de linea.")
async def say(interaction: discord.Interaction, message: str):
    await interaction.response.send_message(message.replace("\\n", "\n"))


@bot.tree.command(name="switchengesp", description="Cambia tu idioma entre ingles y espanol.")
async def switchengesp(interaction: discord.Interaction):
    spanish[interaction.user.id] = not spanish.get(interaction.user.id, False)
    await save_memory()
    await interaction.response.send_message(text(interaction.user.id, "lang"), ephemeral=True)


@bot.tree.command(name="ping", description="Latencia del bot.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        text(interaction.user.id, "pong").format(ms=round(bot.latency * 1000))
    )


@bot.tree.command(name="roll", description="Tira un dado.")
async def roll(interaction: discord.Interaction, sides: int = 6):
    user_id = interaction.user.id
    if sides < 2:
        await interaction.response.send_message(text(user_id, "die_bad"), ephemeral=True)
        return
    await interaction.response.send_message(
        text(user_id, "die").format(n=random.randint(1, sides), sides=sides)
    )


@bot.tree.command(name="coinflip", description="Cara o cruz.")
async def coinflip(interaction: discord.Interaction):
    user_id = interaction.user.id
    side = text(user_id, random.choice(("heads", "tails")))
    await interaction.response.send_message(text(user_id, "coin").format(side=side))


@bot.tree.command(name="choose", description="Elige una opcion. Separa con comas.")
async def choose(interaction: discord.Interaction, options: str):
    user_id = interaction.user.id
    picks = [item.strip() for item in options.split(",") if item.strip()]
    if len(picks) < 2:
        await interaction.response.send_message(text(user_id, "choose_bad"), ephemeral=True)
        return
    await interaction.response.send_message(
        text(user_id, "choose").format(choice=random.choice(picks))
    )


@bot.tree.command(name="8ball", description="Preguntale a la bola 8.")
async def eightball(interaction: discord.Interaction, question: str):
    user_id = interaction.user.id
    answer = random.choice(BALLS[spanish.get(user_id, False)])
    await interaction.response.send_message(text(user_id, "ball").format(q=question, a=answer))


@bot.tree.command(name="balance", description="Cuantas monedas tienes.")
async def balance(interaction: discord.Interaction, user: discord.User | None = None):
    target = user or interaction.user
    row = wallet(target.id)
    items = ", ".join(row["inv"]) if row["inv"] else "nada"
    await interaction.response.send_message(
        f"**{target.display_name}**: **{row['coins']}** monedas · escudos **{row['shield']}** · {items}"
    )


@bot.tree.command(name="work", description="Gana monedas. Espera 5 minutos entre usos.")
async def work(interaction: discord.Interaction):
    row = wallet(interaction.user.id)
    left = wait_left(row["work"], 300)
    if left:
        await interaction.response.send_message(f"Puedes trabajar en **{left}s**.", ephemeral=True)
        return
    pay = random.randint(18, 52)
    row["coins"] += pay
    row["work"] = now().timestamp()
    await save_memory()
    await interaction.response.send_message(f"Trabajaste y ganaste **{pay}**. Ahora tienes **{row['coins']}**.")


@bot.tree.command(name="rob", description="Intenta quitarle monedas a alguien. Puede fallar.")
async def rob(interaction: discord.Interaction, user: discord.User):
    if user.id == interaction.user.id or user.bot:
        await interaction.response.send_message("Ese no.", ephemeral=True)
        return
    thief = wallet(interaction.user.id)
    left = wait_left(thief["rob"], 480)
    if left:
        await interaction.response.send_message(f"Espera **{left}s**.", ephemeral=True)
        return
    if thief["coins"] < 15:
        await interaction.response.send_message("Necesitas al menos 15 monedas.", ephemeral=True)
        return
    victim = wallet(user.id)
    thief["rob"] = now().timestamp()
    if victim["shield"] > 0:
        victim["shield"] -= 1
        fine = min(thief["coins"], 12)
        thief["coins"] -= fine
        await save_memory()
        await interaction.response.send_message(f"{user.display_name} tenia escudo. Perdiste **{fine}**.")
        return
    if victim["coins"] < 10 or random.random() >= 0.45:
        fine = min(thief["coins"], random.randint(8, 20))
        thief["coins"] -= fine
        await save_memory()
        await interaction.response.send_message(f"Fallaste y perdiste **{fine}**.")
        return
    stolen = min(victim["coins"], max(5, int(victim["coins"] * 0.15)), 80)
    victim["coins"] -= stolen
    thief["coins"] += stolen
    await save_memory()
    await interaction.response.send_message(f"Le quitaste **{stolen}** a {user.display_name}. Tienes **{thief['coins']}**.")


@bot.tree.command(name="shop", description="Lista de la tienda.")
async def shop(interaction: discord.Interaction):
    lines = ["Tienda:"]
    for key, (price, label) in SHOP.items():
        lines.append(f"`{key}` {label} — {price}")
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="buy", description="Compra: cookie, hat, shield, plush, title.")
async def buy(interaction: discord.Interaction, item: str):
    listing = SHOP.get(item.strip().lower())
    if listing is None:
        await interaction.response.send_message("No esta en la tienda. Mira /shop.", ephemeral=True)
        return
    price, label = listing
    row = wallet(interaction.user.id)
    if row["coins"] < price:
        await interaction.response.send_message(f"Te faltan monedas. Cuesta **{price}**.", ephemeral=True)
        return
    row["coins"] -= price
    if item.strip().lower() == "shield":
        row["shield"] += 1
    else:
        row["inv"].append(label)
    await save_memory()
    await interaction.response.send_message(f"Compraste **{label}**. Te quedan **{row['coins']}**.")


@bot.tree.command(name="pay", description="Paga monedas a alguien.")
async def pay(interaction: discord.Interaction, user: discord.User, amount: int):
    if user.bot or user.id == interaction.user.id or amount < 1:
        await interaction.response.send_message("No se puede.", ephemeral=True)
        return
    sender = wallet(interaction.user.id)
    if sender["coins"] < amount:
        await interaction.response.send_message("No te alcanza.", ephemeral=True)
        return
    sender["coins"] -= amount
    wallet(user.id)["coins"] += amount
    await save_memory()
    await interaction.response.send_message(f"Le pasaste **{amount}** a {user.display_name}. Te quedan **{sender['coins']}**.")


if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
