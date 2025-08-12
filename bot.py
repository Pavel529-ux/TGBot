# bot.py
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    InlineQueryResultPhoto, InlineQueryResultArticle, InputTextMessageContent,
    ReplyKeyboardMarkup, KeyboardButton
)
from dotenv import load_dotenv
import os, sys, re, requests, traceback, logging, signal, threading, io, csv, zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from io import BytesIO
from datetime import datetime, timedelta, timezone  # ← timezone-aware

# ========================== ENV ==========================
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("bot")
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID_STR = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OR_MODEL = os.getenv("OR_TEXT_MODEL", "openai/gpt-oss-120b")

HF_TOKEN = os.getenv("HF_TOKEN")
HF_IMAGE_MODEL = os.getenv("HF_IMAGE_MODEL", "stabilityai/sdxl-turbo")

CATALOG_URL = os.getenv("CATALOG_URL")
CATALOG_AUTH_USER = os.getenv("CATALOG_AUTH_USER")
CATALOG_AUTH_PASS = os.getenv("CATALOG_AUTH_PASS")
CATALOG_REFRESH_MIN = int(os.getenv("CATALOG_REFRESH_MIN", "30"))
TELEGRAM_ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))
MANAGER_CHAT_ID = int(os.getenv("MANAGER_CHAT_ID", "0"))

missing = [k for k, v in {
    "BOT_TOKEN": BOT_TOKEN, "API_ID": API_ID_STR, "API_HASH": API_HASH,
    "OPENROUTER_API_KEY": OPENROUTER_API_KEY, "HF_TOKEN": HF_TOKEN
}.items() if not v]
if missing:
    log.error("❌ Не заданы переменные окружения: %s", ", ".join(missing)); sys.exit(1)
try:
    API_ID = int(API_ID_STR)
except Exception:
    log.error("❌ API_ID должен быть числом, получено: %r", API_ID_STR); sys.exit(1)

# ========================== Утилиты ==========================
def or_headers(title: str = "TelegramBot"):
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "HTTP-Referer": "https://openrouter.ai",
        "X-Title": title,
    }

def has_cyrillic(text: str) -> bool:
    return bool(re.search(r"[\u0400-\u04FF]", text or ""))

def translate_to_english(text: str) -> str:
    try:
        payload = {"model": OR_MODEL, "messages": [
            {"role": "system", "content": "Translate Russian to concise English. Return ONLY translated text."},
            {"role": "user", "content": text}
        ], "temperature": 0.2}
        r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                          headers=or_headers("PromptTranslator"), json=payload,
                          timeout=40, allow_redirects=False)
        if r.status_code == 200 and r.headers.get("content-type","").startswith("application/json"):
            return r.json()["choices"][0]["message"]["content"].strip()
        log.warning("Translate HTTP %s | %s", r.status_code, r.text[:300])
    except Exception:
        traceback.print_exc()
    return text

def boost_prompt(en_prompt: str, user_negative: str = "") -> tuple[str, str]:
    base_pos = f"{en_prompt}, ultra-detailed, high quality, high resolution, sharp focus, intricate details, 8k, dramatic lighting"
    base_neg = "lowres, blurry, out of focus, pixelated, deformed, bad anatomy, extra fingers, watermark, text, signature"
    neg = (base_neg + ", " + user_negative) if user_negative else base_neg
    return base_pos, neg

PHONE_RE = re.compile(r"^\+?\d[\d\s\-()]{6,}$")

# ========================== Память ==========================
chat_history = defaultdict(list)
HISTORY_LIMIT = 10
def clamp_history(h): return h[-HISTORY_LIMIT:] if len(h) > HISTORY_LIMIT else h

# ========================== Каталог (кэш) ==========================
catalog = []
catalog_last_fetch = None
catalog_lock = threading.Lock()
pending_reserve = {}  # user_id -> product_id

# ---- карточки/кнопки ----
def product_caption(p):
    price = p.get("price"); stock = p.get("stock")
    return "\n".join([
        f"🛒 {p.get('name','')}",
        f"Артикул: {p.get('sku','—')}",
        f"Цена: {price} ₽" if price is not None else "Цена: уточняйте",
        f"В наличии: {stock} шт." if stock is not None else "Наличие: уточняйте",
    ])

