from phBot import *
import QtBind
import json
import os
import time
import struct
import threading
import urllib.request

pName = 'R-QuestRunner'
pVersion = '1.7.0'
pAuthor = '_Apache_'

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
QUEST_FILE = os.path.join(PLUGIN_DIR, 'R-QuestRunner_quests.json')
CONFIG_FILE = os.path.join(PLUGIN_DIR, 'R-QuestRunner_config.json')

STUCK_MINUTES = 20        # bu kadar dakika ilerleme olmazsa "takıldı" uyarısı
NPC_REACH_DIST = 5        # NPC'ye bu mesafeden yakınsa "vardı" sayılır
WALK_TIMEOUT_MIN = 5      # NPC'ye bu sürede varamazsa uyarır
PB_POLL_SEC = 10          # Pushbullet cevabını kaç saniyede bir kontrol etsin

# ---------------- Durum ----------------
quests = []
active = None             # şu an yapılan görev
state = 'idle'            # idle / farming / walking / waiting_approval
auto_next = False
last_check = 0
last_quest_refresh = 0
pushbullet_token = ''
last_level = None
last_progress = None
last_progress_time = 0
stuck_warned = False
walk_started = 0
approval_answer = None    # 'yes' / 'no' (GUI ya da Pushbullet'ten gelir)
approval_since = 0
packet_log = False
current_quests = []       # oyundaki aktif görev isimleri
use_return = False        # teslime return scroll ile git
town_shop = False         # şehre dönünce önce alışveriş yap
custom_radius = None      # kullanıcının girdiği radius (boşsa görevin kendi radius'u)
return_started = 0
return_from = None        # scroll basıldığı andaki konum

RETURN_TIMEOUT_SEC = 60   # scroll basıldıktan sonra ışınlanma için bekleme
SHOP_WALK_TIMEOUT_MIN = 15  # alışveriş + NPC'ye yürüme için toplam süre

# ---------------- GUI ----------------
gui = QtBind.init(__name__, pName)

# Sol sütun: görev listeleri
QtBind.createLabel(gui, 'Görevler', 10, 5)
QtBind.createLabel(gui, 'Lv:', 62, 5)
tbLevel = QtBind.createLineEdit(gui, '', 82, 2, 35, 19)
QtBind.createButton(gui, 'btn_filter', ' Ara ', 120, 1)
cbAutoStart = QtBind.createCheckBox(gui, 'cb_autostart', 'Oto başlat', 200, 4)
lstQuests = QtBind.createList(gui, 10, 22, 260, 110)
QtBind.createLabel(gui, 'Üzerimdeki görevler (detay için seç):', 10, 137)
lstCurrent = QtBind.createList(gui, 10, 153, 260, 85)
QtBind.createLabel(gui, 'Durum:', 10, 244)
lblStatus = QtBind.createLabel(gui, 'Bekliyor', 55, 244)
cbReturn = QtBind.createCheckBox(gui, 'cb_return', 'Teslime Return Scroll ile git', 10, 262)
cbShop = QtBind.createCheckBox(gui, 'cb_shop', 'Şehirde önce alışveriş yap', 200, 262)

# Orta sütun: görev detayı
QtBind.createLabel(gui, 'Görev Detayı:', 280, 5)
lstDetail = QtBind.createList(gui, 280, 22, 220, 196)
QtBind.createLabel(gui, 'Radius:', 280, 226)
tbRadius = QtBind.createLineEdit(gui, '', 322, 223, 45, 19)
QtBind.createButton(gui, 'btn_radius', ' Uygula ', 372, 222)

# Sağ üst: geliştirici ve versiyon
QtBind.createLabel(gui, 'Geliştirici: %s' % pAuthor, 512, 2)
QtBind.createLabel(gui, 'Versiyon: v%s' % pVersion, 512, 14)

# Sağ sütun: kontroller
QtBind.createButton(gui, 'btn_start', ' Başlat ', 510, 32)
QtBind.createButton(gui, 'btn_stop', ' Durdur ', 578, 32)
QtBind.createButton(gui, 'btn_reload', ' Listeyi Yenile ', 510, 58)
QtBind.createButton(gui, 'btn_next', ' Seviyeme Uygun ', 510, 84)
cbAuto = QtBind.createCheckBox(gui, 'cb_auto', 'Bitince sıradakine geç', 510, 110)
cbPacket = QtBind.createCheckBox(gui, 'cb_packet', 'Paket kaydı', 510, 128)

