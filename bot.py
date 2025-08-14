# bot.py
from pyrogram import Client, filters, idle
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    InlineQueryResultPhoto, InlineQueryResultArticle, InputTextMessageContent,
    ReplyKeyboardMarkup, KeyboardButton
)
from pyrogram.enums import ParseMode
from dotenv import load_dotenv
import os, sys, re, requests, traceback, logging, signal, threading, io, csv, zipfile, json
import xml.etree.ElementTree as ET
from collections import defaultdict, Counter, OrderedDict
from io import BytesIO
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("bot")
load_dotenv()

# обязательные
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID_STR = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
HF_TOKEN = os.getenv("HF_TOKEN")

# опциональные / дефолты
OR_MODEL = os.getenv("OR_TEXT_MODEL", "openai/gpt-oss-120b")
HF_IMAGE_MODEL = os.getenv("HF_IMAGE_MODEL", "stabilityai/sdxl-turbo")

CATALOG_URL = os.getenv("CATALOG_URL")
CATALOG_AUTH_USER = os.getenv("CATALOG_AUTH_USER")
CATALOG_AUTH_PASS = os.getenv("CATALOG_AUTH_PASS")
CATALOG_REFRESH_MIN = int(os.getenv("CATALOG_REFRESH_MIN", "30"))

TELEGRAM_ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))
MANAGER_CHAT_ID = int(os.getenv("MANAGER_CHAT_ID", "0"))

AUTOSYNC_NOTIFY = os.getenv("AUTOSYNC_NOTIFY", "1") == "1"
AUTOSYNC_REMIND_EVERY_MIN = int(os.getenv("AUTOSYNC_REMIND_EVERY_MIN", "120"))

SECRET_EXPORT_TOKEN = os.getenv("SECRET_EXPORT_TOKEN")
HTTP_PORT = int(os.getenv("PORT", "8080"))

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

# ───────────── Утилиты ─────────────
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

def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9\-]+", "_", (s or "").strip().lower())

def unslugify(slug: str, choices=None, fallback="Без категории") -> str:
    if choices:
        for c in choices:
            if slugify(c) == slug: return c
    for c in catalog_index.get("categories", []):
        if slugify(c) == slug: return c
    return fallback

# ───────────── Память ─────────────
chat_history = defaultdict(list)
HISTORY_LIMIT = 10
def clamp_history(h): return h[-HISTORY_LIMIT:] if len(h) > HISTORY_LIMIT else h

# ───────────── Каталог / кэш ─────────────
catalog = []               # каждый товар: {..., attrs: {Название: Значение}}
catalog_last_fetch = None
catalog_lock = threading.Lock()
pending_reserve = {}       # user_id -> product_id

# индексы
catalog_index = {
    "categories": [],
    "brands_by_cat": {},      # cat -> Counter(brand)
    "attrs_by_cat": {},       # cat -> {attr_name -> Counter(values)}
    "attr_steps_by_cat": {},  # cat -> [attr_name,...]
}
CAT_PAGE = 8
ITEMS_PAGE = 5
VALUES_PER_STEP = 8

# автонапоминания
_catalog_etag = None
_catalog_last_modified = None
_catalog_last_items = 0
_catalog_last_change = None
_last_reminder_at = None

# карточка товара
def product_caption(p):
    price = p.get("price"); stock = p.get("stock")
    def _fmt_price(val):
        try:
            return f"{float(val):,.0f}".replace(",", " ")
        except Exception:
            return str(val)
    return "\n".join([
        f"🛒 {p.get('name','')}",
        f"Артикул: {p.get('sku','—')}",
        f"Цена: {_fmt_price(price)} ₽" if price is not None else "Цена: уточняйте",
        f"В наличии: {stock} шт." if stock is not None else "Наличие: уточняйте",
    ])

def product_keyboard(p):
    pid = p.get("id") or p.get("sku")
    btns = [[InlineKeyboardButton("📝 Забронировать", callback_data=f"reserve:{pid}")]]
    if p.get("category"):
        btns.append([InlineKeyboardButton(f"📂 Категория: {p['category']}", callback_data=f"cats:p:1")])
    btns.append([InlineKeyboardButton("🔎 Искать в чате", switch_inline_query_current_chat=p.get("sku",""))])
    return InlineKeyboardMarkup(btns)

def send_product_message(message, p):
    img = p.get("image_url"); caption = product_caption(p); kb = product_keyboard(p)
    if img: message.reply_photo(img, caption=caption, reply_markup=kb)
    else:   message.reply_text(caption, reply_markup=kb)