def product_keyboard(p):
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    pid = p.get("id") or p.get("sku")
    btns = [[InlineKeyboardButton("📝 Забронировать", callback_data=f"reserve:{pid}")]]
    if p.get("category"):
        btns.append([InlineKeyboardButton(f"📂 Категория: {p['category']}", callback_data=f"cat:{p['category']}")])
    btns.append([InlineKeyboardButton("🔎 Искать в чате", switch_inline_query_current_chat=p.get("sku",""))])
    return InlineKeyboardMarkup(btns)

def send_product_message(message, p):
    img = p.get("image_url"); caption = product_caption(p); kb = product_keyboard(p)
    if img: message.reply_photo(img, caption=caption, reply_markup=kb)
    else:   message.reply_text(caption, reply_markup=kb)

# ---- поиск/намерение ----
INTENT = re.compile(
    r"(?P<what>кабель|провод|автомат|выключател[ьяь]|пускател[ьяи])?"
    r".*?(?P<num>\d{1,3})\s*(?P<unit>мм2|мм²|мм|sqmm|а|a)?",
    re.IGNORECASE
)
def parse_intent(text: str):
    t = (text or "").lower(); brand=None
    for b in ("abb","schneider","iek","legrand","hager","siemens","rexant","sevkabel"):
        if b in t: brand=b; break
    itype=sqmm=amp=None; m=INTENT.search(t)
    if m:
        what=(m.group("what") or ""); unit=(m.group("unit") or "").lower()
        try: n=int(m.group("num"))
        except: n=None
        if what.startswith("кабель") or "провод" in what: itype="кабель"
        elif what.startswith("автомат") or "выключател" in what: itype="автомат"
        elif "пускател" in what: itype="пускатель"
        if n is not None:
            if unit in ("мм2","мм²","мм","sqmm"): sqmm=n; itype=itype or "кабель"
            elif unit in ("а","a"): amp=n; itype=itype or "автомат"
    return {"type": itype, "sqmm": sqmm, "amp": amp, "brand": brand}

def search_products(q, limit=10):
    q=(q or "").strip().lower(); res=[]
    for it in catalog:
        hay=f"{str(it.get('name','')).lower()} {str(it.get('sku','')).lower()} {str(it.get('brand','')).lower()}"
        if q in hay:
            res.append(it)
            if len(res)>=limit: break
    return res

def search_products_smart(qtext: str, limit=10):
    intent=parse_intent(qtext); q=(qtext or "").strip().lower(); scored=[]
    for p in catalog:
        name=str(p.get("name","")).lower(); sku=str(p.get("sku","")).lower()
        brand=str(p.get("brand","")).lower(); ptype=str(p.get("type","")).lower()
        amp=p.get("amp"); sq=p.get("sqmm"); score=0
        if intent["type"]:
            if intent["type"] not in ptype: continue
            score+=2
        if intent["amp"] and isinstance(amp,(int,float)):
            score+=3 if amp==intent["amp"] else (2 if abs(amp-intent["amp"])<=10 else 0)
        if intent["sqmm"] and isinstance(sq,(int,float)):
            score+=3 if sq==intent["sqmm"] else (2 if abs(sq-intent["sqmm"])<=5 else 0)
        if intent["brand"] and intent["brand"] in brand: score+=2
        if q and q in f"{name} {sku} {brand} {ptype}": score+=1
        if score>0: scored.append((score,p))
    if not scored: return search_products(qtext, limit=limit)
    scored.sort(key=lambda x:x[0], reverse=True)
    return [p for _,p in scored[:limit]]

def suggest_alternatives(intent, limit=6):
    if not intent["type"]: return []
    key="amp" if intent["type"] in ("автомат","пускатель") else "sqmm"
    target=intent["amp"] if key=="amp" else intent["sqmm"]
    if not target: return []
    al=[]
    for p in catalog:
        if intent["type"] not in str(p.get("type","")).lower(): continue
        val=p.get(key)
        if isinstance(val,(int,float)): al.append((abs(val-target), p))
    al.sort(key=lambda x:x[0]); return [p for _,p in al[:limit]]

