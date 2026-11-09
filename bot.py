import html, ipaddress, json, os, queue, re, shutil, subprocess, sys, threading, time
from urllib.parse import urlparse

import requests

TOKEN = os.environ["BOT_TOKEN"]
ALLOWED = {x.strip() for x in os.environ.get("CHAT_ID", "").split(",") if x.strip()}
END = time.time() + int(os.environ.get("BOT_MINUTES", "330")) * 60
MAX_REC = int(os.environ.get("REC_MAX_SECONDS", "240") or 240)  # pengaman, bukan durasi tetap
API = f"https://api.telegram.org/bot{TOKEN}"

# d = perangkat, a = suara, t = tap otomatis, p = kecepatan (f/n/s)
DEFAULT = {"d": "hp", "a": 1, "t": 1, "p": "f"}
DEV_NAME = {"pc": "PC", "hp": "HP Android", "iphone": "iPhone", "ipad": "iPad"}
DEVICES = [("hp", "📱 HP"), ("iphone", "🍎 iPhone"), ("ipad", "📲 iPad"), ("pc", "💻 PC")]
PACE_NAME = {"f": "Cepat", "n": "Normal", "s": "Pelan"}
PACES = [("f", "🐇 Cepat"), ("n", "🚶 Normal"), ("s", "🐢 Pelan")]

jobs = queue.Queue()
state = {"current": None, "last": "belum ada rekaman", "proc": None, "cancel": False}
RULES = {}  # per chat; hilang kalau bot restart
esc = html.escape


def tg(method, http_timeout=30, **params):
    params = {k: v for k, v in params.items() if v is not None}
    try:
        return requests.post(f"{API}/{method}", json=params, timeout=http_timeout).json()
    except Exception as e:
        print("tg error:", method, type(e).__name__, flush=True)
        return {"ok": False}


def enc(act, st):
    return f"{act}|{st['d']}|{st['a']}|{st['t']}|{st['p']}"


def dec(data):
    act, d, a, t, p = ((data or "").split("|") + [""] * 5)[:5]
    return act, {
        "d": d if d in DEV_NAME else DEFAULT["d"],
        "a": 0 if a == "0" else 1,
        "t": 0 if t == "0" else 1,
        "p": p if p in PACE_NAME else DEFAULT["p"],
    }


def rules_of(chat):
    return RULES.setdefault(str(chat), {"klik": [], "hindari": [], "selesai": ""})


def rules_lines(chat):
    r, out = rules_of(chat), []
    if r["klik"]:
        out.append("Tekan duluan: " + ", ".join(r["klik"]))
    if r["hindari"]:
        out.append("Jangan ditekan: " + ", ".join(r["hindari"]))
    if r["selesai"]:
        out.append("Berhenti saat muncul: " + r["selesai"])
    return out


def find_url(text):
    m = re.search(r"https?://\S+", text or "", re.I)
    if not m:
        return None
    raw = m.group(0).rstrip("),.;")
    try:
        u = urlparse(raw)
        h = (u.hostname or "").lower()
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not h or h == "localhost" or h.endswith((".local", ".internal")):
        return None
    try:
        ip = ipaddress.ip_address(h)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return None
    except ValueError:
        pass
    return raw


def card(head, url, st, chat=None, foot=""):
    rows = (
        f"Perangkat  {DEV_NAME[st['d']]}\n"
        f"Suara      {'Aktif' if st['a'] else 'Mati'}\n"
        f"Tap        {'Otomatis' if st['t'] else 'Scroll saja'}\n"
        f"Kecepatan  {PACE_NAME[st['p']]}\n"
        f"Berhenti   Otomatis"
    )
    text = f"{head}\n🔗 {esc(url) if url else 'belum ada, kirim link web'}\n\n<pre>{rows}</pre>"
    extra = rules_lines(chat) if chat is not None else []
    if extra:
        text += "\n<blockquote>" + "\n".join(esc(x) for x in extra) + "</blockquote>"
    return text + (f"\n{foot}" if foot else "")


