"""
Bot auto-accept + auto-reply TikTok Message Requests.

Versi ini TIDAK lagi menebak-nebak koordinat pixel / innerText. Semua elemen
diambil dari atribut `data-e2e` asli milik TikTok (sudah dicek langsung di DOM):

  - daftar chat        : [data-e2e="dm-new-conversation-item"]  (punya data-conv-id unik)
  - menu Message req.  : [class*="DivRequestGroup"]
  - kotak Accept       : [data-e2e="dm-new-stranger-accept"]  -> tombol role=button "Accept"
  - isi pesan          : [data-e2e="dm-new-message-text"]
  - nama di header     : [data-e2e="dm-new-chat-nickname"]
  - kotak ketik        : [data-e2e="message-input-area"] [contenteditable="true"]

Chat yang sudah dibalas disimpan ke `sudah_dibalas.json` (berdasarkan conv-id),
jadi walaupun bot di-restart, TIDAK akan kirim dobel.

Stop bot: tekan Ctrl+C di terminal.
"""

import json
import os
import random
import re
import sys
import time

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# ================== KONFIGURASI ==================

TEKS_BALASAN = (
    "boleh, tapi source codenya aku jual ya, nanti setelah pembayaran aku kasih source codenya sekalian README untuk cara install dan menjalankannya gimana?\n\n"
    "untuk harga sendiri 50rb aja yaa ga mahal2\n"
    "kalau mau lanjut chat WA aja ya\n"
    "085655691457"
)

# Penanda bahwa chat sudah pernah dibalas (dicek di isi pesan).
PENANDA_SUDAH_DIBALAS = ["source codenya aku jual", "085655691457"]

# Kata kunci dicocokkan per-KATA (pakai regex word boundary), bukan substring.
# Jadi "sc" tidak lagi cocok dengan "description", "web" tidak cocok dengan "webtoon" dsb.
# Akhiran "nya" tetap dihitung (codenya, kodenya, scnya, portonya, ...).
KEYWORDS = [
    "github", "source code", "sourcecode", "sc", "kode", "code", "minta", "spill",
    "link", "beli", "buy", "harga", "bagi", "portofolio", "portfolio", "porto",
    "forto", "web", "website", "mau", "info", "inpo", "repo", "repositori", "boleh",
    "bisa", "tutor", "tutorial", "template", "projek", "project", "script", "skrip",
    "share", "kirim", "dapetin", "kasih", "ajarin",
]
_POLA_KEYWORD = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in KEYWORDS) + r")(nya|ny)?\b",
    re.IGNORECASE,
)

# Jika orang membagikan video VT portofolio ke DM, otomatis balas!
BALAS_SHARED_VIDEO = True

# Tipe pesan yang bukan teks dan diabaikan (cuma stiker/gambar tanpa video)
PREVIEW_DILEWATI = ["sent a sticker", "sent a photo", "sent an image"]

FILE_SESI = "./sesi_tiktok"
FILE_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sudah_dibalas.json")

# Jeda manusiawi antar balasan (acak agar tidak terbaca sebagai mesin bot konstan)
JEDA_MIN_DETIK = 12.0         # detik minimal
JEDA_MAKS_DETIK = 22.0        # detik maksimal
JEDA_CEK_ULANG = 30           # detik, kalau tidak ada request baru
MAKS_SCROLL_TANPA_BARU = 5    # berhenti scroll kalau 5x scroll berturut-turut tidak ada item baru

# Mode uji: `python bot_tiktok.py --dry` -> cek semua chat TANPA klik Accept / kirim pesan
# dan tanpa menyimpan apa pun ke sudah_dibalas.json.
DRY_RUN = "--dry" in sys.argv

# ================== SELECTOR ==================

SEL_LIST = '[data-e2e="dm-new-conversation-list"]'
SEL_ITEM = '[data-e2e="dm-new-conversation-item"]'
SEL_REQUEST_GROUP = '[class*="DivRequestGroup"]'
SEL_STRANGER_BOX = '[data-e2e="dm-new-stranger-accept"]'
SEL_MSG_TEXT = '[data-e2e="dm-new-message-text"]'
SEL_CHAT_NICK = '[data-e2e="dm-new-chat-nickname"]'
SEL_EDITOR = '[data-e2e="message-input-area"] [contenteditable="true"]'


