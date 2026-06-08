import os
import requests
import json
import telebot
import time
import re
import subprocess
import random
import string
import threading
from supabase import create_client
from user_agent import generate_user_agent

# ========== الإعدادات ==========
TOKEN = os.environ.get("TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise Exception("Missing required environment variables")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

MIN_DURATION = 10
MAX_DURATION = 60
MAX_VOICES = 5

# ========== تهيئة قاعدة البيانات ==========
def init_db():
    try:
        supabase.table("user_data").select("user_id").limit(1).execute()
    except:
        supabase.table("user_data").insert({"user_id": "0", "voices": [], "active_voice_id": None}).execute()
        supabase.table("user_data").delete().eq("user_id", "0").execute()
    try:
        supabase.table("settings").select("key").limit(1).execute()
    except:
        supabase.table("settings").insert({"key": "admin_settings", "data": {"status": "free", "paid_users": {}, "blocked_users": []}}).execute()
    try:
        supabase.table("stats").select("key").limit(1).execute()
    except:
        supabase.table("stats").insert({"key": "global_stats", "data": {"total_users": 0, "usage_count": 0, "users": {}}}).execute()

init_db()

# ========== دوال قاعدة البيانات ==========
def get_user_entry(user_id):
    uid = str(user_id)
    try:
        r = supabase.table("user_data").select("*").eq("user_id", uid).execute()
        if r.data and len(r.data) > 0:
            entry = r.data[0]
            if "voices" not in entry or not isinstance(entry["voices"], list):
                entry["voices"] = []
            if "active_voice_id" not in entry:
                entry["active_voice_id"] = None
            return entry
        else:
            entry = {"user_id": uid, "voices": [], "active_voice_id": None}
            supabase.table("user_data").insert(entry).execute()
            return entry
    except Exception as e:
        print(f"Error in get_user_entry: {e}")
        return {"user_id": uid, "voices": [], "active_voice_id": None}

def save_user_entry(entry):
    uid = entry["user_id"]
    supabase.table("user_data").update(entry).eq("user_id", uid).execute()

def save_user_voice(user_id, voice_id, voice_name="بدون اسم"):
    entry = get_user_entry(user_id)
    entry["voices"] = [v for v in entry["voices"] if v["id"] != voice_id]
    entry["voices"].append({"id": voice_id, "name": voice_name, "created": time.time()})
    if len(entry["voices"]) > MAX_VOICES:
        entry["voices"].sort(key=lambda x: x.get("created", 0))
        entry["voices"] = entry["voices"][-MAX_VOICES:]
    if entry["active_voice_id"] is None or entry["active_voice_id"] not in [v["id"] for v in entry["voices"]]:
        entry["active_voice_id"] = voice_id
    save_user_entry(entry)

def delete_user_voice(user_id, voice_id):
    entry = get_user_entry(user_id)
    entry["voices"] = [v for v in entry["voices"] if v["id"] != voice_id]
    if entry["active_voice_id"] == voice_id:
        entry["active_voice_id"] = entry["voices"][-1]["id"] if entry["voices"] else None
    save_user_entry(entry)

def rename_user_voice(user_id, voice_id, new_name):
    entry = get_user_entry(user_id)
    for v in entry["voices"]:
        if v["id"] == voice_id:
            v["name"] = new_name
            break
    save_user_entry(entry)

def get_active_voice(user_id):
    entry = get_user_entry(user_id)
    active_id = entry["active_voice_id"]
    if not active_id:
        return None
    for v in entry["voices"]:
        if v["id"] == active_id:
            return v
    return None

def set_active_voice(user_id, voice_id):
    entry = get_user_entry(user_id)
    if any(v["id"] == voice_id for v in entry["voices"]):
        entry["active_voice_id"] = voice_id
        save_user_entry(entry)
        return True
    return False

def load_settings():
    try:
        r = supabase.table("settings").select("*").eq("key", "admin_settings").execute()
        if r.data and len(r.data) > 0:
            data = r.data[0]["data"]
            if "blocked_users" not in data:
                data["blocked_users"] = []
            return data
        else:
            default = {"status": "free", "paid_users": {}, "blocked_users": []}
            supabase.table("settings").insert({"key": "admin_settings", "data": default}).execute()
            return default
    except:
        return {"status": "free", "paid_users": {}, "blocked_users": []}

def save_settings(data):
    supabase.table("settings").update({"data": data}).eq("key", "admin_settings").execute()

def load_stats():
    try:
        r = supabase.table("stats").select("*").eq("key", "global_stats").execute()
        if r.data and len(r.data) > 0:
            return r.data[0]["data"]
        else:
            default = {"total_users": 0, "usage_count": 0, "users": {}}
            supabase.table("stats").insert({"key": "global_stats", "data": default}).execute()
            return default
    except:
        return {"total_users": 0, "usage_count": 0, "users": {}}

def save_stats(data):
    supabase.table("stats").update({"data": data}).eq("key", "global_stats").execute()

def update_stats(user_id):
    stats = load_stats()
    uid = str(user_id)
    if uid not in stats["users"]:
        stats["users"][uid] = {"first_seen": time.time(), "count": 0}
        stats["total_users"] = len(stats["users"])
    stats["usage_count"] += 1
    stats["users"][uid]["count"] = stats["users"][uid].get("count", 0) + 1
    save_stats(stats)

# ========== جلسات VoicesLab ==========
http_session = requests.Session()
http_session.headers.update({"User-Agent": generate_user_agent()})

def create_temp_email():
    r = requests.post("https://api.internal.temp-mail.io/api/v3/email/new",
                      headers={"User-Agent": generate_user_agent()}, json={"min_name_length": 10, "max_name_length": 10}, timeout=10)
    return r.json()["email"]

def get_csrf():
    r = http_session.get("https://voiceslab.io/api/auth/csrf", timeout=10)
    return r.json()["csrfToken"]

def start_email_signin(email, csrf):
    return http_session.post("https://voiceslab.io/api/auth/signin/email",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"email": email, "csrfToken": csrf, "callbackUrl": "https://voiceslab.io/en/dashboard/voice-cloning", "redirect": "false", "lang": "en", "json": "true"},
        allow_redirects=False, timeout=10)