# ───────────── Парсеры каталогов (YML, CommerceML) ─────────────
def _normalize_attr_name(n: str) -> str:
    n = (n or "").strip()
    replacements = {
        "Номинальный ток, А": "Номинальный ток, А",
        "Номинальный ток": "Номинальный ток, А",
        "Катушка управления, В": "Катушка управления, В",
        "Степень защиты, IP": "Степень защиты, IP",
        "IP": "Степень защиты, IP",
        "Серия": "Серия",
        "Вид привода": "Вид привода",
        "В корпусе": "В корпусе",
        "С тепловым реле": "С тепловым реле",
        "Число и исполнение доп. контактов": "Число и исполнение доп. контактов",
    }
    return replacements.get(n, n)

def parse_tilda_yml(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
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
        category = cat_map.get(cat_id, "") or "Без категории"

        attrs = {}
        for prm in o.findall("param"):
            an = prm.get("name") or ""
            av = (prm.text or "").strip()
            if not an or not av: continue
            attrs[_normalize_attr_name(an)] = av

        low_blob = " ".join([name] + [f"{k}: {v}" for k,v in attrs.items()]).lower()
        itype = "кабель" if "кабел" in low_blob else (
            "автомат" if ("автомат" in low_blob or "выключат" in low_blob) else (
                "пускатель" if "пускател" in low_blob else ""
            )
        )
        amp = None; sqmm = None
        m_amp = re.search(r"(\d{2,3})\s*а\b", low_blob)
        if m_amp: amp = int(m_amp.group(1))
        m_sq = re.search(r"(\d{1,3})\s*мм[²2]|\b(\d{1,3})\s*sqmm", low_blob)
        if m_sq: sqmm = int([g for g in m_sq.groups() if g][0])

        items.append({
            "id": sku or name, "sku": sku or name, "name": name,
            "type": itype, "brand": brand, "category": category,
            "amp": amp, "sqmm": sqmm,
            "price": float(price) if price else None,
            "stock": None, "image_url": img,
            "attrs": attrs
        })
    return items

def parse_commerceml(xml_bytes: bytes) -> list[dict]:
    def _attrs_from(root, node):
        attrs = {}
        for z in node.findall(".//ЗначенияСвойств/ЗначенияСвойства"):
            an = z.findtext("Наименование") or ""
            av = z.findtext("Значение") or ""
            if an and av:
                attrs[_normalize_attr_name(an)] = av.strip()
        for z in node.findall(".//ХарактеристикиТовара/ХарактеристикаТовара"):
            an = z.findtext("Наименование") or ""
            av = z.findtext("Значение") or ""
            if an and av:
                attrs[_normalize_attr_name(an)] = av.strip()
        return attrs

    def _parse_catalog(root):
        cat={}
        for t in root.findall(".//Товары/Товар"):
            _id=(t.findtext("Ид") or "").strip()
            name=(t.findtext("Наименование") or "").strip()
            sku=(t.findtext("Артикул") or "") or _id
            brand=(t.findtext("Изготовитель/Наименование") or t.findtext("Бренд") or "").strip()
            image=(t.findtext("Картинка") or "").strip()
            catref=t.find(".//Группы/Ид"); category=(catref.text or "").strip() if catref is not None else "Без категории"
            attrs = _attrs_from(root, t)

            low = f"{name} {json.dumps(attrs, ensure_ascii=False)}".lower()
            itype="кабель" if "кабел" in low else ("автомат" if ("автомат" in low or "выключат" in low) else ("пускатель" if "пускател" in low else ""))
            amp=sqmm=None
            m_amp=re.search(r"(\d{2,3})\s*а\b", low); m_sq=re.search(r"(\d{1,3})\s*мм[²2]|\b(\d{1,3})\s*sqmm", low)
            if m_amp: amp=int(m_amp.group(1))
            if m_sq:  sqmm=int([g for g in m_sq.groups() if g][0])
            if _id:
                cat[_id]={"id":_id,"sku":sku,"name":name or sku,"brand":brand,"category":category,
                          "image_url":image,"type":itype,"amp":amp,"sqmm":sqmm,"attrs":attrs}
        for g in root.findall(".//Группы/Группа"):
            gid=(g.findtext("Ид") or "").strip(); gname=(g.findtext("Наименование") or "").strip()
            if gid and gname:
                for v in cat.values():
                    if v.get("category")==gid: v["category"]=gname or "Без категории"
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
                "type":base.get("type",""),"brand":base.get("brand",""),"category":base.get("category","Без категории"),
                "amp":base.get("amp"),"sqmm":base.get("sqmm"),"price":price,"stock":stock,
                "image_url":base.get("image_url",""),
                "attrs": base.get("attrs", {})
            })
        return items

    if zipfile.is_zipfile(io.BytesIO(xml_bytes)):
        with zipfile.ZipFile(io.BytesIO(xml_bytes)) as z:
            cat_map, off_map = {}, {}
            for name in z.namelist():
                if not name.lower().endswith(".xml"): continue
                data=z.read(name); root=ET.fromstring(data)
                if root.findall(".//Товары/Товар"): cat_map.update({k:v for k,v in _parse_catalog(root).items()})
                if root.findall(".//Предложения/Предложение"):
                    for k,v in _parse_offers(root).items(): off_map[k]=v
            items=[]; keys=set(cat_map.keys())|set(off_map.keys())
            for k in keys:
                base=cat_map.get(k,{}); price=off_map.get(k,{}).get("price"); stock=off_map.get(k,{}).get("stock")
                items.append({
                    "id":base.get("id",k),"sku":base.get("sku",k),"name":base.get("name",k),
                    "type":base.get("type",""),"brand":base.get("brand",""),"category":base.get("category","Без категории"),
                    "amp":base.get("amp"),"sqmm":base.get("sqmm"),"price":price,"stock":stock,
                    "image_url":base.get("image_url",""),
                    "attrs": base.get("attrs", {})
                })
            return items
    return _one(xml_bytes)

