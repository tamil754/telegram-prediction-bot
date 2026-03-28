import asyncio
import aiohttp
import json
import logging
import aiosqlite
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ================= DISABLE VERBOSE LOGGING =================
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ================= COLORED PRINT =================
def cprint(msg, color="cyan"):
    colors = {
        "cyan": "\033[96m",
        "blue": "\033[94m",
        "reset": "\033[0m"
    }
    start = colors.get(color, colors["cyan"])
    end = colors["reset"]
    print(f"{start}{msg}{end}")
    import sys
    sys.stdout.flush()

# ================= CONFIG =================
BOT_TOKEN = "8766752856:AAEiJvfe3sZ2s4h9MDk6zJWF9BJ4990A27A"
OWNER_ID = 8171102858

WIN_STICKER = "CAACAgUAAxkBAAEQ0YdpxAG9XxDr6CTvaAwki7WyW8Sh4AACyBsAAkQ0aFXCWdPVF2tZmjoE"
LOSS_STICKER = "CAACAgUAAxkBAAEQ0aBpxA8Ca1jqDrRxgeNroGQ6M34dtQAChBsAAqnymFb-nWVnvR760DoE"
NUMBER_WIN_STICKER = "CAACAgUAAxkBAAEQwVBptnTvxpiq-ivF1Fr6Y3k8pfrH9AACERkAAqBZoVbtx3BiOZCU4ToE"

HISTORY_API = "https://draw.ar-lottery01.com/WinGo/WinGo_1M/GetHistoryIssuePage.json"
PREDICTION_COST = 10
DEFAULT_BALANCE = 0
DAILY_BONUS_AMOUNT = 10
REFERRAL_REWARD = 5
MAINTENANCE_MODE = False

# Pattern constants
DRAGON_START_STREAK = 3
DRAGON_BREAK_STREAK = 5

# ================= HELPER =================
def getBigSmall(num):
    return "BIG" if num >= 5 else "SMALL"

def getSingleNumber(side, period, history):
    """Return a single number for the predicted side."""
    if side == "SMALL":
        # Possible small numbers: 0,1,2,3,4
        candidates = [0,1,2,3,4]
    else:
        # Possible big numbers: 5,6,7,8,9
        candidates = [5,6,7,8,9]

    last_nums = [int(h["number"]) for h in history[:5]]
    # Choose a number that hasn't appeared in the last 5 draws (if possible)
    for num in candidates:
        if num not in last_nums:
            return num
    # If all candidates appeared, pick the one that appeared least (or just first)
    return candidates[0]

# ================= PATTERN PREDICTION METHODS =================
def predict_dragon_pattern(history):
    if len(history) < 1:
        return None
    current_side = getBigSmall(int(history[0]["number"]))
    streak = 1
    for i in range(1, len(history)):
        if getBigSmall(int(history[i]["number"])) == current_side:
            streak += 1
        else:
            break
    if streak >= DRAGON_BREAK_STREAK:
        return "BIG" if current_side == "SMALL" else "SMALL"
    elif streak >= DRAGON_START_STREAK:
        return current_side
    return None

def predict_zigzag_pattern(history):
    if len(history) < 5:
        return None
    sides = [getBigSmall(int(h["number"])) for h in history[:5]]
    alternations = sum(1 for i in range(4) if sides[i] != sides[i+1])
    if alternations >= 4:
        return "SMALL" if sides[0] == "BIG" else "BIG"
    return None

def alternating_pattern(history):
    if len(history) < 2:
        return None
    last1 = getBigSmall(int(history[0]["number"]))
    last2 = getBigSmall(int(history[1]["number"]))
    if last1 != last2:
        return "SMALL" if last1 == "BIG" else "BIG"
    return None

def predict_v_shape_pattern(history):
    if len(history) < 2:
        return None
    last1 = getBigSmall(int(history[0]["number"]))
    last2 = getBigSmall(int(history[1]["number"]))
    if last1 == "BIG" and last2 == "SMALL":
        return "BIG"
    elif last1 == "SMALL" and last2 == "BIG":
        return "SMALL"
    return None

