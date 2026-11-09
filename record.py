import json
import os
import re
import subprocess
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

URL = os.environ["REC_URL"]
DEVICE = os.environ.get("REC_DEVICE", "hp").lower()
AUDIO = os.environ.get("REC_AUDIO", "1") != "0"
TAP = os.environ.get("REC_TAP", "1") != "0"
PACE = {"f": 0.6, "n": 1.0, "s": 1.7}.get(os.environ.get("REC_PACE", "n"), 1.0)
HARD_CAP = int(os.environ.get("REC_MAX_SECONDS", "480") or 480)  # pengaman, bukan durasi tetap
MAX_CLICKS = 80
OUT_DIR = "out"
DISPLAY = ":99"

try:
    RULES = json.loads(os.environ.get("REC_RULES") or "{}")
except ValueError:
    RULES = {}
CLICK_FIRST = [x.strip() for x in RULES.get("klik", []) if x.strip()]
AVOID = [x.strip() for x in RULES.get("hindari", []) if x.strip()]
END_TEXT = (RULES.get("selesai") or "").strip()

ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
IPAD_UA = (
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)

# w/h = ukuran tampilan (CSS px, harus genap), scale = pembesaran saat encode
PROFILES = {
    "pc": dict(w=1920, h=1080, scale=1, touch=False, mobile=False, ua=None),
    "hp": dict(w=360, h=800, scale=2, touch=True, mobile=True, ua=ANDROID_UA),
    "iphone": dict(w=390, h=844, scale=2, touch=True, mobile=True, ua=IPHONE_UA),
    "ipad": dict(w=820, h=1180, scale=1, touch=True, mobile=True, ua=IPAD_UA),
}
PROF = PROFILES.get(DEVICE, PROFILES["hp"])
TOUCH = PROF["touch"]

# Kursor merah (mode PC) + efek riak saat tap/klik
OVERLAY = """
(() => {
  const boot = () => {
    if (document.getElementById('__cur')) return;
    const c = document.createElement('div');
    c.id = '__cur';
    c.style.cssText = 'position:fixed;left:0;top:0;width:22px;height:22px;border-radius:50%;'
      + 'background:rgba(255,60,60,.55);border:2px solid #fff;box-shadow:0 0 8px rgba(0,0,0,.5);'
      + 'z-index:2147483647;pointer-events:none;transform:translate(-50%,-50%);display:none';
    document.documentElement.appendChild(c);
    const move = (e) => { c.style.display = 'block'; c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; };
    const ripple = (x, y) => {
      const r = document.createElement('div');
      r.style.cssText = 'position:fixed;width:56px;height:56px;border-radius:50%;'
        + 'border:3px solid rgba(255,60,60,.9);background:rgba(255,60,60,.25);'
        + 'z-index:2147483647;pointer-events:none;left:' + x + 'px;top:' + y + 'px;';
      document.documentElement.appendChild(r);
      const a = r.animate([
        {transform: 'translate(-50%,-50%) scale(.3)', opacity: 1},
        {transform: 'translate(-50%,-50%) scale(1.8)', opacity: 0}
      ], {duration: 550});
      a.onfinish = () => r.remove();
    };
    addEventListener('mousemove', move, true);
    addEventListener('mousedown', (e) => { move(e); ripple(e.clientX, e.clientY); }, true);
    addEventListener('touchstart', (e) => { const t = e.touches[0]; ripple(t.clientX, t.clientY); }, true);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
"""