# ───────────── Индексация каталога ─────────────
def rebuild_index():
    global catalog_index
    cats = [str(p.get("category","")).strip() or "Без категории" for p in catalog]
    cat_counts = Counter(cats)
    categories = [c for c,_ in cat_counts.most_common()]

    brands_by_cat = defaultdict(Counter)
    attrs_by_cat = defaultdict(lambda: defaultdict(Counter))

    for p in catalog:
        cat = str(p.get("category","")).strip() or "Без категории"
        brand = (p.get("brand") or "").strip()
        if brand: brands_by_cat[cat][brand] += 1
        attrs = dict(p.get("attrs") or {})
        if brand: attrs.setdefault("Бренд", brand)
        if isinstance(p.get("stock"), (int,float)):
            attrs.setdefault("Наличие", "В наличии" if p["stock"] > 0 else "Под заказ")
        for an,av in attrs.items():
            an_norm = _normalize_attr_name(an)
            av_norm = str(av).strip()
            if not an_norm or not av_norm: continue
            attrs_by_cat[cat][an_norm][av_norm] += 1

    steps_by_cat = {}
    for cat, amap in attrs_by_cat.items():
        keys = list(amap.keys())
        def _key_rank(k):
            if k.lower() == "бренд": return (0, -sum(amap[k].values()))
            if k.lower() == "наличие": return (1, -sum(amap[k].values()))
            return (2, -sum(amap[k].values()))
        keys.sort(key=_key_rank)
        steps_by_cat[cat] = keys

    catalog_index = {
        "categories": categories,
        "brands_by_cat": brands_by_cat,
        "attrs_by_cat": attrs_by_cat,
        "attr_steps_by_cat": steps_by_cat,
    }