# ========================== Загрузчики каталогов ==========================
def parse_tilda_yml(xml_bytes: bytes) -> list[dict]:
    """
    Парсер YML (Tilda/Яндекс.Маркет)
    """
    root = ET.fromstring(xml_bytes)
    # категории
    cat_map = {}
    for c in root.findall(".//categories/category"):
        cid = c.get("id") or ""
        name = (c.text or "").strip()
        if cid: cat_map[cid] = name

    items = []
    for o in root.findall(".//offers/offer"):
        sku = o.get("id") or (o.findtext("vendorCode") or "")
        name = o.findtext("name") or ""
        brand = o.findtext("vendor") or ""
        price = o.findtext("price")
        img = o.findtext("picture") or ""
        cat_id = o.findtext("categoryId") or ""
        category = cat_map.get(cat_id, "")

        # собрать текст для эвристик из name + всех param
        text_for_parse = " ".join([
            name,
            " ".join([(p.text or "") for p in o.findall("param") if p is not None]),
        ]).lower()

        itype = "кабель" if "кабел" in text_for_parse else (
            "автомат" if ("автомат" in text_for_parse or "выключат" in text_for_parse) else (
                "пускатель" if "пускател" in text_for_parse else ""
            )
        )
        amp = None; sqmm = None
        m_amp = re.search(r"(\d{2,3})\s*а\b", text_for_parse)
        if m_amp: amp = int(m_amp.group(1))
        m_sq = re.search(r"(\d{1,3})\s*мм[²2]|\b(\d{1,3})\s*sqmm", text_for_parse)
        if m_sq: sqmm = int([g for g in m_sq.groups() if g][0])

        items.append({
            "id": sku or name, "sku": sku or name, "name": name,
            "type": itype, "brand": brand, "category": category,
            "amp": amp, "sqmm": sqmm,
            "price": float(price) if price else None,
            "stock": None,  # обычно нет остатков в Tilda YML
            "image_url": img
        })
    return items

def parse_commerceml(xml_bytes: bytes) -> list[dict]:
    """CommerceML: одиночный XML или ZIP (import.xml + offers.xml)."""
    def _parse_catalog(root):
        cat={}
        for t in root.findall(".//Товары/Товар"):
            _id=(t.findtext("Ид") or "").strip()
            name=(t.findtext("Наименование") or "").strip()
            sku=(t.findtext("Артикул") or "") or _id
            brand=(t.findtext("Изготовитель/Наименование") or t.findtext("Бренд") or "").strip()
            image=(t.findtext("Картинка") or "").strip()
            catref=t.find(".//Группы/Ид"); category=(catref.text or "").strip() if catref is not None else ""
            low=f"{name} {(t.findtext('Описание') or '')}".lower()
            itype="кабель" if "кабел" in low else ("автомат" if ("автомат" in low or "выключат" in low) else ("пускатель" if "пускател" in low else ""))
            amp=sqmm=None
            m_amp=re.search(r"(\d{2,3})\s*а\b", low); m_sq=re.search(r"(\d{1,3})\s*мм[²2]|\b(\d{1,3})\s*sqmm", low)
            if m_amp: amp=int(m_amp.group(1))
            if m_sq:  sqmm=int([g for g in m_sq.groups() if g][0])
            if _id:
                cat[_id]={"id":_id,"sku":sku,"name":name or sku,"brand":brand,"category":category,
                          "image_url":image,"type":itype,"amp":amp,"sqmm":sqmm}
        for g in root.findall(".//Группы/Группа"):
            gid=(g.findtext("Ид") or "").strip(); gname=(g.findtext("Наименование") or "").strip()
            if gid and gname:
                for v in cat.values():
                    if v.get("category")==gid: v["category"]=gname
        return cat
    def _parse_offers(root):
        offers={}
        for o in root.findall(".//Предложения/Предложение"):
            _id=(o.findtext("Ид") or "").strip()
            if not _id: continue
            price=None; qnode=o.find(".//Цены/Цена/ЦенаЗаЕдиницу")
            if qnode is not None and qnode.text:
                try: price=float(qnode.text.replace(",", ".").strip())
                except: price=None
            stock=None; qty=o.find("Количество")
            if qty is not None and qty.text:
                try: stock=int(float(qty.text.replace(",", ".").strip()))
                except: stock=None
            offers[_id]={"price":price,"stock":stock}
        return offers
    def _one(xml_b: bytes):
        root=ET.fromstring(xml_b); cat_map=_parse_catalog(root); off_map=_parse_offers(root)
        items=[]; keys=set(cat_map.keys())|set(off_map.keys())
        for k in keys:
            base=cat_map.get(k,{}); price=off_map.get(k,{}).get("price"); stock=off_map.get(k,{}).get("stock")
            items.append({
                "id":base.get("id",k),"sku":base.get("sku",k),"name":base.get("name",k),
                "type":base.get("type",""),"brand":base.get("brand",""),"category":base.get("category",""),
                "amp":base.get("amp"),"sqmm":base.get("sqmm"),"price":price,"stock":stock,
                "image_url":base.get("image_url","")
            })
        return items
    if zipfile.is_zipfile(io.BytesIO(xml_bytes)):
        with zipfile.ZipFile(io.BytesIO(xml_bytes)) as z:
            cat_map, off_map = {}, {}
            for name in z.namelist():
                if not name.lower().endswith(".xml"): continue
                data=z.read(name); root=ET.fromstring(data)
                if root.findall(".//Товары/Товар"): cat_map.update(_parse_catalog(root))
                if root.findall(".//Предложения/Предложение"):
                    for k,v in _parse_offers(root).items(): off_map[k]=v
            items=[]; keys=set(cat_map.keys())|set(off_map.keys())
            for k in keys:
                base=cat_map.get(k,{}); price=off_map.get(k,{}).get("price"); stock=off_map.get(k,{}).get("stock")
                items.append({
                    "id":base.get("id",k),"sku":base.get("sku",k),"name":base.get("name",k),
                    "type":base.get("type",""),"brand":base.get("brand",""),"category":base.get("category",""),
                    "amp":base.get("amp"),"sqmm":base.get("sqmm"),"price":price,"stock":stock,
                    "image_url":base.get("image_url","")
                })
            return items
    return _one(xml_bytes)