def keyboard(st):
    def b(label, act, **ch):
        on = all(st[k] == v for k, v in ch.items()) if ch else False
        return {"text": ("✅ " if on else "") + label, "callback_data": enc(act, {**st, **ch})}

    return {"inline_keyboard": [
        [b(l, "set", d=k) for k, l in DEVICES],
        [b("🔊 Suara", "set", a=1), b("🔇 Senyap", "set", a=0)],
        [b("👆 Auto-tap", "set", t=1), b("📜 Scroll saja", "set", t=0)],
        [b(l, "set", p=k) for k, l in PACES],
        [{"text": "▶️  MULAI REKAM", "callback_data": enc("go", st)}],
        [{"text": "📊 Status", "callback_data": enc("st", st)},
         {"text": "🔗 Ganti link", "callback_data": enc("lk", st)},
         {"text": "♻️ Reset", "callback_data": enc("set", DEFAULT)}],
    ]}


def after_markup(st, busy=False):
    row = [{"text": "⏹ Batal", "callback_data": enc("cx", st)}] if busy else [
        {"text": "🔁 Rekam lagi", "callback_data": enc("set", st)}]
    return {"inline_keyboard": [[{"text": "📊 Status", "callback_data": enc("st", st)}] + row]}


def edit(chat, mid, text, kb=None):
    return tg("editMessageText", chat_id=chat, message_id=mid, text=text, parse_mode="HTML",
              reply_markup=kb, link_preview_options={"is_disabled": True})


def send_panel(chat, url, st):
    return tg("sendMessage", chat_id=chat, text=card("🎬 <b>Perekam Web</b>", url, st, chat),
              parse_mode="HTML", reply_markup=keyboard(st), link_preview_options={"is_disabled": True})


def status_text():
    cur = state["current"]
    lines = [f"Sedang merekam: {DEV_NAME[cur['st']['d']]}" if cur else "Menganggur ✅"]
    if jobs.qsize():
        lines.append(f"Antre: {jobs.qsize()}")
    lines += [f"Terakhir: {state['last']}", f"Bot aktif sekitar {max(0, int((END - time.time()) / 60))} menit lagi"]
    return "\n".join(lines)


def cancel_all():
    state["cancel"] = True
    while not jobs.empty():
        try:
            jobs.get_nowait()
        except queue.Empty:
            break
    p = state.get("proc")
    if p and p.poll() is None:
        p.terminate()