# ───────────── Загрузка каталога + автонапоминания ─────────────
def fetch_catalog(force=False):
    global catalog, catalog_last_fetch, _catalog_etag, _catalog_last_modified
    global _catalog_last_items, _catalog_last_change

    with catalog_lock:
        now = datetime.now(timezone.utc)
        if not force and catalog_last_fetch and now - catalog_last_fetch < timedelta(minutes=CATALOG_REFRESH_MIN):
            return False
        if not CATALOG_URL:
            log.warning("CATALOG_URL не задан — пропускаю загрузку каталога")
            return False

        headers = {}
        if _catalog_etag: headers["If-None-Match"] = _catalog_etag
        if _catalog_last_modified: headers["If-Modified-Since"] = _catalog_last_modified
        auth = (CATALOG_AUTH_USER, CATALOG_AUTH_PASS) if CATALOG_AUTH_USER else None

        try:
            try:
                h = requests.head(CATALOG_URL, auth=auth, timeout=20)
                if h.status_code in (200, 304):
                    lm = h.headers.get("Last-Modified"); et = h.headers.get("ETag")
                    if not force and et and _catalog_etag and et == _catalog_etag:
                        catalog_last_fetch = now; return False
                    if not force and lm and _catalog_last_modified and lm == _catalog_last_modified:
                        catalog_last_fetch = now; return False
            except Exception:
                pass

            r = requests.get(CATALOG_URL, auth=auth, timeout=60, headers=headers)
            if r.status_code == 304:
                catalog_last_fetch = now; return False
            r.raise_for_status()

            ct = (r.headers.get("content-type") or "").lower()
            url_l = CATALOG_URL.lower()

            if "xml" in ct and url_l.endswith(".yml"):
                items = parse_tilda_yml(r.content)
            elif "xml" in ct or "zip" in ct or url_l.endswith((".xml", ".zip")):
                try: items = parse_commerceml(r.content)
                except Exception: items = parse_tilda_yml(r.content)
            elif "application/json" in ct or url_l.endswith(".json"):
                data = r.json()
                if not isinstance(data, list): log.error("JSON корень не список"); return False
                items = data
            elif "text/csv" in ct or url_l.endswith(".csv"):
                f = io.StringIO(r.text); reader = csv.DictReader(f); items=[]
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
                        "category": (row.get("category") or row.get("Category") or "Без категории"),
                        "amp": _i(row.get("amp")), "sqmm": _i(row.get("sqmm")),
                        "price": _f(row.get("price")), "stock": _i(row.get("stock")),
                        "image_url": row.get("image_url") or row.get("image") or row.get("Image"),
                        "attrs": {}
                    })
            else:
                log.error("Неизвестный формат каталога: %s", ct or url_l); return False

            norm=[]
            for p in items:
                if not p or not p.get("name"): 
                    continue
                p.setdefault("id", p.get("sku") or p.get("name"))
                p.setdefault("sku", p.get("id"))
                p.setdefault("brand",""); p.setdefault("category","Без категории"); p.setdefault("type","")
                p.setdefault("attrs", {})
                norm.append(p)
            catalog = norm
            catalog_last_fetch = now

            new_etag = r.headers.get("ETag"); new_lm = r.headers.get("Last-Modified")
            if new_etag: _catalog_etag = new_etag
            if new_lm: _catalog_last_modified = new_lm

            changed = (len(catalog) != _catalog_last_items)
            _catalog_last_items = len(catalog)
            if changed: _catalog_last_change = now

            rebuild_index()

            log.info("Каталог обновлён: %d позиций (из %s)", len(catalog), CATALOG_URL)

            if AUTOSYNC_NOTIFY and TELEGRAM_ADMIN_ID and changed:
                try:
                    if getattr(app, "is_connected", False):
                        app.send_message(
                            TELEGRAM_ADMIN_ID,
                            f"✅ Каталог обновлён: {len(catalog)} позиций\nИсточник: {CATALOG_URL}"
                        )
                except Exception:
                    traceback.print_exc()
            return True

        except Exception as e:
            traceback.print_exc()
            log.error("Ошибка загрузки каталога: %s", e)
            return False

def periodic_refresh():
    global _last_reminder_at
    try:
        updated = fetch_catalog(force=False)
        now = datetime.now(timezone.utc)
        if TELEGRAM_ADMIN_ID and AUTOSYNC_NOTIFY and AUTOSYNC_REMIND_EVERY_MIN > 0:
            last_change = _catalog_last_change or catalog_last_fetch
            if last_change:
                due_change = now - last_change >= timedelta(minutes=AUTOSYNC_REMIND_EVERY_MIN)
                due_rem = (not _last_reminder_at) or (now - _last_reminder_at >= timedelta(minutes=AUTOSYNC_REMIND_EVERY_MIN))
                if not updated and due_change and due_rem:
                    try:
                        if getattr(app, "is_connected", False):
                            app.send_message(
                                TELEGRAM_ADMIN_ID,
                                "ℹ️ Каталог не обновлялся. Если в Tilda есть новые данные из 1С, "
                                "нажми «Начать экспорт» в Tilda, затем «Обновить каталог» в боте."
                            )
                            _last_reminder_at = now
                    except Exception:
                        traceback.print_exc()
    finally:
        threading.Timer(CATALOG_REFRESH_MIN * 60, periodic_refresh).start()

# ───────────── HTTP-хук ─────────────
class _HookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            url = urlparse(self.path)
            if url.path != "/hook/tilda-export":
                self.send_response(404); self.end_headers(); self.wfile.write(b"Not found"); return
            qs = parse_qs(url.query or ""); token = (qs.get("token") or [""])[0]
            if SECRET_EXPORT_TOKEN and token != SECRET_EXPORT_TOKEN:
                self.send_response(401); self.end_headers(); self.wfile.write(b"Unauthorized"); return
            ok = fetch_catalog(force=True)
            try:
                if TELEGRAM_ADMIN_ID and getattr(app, "is_connected", False):
                    app.send_message(TELEGRAM_ADMIN_ID, ("✅ Каталог обновлён немедленно" if ok else "ℹ️ Каталог не изменился (304)") + f"\nИсточник: {CATALOG_URL}")
            except Exception:
                traceback.print_exc()
            self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
        except Exception:
            traceback.print_exc()
            try: self.send_response(500); self.end_headers(); self.wfile.write(b"ERROR")
            except Exception: pass

def _run_http_server():
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), _HookHandler)
        log.info("HTTP hook server on port %s started", HTTP_PORT)
        srv.serve_forever()
    except Exception:
        traceback.print_exc()