def fetch_catalog(force=False):
    """
    Загружает каталог из CATALOG_URL: YML (Tilda/ЯМ), CommerceML (XML/ZIP), JSON, CSV.
    """
    global catalog, catalog_last_fetch
    with catalog_lock:
        now = datetime.now(timezone.utc)  # ← timezone-aware
        if not force and catalog_last_fetch and now - catalog_last_fetch < timedelta(minutes=CATALOG_REFRESH_MIN):
            return False
        if not CATALOG_URL:
            log.warning("CATALOG_URL не задан — пропускаю загрузку каталога"); return False

        auth = (CATALOG_AUTH_USER, CATALOG_AUTH_PASS) if CATALOG_AUTH_USER else None
        try:
            r = requests.get(CATALOG_URL, auth=auth, timeout=60)
            r.raise_for_status()
            ct = (r.headers.get("content-type") or "").lower()
            url_l = CATALOG_URL.lower()

            items = []

            # 1) YML
            if "xml" in ct and url_l.endswith(".yml"):
                items = parse_tilda_yml(r.content)

            # 2) CommerceML / XML / ZIP (с фолбэком на YML)
            elif "xml" in ct or "zip" in ct or url_l.endswith((".xml", ".zip")):
                try:
                    items = parse_commerceml(r.content)
                except Exception:
                    items = parse_tilda_yml(r.content)

            # 3) JSON
            elif "application/json" in ct or url_l.endswith(".json"):
                data = r.json()
                if not isinstance(data, list):
                    log.error("JSON корень не список"); return False
                items = data

            # 4) CSV
            elif "text/csv" in ct or url_l.endswith(".csv"):
                f = io.StringIO(r.text); reader = csv.DictReader(f)
                for row in reader:
                    def _i(v):
                        try: return int(str(v).strip().replace(" ", "")) if str(v).strip() else None
                        except: return None
                    def _f(v):
                        try: return float(str(v).replace(",", ".").strip()) if str(v).strip() else None
                        except: return None
                    items.append({
                        "id": row.get("id") or row.get("sku") or row.get("ID"),
                        "sku": row.get("sku") or row.get("SKU"),
                        "name": row.get("name") or row.get("Name"),
                        "type": (row.get("type") or "").lower(),
                        "brand": row.get("brand") or row.get("Brand"),
                        "category": row.get("category") or row.get("Category"),
                        "amp": _i(row.get("amp")), "sqmm": _i(row.get("sqmm")),
                        "price": _f(row.get("price")), "stock": _i(row.get("stock")),
                        "image_url": row.get("image_url") or row.get("image") or row.get("Image"),
                    })
            else:
                log.error("Неизвестный формат каталога: %s", ct or url_l); return False

            # нормализация
            norm = []
            for p in items:
                if not p or not p.get("name"): 
                    continue
                p.setdefault("id", p.get("sku") or p.get("name"))
                p.setdefault("sku", p.get("id"))
                p.setdefault("brand",""); p.setdefault("category",""); p.setdefault("type","")
                norm.append(p)

            catalog = norm
            catalog_last_fetch = now
            log.info("Каталог обновлён: %d позиций (из %s)", len(catalog), CATALOG_URL)
            return True
        except Exception as e:
            traceback.print_exc(); log.error("Ошибка загрузки каталога: %s", e); return False

