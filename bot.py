"""
Джура — постійна частина (Telegram-бот на Render).

Працює цілодобово, навіть коли сайт Джури закритий:
  • нагадування Джури приходять у Telegram вчасно;
  • стежить за повітряною тривогою у твоїй області;
  • щоранку надсилає звіт, а ввечері — підсумок звичок;
  • стежить за посилками Нової Пошти;
  • розуміє повідомлення й голосові в Telegram: покупки й розклад у Пульті,
    звички, сон і фінанси в трекері, нагадування, нотатки, погода, курс тощо.

Усі ключі (Telegram, Gemini, тривоги, Нова Пошта…) бот бере з налаштувань самого Джури у Firebase.
На Render потрібні лише дві змінні середовища: FIREBASE_SA_JSON і PULT_PASSWORD.
"""
import asyncio, base64, json, os, random, re, time, logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
import firebase_admin
from firebase_admin import credentials, firestore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dzhura")

KYIV = ZoneInfo("Europe/Kyiv")
PORT = int(os.environ.get("PORT", "10000"))
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PULT_PASSWORD = os.environ.get("PULT_PASSWORD", "")

PULT = {"apiKey": "AIzaSyBuUZmVUsXqOBxUY_bN1f7DlmFaFsnqou8", "projectId": "familyromashov"}
TRACKER = {"apiKey": "AIzaSyD9PCBz1mt8PWtfk900EsuqTbPPBO-xMXA", "projectId": "chess-life-tracker"}

# ---------------------------------------------------------------- Firebase Джури (сервісний ключ)
cred = credentials.Certificate(json.loads(os.environ["FIREBASE_SA_JSON"]))
firebase_admin.initialize_app(cred)
db = firestore.client()

S = {"uid": None, "doc": {}, "session": None}   # спільний стан


def now():
    return datetime.now(KYIV)


def ymd(d=None):
    return (d or now()).strftime("%Y-%m-%d")


def hhmm(d):
    return d.strftime("%H:%M")


def settings():
    return S["doc"].get("settings") or {}


def address():
    return settings().get("address") or "Пане Антоне"


def addr_low():
    a = address()
    return a[0].lower() + a[1:]


def plural(n, one, few, many):
    n = abs(int(n)); a = n % 100; b = n % 10
    if 10 < a < 20: return many
    if b == 1: return one
    if 2 <= b <= 4: return few
    return many


def money(v):
    return f"{round(v):,}".replace(",", " ")


def norm(s):
    s = str(s or "").lower().replace("’", "'").replace("ʼ", "'").replace("`", "'")
    s = re.sub(r"[^a-zа-яіїєґ0-9' ]+", " ", s).replace("'", "")
    return re.sub(r"\s+", " ", s).strip()


def stem(w):
    w = norm(w)
    return w[:4] if len(w) > 4 else w[:max(2, len(w) - 1)]


# ---------------------------------------------------------------- Документ Джури
def _find_uid():
    want = os.environ.get("DZHURA_UID")
    if want: return want
    docs = list(db.collection("dzhura").stream())
    if not docs: raise RuntimeError("У Firebase Джури немає даних. Увійди в Джуру через Google хоча б раз.")
    docs.sort(key=lambda d: 0 if (d.to_dict().get("settings") or {}).get("tgChat") else 1)
    return docs[0].id


def _load_doc():
    snap = db.collection("dzhura").document(S["uid"]).get()
    return snap.to_dict() or {}


async def refresh_doc():
    S["doc"] = await asyncio.to_thread(_load_doc)
    return S["doc"]


def _bot_state_get():
    return db.collection("dzhuraBot").document(S["uid"]).get().to_dict() or {}


def _bot_state_set(patch):
    db.collection("dzhuraBot").document(S["uid"]).set(patch, merge=True)


async def bot_state():
    return await asyncio.to_thread(_bot_state_get)


async def bot_state_set(patch):
    await asyncio.to_thread(_bot_state_set, patch)


def _tx_update_list(field, fn, default=list):
    """Атомарно змінює поле в документі Джури (нотатки, нагадування, авто) і позначає, що зміна з сервера."""
    ref = db.collection("dzhura").document(S["uid"])

    @firestore.transactional
    def run(tx):
        d = ref.get(transaction=tx).to_dict() or {}
        old = d.get(field) or default()
        new, result = fn(json.loads(json.dumps(old)))
        if new is not None:
            tx.update(ref, {field: new, "from": "server"})
        return result
    return run(db.transaction())


async def update_list(field, fn):
    return await asyncio.to_thread(_tx_update_list, field, fn)


async def update_dict(field, fn):
    return await asyncio.to_thread(_tx_update_list, field, fn, dict)


# ---------------------------------------------------------------- HTTP
async def http_json(method, url, **kw):
    async with S["session"].request(method, url, timeout=aiohttp.ClientTimeout(total=30), **kw) as r:
        txt = await r.text()
        try:
            data = json.loads(txt) if txt else {}
        except Exception:
            data = {"raw": txt}
        return r.status, data


# ---------------------------------------------------------------- Telegram
def tg_url(method):
    tok = os.environ.get("TELEGRAM_TOKEN") or settings().get("tgToken")
    return f"https://api.telegram.org/bot{tok}/{method}"


async def tg_send(text, chat=None, markup=None):
    chat = chat or settings().get("tgChat")
    if not chat: return
    parts = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for i, part in enumerate(parts):
        body = {"chat_id": chat, "text": part, "disable_web_page_preview": True}
        if markup and i == len(parts) - 1: body["reply_markup"] = markup
        await http_json("POST", tg_url("sendMessage"), json=body)


async def tg_action(chat, action="typing"):
    await http_json("POST", tg_url("sendChatAction"), json={"chat_id": chat, "action": action})


# ---------------------------------------------------------------- Firestore REST (Пульт і Трекер)
def fs_enc(v):
    if v is None: return {"nullValue": None}
    if isinstance(v, bool): return {"booleanValue": v}
    if isinstance(v, int): return {"integerValue": str(v)}
    if isinstance(v, float): return {"integerValue": str(int(v))} if v.is_integer() else {"doubleValue": v}
    if isinstance(v, str): return {"stringValue": v}
    if isinstance(v, list): return {"arrayValue": {"values": [fs_enc(x) for x in v]}}
    if isinstance(v, dict): return {"mapValue": {"fields": {k: fs_enc(x) for k, x in v.items()}}}
    return {"stringValue": str(v)}


def fs_dec(v):
    if "nullValue" in v: return None
    if "booleanValue" in v: return v["booleanValue"]
    if "integerValue" in v: return int(v["integerValue"])
    if "doubleValue" in v: return v["doubleValue"]
    if "stringValue" in v: return v["stringValue"]
    if "timestampValue" in v: return v["timestampValue"]
    if "arrayValue" in v: return [fs_dec(x) for x in v["arrayValue"].get("values", [])]
    if "mapValue" in v: return {k: fs_dec(x) for k, x in v["mapValue"].get("fields", {}).items()}
    return None


def fs_fields(doc):
    return {k: fs_dec(v) for k, v in (doc.get("fields") or {}).items()}