# ───────────── Поиск / намерение ─────────────
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

# ───────────── Доп. фильтрация для мастера (НОВОЕ) ─────────────
def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()

def filter_items_by_advanced(category: str, selections: OrderedDict) -> list[dict]:
    """
    Фильтрует товары по категории + выбранным атрибутам (мастер фильтров).
    Поддерживает: точное совпадение атрибута, мягкий матч по подстроке, "Бренд", "Наличие".
    """
    if not catalog:
        return []

    want_cat = (_norm(category) if category else "")
    sel = selections or OrderedDict()

    want_brand = _norm(sel.get("Бренд", ""))
    want_avail = (sel.get("Наличие", "") or "").strip().lower()
    attr_pairs = [(k, str(v)) for k, v in sel.items() if k not in ("Бренд", "Наличие")]

    def ok_availability(p):
        if not want_avail:
            return True
        stock = p.get("stock")
        if not isinstance(stock, (int, float)):
            return want_avail not in ("в наличии", "под заказ")
        return (want_avail == "в наличии" and stock > 0) or (want_avail == "под заказ" and stock <= 0)

    def ok_brand(p):
        if not want_brand:
            return True
        return want_brand in _norm(p.get("brand"))

    def ok_attrs(p):
        if not attr_pairs:
            return True
        p_attrs = { _normalize_attr_name(k): str(v) for k, v in (p.get("attrs") or {}).items() }
        for ak, av in attr_pairs:
            ak_norm = _normalize_attr_name(ak)
            pv = str(p_attrs.get(ak_norm, ""))
            if _norm(av) not in _norm(pv):
                return False
        return True

    res = []
    for it in catalog:
        if want_cat and _norm(it.get("category")) != want_cat:
            continue
        if not ok_brand(it):
            continue
        if not ok_availability(it):
            continue
        if not ok_attrs(it):
            continue
        res.append(it)

    def _key(p):
        stock = p.get("stock")
        have = 1 if (isinstance(stock, (int, float)) and stock > 0) else 0
        price = p.get("price")
        price = float(price) if isinstance(price, (int, float)) else float("inf")
        brand = _norm(p.get("brand"))
        return (-have, price, brand)

    res.sort(key=_key)
    return res

# ───────────── ФИЛЬТРЫ (Stateful Wizard v2) ─────────────
WIZ2 = {}  # key=(chat_id, msg_id) → {"cat": str_slug, "i": int, "sel": OrderedDict()}

def _cat_steps(cat):
    return catalog_index.get("attr_steps_by_cat", {}).get(cat, [])

def _cat_attr_values(cat, attr):
    return [v for v,_ in catalog_index.get("attrs_by_cat", {}).get(cat, {}).get(attr, Counter()).most_common()]

def _w2_key_from_cq(cq):
    return (cq.message.chat.id, cq.message.id)

def _w2_get(cat_slug, key=None):
    return WIZ2.get(key)

def _w2_set(key, data):
    WIZ2[key] = data

