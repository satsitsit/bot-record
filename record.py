import json, math, os, re, shutil, signal, subprocess, sys, time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

signal.signal(signal.SIGTERM, lambda *a: sys.exit(1))  # supaya /batal bersih-bersih

URL = os.environ["REC_URL"]
DEVICE = os.environ.get("REC_DEVICE", "hp").lower()
AUDIO = os.environ.get("REC_AUDIO", "1") != "0"
TAP = os.environ.get("REC_TAP", "1") != "0"
PKEY = os.environ.get("REC_PACE", "f")
PKEY = PKEY if PKEY in ("f", "n", "s") else "f"
HD_FULL = os.environ.get("REC_QUALITY", "a") == "h"  # h = HD penuh tanpa penghematan; a = otomatis (lancar)
HARD_CAP = int(os.environ.get("REC_MAX_SECONDS", "240") or 240)  # pengaman mutlak
DEBUG = os.environ.get("REC_DEBUG") == "1"
PACE = {"f": 0.75, "n": 1.0, "s": 1.5}[PKEY]
BUDGET = max(45, min({"f": 140, "n": 190, "s": 240}[PKEY], HARD_CAP - 10))  # target durasi rekam (detik)
OUT, DISP, TARGET_FPS, MIN_FPS, MAX_CLICKS = "out", ":99", 20.0, 14.0, 70

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
# tangga ukuran, dari paling tajam ke paling ringan: (lebar CSS, tinggi CSS, dpr)
LADDER = {
    "pc": [(1920, 1080, 1), (1280, 720, 1), (960, 540, 1)],
    "hp": [(360, 800, 2), (360, 800, 1.5), (360, 800, 1)],
    "iphone": [(390, 844, 2), (390, 844, 1.5), (390, 844, 1)],
    "ipad": [(820, 1180, 1), (820, 1180, 0.75), (820, 1180, 0.6)],
}
if DEVICE not in LADDER:
    DEVICE = "hp"
RES = LADDER[DEVICE]
TOUCH = DEVICE != "pc"
LOAD = {"t": 0.0}
SEEN_BASE = {}
SESS = {}
SESS_N = [0]

# Daftar fitur Chromium yang dimatikan. HARUS satu flag saja: kalau ada dua --disable-features,
# yang terakhir menang dan daftar bawaan Playwright (termasuk Translate) hilang -> bar terjemahan muncul.
DISABLED_FEATURES = ",".join([
    "Translate", "TranslateUI", "TranslateSubFrames", "CalculateNativeWinOcclusion", "AvoidUnnecessaryBeforeUnloadCheckSync",
    "DestroyProfileOnBrowserClose", "DialMediaRouteProvider", "GlobalMediaControls", "HttpsUpgrades", "LensOverlay",
    "MediaRouter", "PaintHolding", "ThirdPartyStoragePartitioning", "BlockOriginHeaderModificationOnRedirect",
    "AutoDeElevate", "OptimizationHints", "ImprovedCookieControls", "AcceptCHFrame", "PrivacySandboxSettings4",
    "msForceBrowserSignIn", "msEdgeUpdateLaunchServicesPreferredVersion",
])

# kata kunci pemutar musik / suara: elemen ini TIDAK ditekan supaya musik tidak mati
CFG = {
    "audioAny": r"mute|unmute|volume|\bvol\b|sound|suara|speaker|musik|music|lagu|\bsong\b|audio|bisu|senyap",
    "audioCtl": r"\bplay\b|pause|\bstop\b|\bprev\b|previous|\bnext\b|skip|shuffle|repeat|\bloop\b|seek|track|putar|jeda",
    "musicBox": r"music|musik|player|audio|\bsong\b|playlist|spotify|vinyl|\bdisc\b|\btrack\b|lagu|soundcloud|equalizer|listening|now-?playing|jukebox|radio|cassette|turntable|\bbgm\b|\bmp3\b",
}