# Cari semua yang bisa ditekan: tombol/link biasa DAN elemen div/gambar yang
# punya klik lewat JavaScript (dikenali dari cursor:pointer). Hanya yang terlihat
# di layar dan tidak tertutup elemen lain.
CAND_JS = r"""
() => {
  const vw = innerWidth, vh = innerHeight, out = [];
  const de = document.scrollingElement || document.documentElement;
  const NATIVE = ['button', 'a', 'summary', 'select', 'label'];
  const isNative = (el) => {
    const t = el.tagName.toLowerCase();
    return NATIVE.includes(t) || (t === 'input' && ['button', 'submit'].includes(el.type))
      || el.getAttribute('role') === 'button' || el.hasAttribute('onclick');
  };
  const okBox = (el) => {
    const r = el.getBoundingClientRect();
    return r.width >= 14 && r.height >= 14 && r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
  };
  const isCand = (el) => {
    if (!okBox(el)) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity < 0.05 || cs.pointerEvents === 'none') return false;
    const r = el.getBoundingClientRect();
    const big = r.width * r.height > 0.7 * vw * vh;
    if (isNative(el)) return true;
    return cs.cursor === 'pointer' && !big;
  };
  for (const el of document.body.querySelectorAll('*')) {
    if (!isCand(el)) continue;
    const p = el.parentElement;
    if (p && p !== document.body && isCand(p) && !isNative(el)) continue;
    const r = el.getBoundingClientRect();
    const cx = Math.min(Math.max(r.left + r.width / 2, 1), vw - 1);
    const cy = Math.min(Math.max(r.top + r.height / 2, 1), vh - 1);
    const top = document.elementFromPoint(cx, cy);
    if (!top || !(el === top || el.contains(top))) continue;
    const text = ((el.innerText || el.value || el.getAttribute('aria-label') || el.title || el.alt || '') + '')
      .replace(/\s+/g, ' ').trim().slice(0, 60);
    out.push({
      x: cx, y: cy, w: r.width, h: r.height, text,
      aria: (el.getAttribute('aria-label') || el.title || '') + '',
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 40),
      tag: el.tagName.toLowerCase(),
      href: el.getAttribute('href') || '',
      yd: Math.round(cy + de.scrollTop),
    });
  }
  return out.slice(0, 80);
}
"""

# Scroll bagian halaman yang memang bisa di-scroll (halaman utama atau kotak di dalamnya)
SCROLL_JS = r"""
(go) => {
  const de = document.scrollingElement || document.documentElement;
  let sc = de;
  if (de.scrollHeight <= de.clientHeight + 4) {
    let best = null, area = 0;
    for (const el of document.body.querySelectorAll('*')) {
      if (!/(auto|scroll)/.test(getComputedStyle(el).overflowY)) continue;
      if (el.scrollHeight - el.clientHeight > 4 && el.clientHeight * el.clientWidth > area) {
        best = el; area = el.clientHeight * el.clientWidth;
      }
    }
    if (best) sc = best;
  }
  if (go) sc.scrollBy({top: Math.round(sc.clientHeight * 0.7), behavior: 'smooth'});
  return {top: sc.scrollTop, max: sc.scrollHeight - sc.clientHeight};
}
"""

SKIP = re.compile(
    r"log\s?out|sign\s?out|keluar|delete|hapus|remove|bayar|pay\b|checkout|unsubscribe",
    re.I,
)
CLOSE = re.compile(r"\b(close|tutup|dismiss)\b|^[×✕✖✗xX]$", re.I)


def wait(page, ms):
    page.wait_for_timeout(int(ms * PACE))


def blocked(c, host):
    label = c["text"] + " " + c["href"]
    if SKIP.search(label):
        return True
    low = label.lower()
    if any(a.lower() in low for a in AVOID):
        return True
    href = c["href"]
    if href.startswith(("mailto:", "tel:", "javascript:")):
        return True
    if href.startswith("http") and urlparse(href).netloc != host:
        return True
    return False


def key_of(c):
    return f"{c['tag']}|{c['cls']}|{c['text'][:30]}|{int(c['x'] // 40)}|{int(c['yd'] // 40)}"


def pick_target(page, seen, host):
    try:
        cands = page.evaluate(CAND_JS)
    except Exception:
        return None
    cands = [c for c in cands if key_of(c) not in seen and not blocked(c, host)]
    if not cands:
        return None
    for want in CLICK_FIRST:  # urutan prioritas dari bot
        for c in cands:
            if want.lower() in (c["text"] + " " + c["aria"]).lower():
                return c
    cands.sort(key=lambda c: (round(c["y"] / 30), c["x"]))
    return cands[0]