def fp(*segs):
    return ".".join(s if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", s) else "`" + s.replace("`", "\\`") + "`" for s in segs)


def nested(segs, value):
    v = fs_enc(value)
    for s in reversed(segs[1:]):
        v = {"mapValue": {"fields": {s: v}}}
    return {segs[0]: v}


def fs_base(cfg):
    return f"https://firestore.googleapis.com/v1/projects/{cfg['projectId']}/databases/(default)/documents"


# --- Пульт: вхід поштою й паролем, як у самому Пульті
PULT_AUTH = {"token": None, "exp": 0, "email": None}


async def pult_token():
    if PULT_AUTH["token"] and time.time() < PULT_AUTH["exp"]: return PULT_AUTH["token"]
    email = settings().get("pultEmail")
    if not email or not PULT_PASSWORD:
        raise RuntimeError("Пульт не підключений до бота: потрібні пошта в налаштуваннях Джури і PULT_PASSWORD на Render")
    st, d = await http_json("POST", f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={PULT['apiKey']}",
                            json={"email": email, "password": PULT_PASSWORD, "returnSecureToken": True})
    if st != 200: raise RuntimeError("Пульт не пустив: перевір пароль PULT_PASSWORD")
    PULT_AUTH.update(token=d["idToken"], exp=time.time() + int(d.get("expiresIn", 3600)) - 300, email=email.lower())
    return PULT_AUTH["token"]


async def pult_list(col):
    tok = await pult_token(); out = []; page = None
    while True:
        url = f"{fs_base(PULT)}/{col}?pageSize=300" + (f"&pageToken={page}" if page else "")
        st, d = await http_json("GET", url, headers={"Authorization": f"Bearer {tok}"})
        if st != 200: raise RuntimeError(f"Пульт: {col} — помилка {st}")
        out += [fs_fields(x) for x in d.get("documents", [])]
        page = d.get("nextPageToken")
        if not page: return out


async def pult_set(col, doc_id, data):
    tok = await pult_token()
    st, d = await http_json("PATCH", f"{fs_base(PULT)}/{col}/{doc_id}", headers={"Authorization": f"Bearer {tok}"},
                            json={"fields": {k: fs_enc(v) for k, v in data.items()}})
    if st != 200: raise RuntimeError(f"Пульт не записав ({st})")


async def pult_update(col, doc_id, patch):
    tok = await pult_token()
    mask = "&".join(f"updateMask.fieldPaths={k}" for k in patch)
    st, d = await http_json("PATCH", f"{fs_base(PULT)}/{col}/{doc_id}?{mask}", headers={"Authorization": f"Bearer {tok}"},
                            json={"fields": {k: fs_enc(v) for k, v in patch.items()}})
    if st != 200: raise RuntimeError(f"Пульт не оновив ({st})")


def pult_uid():
    return "id" + "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=8)) + format(int(time.time() * 1000), "x")


async def pult_members():
    ms = await pult_list("members")
    return sorted(ms, key=lambda m: m.get("id", ""))


async def pult_me(ms):
    e = PULT_AUTH.get("email") or ""
    m = next((x for x in ms if (x.get("email") or "").lower() == e), None) or next((x for x in ms if x.get("id") == "dad"), None) or (ms[0] if ms else {})
    return m.get("id", "")


def find_member(ms, name):
    if not name: return None
    st = stem(name)
    return next((m for m in ms if any(p.startswith(st) for p in norm(m.get("name")).split())), None)


# --- Трекер: документ users/anton. Якщо задано TRACKER_PASSWORD (+ пошта в налаштуваннях Джури або TRACKER_EMAIL) — заходимо з паролем,
# інакше працюємо як раніше (поки база відкрита). Так можна закрити базу правилами Firebase, не зламавши бота.
TRACKER_DOC = f"{fs_base(TRACKER)}/users/anton"
TRACKER_PASSWORD = os.environ.get("TRACKER_PASSWORD", "")
TRACKER_AUTH = {"token": None, "exp": 0}


async def tracker_headers():
    email = os.environ.get("TRACKER_EMAIL") or settings().get("trackerEmail")
    if not (email and TRACKER_PASSWORD): return {}
    if not (TRACKER_AUTH["token"] and time.time() < TRACKER_AUTH["exp"]):
        st, d = await http_json("POST", f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={TRACKER['apiKey']}",
                                json={"email": email, "password": TRACKER_PASSWORD, "returnSecureToken": True})
        if st != 200: raise RuntimeError("Трекер не пустив бота: перевір пошту й TRACKER_PASSWORD")
        TRACKER_AUTH.update(token=d["idToken"], exp=time.time() + int(d.get("expiresIn", 3600)) - 300)
    return {"Authorization": f"Bearer {TRACKER_AUTH['token']}"}


async def tracker_load():
    st, d = await http_json("GET", f"{TRACKER_DOC}?key={TRACKER['apiKey']}", headers=await tracker_headers())
    if st in (401, 403): raise RuntimeError("Трекер закритий: додай TRACKER_PASSWORD на Render і пошту трекера в налаштуваннях Джури")
    if st != 200: raise RuntimeError("Трекер недоступний")
    t = fs_fields(d)
    for k, dv in (("habits", []), ("data", {}), ("morningLog", {}), ("sleepLog", {}), ("financeLog", []), ("bookLog", []), ("eloHistory", [])):
        t[k] = t.get(k) or dv
    return t


async def tracker_set_path(segs, value):
    st, d = await http_json("PATCH", f"{TRACKER_DOC}?key={TRACKER['apiKey']}&updateMask.fieldPaths={fp(*segs)}",
                            json={"fields": nested(segs, value)}, headers=await tracker_headers())
    if st != 200: raise RuntimeError(f"Трекер не записав ({st})")


async def tracker_append(field, value):
    name = f"projects/{TRACKER['projectId']}/databases/(default)/documents/users/anton"
    st, d = await http_json("POST", f"{fs_base(TRACKER)}:commit?key={TRACKER['apiKey']}", json={"writes": [
        {"transform": {"document": name, "fieldTransforms": [{"fieldPath": field, "appendMissingElements": {"values": [fs_enc(value)]}}]}}]},
        headers=await tracker_headers())
    if st != 200: raise RuntimeError(f"Трекер не записав ({st})")


HABIT_VERBS = {"прочит": "чита", "читав": "чита", "почит": "чита", "погул": "прогул", "гуля": "прогул", "писав": "напис",
               "напис": "напис", "попис": "напис", "кодув": "кодув", "програм": "кодув", "качав": "силов", "тренув": "силов",
               "зарядк": "заряд", "дебют": "дебют", "ендшп": "ендш", "англ": "англ", "когіт": "cogi", "когі": "cogi"}
HABIT_STOP = {"відміть", "відмітити", "познач", "зарахуй", "виконав", "виконала", "зробив", "зробила", "зніми", "скасуй",
              "позначку", "відмітку", "звичку", "трекері", "трекер", "сьогодні", "мені", "для", "хвилин", "хв"}


def habit_stems(s):
    out = []
    for w in norm(s).split():
        if len(w) < 3 or w in HABIT_STOP: continue
        hit = next((v for k, v in HABIT_VERBS.items() if w.startswith(k)), None)
        out.append(hit or w[:4])
    return out


def find_habit(habits, q):
    qs = habit_stems(q)
    best, bs = None, 0
    for h in habits:
        hs = habit_stems(f"{h.get('name', '')} {h.get('cat', '')}")
        sc = sum(1 for x in qs if any(y.startswith(x) or x.startswith(y) for y in hs))
        if sc > bs: best, bs = h, sc
    return best


short = lambda n: re.sub(r"\s*\(.*?\)\s*", " ", n or "").strip()
MORNING = [("wake", "прокинувся вчасно"), ("water", "склянка води"), ("exercise", "зарядка"),
           ("eyes", "вправи для очей"), ("plan", "план на день"), ("read_plan", "прочитати план")]
QUOTES = ["Маленькі кроки щодня ведуть до великих результатів.", "Шахи — це гімнастика розуму. — Блез Паскаль",
          "Без систематичної практики немає таланту. — Михайло Ботвинник", "Те що вимірюється — виконується. — Peter Drucker",
          "Порівнюй себе тільки з тим, ким ти був вчора. — Jordan Peterson", "Переможець — це просто той хто не здався."]


# ---------------------------------------------------------------- Сервіси
WMO = {0: "ясно", 1: "переважно ясно", 2: "мінлива хмарність", 3: "хмарно", 45: "туман", 48: "туман", 51: "легка мряка", 53: "мряка",
       55: "густа мряка", 61: "невеликий дощ", 63: "дощ", 65: "сильний дощ", 71: "невеликий сніг", 73: "сніг", 75: "сильний сніг",
       80: "короткочасні зливи", 81: "зливи", 82: "сильні зливи", 85: "снігопад", 86: "сильний снігопад", 95: "гроза", 96: "гроза з градом", 99: "гроза з градом"}


async def weather(tomorrow=False):
    city = settings().get("city") or "Львів"
    st, g = await http_json("GET", f"https://geocoding-api.open-meteo.com/v1/search?count=1&language=uk&name={city}")
    p = (g.get("results") or [{"name": "Львів", "latitude": 49.8397, "longitude": 24.0297}])[0]
    st, d = await http_json("GET", f"https://api.open-meteo.com/v1/forecast?latitude={p['latitude']}&longitude={p['longitude']}"
                                   "&current=temperature_2m,apparent_temperature,weather_code&daily=temperature_2m_max,temperature_2m_min,"
                                   "precipitation_probability_max,weather_code&timezone=auto&forecast_days=2")
    i = 1 if tomorrow else 0
    dmin, dmax = round(d["daily"]["temperature_2m_min"][i]), round(d["daily"]["temperature_2m_max"][i])
    rain = d["daily"]["precipitation_probability_max"][i]
    sky = WMO.get(d["daily"]["weather_code"][i], "")
    umb = " Парасолю краще взяти." if rain and rain >= 50 else ""
    if tomorrow: return f"Завтра в місті {p['name']} {sky}, від {dmin} до {dmax}°, дощ {rain}%.{umb}"
    c = d["current"]
    return f"Зараз у місті {p['name']} {round(c['temperature_2m'])}°, {WMO.get(c['weather_code'], '')}. Сьогодні від {dmin} до {dmax}°, дощ {rain}%.{umb}"


async def alerts_state():
    tok = settings().get("alertsToken")
    if not tok: return None
    st, d = await http_json("GET", f"https://api.alerts.in.ua/v1/alerts/active.json?token={tok}")
    if st != 200: return None
    obl = (settings().get("oblast") or "Львівська область").replace(" область", "").lower()
    air = [a for a in d.get("alerts", []) if a.get("alert_type") == "air_raid"]
    mine = [a for a in air if obl in (a.get("location_oblast") or a.get("location_title") or "").lower()]
    return {"on": bool(mine), "oblasts": len({a.get("location_title") for a in air if a.get("location_type") == "oblast"})}


async def rates():
    st, d = await http_json("GET", "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json")
    if not isinstance(d, list): return "Курс валют зараз недоступний."
    r = {x["cc"]: x["rate"] for x in d if x.get("cc") in ("USD", "EUR", "PLN")}
    f = lambda v: f"{v:.2f}".replace(".", ",")
    names = (("USD", "долар"), ("EUR", "євро"), ("PLN", "злотий"))
    parts = [f"{n} {f(r[c])}" for c, n in names if c in r]
    return ("Курс НБУ: " + ", ".join(parts) + ".") if parts else "Курс валют зараз недоступний."


async def np_status(numbers):
    key = settings().get("npKey")
    if not key or not numbers: return []
    st, d = await http_json("POST", "https://api.novaposhta.ua/v2.0/json/", json={
        "apiKey": key, "modelName": "TrackingDocument", "calledMethod": "getStatusDocuments",
        "methodProperties": {"Documents": [{"DocumentNumber": n, "Phone": settings().get("npPhone", "")} for n in numbers[:50]]}})
    return d.get("data") or []


# ---------------------------------------------------------------- Gemini (лише для складного й голосових)
async def gemini(parts, system):
    s = settings(); key = s.get("geminiKey")
    if not key: raise RuntimeError("немає ключа Gemini в налаштуваннях Джури")
    chain = []
    for m in [s.get("model") or "gemini-2.5-flash", *[x.strip() for x in str(s.get("fallbackModels", "")).split(",")], "gemini-2.5-flash", "gemini-2.5-flash-lite"]:
        if m and m not in chain: chain.append(m)
    last = ""
    for model in chain:
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": parts}], "generationConfig": {"temperature": 0.7}}
        if model.startswith("gemini-2.5-flash"): body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
        for attempt in range(2):
            st, d = await http_json("POST", f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}", json=body)
            if st == 200:
                return " ".join(p.get("text", "") for p in d.get("candidates", [{}])[0].get("content", {}).get("parts", []) if not p.get("thought")).strip()
            last = (d.get("error") or {}).get("message", str(st))
            if st == 429 and "quota" in last.lower(): break
            if st in (500, 503): await asyncio.sleep(2); continue
            break
    alt = await alt_chat(parts, system)
    if alt is not None: return alt
    await notify_problem("gemini", f"Gemini недоступний ({last or 'ліміт'}), запасний мозок теж не відповів. Працюють лише швидкі команди.")
    raise RuntimeError("Gemini недоступний або вичерпано денний ліміт")


ALT = {"groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
       "openrouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free")}