HELPERS = r"""
(() => {
  if (window.__rec) return;
  const R = window.__rec = { media: new Set(), ctxs: [], reg: new Set(), lv: 0, _sc: null, _sct: 0, _g: [] };

  // 1) browser jangan menawarkan terjemahan (lapis ke-3 setelah flag dan preferensi profil)
  try {
    const mark = () => {
      const h = document.documentElement;
      if (!h || !document.head) return false;
      h.setAttribute('translate', 'no');
      if (!document.querySelector('meta[name="google"]')) {
        const m = document.createElement('meta'); m.name = 'google'; m.content = 'notranslate'; document.head.appendChild(m);
      }
      return true;
    };
    if (!mark()) { const mo = new MutationObserver(() => { if (mark()) mo.disconnect(); }); mo.observe(document, { childList: true, subtree: true }); }
  } catch (e) {}

  // 2) lacak audio (<audio>, new Audio(), WebAudio) supaya klik bot tidak bisa mematikan suara
  try {
    const play = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function () { try { R.media.add(this); } catch (e) {} return play.apply(this, arguments); };
  } catch (e) {}
  try {
    for (const k of ['AudioContext', 'webkitAudioContext']) {
      const AC = window[k]; if (!AC) continue;
      window[k] = new Proxy(AC, { construct(t, a, nt) { const c = Reflect.construct(t, a, nt); R.ctxs.push(c); return c; } });
    }
  } catch (e) {}
  const allMedia = () => { const s = new Set(R.media); try { document.querySelectorAll('audio,video').forEach((m) => s.add(m)); } catch (e) {} return [...s]; };
  R.guardBegin = () => {
    const now = performance.now();
    R._g = allMedia().map((m) => ({ el: m, p: m.paused, e: m.ended, m: m.muted, v: m.volume, t: m.currentTime, time: now }));
    R._ctxRun = R.ctxs.some((c) => { try { return c.state === 'running'; } catch (e) { return false; } });
    return R._g.filter((x) => !x.p).length;
  };
  R.guardEnd = () => {
    const now = performance.now(); let n = 0; const why = [];
    for (const s of R._g || []) {
      if (s.p || s.e) continue;                       // tadinya tidak sedang memutar
      const m = s.el; let bad = '';
      if (m.paused && !m.ended) bad = 'pause';
      else if (m.muted && !s.m) bad = 'mute';
      else if (Math.abs(m.volume - s.v) > 0.05) bad = 'volume';
      else if (!m.loop && m.currentTime < s.t - 1.0) bad = 'ulang';
      if (!bad) continue;
      n++; why.push(bad);
      try {
        if (m.muted && !s.m) m.muted = false;
        if (Math.abs(m.volume - s.v) > 0.05) m.volume = s.v;
        if (bad === 'ulang' && m.readyState >= 1) m.currentTime = s.t + (now - s.time) / 1000;
        if (m.paused) { const pr = m.play(); if (pr && pr.catch) pr.catch(() => {}); }
      } catch (e) {}
    }
    for (const c of R.ctxs) { try { if (R._ctxRun && c.state === 'suspended') { c.resume(); n++; why.push('ctx'); } } catch (e) {} }
    return n ? why.join(',') : '';
  };

  // 3) catat elemen yang didaftari listener klik lewat JS (tidak kelihatan dari DOM)
  try {
    const add = EventTarget.prototype.addEventListener;
    const TYPES = /^(click|pointerdown|pointerup|mousedown|mouseup|touchstart|touchend)$/;
    EventTarget.prototype.addEventListener = function (type, fn, opts) {
      try { if (this instanceof Element && TYPES.test(type) && this !== document.documentElement && this !== document.body) R.reg.add(this); } catch (e) {}
      return add.call(this, type, fn, opts);
    };
  } catch (e) {}

  // 4) kursor merah + riak supaya terlihat di video
  const boot = () => {
    if (document.getElementById('__cur') || !document.documentElement) return;
    const c = document.createElement('div'); c.id = '__cur';
    c.style.cssText = 'position:fixed;left:0;top:0;width:22px;height:22px;border-radius:50%;background:rgba(255,60,60,.55);border:2px solid #fff;box-shadow:0 0 8px rgba(0,0,0,.5);z-index:2147483647;pointer-events:none;transform:translate(-50%,-50%);display:none';
    document.documentElement.appendChild(c);
    const move = (e) => { c.style.display = 'block'; c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; };
    const ripple = (x, y) => {
      const r = document.createElement('div');
      r.style.cssText = 'position:fixed;width:56px;height:56px;border-radius:50%;border:3px solid rgba(255,60,60,.9);background:rgba(255,60,60,.25);z-index:2147483647;pointer-events:none;left:' + x + 'px;top:' + y + 'px;';
      document.documentElement.appendChild(r);
      r.animate([{ transform: 'translate(-50%,-50%) scale(.3)', opacity: 1 }, { transform: 'translate(-50%,-50%) scale(1.8)', opacity: 0 }], { duration: 550 }).onfinish = () => r.remove();
    };
    addEventListener('mousemove', move, true);
    addEventListener('mousedown', (e) => { move(e); ripple(e.clientX, e.clientY); }, true);
    addEventListener('touchstart', (e) => { const t = e.touches[0]; ripple(t.clientX, t.clientY); }, true);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();

  // 5) mode ringan: kecilkan blur besar (dan backdrop-filter) yang berat untuk CPU, hanya bila diminta
  const clampBlur = (s, MAX) => {
    if (typeof s !== 'string' || !/blur\(/i.test(s)) return s;
    let out = '', i = 0; const low = s.toLowerCase();
    while (i < s.length) {
      const j = low.indexOf('blur(', i);
      if (j < 0) { out += s.slice(i); break; }
      out += s.slice(i, j + 5);
      let d = 1, k = j + 5;
      while (k < s.length && d > 0) { if (s[k] === '(') d++; else if (s[k] === ')') d--; k++; }
      const arg = s.slice(j + 5, k - 1).trim();
      out += (/^min\(.*,\s*[\d.]+px\)$/i.test(arg) && arg.endsWith(MAX + 'px)')) ? arg : 'min(' + arg + ', ' + MAX + 'px)';
      out += ')'; i = k;
    }
    return out;
  };
  const MAXB = () => (R.lv >= 2 ? 14 : 24);
  const fixStyle = (st) => {
    const f = st.getPropertyValue('filter');
    if (f && /blur\(/i.test(f)) { const n = clampBlur(f, MAXB()); if (n !== f) st.setProperty('filter', n, st.getPropertyPriority('filter')); }
  };
  const fixRule = (r) => { try { if (r.style) fixStyle(r.style); if (r.cssRules) for (const x of r.cssRules) fixRule(x); } catch (e) {} };
  const sweep = () => {
    if (!R.lv) return;
    try { for (const sh of document.styleSheets) { try { for (const r of sh.cssRules) fixRule(r); } catch (e) {} } } catch (e) {}
    try { document.querySelectorAll('[style*="blur"]').forEach((el) => fixStyle(el.style)); } catch (e) {}
    if (R.lv >= 2 && !document.getElementById('__lite2') && document.documentElement) {
      const st = document.createElement('style'); st.id = '__lite2';
      st.textContent = '*,*::before,*::after{backdrop-filter:none!important;-webkit-backdrop-filter:none!important}';
      (document.head || document.documentElement).appendChild(st);
    }
  };
  try {
    const d = Object.getOwnPropertyDescriptor(CanvasRenderingContext2D.prototype, 'filter');
    if (d && d.set) Object.defineProperty(CanvasRenderingContext2D.prototype, 'filter', { configurable: true, enumerable: d.enumerable, get: d.get, set(v) { d.set.call(this, R.lv ? clampBlur(v, MAXB()) : v); } });
  } catch (e) {}
  R.lite = (lv) => { R.lv = lv; sweep(); return lv; };
  document.addEventListener('DOMContentLoaded', sweep, { once: true });
  addEventListener('load', sweep, { once: true });
  setInterval(sweep, 2500);

  // 6) ukur kecepatan render (frame per detik) yang benar-benar dihasilkan browser
  R.fps = (ms) => new Promise((res) => {
    let n = 0; const t0 = performance.now();
    const f = (t) => { n++; if (t - t0 < ms) requestAnimationFrame(f); else res(n / ((t - t0) / 1000)); };
    requestAnimationFrame(f);
  });

  // 7) posisi scroll (halaman utama atau kotak scroll terbesar)
  R.scroller = () => {
    const now = performance.now();
    if (R._sc && now - R._sct < 4000 && R._sc.isConnected) return R._sc;
    const de = document.scrollingElement || document.documentElement; let sc = de;
    if (de.scrollHeight <= de.clientHeight + 4 && document.body) {
      let best = null, area = 0;
      for (const el of document.body.querySelectorAll('*')) {
        if (!/(auto|scroll)/.test(getComputedStyle(el).overflowY)) continue;
        if (el.scrollHeight - el.clientHeight > 4) { const a = el.clientHeight * el.clientWidth; if (a > area) { best = el; area = a; } }
      }
      if (best) sc = best;
    }
    R._sc = sc; R._sct = now; return sc;
  };
  R.pos = () => {
    const sc = R.scroller(); const page = sc === document.scrollingElement || sc === document.documentElement || sc === document.body;
    return { top: sc.scrollTop, max: Math.max(0, sc.scrollHeight - sc.clientHeight), vh: page ? innerHeight : sc.clientHeight };
  };
  R.scrollBy = (f) => { const p = R.pos(); R.scroller().scrollBy({ top: Math.round(p.vh * f), behavior: 'smooth' }); };
  R.textLen = () => (document.body ? document.body.innerText.length : 0);
  R.visText = () => {
    let n = 0; const vh = innerHeight, vw = innerWidth;
    if (!document.body) return 0;
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let t;
    while ((t = w.nextNode())) {
      const s = (t.nodeValue || '').trim(); if (!s) continue;
      const p = t.parentElement; if (!p) continue;
      const r = p.getBoundingClientRect();
      if (r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw) { const cs = getComputedStyle(p); if (cs.visibility !== 'hidden' && +cs.opacity > 0.05) n += s.length; }
    }
    return n;
  };

  // 8) jendela/overlay besar yang baru muncul (lightbox, menu, popup)
  R.overlay = () => {
    const vw = innerWidth, vh = innerHeight, tot = vw * vh;
    const pts = [[vw / 2, vh / 2], [vw / 2, vh * 0.25], [vw * 0.25, vh / 2], [vw * 0.75, vh * 0.75]];
    for (const [x, y] of pts) {
      let el = document.elementFromPoint(x, y);
      for (let d = 0; el && d < 12 && el !== document.body && el !== document.documentElement; d++, el = el.parentElement) {
        const cs = getComputedStyle(el);
        if (cs.position === 'fixed' && cs.pointerEvents !== 'none' && +cs.opacity > 0.05 && cs.visibility !== 'hidden') {
          const r = el.getBoundingClientRect();
          if (r.width * r.height >= 0.35 * tot) {
            const cl = (typeof el.className === 'string' ? el.className : '').split(' ')[0];
            return { el, sig: el.tagName + '#' + el.id + '.' + cl };
          }
        }
      }
    }
    return null;
  };
  R.overlaySig = () => { const o = R.overlay(); return o ? o.sig : ''; };

  // 9) cari semua yang bisa ditekan: tombol, link, div/gambar ber-klik (cursor:pointer / listener JS)
  R.cands = async (cfg) => {
    const vw = innerWidth, vh = innerHeight;
    const AUDIO_ANY = new RegExp(cfg.audioAny, 'i'), AUDIO_CTL = new RegExp(cfg.audioCtl, 'i'), MUSIC_BOX = new RegExp(cfg.musicBox, 'i');
    const GLYPH = /[⏩-⏺▶■▮◀♪♫\u{1F50A}-\u{1F50C}\u{1F507}-\u{1F509}\u{1F3B5}\u{1F3B6}\u{1F3A7}]/u;
    const NATSEL = 'a[href],button,summary,[role=button],[role=tab],[role=menuitem],[role=switch],[role=link],[role=checkbox],[onclick],[tabindex]:not([tabindex="-1"]),input[type=button],input[type=submit],input[type=checkbox],input[type=radio],label';
    const NOCLICK = 'input[type=range],input[type=file],input[type=text],input[type=password],input[type=email],input[type=search],input[type=number],input[type=tel],input[type=url],textarea,select,[contenteditable=true],[role=slider]';
    const sc = R.scroller(); const st = (sc && sc.scrollTop) || 0;
    const ov = R.overlay();
    const pool = new Set();
    if (!document.body) return [];
    document.body.querySelectorAll(NATSEL).forEach((e) => pool.add(e));
    for (const e of R.reg) { if (!e.isConnected) R.reg.delete(e); else pool.add(e); }
    const all = document.body.getElementsByTagName('*');
    for (let i = 0; i < all.length && pool.size < 500; i++) {
      const el = all[i]; if (pool.has(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 14 || r.height < 14 || r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw || r.width * r.height > 0.7 * vw * vh) continue;
      if (getComputedStyle(el).cursor !== 'pointer') continue;
      const p = el.parentElement;
      if (p && p !== document.body && getComputedStyle(p).cursor === 'pointer') continue;  // anak dari elemen yang sudah bisa ditekan
      pool.add(el);
    }
    const info = (el) => {
      const r = el.getBoundingClientRect();
      if (r.width < 14 || r.height < 14 || r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) return null;
      if (el.matches(NOCLICK) || el.disabled || el.getAttribute('aria-disabled') === 'true' || el.id === '__cur') return null;
      const cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.pointerEvents === 'none' || +cs.opacity < 0.05 || cs.display === 'none') return null;
      let fixed = cs.position === 'fixed';
      for (let a = el.parentElement, d = 0; a && a !== document.documentElement && d < 8; a = a.parentElement, d++) {
        const ca = getComputedStyle(a); if (+ca.opacity < 0.05) return null; if (ca.position === 'fixed') fixed = true;
      }
      const nat = el.matches(NATSEL);
      if (!nat && r.width * r.height > 0.7 * vw * vh) return null;
      const x0 = Math.max(r.left, 0), x1 = Math.min(r.right, vw), y0 = Math.max(r.top, 0), y1 = Math.min(r.bottom, vh);
      const x = (x0 + x1) / 2, y = (y0 + y1) / 2;
      const top = document.elementFromPoint(x, y);
      if (!top || !(el === top || el.contains(top))) return null;
      const lab = ((el.innerText || el.value || el.getAttribute('aria-label') || el.title || el.alt || '') + '').replace(/\s+/g, ' ').trim().slice(0, 60);
      const aria = (el.getAttribute('aria-label') || el.title || '') + '';
      const cls = (typeof el.className === 'string' ? el.className : '').slice(0, 40);
      const img = el.querySelector && el.querySelector('img,use');
      const meta = [lab, aria, el.id, cls, el.getAttribute('name') || '', el.getAttribute('data-action') || '', el.getAttribute('href') || '', img ? (img.getAttribute('src') || img.getAttribute('href') || '') : ''].join(' ');
      // pemutar musik / kontrol suara: jangan disentuh
      let inBox = false;
      for (let a = el, d = 0; a && a !== document.body && d < 6; a = a.parentElement, d++) {
        const ar = a.getBoundingClientRect();
        if (ar.width * ar.height > 0.25 * vw * vh) break;
        if (MUSIC_BOX.test((a.id || '') + ' ' + (typeof a.className === 'string' ? a.className : '') + ' ' + (a.getAttribute('aria-label') || ''))) { inBox = true; break; }
      }
      if (AUDIO_ANY.test(meta) || GLYPH.test(lab) || inBox) return { skip: 'audio' };
      const inOv = !!(ov && ov.el.contains(el));
      return {
        x, y, w: r.width, h: r.height, tag: el.tagName.toLowerCase(), href: el.getAttribute('href') || '', text: lab, aria, cls, id: el.id || '', nat, ov: inOv,
        xd: Math.round(r.left + r.width / 2), yd: Math.round(r.top + r.height / 2 + (fixed ? 0 : st)), fixed,
        r: [r.left, r.top, r.width, r.height], el,
      };
    };
    const first = [];
    for (const el of pool) { const i = info(el); if (i && !i.skip) first.push(i); }
    await new Promise((res) => setTimeout(res, 140));
    const out = [];
    for (const c of first) {                    // yang bergerak (kelopak jatuh, animasi) dibuang
      const r = c.el.getBoundingClientRect();
      if (Math.abs(r.left - c.r[0]) > 3 || Math.abs(r.top - c.r[1]) > 3 || Math.abs(r.width - c.r[2]) > 4 || Math.abs(r.height - c.r[3]) > 4) continue;
      c.base = [c.tag, c.id, c.cls.split(' ')[0], c.text.slice(0, 24)].join('|');
      c.sig = c.base + '|' + Math.round(c.xd / 40) + '|' + Math.round(c.yd / 40) + (c.fixed ? '|F' : '');
      delete c.el; delete c.r; out.push(c);
    }
    return out.slice(0, 80);
  };
})();
"""

