import json, os, re, signal, subprocess, sys, time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

signal.signal(signal.SIGTERM, lambda *a: sys.exit(1))  # supaya /batal bersih-bersih

URL = os.environ["REC_URL"]
DEVICE = os.environ.get("REC_DEVICE", "hp").lower()
AUDIO = os.environ.get("REC_AUDIO", "1") != "0"
TAP = os.environ.get("REC_TAP", "1") != "0"
PACE = {"f": 0.5, "n": 1.0, "s": 1.6}.get(os.environ.get("REC_PACE", "f"), 0.5)
HARD_CAP = int(os.environ.get("REC_MAX_SECONDS", "240") or 240)  # pengaman
MAX_CLICKS, OUT, DISP = 60, "out", ":99"

try:
    RULES = json.loads(os.environ.get("REC_RULES") or "{}")
except ValueError:
    RULES = {}
CLICK_FIRST = [x.strip() for x in RULES.get("klik", []) if x.strip()]
AVOID = [x.strip().lower() for x in RULES.get("hindari", []) if x.strip()]
END_TEXT = (RULES.get("selesai") or "").strip()

UA = {
    "hp": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "iphone": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "ipad": "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
}
# w,h = ukuran tampilan (CSS px, genap); dpr = ketajaman (2 = HD asli, bukan diperbesar)
PROFILES = {
    "pc": dict(w=1920, h=1080, dpr=1, touch=False, ua=None),
    "hp": dict(w=360, h=800, dpr=2, touch=True, ua=UA["hp"]),
    "iphone": dict(w=390, h=844, dpr=2, touch=True, ua=UA["iphone"]),
    "ipad": dict(w=820, h=1180, dpr=1, touch=True, ua=UA["ipad"]),
}
P = PROFILES.get(DEVICE, PROFILES["hp"])
TOUCH, DPR = P["touch"], P["dpr"]
LOAD = {"t": 0.0}

OVERLAY = """
(() => { const boot = () => {
  if (document.getElementById('__cur')) return;
  const c = document.createElement('div'); c.id = '__cur';
  c.style.cssText = 'position:fixed;left:0;top:0;width:22px;height:22px;border-radius:50%;background:rgba(255,60,60,.55);border:2px solid #fff;box-shadow:0 0 8px rgba(0,0,0,.5);z-index:2147483647;pointer-events:none;transform:translate(-50%,-50%);display:none';
  document.documentElement.appendChild(c);
  const move = (e) => { c.style.display = 'block'; c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; };
  const ripple = (x, y) => {
    const r = document.createElement('div');
    r.style.cssText = 'position:fixed;width:56px;height:56px;border-radius:50%;border:3px solid rgba(255,60,60,.9);background:rgba(255,60,60,.25);z-index:2147483647;pointer-events:none;left:' + x + 'px;top:' + y + 'px;';
    document.documentElement.appendChild(r);
    r.animate([{transform:'translate(-50%,-50%) scale(.3)',opacity:1},{transform:'translate(-50%,-50%) scale(1.8)',opacity:0}], {duration:550}).onfinish = () => r.remove();
  };
  addEventListener('mousemove', move, true);
  addEventListener('mousedown', (e) => { move(e); ripple(e.clientX, e.clientY); }, true);
  addEventListener('touchstart', (e) => { const t = e.touches[0]; ripple(t.clientX, t.clientY); }, true);
};
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot(); })();
"""