async def alt_chat(parts, system):
    """Запасний мозок (Groq / OpenRouter) для тексту, коли Gemini недоступний."""
    s = settings(); key = s.get("altKey"); prov = s.get("altProvider") or "groq"
    if not key or prov not in ALT: return None
    text = " ".join(p.get("text", "") for p in parts if p.get("text"))
    if not text or any(p.get("inlineData") for p in parts): return None
    base, model = ALT[prov]
    hdr = {"Authorization": f"Bearer {key}"}
    model = s.get("altModel") or ALT_AUTO.get(prov) or model
    tried = []
    for _ in range(3):
        tried.append(model)
        st, d = await http_json("POST", f"{base}/chat/completions", headers=hdr,
                                json={"model": model, "temperature": 0.7,
                                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]})
        if st == 200:
            return ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip() or None
        msg = str((d.get("error") or {}).get("message", ""))
        if st == 404 or re.search(r"does not exist|not found|no endpoints|decommissioned|do not have access", msg, re.I):
            nxt = await alt_pick(prov, base, hdr, tried)
            if not nxt: return None
            model = ALT_AUTO[prov] = nxt; continue
        return None
    return None


ALT_AUTO = {}
ALT_PREFER = {"groq": ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "meta-llama/llama-4-maverick-17b-128e-instruct",
                       "moonshotai/kimi-k2-instruct", "qwen/qwen3-32b", "openai/gpt-oss-20b", "llama-3.1-8b-instant"],
              "openrouter": ["openai/gpt-oss-120b:free", "meta-llama/llama-3.3-70b-instruct:free", "deepseek/deepseek-chat-v3-0324:free",
                             "qwen/qwen3-235b-a22b:free", "openai/gpt-oss-20b:free"]}


async def alt_pick(prov, base, hdr, exclude):
    """Моделі змінюються — питаємо в сервісу актуальний список і беремо найкращу."""
    st, d = await http_json("GET", f"{base}/models", headers=hdr)
    ids = [m.get("id") for m in (d.get("data") or []) if m.get("active", True) is not False]
    ids = [i for i in ids if i and i not in exclude and not re.search(r"whisper|guard|tts|playai|orpheus|embed|compound|audio", i, re.I)]
    if not ids: return None
    hit = next((i for i in ALT_PREFER.get(prov, []) if i in ids), None)
    if hit: return hit
    if prov == "openrouter": return next((i for i in ids if i.endswith(":free")), None)
    return ids[0]


async def groq_whisper(audio):
    """Розшифровка голосового через Groq Whisper (якщо Gemini вичерпано)."""
    s = settings()
    if s.get("altProvider", "groq") != "groq" or not s.get("altKey"): return None
    form = aiohttp.FormData()
    form.add_field("file", audio, filename="voice.ogg", content_type="audio/ogg")
    form.add_field("model", "whisper-large-v3"); form.add_field("language", "uk"); form.add_field("response_format", "json")
    async with S["session"].post("https://api.groq.com/openai/v1/audio/transcriptions", data=form,
                                 headers={"Authorization": f"Bearer {s['altKey']}"}, timeout=aiohttp.ClientTimeout(total=60)) as r:
        if r.status != 200: return None
        return ((await r.json()).get("text") or "").strip() or None


def system_prompt():
    mem = S["doc"].get("memory") or []
    return (f"Ти — Джура, особистий помічник. Звертайся «{address()}». Відповідай живою українською, коротко (1–4 речення), "
            f"без markdown. Зараз {now().strftime('%d.%m.%Y %H:%M')} (Київ). Ти відповідаєш у Telegram і тут не виконуєш дій — лише радиш і відповідаєш. "
            "Що ти знаєш про господаря: " + ("; ".join(mem) if mem else "поки нічого") + ".")


# ---------------------------------------------------------------- Команди без Gemini
NUMW = {"одну": 1, "одна": 1, "один": 1, "дві": 2, "два": 2, "три": 3, "чотири": 4, "пять": 5, "шість": 6, "сім": 7, "вісім": 8,
        "девять": 9, "десять": 10, "пятнадцять": 15, "двадцять": 20, "тридцять": 30, "сорок": 40, "пів": 0.5, "півгодини": 0.5}


def num(s):
    s = str(s or "").strip().replace(",", ".")
    if re.fullmatch(r"\d+(\.\d+)?", s): return float(s)
    return NUMW.get(norm(s))


def split_items(s):
    return [x.strip() for x in re.split(r"\s*,\s*|\s+(?:і|й|та)\s+", s) if x.strip()]


def nominative(phrase):
    out = []
    for w in phrase.split():
        l = w.lower()
        if l.endswith("ію"): w = w[:-2] + "ія"
        elif re.search(r"[бвгґджзклмнпрстфхцчшщ]у$", l) and len(l) > 3: w = w[:-1] + "а"
        out.append(w)
    return " ".join(out)


DAYS_UA = ["неділю", "понеділок", "вівторок", "середу", "четвер", "п'ятницю", "суботу"]   # «на …»
WD_STEMS = ["неді", "поне", "вівт", "сере", "четв", "пятн", "субо"]


def js_weekday(d):   # як getDay() у JavaScript: 0 — неділя
    return (d.weekday() + 1) % 7


def parse_day(s):
    n = norm(s); d = now()
    if not n or "сьогодн" in n: return d
    if "післязавтр" in n: return d + timedelta(days=2)
    if "завтр" in n: return d + timedelta(days=1)
    i = next((k for k, p in enumerate(WD_STEMS) if p in n), None)
    if i is None: return d
    return d + timedelta(days=(i - js_weekday(d)) % 7)


HELP = ("Тут, у Telegram, я вмію: «що в мене завтра», «що треба оплатити», авто («заправився на 900», «пробіг 128400», «замінив масло», «коли міняти масло»), «котра година», «погода» / «погода завтра», «тривога», «курс», «звіт»; "
        "«нагадай о 18:00 …», «нагадай через 20 хвилин …», «які нагадування»; «запиши …», «нотатки»; "
        "Пульт: «купи молоко й хліб», «що купити», «купив молоко», «що в Злати завтра», «скажи родині …»; "
        "Трекер: «зробив тактику», «випив воду», «спав 7 годин», «витратив 300 на бензин», «що лишилось у трекері»; "
        "«посилки». Голосові теж розумію. Решту питай — подумаю через Gemini.")