def periodic_refresh():
    try:
        fetch_catalog(force=False)
    finally:
        threading.Timer(CATALOG_REFRESH_MIN*60, periodic_refresh).start()

# ========================== Pyrogram (in_memory session) ==========================
app = Client(
    "my_bot",
    bot_token=BOT_TOKEN,
    api_id=API_ID,
    api_hash=API_HASH,
    in_memory=True  # ← ключевая правка: не создаёт/не требует файловую сессию
)

# ========================== Команды / UI (Только ЛИЧНЫЕ чаты) ==========================
@app.on_message(filters.private & filters.command("start"))
def start_handler(_, message):
    uid=message.from_user.id; chat_history[uid]=[]
    kb_inline = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Каталог", callback_data="cat:all"),
         InlineKeyboardButton("🔎 Поиск", switch_inline_query_current_chat="")],
        [InlineKeyboardButton("🧹 Сбросить контекст", callback_data="reset_ctx")]
    ])
    kb_main = ReplyKeyboardMarkup([[KeyboardButton("📦 Каталог"), KeyboardButton("🔎 Поиск")],
                                   [KeyboardButton("🧹 Сброс")]], resize_keyboard=True)
    message.reply_text(
        "Привет! Я бот магазина ⚡ Пиши свободно: «кабель 35мм», «автомат 400А ABB».",
        reply_markup=kb_main
    )
    message.reply_text("Доп. меню:", reply_markup=kb_inline)

@app.on_message(filters.private & filters.command("help"))
def help_handler(_, message):
    message.reply_text("Пиши текстом: «кабель 35мм», «автомат 400А ABB». Кнопки внизу: Каталог / Поиск / Сброс.")

@app.on_message(filters.private & filters.command("ping"))
def ping_handler(_, message): message.reply_text("pong ✅")

@app.on_message(filters.private & filters.command("catalog"))
def catalog_cmd(_, message): show_catalog(_, message)

def show_catalog(_, message):
    if not catalog: message.reply_text("Каталог пока пуст, попробуйте позже."); return
    for p in catalog[:10]:
        try: send_product_message(message, p)
        except Exception: traceback.print_exc()

@app.on_message(filters.private & filters.command("find"))
def find_cmd(_, message):
    query=" ".join(message.command[1:]).strip(); handle_search_text(_, message, query)

def handle_search_text(_, message, text):
    if not text: message.reply_text("Что ищем? Например: кабель 35мм, автомат 400А ABB."); return
    if not catalog: message.reply_text("Каталог пока не загружен."); return
    results=search_products_smart(text, limit=10)
    if results:
        for p in results:
            try: send_product_message(message, p)
            except Exception: traceback.print_exc()
        return
    intent=parse_intent(text); alts=suggest_alternatives(intent, limit=6)
    if alts:
        message.reply_text("Похожее по параметрам:")
        for p in alts:
            try: send_product_message(message, p)
            except Exception: traceback.print_exc()
        return
    message.reply_text("Ничего не нашлось 😕 Уточни запрос: бренд/ток/сечение.")