# Semua yang bisa ditekan (tombol, link, DAN div/gambar ber-klik JS lewat cursor:pointer),
# hanya yang terlihat di layar dan tidak tertutup elemen lain.
CAND_JS = r"""
() => {
  const vw = innerWidth, vh = innerHeight, out = [];
  const de = document.scrollingElement || document.documentElement;
  const NAT = ['button', 'a', 'summary', 'select', 'label'];
  const nat = (el) => NAT.includes(el.tagName.toLowerCase()) || el.getAttribute('role') === 'button'
    || el.hasAttribute('onclick') || (el.tagName === 'INPUT' && ['button', 'submit'].includes(el.type));
  const cand = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 14 || r.height < 14 || r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || +cs.opacity < 0.05 || cs.pointerEvents === 'none') return false;
    return nat(el) || (cs.cursor === 'pointer' && r.width * r.height < 0.7 * vw * vh);
  };
  for (const el of document.body.querySelectorAll('*')) {
    if (!cand(el)) continue;
    const p = el.parentElement;
    if (p && p !== document.body && cand(p) && !nat(el)) continue;
    const r = el.getBoundingClientRect();
    const x = Math.min(Math.max(r.left + r.width / 2, 1), vw - 1), y = Math.min(Math.max(r.top + r.height / 2, 1), vh - 1);
    const top = document.elementFromPoint(x, y);
    if (!top || !(el === top || el.contains(top))) continue;
    out.push({
      x, y, tag: el.tagName.toLowerCase(), href: el.getAttribute('href') || '',
      text: ((el.innerText || el.value || el.getAttribute('aria-label') || el.title || el.alt || '') + '').replace(/\s+/g, ' ').trim().slice(0, 60),
      aria: (el.getAttribute('aria-label') || el.title || '') + '',
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 40),
      yd: Math.round(y + de.scrollTop),
    });
  }
  return out.slice(0, 80);
}
"""

SCROLL_JS = r"""
(go) => {
  const de = document.scrollingElement || document.documentElement;
  let sc = de;
  if (de.scrollHeight <= de.clientHeight + 4) {
    let best = null, area = 0;
    for (const el of document.body.querySelectorAll('*')) {
      if (!/(auto|scroll)/.test(getComputedStyle(el).overflowY)) continue;
      if (el.scrollHeight - el.clientHeight > 4 && el.clientHeight * el.clientWidth > area) { best = el; area = el.clientHeight * el.clientWidth; }
    }
    if (best) sc = best;
  }
  if (go) sc.scrollBy({top: Math.round(sc.clientHeight * 0.75), behavior: 'smooth'});
  return {top: sc.scrollTop, max: sc.scrollHeight - sc.clientHeight};
}
"""

SKIP = re.compile(r"log\s?out|sign\s?out|keluar|delete|hapus|remove|bayar|pay\b|checkout|unsubscribe", re.I)
CLOSE = re.compile(r"\b(close|tutup|dismiss)\b|^[×✕✖✗xX]$", re.I)


def wait(page, ms):
    page.wait_for_timeout(int(ms * PACE))