async def local_command(text):
    t = re.sub(r"^(?:джур\S*)[,!\s]*", "", text.strip(), flags=re.I).rstrip(".!?…").strip()
    low = t.lower().replace("’", "'").replace("ʼ", "'")
    if not low: return None

    if re.fullmatch(r"(/start|/help|що ти (вмієш|можеш)( робити)?|допомога|команди|твої команди)( без (gemini|джеміні|інтернету|мозку))?", low): return HELP
    if re.fullmatch(r"(привіт|вітаю|здоров|добр\S* (ранок|ранку|день|вечір|вечора))", low): return f"Вітаю, {addr_low()}! Чим допомогти?"
    if low == "слава україні": return "Героям слава!"
    if re.fullmatch(r"(котра (зараз )?година|скільки (зараз )?часу)", low): return f"{address()}, зараз {hhmm(now())}."
    if re.fullmatch(r"(/brief|звіт|ранковий звіт|брифінг)", low): return await morning_brief()
    m = re.fullmatch(r"(?:що|які плани|які події)\s+(?:в|у)\s+(?:мене\s+(?:в|у)\s+)?календар\S*(?:\s+(сьогодні|завтра|післязавтра))?|(?:мій\s+)?календар(?:\s+(сьогодні|завтра|післязавтра))?", low)
    if m:
        if not calendar_id(): return "Календар до бота не підключений: потрібна змінна CALENDAR_ID на Render і розшарений календар (див. README)."
        w = m.group(1) or m.group(2) or "сьогодні"
        try: txt = await cal_day_text(parse_day(w))
        except Exception as e: return f"Календар зараз недоступний: {e}"
        return f"📅 {w.capitalize()}: {txt}"
    m = re.match(r"^(?:що|які плани)\s+(?:в|у)\s+мене(?:\s+(сьогодні|завтра|післязавтра|(?:в|у)\s+\S+))?$", low) or \
        re.match(r"^(?:мій\s+)?план(?:\s+дня)?(?:\s+на)?(?:\s+(сьогодні|завтра|післязавтра|\S+))?$", low)
    if m:
        w = m.group(1) or "сьогодні"; day = parse_day(w)
        label = "Сьогодні" if "сьогодн" in w else "Завтра" if w.startswith("завтра") else "Післязавтра" if "після" in w else DAYS_UA[js_weekday(day)].capitalize()
        return f"{label}:\n" + "\n".join(await day_plan(day))
    c = await car_command(low)
    if c: return c
    if re.search(r"що\s+(треба\s+|потрібно\s+)?(оплатити|заплатити)|які рахунки", low):
        bills = [b for b in await pult_bills_due() if b["in_days"] <= 7]
        return "До оплати: " + "; ".join(f"{b['title']} — {money(b['amount'])} грн, " + ("протерміновано" if b["in_days"] < 0 else "сьогодні" if b["in_days"] == 0 else f"через {b['in_days']} {plural(b['in_days'], 'день', 'дні', 'днів')}") for b in bills) + "." if bills else "Найближчим тижнем платити нічого."
    if "погод" in low and not re.search(r"(?:\s|^)(у|в|для)\s+[А-ЯІЇЄҐ]", t): return await weather("завтра" in low)
    if "тривог" in low:
        a = await alerts_state()
        if a is None: return "Токен тривог не налаштований у Джурі."
        return f"{address()}, увага: зараз повітряна тривога!" if a["on"] else f"Тихо, у твоїй області тривоги немає. По Україні областей під тривогою: {a['oblasts']}."
    if "курс" in low: return await rates()

    # Нагадування й нотатки (спільні з сайтом Джури)
    m = re.match(r"^нагадай(?: мені)?\s+(завтра\s+)?о\s+(\d{1,2})(?:[:.\s](\d{2}))?(?:\s*годин\S*)?(\s+завтра)?\s+(.+)$", t, re.I)
    m2 = re.match(r"^нагадай(?: мені)?\s+через\s+(\S+)\s*(хвилин\S*|хв|годин\S*|год)\s+(.+)$", t, re.I)
    if m or m2:
        if m:
            when = now().replace(hour=int(m.group(2)), minute=int(m.group(3) or 0), second=0, microsecond=0)
            if m.group(1) or m.group(4) or when < now(): when += timedelta(days=1)
            what = m.group(5)
        else:
            n = num(m2.group(1))
            if n is None: return None
            when = now() + timedelta(minutes=n * (60 if m2.group(2).startswith("год") else 1)); what = m2.group(3)
        what = re.sub(r"^(що|щоб|про)\s+", "", what, flags=re.I).strip()
        item = {"at": int(when.timestamp() * 1000), "text": what[0].upper() + what[1:], "tg": True}
        await update_list("reminders", lambda L: (sorted(L + [item], key=lambda r: r.get("at", 0)), None))
        return f"Добре, нагадаю {'завтра ' if when.date() != now().date() else ''}о {hhmm(when)}: {what}."
    if re.match(r"^(які|мої|список)\s+нагадуван", low):
        await refresh_doc()
        rs = S["doc"].get("reminders") or []
        if not rs: return "Нагадувань немає."
        return "; ".join(f"{datetime.fromtimestamp(r['at'] / 1000, KYIV).strftime('%d.%m %H:%M')} — {r.get('text')}" for r in rs) + "."
    m = re.match(r"^(?:запиши|занотуй|нотатка)[:,]?\s+(.+)$", t, re.I)
    if m and not re.search(r"(в|у|до)\s+(пульт|календар|трекер)", m.group(1), re.I):
        note = {"text": m.group(1), "at": now().strftime("%d.%m.%Y %H:%M")}
        await update_list("notes", lambda L: (L + [note], None))
        return "Записав у нотатки Джури."
    if re.match(r"^(нотатки|мої нотатки|прочитай нотатки)$", low):
        await refresh_doc(); ns = S["doc"].get("notes") or []
        return "; ".join(f"{i + 1}) {n.get('text')}" for i, n in enumerate(ns[-10:])) + "." if ns else "Нотаток немає."

    # Пульт
    m = re.match(r"^(?:купи(?:ти)?|треба купити|додай (?:в|у|до) (?:список|закупів\S*|покупк\S*|пульт\S*))[:,]?\s+(.+)$", t, re.I)
    m2 = re.match(r"^закінчил\S*\s+(.+)$", t, re.I)
    if m or m2:
        items = [nominative(x) for x in split_items(m.group(1))] if m else split_items(m2.group(1))
        ms = await pult_members(); me = await pult_me(ms)
        allx = await pult_list("shopping"); added = []
        for name in items:
            ex = next((x for x in allx if (x.get("name") or "").lower() == name.lower()), None)
            if ex:
                if not ex.get("lowStock"): await pult_update("shopping", ex["id"], {"lowStock": True})
                added.append(ex["name"])
            else:
                nid = pult_uid(); nice = name[0].upper() + name[1:]
                await pult_set("shopping", nid, {"id": nid, "name": nice, "category": "", "lowStock": True,
                                                  "createdAt": int(time.time() * 1000), "createdBy": me})
                added.append(nice)
        return f"Додав у Пульт → Закупівлі: {', '.join(added)}."
    if re.match(r"^(?:що|чого)\s+(?:треба\s+|потрібно\s+)?купити|^список покупок$", low):
        L = [x["name"] for x in await pult_list("shopping") if x.get("lowStock")]
        return "Треба купити: " + ", ".join(L) + "." if L else "Список покупок порожній."
    m = re.match(r"^(?:я\s+)?купи(?:в|ла|ли)\s+(.+)$", t, re.I)
    if m:
        allx = [x for x in await pult_list("shopping") if x.get("lowStock")]; done = []
        for name in [nominative(x) for x in split_items(m.group(1))]:
            ex = next((x for x in allx if norm(x.get("name")).startswith(stem(name))), None)
            if ex: await pult_update("shopping", ex["id"], {"lowStock": False}); done.append(ex["name"])
        return f"Відмітив куплене: {', '.join(done)}." if done else "Не знайшов цього в списку покупок."
    m = re.match(r"^(?:скажи|напиши|повідом|відправ\S*|надішли|передай)\s+(?:родині|сім'ї|(?:в|у|на)\s+(?:сімейний\s+)?(?:чат|пульт\S*))[:,]?\s+(?:що\s+)?(.+)$", t, re.I)
    if m:
        ms = await pult_members(); me = await pult_me(ms); nid = pult_uid(); msg = m.group(1)
        await pult_set("chatMessages", nid, {"id": nid, "text": msg[0].upper() + msg[1:], "createdAt": int(time.time() * 1000), "createdBy": me})
        return "Написав у Пульт → Сімейний чат."
    m = re.match(r"^(?:що|які плани|який розклад|які гуртки|куди)\s+(?:в|у|для)\s+([а-яіїєґ']+)(?:\s+(сьогодні|завтра|післязавтра|(?:в|у)\s+\S+))?$", low)
    m2 = re.match(r"^розклад(?:\s+на)?\s+(\S+)$", low)
    if m or m2:
        ms = await pult_members()
        who = None if (m2 or re.fullmatch(r"нас|родини|сімї|сім'ї", m.group(1))) else find_member(ms, m.group(1))
        if m and not m2 and not who and not re.fullmatch(r"нас|родини|сімї|сім'ї", m.group(1)): return None
        day = parse_day(m2.group(1) if m2 else (m.group(2) or "сьогодні")); wd = js_weekday(day)
        items = [x for x in await pult_list("logistics") if x.get("weekday") == wd and (not who or x.get("memberId") == who["id"])]
        items.sort(key=lambda x: x.get("startTime", ""))
        name = lambda mid: next((mm.get("name") for mm in ms if mm.get("id") == mid), "")
        head = f"У {m.group(1).capitalize()}" if who else "У родини"   # як сказав господар: «у Злати»
        if not items: return f"{head} на {DAYS_UA[wd]} нічого не заплановано."
        return f"{head} на {DAYS_UA[wd]}: " + ", ".join(f"о {x.get('startTime')} {x.get('title')}" + ("" if who else f" ({name(x.get('memberId'))})") for x in items) + "."

    # Трекер
    def mdone(t_):
        log_ = t_["morningLog"].get(ymd()) or {}
        left = [txt for k, txt in MORNING if not log_.get(k)]
        return f"Ранок: {6 - len(left)} з 6." + (f" Лишилось: {', '.join(left)}." if left else " Ранок виконано! 🎉")
    mornings = [(r"^(?:я\s+)?випи(?:в|ла)\s+(?:склянку\s+)?вод", "water"), (r"^(?:я\s+)?(?:прокинувся|прокинулась|встав|встала)(?:\s|$)", "wake"),
                (r"^(?:я\s+)?склав\S*\s+план", "plan"), (r"^(?:я\s+)?прочита(?:в|ла)\s+план", "read_plan"),
                (r"^(?:я\s+)?(?:зробив|зробила)\s+(?:ранкову\s+)?зарядку$", "exercise")]
    for rx, item in mornings:
        if re.search(rx, low):
            await tracker_set_path(["morningLog", ymd(), item], True)
            tt = await tracker_load()
            if item in ("exercise", "eyes"):
                h = next((x for x in tt["habits"] if re.search("заряд" if item == "exercise" else "очей|очі", x.get("name", ""), re.I)), None)
                if h: await tracker_set_path(["data", ymd(), str(h["id"])], True); tt = await tracker_load()
            return mdone(tt)
    m = re.match(r"^(?:я\s+)?(?:відміть|познач|зарахуй|виконав|виконала|зробив|зробила)\s+(.+?)(?:\s+(?:в|у)\s+трекері)?$", t, re.I)
    if m or re.match(r"^(?:я\s+)?(?:прочитав|почитав|прочитала|погуляв|погуляла|потренувався|покодував|позаймався|позаймалась|пописав)(?:\s|$)", low):
        tt = await tracker_load(); h = find_habit(tt["habits"], m.group(1) if m else t)
        if h:
            await tracker_set_path(["data", ymd(), str(h["id"])], True)
            if re.search("заряд", h.get("name", ""), re.I): await tracker_set_path(["morningLog", ymd(), "exercise"], True)
            if re.search("очей|очі", h.get("name", ""), re.I): await tracker_set_path(["morningLog", ymd(), "eyes"], True)
            tt = await tracker_load(); done = sum(1 for x in tt["habits"] if (tt["data"].get(ymd()) or {}).get(str(x["id"])))
            return f"Відмітив у трекері: {short(h['name'])}. Сьогодні {done}/{len(tt['habits'])}."
        if m: return f"Не знайшов такої звички в трекері."
    if ("трекер" in low and re.search(r"що|скільки|лишил|залишил|прогрес", low)) or re.match(r"^що (ще )?(лишилось|залишилось) (зробити )?сьогодні", low):
        tt = await tracker_load(); day = tt["data"].get(ymd()) or {}
        left = [short(h["name"]) for h in tt["habits"] if not day.get(str(h["id"]))]
        return f"Трекер: {len(tt['habits']) - len(left)} з {len(tt['habits'])}." + (f" Лишилось: {', '.join(left)}." if left else " Усе виконано! 🏆")
    m = re.match(r"^(?:я\s+)?(?:спав|спала|сон)\s+(\S+)(\s+з\s+половиною)?(?:\s+годин\S*)?$", low)
    if m and num(m.group(1)) is not None:
        h = num(m.group(1)) + (0.5 if m.group(2) else 0)
        await tracker_set_path(["sleepLog", ymd(), "hours"], round(h * 2) / 2)
        return f"Записав сон: {str(h).replace('.0', '').replace('.', ',')} год." + (" Маловато — норма 7–9." if h < 7 else "")
    m = re.match(r"^(?:я\s+)?(витратив|витратила|потратив|заплатив|заплатила|заробив|заробила|отримав|отримала)\s+(\d+(?:[.,]\d+)?)\s*(?:грн|гривень|гривні|₴)?\s*(?:на|за|від)?\s*(.*)$", t, re.I)
    if m:
        typ = "income" if re.match(r"зароб|отрим", m.group(1), re.I) else "expense"
        amt = float(m.group(2).replace(",", "."))
        await tracker_append("financeLog", {"type": typ, "amount": int(amt) if amt.is_integer() else amt, "desc": (m.group(3) or "").capitalize(),
                                            "date": ymd(), "at": int(time.time() * 1000)})
        return f"Записав у трекер {'дохід' if typ == 'income' else 'витрату'} {m.group(2)} грн" + (f" — {m.group(3)}" if m.group(3) else "") + "."

    # Посилки
    if re.search(r"посилк|ттн|нов\S* пошт", low):
        nums = re.findall(r"\d{10,}", t.replace(" ", "")) or (S["doc"].get("parcels") or [])
        if not nums: return "Не знаю жодної ТТН. Напиши «посилка» і номер."
        data = await np_status(nums)
        act = [x for x in data if str(x.get("StatusCode")) not in ("9", "10", "11")] or data
        return "; ".join(f"{str(x.get('Number'))[-4:]}: {x.get('Status')}" + (f", {x.get('WarehouseRecipient')}" if x.get("WarehouseRecipient") else "") for x in act) + "." if act else "Нічого не знайшов."
    return None