def find_close(page):
    try:
        for c in page.evaluate(CAND_JS):
            if CLOSE.search(c["text"] + " " + c["aria"] + " " + c["cls"]):
                return c
    except Exception:
        pass
    return None


def tap_at(page, x, y):
    if TOUCH:
        page.touchscreen.tap(x, y)
    else:
        page.mouse.move(x, y, steps=25)
        wait(page, 250)
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
            """(t) => {
              t = t.toLowerCase();
              for (const el of document.body.querySelectorAll('*')) {
                if (el.children.length) continue;
                if (!(el.innerText || '').toLowerCase().includes(t)) continue;
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.bottom > 0 && r.top < innerHeight) return true;
              }
              return false;
            }""",
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

def sh(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def start_xvfb(w, h):
    proc = subprocess.Popen(
        ["Xvfb", DISPLAY, "-screen", "0", f"{w + 64}x{h + 220}x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = DISPLAY
    for _ in range(40):
        if os.path.exists("/tmp/.X11-unix/X99"):
            break
        time.sleep(0.25)
    return proc


def start_audio():
    os.makedirs("/tmp/xdg", mode=0o700, exist_ok=True)
    os.environ["XDG_RUNTIME_DIR"] = "/tmp/xdg"
    try:
        sh(["pulseaudio", "-D", "--exit-idle-time=-1", "--log-target=null"])
        time.sleep(1.5)
        a = sh(["pactl", "load-module", "module-null-sink", "sink_name=rec"])
        b = sh(["pactl", "set-default-sink", "rec"])
        return a.returncode == 0 and b.returncode == 0
    except Exception:
        return False


def find_page_origin(page, w, h):
    """Cari posisi area halaman di layar virtual (supaya bingkai browser tidak ikut terekam)."""
    try:
        from PIL import Image, ImageChops

        page.set_content(
            "<html><body style='margin:0;background:#f0f'></body></html>"
        )
        page.evaluate("document.documentElement.style.background='#f0f'")
        time.sleep(0.8)
        shot = os.path.join(OUT_DIR, "calib.png")
        sh(["ffmpeg", "-y", "-f", "x11grab", "-i", DISPLAY, "-frames:v", "1", shot])
        im = Image.open(shot).convert("RGB")
        r, g, b = im.split()
        hi = lambda v: 255 if v > 240 else 0
        lo = lambda v: 255 if v < 16 else 0
        mask = ImageChops.darker(
            ImageChops.darker(r.point(hi), b.point(hi)), g.point(lo)
        )
        box = mask.getbbox()
        if box and abs((box[2] - box[0]) - w) <= 4 and abs((box[3] - box[1]) - h) <= 4:
            return box[0] - box[0] % 2, box[1] - box[1] % 2
    except Exception as e:
        print("kalibrasi gagal:", type(e).__name__, flush=True)
    return 0, 0


def start_recorder(raw, w, h, x, y, with_audio):
    cmd = [
        "ffmpeg", "-y", "-thread_queue_size", "512",
        "-f", "x11grab", "-draw_mouse", "0", "-framerate", "25",
        "-video_size", f"{w}x{h}", "-i", f"{DISPLAY}.0+{x},{y}",
    ]
    if with_audio:
        cmd += ["-thread_queue_size", "512", "-f", "pulse", "-i", "rec.monitor"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd.append(raw)
    return subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def stop_recorder(proc):
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
        proc.wait(timeout=40)
    except Exception:
        proc.kill()


def encode(src, dst, crf, with_audio):
    s = PROF["scale"]
    vf = f"scale=trunc(iw*{s}/2)*2:trunc(ih*{s}/2)*2:flags=lanczos"
    cmd = [
        "ffmpeg", "-y", "-i", src, "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    cmd += ["-c:a", "aac", "-b:a", "128k"] if with_audio else ["-an"]
    cmd.append(dst)
    sh(cmd, check=True)


def audio_status(mp4):
    r = subprocess.run(
        ["ffmpeg", "-i", mp4, "-af", "volumedetect", "-vn", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.search(r"mean_volume:\s*(-?[\d.]+)", r.stderr or "")
    if not m:
        return "tidak-ada"
    return "ok" if float(m.group(1)) > -60 else "hening"


# ---------- alur utama ----------

def run_page(page, ctx, host):
    page.goto(URL, wait_until="load", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    wait(page, 2000)

    t0 = time.time()
    seen, clicks, quiet, navs = set(), 0, 0, 0
    while True:
        if time.time() - t0 > HARD_CAP:
            return "batas aman"
        if end_visible(page):
            wait(page, 4000)
            return "teks penanda"
        close_popups(ctx, page)

        target = pick_target(page, seen, host) if TAP and clicks < MAX_CLICKS else None
        if target:
            quiet = 0
            clicks += 1
            seen.add(key_of(target))
            before = text_len(page)
            try:
                tap_at(page, target["x"], target["y"])
            except Exception:
                continue
            wait(page, 1200)
            new_chars = abs(text_len(page) - before)
            wait(page, min(new_chars, 400) / 45 * 1000)  # waktu baca konten baru
            close_popups(ctx, page)
            if page.url.split("#")[0] != URL.split("#")[0] and navs < 6:
                navs += 1
                wait(page, 1500)
                try:
                    page.go_back(timeout=8000)
                except Exception:
                    page.goto(URL, wait_until="load", timeout=30000)
                wait(page, 1000)
                continue
            btn = find_close(page)
            if btn:
                try:
                    tap_at(page, btn["x"], btn["y"])
                except Exception:
                    pass
            elif new_chars > 30:
                page.keyboard.press("Escape")
            wait(page, 700)
            continue

        # tidak ada yang bisa ditekan: scroll turun, kalau sudah mentok dan sepi -> selesai
        try:
            a = page.evaluate(SCROLL_JS, True)
            wait(page, 900)
            b = page.evaluate(SCROLL_JS, False)
            moved = b["top"] > a["top"] + 2
        except Exception:
            moved = False
        if moved:
            quiet = 0
        else:
            quiet += 1
            wait(page, 1200)
            if quiet >= 3:
                wait(page, 1500)
                return "semua sudah ditekan dan di-scroll"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    host = urlparse(URL).netloc
    w, h = PROF["w"], PROF["h"]
    xvfb = start_xvfb(w, h)
    with_audio = AUDIO and start_audio()
    raw = os.path.join(OUT_DIR, "raw.mkv")
    rec, err, reason, t_start = None, None, "-", time.time()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=[
                    "--no-sandbox", "--disable-dev-shm-usage",
                    "--autoplay-policy=no-user-gesture-required",
                    "--window-position=0,0", "--hide-scrollbars",
                ],
            )
            ctx = browser.new_context(
                viewport={"width": w, "height": h},
                device_scale_factor=1,
                is_mobile=PROF["mobile"],
                has_touch=TOUCH,
                user_agent=PROF["ua"],
            )
            ctx.add_init_script(OVERLAY)
            page = ctx.new_page()
            try:
                x, y = find_page_origin(page, w, h)
                page.goto("about:blank")
                rec = start_recorder(raw, w, h, x, y, with_audio)
                t_start = time.time()
                reason = run_page(page, ctx, host)
                wait(page, 1000)
            except Exception as e:  # tetap simpan video walau ada error
                err = e
            if rec:
                stop_recorder(rec)
            browser.close()
    finally:
        xvfb.terminate()
        sh(["pulseaudio", "--kill"])

    if not os.path.exists(raw):
        raise SystemExit("perekaman gagal: " + str(err))
    mp4 = os.path.join(OUT_DIR, "rec.mp4")
    for crf in (26, 32, 38):
        encode(raw, mp4, crf, with_audio)
        if os.path.getsize(mp4) <= 49 * 1024 * 1024:
            break
    suara = audio_status(mp4) if with_audio else "off"
    dur = int(time.time() - t_start)
    print(
        f"selesai: {mp4} {os.path.getsize(mp4)} bytes | durasi: {dur} | suara: {suara}"
        f" | alasan: {reason} | error: {err}"
    )


if __name__ == "__main__":
    main()