QtBind.createLabel(gui, 'Pushbullet Token:', 510, 150)
tbPushbullet = QtBind.createLineEdit(gui, '', 510, 166, 135, 20)
QtBind.createButton(gui, 'btn_save_webhook', ' Kaydet ', 510, 190)
QtBind.createButton(gui, 'btn_test', ' Test ', 578, 190)

QtBind.createLabel(gui, 'Tekrarlı görev onayı:', 510, 220)
QtBind.createButton(gui, 'btn_yes', ' Evet ', 510, 237)
QtBind.createButton(gui, 'btn_no', ' Hayır ', 578, 237)

def set_status(text):
    QtBind.setText(gui, lblStatus, text)
    log('[%s] %s' % (pName, text))

# ---------------- Bildirimler ----------------
def _send_pushbullet(token, title, text):
    try:
        body = json.dumps({'type': 'note', 'title': title, 'body': text}).encode('utf-8')
        req = urllib.request.Request('https://api.pushbullet.com/v2/pushes', data=body, headers={
            'Content-Type': 'application/json', 'Access-Token': token,
            'User-Agent': 'phBot-R-QuestRunner'})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log('[%s] Pushbullet gönderilemedi: %s' % (pName, e))

def char_name():
    char = get_character_data()
    return char['name'] if char and char.get('name') else '?'

def notify(text):
    who = char_name()
    set_status(text)
    if pushbullet_token:
        threading.Thread(target=_send_pushbullet,
                         args=(pushbullet_token, 'R-QuestRunner - %s' % who, text), daemon=True).start()

# ---------------- Pushbullet'ten cevap okuma ----------------
def _poll_pushbullet_reply(since):
    # Telefondan Pushbullet ile "evet" ya da "hayır" yazılırsa onu cevap sayar
    global approval_answer
    while state == 'waiting_approval' and approval_answer is None and pushbullet_token:
        try:
            req = urllib.request.Request(
                'https://api.pushbullet.com/v2/pushes?active=true&modified_after=%f' % since,
                headers={'Access-Token': pushbullet_token, 'User-Agent': 'phBot-R-QuestRunner'})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode('utf-8'))
            for p in data.get('pushes', []):
                if str(p.get('title', '')).startswith('R-QuestRunner'):
                    continue   # kendi gönderdiğimiz bildirim
                txt = (str(p.get('body', '')) + ' ' + str(p.get('title', ''))).strip().lower()
                if txt.startswith('evet') or txt == 'e':
                    approval_answer = 'yes'
                    return
                if txt.startswith('hay') or txt == 'h':
                    approval_answer = 'no'
                    return
        except Exception as e:
            log('[%s] Pushbullet cevabı okunamadı: %s' % (pName, e))
        time.sleep(PB_POLL_SEC)

# ---------------- Config ----------------
def load_config():
    global pushbullet_token, use_return, town_shop, custom_radius
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            pushbullet_token = data.get('pushbullet', '')
            use_return = data.get('use_return', False)
            custom_radius = data.get('radius')
            town_shop = data.get('town_shop', False)
        except Exception:
            pass
    QtBind.setText(gui, tbPushbullet, pushbullet_token)
    QtBind.setChecked(gui, cbReturn, use_return)
    QtBind.setChecked(gui, cbShop, town_shop)
    QtBind.setText(gui, tbRadius, str(custom_radius) if custom_radius else '')

def save_config():
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump({'pushbullet': pushbullet_token,
                       'use_return': use_return, 'town_shop': town_shop,
                       'radius': custom_radius}, f)
        return True
    except Exception as e:
        set_status('Ayarlar kaydedilemedi: %s' % e)
        return False

def btn_save_webhook():
    global pushbullet_token
    pushbullet_token = QtBind.text(gui, tbPushbullet).strip()
    if save_config():
        set_status('Bildirim ayarları kaydedildi.')

def cb_return(checked):
    global use_return
    use_return = checked
    save_config()