def run_job(job):
    url, st, chat, mid = job["url"], job["st"], job["chat"], job["mid"]
    state["cancel"] = False
    shutil.rmtree("out", ignore_errors=True)
    env = {k: v for k, v in os.environ.items() if k != "BOT_TOKEN"}
    env.update(REC_URL=url, REC_DEVICE=st["d"], REC_AUDIO=str(st["a"]), REC_TAP=str(st["t"]),
               REC_PACE=st["p"], REC_MAX_SECONDS=str(MAX_REC), REC_RULES=json.dumps(rules_of(chat)))
    edit(chat, mid, card("🔴 <b>Merekam…</b>", url, st, chat, "Berhenti sendiri saat web selesai."),
         after_markup(st, busy=True))
    proc = subprocess.Popen([sys.executable, "record.py"], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    state["proc"] = proc
    try:
        out, err = proc.communicate(timeout=MAX_REC + 240)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("terlalu lama, dihentikan")
    if state["cancel"]:
        raise RuntimeError("dibatalkan")
    mp4 = os.path.join("out", "rec.mp4")
    if proc.returncode != 0 or not os.path.exists(mp4):
        raise RuntimeError(((err or "") + (out or "")).strip()[-300:] or "rekaman gagal")

    def field(name):
        m = re.search(rf"\| {name}: ([^|]*)", out)
        return m.group(1).strip() if m else "-"

    suara = {"ok": "🔊 ada suara", "hening": "🔇 hening (web tidak bersuara)", "off": "🔇 tanpa suara",
             "tidak-ada": "🔇 audio gagal ditangkap"}.get(field("suara"), "-")
    wh = field("ukuran")
    w, _, h = wh.partition("x")
    dur = field("durasi")
    note = ""
    m = re.search(r"\| error: (.*)$", out, re.M)
    if m and m.group(1).strip() != "None":
        note = f"\n⚠️ Ada masalah: {m.group(1).strip()[:150]}"
    edit(chat, mid, card("📤 <b>Mengirim video…</b>", url, st, chat), after_markup(st, busy=True))
    data = {"chat_id": chat, "supports_streaming": "true",
            "caption": f"{url}\n{DEV_NAME[st['d']]} · {wh} · {dur} dtk · {suara}\nBerhenti: {field('alasan')}{note}"[:1000]}
    if w.isdigit() and h.isdigit():
        data.update(width=w, height=h)
    if dur.isdigit():
        data["duration"] = dur
    with open(mp4, "rb") as f:
        r = requests.post(f"{API}/sendVideo", data=data, files={"video": f}, timeout=300).json()
    if not r.get("ok"):
        raise RuntimeError("gagal kirim video: " + str(r.get("description")))
    state["last"] = "selesai ✅"
    edit(chat, mid, card(f"✅ <b>Selesai</b> · {dur} dtk", url, st, chat), after_markup(st))


def worker():
    while True:
        job = jobs.get()
        state["current"] = job
        try:
            run_job(job)
        except Exception as e:
            msg = str(e)[:300]
            print("job error:", msg, flush=True)
            cancelled = msg == "dibatalkan"
            state["last"] = "dibatalkan ⏹" if cancelled else "gagal ❌"
            head = "⏹ <b>Dibatalkan</b>" if cancelled else "❌ <b>Gagal merekam</b>"
            edit(job["chat"], job["mid"],
                 card(head, job["url"], job["st"], job["chat"], "" if cancelled else f"<code>{esc(msg)}</code>"),
                 after_markup(job["st"]))
        finally:
            state["current"], state["proc"] = None, None


def words(s):
    return [x.strip() for x in re.split(r"[,\n]", s) if x.strip()][:12]


def on_rule_command(chat, text):
    m = re.match(r"^/(klik|hindari|selesai|aturan|hapusaturan|batal)(?:@\w+)?\s*(.*)$", text, re.S)
    if not m:
        return None
    cmd, arg = m.group(1), m.group(2).strip()
    say = lambda t: tg("sendMessage", chat_id=chat, text=t)
    if cmd == "batal":
        cancel_all()
        return say("⏹ Rekaman dihentikan dan antrean dikosongkan.")
    if cmd == "hapusaturan":
        RULES[str(chat)] = {"klik": [], "hindari": [], "selesai": ""}
        return say("Aturan dihapus ✅")
    if cmd == "aturan" or not arg:
        return say("\n".join((rules_lines(chat) or ["Belum ada aturan. Bot menekan semua tombol yang terdeteksi."]) + [
            "", "/klik a, b → tekan ini duluan (sesuai urutan)", "/hindari a, b → jangan ditekan",
            "/selesai teks → berhenti saat teks ini muncul", "/hapusaturan → kosongkan",
            "/batal → hentikan rekaman & kosongkan antrean"]))
    r = rules_of(chat)
    if cmd == "klik":
        r["klik"] = words(arg)
    elif cmd == "hindari":
        r["hindari"] = words(arg)
    else:
        r["selesai"] = arg[:60]
    return say("Tersimpan ✅\n\n" + "\n".join(rules_lines(chat)))


def on_message(m):
    chat, uid, text = m["chat"]["id"], m["from"]["id"], m.get("text") or ""
    if re.match(r"^/id\b", text):
        return tg("sendMessage", chat_id=chat, text=f"ID kamu: {uid}")
    if str(uid) not in ALLOWED:
        return tg("sendMessage", chat_id=chat, text=f"Akses ditolak. ID kamu: {uid}\nIsi secret CHAT_ID dengan ID ini.")
    if text.startswith("/"):
        done = on_rule_command(chat, text)
        if done is not None:
            return done
    url = find_url(text)
    if url:
        reply = (m.get("reply_to_message") or {}).get("text") or ""
        hint = re.search(r"\((pc|hp|iphone|ipad)\|([01])\|([01])\|([fns])\)", reply)
        return send_panel(chat, url, dec("x|" + "|".join(hint.groups()))[1] if hint else dict(DEFAULT))
    if re.match(r"^/(start|panel|menu)\b", text):
        return send_panel(chat, None, dict(DEFAULT))
    return tg("sendMessage", chat_id=chat, text="Kirim link web (https://...) atau ketik /panel. /aturan untuk atur tombol yang ditekan.")


def on_callback(q):
    msg = q.get("message")
    if not msg:
        return
    chat, mid = msg["chat"]["id"], msg["message_id"]

    def answer(text=None, alert=False):
        tg("answerCallbackQuery", callback_query_id=q["id"], text=(text or "")[:190] or None, show_alert=alert)

    if str(q["from"]["id"]) not in ALLOWED:
        return answer("Akses ditolak", True)
    act, st = dec(q.get("data"))
    url = find_url(msg.get("text"))

    if act == "set":
        answer()
        return edit(chat, mid, card("🎬 <b>Perekam Web</b>", url, st, chat), keyboard(st))
    if act == "lk":
        answer()
        return tg("sendMessage", chat_id=chat,
                  text=f"Balas pesan ini dengan link web.\n({st['d']}|{st['a']}|{st['t']}|{st['p']})",
                  reply_markup={"force_reply": True, "input_field_placeholder": "https://..."})
    if act == "st":
        return answer(status_text(), True)
    if act == "cx":
        cancel_all()
        return answer("Dihentikan, antrean dikosongkan")
    if act == "go":
        if not url:
            return answer("Kirim link web dulu", True)
        if any(j["mid"] == mid for j in list(jobs.queue)) or (state["current"] and state["current"]["mid"] == mid):
            return answer("Sudah masuk antrean")
        if jobs.qsize() >= 3:
            return answer("Antrean penuh, coba lagi nanti", True)
        busy = state["current"] is not None or not jobs.empty()
        jobs.put({"url": url, "st": st, "chat": chat, "mid": mid})
        answer("Masuk antrean" if busy else "Rekaman dimulai")
        if busy:
            return edit(chat, mid, card("⏳ <b>Dalam antrean…</b>", url, st, chat), after_markup(st, busy=True))
    return answer()


def main():
    tg("deleteWebhook")
    threading.Thread(target=worker, daemon=True).start()
    first = next(iter(sorted(ALLOWED)), None)
    if first:
        tg("sendMessage", chat_id=first, text="🟢 Bot aktif sekitar 5 jam. Ketik /panel untuk mulai.")
    print("bot jalan", flush=True)
    offset = None
    while time.time() < END:
        r = tg("getUpdates", http_timeout=45, offset=offset, timeout=25, allowed_updates=["message", "callback_query"])
        if not r.get("ok"):
            time.sleep(5)
            continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            try:
                if "message" in u:
                    on_message(u["message"])
                elif "callback_query" in u:
                    on_callback(u["callback_query"])
            except Exception as e:
                print("handle error:", type(e).__name__, str(e)[:200], flush=True)
    grace = time.time() + 900
    while (state["current"] or not jobs.empty()) and time.time() < grace:
        time.sleep(5)
    print("selesai, menunggu restart", flush=True)


if __name__ == "__main__":
    main()