def pattern_confidence(pattern_name, history):
    if pattern_name == "dragon_pattern":
        if len(history) > 0:
            current = getBigSmall(int(history[0]["number"]))
            streak = 1
            for i in range(1, len(history)):
                if getBigSmall(int(history[i]["number"])) == current:
                    streak += 1
                else:
                    break
            if streak >= DRAGON_BREAK_STREAK:
                return 85
            elif streak >= DRAGON_START_STREAK:
                return 70 + min(10, (streak - DRAGON_START_STREAK) * 3)
    elif pattern_name == "zigzag_pattern":
        if len(history) >= 5:
            sides = [getBigSmall(int(h["number"])) for h in history[:5]]
            alternations = sum(1 for i in range(4) if sides[i] != sides[i+1])
            if alternations >= 4:
                return 85
            else:
                return 70
        return 70
    elif pattern_name == "alternating_pattern":
        return 75
    elif pattern_name == "v_shape_pattern":
        return 75
    return 70

def finalDecision(history):
    """Returns (side, confidence, method_name) based only on global history."""
    # 1. Dragon
    dragon = predict_dragon_pattern(history)
    if dragon:
        confidence = pattern_confidence("dragon_pattern", history)
        return dragon, confidence, "dragon_pattern"

    # 2. Zigzag
    zigzag = predict_zigzag_pattern(history)
    if zigzag:
        confidence = pattern_confidence("zigzag_pattern", history)
        return zigzag, confidence, "zigzag_pattern"

    # 3. Alternating
    alternating = alternating_pattern(history)
    if alternating:
        confidence = pattern_confidence("alternating_pattern", history)
        return alternating, confidence, "alternating_pattern"

    # 4. V‑shape
    vshape = predict_v_shape_pattern(history)
    if vshape:
        confidence = pattern_confidence("v_shape_pattern", history)
        return vshape, confidence, "v_shape_pattern"

    # 5. No pattern – fallback to trend (last outcome)
    if len(history) > 0:
        trend = getBigSmall(int(history[0]["number"]))
        confidence = 60
        return trend, confidence, "Trend (last outcome)"
    else:
        return "BIG", 50, "Default"