def cb_shop(checked):
    global town_shop
    town_shop = checked
    save_config()

def btn_test():
    notify('Test bildirimi - R-QuestRunner çalışıyor ✅')

# ---------------- Görev listesi ----------------
def is_current(q):
    n = q['name'].lower()
    return any(n in c.lower() or c.lower() in n for c in current_quests)

def quest_label(q):
    tags = ''
    if is_current(q):
        tags += ' [ÜZERİMDE]'
    if q.get('repeat'):
        tags += ' [TEKRARLI]'
    return 'Lv%d - %s  (%s)%s' % (q['level'], q['name'], q.get('mob', '?'), tags)

level_filter = ''        # Lv kutusuna yazılan değer
auto_start_on_select = True
last_quest_sel = None

def parse_level_filter(text):
    # '' -> hepsi, '10' -> sadece 10, '1-10' -> 1 ile 10 arası
    text = text.strip().replace(' ', '')
    if not text:
        return None
    try:
        if '-' in text:
            a, b = text.split('-', 1)
            return (int(a), int(b))
        n = int(text)
        return (n, n)
    except ValueError:
        return 'hata'

def filtered_quests():
    rng = parse_level_filter(level_filter)
    if rng is None or rng == 'hata':
        return quests
    lo, hi = min(rng), max(rng)
    return [q for q in quests if lo <= q['level'] <= hi]

def fill_quest_list():
    global last_quest_sel
    QtBind.clear(gui, lstQuests)
    for q in filtered_quests():
        QtBind.append(gui, lstQuests, quest_label(q))
    last_quest_sel = None

def btn_filter():
    # "Ara" butonu: Lv kutusundaki değere göre listeyi hemen süz
    apply_level_filter(QtBind.text(gui, tbLevel), force=True)

def check_level_filter():
    # Lv kutusundaki yazı değişince listeyi kendiliğinden süz
    apply_level_filter(QtBind.text(gui, tbLevel))

def apply_level_filter(text, force=False):
    global level_filter
    if text == level_filter and not force:
        return
    level_filter = text
    fill_quest_list()
    rng = parse_level_filter(text)
    if rng == 'hata':
        set_status('Lv kutusuna sayı yaz (örn. 10 ya da 1-10).')
    elif rng is None:
        set_status('Tüm görevler: %d' % len(quests))
    else:
        n = len(filtered_quests())
        if n:
            set_status('Lv %s görevleri: %d' % (text.strip(), n))
        else:
            set_status('Lv %s için listede görev yok (JSON\'a eklenmemiş).' % text.strip())

def check_quest_selection():
    # Listeden görev seçilince bot otomatik başlasın
    global last_quest_sel
    sel = QtBind.text(gui, lstQuests)
    if not sel or sel == last_quest_sel:
        return
    last_quest_sel = sel
    if not auto_start_on_select:
        return
    q = selected_quest()
    if q and not (active is q and state != 'idle'):
        start_quest(q)

def cb_autostart(checked):
    global auto_start_on_select
    auto_start_on_select = checked

def load_quests():
    global quests
    if not os.path.exists(QUEST_FILE):
        set_status('Görev dosyası yok: R-QuestRunner_quests.json')
        quests = []
    else:
        try:
            with open(QUEST_FILE, 'r', encoding='utf-8') as f:
                quests = sorted(json.load(f), key=lambda q: q['level'])
        except Exception as e:
            set_status('Görev dosyası okunamadı: %s' % e)
            quests = []
    fill_quest_list()

def selected_quest():
    label = QtBind.text(gui, lstQuests)
    for q in quests:
        if quest_label(q) == label:
            return q
    return None

ICON_DONE = '🟢'       # bitti, teslime hazır
ICON_PROGRESS = '🟡'   # devam ediyor
ICON_WAITING = '🔴'    # bekliyor, henüz ilerleme yok
UNKNOWN_PROGRESS = 255 # oyun bu değeri "ilerleme bilgisi yok" için kullanıyor gibi
quest_debug_logged = False
current_quest_data = {}   # listedeki satır -> görev verisi
last_detail_sel = None

def objective_progress(o):
    p = o.get('progress')
    if isinstance(p, int) and not isinstance(p, bool) and 0 <= p < UNKNOWN_PROGRESS:
        return p
    return None