MEASURE_JS = r"""(ms) => new Promise((res) => {
  let n = 0, sc = false; const t0 = performance.now();
  const f = (t) => { n++; const el = t - t0;
    if (!sc && el > ms * 0.35) { sc = true; try { scrollBy({ top: innerHeight * 0.6, behavior: 'smooth' }); } catch (e) {} }
    if (el < ms) requestAnimationFrame(f); else { try { scrollTo({ top: 0, behavior: 'instant' }); } catch (e) {} res(n / (el / 1000)); } };
  requestAnimationFrame(f); })"""

SKIP = re.compile(r"log\s?out|sign\s?out|keluar|delete|hapus|remove|bayar|pay\b|checkout|unsubscribe|download|unduh", re.I)
BADHREF = re.compile(r"\.(pdf|zip|apk|exe|dmg|rar|7z|mp3|mp4|mov)(\?|#|$)", re.I)
CLOSE_STRONG = re.compile(r"\b(close|tutup|dismiss)\b|^[×✕✖✗xX]$", re.I)
CLOSE_WEAK = re.compile(r"\b(back|kembali|cancel|batal|ok|oke)\b", re.I)


def dbg(*a):
    if DEBUG:
        print("[rec]", *a, file=sys.stderr, flush=True)


# ---------- util browser ----------