def build_cat_list_kb(page: int = 1):
    cats = catalog_index.get("categories", [])
    total = len(cats)
    if total == 0:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Обновить каталог", callback_data="cats:refresh")]])
    pages = max(1, (total + CAT_PAGE - 1) // CAT_PAGE)
    page = max(1, min(page, pages))
    start = (page - 1) * CAT_PAGE
    chunk = cats[start:start+CAT_PAGE]
    rows = []
    for c in chunk:
        rows.append([InlineKeyboardButton(f"{c}", callback_data=f"fw2start:{slugify(c)}")])
    nav = []
    if page > 1: nav.append(InlineKeyboardButton("« Назад", callback_data=f"cats:p:{page-1}"))
    if page < pages: nav.append(InlineKeyboardButton("Вперёд »", callback_data=f"cats:p:{page+1}"))
    if nav: rows.append(nav)
    return InlineKeyboardMarkup(rows)

def wizard2_text(cat_slug: str, i: int, selections: OrderedDict):
    cat = unslugify(cat_slug)
    steps = _cat_steps(cat)
    lines = [f"📂 Категория: <b>{cat}</b>",
             "Выбирайте параметры. Можно «Пропустить» любой шаг или нажать «Показать сейчас ✅» в любой момент."]
    if steps:
        for idx, an in enumerate(steps):
            mark = "✅" if an in selections else "—"
            val = selections.get(an, "не выбрано")
            pointer = " ← сейчас" if idx == i else ""
            lines.append(f"{idx+1}) {an}: <b>{val}</b> {mark}{pointer}")
    else:
        lines.append("<i>Для этой категории нет атрибутов.</i>")
    return "\n".join(lines)

def kb_wizard2(cat_slug: str, i: int, selections: OrderedDict):
    cat = unslugify(cat_slug)
    steps = _cat_steps(cat)
    rows = []

    if steps and 0 <= i < len(steps):
        an = steps[i]
        values = _cat_attr_values(cat, an)[:VALUES_PER_STEP]
        if values:
            for vidx, v in enumerate(values):
                rows.append([InlineKeyboardButton(v, callback_data=f"fw2v:{i}:{vidx}")])
        else:
            rows.append([InlineKeyboardButton("Нет значений", callback_data="noop")])

        rows.append([
            InlineKeyboardButton("Пропустить", callback_data=f"fw2skip:{i}"),
            InlineKeyboardButton("Показать сейчас ✅", callback_data=f"fw2show")
        ])

        nav = []
        if i > 0:
            nav.append(InlineKeyboardButton("← Назад", callback_data=f"fw2back:{i}"))
        nav.append(InlineKeyboardButton("Сбросить", callback_data=f"fw2reset"))
        rows.append(nav)
    else:
        rows.append([InlineKeyboardButton("✅ Показать товары", callback_data=f"fw2show")])
        rows.append([InlineKeyboardButton("← К категориям", callback_data="cats:p:1")])

    rows.append([InlineKeyboardButton("← Категории", callback_data="cats:p:1")])
    return InlineKeyboardMarkup(rows)

def wizard2_edit_message(cq):
    key = _w2_key_from_cq(cq)
    state = _w2_get(None, key=key)
    if not state:
        return
    txt = wizard2_text(state["cat"], state["i"], state["sel"])
    kb  = kb_wizard2(state["cat"], state["i"], state["sel"])
    try:
        cq.message.edit_text(txt, reply_markup=kb)
    except Exception:
        cq.message.reply_text(txt, reply_markup=kb)

def wizard2_show_results(cq):
    key = _w2_key_from_cq(cq)
    state = _w2_get(None, key=key)
    if not state:
        return
    cat = unslugify(state["cat"])
    selections = state["sel"]
    items = filter_items_by_advanced(cat, selections)
    header = f"📦 Результаты для «{cat}»"
    if selections:
        pretty = ", ".join([f"{k}: {v}" for k,v in selections.items()])
        header += f"\nФильтры: {pretty}"
    header += f"\nНайдено: {len(items)} шт."
    try:
        cq.message.edit_text(header)
    except Exception:
        cq.message.reply_text(header)
    for p in items[:20]:
        try: send_product_message(cq.message, p)
        except Exception: traceback.print_exc()
    if len(items) > 20:
        cq.message.reply_text(f"Показаны первые 20 из {len(items)}. Уточни фильтры или используй поиск.")

# ───────────── Pyrogram ─────────────
app = Client(
    "my_bot",
    bot_token=BOT_TOKEN,
    api_id=API_ID,
    api_hash=API_HASH,
    in_memory=True,
    parse_mode=ParseMode.HTML
)

# ───────────── Команды / UI ─────────────
def reply_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("🏠 Старт"), KeyboardButton("📂 Категории")],
        [KeyboardButton("📦 Каталог"), KeyboardButton("🔎 Поиск")],
        [KeyboardButton("🧹 Сброс")]
    ]
    if user_id == TELEGRAM_ADMIN_ID:
        rows.insert(1, [KeyboardButton("Обновить каталог")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

@app.on_message(filters.private & filters.command("start"))
def start_handler(_, message):
    uid = message.from_user.id
    chat_history[uid] = []
    kb_main = reply_main_keyboard(uid)
    message.reply_text(
        "Привет! Я бот магазина ⚡ Выбирай «📂 Категории» → фильтры по шагам (в одном сообщении), "
        "или пиши свободно: «контактор 25А катушка 220В IP20».",
        reply_markup=kb_main
    )
    kb_inline = InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Открыть категории", callback_data="cats:p:1")]
    ])
    message.reply_text("Быстрое меню:", reply_markup=kb_inline)

@app.on_message(filters.private & filters.text & filters.regex(r"^(🏠 Старт|Старт|Меню|Главное меню)$"))
def start_button_handler(_, message):
    return start_handler(_, message)

@app.on_message(filters.private & filters.command("help"))
def help_handler(_, message):
    message.reply_text("Категории → мастер фильтров по шагам (в одном сообщении). Можно «Пропустить» шаг или «Показать сейчас». Кнопка «🏠 Старт» — главное меню.")

def show_catalog(_, message):
    if not catalog: message.reply_text("Каталог пока пуст, попробуйте позже."); return
    for p in catalog[:10]:
        try: send_product_message(message, p)
        except Exception: traceback.print_exc()