def quest_state(qq):
    # 'done' / 'progress' / 'waiting'
    if not isinstance(qq, dict):
        return 'waiting'
    if qq.get('completed') or qq.get('objectives_completed'):
        return 'done'
    for o in qq.get('objectives') or []:
        if o.get('completed') or (objective_progress(o) or 0) > 0:
            return 'progress'
    return 'waiting'

def quest_icon(qq):
    return {'done': ICON_DONE, 'progress': ICON_PROGRESS}.get(quest_state(qq), ICON_WAITING)

STATE_TEXT = {'done': 'Bitti, teslime hazır', 'progress': 'Devam ediyor', 'waiting': 'Bekliyor'}

def npc_names(ids):
    # Yakındaki NPC'lerden numarası eşleşenlerin adını bulur
    found = []
    try:
        for n in (get_npcs() or {}).values():
            if n.get('model') in ids or n.get('id') in ids:
                found.append(n.get('name', '?'))
    except Exception:
        pass
    return found

def json_quest_for(name):
    for q in quests:
        if q['name'].lower() in name.lower() or name.lower() in q['name'].lower():
            return q
    return None

DETAIL_WIDTH = 30   # detay panelinde bir satıra sığan karakter sayısı

def wrap(text, width=DETAIL_WIDTH, indent='   '):
    # Uzun yazıyı panele sığacak şekilde satırlara böler
    lines, cur = [], ''
    for w in str(text).split():
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + ' ' + w).strip()
    if cur:
        lines.append(cur)
    out = [indent + lines[0]] if lines else [indent]
    out += [indent + '   ' + l for l in lines[1:]]
    return out

def show_detail(qq):
    QtBind.clear(gui, lstDetail)
    add = lambda t: QtBind.append(gui, lstDetail, t)
    name = qq.get('name', '?')
    st = quest_state(qq)
    add('◆ Görev')
    for line in wrap(name):
        add(line)
    add('◆ Durum')
    add('   %s %s' % (quest_icon(qq), STATE_TEXT[st]))
    add('◆ Hedefler')
    objs = qq.get('objectives') or []
    if not objs:
        add('   (bilgi yok)')
    for o in objs:
        mark = '✔' if o.get('completed') else '•'
        for line in wrap('%s %s' % (mark, o.get('notice', '?'))):
            add(line)
    add('◆ NPC')
    ids = qq.get('npc') or []
    names = npc_names(ids)
    if names:
        for line in wrap(', '.join(names)):
            add(line)
    elif ids:
        add('   Numara: %s' % ', '.join(str(i) for i in ids))
        add('   (adı NPC yakınken görünür)')
    else:
        add('   (bilgi yok)')
    jq = json_quest_for(name)
    if jq:
        add('◆ Plugin ayarı')
        for line in wrap('Lv%d, mob: %s' % (jq['level'], jq.get('mob', '?'))):
            add(line)
        add('   Kasılma: %s, %s (r%s)' % (jq['x'], jq['y'], quest_radius(jq)))
        if jq.get('item'):
            for line in wrap('Toplanacak: %s x%s' % (jq['item'], jq.get('count', '?'))):
                add(line)
        add('   Tür: %s' % ('Tekrarlı' if jq.get('repeat') else 'Tek seferlik'))
        if jq.get('npc', {}).get('name'):
            for line in wrap('Teslim NPC: %s' % jq['npc']['name']):
                add(line)

def check_detail_selection():
    global last_detail_sel
    sel = QtBind.text(gui, lstCurrent)
    if sel and sel != last_detail_sel and sel in current_quest_data:
        last_detail_sel = sel
        show_detail(current_quest_data[sel])

