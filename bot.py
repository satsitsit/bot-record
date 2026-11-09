import ipaddress
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import requests

TOKEN = os.environ["BOT_TOKEN"]
ALLOWED = {x.strip() for x in os.environ.get("CHAT_ID", "").split(",") if x.strip()}
END = time.time() + int(os.environ.get("BOT_MINUTES", "330")) * 60
API = f"https://api.telegram.org/bot{TOKEN}"

DEFAULT = {"d": "pc", "s": 45, "t": 1}
DEV_NAME = {"pc": "PC", "iphone": "iPhone (Safari)", "ipad": "iPad (Safari)"}
DEVICES = [("pc", "💻 PC"), ("iphone", "📱 iPhone"), ("ipad", "📲 iPad")]
SECS = [30, 45, 60]

jobs = queue.Queue()
state = {"current": None, "last": "belum ada rekaman"}


def tg(method, http_timeout=30, **params):
    params = {k: v for k, v in params.items() if v is not None}
    try:
        return requests.post(f"{API}/{method}", json=params, timeout=http_timeout).json()
    except Exception as e:
        print("tg error:", method, type(e).__name__, flush=True)
        return {"ok": False}


def enc(act, st):
    return f"{act}|{st['d']}|{st['s']}|{st['t']}"


def dec(data):
    p = (data or "").split("|") + ["", "", "", ""]
    act, d, s, t = p[:4]
    st = {
        "d": d if d in DEV_NAME else DEFAULT["d"],
        "s": int(s) if s.isdigit() and int(s) in SECS else DEFAULT["s"],
        "t": 0 if t == "0" else 1,
    }
    return act, st


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
    if u.scheme not in ("http", "https") or not h:
        return None
    if h == "localhost" or h.endswith((".local", ".internal")):
        return None
    try:
        ip = ipaddress.ip_address(h)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return None
    except ValueError:
        pass
    return raw


def panel_text(url, st):
    return "\n".join(
        [
            "🎬 Panel rekam web",
            "",
            f"Link: {url or 'belum ada, kirim link web dulu'}",
            f"Perangkat: {DEV_NAME[st['d']]}",
            f"Durasi: {st['s']} detik",
            f"Tap: {'otomatis' if st['t'] else 'scroll saja'}",
        ]
    )


def keyboard(st):
    def on(c, label):
        return ("✅ " if c else "") + label

    return {
        "inline_keyboard": [
            [
                {"text": on(st["d"] == k, l), "callback_data": enc("set", {**st, "d": k})}
                for k, l in DEVICES
            ],
            [
                {"text": on(st["s"] == n, f"⏱ {n} dtk"), "callback_data": enc("set", {**st, "s": n})}
                for n in SECS
            ],
            [
                {"text": on(st["t"] == 1, "👆 Tap otomatis"), "callback_data": enc("set", {**st, "t": 1})},
                {"text": on(st["t"] == 0, "📜 Scroll saja"), "callback_data": enc("set", {**st, "t": 0})},
            ],
            [{"text": "▶️ Mulai rekam", "callback_data": enc("go", st)}],
            [
                {"text": "📊 Status", "callback_data": enc("st", st)},
                {"text": "🔗 Ganti link", "callback_data": enc("lk", st)},
                {"text": "♻️ Reset", "callback_data": enc("set", DEFAULT)},
            ],
        ]
    }


def after_markup(st):
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Status", "callback_data": enc("st", st)},
                {"text": "🔁 Rekam lagi", "callback_data": enc("set", st)},
            ]
        ]
    }


def send_panel(chat, url, st):
    return tg(
        "sendMessage",
        chat_id=chat,
        text=panel_text(url, st),
        reply_markup=keyboard(st),
        link_preview_options={"is_disabled": True},
    )


def edit(chat, mid, text, kb=None):
    return tg(
        "editMessageText",
        chat_id=chat,
        message_id=mid,
        text=text,
        reply_markup=kb,
        link_preview_options={"is_disabled": True},
    )


def status_text():
    left = max(0, int((END - time.time()) / 60))
    cur = state["current"]
    lines = [f"Sedang merekam: {DEV_NAME[cur['st']['d']]}" if cur else "Menganggur ✅"]
    if jobs.qsize():
        lines.append(f"Antre: {jobs.qsize()}")
    lines.append(f"Terakhir: {state['last']}")
    lines.append(f"Bot aktif sekitar {left} menit lagi")
    return "\n".join(lines)