async def pult_bills_due():
    """Несплачені рахунки Пульта з кількістю днів до оплати (як рахує сам Пульт)."""
    today0 = now().replace(hour=0, minute=0, second=0, microsecond=0); out = []
    for b in await pult_list("bills"):
        rec = b.get("recurring")
        period = today0.strftime("%Y-%m") if rec == "monthly" else str(today0.year) if rec == "yearly" else "once"
        if b.get("paidPeriod") == period: continue
        try:
            if rec == "monthly":
                import calendar
                dim = calendar.monthrange(today0.year, today0.month)[1]
                due = today0.replace(day=min(int(b.get("dueDay") or 1), dim))
            elif rec == "yearly" and b.get("date"):
                _, mo, d = map(int, b["date"].split("-")); due = today0.replace(month=mo, day=d)
            elif b.get("date"):
                y, mo, d = map(int, b["date"].split("-")); due = today0.replace(year=y, month=mo, day=d)
            else: continue
        except Exception: continue
        out.append({"title": b.get("title", ""), "amount": b.get("amount") or 0, "in_days": (due - today0).days})
    return sorted(out, key=lambda x: x["in_days"])


def car_status():
    log_ = (S["doc"].get("car") or {}).get("log") or []
    if not log_: return None
    km = max([x.get("km") or 0 for x in log_] or [0]) or None
    last = lambda t: next((x for x in reversed(log_) if x.get("type") == t), None)
    oil_every = int(settings().get("carOilEvery") or 10000); svc_every = int(settings().get("carServiceEvery") or 15000)
    left = lambda ev, every: every - (km - ev["km"]) if ev and ev.get("km") and km else None
    month = ymd()[:7]
    fuel = sum(x.get("uah") or 0 for x in log_ if x.get("type") == "fuel" and str(x.get("date", "")).startswith(month))
    return {"km": km, "oil_left": left(last("oil"), oil_every), "service_left": left(last("service"), svc_every), "fuel_month": fuel, "oil_every": oil_every}


def car_text():
    c = car_status()
    if not c: return "Журнал авто порожній. Напиши «пробіг 128400» або «замінив масло на 128400»."
    out = [f"Пробіг {money(c['km'])} км."] if c["km"] else []
    if c["oil_left"] is not None: out.append("⚠️ Мастило пора міняти." if c["oil_left"] <= 0 else f"До заміни мастила ~{money(c['oil_left'])} км.")
    if c["service_left"] is not None: out.append("⚠️ ТО вже пора." if c["service_left"] <= 0 else f"До ТО ~{money(c['service_left'])} км.")
    if c["fuel_month"]: out.append(f"На пальне цього місяця {money(c['fuel_month'])} грн.")
    return " ".join(out)


def km_from(s):
    m = re.search(r"(\d{4,7})", re.sub(r"(\d)\s+(?=\d{3}\b)", r"\1", s))
    return int(m.group(1)) if m else None


async def car_add(entry):
    entry = {"date": ymd(), "at": int(time.time() * 1000), **{k: v for k, v in entry.items() if v is not None}}
    def fn(d):
        d.setdefault("log", []).append(entry); d["log"] = d["log"][-500:]; return d, None
    await update_dict("car", fn); await refresh_doc()


async def car_command(low):
    m = re.match(r"^(?:я\s+)?заправи(?:вся|лась|в)(?:\s+на)?\s+(\d+(?:[.,]\d+)?)\s*(грн|гривень|гривні|л|літр\S*)?(.*)$", low)
    if m:
        v = float(m.group(1).replace(",", ".")); liters = (m.group(2) or "").startswith("л")
        await car_add({"type": "fuel", "uah": 0 if liters else v, "liters": v if liters else 0, "km": km_from(m.group(3) or "")})
        return f"Записав заправку: {v:g} {'л' if liters else 'грн'}. " + (f"Цього місяця на пальне {money(car_status()['fuel_month'])} грн." if not liters else "")
    m = re.match(r"^(?:поточний\s+)?пробіг\s+([\d\s]{4,9})(?:\s*км)?$", low)
    if m:
        await car_add({"type": "km", "km": int(m.group(1).replace(" ", ""))}); return "Записав. " + car_text()
    if re.match(r"^(?:я\s+)?(?:замінив|поміняв|міняв)\s+(?:масло|мастило|олив)", low):
        km = km_from(low) or (car_status() or {}).get("km"); await car_add({"type": "oil", "km": km})
        return f"Записав заміну мастила{f' на {money(km)} км' if km else ''}."
    if re.match(r"^(?:я\s+)?(?:пройшов|зробив|був на)\s+(?:то|техобслуговуванн\S*|сервіс\S*)(?:\s|$)", low):
        km = km_from(low) or (car_status() or {}).get("km"); await car_add({"type": "service", "km": km})
        return f"Записав ТО{f' на {money(km)} км' if km else ''}."
    if re.search(r"(коли|скільки до)\s+(заміни|міняти|поміняти)?\s*(масл|мастил|то\b|техобслуг)|стан (авто|машини)|^(авто|машина|джип)$", low):
        return car_text()
    return None