# ================= DATABASE =================
DB_PATH = "vip_bot.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0,
                is_admin INTEGER DEFAULT 0,
                is_blocked INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                auto_predict INTEGER DEFAULT 0,
                referral_by INTEGER DEFAULT NULL,
                last_bonus_claim TIMESTAMP DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        for col in ["is_active", "auto_predict", "last_bonus_claim"]:
            try:
                if col == "last_bonus_claim":
                    await db.execute("ALTER TABLE users ADD COLUMN last_bonus_claim TIMESTAMP")
                else:
                    await db.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 1" if col == "is_active" else f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0")
            except:
                pass

        await db.execute('''
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                period TEXT,
                predicted_side TEXT,
                predicted_numbers TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount INTEGER,
                type TEXT,
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        await db.execute("INSERT OR IGNORE INTO users (user_id, username, is_admin) VALUES (?, ?, ?)",
                         (OWNER_ID, "owner", 1))
        await db.execute("UPDATE users SET is_admin = 1 WHERE user_id = ? AND is_admin != 1", (OWNER_ID,))
        await db.commit()

async def get_user_balance(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0

async def update_balance(user_id, delta, type, note=""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (delta, user_id))
        await db.execute("INSERT INTO transactions (user_id, amount, type, note) VALUES (?, ?, ?, ?)",
                         (user_id, delta, type, note))
        await db.commit()
    return await get_user_balance(user_id)

async def deduct_coin(user_id):
    bal = await get_user_balance(user_id)
    if bal >= PREDICTION_COST:
        new_bal = await update_balance(user_id, -PREDICTION_COST, "prediction_cost", f"Paid {PREDICTION_COST} coin for prediction")
        return True, new_bal
    return False, bal

async def add_daily_bonus(user_id, amount):
    return await update_balance(user_id, amount, "bonus", f"Daily bonus")

async def can_claim_bonus(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT last_bonus_claim FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row or row[0] is None:
            return True
        last_claim = datetime.fromisoformat(row[0])
        return datetime.now() - last_claim >= timedelta(hours=24)

async def set_last_bonus_claim(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_bonus_claim = ? WHERE user_id = ?",
                         (datetime.now().isoformat(), user_id))
        await db.commit()

async def get_referral_count(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users WHERE referral_by = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0

async def get_total_users():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        return row[0] if row else 0

async def get_user_total_predictions(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0

async def get_user_total_coins_spent(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT SUM(amount) FROM transactions WHERE user_id = ? AND type = 'prediction_cost'", (user_id,))
        row = await cursor.fetchone()
        return abs(row[0]) if row and row[0] else 0

async def get_user_win_count(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ? AND status = 'WIN'", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0

async def get_user_loss_count(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ? AND status = 'LOSS'", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0

async def is_admin(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row and row[0] == 1

async def is_blocked(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row and row[0] == 1

async def is_active(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT is_active FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row and row[0] == 1

async def set_active(user_id, active):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_active = ? WHERE user_id = ?", (active, user_id))
        await db.commit()

async def get_auto_predict(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT auto_predict FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0

async def set_auto_predict(user_id, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET auto_predict = ? WHERE user_id = ?", (value, user_id))
        await db.commit()

async def get_maintenance():
    global MAINTENANCE_MODE
    return MAINTENANCE_MODE

async def set_maintenance(mode):
    global MAINTENANCE_MODE
    MAINTENANCE_MODE = mode

# ================= KEYBOARDS =================
def get_user_keyboard():
    buttons = [
        ["💰 My Balance", "👥 Referral"],
        ["🎁 Bonus", "📊 Stats"],
        ["📞 Contact"]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def get_admin_main_keyboard():
    buttons = [
        ["⚙️ Admin Panel", "🛠 Maintenance", "📢 Broadcast"],
        ["📊 Stats"]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def get_admin_panel_keyboard():
    buttons = [
        ["➕ Add Coin", "➖ Remove Coin"],
        ["🚫 Block User", "🎁 Bonus Add User"],
        ["🔙 Back"]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

# ================= FETCH HISTORY =================
async def fetch_history():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*"
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(HISTORY_API, timeout=10) as resp:
                if resp.status != 200:
                    return None
                text = await resp.text()
                data = json.loads(text)
                return data
    except Exception as e:
        cprint(f"fetch_history error: {e}", "cyan")
        return None

# ================= NOTIFY ADMINS =================
async def notify_admins(context, title, user_id, user_name, amount, new_balance, event_type):
    total_users = await get_total_users()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users WHERE is_admin = 1")
        admins = await cursor.fetchall()
    admin_ids = [a[0] for a in admins]

    if event_type == "insufficient_coins":
        notification = (
            f"⚠️ <b>Insufficient Coins</b> ⚠️\n\n"
            f"👤 <b>User:</b> <a href='tg://user?id={user_id}'>{user_name}</a>\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            f"💰 <b>Current Balance:</b> {amount} coins\n"
            f"🔄 <b>Auto-predict stopped</b>\n"
            f"🌝 <b>Total Users:</b> {total_users}\n"
            f"📅 <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
    else:
        return

    for admin_id in admin_ids:
        try:
            await context.bot.send_message(admin_id, notification, parse_mode="HTML")
        except:
            pass

# ================= CORE PREDICTION LOGIC (GLOBAL) =================
async def make_prediction(user_id, context):
    admin = await is_admin(user_id)

    if not admin:
        bal = await get_user_balance(user_id)
        if bal < PREDICTION_COST:
            user = await context.bot.get_chat(user_id)
            user_name = user.first_name
            await notify_admins(context, "insufficient_coins", user_id, user_name, bal, 0, "insufficient_coins")
            await set_auto_predict(user_id, 0)
            try:
                await context.bot.send_message(user_id, f"❌ Insufficient coins! Auto-predict stopped. Need {PREDICTION_COST} coin per prediction.")
            except:
                pass
            return False

    data = await fetch_history()
    if not data:
        return False

    history = data["data"]["list"]
    next_period = str(int(history[0]["issueNumber"]) + 1)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM predictions WHERE user_id = ? AND period = ? AND status = 'pending'",
            (user_id, next_period)
        )
        existing = await cursor.fetchone()
        if existing:
            return False

    # Global prediction (same for all users)
    side, confidence, pattern = finalDecision(history)

    # Generate a single number based on side and history
    number = getSingleNumber(side, next_period, history)

    # Deduct coins and get new balance
    new_bal = None
    if not admin:
        success, new_bal = await deduct_coin(user_id)
        if not success:
            user = await context.bot.get_chat(user_id)
            user_name = user.first_name
            await notify_admins(context, "insufficient_coins", user_id, user_name, await get_user_balance(user_id), 0, "insufficient_coins")
            await set_auto_predict(user_id, 0)
            try:
                await context.bot.send_message(user_id, f"❌ Insufficient coins! Auto-predict stopped. Need {PREDICTION_COST} coin per prediction.")
            except:
                pass
            return False

    # Store prediction (single number as string)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO predictions (user_id, period, predicted_side, predicted_numbers) VALUES (?, ?, ?, ?)",
            (user_id, next_period, side, str(number))
        )
        await db.commit()

    # Build prediction message
    period_short = next_period[-5:] if len(next_period) >= 5 else next_period
    msg = f"🎁 TAMIL VIP PREDICTION 🎉\n\n"
    msg += f"🆔 Period    :  {period_short}\n"
    msg += f"🛡 Predict    :  {side} {number}\n"
    msg += f"🎯 Pattern    :  {pattern}\n"

    if not admin:
        msg += f"\n💰 Cost        {PREDICTION_COST} coins\n"
        msg += f"💳 balance     {new_bal}"
    else:
        msg += f"\n💰 Cost        FREE (admin)"

    try:
        await context.bot.send_message(user_id, msg)
    except Exception as e:
        cprint(f"Failed to send prediction to {user_id}: {e}", "cyan")

    return True

# ================= COMMAND HANDLERS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, balance, is_active) VALUES (?, ?, ?, 1)",
            (user_id, username, DEFAULT_BALANCE)
        )
        await db.commit()

    args = context.args
    if args and args[0].startswith("ref_"):
        referrer_id = int(args[0][4:])
        if referrer_id != user_id:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET referral_by = ? WHERE user_id = ?", (referrer_id, user_id))
                await db.commit()
            await update_balance(referrer_id, REFERRAL_REWARD, "referral", f"Referral bonus for {user_id}")

    await set_active(user_id, 1)

    admin = await is_admin(user_id)
    keyboard = get_admin_main_keyboard() if admin else get_user_keyboard()
    await update.message.reply_text(
        f"Welcome {username}!\n"
        f"Your balance: {await get_user_balance(user_id)} coins\n\n"
        f"Use /predict to start automatic predictions (costs {PREDICTION_COST} coins each).\n"
        f"Use /stop to stop auto-predict.\n\n"
        f"🎨 Patterns used: Dragon → Zigzag → Alternating → V‑shape → Trend (fallback)",
        reply_markup=keyboard
    )

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await set_auto_predict(user_id, 0)
    await update.message.reply_text("✅ Auto‑predict mode disabled. Use /predict to enable again.")

async def predict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not await is_active(user_id):
        await update.message.reply_text("⚠️ Your account is inactive. Use /start to activate.")
        return

    if await get_maintenance() and not await is_admin(user_id):
        await update.message.reply_text("🔧 Bot is under maintenance. Please try later.")
        return

    if await is_blocked(user_id):
        await update.message.reply_text("🚫 You are blocked from using this bot.")
        return

    if not await is_admin(user_id):
        bal = await get_user_balance(user_id)
        if bal < PREDICTION_COST:
            await update.message.reply_text(f"❌ Insufficient coins! Need {PREDICTION_COST} coins to start auto-predict.")
            return

    await set_auto_predict(user_id, 1)
    await update.message.reply_text(f"✅ Auto‑predict enabled! I'll predict every new period until you run out of coins. Each prediction costs {PREDICTION_COST} coins.\nUse /stop to cancel.")
    await make_prediction(user_id, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *TAMIL VIP PREDICTION BOT*\n\n"
        "📌 *Commands:*\n"
        "/start - Register and show menu\n"
        "/stop - Disable auto-predict\n"
        "/predict - Start auto-predict mode\n"
        "/help - Show this message\n\n"
        "🎮 *Features:*\n"
        "• Auto‑predict mode (10 coins per prediction)\n"
        "• Real-time result notifications\n"
        "• Referral bonuses\n"
        "• Daily bonus (10 coins every 24 hours)\n"
        "• Admin panel for coin management\n\n"
        "🎨 *Patterns used:* Dragon → Zigzag → Alternating → V‑shape → Trend (fallback)\n\n"
        "👑 *Owner:* @TAMIL_VIP_1",
        parse_mode="Markdown"
    )

# ================= MENU BUTTON HANDLERS =================
async def mybalance_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bal = await get_user_balance(user_id)
    await update.message.reply_text(f"💰 Your balance: {bal} coins")

async def referral_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ref_count = await get_referral_count(user_id)
    link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
    await update.message.reply_text(
        f"👥 *Referral Program*\n\n"
        f"📊 *Total Referrals:* {ref_count}\n"
        f"🎁 *Reward per Referral:* {REFERRAL_REWARD} coins\n\n"
        f"🔗 *Your Invite Link:*\n`{link}`\n\n"
        f"Share the link and earn {REFERRAL_REWARD} coins for every friend who joins!",
        parse_mode="Markdown"
    )

async def bonus_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not await is_active(user_id):
        await update.message.reply_text("⚠️ Your account is inactive. Use /start to activate.")
        return

    if await is_blocked(user_id):
        await update.message.reply_text("🚫 You are blocked from using this bot.")
        return

    if not await can_claim_bonus(user_id):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT last_bonus_claim FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row and row[0]:
                last_claim = datetime.fromisoformat(row[0])
                next_claim = last_claim + timedelta(hours=24)
                remaining = next_claim - datetime.now()
                hours = remaining.seconds // 3600
                minutes = (remaining.seconds % 3600) // 60
                seconds = remaining.seconds % 60
                await update.message.reply_text(
                    f"⏳ You already claimed your daily bonus!\n"
                    f"Next claim available in: {hours}h {minutes}m {seconds}s"
                )
            else:
                await update.message.reply_text("⏳ You can claim your daily bonus now! Try again.")
        return

    new_bal = await add_daily_bonus(user_id, DAILY_BONUS_AMOUNT)
    await set_last_bonus_claim(user_id)

    await update.message.reply_text(
        f"🎁 Daily bonus claimed!\n"
        f"💰 +{DAILY_BONUS_AMOUNT} coins\n"
        f"💳 New balance: {new_bal}\n\n"
        f"Come back in 24 hours for your next bonus!"
    )

    # Notify owner and admins
    user = update.effective_user
    total_users = await get_total_users()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users WHERE is_admin = 1")
        admins = await cursor.fetchall()
    admin_ids = [a[0] for a in admins]

    notification = (
        f"➕ <b>Daily Bonus Claimed</b> ➕\n\n"
        f"👤 <b>User:</b> <a href='tg://user?id={user_id}'>{user.first_name}</a>\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
        f"💰 <b>Amount:</b> +{DAILY_BONUS_AMOUNT} coins\n"
        f"💳 <b>New Balance:</b> {new_bal}\n"
        f"🌝 <b>Total Users:</b> {total_users}\n"
        f"📅 <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    for admin_id in admin_ids:
        try:
            await context.bot.send_message(admin_id, notification, parse_mode="HTML")
        except:
            pass

async def stats_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    balance = await get_user_balance(user_id)
    total_pred = await get_user_total_predictions(user_id)
    total_spent = await get_user_total_coins_spent(user_id)
    wins = await get_user_win_count(user_id)
    losses = await get_user_loss_count(user_id)
    referrals = await get_referral_count(user_id)

    win_rate = (wins / total_pred * 100) if total_pred > 0 else 0

    await update.message.reply_text(
        f"📊 *Your Statistics*\n\n"
        f"💰 *Balance:* {balance} coins\n"
        f"🔮 *Total Predictions:* {total_pred}\n"
        f"✅ *Wins:* {wins}\n"
        f"❌ *Losses:* {losses}\n"
        f"📈 *Win Rate:* {win_rate:.1f}%\n"
        f"💸 *Total Coins Spent:* {total_spent}\n"
        f"👥 *Referrals:* {referrals}\n\n"
        f"©️ Powered by @TAMIL_VIP_1",
        parse_mode="Markdown"
    )

async def contact_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📞 Contact: @TAMIL_VIP_1")

# ================= ADMIN HANDLERS =================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        await update.message.reply_text("⛔ Access denied.")
        return
    await update.message.reply_text("Admin Panel:", reply_markup=get_admin_panel_keyboard())
    context.user_data["in_admin_panel"] = True

async def toggle_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        await update.message.reply_text("⛔ Access denied.")
        return
    mode = await get_maintenance()
    await set_maintenance(not mode)
    await update.message.reply_text(f"🛠 Maintenance mode set to {not mode}")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        await update.message.reply_text("⛔ Access denied.")
        return
    context.user_data["awaiting_broadcast"] = True
    await update.message.reply_text("📢 Send the message you want to broadcast to all users:")

async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_broadcast"):
        msg = update.message.text
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT user_id FROM users WHERE is_blocked = 0")
            rows = await cursor.fetchall()
            for row in rows:
                try:
                    await context.bot.send_message(row[0], msg)
                except:
                    pass
        await update.message.reply_text("✅ Broadcast sent.")
        del context.user_data["awaiting_broadcast"]
        await update.message.reply_text("Admin Menu:", reply_markup=get_admin_main_keyboard())

# ================= ADMIN PANEL ACTIONS =================
async def handle_admin_panel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data is None:
        context.user_data = {}
    if not context.user_data.get("in_admin_panel"):
        return
    text = update.message.text
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        await update.message.reply_text("⛔ Access denied.")
        return

    if text == "➕ Add Coin":
        context.user_data["admin_action"] = "add_coin"
        await update.message.reply_text("Enter user ID and amount (space separated):")
    elif text == "➖ Remove Coin":
        context.user_data["admin_action"] = "remove_coin"
        await update.message.reply_text("Enter user ID and amount (space separated):")
    elif text == "🚫 Block User":
        context.user_data["admin_action"] = "block_user"
        await update.message.reply_text("Enter user ID to block:")
    elif text == "🎁 Bonus Add User":
        context.user_data["admin_action"] = "bonus_user"
        await update.message.reply_text("Enter user ID and amount (space separated):")
    elif text == "🔙 Back":
        del context.user_data["in_admin_panel"]
        await update.message.reply_text("Returning to admin menu.", reply_markup=get_admin_main_keyboard())
    else:
        await handle_admin_input(update, context)

async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get("admin_action")
    if not action:
        return

    text = update.message.text.strip()
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        await update.message.reply_text("⛔ Access denied.")
        return

    if action == "add_coin":
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("Invalid format. Use: user_id amount")
            return
        try:
            target = int(parts[0])
            amount = int(parts[1])
            await update_balance(target, amount, "admin_add", f"Added by admin {user_id}")
            await update.message.reply_text(f"Added {amount} coins to user {target}.")
        except:
            await update.message.reply_text("Invalid input.")
        del context.user_data["admin_action"]
        await update.message.reply_text("Admin Panel:", reply_markup=get_admin_panel_keyboard())

    elif action == "remove_coin":
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("Invalid format. Use: user_id amount")
            return
        try:
            target = int(parts[0])
            amount = int(parts[1])
            await update_balance(target, -amount, "admin_remove", f"Removed by admin {user_id}")
            await update.message.reply_text(f"Removed {amount} coins from user {target}.")
        except:
            await update.message.reply_text("Invalid input.")
        del context.user_data["admin_action"]
        await update.message.reply_text("Admin Panel:", reply_markup=get_admin_panel_keyboard())

    elif action == "block_user":
        try:
            target = int(text)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET is_blocked = 1 WHERE user_id = ?", (target,))
                await db.commit()
            await update.message.reply_text(f"User {target} blocked.")
        except:
            await update.message.reply_text("Invalid user ID.")
        del context.user_data["admin_action"]
        await update.message.reply_text("Admin Panel:", reply_markup=get_admin_panel_keyboard())

    elif action == "bonus_user":
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("Invalid format. Use: user_id amount")
            return
        try:
            target = int(parts[0])
            amount = int(parts[1])
            await update_balance(target, amount, "bonus", f"Bonus added by admin {user_id}")
            await update.message.reply_text(f"Added {amount} bonus coins to user {target}.")
        except:
            await update.message.reply_text("Invalid input.")
        del context.user_data["admin_action"]
        await update.message.reply_text("Admin Panel:", reply_markup=get_admin_panel_keyboard())

# ================= BACKGROUND RESOLVER =================
async def check_results(context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await fetch_history()
        if not data:
            return
        history = data["data"]["list"]
        latest = history[0]
        period = str(latest["issueNumber"])
        actual_num = int(latest["number"])
        actual_side = getBigSmall(actual_num)

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT id, user_id, predicted_side, predicted_numbers FROM predictions WHERE period = ? AND status = 'pending'",
                (period,)
            )
            pending = await cursor.fetchall()
            for pred_id, user_id, side, num_str in pending:
                predicted_num = int(num_str)  # single number
                exact_match = (actual_num == predicted_num)
                side_match = (side == actual_side)

                if exact_match:
                    status = "WIN"
                    sticker = NUMBER_WIN_STICKER
                    message = f"🎉 JACKPOT! Exact number match {actual_num}"
                elif side_match:
                    status = "WIN"
                    sticker = WIN_STICKER
                    message = f"✅ WIN!!! {side} Matched 🔥"
                else:
                    status = "LOSS"
                    sticker = LOSS_STICKER
                    message = f"❌ LOSS {actual_side}"

                await db.execute("UPDATE predictions SET status = ? WHERE id = ?", (status, pred_id))
                await db.commit()

                if await is_active(user_id):
                    try:
                        await context.bot.send_sticker(user_id, sticker)
                        await context.bot.send_message(user_id, message)
                    except Exception as e:
                        cprint(f"Failed to notify user {user_id}: {e}", "cyan")

            # Auto-predict for users
            cursor = await db.execute(
                "SELECT user_id FROM users WHERE auto_predict = 1 AND is_blocked = 0"
            )
            auto_users = await cursor.fetchall()
            for (user_id,) in auto_users:
                if not await is_active(user_id):
                    continue
                await make_prediction(user_id, context)

    except Exception as e:
        cprint(f"Error in check_results: {e}", "cyan")

# ================= MAIN =================
def main():
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(init_db())

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("predict", predict))
    app.add_handler(CommandHandler("help", help_command))

    # User menu buttons
    app.add_handler(MessageHandler(filters.Text("💰 My Balance"), mybalance_button))
    app.add_handler(MessageHandler(filters.Text("👥 Referral"), referral_button))
    app.add_handler(MessageHandler(filters.Text("🎁 Bonus"), bonus_button))
    app.add_handler(MessageHandler(filters.Text("📊 Stats"), stats_button))
    app.add_handler(MessageHandler(filters.Text("📞 Contact"), contact_button))

    # Admin panel and broadcast
    app.add_handler(MessageHandler(filters.Text("⚙️ Admin Panel"), admin_panel))
    app.add_handler(MessageHandler(filters.Text("🛠 Maintenance"), toggle_maintenance))
    app.add_handler(MessageHandler(filters.Text("📢 Broadcast"), broadcast))

    # Admin panel actions
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_panel_action))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_broadcast))

    # Background job
    app.job_queue.run_repeating(check_results, interval=4, first=4, job_kwargs={'max_instances': 3})

    cprint("🤖 TAMIL VIP PREDICTION BOT STARTED (Pattern-based, Global Predictions, Single Number)", "cyan")
    cprint("===================================", "cyan")
    cprint(f"Prediction cost: {PREDICTION_COST} coins", "cyan")
    cprint(f"Daily bonus: {DAILY_BONUS_AMOUNT} coins", "cyan")
    cprint(f"Referral reward: {REFERRAL_REWARD} coins", "cyan")
    cprint(f"Owner ID: {OWNER_ID}", "cyan")
    cprint("🎨 Patterns used: Dragon → Zigzag → Alternating → V‑shape → Trend (fallback)", "cyan")
    cprint("📌 All users receive the SAME prediction for each period", "cyan")
    cprint("🔢 Predictions show a single number (instead of two)", "cyan")
    cprint("===================================", "cyan")

    app.run_polling()

if __name__ == "__main__":
    main()