def refresh_current_quests():
    # phBot sürümünde get_quests varsa oyundaki aktif görevleri okur
    global current_quests, current_quest_data, last_detail_sel
    if 'get_quests' not in globals():
        return
    try:
        data = get_quests()
        items = list(data.values() if isinstance(data, dict) else (data or []))
        names = []
        qdata = {}
        for qq in items:
            if not isinstance(qq, dict) or not qq.get('name'):
                continue
            label = '%s %s' % (quest_icon(qq), qq['name'])
            names.append(label)
            qdata[label] = qq
    except Exception as e:
        log('[%s] Aktif görevler okunamadı: %s' % (pName, e))
        return
    if names != current_quests:
        sel = QtBind.text(gui, lstCurrent)
        current_quests = names
        QtBind.clear(gui, lstCurrent)
        for n in names:
            QtBind.append(gui, lstCurrent, n)
        fill_quest_list()
        last_detail_sel = None   # seçili görevin detayı yenilensin
    current_quest_data = qdata
    # Seçili görevin bilgisi değiştiyse detayı güncelle
    if last_detail_sel in qdata:
        show_detail(qdata[last_detail_sel])

# ---------------- Görev akışı ----------------
def reset_progress():
    global last_progress, last_progress_time, stuck_warned
    last_progress = None
    last_progress_time = time.time()
    stuck_warned = False

def quest_radius(q):
    # Kutuya radius yazıldıysa o, yazılmadıysa görevin kendi radius'u
    return custom_radius if custom_radius else q.get('radius', 50)

def btn_radius():
    global custom_radius
    text = QtBind.text(gui, tbRadius).strip()
    if not text:
        custom_radius = None
        save_config()
        set_status('Radius: görevlerin kendi değeri kullanılacak.')
        return
    try:
        r = int(text)
        if r <= 0:
            raise ValueError
    except ValueError:
        set_status('Radius için pozitif bir sayı yaz (örn. 50).')
        return
    custom_radius = r
    save_config()
    if active and state == 'farming':
        set_training_radius(r)
        set_status('Radius %d yapıldı ve şu anki göreve uygulandı.' % r)
    else:
        set_status('Radius %d kaydedildi.' % r)

def start_quest(q):
    global active, state
    char = get_character_data()
    if char and char.get('level', 0) < q['level']:
        set_status('Seviye yetmiyor: %s için Lv%d lazım' % (q['name'], q['level']))
        return
    stop_bot()
    set_training_position(q['region'], q['x'], q['y'], q.get('z', 0))
    set_training_radius(quest_radius(q))
    active = q
    state = 'farming'
    reset_progress()
    start_bot()
    notify('▶️ Başladı: %s -> %s' % (q['name'], q.get('mob', '?')))

def btn_start():
    q = selected_quest()
    if not q:
        set_status('Önce listeden bir görev seç.')
        return
    start_quest(q)

def btn_stop():
    global active, state
    stop_bot()
    active = None
    state = 'idle'
    set_status('Durduruldu.')

def btn_reload():
    load_quests()
    set_status('%d görev yüklendi.' % len(quests))

def next_quest_for_level(after=None):
    char = get_character_data()
    lvl = char.get('level', 0) if char else 0
    start_idx = quests.index(after) + 1 if after in quests else 0
    for q in quests[start_idx:]:
        if q['level'] <= lvl:
            return q
    return None

def btn_next():
    q = next_quest_for_level()
    if q:
        start_quest(q)
    else:
        set_status('Seviyene uygun görev bulunamadı.')

def cb_auto(checked):
    global auto_next
    auto_next = checked

def cb_packet(checked):
    global packet_log
    packet_log = checked
    set_status('Paket kaydı %s.' % ('açık' if checked else 'kapalı'))

def btn_yes():
    global approval_answer
    if state == 'waiting_approval':
        approval_answer = 'yes'

def btn_no():
    global approval_answer
    if state == 'waiting_approval':
        approval_answer = 'no'

# ---------------- İlerleme ----------------
def count_item(name):
    inv = get_inventory()
    if not inv:
        return 0
    total = 0
    for item in inv['items']:
        if item and name.lower() in item['name'].lower():
            total += item['quantity']
    return total

def track_progress(value):
    global last_progress, last_progress_time, stuck_warned
    now = time.time()
    if value != last_progress:
        last_progress = value
        last_progress_time = now
        stuck_warned = False
    elif not stuck_warned and now - last_progress_time > STUCK_MINUTES * 60:
        stuck_warned = True
        notify('⚠️ %d dakikadır ilerleme yok, bot takılmış olabilir: %s' % (STUCK_MINUTES, active['name']))