def run_job(job):
    url, st, chat, mid = job["url"], job["st"], job["chat"], job["mid"]
    shutil.rmtree("out", ignore_errors=True)
    env = {k: v for k, v in os.environ.items() if k != "BOT_TOKEN"}
    env.update(
        REC_URL=url,
        REC_DEVICE=st["d"],
        REC_SECONDS=str(st["s"]),
        REC_TAP=str(st["t"]),
    )
    p = subprocess.run(
        [sys.executable, "record.py"],
        env=env,
        capture_output=True,
        text=True,
        timeout=st["s"] + 180,
    )
    mp4 = os.path.join("out", "rec.mp4")
    if p.returncode != 0 or not os.path.exists(mp4):
        tail = ((p.stderr or "") + (p.stdout or "")).strip()[-300:]
        raise RuntimeError(tail or "rekaman gagal")
    note = ""
    m = re.search(r"\| error: (.*)$", p.stdout or "", re.M)
    if m and m.group(1).strip() != "None":
        note = f"\n⚠️ Ada masalah saat membuka halaman: {m.group(1).strip()[:150]}"
    caption = f"Rekaman: {url}\n{DEV_NAME[st['d']]}, {st['s']} dtk{note}"[:1000]
    with open(mp4, "rb") as f:
        r = requests.post(
            f"{API}/sendVideo",
            data={"chat_id": chat, "caption": caption, "supports_streaming": "true"},
            files={"video": f},
            timeout=240,
        ).json()
    if not r.get("ok"):
        raise RuntimeError("gagal kirim video: " + str(r.get("description")))
    state["last"] = "selesai ✅"
    edit(
        chat,
        mid,
        f"✅ Selesai\n\nLink: {url}\nPerangkat: {DEV_NAME[st['d']]}",
        after_markup(st),
    )


def worker():
    while True:
        job = jobs.get()
        state["current"] = job
        try:
            run_job(job)
        except Exception as e:
            msg = str(e)[:300]
            print("job error:", msg, flush=True)
            state["last"] = "gagal ❌"
            edit(
                job["chat"],
                job["mid"],
                f"❌ Gagal merekam\n\nLink: {job['url']}\n{msg}",
                after_markup(job["st"]),
            )
        finally:
            state["current"] = None


def on_message(m):
    chat = m["chat"]["id"]
    uid = m["from"]["id"]
    text = m.get("text") or ""
    if re.match(r"^/id\b", text):
        return tg("sendMessage", chat_id=chat, text=f"ID kamu: {uid}")
    if str(uid) not in ALLOWED:
        return tg(
            "sendMessage",
            chat_id=chat,
            text=f"Akses ditolak. ID kamu: {uid}\nIsi secret CHAT_ID dengan ID ini.",
        )
    url = find_url(text)
    if url:
        reply = (m.get("reply_to_message") or {}).get("text") or ""
        hint = re.search(r"\((pc|iphone|ipad)\|(\d+)\|([01])\)", reply)
        st = dec("x|" + "|".join(hint.groups()))[1] if hint else dict(DEFAULT)
        return send_panel(chat, url, st)
    if re.match(r"^/(start|panel|menu)\b", text):
        return send_panel(chat, None, dict(DEFAULT))
    return tg("sendMessage", chat_id=chat, text="Kirim link web (https://...) atau ketik /panel.")


def on_callback(q):
    msg = q.get("message")
    if not msg:
        return
    chat = msg["chat"]["id"]
    mid = msg["message_id"]

    def answer(text=None, alert=False):
        tg("answerCallbackQuery", callback_query_id=q["id"], text=(text or "")[:190] or None, show_alert=alert)

    if str(q["from"]["id"]) not in ALLOWED:
        return answer("Akses ditolak", True)
    act, st = dec(q.get("data"))
    url = find_url(msg.get("text"))

    if act == "set":
        answer()
        return edit(chat, mid, panel_text(url, st), keyboard(st))
    if act == "lk":
        answer()
        return tg(
            "sendMessage",
            chat_id=chat,
            text=f"Balas pesan ini dengan link web.\n({st['d']}|{st['s']}|{st['t']})",
            reply_markup={"force_reply": True, "input_field_placeholder": "https://..."},
        )
    if act == "st":
        return answer(status_text(), True)
    if act == "go":
        if not url:
            return answer("Kirim link web dulu", True)
        if jobs.qsize() >= 3:
            return answer("Antrean penuh, coba lagi nanti", True)
        busy = state["current"] is not None or not jobs.empty()
        jobs.put({"url": url, "st": st, "chat": chat, "mid": mid})
        answer("Masuk antrean" if busy else "Rekaman dimulai")
        head = "⏳ Antre..." if busy else "⏳ Merekam..."
        return edit(
            chat,
            mid,
            f"{head}\n\nLink: {url}\nPerangkat: {DEV_NAME[st['d']]}\nVideo dikirim ke chat ini, sekitar 1-2 menit.",
            after_markup(st),
        )
    return answer()


def handle(u):
    if "message" in u:
        on_message(u["message"])
    elif "callback_query" in u:
        on_callback(u["callback_query"])


def main():
    tg("deleteWebhook")
    threading.Thread(target=worker, daemon=True).start()
    first = next(iter(sorted(ALLOWED)), None)
    if first:
        tg("sendMessage", chat_id=first, text="🟢 Bot aktif sekitar 5 jam. Ketik /panel untuk mulai.")
    print("bot jalan", flush=True)
    offset = None
    while time.time() < END:
        r = tg(
            "getUpdates",
            http_timeout=45,
            offset=offset,
            timeout=25,
            allowed_updates=["message", "callback_query"],
        )
        if not r.get("ok"):
            time.sleep(5)
            continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            try:
                handle(u)
            except Exception as e:
                print("handle error:", type(e).__name__, str(e)[:200], flush=True)
    grace = time.time() + 600
    while (state["current"] or not jobs.empty()) and time.time() < grace:
        time.sleep(5)
    print("selesai, menunggu restart", flush=True)


if __name__ == "__main__":
    main()
