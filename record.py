import os
import re
import subprocess
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

URL = os.environ["REC_URL"]
DEVICE = os.environ.get("REC_DEVICE", "pc").lower()
MAX_SEC = int(os.environ.get("REC_SECONDS", "45") or 45)
TAP = os.environ.get("REC_TAP", "1") != "0"
OUT_DIR = "out"

IPAD_UA = (
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)

# browser: chromium untuk PC, webkit (mesin Safari) untuk iPhone dan iPad
# scale: pembesaran video saat di-encode (layar HP kecil, jadi dibesarkan 2x)
PROFILES = {
    "pc": {
        "browser": "chromium",
        "scale": 1,
        "ctx": dict(
            viewport={"width": 1920, "height": 1080},
            is_mobile=False,
            has_touch=False,
            device_scale_factor=1,
        ),
    },
    "iphone": {
        "browser": "webkit",
        "scale": 2,
        "ctx": dict(
            viewport={"width": 393, "height": 852},
            is_mobile=True,
            has_touch=True,
            device_scale_factor=3,
            user_agent=IPHONE_UA,
        ),
    },
    "ipad": {
        "browser": "webkit",
        "scale": 1,
        "ctx": dict(
            viewport={"width": 1366, "height": 1024},
            is_mobile=True,
            has_touch=True,
            device_scale_factor=2,
            user_agent=IPAD_UA,
        ),
    },
}
PROF = PROFILES.get(DEVICE, PROFILES["pc"])
CTX = PROF["ctx"]
TOUCH = CTX["has_touch"]

# Kursor merah (hanya muncul di mode PC) + efek riak saat tap/klik
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

SEL = "button, a[href], [role=button], input[type=submit], input[type=button], summary, [onclick]"
SKIP = re.compile(
    r"log\s?out|sign\s?out|keluar|delete|hapus|remove|bayar|pay|checkout|unsubscribe",
    re.I,
)


def scroll_tour(page):
    try:
        height = page.evaluate("document.documentElement.scrollHeight")
        view = CTX["viewport"]["height"]
        step = int(view * 0.6)
        y = 0
        for _ in range(8):
            y += step
            if y >= height:
                break
            page.evaluate(f"window.scrollTo({{top:{y},behavior:'smooth'}})")
            page.wait_for_timeout(700)
        page.evaluate("window.scrollTo({top:0,behavior:'smooth'})")
        page.wait_for_timeout(900)
    except Exception:
        pass


def next_target(page, seen, host):
    for el in page.query_selector_all(SEL)[:60]:
        try:
            if not el.is_visible():
                continue
            txt = (
                el.inner_text()
                or el.get_attribute("value")
                or el.get_attribute("aria-label")
                or ""
            ).strip()[:40]
            href = el.get_attribute("href") or ""
            if SKIP.search(txt) or SKIP.search(href):
                continue
            if href.startswith(("mailto:", "tel:", "javascript:")):
                continue
            if href.startswith("http") and urlparse(href).netloc != host:
                continue
            box = el.bounding_box()
            if not box or box["width"] < 8 or box["height"] < 8:
                continue
            key = (el.evaluate("e => e.tagName"), txt, href)
            if key in seen:
                continue
            seen.add(key)
            return el
        except Exception:
            continue
    return None


def tap(page, el):
    el.scroll_into_view_if_needed(timeout=2000)
    page.wait_for_timeout(400)
    box = el.bounding_box()
    if not box:
        return False
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    if TOUCH:
        page.touchscreen.tap(x, y)
    else:
        page.mouse.move(x, y, steps=30)
        page.wait_for_timeout(250)
        page.mouse.click(x, y)
    return True


def close_popups(ctx, page):
    for pg in ctx.pages:
        if pg != page:
            try:
                pg.close()
            except Exception:
                pass


def encode(src, dst, crf):
    s = PROF["scale"]
    # lebar/tinggi harus genap untuk H.264
    vf = f"scale=trunc(iw*{s}/2)*2:trunc(ih*{s}/2)*2:flags=lanczos"
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", src, "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", dst,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    host = urlparse(URL).netloc
    t0 = time.time()
    with sync_playwright() as p:
        engine = getattr(p, PROF["browser"])
        args = ["--no-sandbox"] if PROF["browser"] == "chromium" else []
        browser = engine.launch(args=args)
        ctx = browser.new_context(
            record_video_dir=OUT_DIR,
            record_video_size=CTX["viewport"],
            **CTX,
        )
        ctx.add_init_script(OVERLAY)
        page = ctx.new_page()
        video = page.video
        err = None
        try:
            page.goto(URL, wait_until="load", timeout=30000)
            page.wait_for_timeout(2000)
            scroll_tour(page)
            if TAP:
                seen = set()
                while time.time() - t0 < MAX_SEC - 4:
                    el = next_target(page, seen, host)
                    if el is None:
                        break
                    try:
                        tap(page, el)
                    except Exception:
                        continue
                    page.wait_for_timeout(1500)
                    close_popups(ctx, page)
                    if page.url.split("#")[0] != URL.split("#")[0]:
                        page.wait_for_timeout(1200)
                        scroll_tour(page)
                        page.goto(URL, wait_until="load", timeout=30000)
                        page.wait_for_timeout(1000)
                    else:
                        page.keyboard.press("Escape")
            else:
                page.wait_for_timeout(500)
                scroll_tour(page)
            page.wait_for_timeout(1000)
        except Exception as e:  # tetap simpan video walau ada error
            err = e
        ctx.close()
        webm = video.path()
        browser.close()
    mp4 = os.path.join(OUT_DIR, "rec.mp4")
    encode(webm, mp4, 26)
    if os.path.getsize(mp4) > 49 * 1024 * 1024:
        encode(webm, mp4, 34)
    print("selesai:", mp4, os.path.getsize(mp4), "bytes", "| error:", err)


if __name__ == "__main__":
    main()