# ---------- Inline (оставляем; можно вызывать из лички кнопкой "Искать в чате") ----------
@app.on_inline_query()
def inline_query_handler(client, inline_query):
    q=inline_query.query.strip()
    if not q or not catalog: return
    results=search_products_smart(q, limit=25); items=[]
    for idx,p in enumerate(results):
        caption=product_caption(p); kb=product_keyboard(p); img=p.get("image_url")
        if img:
            items.append(InlineQueryResultPhoto(photo_url=img, thumb_url=img, caption=caption, reply_markup=kb, id=str(idx)))
        else:
            items.append(InlineQueryResultArticle(title=p.get("name","Товар"),
                description=f"SKU: {p.get('sku','—')} | {p.get('price','—')} ₽",
                input_message_content=InputTextMessageContent(caption), reply_markup=kb, id=str(idx)))
    try:
        inline_query.answer(items, cache_time=5, is_personal=True)
    except Exception:
        traceback.print_exc()

# ---------- Callbacks ----------
@app.on_callback_query()
def callbacks_handler(client, cq):
    data=cq.data or ""
    if data=="reset_ctx":
        chat_history[cq.from_user.id]=[]; cq.answer("Контекст очищён"); cq.message.reply_text("Контекст очищён. /start"); return
    if data.startswith("reserve:"):
        pid=data.split(":",1)[1]; pending_reserve[cq.from_user.id]=pid
        cq.message.reply_text("Оставьте, пожалуйста, номер телефона для связи:"); cq.answer(); return
    if data.startswith("cat:"):
        cat_str=data.split(":",1)[1].strip().lower()
        items=[p for p in catalog if cat_str in ("all", str(p.get("category","")).lower())]
        if not items: cq.message.reply_text("В этой категории пока пусто."); cq.answer(); return
        for p in items[:10]:
            try: send_product_message(cq.message, p)
            except Exception: traceback.print_exc()
        cq.answer()

# ---------- Sync ----------
@app.on_message(filters.private & filters.command("sync1c"))
def sync1c_handler(_, message):
    if TELEGRAM_ADMIN_ID and message.from_user.id != TELEGRAM_ADMIN_ID:
        message.reply_text("Недостаточно прав."); return
    ok=fetch_catalog(force=True)
    message.reply_text("✅ Каталог обновлён" if ok else "❌ Не удалось обновить каталог, проверь логи.")

# ---------- Сбор телефона ----------
@app.on_message(filters.private & filters.text & ~filters.command(["start","reset","img","catalog","find","sync1c","help","ping"]))
def maybe_collect_phone(_, message):
    uid=message.from_user.id
    if uid in pending_reserve:
        pid=pending_reserve.get(uid); phone=(message.text or "").strip()
        if not PHONE_RE.match(phone):
            message.reply_text("Похоже, номер не распознан. Пример: +7 999 123-45-67\nОтправьте номер ещё раз."); return
        product=None
        for p in catalog:
            if p.get("id")==pid or p.get("sku")==pid: product=p; break
        text=("🧾 Новая бронь:\n"
              f"Пользователь: @{message.from_user.username or message.from_user.id}\n"
              f"Телефон: {phone}\n"
              f"Товар: {product.get('name','') if product else pid}\n"
              f"SKU: {product.get('sku','—') if product else '—'}\n"
              f"Цена: {product.get('price','—') if product else '—'} ₽")
        pending_reserve.pop(uid, None)
        if MANAGER_CHAT_ID:
            try: _.send_message(MANAGER_CHAT_ID, text)
            except Exception: traceback.print_exc()
        message.reply_text("Спасибо! Менеджер скоро свяжется для подтверждения 😊")
        return

# ---------- /img ----------
@app.on_message(filters.private & filters.command("img"))
def image_handler(_, message):
    raw=" ".join(message.command[1:]).strip()
    if not raw: message.reply_text("Напиши: /img кот в космосе --no текст, подписи"); return
    user_neg=""
    if "--no" in raw:
        parts=raw.split("--no",1); raw=parts[0].strip(); user_neg=parts[1].strip()
    prompt_src=raw; prompt_en=translate_to_english(raw) if has_cyrillic(raw) else raw
    pos_prompt, neg_prompt = boost_prompt(prompt_en, user_negative=user_neg)
    try:
        model=(HF_IMAGE_MODEL or "stabilityai/sdxl-turbo").strip()
        url=f"https://api-inference.huggingface.co/models/{model}"
        headers={"Authorization": f"Bearer {HF_TOKEN}", "Accept":"image/png"}
        payload={"inputs":pos_prompt,"parameters":{"negative_prompt":neg_prompt,"num_inference_steps":24,"guidance_scale":7.0},"options":{"wait_for_model":True}}
        resp=requests.post(url, headers=headers, json=payload, timeout=180); ct=resp.headers.get("content-type","")
        if resp.status_code==200 and ct.startswith("image/"):
            bio=BytesIO(resp.content); bio.name="image.png"
            message.reply_photo(bio, caption=f"🎨 По запросу: {prompt_src or prompt_en}"); return
        if resp.status_code in (429,503): message.reply_text("Модель занята или лимит. Попробуйте ещё раз через минуту ⏳"); return
        snippet=(getattr(resp,"text","") or "")[:800]; message.reply_text(f"❌ Hugging Face {resp.status_code}\n{snippet}")
    except Exception:
        traceback.print_exc(); message.reply_text("Ошибка при генерации изображения 🎨")