def quest_done(q):
    if q.get('item') and q.get('count'):
        have = count_item(q['item'])
        QtBind.setText(gui, lblStatus, '%s: %d/%d %s' % (q['name'], have, q['count'], q['item']))
        track_progress(have)
        return have >= q['count']
    if 'get_quests' in globals():
        try:
            data = get_quests()
            items = data.values() if isinstance(data, dict) else (data or [])
            for qq in items:
                if isinstance(qq, dict) and q['name'].lower() in str(qq.get('name', '')).lower():
                    return bool(qq.get('completed', False))
        except Exception:
            pass
    return False

def check_level():
    global last_level
    char = get_character_data()
    if not char:
        return
    lvl = char.get('level')
    if last_level is not None and lvl and lvl > last_level:
        notify('⬆️ Level atladı: %d -> %d' % (last_level, lvl))
    last_level = lvl

# ---------------- NPC'ye gidip teslim ----------------
def distance_to(npc):
    pos = get_position()
    if not pos:
        return 9999
    return ((pos['x'] - npc['x']) ** 2 + (pos['y'] - npc['y']) ** 2) ** 0.5

def find_npc_uid(npc_name):
    npcs = get_npcs() or {}
    for uid, n in npcs.items():
        if npc_name.lower() in str(n.get('name', '')).lower():
            return uid
    return None

def find_return_scroll_slot():
    inv = get_inventory()
    if not inv:
        return None
    for slot, item in enumerate(inv['items']):
        if slot < 13 or not item:   # 0-12 ekipman slotları
            continue
        if 'return scroll' in item['name'].lower():
            return slot
    return None

def use_return_scroll():
    slot = find_return_scroll_slot()
    if slot is None:
        return False
    # Item kullanma paketi: slot + return scroll tipi
    inject_joymax(0x704C, struct.pack('<B', slot) + b'\xEC\x09', True)
    return True

def walk_to_npc(q):
    global state, walk_started
    npc = q['npc']
    walk_started = time.time()
    if town_shop and 'region' in npc:
        # Kasılma alanını NPC'ye ayarla: bot önce şehirde alışverişini yapar, sonra NPC'ye yürür
        set_training_position(npc['region'], npc['x'], npc['y'], npc.get('z', 0))
        set_training_radius(1)
        state = 'walking_bot'
        start_bot()
        notify('🛒 Şehirde alışveriş yapıp %s NPC\'sine gidiyorum.' % npc['name'])
    else:
        if town_shop:
            log('[%s] NPC için "region" yazılmamış, alışveriş atlanıyor.' % pName)
        state = 'walking'
        move_to(npc['x'], npc['y'], npc.get('z', 0))
        notify('🚶 %s NPC\'sine yürüyorum.' % npc['name'])

def go_to_npc(q):
    global state, return_started, return_from
    npc = q['npc']
    stop_bot()
    if use_return:
        if use_return_scroll():
            state = 'returning'
            return_started = time.time()
            return_from = get_position()
            notify('📜 %s bitti, Return Scroll ile şehre dönüyorum.' % q['name'])
            return
        notify('⚠️ Envanterde Return Scroll yok, %s NPC\'sine yürüyerek gidiyorum.' % npc['name'])
    walk_to_npc(q)

def teleported():
    pos = get_position()
    if not pos or not return_from:
        return False
    if pos.get('region') != return_from.get('region'):
        return True
    return ((pos['x'] - return_from['x']) ** 2 + (pos['y'] - return_from['y']) ** 2) ** 0.5 > 300

def turn_in(q):
    npc = q['npc']
    uid = find_npc_uid(npc['name'])
    if uid is None:
        notify('⚠️ %s NPC\'si yakında bulunamadı, teslimi sen yap: %s' % (npc['name'], q['name']))
        return
    uid_hex = struct.pack('<I', uid).hex()
    # 1) NPC'yi seç
    inject_joymax(0x7045, bytes.fromhex(uid_hex), False)
    # 2) Teslim paketleri (R-QuestRunner_quests.json içindeki "turnin_packets")
    packets = npc.get('turnin_packets') or q.get('turnin_packets')
    if not packets:
        notify('📍 %s yanındayım. Teslim paketi tanımlı değil, teslimi sen yap: %s' % (npc['name'], q['name']))
        return
    for p in packets:
        data = p.get('data', '').replace('{npc}', uid_hex).replace(' ', '')
        inject_joymax(int(p['opcode'], 16), bytes.fromhex(data), bool(p.get('encrypted', False)))
        time.sleep(float(p.get('delay', 0.5)))
    notify('✅ Teslim edildi: %s' % q['name'])