async def day_plan(day):
    """«Що в мене завтра»: розклад родини, дні народження, рахунки, нагадування, погода."""
    day0 = day.replace(hour=0, minute=0, second=0, microsecond=0)
    ahead = (day0 - now().replace(hour=0, minute=0, second=0, microsecond=0)).days
    items, tasks = [], []
    try:
        ms = await pult_members(); wd = js_weekday(day0)
        nm = lambda mid: next((m.get("name") for m in ms if m.get("id") == mid), "")
        for x in await pult_list("logistics"):
            if x.get("weekday") == wd: items.append((x.get("startTime", ""), f"👪 {x.get('title')}" + (f" ({nm(x.get('memberId'))})" if x.get("memberId") else "")))
        for e in await pult_list("events"):
            try:
                y, mo, d = map(int, e["date"].split("-"))
                if (mo, d) == (day0.month, day0.day) and (e.get("recurring") or y == day0.year): tasks.append(f"🎂 {e['title']}")
            except Exception: pass
        for b in await pult_bills_due():
            if b["in_days"] == ahead: tasks.append(f"🧾 оплатити {b['title']} — {money(b['amount'])} грн")
    except Exception as e: log.warning("plan pult: %s", e)
    await refresh_doc()
    for r in S["doc"].get("reminders") or []:
        t = datetime.fromtimestamp(r["at"] / 1000, KYIV)
        if t.date() == day0.date(): items.append((hhmm(t), f"⏰ {r.get('text')}"))
    items.sort()
    lines = [f"{t or 'Увесь день'} — {txt}" for t, txt in items] + tasks
    if not lines: lines.append("Нічого не заплановано — вільний день.")
    try:
        if ahead in (0, 1): lines.append("🌦 " + await weather(ahead == 1))
    except Exception: pass
    return lines


async def morning_brief():
    parts = [f"☀️ Доброго ранку, {addr_low()}! Сьогодні {['неділя','понеділок','вівторок','середа','четвер','пʼятниця','субота'][js_weekday(now())]}, {now().strftime('%d.%m')}."]
    if calendar_id():
        try:
            ct = await cal_day_text(now())
            if ct: parts.append("📅 Календар: " + ct)
        except Exception as e: log.warning("brief calendar: %s", e)
    for fn in (lambda: weather(False), rates):
        try: parts.append(await fn())
        except Exception as e: log.warning("brief part: %s", e)
    try:
        a = await alerts_state()
        if a is not None: parts.append("🚨 Зараз повітряна тривога!" if a["on"] else "Тривоги у твоїй області немає.")
    except Exception: pass
    try:
        ms = await pult_members(); wd = js_weekday(now())
        items = sorted([x for x in await pult_list("logistics") if x.get("weekday") == wd], key=lambda x: x.get("startTime", ""))
        nm = lambda mid: next((m.get("name") for m in ms if m.get("id") == mid), "")
        if items: parts.append("👪 Сьогодні в родини: " + ", ".join(f"{x.get('startTime')} {x.get('title')}" + (f" ({nm(x.get('memberId'))})" if x.get("memberId") else "") for x in items) + ".")
        today0 = now().replace(hour=0, minute=0, second=0, microsecond=0); soon = []
        for e in await pult_list("events"):
            try:
                y, mo, d = map(int, e["date"].split("-")); tgt = today0.replace(year=today0.year, month=mo, day=d)
                if e.get("recurring") and tgt < today0: tgt = tgt.replace(year=today0.year + 1)
                dd = (tgt - today0).days
                if 0 <= dd <= 3: soon.append(f"{e['title']} — " + ('сьогодні' if dd == 0 else f"через {dd} {plural(dd, 'день', 'дні', 'днів')}"))
            except Exception: pass
        if soon: parts.append("🎂 " + "; ".join(soon) + ".")
        due = [b for b in await pult_bills_due() if b["in_days"] <= 3]
        if due: parts.append("🧾 Оплатити: " + "; ".join(f"{b['title']} — {money(b['amount'])} грн" + (" (протерміновано)" if b["in_days"] < 0 else " (сьогодні)" if b["in_days"] == 0 else f" (через {b['in_days']} дн.)") for b in due) + ".")
        buy = [x["name"] for x in await pult_list("shopping") if x.get("lowStock")]
        if buy: parts.append(f"🛒 У списку покупок: {len(buy)}.")
    except Exception as e: log.warning("brief pult: %s", e)
    try:
        await refresh_doc()
        rs = [r for r in (S["doc"].get("reminders") or []) if datetime.fromtimestamp(r["at"] / 1000, KYIV).date() == now().date()]
        if rs: parts.append("⏰ Нагадування на сьогодні: " + "; ".join(f"{datetime.fromtimestamp(r['at'] / 1000, KYIV).strftime('%H:%M')} {r.get('text')}" for r in rs) + ".")
    except Exception: pass
    c = car_status()
    if c and c["oil_left"] is not None and c["oil_left"] < 800:
        parts.append("🚗 Мастило вже пора міняти." if c["oil_left"] <= 0 else f"🚗 До заміни мастила ~{c['oil_left']} км.")
    try:
        tt = await tracker_load(); parts.append(f"✅ Звичок на сьогодні: {len(tt['habits'])}. Почни з ранкового ритуалу.")
    except Exception: pass
    parts.append("💬 " + QUOTES[now().timetuple().tm_yday % len(QUOTES)])
    return "\n".join(parts)


async def evening_summary():
    try:
        tt = await tracker_load(); day = tt["data"].get(ymd()) or {}
        left = [short(h["name"]) for h in tt["habits"] if not day.get(str(h["id"]))]
        done = len(tt["habits"]) - len(left)
        if not left: return f"🌙 Усі {done} звичок сьогодні виконано, {addr_low()}! 🏆 Відпочивай."
        return f"🌙 Вечірній підсумок: виконано {done} з {len(tt['habits'])}. Ще можна встигнути: {', '.join(left[:6])}."
    except Exception as e:
        return None


# ---------------------------------------------------------------- Фонові задачі
async def check_reminders():
    nowms = int(time.time() * 1000)

    def claim(L):
        due = [r for r in L if r.get("at", 0) <= nowms]
        if not due: return None, []
        return [r for r in L if r.get("at", 0) > nowms], due
    due = await update_list("reminders", claim)
    for r in due or []:
        late = nowms - r.get("at", nowms) > 10 * 60000
        await tg_send(f"⏰ {address()}, нагадую: {r.get('text')}" + (" (запізнене нагадування)" if late else ""))


async def check_alerts(state):
    a = await alerts_state()
    if a is None: return
    prev = state.get("alertOn")
    if prev is not None and prev != a["on"]:
        obl = settings().get("oblast") or "Львівська область"
        await tg_send(f"🚨 {address()}, повітряна тривога! {obl}. До укриття." if a["on"] else f"✅ Відбій тривоги. {obl}.")
    if prev != a["on"]:
        state["alertOn"] = a["on"]; await bot_state_set({"alertOn": a["on"]})


async def check_parcels(state):
    nums = S["doc"].get("parcels") or []
    if not nums: return
    seen = state.get("parcelStatus") or {}; changed = False
    for x in await np_status(nums):
        n = str(x.get("Number")); s = x.get("Status") or ""
        if seen.get(n) and seen[n] != s:
            await tg_send(f"📦 Посилка …{n[-4:]}: {s}" + (f". {x.get('WarehouseRecipient')}" if x.get("WarehouseRecipient") else "") + (f". До сплати {x.get('DocumentCost')} грн" if x.get("DocumentCost") else "") + ".")
        if seen.get(n) != s: seen[n] = s; changed = True
    if changed:
        state["parcelStatus"] = seen; await bot_state_set({"parcelStatus": seen})


# ---------------------------------------------------------------- Сповіщення про збої
PROBLEMS = {}


async def notify_problem(key, text, cooldown=6 * 3600):
    """Повідомляє господаря про збій, але не частіше, ніж раз на cooldown секунд для кожного ключа."""
    t = time.time()
    if t - PROBLEMS.get(key, 0) < cooldown: return
    PROBLEMS[key] = t
    try: await tg_send("⚠️ " + text)
    except Exception as e: log.warning("notify_problem: %s", e)


# ---------------------------------------------------------------- Кнопки в Telegram
def pl(n, one, few, many):
    n = abs(int(n)); 
    if 11 <= n % 100 <= 14: return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def kb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


MENU_ROWS = [[("☀️ Звіт", "cmd:звіт"), ("📋 План дня", "cmd:план на сьогодні")],
             [("✅ Звички", "habits"), ("🧾 Платежі", "cmd:що треба оплатити")],
             [("🚗 Авто", "cmd:авто"), ("🌦 Погода", "cmd:погода")],
             [("🚨 Тривога?", "cmd:є тривога"), ("💱 Курс", "cmd:курс")],
             [("📅 Календар", "cmd:що в календарі"), ("📦 Посилки", "cmd:посилки")],
             [("💾 Бекап", "backup"), ("📊 Підсумок тижня", "weekly")]]


async def send_menu(chat=None):
    await tg_send("Що зробити, " + addr_low() + "?", chat, kb(MENU_ROWS))


def habit_rows(left):
    btns = [("⬜ " + short(h["name"])[:24], f"h:{h['id']}") for h in left[:12]]
    return [btns[i:i + 2] for i in range(0, len(btns), 2)]


async def send_habits(chat=None, intro=None):
    tt = await tracker_load(); day = tt["data"].get(ymd()) or {}
    left = [h for h in tt["habits"] if not day.get(str(h["id"]))]
    total = len(tt["habits"]); done = total - len(left)
    if not left:
        await tg_send(f"{intro + ' ' if intro else ''}Усі {total} {pl(total, 'звичка', 'звички', 'звичок')} сьогодні виконано! 🏆", chat); return
    await tg_send(f"{intro + ' ' if intro else ''}Сьогодні {done} з {total}. Тисни, що вже зроблено:", chat, kb(habit_rows(left)))