def poll_for_callback(email, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        r = requests.get(f"https://api.internal.temp-mail.io/api/v3/email/{email}/messages", timeout=10)
        if r.ok:
            for msg in r.json():
                text = (msg.get("body_text", "") + msg.get("body_html", ""))
                match = re.search(r"https?://[^\s'\"<>]+", text)
                if match:
                    link = match.group(0).replace("&amp;", "&")
                    if "callback" in link or "token=" in link:
                        return link
        time.sleep(2)
    raise TimeoutError("لم يصل رابط التفعيل")

def follow_link(link):
    r = http_session.get(link, headers={"Referer": "https://voiceslab.io/"}, allow_redirects=True, timeout=15)
    cookies = {k: v for k, v in http_session.cookies.items() if "next-auth" in k.lower() or "session" in k.lower()}
    return r, cookies

def create_new_session():
    http_session.cookies.clear()
    email = create_temp_email()
    csrf = get_csrf()
    start_email_signin(email, csrf)
    link = poll_for_callback(email)
    _, cookies = follow_link(link)
    new_csrf = get_csrf()
    return {"email": email, "verify_link": link, "cookies": cookies, "csrfToken": new_csrf}

def get_valid_session():
    return create_new_session()

def get_cookie_header(session_data):
    return "; ".join(f"{k}={v}" for k, v in session_data["cookies"].items())

def upload_voice_file(audio_path, session_data):
    cookie = get_cookie_header(session_data)
    csrf = session_data.get("csrfToken", "")
    url = "https://voiceslab.io/api/create-voice"
    headers = {
        "User-Agent": generate_user_agent(),
        "origin": "https://voiceslab.io",
        "referer": "https://voiceslab.io/ar/dashboard/voice-cloning",
        "accept-language": "ar",
        "Cookie": cookie,
        "x-csrf-token": csrf
    }
    title = "v_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    payload = {"title": title, "language": "ar"}
    try:
        time.sleep(random.uniform(0.2, 0.6))
        with open(audio_path, "rb") as f:
            files = [("audio", ("audio.mp3", f, "audio/mpeg"))]
            r = requests.post(url, data=payload, files=files, headers=headers, timeout=20)
        result = r.json()
        if "voiceId" in result:
            return result["voiceId"]
    except: pass
    new_session = create_new_session()
    cookie = get_cookie_header(new_session)
    csrf = new_session.get("csrfToken", "")
    headers["Cookie"] = cookie
    headers["x-csrf-token"] = csrf
    payload["title"] = "v_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    try:
        time.sleep(random.uniform(0.3, 0.8))
        with open(audio_path, "rb") as f:
            files = [("audio", ("audio.mp3", f, "audio/mpeg"))]
            r = requests.post(url, data=payload, files=files, headers=headers, timeout=20)
        result = r.json()
        if "voiceId" in result:
            return result["voiceId"]
    except: pass
    return None

def check_credits(cookie_header):
    try:
        r = requests.post("https://voiceslab.io/api/get-user-info",
                          headers={"User-Agent": str(generate_user_agent()), "Cookie": cookie_header,
                                   "origin": "https://voiceslab.io", "referer": "https://voiceslab.io/ar/dashboard/voice-cloning"},
                          timeout=10)
        return r.json()["data"]["clone_credits"]["left_credits"] < 50
    except:
        return True

def clone_text(text, voice_id, cookie_header):
    url = "https://voiceslab.io/api/clone-voice"
    payload = {"text": text, "voiceId": voice_id, "isPublicVoice": False, "settings": {"rate": 0, "volume": 0, "pitch": 0}}
    headers = {"User-Agent": str(generate_user_agent()), "Content-Type": "application/json", "Cookie": cookie_header,
               "origin": "https://voiceslab.io", "referer": "https://voiceslab.io/ar/dashboard/text-to-speech"}
    r = requests.post(url, json=payload, headers=headers, timeout=25)
    return r.json().get("audioUrl")

def download_and_convert_async(audio_url, user_id, chat_id, msg_id, bot_instance):
    mp3_path = f"/tmp/temp_{user_id}.mp3"
    ogg_path = f"/tmp/voice_{user_id}.ogg"
    success = False
    try:
        r = requests.get(audio_url, timeout=30)
        with open(mp3_path, "wb") as f: f.write(r.content)
        subprocess.run(["ffmpeg", "-y", "-i", mp3_path, "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "16k", ogg_path],
                       check=True, capture_output=True)
        if os.path.exists(ogg_path):
            with open(ogg_path, "rb") as f:
                bot_instance.send_voice(chat_id, f)
            os.remove(ogg_path)
            success = True
    except:
        pass
    finally:
        if os.path.exists(mp3_path): os.remove(mp3_path)
        if success:
            bot_instance.edit_message_text("✅ تم الاستنساخ", chat_id, msg_id)
            update_stats(user_id)
        else:
            bot_instance.edit_message_text("❌ فشل تجهيز الصوت. حاول مرة أخرى.", chat_id, msg_id)

# ========== البوت ==========
bot = telebot.TeleBot(TOKEN)
user_sessions = {}
user_state = {}
temp_activation = {}
pending_voice = {}

def reset_user(uid):
    user_state.pop(uid, None)
    user_sessions.pop(uid, None)
    pending_voice.pop(uid, None)

def main_menu_keyboard():
    mk = telebot.types.InlineKeyboardMarkup(row_width=1)
    mk.add(telebot.types.InlineKeyboardButton("⚜️ الأمر الصوتي", callback_data="start_clone"),
           telebot.types.InlineKeyboardButton("🗃️ خزنة الأصوات", callback_data="manage_voices"),
           telebot.types.InlineKeyboardButton("📊 إحصائياتي", callback_data="my_stats"),
           telebot.types.InlineKeyboardButton("📤 مشاركة الصوت", callback_data="share_voice"))
    return mk

def after_save_menu():
    mk = telebot.types.InlineKeyboardMarkup(row_width=1)
    mk.add(telebot.types.InlineKeyboardButton("🎙️ التكملة على نفس الصوت", callback_data="continue_same"),
           telebot.types.InlineKeyboardButton("🆕 رفع صوت جديد", callback_data="send_new"),
           telebot.types.InlineKeyboardButton("🗃️ خزنة الأصوات", callback_data="manage_voices"),
           telebot.types.InlineKeyboardButton("↩️ القائمة الرئيسية", callback_data="main_menu"))
    return mk

def is_authorized(uid, sets):
    if uid == ADMIN_ID: return True
    if sets.get("status") == "free": return True
    if str(uid) in sets.get("blocked_users", []): return False
    return sets.get("paid_users", {}).get(str(uid), False)

@bot.message_handler(commands=['start'])
def cmd_start(msg):
    uid = msg.from_user.id
    reset_user(uid)
    sets = load_settings()
    name = msg.from_user.first_name or "صديقنا"
    if str(uid) in sets.get("blocked_users", []):
        bot.send_message(uid, "عذراً، لقد تم حظرك من استخدام البوت.")
        return
    if uid == ADMIN_ID:
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        st = sets.get("status", "free")
        toggle = "💰 البوت مدفوع (اضغط للتحويل إلى مجاني)" if st == "paid" else "🆓 البوت مجاني (اضغط للتحويل إلى مدفوع)"
        markup.add(telebot.types.InlineKeyboardButton(toggle, callback_data=f"toggle_{st}"),
                   telebot.types.InlineKeyboardButton("⚙️ إدارة المستخدمين", callback_data="admin_users"),
                   telebot.types.InlineKeyboardButton("📊 إحصائيات البوت", callback_data="admin_stats"))
        bot.send_message(uid, "لوحة تحكم الأدمن:", reply_markup=markup)
    if not is_authorized(uid, sets):
        bot.send_message(uid, f"عذراً {name}، البوت مدفوع حالياً. راسل المطور.")
        return
    entry = get_user_entry(uid)
    voices_count = len(entry["voices"])
    text = f"🏆 **مرحباً {name} في العرش الصوتي**\n🎙️ الأصوات: {voices_count}/{MAX_VOICES}\n\n⚜️ اختر أمراً:"
    bot.send_message(uid, text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")

@bot.message_handler(commands=['cancel'])
def cmd_cancel(msg):
    uid = msg.from_user.id
    reset_user(uid)
    bot.send_message(uid, "✅ تم إلغاء العملية.", reply_markup=main_menu_keyboard())

@bot.message_handler(commands=['block'])
def cmd_block(msg):
    uid = msg.from_user.id
    if uid != ADMIN_ID:
        bot.reply_to(msg, "غير مصرح لك.")
        return
    parts = msg.text.split()
    if len(parts) < 2:
        bot.reply_to(msg, "استخدم: /block [معرف_المستخدم أو @اسم_المستخدم]")
        return
    target = parts[1]
    sets = load_settings()
    target_id = None
    if target.startswith("@"):
        # البحث عن المستخدم باستخدام اسم المستخدم
        username = target[1:]
        # نحتاج لتجربة إيجاد المستخدم، سنطلب من الأدمن استخدام ID
        bot.reply_to(msg, "لحظر مستخدم باستخدام @username، استخدم معرفه الرقمي (ID).")
        return
    else:
        try:
            target_id = int(target)
        except:
            bot.reply_to(msg, "معرف غير صالح.")
            return
    blocked = sets.get("blocked_users", [])
    if str(target_id) in blocked:
        bot.reply_to(msg, "المستخدم محظور بالفعل.")
        return
    blocked.append(str(target_id))
    sets["blocked_users"] = blocked
    save_settings(sets)
    bot.reply_to(msg, f"✅ تم حظر المستخدم {target_id}.")

@bot.message_handler(commands=['unblock'])
def cmd_unblock(msg):
    uid = msg.from_user.id
    if uid != ADMIN_ID:
        bot.reply_to(msg, "غير مصرح لك.")
        return
    parts = msg.text.split()
    if len(parts) < 2:
        bot.reply_to(msg, "استخدم: /unblock [معرف_المستخدم]")
        return
    try:
        target_id = int(parts[1])
    except:
        bot.reply_to(msg, "معرف غير صالح.")
        return
    sets = load_settings()
    blocked = sets.get("blocked_users", [])
    if str(target_id) not in blocked:
        bot.reply_to(msg, "المستخدم ليس محظوراً.")
        return
    blocked.remove(str(target_id))
    sets["blocked_users"] = blocked
    save_settings(sets)
    bot.reply_to(msg, f"✅ تم إلغاء حظر المستخدم {target_id}.")

@bot.message_handler(commands=['broadcast'])
def cmd_broadcast(msg):
    uid = msg.from_user.id
    if uid != ADMIN_ID:
        bot.reply_to(msg, "غير مصرح لك.")
        return
    text = msg.text.replace("/broadcast", "").strip()
    if not text:
        bot.reply_to(msg, "استخدم: /broadcast [الرسالة]")
        return
    # جلب جميع المستخدمين من قاعدة البيانات
    try:
        r = supabase.table("user_data").select("user_id").execute()
        users = r.data or []
        count = 0
        for u in users:
            try:
                bot.send_message(int(u["user_id"]), f"📢 رسالة من المطور:\n{text}")
                count += 1
                time.sleep(0.05)  # لتجنب حد المعدل
            except: pass
        bot.reply_to(msg, f"✅ تم إرسال الرسالة إلى {count} مستخدم.")
    except Exception as e:
        bot.reply_to(msg, f"❌ حدث خطأ: {e}")

@bot.message_handler(commands=['blocked_list'])
def cmd_blocked_list(msg):
    uid = msg.from_user.id
    if uid != ADMIN_ID:
        bot.reply_to(msg, "غير مصرح لك.")
        return
    sets = load_settings()
    blocked = sets.get("blocked_users", [])
    if not blocked:
        bot.reply_to(msg, "لا يوجد مستخدمون محظورون.")
        return
    bot.reply_to(msg, f"📋 قائمة المحظورين:\n" + "\n".join(blocked))

@bot.message_handler(content_types=['audio', 'voice'])
def handle_audio(msg):
    uid = msg.from_user.id
    sets = load_settings()
    if str(uid) in sets.get("blocked_users", []):
        bot.reply_to(msg, "عذراً، لقد تم حظرك.")
        return
    if not is_authorized(uid, sets): return
    if user_state.get(uid) != "waiting_for_audio":
        bot.reply_to(msg, "اضغط 'رفع صوت جديد' أولاً.")
        return

    media = msg.audio if msg.audio else msg.voice
    if msg.audio and media.mime_type != "audio/mpeg":
        bot.reply_to(msg, "أرسل ملف MP3 فقط."); return
    if not (MIN_DURATION <= media.duration <= MAX_DURATION):
        bot.reply_to(msg, f"المدة يجب أن تكون بين {MIN_DURATION}-{MAX_DURATION} ثانية."); return

    status_msg = bot.reply_to(msg, "⏳ جارٍ رفع الصوت...")

    file_info = bot.get_file(media.file_id)
    audio_bytes = bot.download_file(file_info.file_path)
    path = f"/tmp/upload_{uid}_{int(time.time())}.mp3"
    with open(path, "wb") as f: f.write(audio_bytes)

    bot.edit_message_text("⏳ جارٍ الرفع إلى الخادم...", msg.chat.id, status_msg.message_id)

    session = get_valid_session()
    vid = upload_voice_file(path, session)

    if os.path.exists(path): os.remove(path)

    if not vid:
        bot.edit_message_text("❌ فشل رفع الصوت. حاول مرة أخرى.", msg.chat.id, status_msg.message_id)
        reset_user(uid)
        return

    pending_voice[uid] = {"voice_id": vid, "cookie_header": get_cookie_header(session)}
    user_state[uid] = "waiting_for_name"

    mk = telebot.types.InlineKeyboardMarkup()
    mk.add(telebot.types.InlineKeyboardButton("تخطي (اسم افتراضي)", callback_data="skip_name"))
    bot.edit_message_text("✅ تم الرفع بنجاح!\nأرسل اسمًا للصوت أو اضغط تخطي:", msg.chat.id, status_msg.message_id, reply_markup=mk)

@bot.message_handler(content_types=['text'])
def handle_text(msg):
    uid = msg.from_user.id; text = msg.text

    if uid == ADMIN_ID and uid in temp_activation:
        handle_admin_activation(msg); return

    sets = load_settings()
    if str(uid) in sets.get("blocked_users", []):
        bot.reply_to(msg, "عذراً، لقد تم حظرك.")
        return
    if not is_authorized(uid, sets):
        bot.reply_to(msg, "البوت مدفوع حالياً."); return

    if user_state.get(uid) == "waiting_for_name":
        if uid not in pending_voice:
            bot.reply_to(msg, "انتهت الجلسة."); reset_user(uid); return
        vdata = pending_voice.pop(uid)
        voice_id = vdata["voice_id"]

        status_msg = bot.reply_to(msg, "⏳ جارٍ الحفظ...")
        if vdata.get("rename"):
            new_name = text.strip()[:30] or "بدون اسم"
            rename_user_voice(uid, voice_id, new_name)
            bot.edit_message_text(f"✅ تم تغيير الاسم إلى '{new_name}'.",
                                  msg.chat.id, status_msg.message_id,
                                  reply_markup=after_save_menu())
        else:
            name = text.strip()[:30] or "صوت " + str(len(get_user_entry(uid)["voices"]) + 1)
            save_user_voice(uid, voice_id, name)
            set_active_voice(uid, voice_id)
            get_voice_ready(uid, voice_id)
            bot.edit_message_text(f"✅ تم حفظ '{name}' وتفعيله.\nيمكنك الآن إرسال النص مباشرة.",
                                  msg.chat.id, status_msg.message_id,
                                  reply_markup=after_save_menu())
        return

    active = get_active_voice(uid)
    if active:
        if user_state.get(uid) != "ready":
            get_voice_ready(uid, active["id"])
        sess = user_sessions.get(uid)
        if not sess:
            bot.reply_to(msg, "انتهت الجلسة، ابدأ من جديد."); reset_user(uid); return

        vid = sess["voice_id"]
        cookie = sess["cookie_header"]

        if check_credits(cookie):
            new_sess = get_valid_session()
            cookie = get_cookie_header(new_sess)
            sess["cookie_header"] = cookie

        wait = bot.reply_to(msg, "🔄 جارٍ الاستنساخ...")
        audio_url = clone_text(text, vid, cookie)
        if not audio_url:
            bot.edit_message_text("❌ فشل الاستنساخ. حاول لاحقاً.", msg.chat.id, wait.message_id)
            return
        threading.Thread(target=download_and_convert_async, args=(audio_url, uid, msg.chat.id, wait.message_id, bot)).start()
    else:
        bot.reply_to(msg, "لا يوجد صوت نشط. اختر 'الأمر الصوتي' لإضافة صوت.")

def handle_admin_activation(msg):
    uid = msg.from_user.id; target = temp_activation[uid]
    try:
        target_id = int(msg.text.strip())
    except ValueError:
        bot.reply_to(msg, "أرسل ID صحيح."); return
    sets = load_settings()
    if target == 'activate':
        sets['paid_users'][str(target_id)] = True
        bot.reply_to(msg, f"✅ تم تفعيل المستخدم {target_id}.")
    elif target == 'deactivate':
        sets['paid_users'].pop(str(target_id), None)
        bot.reply_to(msg, f"❌ تم إلغاء تفعيل المستخدم {target_id}.")
    save_settings(sets)
    del temp_activation[uid]

def get_voice_ready(user_id, voice_id):
    session = get_valid_session()
    cookie = get_cookie_header(session)
    user_sessions[user_id] = {"voice_id": voice_id, "cookie_header": cookie}
    user_state[user_id] = "ready"

# ==================== الأزرار ====================
@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    uid = call.from_user.id
    data = call.data

    sets = load_settings()
    if str(uid) in sets.get("blocked_users", []):
        bot.answer_callback_query(call.id, "عذراً، لقد تم حظرك."); return

    if data == "start_clone":
        if not is_authorized(uid, sets):
            bot.answer_callback_query(call.id, "عذراً، البوت مدفوع."); return
        active = get_active_voice(uid)
        if active:
            mk = telebot.types.InlineKeyboardMarkup(row_width=1)
            mk.add(telebot.types.InlineKeyboardButton("🎤 استخدام الصوت النشط", callback_data="use_active_voice"),
                   telebot.types.InlineKeyboardButton("🆕 رفع صوت جديد", callback_data="send_new"),
                   telebot.types.InlineKeyboardButton("🔊 تشغيل تجربة", callback_data="test_voice"),
                   telebot.types.InlineKeyboardButton("↩️ رجوع", callback_data="main_menu"))
            bot.edit_message_text(f"⚜️ الصوت النشط: **{active['name']}**", call.message.chat.id, call.message.message_id,
                                  reply_markup=mk, parse_mode="Markdown")
        else:
            user_state[uid] = "waiting_for_audio"
            mk = telebot.types.InlineKeyboardMarkup()
            mk.add(telebot.types.InlineKeyboardButton("↩️ رجوع", callback_data="main_menu"))
            bot.edit_message_text("🎙️ أرسل مقطعًا صوتيًا (10-60 ثانية):", call.message.chat.id, call.message.message_id, reply_markup=mk)

    elif data == "use_active_voice":
        active = get_active_voice(uid)
        if not active:
            bot.answer_callback_query(call.id, "لا يوجد صوت نشط."); return
        get_voice_ready(uid, active["id"])
        bot.send_message(uid, f"✅ الصوت '{active['name']}' جاهز. أرسل النص للاستنساخ.", reply_markup=main_menu_keyboard())

    elif data == "send_new":
        entry = get_user_entry(uid)
        if len(entry["voices"]) >= MAX_VOICES:
            bot.answer_callback_query(call.id, "الحد الأقصى 5 أصوات. احذف صوتًا قديمًا.", show_alert=True)
            return
        user_state[uid] = "waiting_for_audio"
        mk = telebot.types.InlineKeyboardMarkup()
        mk.add(telebot.types.InlineKeyboardButton("↩️ رجوع", callback_data="main_menu"))
        bot.edit_message_text("🎙️ أرسل الصوتية الجديدة:", call.message.chat.id, call.message.message_id, reply_markup=mk)

    elif data == "test_voice":
        active = get_active_voice(uid)
        if not active:
            bot.answer_callback_query(call.id, "لا يوجد صوت نشط."); return
        get_voice_ready(uid, active["id"])
        sess = user_sessions.get(uid)
        if not sess:
            bot.answer_callback_query(call.id, "خطأ في الجلسة."); return
        wait = bot.send_message(uid, "🔄 جارٍ توليد عينة تجريبية...")
        audio_url = clone_text("مرحباً، هذا اختبار لصوتي", active["id"], sess["cookie_header"])
        if audio_url:
            threading.Thread(target=download_and_convert_async, args=(audio_url, uid, uid, wait.message_id, bot)).start()
        else:
            bot.edit_message_text("❌ فشل الاختبار.", uid, wait.message_id)

    elif data == "manage_voices":
        show_user_voices(uid, call.message)

    elif data == "my_stats":
        stats = load_stats()
        u = stats["users"].get(str(uid), {"count": 0})
        text = f"📊 **إحصائياتك:**\n- عدد الاستنساخات: {u.get('count', 0)}"
        mk = telebot.types.InlineKeyboardMarkup()
        mk.add(telebot.types.InlineKeyboardButton("↩️ رجوع", callback_data="main_menu"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=mk, parse_mode="Markdown")

    elif data == "share_voice":
        text = ("📤 **لمشاركة الصوت:**\n"
                "1. اضغط مطوّلاً على الرسالة الصوتية.\n"
                "2. اختر 'مشاركة'.\n"
                "3. اختر التطبيق.\n"
                "ستظهر كرسالة صوتية أصلية.")
        mk = telebot.types.InlineKeyboardMarkup()
        mk.add(telebot.types.InlineKeyboardButton("↩️ رجوع", callback_data="main_menu"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=mk, parse_mode="Markdown")

    elif data == "main_menu":
        reset_user(uid)
        entry = get_user_entry(uid)
        voices_count = len(entry["voices"])
        text = f"🏆 **العرش الصوتي**\n🎙️ الأصوات: {voices_count}/{MAX_VOICES}"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=main_menu_keyboard(), parse_mode="Markdown")

    elif data == "continue_same":
        active = get_active_voice(uid)
        if active:
            get_voice_ready(uid, active["id"])
            bot.send_message(uid, f"✅ الصوت '{active['name']}' جاهز. أرسل النص للاستنساخ.", reply_markup=main_menu_keyboard())
        else:
            bot.send_message(uid, "لا يوجد صوت نشط حالياً. اختر 'الأمر الصوتي'.", reply_markup=main_menu_keyboard())

    elif data.startswith("direct_activate_"):
        voice_id = data[len("direct_activate_"):]
        entry = get_user_entry(uid)
        if entry["active_voice_id"] == voice_id:
            bot.answer_callback_query(call.id, "الصوت مفعّل بالفعل")
            v = next((v for v in entry["voices"] if v["id"] == voice_id), None)
            name = v["name"] if v else "الصوت"
            get_voice_ready(uid, voice_id)
            bot.send_message(uid, f"✅ الصوت '{name}' مفعّل مسبقاً. أرسل كلماتك للتحويل.")
            return
        if set_active_voice(uid, voice_id):
            v = next((v for v in entry["voices"] if v["id"] == voice_id), None)
            name = v["name"] if v else "الصوت"
            get_voice_ready(uid, voice_id)
            bot.answer_callback_query(call.id, f"تم تفعيل '{name}'!")
            bot.send_message(uid, f"✅ الصوت '{name}' مفعّل الآن. أرسل كلماتك للتحويل.")
            show_user_voices(uid, call.message)
        else:
            bot.answer_callback_query(call.id, "فشل التفعيل.")

    elif data.startswith("voice_menu_"):
        voice_id = data[len("voice_menu_"):]
        entry = get_user_entry(uid)
        v = next((v for v in entry["voices"] if v["id"] == voice_id), None)
        if not v:
            bot.answer_callback_query(call.id, "الصوت لم يعد موجوداً."); return
        is_active = entry["active_voice_id"] == voice_id
        mk = telebot.types.InlineKeyboardMarkup(row_width=1)
        if not is_active:
            mk.add(telebot.types.InlineKeyboardButton("✅ تفعيل هذا الصوت", callback_data=f"activate_{voice_id}"))
        mk.add(telebot.types.InlineKeyboardButton("✏️ إعادة تسمية", callback_data=f"rename_{voice_id}"),
               telebot.types.InlineKeyboardButton("🗑️ حذف الصوت", callback_data=f"delete_{voice_id}"),
               telebot.types.InlineKeyboardButton("↩️ عودة للأصوات", callback_data="manage_voices"))
        bot.edit_message_text(f"الصوت: **{v['name']}**", call.message.chat.id, call.message.message_id,
                              reply_markup=mk, parse_mode="Markdown")

    elif data.startswith("activate_"):
        voice_id = data[len("activate_"):]
        if get_user_entry(uid)["active_voice_id"] == voice_id:
            bot.answer_callback_query(call.id, "الصوت مفعل بالفعل")
            return
        if set_active_voice(uid, voice_id):
            get_voice_ready(uid, voice_id)
            bot.answer_callback_query(call.id, "تم التفعيل!")
            show_user_voices(uid, call.message)
            v = next((v for v in get_user_entry(uid)["voices"] if v["id"] == voice_id), None)
            name = v["name"] if v else "الصوت"
            bot.send_message(uid, f"✅ الصوت '{name}' مفعّل الآن. أرسل كلماتك للتحويل.")
        else:
            bot.answer_callback_query(call.id, "فشل التفعيل.")

    elif data.startswith("rename_"):
        voice_id = data[len("rename_"):]
        pending_voice[uid] = {"voice_id": voice_id, "rename": True}
        user_state[uid] = "waiting_for_name"
        mk = telebot.types.InlineKeyboardMarkup()
        mk.add(telebot.types.InlineKeyboardButton("↩️ إلغاء", callback_data=f"voice_menu_{voice_id}"))
        bot.edit_message_text("✏️ أرسل الاسم الجديد:", call.message.chat.id, call.message.message_id, reply_markup=mk)

    elif data.startswith("delete_"):
        voice_id = data[len("delete_"):]
        mk = telebot.types.InlineKeyboardMarkup()
        mk.add(telebot.types.InlineKeyboardButton("🗑️ تأكيد الحذف", callback_data=f"confirm_delete_{voice_id}"),
               telebot.types.InlineKeyboardButton("↩️ إلغاء", callback_data=f"voice_menu_{voice_id}"))
        bot.edit_message_text("⚠️ هل أنت متأكد من حذف الصوت؟", call.message.chat.id, call.message.message_id, reply_markup=mk)

    elif data.startswith("confirm_delete_"):
        voice_id = data[len("confirm_delete_"):]
        delete_user_voice(uid, voice_id)
        bot.answer_callback_query(call.id, "تم الحذف.")
        show_user_voices(uid, call.message)

    elif data == "skip_name":
        if uid in pending_voice:
            vdata = pending_voice.pop(uid)
            if vdata.get("rename"):
                bot.answer_callback_query(call.id, "تم الإبقاء على الاسم.")
                show_user_voices(uid, call.message)
            else:
                name = "صوت " + str(len(get_user_entry(uid)["voices"]) + 1)
                save_user_voice(uid, vdata["voice_id"], name)
                set_active_voice(uid, vdata["voice_id"])
                get_voice_ready(uid, vdata["voice_id"])
                bot.answer_callback_query(call.id, f"تم الحفظ باسم '{name}'.")
                bot.send_message(uid, f"✅ الصوت '{name}' جاهز. أرسل النص للاستنساخ.", reply_markup=after_save_menu())
        else:
            bot.answer_callback_query(call.id, "انتهت الجلسة.")

    elif data == "send_new_from_manage":
        entry = get_user_entry(uid)
        if len(entry["voices"]) >= MAX_VOICES:
            bot.answer_callback_query(call.id, "الحد الأقصى 5 أصوات. احذف صوتًا قديمًا أولاً.", show_alert=True)
            return
        user_state[uid] = "waiting_for_audio"
        mk = telebot.types.InlineKeyboardMarkup()
        mk.add(telebot.types.InlineKeyboardButton("↩️ رجوع", callback_data="manage_voices"))
        bot.edit_message_text("🎙️ أرسل الصوتية الجديدة:", call.message.chat.id, call.message.message_id, reply_markup=mk)

    # --- أزرار الأدمن ---
    elif data.startswith("toggle_"):
        if uid != ADMIN_ID: return
        cur = data.split("_")[1]
        new = "paid" if cur == "free" else "free"
        sets["status"] = new; save_settings(sets)
        bot.edit_message_text(f"✅ حالة البوت الآن: {new}", call.message.chat.id, call.message.message_id,
                              reply_markup=admin_markup(sets))
    elif data == "admin_users":
        if uid != ADMIN_ID: return
        cnt = len([v for v in sets.get("paid_users",{}).values() if v])
        blocked_cnt = len(sets.get("blocked_users", []))
        mk = telebot.types.InlineKeyboardMarkup(row_width=1)
        mk.add(telebot.types.InlineKeyboardButton("➕ تفعيل مستخدم", callback_data="admin_activate"),
               telebot.types.InlineKeyboardButton("➖ إلغاء تفعيل", callback_data="admin_deactivate"),
               telebot.types.InlineKeyboardButton("🚫 قائمة المحظورين", callback_data="admin_blocked_list"),
               telebot.types.InlineKeyboardButton("↩️ رجوع", callback_data="admin_back"))
        bot.edit_message_text(f"👥 المدفوعين: {cnt}\n🚫 المحظورين: {blocked_cnt}", call.message.chat.id, call.message.message_id, reply_markup=mk)
    elif data == "admin_blocked_list":
        if uid != ADMIN_ID: return
        blocked = sets.get("blocked_users", [])
        text = "🚫 قائمة المحظورين:\n" + "\n".join(blocked) if blocked else "لا يوجد محظورون."
        mk = telebot.types.InlineKeyboardMarkup()
        mk.add(telebot.types.InlineKeyboardButton("↩️ رجوع", callback_data="admin_users"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=mk)
    elif data == "admin_activate":
        temp_activation[uid] = 'activate'
        bot.send_message(uid, "أرسل ID المستخدم لتفعيله:")
    elif data == "admin_deactivate":
        temp_activation[uid] = 'deactivate'
        bot.send_message(uid, "أرسل ID المستخدم لإلغاء تفعيله:")
    elif data == "admin_stats":
        if uid != ADMIN_ID: return
        stats = load_stats()
        text = f"📊 إجمالي المستخدمين: {stats['total_users']}\n📊 إجمالي الاستخدامات: {stats['usage_count']}"
        mk = telebot.types.InlineKeyboardMarkup()
        mk.add(telebot.types.InlineKeyboardButton("↩️ رجوع", callback_data="admin_back"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=mk)
    elif data == "admin_back":
        bot.edit_message_text("لوحة تحكم الأدمن:", call.message.chat.id, call.message.message_id,
                              reply_markup=admin_markup(sets))

def show_user_voices(uid, message):
    entry = get_user_entry(uid)
    voices = entry["voices"]
    active_id = entry["active_voice_id"]
    mk = telebot.types.InlineKeyboardMarkup(row_width=1)
    if not voices:
        text = "🗃️ لا توجد أصوات محفوظة."
        mk.add(telebot.types.InlineKeyboardButton("🎤 إضافة صوت جديد", callback_data="send_new_from_manage"))
    else:
        text = f"🗃️ **الأصوات ({len(voices)}/{MAX_VOICES}):**\n"
        for i, v in enumerate(voices, 1):
            marker = "✅" if v["id"] == active_id else "  "
            text += f"{marker} {i}. {v['name']}\n"
        text += "\nاضغط على الاسم للتفعيل المباشر، أو 'خيارات' للإدارة."
        for v in voices:
            mk.add(telebot.types.InlineKeyboardButton(f"🎤 {v['name']}", callback_data=f"direct_activate_{v['id']}"))
        for v in voices:
            mk.add(telebot.types.InlineKeyboardButton(f"⚙️ خيارات {v['name']}", callback_data=f"voice_menu_{v['id']}"))
        mk.add(telebot.types.InlineKeyboardButton("🎤 إضافة صوت جديد", callback_data="send_new_from_manage"))
    mk.add(telebot.types.InlineKeyboardButton("↩️ القائمة الرئيسية", callback_data="main_menu"))

    try:
        bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=mk, parse_mode="Markdown")
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" not in str(e):
            raise

def admin_markup(sets):
    st = sets.get("status", "free")
    toggle = "💰 مدفوع (اضغط للتحويل لمجاني)" if st == "paid" else "🆓 مجاني (اضغط للتحويل لمدفوع)"
    mk = telebot.types.InlineKeyboardMarkup(row_width=1)
    mk.add(telebot.types.InlineKeyboardButton(toggle, callback_data=f"toggle_{st}"),
           telebot.types.InlineKeyboardButton("⚙️ إدارة المستخدمين", callback_data="admin_users"),
           telebot.types.InlineKeyboardButton("📊 إحصائيات البوت", callback_data="admin_stats"))
    return mk

if __name__ == '__main__':
    print("✅ بوت استنساخ الصوت يعمل على Supabase!")
    # للتشغيل المحلي استخدم polling، وللـ Render استخدم webhook
    port = int(os.environ.get("PORT", 8080))
    bot.remove_webhook()
    time.sleep(0.5)
    bot.set_webhook(url=f"{os.environ.get('RENDER_EXTERNAL_URL', 'https://your-app.onrender.com')}/")
    from flask import Flask, request
    app = Flask(__name__)
    @app.route('/', methods=['POST'])
    def webhook():
        if request.headers.get('content-type') == 'application/json':
            json_string = request.get_data().decode('utf-8')
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
            return '!', 200
        return 'Bad request', 400
    app.run(host='0.0.0.0', port=port)