@app.on_message(filters.private & filters.command("catalog"))
def catalog_cmd(_, message): show_catalog(_, message)

@app.on_message(filters.private & filters.command("find"))
def find_cmd(_, message):
    query=" ".join(message.command[1:]).strip(); handle_search_text(_, message, query)

def handle_search_text(_, message, text):
    if not text: message.reply_text("Что ищем? Например: контактор 25А катушка 220В IP20."); return
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
    message.reply_text("Ничего не нашлось 😕 Уточни запрос или открой «📂 Категории».")

# ───────────── Callback’и ─────────────
@app.on_callback_query()
def callbacks_handler(client, cq):
    try:
        data=cq.data or ""

        # Бронирование товара (НОВОЕ)
        if data.startswith("reserve:"):
            pid = data.split(":", 1)[1]
            user_id = cq.from_user.id
            pending_reserve[user_id] = pid
            try:
                cq.message.reply_text(
                    "Окей! Отправьте, пожалуйста, номер телефона для связи 📞\n"
                    "Пример: +7 999 123-45-67"
                )
            except Exception:
                traceback.print_exc()
            return cq.answer("Жду номер телефона")

        # Категории/пагинация
        if data.startswith("cats:"):
            if data == "cats:refresh":
                ok = fetch_catalog(force=True)
                try: cq.message.edit_text("✅ Каталог обновлён" if ok else "❌ Не удалось обновить каталог")
                except Exception: cq.message.reply_text("✅ Каталог обновлён" if ok else "❌ Не удалось обновить каталог")
                return cq.answer()
            m = re.search(r"cats:p:(\d+)", data)
            page = int(m.group(1)) if m else 1
            txt = "Категории:"
            kb = build_cat_list_kb(page)
            try: cq.message.edit_text(txt, reply_markup=kb)
            except Exception: cq.message.reply_text(txt, reply_markup=kb)
            return cq.answer()

        # ── Новый мастер фильтров (короткие колбэки) ──
        if data.startswith("fw2start:"):
            cat_slug = data.split(":",1)[1]
            key = (cq.message.chat.id, cq.message.id)
            _w2_set(key, {"cat": cat_slug, "i": 0, "sel": OrderedDict()})
            wizard2_edit_message(cq)
            return cq.answer()

        if data.startswith("fw2v:"):
            try:
                _, aidx, vidx = data.split(":")
                aidx = int(aidx); vidx = int(vidx)
            except Exception:
                return cq.answer()
            key = _w2_key_from_cq(cq); st = _w2_get(None, key=key)
            if not st: return cq.answer()
            cat = unslugify(st["cat"]); steps = _cat_steps(cat)
            if not steps or aidx<0 or aidx>=len(steps): return cq.answer()
            an = steps[aidx]
            values = _cat_attr_values(cat, an)[:VALUES_PER_STEP]
            if not values or vidx<0 or vidx>=len(values): return cq.answer()
            val = values[vidx]
            st["sel"][an] = val
            st["i"] = min(aidx+1, len(steps))
            _w2_set(key, st)
            wizard2_edit_message(cq)
            return cq.answer()

        if data.startswith("fw2skip:"):
            try:
                _, aidx = data.split(":")
                aidx = int(aidx)
            except Exception:
                return cq.answer()
            key = _w2_key_from_cq(cq); st = _w2_get(None, key=key)
            if not st: return cq.answer()
            cat = unslugify(st["cat"]); steps = _cat_steps(cat)
            st["i"] = min(aidx+1, len(steps))
            _w2_set(key, st)
            wizard2_edit_message(cq)
            return cq.answer()

        if data.startswith("fw2back:"):
            try:
                _, aidx = data.split(":"); aidx = int(aidx)
            except Exception:
                return cq.answer()
            key = _w2_key_from_cq(cq); st = _w2_get(None, key=key)
            if not st: return cq.answer()
            cat = unslugify(st["cat"]); steps = _cat_steps(cat)
            prev_i = max(0, aidx-1)
            if 0 <= aidx < len(steps):
                st["sel"].pop(steps[aidx], None)
            st["i"] = prev_i
            _w2_set(key, st)
            wizard2_edit_message(cq)
            return cq.answer()

        if data == "fw2reset":
            key = _w2_key_from_cq(cq); st = _w2_get(None, key=key)
            if not st: return cq.answer()
            st["sel"].clear(); st["i"] = 0
            _w2_set(key, st)
            wizard2_edit_message(cq)
            return cq.answer()

        if data == "fw2show":
            wizard2_show_results(cq)
            return cq.answer()

        if data == "noop":
            return cq.answer()

    except Exception:
        traceback.print_exc()
        cq.answer("Ошибка обработчика", show_alert=False)