def wait(page, ms):  # jeda yang ikut kecepatan (Cepat/Normal/Pelan)
    page.wait_for_timeout(int(ms * PACE))


def pause(page, sec):  # jeda tetap, tidak ikut kecepatan
    page.wait_for_timeout(int(sec * 1000))


def sh(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def ev(page, js, arg=None, default=None):
    try:
        return page.evaluate(js, arg) if arg is not None else page.evaluate(js)
    except Exception:
        return default


def blocked(c, host):
    label = (c["text"] + " " + c["href"] + " " + c["aria"]).lower()
    h = c["href"]
    return bool(
        SKIP.search(label) or any(a in label for a in AVOID) or BADHREF.search(h)
        or h.startswith(("mailto:", "tel:", "javascript:"))
        or (h.startswith("http") and urlparse(h).netloc != host)
    )


def cands(page):
    return ev(page, "cfg => window.__rec.cands(cfg)", CFG, [])


def pick_target(page, seen, bad, host):
    def again(c):  # elemen yang sama (id/teks/kelas) tidak ditekan berulang walau posisinya berubah
        return SEEN_BASE.get(c["base"], 0) >= (1 if c["id"] else 3 if c["text"] else 4)
    cs = [c for c in cands(page) if c["sig"] not in seen and c["sig"] not in bad and not again(c) and not blocked(c, host)]
    inov = [c for c in cs if c["ov"]]
    cs = inov or cs                                  # kalau ada jendela terbuka, utamakan isinya
    for want in CLICK_FIRST:
        for c in cs:
            if want.lower() in (c["text"] + " " + c["aria"]).lower():
                return c
    cs.sort(key=lambda c: (round(c["y"] / 40), c["x"]))
    return cs[0] if cs else None


def tap_at(page, x, y):
    if TOUCH:
        page.touchscreen.tap(x, y)
    else:
        page.mouse.move(x, y, steps=12)
        pause(page, 0.1)
        page.mouse.click(x, y, delay=50)


def pos_of(page):
    return ev(page, "window.__rec.pos()", None, {"top": 0, "max": 0, "vh": 800})


def text_len(page):
    return ev(page, "window.__rec.textLen()", None, 0) or 0


def overlay_sig(page):
    return ev(page, "window.__rec.overlaySig()", None, "") or ""


def end_visible(page):
    if not END_TEXT:
        return False
    return bool(ev(page, """(t) => { t = t.toLowerCase(); if (!(document.body.innerText || '').toLowerCase().includes(t)) return false;
        for (const el of document.body.querySelectorAll('*')) {
          if (el.children.length || !(el.innerText || '').toLowerCase().includes(t)) continue;
          const r = el.getBoundingClientRect();
          if (r.width > 0 && r.bottom > 0 && r.top < innerHeight) return true;
        } return false; }""", END_TEXT, False))


def close_popups(ctx, page):
    for pg in ctx.pages:
        if pg != page:
            try:
                pg.close()
            except Exception:
                pass


def same_page(a, b):
    ua, ub = urlparse(a), urlparse(b)
    return (ua.scheme, ua.netloc, ua.path, ua.query) == (ub.scheme, ub.netloc, ub.path, ub.query)


def label_of(c):
    return c["text"] + " " + c["aria"] + " " + c["cls"] + " " + c["id"]


def close_overlay(page, base_sig):
    """Tutup lightbox/popup yang baru muncul: tombol tutup -> Escape -> tap area luar."""
    for attempt in range(3):
        if attempt == 0:
            cs = cands(page)
            btn = next((c for c in cs if c["ov"] and (CLOSE_STRONG.search(label_of(c)) or CLOSE_WEAK.search(label_of(c)))), None) \
                or next((c for c in cs if CLOSE_STRONG.search(label_of(c))), None)
            if btn:
                try:
                    tap_at(page, btn["x"], btn["y"])
                except Exception:
                    pass
        elif attempt == 1:
            page.keyboard.press("Escape")
        else:
            vp = page.viewport_size or {"width": 360, "height": 800}
            try:
                tap_at(page, 6, vp["height"] / 2)
            except Exception:
                pass
        wait(page, 500)
        now = overlay_sig(page)
        if now in ("", base_sig):
            return True
    return False


# ---------- layar virtual, suara, perekam ----------

def start_xvfb(w, h, dpr):
    sh(["pkill", "-f", f"Xvfb {DISP}"])
    time.sleep(0.4)
    for f in ("/tmp/.X11-unix/X99", "/tmp/.X99-lock"):  # sisa Xvfb lama bikin pengecekan "sudah siap" salah
        try:
            os.remove(f)
        except OSError:
            pass
    sw, sh_ = int(w * dpr) + 1200, int(h * dpr) + 500
    proc = subprocess.Popen(["Xvfb", DISP, "-screen", "0", f"{sw}x{sh_}x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.environ["DISPLAY"] = DISP
    for _ in range(50):
        if os.path.exists("/tmp/.X11-unix/X99") and proc.poll() is None:
            break
        time.sleep(0.2)
    time.sleep(0.5)
    return proc


def start_audio():
    os.makedirs("/tmp/xdg", mode=0o700, exist_ok=True)
    os.environ["XDG_RUNTIME_DIR"] = "/tmp/xdg"
    try:
        sh(["pulseaudio", "-D", "--exit-idle-time=-1", "--log-target=null"])
        for _ in range(25):  # tunggu server suara benar-benar siap
            if sh(["pactl", "info"]).returncode == 0:
                break
            time.sleep(0.2)
        return sh(["pactl", "load-module", "module-null-sink", "sink_name=rec"]).returncode == 0 and \
            sh(["pactl", "set-default-sink", "rec"]).returncode == 0
    except Exception:
        return False


def find_area(page, w, h, dpr):
    """Cari area halaman di layar virtual (bingkai browser tidak ikut terekam). Hasil: x, y, w, h."""
    ew, eh = int(w * dpr) // 2 * 2, int(h * dpr) // 2 * 2
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


# ---------- sesi browser + uji kelancaran ----------

def shm_ok():
    try:
        st = os.statvfs("/dev/shm")
        return st.f_bavail * st.f_frsize >= 1 << 30
    except Exception:
        return False


def seed_profile(prof):
    """Profil Chromium baru dengan terjemahan, notifikasi, dan popup izin dimatikan."""
    shutil.rmtree(prof, ignore_errors=True)
    os.makedirs(os.path.join(prof, "Default"), exist_ok=True)
    prefs = {
        "translate": {"enabled": False},
        "credentials_enable_service": False,
        "profile": {"password_manager_enabled": False,
                    "default_content_setting_values": {"notifications": 2, "geolocation": 2}},
        "autofill": {"profile_enabled": False, "credit_card_enabled": False},
        "browser": {"check_default_browser": False},
        "session": {"restore_on_startup": 5},
    }
    with open(os.path.join(prof, "Default", "Preferences"), "w") as f:
        json.dump(prefs, f)


def open_session(p, ri, idx):
    w, h, dpr = RES[ri]
    xvfb = start_xvfb(w, h, dpr)
    prof = f"/tmp/recprof-{os.getpid()}-{idx}"
    seed_profile(prof)
    args = ["--no-sandbox", "--window-position=0,0", "--hide-scrollbars", f"--force-device-scale-factor={dpr}",
            "--autoplay-policy=no-user-gesture-required", "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
            "--disable-features=" + DISABLED_FEATURES, "--disable-translate", "--deny-permission-prompts",
            "--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble", "--disable-infobars",
            "--password-store=basic", "--use-mock-keychain"]
    ignore = []
    if shm_ok():
        ignore.append("--disable-dev-shm-usage")  # bitmap di RAM, bukan di disk: lebih ringan buat CPU
    else:
        args.append("--disable-dev-shm-usage")
    ctx = p.chromium.launch_persistent_context(
        prof, headless=False, args=args, ignore_default_args=ignore or None,
        viewport={"width": w, "height": h}, device_scale_factor=dpr,
        is_mobile=TOUCH, has_touch=TOUCH, user_agent=UA.get(DEVICE), env={**os.environ, "DISPLAY": DISP})
    ctx.add_init_script(HELPERS)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return {"xvfb": xvfb, "prof": prof, "ctx": ctx, "page": page, "ri": ri, "lite": 0, "fps": None}


def close_session(s):
    try:
        s["ctx"].close()
    except Exception:
        pass
    try:
        s["xvfb"].terminate()
    except Exception:
        pass
    shutil.rmtree(s["prof"], ignore_errors=True)


def page_goto(page):
    try:
        page.goto(URL, wait_until="load", timeout=45000)
    except Exception:
        page.goto(URL, wait_until="domcontentloaded", timeout=25000)
    try:
        page.wait_for_load_state("networkidle", timeout=2500)
    except Exception:
        pass


def measure(page):
    pause(page, 0.5)
    v = ev(page, MEASURE_JS, 2600, None)
    return float(v) if v else None


def prepare(p, cur, ri, lv):
    """Pastikan sesi sesuai (ukuran ri, tingkat ringan lv); buka ulang browser kalau ukurannya beda. Hasil: (sesi, fps)."""
    if cur is not None and (cur["ri"] != ri or lv < cur["lite"]):  # mode ringan tidak bisa dibatalkan di sesi yang sama
        close_session(cur)
        cur = None
    if cur is None:
        SESS_N[0] += 1
        cur = open_session(p, ri, SESS_N[0])
        page_goto(cur["page"])
        pause(cur["page"], 0.8)
    if lv != cur["lite"]:
        ev(cur["page"], "l => window.__rec.lite(l)", lv)
        cur["lite"] = lv
        pause(cur["page"], 0.6)
    cur["fps"] = measure(cur["page"])
    dbg("preflight", RES[ri], "ringan", lv, "fps", cur["fps"])
    return cur, cur["fps"]


def choose_session(p):
    """Pilih ukuran + mode terbaik yang bisa dirender cukup halus di mesin ini. Browser yang terpilih dibiarkan terbuka."""
    if HD_FULL:
        return open_session(p, 0, 0)
    plan = [(0, 0), (0, 1), (0, 2), (1, 2), (2, 2)]  # (indeks ukuran, tingkat ringan)
    cur, i, res = None, 0, []
    while i < len(plan):
        ri, lv = plan[i]
        cur, fps = prepare(p, cur, ri, lv)
        res.append((ri, lv, fps or 0.0))
        if fps is None or fps >= TARGET_FPS:
            return cur
        i += 2 if (i == 0 and fps < 5) else 1  # sangat berat: lompati tingkat ringan-1
    ok = [r for r in res if r[2] >= MIN_FPS]  # tak ada yang ideal: ambil yang paling tajam yang masih cukup halus
    if ok:
        cur, _ = prepare(p, cur, ok[0][0], ok[0][1])
    return cur


# ---------- alur utama ----------

def end_dwell(page):
    """Diam sebentar di bagian akhir supaya teks penutup sempat terbaca dan animasinya selesai."""
    n = ev(page, "window.__rec.visText()", None, 0) or 0
    pause(page, min(8.0, max(3.0, n / 25.0)) * (0.85 if PKEY == "f" else 1.0))
    for _ in range(4):  # teks yang masih mengetik / animasi akhir
        a = text_len(page)
        pause(page, 0.9)
        if text_len(page) == a:
            break
    pause(page, 0.6)


def scroll_step(page):
    a = pos_of(page)
    ev(page, "f => window.__rec.scrollBy(f)", 0.7)
    last, stable = a["top"], 0
    for _ in range(16):  # tunggu scroll halus selesai
        pause(page, 0.12)
        b = pos_of(page)
        stable = stable + 1 if abs(b["top"] - last) < 1 else 0
        last = b["top"]
        if stable >= 2:
            break
    moved = last > a["top"] + 2
    if not moved:  # situs yang membajak scroll: coba roda mouse
        before = f"{last}|{text_len(page)}|{overlay_sig(page)}"
        vp = page.viewport_size or {"width": 360, "height": 800}
        try:
            page.mouse.move(vp["width"] / 2, vp["height"] / 2)
            page.mouse.wheel(0, vp["height"] * 0.7)
        except Exception:
            pass
        pause(page, 0.8)
        moved = f"{pos_of(page)['top']}|{text_len(page)}|{overlay_sig(page)}" != before
    wait(page, 450)
    return moved


def run_page(page, ctx, host, st):
    page_goto(page)
    LOAD["t"] = time.time()
    wait(page, 1500)
    t0 = time.time()
    seen, bad = set(), set()
    quiet = low = errs = regrow = 0
    screen_clicks = 0
    last_probe = time.time()
    lv = SESS["lite"]

    def left():
        return BUDGET - (time.time() - t0)

    while True:
        if left() <= 0:
            p0 = pos_of(page)
            if p0["max"] <= 4 or p0["top"] >= p0["max"] - 4:
                end_dwell(page)
            return "batas waktu"
        if time.time() - t0 > HARD_CAP:
            return "batas aman"
        try:
            if END_TEXT and end_visible(page):
                pause(page, 2.5)
                return "teks penanda"
            close_popups(ctx, page)
            pos = pos_of(page)
            at_bottom = pos["max"] <= 4 or pos["top"] >= pos["max"] - 4
            # pantau kelancaran; kalau patah-patah, naikkan mode ringan saat itu juga
            if not HD_FULL and time.time() - last_probe > 8 and lv < 2:
                last_probe = time.time()
                f = ev(page, "ms => window.__rec.fps(ms)", 900, None)
                low = low + 1 if (f is not None and f < 14) else 0
                if low >= 2:
                    lv += 1
                    low = 0
                    ev(page, "l => window.__rec.lite(l)", lv)
                    st["lite_up"] = st.get("lite_up", 0) + 1
                    dbg("naik mode ringan ->", lv, "fps", f)
            hurry = (not at_bottom) and left() < 24
            screens = 0 if pos["max"] <= 4 else math.ceil((pos["max"] - pos["top"]) / (0.7 * max(1, pos["vh"])))
            allow = 99 if pos["max"] <= 4 else max(2, int(((left() - 8) / (screens + 1)) / 3.2))
            target = None
            if TAP and not hurry and st["clicks"] < MAX_CLICKS and screen_clicks < allow:
                target = pick_target(page, seen, bad, host)
            if target:
                quiet = 0
                seen.add(target["sig"])
                SEEN_BASE[target["base"]] = SEEN_BASE.get(target["base"], 0) + 1
                st["clicks"] += 1
                screen_clicks += 1
                before, ov_before, url_before = text_len(page), overlay_sig(page), page.url
                ev(page, "window.__rec.guardBegin()")
                dbg("klik", target["tag"], repr(target["text"][:25]), round(target["x"]), round(target["y"]))
                try:
                    tap_at(page, target["x"], target["y"])
                except Exception:
                    continue
                wait(page, 900)
                dmg = ev(page, "window.__rec.guardEnd()", None, "")
                if dmg:  # klik tadi mematikan suara: dipulihkan dan tidak ditekan lagi
                    bad.add(target["sig"])
                    st["damage"] = st.get("damage", 0) + 1
                    dbg("suara dipulihkan:", dmg)
                new = abs(text_len(page) - before)
                pause(page, min(new, 300) / 60 * PACE)  # waktu baca konten baru
                close_popups(ctx, page)
                if not same_page(page.url, url_before) and st["navs"] < 6:
                    st["navs"] += 1
                    pause(page, 1.2 * PACE)
                    try:
                        page.go_back(timeout=8000)
                    except Exception:
                        page_goto(page)
                    wait(page, 800)
                    continue
                ov_now = overlay_sig(page)
                if ov_now and ov_now != ov_before:
                    close_overlay(page, ov_before)
                wait(page, 400)
                continue
            # tidak ada yang perlu ditekan di layar ini
            if at_bottom:
                if regrow < 3:  # halaman kadang bertambah panjang setelah muncul konten baru
                    pause(page, 0.8)
                    p2 = pos_of(page)
                    if p2["max"] > pos["max"] + 40:
                        regrow += 1
                        continue
                if pos["max"] <= 4 and quiet < 6:  # layar tunggal: coba gulir lewat roda mouse (situs slide)
                    if scroll_step(page):
                        quiet = 0
                        screen_clicks = 0
                        continue
                    quiet += 3
                end_dwell(page)
                return "sampai ujung halaman" if pos["max"] > 4 else "semua sudah ditekan"
            moved = scroll_step(page)
            st["scrolls"] = st.get("scrolls", 0) + 1
            if moved:
                quiet, screen_clicks = 0, 0
            else:
                quiet += 1
                if quiet >= 3:
                    end_dwell(page)
                    return "tidak bisa scroll lagi"
            errs = 0
        except Exception as e:
            errs += 1
            dbg("error loop:", type(e).__name__, str(e)[:120])
            if errs >= 6:
                raise
            pause(page, 0.5)


def main():
    os.makedirs(OUT, exist_ok=True)
    host = urlparse(URL).netloc
    with_audio = AUDIO and start_audio()
    raw = os.path.join(OUT, "raw.mkv")
    rec, err, reason, t_rec = None, None, "-", time.time()
    st = {"clicks": 0, "navs": 0}
    sess = None
    os.environ["DISPLAY"] = DISP  # harus ada sebelum Playwright dijalankan, kalau tidak browser tidak menemukan layar
    try:
        with sync_playwright() as p:
            try:
                sess = choose_session(p)
                SESS.update(sess)
                page, ctx = sess["page"], sess["ctx"]
                w, h, dpr = RES[sess["ri"]]
                # hapus jejak uji kelancaran supaya rekaman mulai dari awal (intro dll tidak terlewat)
                try:
                    ctx.clear_cookies()
                    o = urlparse(URL)
                    ctx.new_cdp_session(page).send("Storage.clearDataForOrigin", {"origin": f"{o.scheme}://{o.netloc}", "storageTypes": "all"})
                except Exception:
                    pass
                area = find_area(page, w, h, dpr)
                page.goto("about:blank")
                if sess["lite"]:
                    ctx.add_init_script(f"window.__rec && window.__rec.lite({sess['lite']});")
                rec = start_recorder(raw, area, with_audio)
                t_rec = time.time()
                time.sleep(0.4)
                reason = run_page(page, ctx, host, st)
                pause(page, 0.8)
            except Exception as e:  # video tetap disimpan walau ada error
                err = e
            if rec:
                stop_recorder(rec)
            if sess:
                close_session(sess)
                sess = None
    finally:
        if rec and rec.poll() is None:
            rec.kill()
        if sess:
            close_session(sess)
        sh(["pkill", "-f", f"Xvfb {DISP}"])
        try:
            sh(["pulseaudio", "--kill"])
        except Exception:
            pass
    if not os.path.exists(raw):
        raise SystemExit("perekaman gagal: " + str(err))
    mp4, trim = os.path.join(OUT, "rec.mp4"), max(0.0, LOAD["t"] - t_rec - 0.3) if LOAD["t"] else 0.0
    for crf in (22, 28, 34, 38):
        encode(raw, mp4, crf, with_audio, trim)
        if os.path.getsize(mp4) <= 49 * 1024 * 1024:
            break
    suara = audio_status(mp4) if with_audio else "off"
    w, h, dpr = RES[SESS.get("ri", 0)]
    lite = SESS.get("lite", 0) + (st.get("lite_up", 0) if st.get("lite_up") else 0)
    mode = f"{w}x{h}" + ("" if dpr == 1 else f"@{dpr:g}x") + (" asli" if not min(2, lite) else f" ringan{min(2, lite)}")
    fps = SESS.get("fps")
    print(f"selesai: {mp4} {os.path.getsize(mp4)} bytes | durasi: {int(time.time() - t_rec - trim)}"
          f" | ukuran: {probe(mp4)} | suara: {suara} | alasan: {reason} | mode: {mode}"
          f" | render: {int(fps) if fps else '-'} | klik: {st.get('clicks', 0)} | error: {err}")


if __name__ == "__main__":
    main()