# ---------- Текст (личка) → поиск/альтернативы/AI ----------
@app.on_message(filters.private & filters.text & ~filters.command(["start","reset","img","catalog","find","sync1c","help","ping"]), group=1)
def text_handler(_, message):
    uid=message.from_user.id; user_text=(message.text or "").strip(); low=user_text.lower()
    if low in ("📦 каталог","каталог"): return show_catalog(_, message)
    if low in ("🔎 поиск","поиск"): message.reply_text("Что ищем? Пиши свободно: «кабель 35мм», «автомат 400А ABB»."); return
    if low in ("🧹 сброс","сброс"): return reset_handler(_, message)

    if catalog:
        results=search_products_smart(user_text, limit=8)
        if results:
            for p in results:
                try: send_product_message(message, p)
                except Exception: traceback.print_exc()
            return
        intent=parse_intent(user_text); alts=suggest_alternatives(intent, limit=6)
        if alts:
            message.reply_text("Похожее по параметрам:")
            for p in alts:
                try: send_product_message(message, p)
                except Exception: traceback.print_exc()
            return

    if re.search(r"\b(привет|здравствуй|здравствуйте|добрый день|hi|hello)\b", low):
        message.reply_text("Привет! Напиши, что нужно: «кабель 35мм», «автомат 400А ABB», или жми «📦 Каталог»."); return

    chat_history[uid].append({"role":"user","content":user_text}); chat_history[uid]=clamp_history(chat_history[uid])
    try:
        payload={"model":OR_MODEL,"messages":[
            {"role":"system","content":"Ты — бот магазина электрооборудования. Сначала помогай по каталогу, если не получается — отвечай кратко и по делу."},
            *chat_history[uid],
        ]}
        resp=requests.post("https://openrouter.ai/api/v1/chat/completions", headers=or_headers("TelegramBotNLSearch"),
                           json=payload, timeout=60, allow_redirects=False)
        if resp.status_code!=200: message.reply_text("Не понял запрос. Примеры: «кабель 35мм», «автомат 400А ABB»."); return
        bot_reply=resp.json()["choices"][0]["message"]["content"].strip() or "🤖 (пустой ответ)"
        chat_history[uid].append({"role":"assistant","content":bot_reply}); chat_history[uid]=clamp_history(chat_history[uid])
        message.reply_text(bot_reply)
    except Exception:
        traceback.print_exc(); message.reply_text("Упс, не разобрал. Пример: «кабель 35мм» или «автомат 400А ABB».")

# ---------- Reset ----------
@app.on_message(filters.private & filters.command("reset"))
def reset_handler(_, message):
    chat_history[message.from_user.id]=[]; message.reply_text("🧹 Память очищена!")

# ---------- Завершение ----------
def _graceful_exit(sig, frame):
    logging.getLogger().info("Stop signal received (%s). Exiting...", sig)
    try: app.stop()
    finally: os._exit(0)
signal.signal(signal.SIGTERM, _graceful_exit)
signal.signal(signal.SIGINT, _graceful_exit)

# ---------- Запуск ----------
if __name__ == "__main__":
    try:
        log.info("✅ Бот запускается...")
        if CATALOG_URL:
            if not fetch_catalog(force=True): log.warning("Каталог не удалось загрузить на старте")
            periodic_refresh()
        app.run()
    except Exception:
        traceback.print_exc(); sys.exit(1)



