# /sync1c — только админ (и кнопка Reply «Обновить каталог»)
@app.on_message(filters.private & (filters.command("sync1c") | filters.regex("^Обновить каталог$")))
def sync1c_handler(_, message):
    if TELEGRAM_ADMIN_ID and message.from_user.id != TELEGRAM_ADMIN_ID:
        message.reply_text("❌ Недостаточно прав."); return
    ok=fetch_catalog(force=True)
    message.reply_text("✅ Каталог обновлён" if ok else "❌ Не удалось обновить каталог, проверь логи.")

# Сбор телефона для брони
@app.on_message(filters.private & filters.text & ~filters.command(["start","reset","img","catalog","find","sync1c","help"]))
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

# /img
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
        if resp.status_code in (429,503): message.reply_text("Модель занята или лимит. Попробуйте ещё раз позже ⏳"); return
        snippet=(getattr(resp,"text","") or "")[:800]; message.reply_text(f"❌ Hugging Face {resp.status_code}\n{snippet}")
    except Exception:
        traceback.print_exc(); message.reply_text("Ошибка при генерации изображения 🎨")

# Текст (личка)
@app.on_message(filters.private & filters.text & ~filters.command(["start","reset","img","catalog","find","sync1c","help"]), group=1)
def text_handler(_, message):
    uid=message.from_user.id; user_text=(message.text or "").strip(); low=user_text.lower()
    if low in ("🏠 старт","старт","меню","главное меню"):
        return start_handler(_, message)
    if low in ("📦 каталог","каталог"): return show_catalog(_, message)
    if low in ("📂 категории","категории"):
        try: message.reply_text("Категории:", reply_markup=build_cat_list_kb(page=1))
        except Exception: message.reply_text("Категории недоступны сейчас.")
        return
    if low in ("🔎 поиск","поиск"): message.reply_text("Что ищем? Пиши свободно: «контактор 25А катушка 220В IP20»."); return
    if low in ("🧹 сброс","сброс"): 
        chat_history[uid]=[]
        message.reply_text("🧹 Память очищена!")
        return

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
        message.reply_text("Привет! Открой «📂 Категории» и собери фильтры по шагам, или напиши, что нужно (пример: «контактор 25А катушка 220В»)."); return

    chat_history[uid].append({"role":"user","content":user_text}); chat_history[uid]=clamp_history(chat_history[uid])
    try:
        payload={"model":OR_MODEL,"messages":[
            {"role":"system","content":"Ты — бот магазина электрооборудования. Сначала помогай по каталогу, если не получается — отвечай кратко и по делу."},
            *chat_history[uid],
        ]}
        resp=requests.post("https://openrouter.ai/api/v1/chat/completions", headers=or_headers("TelegramBotNLSearch"),
                           json=payload, timeout=60, allow_redirects=False)
        if resp.status_code!=200: message.reply_text("Не понял запрос. Пример: «контактор 25А катушка 220В» или открой «📂 Категории»."); return
        bot_reply=resp.json()["choices"][0]["message"]["content"].strip() or "🤖 (пустой ответ)"
        chat_history[uid].append({"role":"assistant","content":bot_reply}); chat_history[uid]=clamp_history(chat_history[uid])
        message.reply_text(bot_reply)
    except Exception:
        traceback.print_exc(); message.reply_text("Упс, не разобрал. Попробуй «📂 Категории» и фильтры.")

# Reset
@app.on_message(filters.private & filters.command("reset"))
def reset_handler(_, message):
    chat_history[message.from_user.id]=[]; message.reply_text("🧹 Память очищена!")

# Завершение
def _graceful_exit(sig, frame):
    logging.getLogger().info("Stop signal received (%s). Exiting...", sig)
    try: app.stop()
    finally: os._exit(0)
signal.signal(signal.SIGTERM, _graceful_exit)
signal.signal(signal.SIGINT, _graceful_exit)

# ───────────── Запуск ─────────────
if __name__ == "__main__":
    try:
        log.info("✅ Бот запускается...")
        app.start()  # СНАЧАЛА стартуем Pyrogram

        # Логирование инфо о боте (НОВОЕ)
        try:
            me = app.get_me()
            log.info("🤖 Запущен бот: @%s (id=%s)", me.username, me.id)
        except Exception:
            traceback.print_exc()

        # После старта: загрузка каталога и таймер
        if CATALOG_URL:
            if not fetch_catalog(force=True):
                log.warning("Каталог не удалось загрузить на старте")
            periodic_refresh()

        # HTTP-хук
        threading.Thread(target=_run_http_server, daemon=True).start()

        idle()

    except Exception:
        traceback.print_exc(); sys.exit(1)
    finally:
        try: app.stop()
        except Exception: pass

