def sh(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def blocked(c, host):
    label = (c["text"] + " " + c["href"]).lower()
    h = c["href"]
    return bool(
        SKIP.search(label) or any(a in label for a in AVOID)
        or h.startswith(("mailto:", "tel:", "javascript:"))
        or (h.startswith("http") and urlparse(h).netloc != host)
    )


def key_of(c):
    return f"{c['tag']}|{c['cls']}|{c['text'][:30]}|{int(c['x'] // 40)}|{int(c['yd'] // 40)}"


def cands(page):
    try:
        return page.evaluate(CAND_JS)
    except Exception:
        return []


def pick_target(page, seen, host):
    cs = [c for c in cands(page) if key_of(c) not in seen and not blocked(c, host)]
    for want in CLICK_FIRST:
        for c in cs:
            if want.lower() in (c["text"] + " " + c["aria"]).lower():
                return c
    cs.sort(key=lambda c: (round(c["y"] / 30), c["x"]))
    return cs[0] if cs else None


def tap_at(page, x, y):
    if TOUCH:
        page.touchscreen.tap(x, y)
    else:
        page.mouse.move(x, y, steps=15)
        wait(page, 150)
        page.mouse.click(x, y)


def text_len(page):
    try:
        return page.evaluate("document.body ? document.body.innerText.length : 0")
    except Exception:
        return 0


def end_visible(page):
    if not END_TEXT:
        return False
    try:
        return page.evaluate(
            """(t) => { t = t.toLowerCase();
              for (const el of document.body.querySelectorAll('*')) {
                if (el.children.length || !(el.innerText || '').toLowerCase().includes(t)) continue;
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.bottom > 0 && r.top < innerHeight) return true;
              } return false; }""",
            END_TEXT,
        )
    except Exception:
        return False


def close_popups(ctx, page):
    for pg in ctx.pages:
        if pg != page:
            try:
                pg.close()
            except Exception:
                pass


# ---------- layar virtual, suara, perekam ----------

def start_xvfb():
    sh(["pkill", "-f", f"Xvfb {DISP}"])
    sw, sh_ = P["w"] * DPR + 1200, P["h"] * DPR + 500
    proc = subprocess.Popen(["Xvfb", DISP, "-screen", "0", f"{sw}x{sh_}x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.environ["DISPLAY"] = DISP
    for _ in range(40):
        if os.path.exists("/tmp/.X11-unix/X99"):
            break
        time.sleep(0.2)
    return proc


def start_audio():
    os.makedirs("/tmp/xdg", mode=0o700, exist_ok=True)
    os.environ["XDG_RUNTIME_DIR"] = "/tmp/xdg"
    try:
        sh(["pulseaudio", "-D", "--exit-idle-time=-1", "--log-target=null"])
        time.sleep(1.2)
        return sh(["pactl", "load-module", "module-null-sink", "sink_name=rec"]).returncode == 0 and \
            sh(["pactl", "set-default-sink", "rec"]).returncode == 0
    except Exception:
        return False


def find_area(page):
    """Cari area halaman di layar virtual (bingkai browser tidak ikut terekam). Hasil: x, y, w, h."""
    ew, eh = P["w"] * DPR, P["h"] * DPR
    try:
        from PIL import Image, ImageChops
        page.set_content("<html><body style='margin:0;background:#f0f'></body></html>")
        time.sleep(0.6)
        shot = os.path.join(OUT, "calib.png")
        sh(["ffmpeg", "-y", "-f", "x11grab", "-i", DISP, "-frames:v", "1", shot])
        r, g, b = Image.open(shot).convert("RGB").split()
        hi, lo = (lambda v: 255 if v > 240 else 0), (lambda v: 255 if v < 16 else 0)
        box = ImageChops.darker(ImageChops.darker(r.point(hi), b.point(hi)), g.point(lo)).getbbox()
        if box and box[2] - box[0] > 100 and box[3] - box[1] > 100:
            return box[0] - box[0] % 2, box[1] - box[1] % 2, (box[2] - box[0]) // 2 * 2, (box[3] - box[1]) // 2 * 2
    except Exception as e:
        print("kalibrasi gagal:", type(e).__name__, flush=True)
    return 0, 0, ew, eh


def start_recorder(raw, area, with_audio):
    x, y, w, h = area
    cmd = ["ffmpeg", "-y", "-thread_queue_size", "1024", "-f", "x11grab", "-draw_mouse", "0",
           "-framerate", "30", "-video_size", f"{w}x{h}", "-i", f"{DISP}.0+{x},{y}"]
    if with_audio:
        cmd += ["-thread_queue_size", "1024", "-f", "pulse", "-i", "rec.monitor"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "17", "-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    return subprocess.Popen(cmd + [raw], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_recorder(proc):
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
        proc.wait(timeout=40)
    except Exception:
        proc.kill()


def encode(src, dst, crf, with_audio, trim):
    cmd = ["ffmpeg", "-y", "-ss", f"{trim:.2f}", "-i", src, "-c:v", "libx264", "-preset", "veryfast",
           "-crf", str(crf), "-r", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    cmd += ["-c:a", "aac", "-b:a", "160k"] if with_audio else ["-an"]
    sh(cmd + [dst], check=True)


def probe(mp4):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0:s=x", mp4], capture_output=True, text=True)
    return r.stdout.strip() or "-"


def audio_status(mp4):
    r = subprocess.run(["ffmpeg", "-i", mp4, "-af", "volumedetect", "-vn", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+)", r.stderr or "")
    return "tidak-ada" if not m else ("ok" if float(m.group(1)) > -60 else "hening")


# ---------- alur utama ----------

def run_page(page, ctx, host):
    page.goto(URL, wait_until="load", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=2500)
    except Exception:
        pass
    LOAD["t"] = time.time()
    wait(page, 1500)
    t0 = time.time()
    seen, clicks, quiet, navs = set(), 0, 0, 0
    while True:
        if time.time() - t0 > HARD_CAP:
            return "batas aman"
        if end_visible(page):
            wait(page, 3000)
            return "teks penanda"
        close_popups(ctx, page)
        target = pick_target(page, seen, host) if TAP and clicks < MAX_CLICKS else None
        if target:
            quiet, clicks = 0, clicks + 1
            seen.add(key_of(target))
            before = text_len(page)
            try:
                tap_at(page, target["x"], target["y"])
            except Exception:
                continue
            wait(page, 1000)
            new = abs(text_len(page) - before)
            wait(page, min(new, 300) / 60 * 1000)  # waktu baca konten baru
            close_popups(ctx, page)
            if page.url.split("#")[0] != URL.split("#")[0] and navs < 5:
                navs += 1
                try:
                    page.go_back(timeout=8000)
                except Exception:
                    page.goto(URL, wait_until="load", timeout=30000)
                wait(page, 800)
                continue
            btn = next((c for c in cands(page) if CLOSE.search(c["text"] + " " + c["aria"] + " " + c["cls"])), None)
            if btn:
                try:
                    tap_at(page, btn["x"], btn["y"])
                except Exception:
                    pass
            elif new > 30:
                page.keyboard.press("Escape")
            wait(page, 500)
            continue
        # tidak ada yang bisa ditekan: scroll turun; kalau sudah mentok -> selesai
        try:
            a = page.evaluate(SCROLL_JS, True)
            wait(page, 800)
            moved = page.evaluate(SCROLL_JS, False)["top"] > a["top"] + 2
        except Exception:
            moved = False
        quiet = 0 if moved else quiet + 1
        if quiet >= 2:
            wait(page, 1000)
            return "semua sudah ditekan dan di-scroll"
        if not moved:
            wait(page, 900)


def main():
    os.makedirs(OUT, exist_ok=True)
    host = urlparse(URL).netloc
    xvfb = start_xvfb()
    with_audio = AUDIO and start_audio()
    raw = os.path.join(OUT, "raw.mkv")
    rec, err, reason, t_rec = None, None, "-", time.time()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=[
                "--no-sandbox", "--disable-dev-shm-usage", "--window-position=0,0", "--hide-scrollbars",
                f"--force-device-scale-factor={DPR}", "--autoplay-policy=no-user-gesture-required",
                "--disable-renderer-backgrounding", "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows", "--disable-features=CalculateNativeWinOcclusion",
            ])
            ctx = browser.new_context(
                viewport={"width": P["w"], "height": P["h"]}, device_scale_factor=DPR,
                is_mobile=TOUCH, has_touch=TOUCH, user_agent=P["ua"])
            ctx.add_init_script(OVERLAY)
            page = ctx.new_page()
            try:
                area = find_area(page)
                page.goto("about:blank")
                rec = start_recorder(raw, area, with_audio)
                t_rec = time.time()
                reason = run_page(page, ctx, host)
                wait(page, 800)
            except Exception as e:  # video tetap disimpan walau ada error
                err = e
            if rec:
                stop_recorder(rec)
            browser.close()
    finally:
        xvfb.terminate()
        sh(["pulseaudio", "--kill"])
    if not os.path.exists(raw):
        raise SystemExit("perekaman gagal: " + str(err))
    mp4, trim = os.path.join(OUT, "rec.mp4"), max(0.0, LOAD["t"] - t_rec - 0.3) if LOAD["t"] else 0.0
    for crf in (22, 28, 34, 38):
        encode(raw, mp4, crf, with_audio, trim)
        if os.path.getsize(mp4) <= 49 * 1024 * 1024:
            break
    suara = audio_status(mp4) if with_audio else "off"
    print(f"selesai: {mp4} {os.path.getsize(mp4)} bytes | durasi: {int(time.time() - t_rec - trim)}"
          f" | ukuran: {probe(mp4)} | suara: {suara} | alasan: {reason} | error: {err}")


if __name__ == "__main__":
    main()