# ================== PENYIMPANAN ==================

def muat_data():
    data = {}
    if os.path.exists(FILE_DATA):
        try:
            with open(FILE_DATA, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    for k in ("dibalas", "dilewati", "tertunda", "bukan_request"):
        data.setdefault(k, {})
    # Reset daftar dilewati agar request yang belum sempat dibalas (misal kata kunci baru atau video VT) bisa dicek ulang
    data["dilewati"] = {}
    return data


def simpan_data(data):
    if DRY_RUN:
        return
    with open(FILE_DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def log(msg):
    teks = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(teks, flush=True)
    except Exception:
        try:
            print(teks.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


# ================== AKSI HALAMAN ==================

def sedang_di_message_requests(page):
    """
    Cek apakah saat ini BENAR-BENAR sedang berada di dalam sub-menu Message requests.
    Di TikTok Web, di panel kiri ada h2 yang posisinya visible di top ~20px:
      - Di dalam Message requests: text mengandung 'message requests' atau 'permintaan pesan'
      - Di Inbox utama: text bertuliskan 'messages' atau 'pesan'
    """
    try:
        return page.evaluate("""() => {
            for (const h2 of document.querySelectorAll('h2')) {
                const r = h2.getBoundingClientRect();
                if (r.left > 50 && r.left < 350 && r.top >= 0 && r.top < 60 && r.width > 20) {
                    const t = (h2.innerText || '').toLowerCase().trim();
                    return t.includes('request') || t.includes('permintaan');
                }
            }
            const header = document.querySelector('[class*="DivHeaderLeft"]');
            if (header) {
                const t = (header.innerText || '').toLowerCase().trim();
                return t.includes('request') || t.includes('permintaan');
            }
            return false;
        }""")
    except Exception:
        return False


def masuk_ke_message_requests(page):
    """
    Mengarahkan panel kiri agar PASTI masuk ke sub-menu Message requests.
    Jika sudah di dalam, langsung return True.
    Jika masih di Inbox utama:
      1. Reset scroll ke paling atas (scrollTop = 0) agar DivRequestGroup di-mount kembali oleh virtual DOM TikTok.
      2. Klik tombol Message requests menggunakan Playwright real pointer/mouse event (bukan JS .click()).
      3. Jika tenggelam karena banyak chat baru, gulir bertahap ke bawah hingga ditemukan.
      4. Verifikasi header h2 telah berubah menjadi 'Message requests'.
    """
    if sedang_di_message_requests(page):
        return True

    log("   [NAVIGASI] Mencari dan mengeklik menu 'Message requests'...")

    # 1. Scroll daftar kiri ke paling atas dulu agar tombol yang di atas terlihat jelas
    scroll_daftar_ke_atas(page)
    time.sleep(1.0)

    # 2. Coba cari dan klik tombol Message requests
    for percobaan in range(8):
        if sedang_di_message_requests(page):
            log("   [NAVIGASI] Berhasil berada di Message requests!")
            return True

        # Opsi 1: Locator Playwright DivRequestGroup (event klik browser asli)
        loc = page.locator('[class*="DivRequestGroup"]').first
        if loc.count() > 0:
            try:
                loc.scroll_into_view_if_needed(timeout=2000)
                loc.click()
                time.sleep(2.0)
                if sedang_di_message_requests(page):
                    log("   [NAVIGASI] Berhasil masuk ke Message requests!")
                    return True
            except Exception:
                pass

        # Opsi 2: Koordinat tombol grup / button di panel kiri
        coords = page.evaluate("""() => {
            for (const el of document.querySelectorAll('div[role="button"], div, p, span, a')) {
                if (el.tagName.toLowerCase() === 'h2') continue;
                const r = el.getBoundingClientRect();
                if (r.left >= 20 && r.left < 380 && r.height >= 18 && r.height <= 120) {
                    const t = (el.innerText || '').toLowerCase().trim();
                    if ((t.includes('message requests') || t.includes('permintaan pesan')) && !t.includes('shared')) {
                        const target = el.closest('[role="button"]') || el;
                        target.scrollIntoView({ block: 'center', inline: 'center' });
                        const tr = target.getBoundingClientRect();
                        return { x: tr.left + tr.width / 2, y: tr.top + tr.height / 2 };
                    }
                }
            }
            return null;
        }""")

        if coords:
            # WAJIB gunakan page.mouse.click agar React event listener TikTok menerima event pointer user asli
            page.mouse.click(coords["x"], coords["y"])
            time.sleep(2.0)
            if sedang_di_message_requests(page):
                log("   [NAVIGASI] Berhasil masuk ke Message requests!")
                return True

        # Jika di bagian atas belum ketemu (karena tenggelam di bawah chat baru),
        # scroll pelan ke bawah di panel kiri
        if percobaan < 7:
            page.evaluate("""() => {
                const sc = Array.from(document.querySelectorAll('div')).find(d => {
                    const r = d.getBoundingClientRect();
                    return r.left < 400 && r.width > 150 && d.scrollHeight > d.clientHeight + 50;
                });
                if (sc) sc.scrollTop += 350;
            }""")
            page.mouse.move(200, 350)
            page.mouse.wheel(0, 350)
            time.sleep(1.0)

    # Verifikasi akhir
    if sedang_di_message_requests(page):
        return True

    log("   [NAVIGASI] Tidak bisa menemukan menu 'Message requests' (atau memang semua permintaan sudah habis).")
    return False


def ambil_daftar_item(page):
    """Ambil semua item chat yang sedang ter-render: conv-id, nama, preview."""
    return page.evaluate(
        """(sel) => Array.from(document.querySelectorAll(sel)).map(el => {
            const nick = el.querySelector('[data-e2e="dm-new-conversation-nickname"]');
            const prev = el.querySelector('[class*="SpanInfoExtract"]');
            return {
                id: el.getAttribute('data-conv-id') || '',
                nama: nick ? nick.innerText.trim() : '',
                preview: prev ? prev.innerText.trim() : '',
            };
        })""",
        SEL_ITEM,
    )


def scroll_daftar(page):
    """Scroll daftar chat kiri ke bawah (untuk memuat request lama)."""
    try:
        page.evaluate("""() => {
            const sc = Array.from(document.querySelectorAll('div')).find(d => {
                const r = d.getBoundingClientRect();
                return r.left < 400 && r.width > 150 && d.scrollHeight > d.clientHeight + 50;
            });
            if (sc) sc.scrollTop += 500;
        }""")
        page.mouse.move(200, 350)
        page.mouse.wheel(0, 500)
    except Exception:
        pass
    time.sleep(1.5)


def scroll_daftar_ke_atas(page):
    """Reset scroll daftar chat kiri langsung ke paling atas (scrollTop = 0)."""
    try:
        page.evaluate("""() => {
            const sc = Array.from(document.querySelectorAll('div')).find(d => {
                const r = d.getBoundingClientRect();
                return r.left < 400 && r.width > 150 && d.scrollHeight > d.clientHeight + 50;
            });
            if (sc) sc.scrollTop = 0;
        }""")
        page.mouse.move(200, 300)
        page.mouse.wheel(0, -1000)
    except Exception:
        pass
    time.sleep(1.0)


def buka_chat(page, conv_id, nama):
    """Klik item berdasarkan conv-id lalu tunggu sampai panel kanan benar-benar pindah."""
    item = page.locator(f'{SEL_ITEM}[data-conv-id="{conv_id}"]')
    if item.count() == 0:
        return False
    item.first.scroll_into_view_if_needed()
    item.first.click()

    # Tunggu header / chatbox kanan merespon
    try:
        if nama and nama.strip():
            page.wait_for_function(
                """([sel, nama]) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const norm = s => s.replace(/\\s+/g, ' ').trim().toLowerCase();
                    const a = norm(el.innerText);
                    const b = norm(nama);
                    return a === b || a.includes(b) || b.includes(a);
                }""",
                arg=[SEL_CHAT_NICK, nama],
                timeout=5000,
            )
        else:
            page.locator(SEL_CHAT_NICK).first.wait_for(state="visible", timeout=4000)
    except PWTimeout:
        # Jika nama ada perbedaan emoji atau format, tapi chatbox kanan sudah aktif, tetap lanjut
        pass

    time.sleep(1.0)  # beri waktu pesan ter-render
    return True


def ambil_isi_pesan(page):
    return [t.strip() for t in page.locator(SEL_MSG_TEXT).all_inner_texts()]


def klik_accept(page):
    """Klik tombol Accept yang ASLI (di dalam kotak stranger), lalu tunggu kotak ketik muncul."""
    box = page.locator(SEL_STRANGER_BOX)
    if box.count() == 0:
        return False
    tombol = box.locator('[role="button"]').filter(
        has_text=re.compile(r"^\s*(accept|terima)\s*$", re.IGNORECASE)
    )
    if tombol.count() == 0:
        log("   [GAGAL] Tombol Accept tidak ditemukan di kotak request.")
        return False
    tombol.first.click()
    # tunggu kotak Accept hilang = request sudah diterima
    try:
        box.first.wait_for(state="hidden", timeout=10000)
    except PWTimeout:
        log("   [GAGAL] Sudah klik Accept tapi kotak request tidak hilang.")
        return False
    try:
        page.locator(SEL_EDITOR).first.wait_for(state="visible", timeout=10000)
    except PWTimeout:
        log("   [GAGAL] Sudah klik Accept tapi kotak ketik tidak muncul dalam 10 detik.")
        return False
    # Setelah Accept, TikTok me-render ulang panel (daftar kiri pindah ke inbox utama,
    # kotak ketik dibuat ulang). Kalau langsung paste, teksnya masuk ke elemen lama -> kosong.
    time.sleep(2.5)
    return True


# Paste lewat event sintetis -> TIDAK pakai clipboard Windows, jadi tidak bisa
# "kosong" atau ketukar kalau kamu sedang copy sesuatu sambil bot jalan.
JS_PASTE = """([sel, t]) => {
    const el = document.querySelector(sel);
    if (!el) return false;
    el.focus();
    const dt = new DataTransfer();
    dt.setData('text/plain', t);
    el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
    return true;
}"""

JS_FOKUS = """(sel) => {
    const el = document.querySelector(sel);
    if (!el) return false;
    el.focus();
    return el === document.activeElement || el.contains(document.activeElement);
}"""


def isi_editor(page):
    ed = page.locator(SEL_EDITOR)
    return ed.first.inner_text() if ed.count() else ""


def kosongkan_editor(page):
    page.evaluate(JS_FOKUS, SEL_EDITOR)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    time.sleep(0.3)


def teks_lengkap(teks):
    return "50rb" in teks and "085655691457" in teks


def cek_terkena_rate_limit(page):
    """Cek apakah TikTok menampilkan peringatan batas pengiriman pesan."""
    try:
        teks = page.locator('[data-e2e="dm-new-chatbox"], [data-e2e="dm-warning"], body').all_inner_texts()
        gabung = " ".join(teks).lower()
        if "too fast" in gabung or "take a rest" in gabung or "terlalu cepat" in gabung:
            return True
    except Exception:
        pass
    return False


def kirim_balasan(page):
    """Paste teks utuh dalam 1 bubble (maks 3x percobaan), verifikasi, baru tekan Enter."""
    for percobaan in range(1, 4):
        try:
            page.locator(SEL_EDITOR).first.wait_for(state="visible", timeout=8000)
            if isi_editor(page).strip():
                kosongkan_editor(page)
            page.evaluate(JS_PASTE, [SEL_EDITOR, TEKS_BALASAN])
            time.sleep(0.8)
            isi = isi_editor(page)
            if teks_lengkap(isi):
                break
            log(f"   [ULANG {percobaan}/3] Kotak ketik belum terisi lengkap: {isi[:40]!r}")
            if isi.strip():
                kosongkan_editor(page)
        except Exception as e:
            log(f"   [ULANG {percobaan}/3] {type(e).__name__}: {str(e)[:80]}")
        time.sleep(2.0)
    else:
        log("   [GAGAL] Tidak bisa mengisi kotak ketik setelah 3x percobaan.")
        return False

    # pastikan fokus masih di kotak ketik sebelum Enter
    if not page.evaluate(JS_FOKUS, SEL_EDITOR):
        log("   [GAGAL] Kotak ketik tidak bisa difokus, batal kirim.")
        return False

    jumlah_sebelum = page.locator(SEL_MSG_TEXT).count()
    page.keyboard.press("Enter")
    time.sleep(1.5)

    if cek_terkena_rate_limit(page):
        log("   [RATE LIMIT] TikTok memblokir pesan: 'You are sending messages too fast. Take a rest.'")
        return "rate_limited"

    # verifikasi bubble benar-benar terkirim
    try:
        page.wait_for_function(
            """([sel, n]) => {
                const els = document.querySelectorAll(sel);
                if (els.length <= n) return false;
                return els[els.length - 1].innerText.includes('source codenya aku jual');
            }""",
            arg=[SEL_MSG_TEXT, jumlah_sebelum],
            timeout=10000,
        )
    except PWTimeout:
        if cek_terkena_rate_limit(page):
            log("   [RATE LIMIT] Pesan gagal terkirim karena dibatasi TikTok ('too fast').")
            return "rate_limited"
        log("   [PERINGATAN] Enter sudah ditekan tapi bubble balasan belum terlihat.")
        return False

    terakhir = page.locator(SEL_MSG_TEXT).last.inner_text()
    if "085655691457" not in terakhir:
        log("   [PERINGATAN] Pesan terkirim, tapi nomor WA tidak ikut muncul "
            "(kemungkinan disaring TikTok).")
    return True


# ================== LOGIKA UTAMA ==================

def proses_satu_item(page, item, data):
    """Return "dibalas", "lewat", atau "reset" (daftar harus diambil ulang)."""
    conv_id, nama, preview = item["id"], item["nama"], item["preview"]
    prev_lower = preview.lower()

    if any(p in prev_lower for p in PREVIEW_DILEWATI):
        log(f"-> {nama}: '{preview[:30]}' -> [LEWATI: Stiker/Gambar]")
        data["dilewati"][conv_id] = nama
        return "lewat"

    is_shared_video = ("shared a video" in prev_lower or "shared a post" in prev_lower) and BALAS_SHARED_VIDEO
    is_story = "replied to your story" in prev_lower

    m = _POLA_KEYWORD.search(preview)
    alasan = None
    if is_shared_video:
        alasan = "Shared video VT"
    elif m:
        alasan = f"Kata kunci '{m.group(0)}'"
    elif is_story:
        alasan = "Replied to story"

    # Jika preview tidak cocok dan bukan video/story, lewati cepat
    if not alasan:
        data["dilewati"][conv_id] = nama
        log(f"-> {nama}: '{preview[:40]}' -> [LEWATI: Tidak ada kata kunci]")
        return "lewat"

    log(f"-> {nama}: '{preview[:45]}' [{alasan}]")
    if not buka_chat(page, conv_id, nama):
        # Item tidak ada di layar (daftar berubah) -> minta daftar diambil ulang.
        log("   [INFO] Daftar chat berubah, memuat ulang daftar...")
        return "reset"

    pesan = ambil_isi_pesan(page)
    gabungan = " ".join(pesan).lower()

    if any(p in gabungan for p in PENANDA_SUDAH_DIBALAS):
        log("   [SKIP] Sudah pernah dibalas sebelumnya.")
        data["dibalas"][conv_id] = nama
        return "lewat"

    if page.locator(SEL_STRANGER_BOX).count() == 0:
        log("   [SKIP] Tidak ada tombol Accept (bukan request baru).")
        data["dilewati"][conv_id] = nama
        return "lewat"

    m_isi = _POLA_KEYWORD.search(gabungan) or m
    if not is_shared_video and not m_isi:
        log("   [LEWATI: Isi chat tidak meminta source code]")
        data["dilewati"][conv_id] = nama
        return "lewat"

    label_cocok = alasan if is_shared_video else f"'{m_isi.group(0)}'"
    if DRY_RUN:
        log(f"   [DRY RUN] COCOK: {label_cocok} -> akan di-Accept & dibalas (tidak dilakukan).")
        return "lewat"

    log(f"   [COCOK: {label_cocok}] Klik Accept...")
    if not klik_accept(page):
        return "reset"

    hasil_kirim = kirim_balasan(page)
    if hasil_kirim == "rate_limited":
        return "rate_limited"
    if hasil_kirim:
        log("   [BERHASIL] Accept + balasan terkirim.")
        data["dibalas"][conv_id] = nama
        data["tertunda"].pop(conv_id, None)
        return "dibalas"

    # Sudah di-Accept tapi balasan gagal -> chat sudah pindah ke inbox utama.
    # Dicatat supaya dibalas oleh pulihkan_yang_tertunda() di putaran berikutnya.
    log("   [TERTUNDA] Sudah di-Accept tapi balasan gagal, akan dicoba lagi dari inbox utama.")
    data["tertunda"][conv_id] = nama
    return "reset"


def chat_hasil_accept(page):
    """True kalau chat yang terbuka berisi tanda 'Message request accepted'."""
    teks = " ".join(page.locator('[data-e2e="chat-item"], [data-e2e="dm-new-chatbox"]').all_inner_texts()).lower()
    return "request accepted" in teks or "permintaan pesan diterima" in teks


def pulihkan_yang_tertunda(page, data):
    """
    Cari chat di INBOX UTAMA yang sudah di-Accept tapi belum dibalas
    (misal balasan gagal di putaran sebelumnya), lalu balas.
    Hanya chat yang: preview-nya mengandung kata kunci, belum ada balasan kita,
    dan berisi tanda 'Message request accepted'. Chat teman biasa tidak disentuh.
    """
    # Hanya lakukan pemulihan jika memang ada chat yang berstatus tertunda
    if not data.get("tertunda"):
        return 0

    if page.locator(SEL_LIST).count() == 0:
        return 0
    dibalas = 0
    for it in ambil_daftar_item(page):
        cid, nama, preview = it["id"], it["nama"], it["preview"]
        if not cid or cid in data["dibalas"] or cid in data["bukan_request"]:
            continue
        pl = preview.lower()
        if any(p in pl for p in PENANDA_SUDAH_DIBALAS):
            data["dibalas"][cid] = nama
            continue
        if cid not in data["tertunda"] and not _POLA_KEYWORD.search(preview) \
                and "request accepted" not in pl:
            continue

        log(f"-> [PEMULIHAN] {nama}: '{preview[:50]}'")
        if not buka_chat(page, cid, nama):
            continue
        gabungan = " ".join(ambil_isi_pesan(page)).lower()
        if any(p in gabungan for p in PENANDA_SUDAH_DIBALAS):
            log("   [SKIP] Ternyata sudah dibalas.")
            data["dibalas"][cid] = nama
            data["tertunda"].pop(cid, None)
            continue
        if page.locator(SEL_STRANGER_BOX).count() > 0 or not chat_hasil_accept(page):
            data["bukan_request"][cid] = nama   # chat biasa, jangan dicek lagi
            continue
        if DRY_RUN:
            log("   [DRY RUN] akan dibalas (tidak dilakukan).")
            continue
        hasil_kirim = kirim_balasan(page)
        if hasil_kirim == "rate_limited":
            return -1
        if hasil_kirim:
            log("   [BERHASIL] Balasan susulan terkirim.")
            data["dibalas"][cid] = nama
            data["tertunda"].pop(cid, None)
            dibalas += 1
            jeda = random.uniform(JEDA_MIN_DETIK, JEDA_MAKS_DETIK)
            log(f"   [JEDA] Istirahat {jeda:.1f} detik sebelum chat berikutnya...")
            time.sleep(jeda)
        else:
            data["tertunda"][cid] = nama
        simpan_data(data)
    simpan_data(data)
    return dibalas


def satu_putaran(page, data):
    """Satu kali sapu seluruh daftar Message requests. Return jumlah yang dibalas (-1 jika kena rate limit)."""
    # 1) balas dulu chat yang kemarin sudah di-Accept tapi balasannya gagal
    dibalas = pulihkan_yang_tertunda(page, data)
    if dibalas == -1:
        return -1

    # 2) sapu Message requests - pastikan BENAR-BENAR berada di sub-menu Message requests
    if not masuk_ke_message_requests(page):
        log("Tidak berada di Message requests. Melewati pemindaian putaran ini.")
        return dibalas

    scroll_daftar_ke_atas(page)
    sudah_dilihat = set()
    scroll_kosong = 0
    reset_beruntun = 0

    while scroll_kosong < MAKS_SCROLL_TANPA_BARU:
        # PENGAMAN MUTLAK: Jangan pernah scan atau scroll jika posisi tidak di dalam Message requests!
        if not sedang_di_message_requests(page):
            log("   [NAVIGASI] Posisi terlempar keluar dari Message requests. Masuk kembali...")
            if not masuk_ke_message_requests(page):
                log("   [NAVIGASI] Gagal masuk ke Message requests, hentikan putaran ini.")
                break

        items = ambil_daftar_item(page)
        baru = [it for it in items if it["id"] and it["id"] not in sudah_dilihat]

        if not baru:
            scroll_kosong += 1
            scroll_daftar(page)
            continue
        scroll_kosong = 0

        for it in baru:
            sudah_dilihat.add(it["id"])
            if it["id"] in data["dibalas"] or it["id"] in data["dilewati"] \
                    or it["id"] in data["tertunda"]:
                continue
            try:
                hasil = proses_satu_item(page, it, data)
            except Exception as e:  # satu chat error jangan bikin bot mati
                log(f"   [ERROR] {type(e).__name__}: {str(e)[:120]}")
                hasil = "reset"
            simpan_data(data)

            if hasil == "rate_limited":
                log("\n" + "=" * 60)
                log("[RATE LIMIT TERDETEKSI] TikTok membatasi pengiriman pesan:")
                log("   'You are sending messages too fast. Take a rest.'")
                log("Bot otomatis DIHENTIKAN agar akun tetap aman dan tidak terkena sanksi.")
                log("Silakan istirahatkan akun beberapa jam atau jalankan kembali besok.")
                log("=" * 60 + "\n")
                return -1

            if hasil == "dibalas":
                dibalas += 1
                jeda = random.uniform(JEDA_MIN_DETIK, JEDA_MAKS_DETIK)
                log(f"   [JEDA] Istirahat {jeda:.1f} detik sebelum chat berikutnya...")
                time.sleep(jeda)

            if hasil in ("dibalas", "reset"):
                # Setelah Accept, TikTok memindahkan panel kiri ke inbox utama.
                # JANGAN lanjut pakai daftar lama (itu penyebab error beruntun) ->
                # masuk lagi ke Message requests & ambil ulang daftar dari DOM.
                reset_beruntun = reset_beruntun + 1 if hasil == "reset" else 0
                if reset_beruntun >= 5:
                    log("   [INFO] Terlalu banyak error beruntun, muat ulang halaman...")
                    page.reload()
                    page.wait_for_selector(SEL_LIST, timeout=60000)
                    time.sleep(2)
                    reset_beruntun = 0
                masuk_ke_message_requests(page)
                break
            reset_beruntun = 0
        else:
            scroll_daftar(page)

    return dibalas


def jalankan_bot():
    data = muat_data()
    log(f"Data tersimpan: {len(data['dibalas'])} sudah dibalas, "
        f"{len(data['dilewati'])} dilewati.")

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=FILE_SESI,
            headless=False,
            viewport={"width": 1280, "height": 800},
        )
        browser.grant_permissions(["clipboard-read", "clipboard-write"],
                                  origin="https://www.tiktok.com")
        page = browser.pages[0]

        log("Membuka TikTok Messages...")
        page.goto("https://www.tiktok.com/messages")
        try:
            page.wait_for_selector(SEL_LIST, timeout=60000)
        except PWTimeout:
            log("Halaman Messages tidak muncul. Pastikan sudah login di jendela browser.")
            browser.close()
            return
        time.sleep(2)

        try:
            putaran = 0
            while True:
                putaran += 1
                log(f"===== Putaran ke-{putaran} =====")
                n = satu_putaran(page, data)
                if n == -1:
                    log("Bot dihentikan karena akun terkena batas rate limit TikTok.")
                    break
                if DRY_RUN:
                    log("Dry run selesai.")
                    break
                log(f"Putaran selesai, {n} chat dibalas. "
                    f"Cek lagi dalam {JEDA_CEK_ULANG} detik (Ctrl+C untuk berhenti).")
                time.sleep(JEDA_CEK_ULANG)
                page.reload()
                page.wait_for_selector(SEL_LIST, timeout=60000)
                time.sleep(2)
        except KeyboardInterrupt:
            log("Dihentikan oleh user.")
        finally:
            simpan_data(data)
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    jalankan_bot()