async def mark_habit(chat, hid):
    tt = await tracker_load()
    h = next((x for x in tt["habits"] if str(x["id"]) == hid), None)
    if not h: return await tg_send("Не знайшов цієї звички.", chat)
    await tracker_set_path(["data", ymd(), str(h["id"])], True)
    if re.search("заряд", h.get("name", ""), re.I): await tracker_set_path(["morningLog", ymd(), "exercise"], True)
    if re.search("очей|очі", h.get("name", ""), re.I): await tracker_set_path(["morningLog", ymd(), "eyes"], True)
    await send_habits(chat, f"✅ {short(h['name'])}.")


async def handle_callback(cb):
    chat = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
    try: await http_json("POST", tg_url("answerCallbackQuery"), json={"callback_query_id": cb["id"]})
    except Exception: pass
    if chat != str(settings().get("tgChat")): return
    data = cb.get("data") or ""
    try:
        if data.startswith("cmd:"): await handle_text(chat, data[4:])
        elif data == "habits": await send_habits(chat)
        elif data.startswith("h:"): await mark_habit(chat, data[2:])
        elif data == "backup": await send_backup(chat)
        elif data == "weekly": await tg_send(await weekly_summary(), chat)
        elif data == "menu": await send_menu(chat)
    except Exception as e:
        await tg_send(f"Халепа: {e}", chat)


# ---------------------------------------------------------------- Резервна копія
def _sanitized_settings(st):
    return {k: v for k, v in (st or {}).items() if not re.search(r"key|token|pass|secret|fbconfig", k, re.I)}


async def send_backup(chat=None):
    chat = chat or settings().get("tgChat")
    doc = dict(S.get("doc") or {})
    doc["settings"] = _sanitized_settings(doc.get("settings"))
    doc.pop("history", None)   # розмови з Джурою не потрібні, і вони важкі
    data = {"exportedAt": now().isoformat(), "dzhura": doc}
    try: data["tracker"] = await tracker_load()
    except Exception as e: data["tracker"] = {"error": str(e)}
    raw = json.dumps(data, ensure_ascii=False, indent=1, default=str).encode()
    fd = aiohttp.FormData()
    fd.add_field("chat_id", str(chat))
    fd.add_field("caption", f"💾 Резервна копія Джури й трекера, {ymd()}. Ключі й паролі в неї не потрапляють.")
    fd.add_field("document", raw, filename=f"dzhura-backup-{ymd()}.json", content_type="application/json")
    async with S["session"].post(tg_url("sendDocument"), data=fd) as r:
        if r.status != 200: raise RuntimeError(f"Telegram не прийняв файл ({r.status})")


# ---------------------------------------------------------------- Підсумок тижня
async def weekly_summary():
    days = [ymd(now() - timedelta(days=i)) for i in range(6, -1, -1)]
    parts = [f"📊 Підсумок тижня, {addr_low()}"]
    try:
        tt = await tracker_load(); hb = tt["habits"]
        if hb:
            counts = {h["id"]: sum(1 for d in days if (tt["data"].get(d) or {}).get(str(h["id"]))) for h in hb}
            total = sum(counts.values()); pct = round(100 * total / (7 * len(hb)))
            best = max(hb, key=lambda h: counts[h["id"]]); worst = min(hb, key=lambda h: counts[h["id"]])
            parts.append(f"✅ Звички: {pct}% ({total} із {7 * len(hb)}). Найкраще — {short(best['name'])} ({counts[best['id']]}/7)" +
                         (f", найслабше — {short(worst['name'])} ({counts[worst['id']]}/7)." if counts[worst['id']] < counts[best['id']] else "."))
        sl = [(tt["sleepLog"].get(d) or {}).get("hours") for d in days]; sl = [x for x in sl if x]
        if sl: parts.append(f"😴 Сон у середньому {sum(sl) / len(sl):.1f} год".replace(".", ",") + f" ({len(sl)} із 7 ночей записано).")
        fin = [x for x in tt["financeLog"] if x.get("date") in days]
        if fin:
            exp = sum(x.get("amount", 0) for x in fin if x.get("type") != "income"); inc = sum(x.get("amount", 0) for x in fin if x.get("type") == "income")
            parts.append(f"💸 Витрати {money(exp)} грн" + (f", доходи {money(inc)} грн" if inc else "") + ".")
        pages = sum(x.get("pages", 0) for x in tt["bookLog"] if x.get("date") in days)
        if pages: parts.append(f"📖 Читання: {pages} хв.")
    except Exception as e:
        parts.append("Трекер зараз недоступний.")
        await notify_problem("tracker", f"Не можу прочитати трекер: {e}")
    try:
        c = car_status()
        if c and c.get("fuel_month_uah"): parts.append(f"⛽ На пальне цього місяця {money(c['fuel_month_uah'])} грн.")
    except Exception: pass
    try:
        soon = [b for b in await pult_bills_due() if b["in_days"] <= 7]
        if soon: parts.append("🧾 Платежі на тиждень: " + "; ".join(f"{b['title']} — {money(b['amount'])} грн" for b in soon) + ".")
    except Exception: pass
    return "\n".join(parts)


# ---------------------------------------------------------------- Google Календар (через сервісний акаунт: календар треба розшарити на його пошту)
CAL = {"creds": None, "cache": [], "at": 0}


def calendar_id():
    return os.environ.get("CALENDAR_ID") or settings().get("calendarId") or ""


def _cal_token():
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    if CAL["creds"] is None:
        CAL["creds"] = service_account.Credentials.from_service_account_info(
            json.loads(os.environ["FIREBASE_SA_JSON"]), scopes=["https://www.googleapis.com/auth/calendar.readonly"])
    if not CAL["creds"].valid: CAL["creds"].refresh(Request())
    return CAL["creds"].token


async def cal_events(t_from, t_to):
    cid = calendar_id()
    if not cid: return None
    tok = await asyncio.to_thread(_cal_token)
    from urllib.parse import quote, urlencode
    q = urlencode({"timeMin": t_from.isoformat(), "timeMax": t_to.isoformat(), "singleEvents": "true", "orderBy": "startTime", "maxResults": 30})
    st, d = await http_json("GET", f"https://www.googleapis.com/calendar/v3/calendars/{quote(cid)}/events?{q}", headers={"Authorization": f"Bearer {tok}"})
    if st != 200: raise RuntimeError(f"Google Календар відповів {st}: {(d.get('error') or {}).get('message', '')}")
    out = []
    for e in d.get("items", []):
        if e.get("status") == "cancelled": continue
        st_ = e.get("start") or {}
        if st_.get("dateTime"):
            when = datetime.fromisoformat(st_["dateTime"].replace("Z", "+00:00")).astimezone(KYIV); allday = False
        elif st_.get("date"):
            when = datetime.fromisoformat(st_["date"]).replace(tzinfo=KYIV); allday = True
        else: continue
        out.append({"id": e.get("id"), "title": e.get("summary") or "(без назви)", "when": when, "allday": allday})
    return out


async def cal_day_text(day):
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    ev = await cal_events(start, start + timedelta(days=1))
    if ev is None: return None
    if not ev: return "У календарі порожньо."
    return "; ".join((e["title"] if e["allday"] else f"{hhmm(e['when'])} {e['title']}") for e in ev) + "."


async def check_calendar(state):
    """Нагадує про події за годину і за 10 хвилин. Календар опитуємо раз на 5 хвилин."""
    if not calendar_id(): return
    if time.time() - CAL["at"] > 300:
        CAL["at"] = time.time()
        try: CAL["cache"] = await cal_events(now(), now() + timedelta(hours=3)) or []
        except Exception as e:
            CAL["cache"] = []
            await notify_problem("calendar", f"Календар недоступний: {e}. Перевір, що календар розшарено на пошту сервісного акаунта (client_email з FIREBASE_SA_JSON) і задано CALENDAR_ID.")
            return
    sent = list(state.get("calSent") or []); changed = False
    for e in CAL["cache"]:
        if e["allday"]: continue
        mins = (e["when"] - now()).total_seconds() / 60
        for lead in (60, 10):
            key = f"{e['id']}:{e['when'].isoformat()}:{lead}"
            if 0 < mins <= lead and key not in sent:
                sent.append(key); changed = True
                if lead == 60 and mins < 30: continue   # подію додали пізно — вистачить нагадування за 10 хв
                await tg_send(f"📅 Через {round(mins)} хв: {e['title']} ({hhmm(e['when'])})")
    if changed:
        state["calSent"] = sent[-200:]; await bot_state_set({"calSent": state["calSent"]})