def after_turnin(q):
    global active, state, approval_answer, approval_since
    if q.get('repeat'):
        state = 'waiting_approval'
        approval_answer = None
        approval_since = time.time()
        notify('🔁 "%s" tekrarlı bir görev. Yeniden yapayım mı? (Evet/Hayır yaz)' % q['name'])
        if pushbullet_token:
            threading.Thread(target=_poll_pushbullet_reply, args=(approval_since,), daemon=True).start()
        return
    go_next(q)

def go_next(q):
    global active, state
    active = None
    state = 'idle'
    if auto_next:
        nq = next_quest_for_level(after=q)
        if nq:
            start_quest(nq)
        else:
            notify('Seviyene uygun başka görev yok, bekliyorum.')

# ---------------- phBot event'leri ----------------
def handle_silkroad(opcode, data):
    # Paket kaydı açıksa client -> server paketlerini loga yazar
    if packet_log and opcode not in (0x2002, 0x7021):   # ping ve yürüme paketlerini atla
        log('[%s] PAKET 0x%04X %s' % (pName, opcode, data.hex() if data else ''))
    return True

def event_loop():
    global last_check, last_quest_refresh, state, approval_answer
    try:
        check_detail_selection()   # tıklanan görevin detayını hemen göster
        check_level_filter()       # Lv kutusu değiştiyse listeyi süz
        check_quest_selection()    # seçilen görevi otomatik başlat
    except Exception:
        pass
    now = time.time()
    if now - last_check < 3:
        return
    last_check = now
    try:
        check_level()
        if now - last_quest_refresh > 10:
            last_quest_refresh = now
            refresh_current_quests()

        if state == 'farming' and active and quest_done(active):
            stop_bot()
            if active.get('npc'):
                go_to_npc(active)
            else:
                notify('✅ TAMAMLANDI: %s - NPC tanımlı değil, teslimi sen yap.' % active['name'])
                after_turnin(active)

        elif state == 'returning' and active:
            if teleported():
                walk_to_npc(active)
            elif now - return_started > RETURN_TIMEOUT_SEC:
                notify('⚠️ Return Scroll çalışmadı, %s NPC\'sine yürüyerek gidiyorum.' % active['npc']['name'])
                walk_to_npc(active)

        elif state == 'walking_bot' and active:
            npc = active['npc']
            if distance_to(npc) <= NPC_REACH_DIST:
                stop_bot()
                turn_in(active)
                after_turnin(active)
            elif now - walk_started > SHOP_WALK_TIMEOUT_MIN * 60:
                stop_bot()
                notify('⚠️ %d dakikadır %s NPC\'sine varamadım, kontrol et.' % (SHOP_WALK_TIMEOUT_MIN, npc['name']))
                state = 'idle'

        elif state == 'walking' and active:
            npc = active['npc']
            if distance_to(npc) <= NPC_REACH_DIST:
                turn_in(active)
                after_turnin(active)
            elif now - walk_started > WALK_TIMEOUT_MIN * 60:
                notify('⚠️ %d dakikadır %s NPC\'sine varamadım, kontrol et.' % (WALK_TIMEOUT_MIN, npc['name']))
                state = 'idle'
            else:
                move_to(npc['x'], npc['y'], npc.get('z', 0))

        elif state == 'waiting_approval' and active and approval_answer:
            q = active
            if approval_answer == 'yes':
                notify('👍 Onay geldi, %s yeniden başlıyor.' % q['name'])
                start_quest(q)
            else:
                notify('👌 %s tekrar yapılmayacak.' % q['name'])
                go_next(q)
            approval_answer = None
    except Exception as e:
        log('[%s] Hata: %s' % (pName, e))

QtBind.setChecked(gui, cbAutoStart, True)
load_quests()
load_config()
refresh_current_quests()
log('[%s] v%s yüklendi.' % (pName, pVersion))