def norm_hm(v, default):
    m = re.match(r"^\s*(\d{1,2})[:.](\d{2})\s*$", str(v or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59: return default
    return f"{int(m.group(1)):02d}:{m.group(2)}"


async def scheduler():
    state = await bot_state(); last_parcels = 0
    while True:
        try:
            await refresh_doc()
            await check_reminders()
            await check_alerts(state)
            hm = hhmm(now()); today = ymd()
            brief_at = norm_hm(settings().get("briefTime") or os.environ.get("BRIEF_TIME"), "07:30")
            eve_at = norm_hm(settings().get("eveningTime") or os.environ.get("EVENING_TIME"), "21:00")
            if hm >= brief_at and state.get("lastBrief") != today and hm < "12:00":
                state["lastBrief"] = today; await bot_state_set({"lastBrief": today})
                await tg_send(await morning_brief())
            if hm >= eve_at and state.get("lastEvening") != today:
                state["lastEvening"] = today; await bot_state_set({"lastEvening": today})
                msg = await evening_summary()
                if msg: await tg_send(msg)
            if hm >= "10:00" and state.get("lastBills") != today:
                state["lastBills"] = today; await bot_state_set({"lastBills": today})
                try:
                    soon = [b for b in await pult_bills_due() if b["in_days"] in (1, 2)]
                    if soon:
                        await tg_send("🧾 Нагадую про оплату: " + "; ".join(f"{b['title']} — {money(b['amount'])} грн, " + ("завтра" if b["in_days"] == 1 else "післязавтра") for b in soon) + ".")
                except Exception as e: log.warning("bills: %s", e)
            habit_at = norm_hm(settings().get("habitRemindTime") or os.environ.get("HABIT_TIME"), "20:00")
            if str(settings().get("habitRemindTime", "")).lower() != "off" and hm >= habit_at and hm < "23:00" and state.get("lastHabitNudge") != today:
                state["lastHabitNudge"] = today; await bot_state_set({"lastHabitNudge": today})
                try:
                    tt = await tracker_load(); day = tt["data"].get(today) or {}
                    left = [h for h in tt["habits"] if not day.get(str(h["id"]))]
                    if left: await tg_send(f"🔔 {address()}, ще не відмічено {len(left)} з {len(tt['habits'])} звичок. Тисни, що вже зроблено:", None, kb(habit_rows(left)))
                except Exception as e: await notify_problem("tracker", f"Не можу прочитати трекер для нагадування про звички: {e}")
            if now().weekday() == 6 and hm >= norm_hm(os.environ.get("WEEKLY_TIME"), "20:30") and state.get("lastWeekly") != today:
                state["lastWeekly"] = today; await bot_state_set({"lastWeekly": today})
                await tg_send(await weekly_summary())
                try: await send_backup()
                except Exception as e: await notify_problem("backup", f"Резервна копія не надіслалась: {e}")
            await check_calendar(state)
            if time.time() - last_parcels > 2 * 3600:
                last_parcels = time.time(); await check_parcels(state)
        except Exception as e:
            log.exception("scheduler: %s", e)
            await notify_problem("scheduler", f"Збій у фоновому циклі бота: {e}", 3600)
        await asyncio.sleep(30)


async def keepalive():
    # Render на безкоштовному тарифі засинає після ~15 хв тиші — стукаємо самі до себе кожні 5 хвилин
    while True:
        await asyncio.sleep(300)
        if PUBLIC_URL:
            try: await http_json("GET", PUBLIC_URL + "/health")
            except Exception: pass


# ---------------------------------------------------------------- Обробка повідомлень Telegram
async def handle_text(chat, text):
    if re.fullmatch(r"/?(menu|меню|кнопки)", text.strip().lower()): return await send_menu(chat)
    if re.fullmatch(r"/?(habits|звички)", text.strip().lower()): return await send_habits(chat)
    if re.fullmatch(r"/?(backup|бекап|резервна копія)", text.strip().lower()): return await send_backup(chat)
    if re.fullmatch(r"/?(weekly|підсумок тижня)", text.strip().lower()): return await tg_send(await weekly_summary(), chat)
    await tg_action(chat)
    try:
        r = await local_command(text)
    except Exception as e:
        r = f"Халепа: {e}"
    if r is None:
        try:
            r = await gemini([{"text": text}], system_prompt())
        except Exception as e:
            r = f"Цього я без Gemini не зроблю, а він зараз недоступний ({e}). Напиши «що ти вмієш» — перелічу, що працює без нього."
    await tg_send(r or "Хм, не знайшов слів.", chat)
    if text.strip().lower() in ("/start", "/help"): await send_menu(chat)


async def handle_voice(chat, file_id):
    await tg_action(chat)
    st, f = await http_json("GET", tg_url("getFile") + f"?file_id={file_id}")
    path = (f.get("result") or {}).get("file_path")
    if not path: return await tg_send("Не вдалося завантажити голосове.", chat)
    tok = os.environ.get("TELEGRAM_TOKEN") or settings().get("tgToken")
    async with S["session"].get(f"https://api.telegram.org/file/bot{tok}/{path}") as r:
        audio = await r.read()
    try:
        text = await gemini([{"inlineData": {"mimeType": "audio/ogg", "data": base64.b64encode(audio).decode()}},
                             {"text": "Перепиши це голосове дослівно українською. Лише текст, без лапок і пояснень."}],
                            "Ти точно розшифровуєш українське мовлення в текст.")
    except Exception as e:
        text = None
        try: text = await groq_whisper(audio)
        except Exception: pass
        if not text:
            return await tg_send(f"Голосові розшифровую через Gemini, а він зараз недоступний ({e}). Напиши текстом або додай ключ Groq у налаштуваннях Джури.", chat)
    await tg_send(f"🎙 «{text}»", chat)
    await handle_text(chat, text)


async def handle_photo(chat, file_id, caption):
    await tg_action(chat)
    st, f = await http_json("GET", tg_url("getFile") + f"?file_id={file_id}")
    path = (f.get("result") or {}).get("file_path")
    if not path: return await tg_send("Не вдалося завантажити фото.", chat)
    tok = os.environ.get("TELEGRAM_TOKEN") or settings().get("tgToken")
    async with S["session"].get(f"https://api.telegram.org/file/bot{tok}/{path}") as r:
        img = await r.read()
    ask = caption or ("Подивись на фото. Якщо це чек або рахунок — назви магазин, дату й підсумкову суму в гривнях. "
                      "Якщо це документ чи текст — коротко перекажи суть. Інакше — коротко опиши, що на фото.")
    mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    try:
        r = await gemini([{"inlineData": {"mimeType": mime, "data": base64.b64encode(img).decode()}}, {"text": ask}], system_prompt())
    except Exception as e:
        r = f"Фото я читаю лише через Gemini, а він зараз недоступний ({e}). Спробуй пізніше."
    await tg_send(r or "Не зміг розібрати фото.", chat)


async def poll_telegram():
    try: await http_json("POST", tg_url("deleteWebhook"), json={"drop_pending_updates": False})
    except Exception as e: log.warning("deleteWebhook: %s", e)
    offset = 0
    while True:
        try:
            st, d = await http_json("GET", tg_url("getUpdates") + f"?timeout=25&offset={offset}")
            for u in d.get("result", []):
                offset = u["update_id"] + 1
                if u.get("callback_query"):
                    asyncio.create_task(handle_callback(u["callback_query"])); continue
                msg = u.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id", ""))
                if not chat: continue
                if chat != str(settings().get("tgChat")):
                    await tg_send("Це особистий помічник, вибач 🙂", chat); continue
                if msg.get("voice") or msg.get("audio"):
                    asyncio.create_task(handle_voice(chat, (msg.get("voice") or msg.get("audio"))["file_id"]))
                elif msg.get("photo"):
                    asyncio.create_task(handle_photo(chat, msg["photo"][-1]["file_id"], msg.get("caption", "")))
                elif msg.get("text"):
                    asyncio.create_task(handle_text(chat, msg["text"]))
        except Exception as e:
            log.warning("poll: %s", e); await asyncio.sleep(5)


CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Allow-Headers": "*"}


async def api_alerts(request):
    # Для Джури в браузері: alerts.in.ua блокує прямі запити з браузера, тому питаємо через бота
    if request.method == "OPTIONS": return web.Response(headers=CORS)
    tok = settings().get("alertsToken")
    if not tok: return web.json_response({"error": "У налаштуваннях Джури немає токена alerts.in.ua"}, headers=CORS)
    try:
        st, d = await http_json("GET", f"https://api.alerts.in.ua/v1/alerts/active.json?token={tok}")
    except Exception as e:
        return web.json_response({"error": f"alerts.in.ua недоступний: {e}"}, headers=CORS)
    if st != 200: return web.json_response({"error": f"alerts.in.ua відповів кодом {st}"}, headers=CORS)
    oblast = settings().get("oblast") or "Львівська область"
    key = oblast.replace(" область", "").strip().lower()
    air = [a for a in d.get("alerts", []) if a.get("alert_type") == "air_raid"]
    mine = [a for a in air if key in (a.get("location_oblast") or a.get("location_title") or "").lower()]
    return web.json_response({"my_oblast": oblast, "alert_in_my_oblast": bool(mine),
        "my_oblast_places": [a.get("location_title") for a in mine],
        "oblasts_under_alert": sorted({a.get("location_title") for a in air if a.get("location_type") == "oblast"}),
        "total_active": len(air)}, headers=CORS)


async def health(_):
    return web.Response(text="Джура на посту ✅")


async def main():
    S["session"] = aiohttp.ClientSession()
    S["uid"] = await asyncio.to_thread(_find_uid)
    await refresh_doc()
    log.info("Джура: документ %s, чат %s", S["uid"], settings().get("tgChat"))
    app = web.Application(); app.router.add_get("/", health); app.router.add_get("/health", health); app.router.add_route("*", "/api/alerts", api_alerts)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    st = await bot_state()
    if not st.get("welcomed"):
        await tg_send("🛡️ Джура на посту: нагадування, тривоги, ранковий звіт і Telegram-команди працюють цілодобово. Напиши «що ти вмієш».")
        await bot_state_set({"welcomed": True})
    try:
        await http_json("POST", tg_url("setMyCommands"), json={"commands": [
            {"command": "menu", "description": "Меню з кнопками"}, {"command": "habits", "description": "Звички на сьогодні"},
            {"command": "brief", "description": "Ранковий звіт"}, {"command": "weekly", "description": "Підсумок тижня"},
            {"command": "backup", "description": "Резервна копія"}]})
    except Exception as e: log.warning("setMyCommands: %s", e)
    await asyncio.gather(poll_telegram(), scheduler(), keepalive())


if __name__ == "__main__":
    asyncio.run(main())
