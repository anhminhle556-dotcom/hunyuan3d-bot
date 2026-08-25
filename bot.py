import asyncio
import json
import logging
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Page
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("hunyuan-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.environ.get("OWNER_ID", "").strip()
OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW.isdigit() else 0
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PROFILE_DIR = DATA_DIR / "chrome-profile"
JOBS_DIR = DATA_DIR / "jobs"
DOWNLOADS_DIR = DATA_DIR / "downloads"
HUNYUAN_URL = os.environ.get("HUNYUAN_URL", "https://3d.hunyuan.tencent.com/")
GENERATION_TIMEOUT_MIN = int(os.environ.get("GENERATION_TIMEOUT_MIN", "25"))
MANUAL_DEFAULT = os.environ.get("MANUAL_DEFAULT", "1").strip().lower() not in ("0", "false", "no", "off")
TRAIN_DIR = DATA_DIR / "training"
TRAIN_LOG = TRAIN_DIR / "actions.jsonl"
MARKS_DIR = TRAIN_DIR / "marks"
SESSION_DIR = TRAIN_DIR / "number-session"
SESSION_SHOTS_DIR = SESSION_DIR / "screens"
SESSION_TARGETS = SESSION_DIR / "latest-targets.json"
SESSION_META = SESSION_DIR / "session-meta.json"

for p in (PROFILE_DIR, JOBS_DIR, DOWNLOADS_DIR, TRAIN_DIR, MARKS_DIR, SESSION_DIR, SESSION_SHOTS_DIR):
    p.mkdir(parents=True, exist_ok=True)


def owner_only(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        if OWNER_ID and uid != OWNER_ID:
            if update.effective_message:
                await update.effective_message.reply_text("⛔ هذا البوت خاص بصاحبه.")
            return
        return await func(update, context)
    return wrapped


def novnc_url() -> str:
    explicit = os.environ.get("NOVNC_URL", "").strip()
    if explicit:
        return explicit
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if domain:
        return f"https://{domain}/vnc.html?autoconnect=true&resize=scale"
    return "رابط Railway العام + /vnc.html?autoconnect=true&resize=scale"


class HunyuanBrowser:
    def __init__(self):
        self.pw = None
        self.context = None
        self.page: Optional[Page] = None
        self.lock = asyncio.Lock()
        self.control_lock = asyncio.Lock()

    async def start(self):
        self.pw = await async_playwright().start()
        self.context = await self.pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1365, "height": 768},
            accept_downloads=True,
            downloads_path=str(DOWNLOADS_DIR),
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--start-maximized",
                "--restore-last-session",
            ],
        )
        pages = self.context.pages
        restored = [p for p in pages if p.url not in ("", "about:blank")]
        self.page = restored[0] if restored else (pages[0] if pages else await self.context.new_page())
        # With the persistent /data profile, prefer Chrome's restored tab. Only
        # navigate to the root when there really is no restored page at all.
        if self.page.url in ("", "about:blank"):
            try:
                await self.page.goto(HUNYUAN_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log.warning("Initial navigation failed: %s", e)

    async def stop(self):
        try:
            if self.context:
                await self.context.close()
        finally:
            if self.pw:
                await self.pw.stop()

    async def screenshot(self, path: Path):
        if not self.page:
            raise RuntimeError("Browser not started")
        await self.page.screenshot(path=str(path), full_page=False)

    async def open_home(self):
        if not self.page:
            raise RuntimeError("Browser not started")
        await self.page.goto(HUNYUAN_URL, wait_until="domcontentloaded", timeout=60000)
        await self.page.wait_for_timeout(2000)

    async def _click_text(self, pattern: str, timeout: int = 3500) -> bool:
        assert self.page
        candidates = [
            self.page.get_by_role("button", name=re.compile(pattern, re.I)),
            self.page.get_by_role("link", name=re.compile(pattern, re.I)),
            self.page.get_by_text(re.compile(pattern, re.I)),
        ]
        for loc in candidates:
            try:
                first = loc.first
                if await first.count() and await first.is_visible(timeout=500):
                    await first.click(timeout=timeout)
                    await self.page.wait_for_timeout(1200)
                    return True
            except Exception:
                continue
        return False

    async def _creator_upload_ui_present(self) -> bool:
        """True only on the actual Image/Text-to-3D upload workspace."""
        assert self.page
        try:
            body = await self.page.locator("body").inner_text(timeout=2500)
        except Exception:
            body = ""

        # A visible upload label is the strongest signal.
        if re.search(r"上传图片|Upload\s*Image|Choose\s*Image|Select\s*Image", body, re.I):
            return True

        # Some builds hide the file input behind a custom control. Only accept
        # image-capable inputs; do not treat arbitrary file inputs as the creator.
        inputs = self.page.locator('input[type="file"]')
        try:
            for i in range(await inputs.count()):
                el = inputs.nth(i)
                accept = ((await el.get_attribute("accept")) or "").lower()
                if not accept or "image" in accept:
                    return True
        except Exception:
            pass
        return False

    async def page_mode(self) -> str:
        """Classify the current Hunyuan SPA without clicking anything."""
        assert self.page
        try:
            body = await self.page.locator("body").inner_text(timeout=2500)
        except Exception:
            body = ""

        # Important: the real creator workspace is the only place where we
        # accept an image upload control as a positive signal.
        if await self._creator_upload_ui_present():
            return "image3d"

        if re.search(r"欢迎来到腾讯混元3D|登录后开启3D创作之旅", body, re.I):
            return "login"

        # Main Hunyuan 3D landing page. NOTE: this page also contains a card
        # called "3D世界模型". Therefore the generic word 世界模型 MUST NOT be
        # used to classify this page as World Model. The combination below is
        # specific to the Image/Text-to-3D hero card shown on the landing page.
        if re.search(r"图/文生3D|图生3D|文生3D", body, re.I) and re.search(
            r"立即开始|开始创作|立即体验|Start\s*Now|Get\s*Started", body, re.I
        ):
            return "image3d_landing"

        # World Model has a distinctive top navigation. Require at least two of
        # those markers instead of matching the isolated 世界模型 card on home.
        world_markers = [
            r"世界生成",
            r"世界重建",
            r"360.?全景图",
            r"实时生世界",
        ]
        world_hits = sum(1 for pat in world_markers if re.search(pat, body, re.I))
        if world_hits >= 2:
            return "world"

        return "unknown"

    async def _enter_image3d_from_landing(self) -> bool:
        """Open the Image/Text-to-3D creator from the Hunyuan landing page."""
        assert self.page
        start_pattern = re.compile(r"立即开始|开始创作|立即体验|Start\s*Now|Get\s*Started", re.I)

        # Prefer a button close to the 图/文生3D card. This avoids clicking a
        # different product's start button if the home page has several cards.
        labels = self.page.get_by_text(re.compile(r"图/文生3D|图生3D|文生3D", re.I))
        buttons = self.page.get_by_role("button", name=start_pattern)
        try:
            label_boxes = []
            for i in range(min(await labels.count(), 6)):
                loc = labels.nth(i)
                if await loc.is_visible(timeout=300):
                    box = await loc.bounding_box()
                    if box:
                        label_boxes.append(box)

            candidates = []
            for i in range(min(await buttons.count(), 12)):
                btn = buttons.nth(i)
                if not await btn.is_visible(timeout=300) or not await btn.is_enabled(timeout=300):
                    continue
                box = await btn.bounding_box()
                if not box:
                    continue
                if label_boxes:
                    bx = box["x"] + box["width"] / 2
                    by = box["y"] + box["height"] / 2
                    dist = min(
                        (bx - (lb["x"] + lb["width"] / 2)) ** 2
                        + (by - (lb["y"] + lb["height"] / 2)) ** 2
                        for lb in label_boxes
                    )
                else:
                    dist = i
                candidates.append((dist, i, btn))

            candidates.sort(key=lambda x: (x[0], x[1]))
            for _, _, btn in candidates:
                try:
                    before_pages = set(self.context.pages) if self.context else set()
                    await btn.click(timeout=5000)
                    # Some Hunyuan builds navigate in-place; others can open a
                    # new tab. If a new page appears, follow it explicitly.
                    await self.page.wait_for_timeout(800)
                    if self.context:
                        new_pages = [p for p in self.context.pages if p not in before_pages]
                        if new_pages:
                            self.page = new_pages[-1]
                            try:
                                await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                            except Exception:
                                pass
                    # The SPA can take a moment to mount the upload widget.
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        await self.page.wait_for_timeout(500)
                        if await self._creator_upload_ui_present():
                            return True
                    # If this button did not open the creator, do not click other
                    # unrelated cards blindly after navigation/state changes.
                    return False
                except Exception:
                    continue
        except Exception:
            pass

        # Fallback only when there is a single explicit start button on screen.
        try:
            texts = self.page.get_by_text(start_pattern)
            visible = []
            for i in range(min(await texts.count(), 12)):
                loc = texts.nth(i)
                if await loc.is_visible(timeout=300):
                    visible.append(loc)
            if len(visible) == 1:
                await visible[0].click(timeout=5000)
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    await self.page.wait_for_timeout(500)
                    if await self._creator_upload_ui_present():
                        return True
        except Exception:
            pass
        return False

    async def prepare_image_to_3d(self):
        assert self.page
        mode = await self.page_mode()
        if mode == "image3d":
            return

        # The main Hunyuan landing page is safe, but it is not the upload page.
        # Enter the explicit 图/文生3D creator before touching any file input.
        if mode == "image3d_landing":
            if await self._enter_image3d_from_landing():
                return
            raise RuntimeError(
                "وصلت لواجهة Hunyuan الرئيسية لكن ما قدرت أفتح صفحة رفع 图/文生3D تلقائياً. "
                "أوقفت الطلب قبل صرف أي نقطة."
            )

        # The previous build navigated to the root URL immediately before every
        # job. The root can open Hunyuan World Model. If that just happened,
        # browser history normally contains the correct Image-to-3D page, so try
        # Back only; this is navigation-only and cannot spend generation credit.
        if mode == "world":
            for _ in range(2):
                try:
                    await self.page.go_back(wait_until="domcontentloaded", timeout=12000)
                    await self.page.wait_for_timeout(1200)
                except Exception:
                    break
                if await self.page_mode() == "image3d":
                    return

            raise RuntimeError(
                "المتصفح موجود في صفحة Hunyuan World Model (世界模型)، مو صفحة 图/文生3D. "
                "جلسة الدخول شغالة؛ افتح واجهة noVNC وارجع إلى صفحة 图/文生3D ثم أرسل الصورة."
            )

        if mode == "login":
            raise RuntimeError("جلسة الدخول غير مفعلة؛ افتح /login وسجّل الدخول أولاً.")

        # On an unknown page, only click explicit Image-to-3D labels. Do not use
        # broad words such as 'Creation/创作' because they also exist on World Model.
        clicked = await self._click_text(r"Image\s*(?:to|[-→])\s*3D|Image.*3D|图生\s*3D|图片.*3D|图/文生3D")
        if clicked:
            await self.page.wait_for_timeout(1600)
            if await self.page_mode() == "image3d":
                return

        raise RuntimeError(
            "ما قدرت أوصل بأمان إلى صفحة 图/文生3D. افتح /login، خلّ المتصفح على صفحة رفع الصورة، وبعدها أرسل الصورة."
        )

    async def _attached_image_count(self) -> int:
        """Return how many local files are currently attached to file inputs."""
        assert self.page
        total = 0
        inputs = self.page.locator('input[type="file"]')
        for i in range(await inputs.count()):
            try:
                n = await inputs.nth(i).evaluate("el => el.files ? el.files.length : 0")
                total += int(n or 0)
            except Exception:
                pass
        return total

    async def upload_image(self, image_path: Path):
        assert self.page
        await self.prepare_image_to_3d()

        # Current Hunyuan UI exposes a visible Chinese "上传图片" control.
        # Prefer the real file-chooser event so we do not accidentally fill an
        # unrelated hidden input elsewhere in the SPA.
        upload_pattern = re.compile(r"上传图片|Upload\s*Image|Choose\s*Image|Select\s*Image", re.I)
        upload_targets = [
            self.page.get_by_role("button", name=upload_pattern),
            self.page.get_by_text(upload_pattern),
        ]
        chooser_used = False
        for loc in upload_targets:
            try:
                target = loc.first
                if await target.count() and await target.is_visible(timeout=500):
                    try:
                        async with self.page.expect_file_chooser(timeout=5000) as info:
                            await target.click(timeout=5000)
                        chooser = await info.value
                        await chooser.set_files(str(image_path))
                        chooser_used = True
                        break
                    except Exception:
                        # Some builds wrap the label around a hidden file input;
                        # fall through to the direct-input fallback below.
                        pass
            except Exception:
                continue

        if not chooser_used:
            inputs = self.page.locator('input[type="file"]')
            count = await inputs.count()
            if count == 0:
                raise RuntimeError("لم أجد مربع رفع الصورة في صفحة Hunyuan الحالية.")

            candidates = []
            for i in range(count):
                el = inputs.nth(i)
                try:
                    accept = ((await el.get_attribute("accept")) or "").lower()
                    # Put image-specific inputs first. Keep empty accept as fallback.
                    score = 0 if "image" in accept else (1 if accept == "" else 2)
                    candidates.append((score, i, el))
                except Exception:
                    candidates.append((3, i, el))
            candidates.sort(key=lambda x: (x[0], x[1]))

            attached = False
            for _, _, el in candidates:
                try:
                    await el.set_input_files(str(image_path))
                    await self.page.wait_for_timeout(700)
                    n = await el.evaluate("node => node.files ? node.files.length : 0")
                    if int(n or 0) > 0:
                        attached = True
                        break
                except Exception:
                    continue
            if not attached:
                raise RuntimeError("وجدت حقل الرفع لكن Hunyuan لم يقبل الصورة داخله.")

        # Do not pretend generation started unless the browser really has a file.
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if await self._attached_image_count() > 0:
                await self.page.wait_for_timeout(1800)
                return
            await self.page.wait_for_timeout(400)
        raise RuntimeError("تم اختيار الصورة لكن واجهة Hunyuan لم تثبت تحميلها؛ أوقفت الطلب قبل صرف أي نقطة.")

    async def click_generate(self) -> bool:
        assert self.page
        pattern = re.compile(
            r"Generate\s*Immediately|Generate|Create\s*3D|生成\s*3D|立即生成|开始生成|生成",
            re.I,
        )
        # Prefer actual buttons and only click an enabled visible control.
        buttons = self.page.get_by_role("button", name=pattern)
        for i in range(await buttons.count()):
            btn = buttons.nth(i)
            try:
                if await btn.is_visible(timeout=300) and await btn.is_enabled(timeout=300):
                    await btn.click(timeout=5000)
                    await self.page.wait_for_timeout(1200)
                    return True
            except Exception:
                continue

        # Fallback for non-semantic clickable elements.
        texts = self.page.get_by_text(pattern)
        for i in range(min(await texts.count(), 8)):
            el = texts.nth(i)
            try:
                if not await el.is_visible(timeout=300):
                    continue
                disabled = await el.get_attribute("aria-disabled")
                if disabled == "true":
                    continue
                await el.click(timeout=5000)
                await self.page.wait_for_timeout(1200)
                return True
            except Exception:
                continue
        return False

    async def _download_button_visible(self) -> bool:
        assert self.page
        pattern = re.compile(r"Download|Export|下载|导出", re.I)
        for loc in (
            self.page.get_by_role("button", name=pattern),
            self.page.get_by_role("link", name=pattern),
            self.page.get_by_text(pattern),
        ):
            try:
                if await loc.count() and await loc.first.is_visible(timeout=300):
                    return True
            except Exception:
                pass
        return False

    async def wait_until_ready(self, progress_cb=None):
        assert self.page
        deadline = time.monotonic() + GENERATION_TIMEOUT_MIN * 60
        last_notice = 0
        error_pattern = re.compile(
            r"generation failed|failed to generate|error|生成失败|任务失败|失败|insufficient|余额不足|credits?不足",
            re.I,
        )
        login_pattern = re.compile(r"login|log\s*in|sign\s*in|登录|登入", re.I)

        while time.monotonic() < deadline:
            if await self._download_button_visible():
                return

            try:
                body = await self.page.locator("body").inner_text(timeout=3000)
            except Exception:
                body = ""

            if error_pattern.search(body):
                raise RuntimeError("الموقع أظهر رسالة فشل أثناء التوليد.")

            # Avoid false positives from small Login links; only treat it as expiry if URL also hints auth.
            if login_pattern.search(body) and any(x in self.page.url.lower() for x in ("login", "signin", "auth")):
                raise RuntimeError("جلسة تسجيل الدخول منتهية وتحتاج تسجيل دخول من جديد.")

            now = time.monotonic()
            if progress_cb and now - last_notice >= 25:
                await progress_cb()
                last_notice = now
            await self.page.wait_for_timeout(5000)

        raise RuntimeError(f"انتهت مهلة الانتظار ({GENERATION_TIMEOUT_MIN} دقيقة) بدون ظهور زر التنزيل.")

    async def _click_and_capture_download(self, locator, timeout_ms=9000):
        assert self.page
        try:
            async with self.page.expect_download(timeout=timeout_ms) as info:
                await locator.click(timeout=5000)
            return await info.value
        except PlaywrightTimeoutError:
            return None
        except Exception:
            return None

    async def download_result(self) -> Path:
        assert self.page
        download_pattern = re.compile(r"Download|Export|下载|导出", re.I)
        download_locators = [
            self.page.get_by_role("button", name=download_pattern),
            self.page.get_by_role("link", name=download_pattern),
            self.page.get_by_text(download_pattern),
        ]

        for loc in download_locators:
            try:
                if not await loc.count() or not await loc.first.is_visible(timeout=500):
                    continue
                dl = await self._click_and_capture_download(loc.first)
                if dl:
                    return await self._save_download(dl)
                await self.page.wait_for_timeout(900)
                break
            except Exception:
                continue

        # Many sites open a format menu after pressing Download. Prefer GLB.
        format_patterns = [r"^GLB$", r"GLB", r"glTF", r"OBJ", r"FBX"]
        for pat in format_patterns:
            regex = re.compile(pat, re.I)
            for loc in (
                self.page.get_by_role("button", name=regex),
                self.page.get_by_role("menuitem", name=regex),
                self.page.get_by_text(regex),
            ):
                try:
                    if await loc.count() and await loc.first.is_visible(timeout=500):
                        dl = await self._click_and_capture_download(loc.first, timeout_ms=12000)
                        if dl:
                            return await self._save_download(dl)
                except Exception:
                    continue

        # Last chance: downloadable anchors.
        anchors = self.page.locator('a[download]')
        if await anchors.count():
            dl = await self._click_and_capture_download(anchors.first, timeout_ms=12000)
            if dl:
                return await self._save_download(dl)

        raise RuntimeError("ظهر أن التوليد اكتمل لكن لم أستطع التقاط ملف التنزيل تلقائياً.")

    async def _save_download(self, download) -> Path:
        suggested = download.suggested_filename or f"hunyuan-{int(time.time())}.glb"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", suggested)
        target = DOWNLOADS_DIR / f"{int(time.time())}-{safe}"
        await download.save_as(str(target))
        return target

    async def _viewport(self):
        assert self.page
        try:
            return await self.page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
        except Exception:
            return {"w": 1365, "h": 768}

    async def _element_at(self, x: int, y: int):
        assert self.page
        script = r"""
        ({x, y}) => {
          const el = document.elementFromPoint(x, y);
          if (!el) return null;
          const r = el.getBoundingClientRect();
          const txt = (el.innerText || el.textContent || '').replace(/\s+/g,' ').trim().slice(0,300);
          const attrs = {};
          for (const name of ['id','class','role','aria-label','name','type','href','title','placeholder','data-testid','data-test','data-cy']) {
            const v = el.getAttribute && el.getAttribute(name);
            if (v) attrs[name] = v.slice(0,300);
          }
          function cssPath(node) {
            if (!node || node.nodeType !== 1) return '';
            if (node.id) return '#' + CSS.escape(node.id);
            const parts = [];
            let cur = node;
            for (let depth=0; cur && cur.nodeType===1 && depth<6; depth++, cur=cur.parentElement) {
              let part = cur.tagName.toLowerCase();
              const cls = [...cur.classList].filter(Boolean).slice(0,2);
              if (cls.length) part += '.' + cls.map(c => CSS.escape(c)).join('.');
              if (cur.parentElement) {
                const same = [...cur.parentElement.children].filter(n => n.tagName === cur.tagName);
                if (same.length > 1) part += `:nth-of-type(${same.indexOf(cur)+1})`;
              }
              parts.unshift(part);
              if (cur.id) break;
            }
            return parts.join(' > ');
          }
          return {
            tag: el.tagName.toLowerCase(),
            text: txt,
            attrs,
            selector: cssPath(el),
            bbox: {x:r.x, y:r.y, width:r.width, height:r.height},
            outerHTML: (el.outerHTML || '').slice(0,1000)
          };
        }
        """
        try:
            return await self.page.evaluate(script, {"x": int(x), "y": int(y)})
        except Exception as e:
            return {"error": str(e)}

    def _append_training(self, record: dict):
        record = dict(record)
        record.setdefault("ts", time.time())
        TRAIN_DIR.mkdir(parents=True, exist_ok=True)
        with TRAIN_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def log_training_action(self, action: str, x: int = None, y: int = None, extra: dict = None, before=None):
        url = self.page.url if self.page else ""
        title = ""
        try:
            title = await self.page.title() if self.page else ""
        except Exception:
            pass
        vp = await self._viewport() if self.page else {"w": 0, "h": 0}
        rec = {"action": action, "x": x, "y": y, "url": url, "title": title, "viewport": vp}
        if before is not None:
            rec["element_before"] = before
        if extra:
            rec["extra"] = extra
        self._append_training(rec)

    async def screenshot_with_cursor(self, path: Path, x: int, y: int):
        assert self.page
        overlay_id = "__chatgpt_mouse_cursor__"
        try:
            await self.page.evaluate(
                r"""({id,x,y}) => {
                    const old = document.getElementById(id); if (old) old.remove();
                    const d = document.createElement('div');
                    d.id = id;
                    d.style.cssText = `position:fixed;left:${x-16}px;top:${y-16}px;width:32px;height:32px;border:3px solid #ff2d2d;border-radius:50%;z-index:2147483647;pointer-events:none;box-sizing:border-box;box-shadow:0 0 0 2px white;`;
                    const h = document.createElement('div');
                    h.style.cssText = 'position:absolute;left:13px;top:-8px;width:2px;height:48px;background:#ff2d2d;';
                    const v = document.createElement('div');
                    v.style.cssText = 'position:absolute;left:-8px;top:13px;width:48px;height:2px;background:#ff2d2d;';
                    d.appendChild(h); d.appendChild(v); document.documentElement.appendChild(d);
                }""",
                {"id": overlay_id, "x": int(x), "y": int(y)},
            )
        except Exception:
            pass
        try:
            await self.page.screenshot(path=str(path), full_page=False)
        finally:
            try:
                await self.page.evaluate("id => document.getElementById(id)?.remove()", overlay_id)
            except Exception:
                pass

    async def numbered_targets(self, kind: str = "smart", max_items: int = 90):
        """Return numbered click targets for the current viewport.

        smart: visible interactive DOM elements, best for tiny buttons.
        grid: fixed 10x6 screen cells as a fallback for canvas/non-semantic UI.
        """
        assert self.page
        vp = await self._viewport()
        if kind == "grid":
            cols, rows = 10, 6
            items = []
            cw, ch = vp["w"] / cols, vp["h"] / rows
            n = 1
            for r in range(rows):
                for c in range(cols):
                    x0, y0 = c * cw, r * ch
                    items.append({
                        "n": n,
                        "kind": "grid",
                        "x": int(x0 + cw / 2),
                        "y": int(y0 + ch / 2),
                        "bbox": {"x": x0, "y": y0, "width": cw, "height": ch},
                        "tag": "grid-cell",
                        "text": f"screen-cell-{n}",
                        "attrs": {},
                        "selector": "",
                    })
                    n += 1
            return items

        script = r"""
        ({maxItems}) => {
          const vw = window.innerWidth, vh = window.innerHeight;
          const selectors = 'button,a,input,textarea,select,[role="button"],[role="link"],[role="menuitem"],[role="tab"],[onclick],[tabindex]';
          const base = [...document.querySelectorAll(selectors)];
          // Hunyuan uses clickable divs in several places. Add pointer-cursor nodes too.
          for (const el of document.querySelectorAll('div,span,label,img,svg')) {
            try { if (getComputedStyle(el).cursor === 'pointer') base.push(el); } catch (_) {}
          }
          const uniq = [...new Set(base)];
          const raw = [];
          function cssPath(node) {
            if (!node || node.nodeType !== 1) return '';
            if (node.id) return '#' + CSS.escape(node.id);
            const parts = [];
            let cur = node;
            for (let depth=0; cur && cur.nodeType===1 && depth<6; depth++,cur=cur.parentElement) {
              let part = cur.tagName.toLowerCase();
              const cls = [...cur.classList].filter(Boolean).slice(0,2);
              if (cls.length) part += '.' + cls.map(c=>CSS.escape(c)).join('.');
              if (cur.parentElement) {
                const same = [...cur.parentElement.children].filter(n=>n.tagName===cur.tagName);
                if (same.length>1) part += `:nth-of-type(${same.indexOf(cur)+1})`;
              }
              parts.unshift(part);
              if (cur.id) break;
            }
            return parts.join(' > ');
          }
          function score(el, r) {
            const tag = el.tagName.toLowerCase();
            let s = 50;
            if (tag === 'button') s -= 20;
            if (tag === 'input') s -= 15;
            if ((el.getAttribute('role')||'').match(/button|tab|menuitem|link/i)) s -= 15;
            if ((el.innerText||el.textContent||'').trim()) s -= 5;
            // Prefer smaller concrete controls over giant clickable cards.
            s += Math.min(30, (r.width*r.height)/(vw*vh)*100);
            return s;
          }
          for (const el of uniq) {
            let r; try { r = el.getBoundingClientRect(); } catch (_) { continue; }
            if (!r || r.width < 8 || r.height < 8) continue;
            if (r.bottom <= 0 || r.right <= 0 || r.left >= vw || r.top >= vh) continue;
            let st; try { st = getComputedStyle(el); } catch (_) { continue; }
            if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity||1) < 0.05) continue;
            if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
            const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '').replace(/\s+/g,' ').trim().slice(0,160);
            const attrs = {};
            for (const name of ['id','class','role','aria-label','name','type','href','title','placeholder','data-testid','data-test','data-cy']) {
              const v = el.getAttribute && el.getAttribute(name); if (v) attrs[name] = String(v).slice(0,240);
            }
            raw.push({
              score: score(el,r),
              x: Math.round(Math.max(0,Math.min(vw-1,r.left+r.width/2))),
              y: Math.round(Math.max(0,Math.min(vh-1,r.top+r.height/2))),
              bbox: {x:r.x,y:r.y,width:r.width,height:r.height},
              tag: el.tagName.toLowerCase(), text, attrs, selector: cssPath(el)
            });
          }
          // Remove near-duplicate nested targets that point to the same place/box.
          raw.sort((a,b)=>a.score-b.score || a.bbox.y-b.bbox.y || a.bbox.x-b.bbox.x);
          const kept = [];
          for (const it of raw) {
            const dup = kept.some(k => Math.abs(k.x-it.x)<5 && Math.abs(k.y-it.y)<5 &&
              Math.abs(k.bbox.width-it.bbox.width)<8 && Math.abs(k.bbox.height-it.bbox.height)<8);
            if (!dup) kept.push(it);
            if (kept.length >= maxItems) break;
          }
          kept.sort((a,b)=>a.bbox.y-b.bbox.y || a.bbox.x-b.bbox.x);
          return kept.map((it,i)=>({...it,n:i+1,kind:'smart'}));
        }
        """
        try:
            return await self.page.evaluate(script, {"maxItems": int(max_items)})
        except Exception:
            return []

    async def screenshot_numbered(self, path: Path, targets: list, kind: str = "smart"):
        assert self.page
        overlay_id = "__chatgpt_number_grid__"
        try:
            await self.page.evaluate(
                r"""({id, targets, kind}) => {
                  document.getElementById(id)?.remove();
                  const root = document.createElement('div');
                  root.id = id;
                  root.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none;font-family:Arial,sans-serif;';
                  for (const t of targets) {
                    const b = t.bbox;
                    if (!b) continue;
                    if (kind === 'grid') {
                      const box = document.createElement('div');
                      box.style.cssText = `position:absolute;left:${b.x}px;top:${b.y}px;width:${b.width}px;height:${b.height}px;border:1px solid rgba(255,220,0,.7);box-sizing:border-box;`;
                      root.appendChild(box);
                    } else {
                      const box = document.createElement('div');
                      box.style.cssText = `position:absolute;left:${b.x}px;top:${b.y}px;width:${b.width}px;height:${b.height}px;border:2px solid rgba(255,220,0,.92);border-radius:5px;box-sizing:border-box;`;
                      root.appendChild(box);
                    }
                    const badge = document.createElement('div');
                    const bx = Math.max(2, Math.min(window.innerWidth-42, b.x + Math.min(6,b.width/3)));
                    const by = Math.max(2, Math.min(window.innerHeight-28, b.y + Math.min(4,b.height/3)));
                    badge.textContent = String(t.n);
                    badge.style.cssText = `position:absolute;left:${bx}px;top:${by}px;min-width:24px;height:24px;padding:0 5px;background:#111;color:#fff;border:2px solid #ffe000;border-radius:7px;font:bold 15px/20px Arial;text-align:center;box-sizing:border-box;box-shadow:0 1px 5px #000;`;
                    root.appendChild(badge);
                  }
                  document.documentElement.appendChild(root);
                }""",
                {"id": overlay_id, "targets": targets, "kind": kind},
            )
        except Exception:
            pass
        try:
            await self.page.screenshot(path=str(path), full_page=False)
        finally:
            try:
                await self.page.evaluate("id => document.getElementById(id)?.remove()", overlay_id)
            except Exception:
                pass

    async def click_number_target(self, target: dict, image_path: Optional[Path] = None):
        """Click one numbered target and log enough detail to replay it later."""
        assert self.page
        async with self.control_lock:
            x, y = int(target["x"]), int(target["y"])
            before = await self._element_at(x, y)
            text_blob = " ".join([
                str(target.get("text") or ""),
                str((target.get("attrs") or {}).get("aria-label") or ""),
                str((target.get("attrs") or {}).get("title") or ""),
            ])
            before_pages = list(self.context.pages) if self.context else []
            used_file_chooser = False
            saved_download = None

            uploadish = bool(re.search(r"上传图片|upload|choose\s*image|select\s*image|添加图片", text_blob, re.I))
            downloadish = bool(re.search(r"download|export|下载|导出|\bGLB\b|\bOBJ\b|\bFBX\b", text_blob, re.I))

            if uploadish and image_path and image_path.exists():
                try:
                    async with self.page.expect_file_chooser(timeout=2500) as info:
                        await self.page.mouse.click(x, y, delay=70)
                    chooser = await info.value
                    await chooser.set_files(str(image_path))
                    used_file_chooser = True
                    await self.page.wait_for_timeout(900)
                except Exception:
                    # The click already happened. If Hunyuan uses a hidden file input
                    # without a chooser, fill that input directly; never click twice.
                    try:
                        await self.manual_upload_file({"x": x, "y": y}, image_path)
                        used_file_chooser = True
                    except Exception:
                        pass
            elif downloadish:
                try:
                    async with self.page.expect_download(timeout=3500) as info:
                        await self.page.mouse.click(x, y, delay=70)
                    dl = await info.value
                    saved_download = await self._save_download(dl)
                except Exception:
                    # The first click may have opened a format menu instead of starting
                    # a download. Do not click twice; refresh the numbered map instead.
                    pass
            else:
                await self.page.mouse.click(x, y, delay=70)

            await self.page.wait_for_timeout(650)
            if self.context:
                new_pages = [p for p in self.context.pages if p not in before_pages]
                if new_pages:
                    self.page = new_pages[-1]
                    try:
                        await self.page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass

            await self.log_training_action(
                "number_click",
                x, y,
                {
                    "number": int(target.get("n") or 0),
                    "target": target,
                    "used_file_chooser": used_file_chooser,
                    "download": str(saved_download) if saved_download else None,
                },
                before=before,
            )
            return {"element": before, "download": saved_download, "upload": used_file_chooser}

    async def manual_move(self, state: dict, dx: int, dy: int):
        vp = await self._viewport()
        state["x"] = max(0, min(int(vp["w"]) - 1, int(state.get("x", vp["w"]//2)) + int(dx)))
        state["y"] = max(0, min(int(vp["h"]) - 1, int(state.get("y", vp["h"]//2)) + int(dy)))
        await self.log_training_action("move", state["x"], state["y"], {"dx": dx, "dy": dy})

    async def manual_click(self, state: dict, count: int = 1):
        assert self.page
        x, y = int(state["x"]), int(state["y"])
        before = await self._element_at(x, y)
        before_pages = list(self.context.pages) if self.context else []
        await self.page.mouse.click(x, y, click_count=count, delay=80)
        await self.page.wait_for_timeout(650)
        if self.context:
            new_pages = [p for p in self.context.pages if p not in before_pages]
            if new_pages:
                self.page = new_pages[-1]
                try:
                    await self.page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
        await self.log_training_action("double_click" if count == 2 else "click", x, y, before=before)
        return before

    async def manual_scroll(self, state: dict, delta: int):
        assert self.page
        x, y = int(state["x"]), int(state["y"])
        before = await self._element_at(x, y)
        await self.page.mouse.move(x, y)
        await self.page.mouse.wheel(0, int(delta))
        await self.page.wait_for_timeout(500)
        await self.log_training_action("scroll", x, y, {"delta": delta}, before=before)

    async def manual_key(self, state: dict, key: str):
        assert self.page
        x, y = int(state["x"]), int(state["y"])
        before = await self._element_at(x, y)
        await self.page.keyboard.press(key)
        await self.page.wait_for_timeout(350)
        await self.log_training_action("key", x, y, {"key": key}, before=before)

    async def manual_type(self, state: dict, text: str):
        assert self.page
        x, y = int(state["x"]), int(state["y"])
        before = await self._element_at(x, y)
        await self.page.mouse.click(x, y)
        await self.page.keyboard.insert_text(text)
        await self.page.wait_for_timeout(300)
        await self.log_training_action("type", x, y, {"text": text[:500]}, before=before)

    async def manual_back(self, state: dict):
        assert self.page
        await self.log_training_action("back", int(state["x"]), int(state["y"]))
        try:
            await self.page.go_back(wait_until="domcontentloaded", timeout=12000)
        except Exception:
            pass
        await self.page.wait_for_timeout(500)

    async def manual_reload(self, state: dict):
        assert self.page
        await self.log_training_action("reload", int(state["x"]), int(state["y"]))
        try:
            await self.page.reload(wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
        await self.page.wait_for_timeout(500)

    async def manual_open_home(self, state: dict):
        assert self.page
        await self.log_training_action("open_home", int(state["x"]), int(state["y"]), {"target": HUNYUAN_URL})
        await self.page.goto(HUNYUAN_URL, wait_until="domcontentloaded", timeout=60000)
        await self.page.wait_for_timeout(800)

    async def manual_upload_file(self, state: dict, image_path: Path):
        assert self.page
        inputs = self.page.locator('input[type="file"]')
        count = await inputs.count()
        if count == 0:
            raise RuntimeError("ماكو input رفع ظاهر بالصفحة الحالية.")
        candidates = []
        for i in range(count):
            el = inputs.nth(i)
            try:
                accept = ((await el.get_attribute("accept")) or "").lower()
                score = 0 if "image" in accept else (1 if not accept else 2)
                candidates.append((score, i, el, accept))
            except Exception:
                candidates.append((3, i, el, ""))
        candidates.sort(key=lambda t: (t[0], t[1]))
        last_err = None
        for _, i, el, accept in candidates:
            try:
                meta = await el.evaluate(r"""el => ({tag:el.tagName.toLowerCase(), id:el.id||'', name:el.name||'', accept:el.accept||'', outerHTML:(el.outerHTML||'').slice(0,1000)})""")
                await el.set_input_files(str(image_path))
                await self.page.wait_for_timeout(1200)
                n = await el.evaluate("el => el.files ? el.files.length : 0")
                if int(n or 0) > 0:
                    await self.log_training_action("upload_file", int(state["x"]), int(state["y"]), {"input_index": i, "accept": accept, "input": meta, "file_name": image_path.name})
                    return meta
            except Exception as e:
                last_err = e
        raise RuntimeError(f"حاولت حقول الرفع بس ما قبلت الصورة: {last_err or 'unknown'}")

    async def mark_training(self, state: dict, name: str):
        assert self.page
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())[:80] or "mark"
        path = MARKS_DIR / f"{int(time.time())}-{safe}.png"
        await self.screenshot_with_cursor(path, int(state["x"]), int(state["y"]))
        elem = await self._element_at(int(state["x"]), int(state["y"]))
        await self.log_training_action("mark", int(state["x"]), int(state["y"]), {"name": name, "screenshot": str(path)}, before=elem)
        return path, elem

    async def generate(self, image_path: Path, progress_cb=None) -> Path:
        async with self.lock:
            # IMPORTANT: do not navigate to HUNYUAN_URL here. The root URL can
            # open Hunyuan World Model and destroy the user's already-correct
            # Image-to-3D page state. Work on the current tab instead.
            await self.upload_image(image_path)
            clicked = await self.click_generate()
            if not clicked:
                body = ""
                try:
                    body = await self.page.locator("body").inner_text(timeout=3000)
                except Exception:
                    pass
                if re.search(r"Login|Log in|Sign in|登录|登入", body, re.I) and any(
                    x in self.page.url.lower() for x in ("login", "signin", "auth")
                ):
                    raise RuntimeError("تحتاج تسجّل دخول من واجهة المتصفح أولاً.")
                raise RuntimeError("تم رفع الصورة لكن لم أجد زر التوليد المفعّل؛ أوقفت الطلب قبل الانتظار الوهمي.")
            await self.wait_until_ready(progress_cb=progress_cb)
            return await self.download_result()


async def get_browser(context: ContextTypes.DEFAULT_TYPE) -> HunyuanBrowser:
    return context.application.bot_data["hunyuan_browser"]


def _main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔢 شبكة الأرقام الذكية", callback_data="menu:numsmart")],
        [InlineKeyboardButton("🧩 شبكة الشاشة 1-60", callback_data="menu:numgrid"), InlineKeyboardButton("📸 لقطة الشاشة", callback_data="menu:shot")],
        [InlineKeyboardButton("🧪 وضع تدريب", callback_data="menu:manual"), InlineKeyboardButton("🤖 وضع تلقائي", callback_data="menu:auto")],
        [InlineKeyboardButton("📊 حالة البوت", callback_data="menu:status"), InlineKeyboardButton("🏠 فتح Hunyuan", callback_data="menu:open")],
        [InlineKeyboardButton("🔐 تسجيل الدخول", callback_data="menu:login"), InlineKeyboardButton("📦 إرسال جلسة التدريب", callback_data="menu:session")],
        [InlineKeyboardButton("📎 رفع آخر صورة", callback_data="menu:upload"), InlineKeyboardButton("⌨️ كتابة نص", callback_data="menu:type")],
        [InlineKeyboardButton("🧹 جلسة جديدة", callback_data="menu:clearlog")],
    ])


async def _send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None):
    msg = update.effective_message
    if not msg and update.callback_query:
        msg = update.callback_query.message
    if not msg:
        return
    await msg.reply_text(
        text or (
            "🤖 بوت Hunyuan 3D — وضع التدريب\n\n"
            "ما تحتاج تحرك موس ولا تحفظ أوامر.\n"
            "اضغط 🔢 شبكة الأرقام الذكية، راح أرقّم كل زر/عنصر قابل للضغط على الشاشة.\n"
            "بعدها اكتب رقم العنصر فقط وأنا أضغطه وأسجل الخطوة. للأماكن غير القابلة للكشف استخدم 🧩 شبكة الشاشة 1-60."
        ),
        reply_markup=_main_menu_keyboard(),
    )


@owner_only
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["manual_mode"] = True
    await _send_main_menu(update, context)


@owner_only
async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    browser = await get_browser(context)
    if browser.lock.locked():
        await update.effective_message.reply_text(
            "🟠 يوجد توليد قيد التنفيذ. استخدم /shot أو /status، ولا أفتح صفحة جديدة حتى لا يتعطل الطلب الحالي."
        )
        return
    # Preserve the current page. /login is a remote-screen link, not a command
    # that should navigate away from an already-correct Image-to-3D screen.
    if not browser.page or browser.page.url in ("", "about:blank"):
        try:
            await browser.open_home()
        except Exception:
            pass
    await update.effective_message.reply_text(
        "🔐 افتح واجهة المتصفح وسجّل دخولك يدويًا إلى Hunyuan.\n"
        "بعدها لا تسجّل خروج؛ الجلسة تنحفظ داخل Volume.\n\n"
        f"🌐 {novnc_url()}\n\n"
        "إذا ظهر QR أو CAPTCHA كمّله بنفسك من هذه الواجهة."
    )


@owner_only
async def open_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    browser = await get_browser(context)
    if browser.lock.locked():
        await update.effective_message.reply_text(
            "🟠 يوجد توليد قيد التنفيذ. استخدم /shot أو /status؛ لن أغيّر الصفحة أثناء التوليد."
        )
        return
    try:
        await browser.open_home()
        await update.effective_message.reply_text("✅ فتحت صفحة Hunyuan داخل المتصفح.")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ تعذر فتح الموقع: {e}")


@owner_only
async def shot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    browser = await get_browser(context)
    path = JOBS_DIR / f"screen-{int(time.time())}.png"
    try:
        await browser.screenshot(path)
        mode = await browser.page_mode() if browser.page else "unknown"
        mode_label = {
            "image3d": "✅ صفحة رفع 图/文生3D جاهزة",
            "image3d_landing": "🟡 واجهة Hunyuan الرئيسية — راح أفتح 图/文生3D تلقائياً عند إرسال صورة",
            "world": "⚠️ صفحة 世界模型 (مو Image-to-3D)",
            "login": "🔐 صفحة تسجيل الدخول",
            "unknown": "❔ صفحة غير معروفة",
        }.get(mode, mode)
        with path.open("rb") as f:
            await update.effective_message.reply_photo(
                f,
                caption=f"🖥 حالة المتصفح\n{mode_label}\n{browser.page.url if browser.page else ''}"
            )
    except Exception as e:
        await update.effective_message.reply_text(f"❌ فشل أخذ اللقطة: {e}")
    finally:
        path.unlink(missing_ok=True)


@owner_only
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    browser = await get_browser(context)
    busy = browser.lock.locked()
    url = browser.page.url if browser.page else "غير متاح"
    mode = await browser.page_mode() if browser.page else "unknown"
    mode_label = {
        "image3d": "✅ صفحة رفع 图/文生3D",
        "image3d_landing": "🟡 الرئيسية — جاهز للدخول إلى 图/文生3D تلقائياً",
        "world": "⚠️ 世界模型 — مو صفحة التوليد المطلوبة",
        "login": "🔐 تسجيل الدخول",
        "unknown": "❔ غير معروف",
    }.get(mode, mode)
    await update.effective_message.reply_text(
        f"{'🟠 يوجد توليد قيد التنفيذ' if busy else '🟢 جاهز'}\n"
        f"🧭 الواجهة: {mode_label}\n🌐 الصفحة الحالية: {url}"
    )


def _number_keyboard(kind: str):
    other = "grid" if kind == "smart" else "smart"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 تحديث الأرقام", callback_data=f"num:{kind}:refresh"), InlineKeyboardButton("🔁 تبديل الشبكة", callback_data=f"num:{other}:refresh")],
        [InlineKeyboardButton("📎 رفع آخر صورة", callback_data="num:upload"), InlineKeyboardButton("📦 إرسال الجلسة", callback_data="menu:session")],
        [InlineKeyboardButton("⬅️ رجوع", callback_data="num:back"), InlineKeyboardButton("↻ تحديث الصفحة", callback_data="num:reload"), InlineKeyboardButton("🏠 القائمة", callback_data="menu:main")],
    ])


async def _save_targets_snapshot(browser: HunyuanBrowser, kind: str, targets: list, path: Path):
    meta = {
        "ts": time.time(), "kind": kind,
        "url": browser.page.url if browser.page else "",
        "viewport": await browser._viewport() if browser.page else {},
        "targets": targets,
    }
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_TARGETS.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    # Keep screenshots so the session zip can be inspected/replayed later.
    saved = SESSION_SHOTS_DIR / path.name
    try:
        shutil.copy2(path, saved)
    except Exception:
        pass
    shots = sorted(SESSION_SHOTS_DIR.glob("number-*.png"), key=lambda x: x.stat().st_mtime)
    for old in shots[:-60]:
        old.unlink(missing_ok=True)


async def _render_number_grid(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str = "smart", query=None, note: str = ""):
    browser = await get_browser(context)
    if not browser.page:
        return
    context.application.bot_data["manual_mode"] = True
    targets = await browser.numbered_targets(kind)
    if kind == "smart" and not targets:
        kind = "grid"
        targets = await browser.numbered_targets(kind)
        note = (note + "\n" if note else "") + "⚠️ ما اكتشفت عناصر تفاعلية، حولت تلقائياً لشبكة الشاشة."
    context.user_data["number_mode"] = {"kind": kind, "targets": targets, "ts": time.time()}
    path = JOBS_DIR / f"number-{int(time.time()*1000)}.png"
    await browser.screenshot_numbered(path, targets, kind)
    await _save_targets_snapshot(browser, kind, targets, path)
    caption = (
        ("🔢 شبكة العناصر الذكية" if kind == "smart" else "🧩 شبكة الشاشة") + "\n"
        f"عدد الأرقام: {len(targets)}\n"
        "✍️ اكتب رقم فقط في الدردشة، مثال: 12 — وأنا أضغطه مباشرة وأسجل العنصر.\n"
        "🎯 للشغلات الصغيرة استخدم الشبكة الذكية؛ الرقم مربوط بالعنصر نفسه مو بحركة موس."
    )
    if note:
        caption += f"\n{note[:500]}"
    kb = _number_keyboard(kind)
    try:
        if query:
            with path.open("rb") as f:
                try:
                    await query.edit_message_media(media=InputMediaPhoto(media=f, caption=caption), reply_markup=kb)
                    return
                except Exception:
                    pass
        msg = update.effective_message if update else None
        if msg:
            with path.open("rb") as f:
                await msg.reply_photo(f, caption=caption, reply_markup=kb)
        elif query:
            with path.open("rb") as f:
                await query.message.reply_photo(f, caption=caption, reply_markup=kb)
    finally:
        path.unlink(missing_ok=True)


async def _export_training_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    browser = await get_browser(context)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    meta = {
        "exported_at": time.time(),
        "url": browser.page.url if browser.page else "",
        "title": (await browser.page.title()) if browser.page else "",
        "viewport": await browser._viewport() if browser.page else {},
        "manual_mode": bool(context.application.bot_data.get("manual_mode", True)),
        "last_image": Path(context.application.bot_data.get("last_manual_image", "")).name if context.application.bot_data.get("last_manual_image") else None,
    }
    SESSION_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    out = JOBS_DIR / f"hunyuan-training-session-{stamp}.zip"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        if TRAIN_LOG.exists(): z.write(TRAIN_LOG, "actions.jsonl")
        if SESSION_TARGETS.exists(): z.write(SESSION_TARGETS, "latest-targets.json")
        if SESSION_META.exists(): z.write(SESSION_META, "session-meta.json")
        for fp in sorted(SESSION_SHOTS_DIR.glob("*.png"))[-60:]:
            z.write(fp, f"screens/{fp.name}")
        for fp in sorted(MARKS_DIR.glob("*.png"))[-30:]:
            z.write(fp, f"marks/{fp.name}")
    msg = update.effective_message if update.effective_message else (update.callback_query.message if update.callback_query else None)
    if msg:
        with out.open("rb") as f:
            await msg.reply_document(f, filename=out.name, caption="📦 جلسة التدريب كاملة: الضغطات + العناصر + الإحداثيات + لقطات الشاشة. دزلي هذا الملف حتى أبني الأتمتة النهائية.")
    out.unlink(missing_ok=True)


async def _clear_training_session(context: ContextTypes.DEFAULT_TYPE):
    TRAIN_LOG.unlink(missing_ok=True)
    SESSION_TARGETS.unlink(missing_ok=True)
    SESSION_META.unlink(missing_ok=True)
    for d in (SESSION_SHOTS_DIR, MARKS_DIR):
        for fp in d.glob("*"):
            if fp.is_file(): fp.unlink(missing_ok=True)
    context.user_data.pop("number_mode", None)


def _control_keyboard(step: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↖️", callback_data="ctl:ul"), InlineKeyboardButton("⬆️", callback_data="ctl:u"), InlineKeyboardButton("↗️", callback_data="ctl:ur")],
        [InlineKeyboardButton("⬅️", callback_data="ctl:l"), InlineKeyboardButton("🖱 ضغط", callback_data="ctl:click"), InlineKeyboardButton("➡️", callback_data="ctl:r")],
        [InlineKeyboardButton("↙️", callback_data="ctl:dl"), InlineKeyboardButton("⬇️", callback_data="ctl:d"), InlineKeyboardButton("↘️", callback_data="ctl:dr")],
        [InlineKeyboardButton("🖱×2", callback_data="ctl:dbl"), InlineKeyboardButton(f"📏 {step}px", callback_data="ctl:step"), InlineKeyboardButton("👁 فحص", callback_data="ctl:inspect")],
        [InlineKeyboardButton("⇧ سكرول", callback_data="ctl:su"), InlineKeyboardButton("⇩ سكرول", callback_data="ctl:sd"), InlineKeyboardButton("🔄 صورة", callback_data="ctl:refresh")],
        [InlineKeyboardButton("⬅️ رجوع", callback_data="ctl:back"), InlineKeyboardButton("↻ تحديث", callback_data="ctl:reload"), InlineKeyboardButton("🏠 Hunyuan", callback_data="ctl:home")],
        [InlineKeyboardButton("📎 رفع آخر صورة", callback_data="ctl:upload"), InlineKeyboardButton("↵ Enter", callback_data="ctl:enter"), InlineKeyboardButton("Esc", callback_data="ctl:esc")],
        [InlineKeyboardButton("⌨️ كتابة", callback_data="ctl:type"), InlineKeyboardButton("🏷 حفظ خطوة", callback_data="ctl:mark"), InlineKeyboardButton("🏠 القائمة", callback_data="ctl:menu")],
    ])


def _manual_state(context: ContextTypes.DEFAULT_TYPE):
    st = context.application.bot_data.setdefault("manual_state", {"x": 680, "y": 380, "step": 75})
    st.setdefault("x", 680)
    st.setdefault("y", 380)
    st.setdefault("step", 75)
    return st


async def _render_control(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None, note: str = ""):
    browser = await get_browser(context)
    st = _manual_state(context)
    vp = await browser._viewport()
    st["x"] = max(0, min(int(vp["w"]) - 1, int(st["x"])))
    st["y"] = max(0, min(int(vp["h"]) - 1, int(st["y"])))
    path = JOBS_DIR / f"control-{int(time.time()*1000)}.png"
    await browser.screenshot_with_cursor(path, st["x"], st["y"])
    mode = await browser.page_mode() if browser.page else "unknown"
    caption = (
        f"🖱 وضع التدريب اليدوي\n"
        f"📍 X={st['x']}  Y={st['y']}   📏 خطوة={st['step']}px\n"
        f"🧭 {mode}  🌐 {(browser.page.url if browser.page else '')[:220]}"
    )
    if note:
        caption += f"\n{note[:500]}"
    kb = _control_keyboard(int(st["step"]))
    try:
        if query:
            with path.open("rb") as f:
                try:
                    await query.edit_message_media(media=InputMediaPhoto(media=f, caption=caption), reply_markup=kb)
                    return
                except Exception:
                    pass
        msg = update.effective_message if update else None
        if msg:
            with path.open("rb") as f:
                await msg.reply_photo(f, caption=caption, reply_markup=kb)
        elif query:
            with path.open("rb") as f:
                await query.message.reply_photo(f, caption=caption, reply_markup=kb)
    finally:
        path.unlink(missing_ok=True)


@owner_only
async def manual_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["manual_mode"] = True
    await update.effective_message.reply_text(
        "🧪 فعلت وضع التدريب اليدوي. أي صورة ترسلها راح أخزنها فقط، وما راح أضغط Generate وحدي.\n"
        "استخدم /control للتحكم بالماوس من البوت."
    )
    await _render_control(update, context)


@owner_only
async def auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    browser = await get_browser(context)
    if browser.lock.locked():
        await update.effective_message.reply_text("🟠 أكو عملية حالياً؛ ما أبدل الوضع وهي شغالة.")
        return
    context.application.bot_data["manual_mode"] = False
    await update.effective_message.reply_text("🤖 فعلت الوضع التلقائي الحالي. للتدريب ارجع بـ /manual.")


@owner_only
async def control_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["manual_mode"] = True
    await _render_control(update, context)


@owner_only
async def type_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.effective_message.reply_text("اكتب هكذا: /type النص الذي تريد كتابته")
        return
    browser = await get_browser(context)
    st = _manual_state(context)
    await browser.manual_type(st, text)
    await update.effective_message.reply_text("⌨️ كتبت النص وسجلت الخطوة.")
    await _render_control(update, context)


@owner_only
async def mark_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = " ".join(context.args).strip() or "step"
    browser = await get_browser(context)
    st = _manual_state(context)
    path, elem = await browser.mark_training(st, name)
    txt = json.dumps(elem, ensure_ascii=False, indent=2)[:2500]
    with path.open("rb") as f:
        await update.effective_message.reply_photo(f, caption=f"🏷 Mark: {name}\n{txt}")


@owner_only
async def exportlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not TRAIN_LOG.exists() or TRAIN_LOG.stat().st_size == 0:
        await update.effective_message.reply_text("📭 بعد ماكو سجل تدريب. استخدم /control وابدأ تضغط.")
        return
    with TRAIN_LOG.open("rb") as f:
        await update.effective_message.reply_document(f, filename="hunyuan-training-actions.jsonl", caption="🧠 سجل التدريب: الضغطات + الإحداثيات + العنصر تحت المؤشر + الصفحة.")


@owner_only
async def clearlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    TRAIN_LOG.unlink(missing_ok=True)
    await update.effective_message.reply_text("🧹 مسحت سجل التدريب القديم.")


@owner_only
async def control_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    uid = q.from_user.id if q.from_user else 0
    if OWNER_ID and uid != OWNER_ID:
        await q.answer("هذا التحكم خاص بصاحب البوت", show_alert=True)
        return
    await q.answer()
    context.application.bot_data["manual_mode"] = True
    browser = await get_browser(context)
    st = _manual_state(context)
    action = (q.data or "").split(":", 1)[-1]
    step = int(st.get("step", 75))
    move_map = {
        "u": (0, -step), "d": (0, step), "l": (-step, 0), "r": (step, 0),
        "ul": (-step, -step), "ur": (step, -step), "dl": (-step, step), "dr": (step, step),
    }
    note = ""
    try:
        if action in move_map:
            await browser.manual_move(st, *move_map[action])
        elif action == "step":
            vals = [20, 50, 100, 180]
            cur = int(st.get("step", 75))
            nearest = min(range(len(vals)), key=lambda i: abs(vals[i] - cur))
            st["step"] = vals[(nearest + 1) % len(vals)]
            await browser.log_training_action("step_change", int(st["x"]), int(st["y"]), {"step": st["step"]})
        elif action == "click":
            el = await browser.manual_click(st, 1)
            note = "✅ ضغطت وسجلت العنصر: " + ((el or {}).get("text") or (el or {}).get("selector") or "")[:160]
        elif action == "dbl":
            await browser.manual_click(st, 2)
            note = "✅ Double click"
        elif action == "su":
            await browser.manual_scroll(st, -520)
        elif action == "sd":
            await browser.manual_scroll(st, 520)
        elif action == "back":
            await browser.manual_back(st)
        elif action == "reload":
            await browser.manual_reload(st)
        elif action == "home":
            await browser.manual_open_home(st)
        elif action == "enter":
            await browser.manual_key(st, "Enter")
        elif action == "esc":
            await browser.manual_key(st, "Escape")
        elif action == "inspect":
            el = await browser._element_at(int(st["x"]), int(st["y"]))
            await browser.log_training_action("inspect", int(st["x"]), int(st["y"]), before=el)
            txt = json.dumps(el, ensure_ascii=False, indent=2)[:3500]
            await q.message.reply_text(f"👁 العنصر تحت المؤشر:\n{txt}")
        elif action == "upload":
            last = context.application.bot_data.get("last_manual_image")
            if not last or not Path(last).exists():
                note = "⚠️ أرسل صورة للبوت أولاً، بعدها اضغط رفع آخر صورة."
            else:
                await browser.manual_upload_file(st, Path(last))
                note = "📎 رفعت آخر صورة وسجلت input المستخدم."
        elif action == "type":
            context.user_data["awaiting_trainer_text"] = "type"
            await q.message.reply_text("⌨️ أرسل النص هسه، وأنا أكتبه بمكان المؤشر الحالي.")
            return
        elif action == "mark":
            context.user_data["awaiting_trainer_text"] = "mark"
            await q.message.reply_text("🏷 أرسل اسم الخطوة، مثال: generate_button")
            return
        elif action == "menu":
            await _send_main_menu(update, context, "🏠 القائمة الرئيسية")
            return
        elif action == "refresh":
            pass
    except Exception as e:
        log.exception("Manual control action failed")
        note = f"❌ {e}"
    await _render_control(update, context, query=q, note=note)


@owner_only
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    action = (q.data or "").split(":", 1)[-1]

    if action == "main":
        await _send_main_menu(update, context, "🏠 القائمة الرئيسية")
    elif action == "control":
        context.application.bot_data["manual_mode"] = True
        await _render_control(update, context, query=q)
    elif action == "numsmart":
        await _render_number_grid(update, context, "smart", query=q)
    elif action == "numgrid":
        await _render_number_grid(update, context, "grid", query=q)
    elif action == "manual":
        context.application.bot_data["manual_mode"] = True
        await q.message.reply_text("🧪 تم تفعيل الوضع اليدوي. الصور تنخزن فقط وماكو ضغط تلقائي.", reply_markup=_main_menu_keyboard())
    elif action == "auto":
        browser = await get_browser(context)
        if browser.lock.locked():
            await q.message.reply_text("🟠 أكو عملية حالياً؛ ما أبدل الوضع وهي شغالة.", reply_markup=_main_menu_keyboard())
        else:
            context.application.bot_data["manual_mode"] = False
            await q.message.reply_text("🤖 تم تفعيل الوضع التلقائي الحالي.", reply_markup=_main_menu_keyboard())
    elif action == "status":
        await status_cmd(update, context)
    elif action == "shot":
        await shot_cmd(update, context)
    elif action == "open":
        await open_cmd(update, context)
    elif action == "login":
        await login_cmd(update, context)
    elif action == "exportlog":
        await exportlog_cmd(update, context)
    elif action == "session":
        await _export_training_session(update, context)
    elif action == "upload":
        browser = await get_browser(context)
        last = context.application.bot_data.get("last_manual_image")
        if not last or not Path(last).exists():
            await q.message.reply_text("⚠️ أرسل صورة للبوت أولاً.", reply_markup=_main_menu_keyboard())
        else:
            try:
                await browser.manual_upload_file(_manual_state(context), Path(last))
                await q.message.reply_text("📎 رفعت آخر صورة. افتح 🔢 شبكة الأرقام حتى تضغط Generate.", reply_markup=_main_menu_keyboard())
            except Exception as e:
                await q.message.reply_text(f"❌ تعذر الرفع: {e}", reply_markup=_main_menu_keyboard())
    elif action == "clearlog":
        await _clear_training_session(context)
        await q.message.reply_text("🧹 بدأت جلسة تدريب جديدة ومسحت السجل واللقطات القديمة.", reply_markup=_main_menu_keyboard())
    elif action == "type":
        context.user_data["awaiting_trainer_text"] = "type"
        await q.message.reply_text("⌨️ أرسل النص هسه، وأنا أكتبه بمكان المؤشر الحالي.")
    elif action == "mark":
        context.user_data["awaiting_trainer_text"] = "mark"
        await q.message.reply_text("🏷 أرسل اسم الخطوة، مثال: generate_button")


@owner_only
async def grid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _render_number_grid(update, context, "smart")


@owner_only
async def screen_grid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _render_number_grid(update, context, "grid")


@owner_only
async def session_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _export_training_session(update, context)


@owner_only
async def number_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = (q.data or "").split(":")
    if len(data) >= 3 and data[1] in ("smart", "grid"):
        await _render_number_grid(update, context, data[1], query=q)
        return
    action = data[1] if len(data) > 1 else ""
    browser = await get_browser(context)
    if action == "back":
        await browser.manual_back(_manual_state(context))
        await _render_number_grid(update, context, context.user_data.get("number_mode", {}).get("kind", "smart"), query=q)
    elif action == "reload":
        await browser.manual_reload(_manual_state(context))
        await _render_number_grid(update, context, context.user_data.get("number_mode", {}).get("kind", "smart"), query=q)
    elif action == "upload":
        last = context.application.bot_data.get("last_manual_image")
        if not last or not Path(last).exists():
            await q.message.reply_text("⚠️ أرسل صورة للبوت أولاً.")
        else:
            try:
                await browser.manual_upload_file(_manual_state(context), Path(last))
                await _render_number_grid(update, context, context.user_data.get("number_mode", {}).get("kind", "smart"), query=q, note="📎 تم رفع آخر صورة.")
            except Exception as e:
                await q.message.reply_text(f"❌ تعذر الرفع: {e}")


@owner_only
async def trainer_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    mode = context.user_data.get("awaiting_trainer_text")

    # Number-grid mode has priority. A plain integer is a direct click command.
    number_state = context.user_data.get("number_mode") or {}
    if not mode and text.isdigit() and number_state.get("targets"):
        n = int(text)
        targets = number_state.get("targets") or []
        target = next((t for t in targets if int(t.get("n") or -1) == n), None)
        if not target:
            await update.effective_message.reply_text(f"⚠️ الرقم {n} مو موجود بالصورة الحالية. اختر من 1 إلى {len(targets)}.")
            return
        browser = await get_browser(context)
        last = context.application.bot_data.get("last_manual_image")
        image_path = Path(last) if last and Path(last).exists() else None
        try:
            result = await browser.click_number_target(target, image_path=image_path)
            note_parts = [f"✅ ضغطت الرقم {n}"]
            label = ((target.get("text") or "").strip() or target.get("selector") or "")[:120]
            if label: note_parts.append(f"العنصر: {label}")
            if result.get("upload"): note_parts.append("📎 وتم تمرير آخر صورة إلى اختيار الملف")
            if result.get("download"):
                dl = Path(result["download"])
                with dl.open("rb") as f:
                    await update.effective_message.reply_document(f, filename=dl.name, caption="📦 التقطت ملف التنزيل أثناء التدريب.")
            await _render_number_grid(update, context, number_state.get("kind", "smart"), note=" | ".join(note_parts))
        except Exception as e:
            log.exception("Number click failed")
            await update.effective_message.reply_text(f"❌ فشل ضغط الرقم {n}: {e}")
        return

    mode = context.user_data.pop("awaiting_trainer_text", None)
    if not mode:
        await update.effective_message.reply_text("🔢 إذا تريد تضغط رقم، افتح أولاً زر «شبكة الأرقام الذكية» من /start.", reply_markup=_main_menu_keyboard())
        return
    if not text:
        await update.effective_message.reply_text("⚠️ أرسل نص غير فارغ.", reply_markup=_main_menu_keyboard())
        return
    browser = await get_browser(context)
    st = _manual_state(context)
    if mode == "type":
        await browser.manual_type(st, text)
        await update.effective_message.reply_text("⌨️ تمّت الكتابة وسجلت الخطوة.", reply_markup=_main_menu_keyboard())
    elif mode == "mark":
        path, elem = await browser.mark_training(st, text)
        txt = json.dumps(elem, ensure_ascii=False, indent=2)[:2200]
        try:
            with path.open("rb") as f:
                await update.effective_message.reply_photo(f, caption=f"🏷 Mark: {text}\n{txt}", reply_markup=_main_menu_keyboard())
        finally:
            path.unlink(missing_ok=True)


async def _download_telegram_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[Path]:
    msg = update.effective_message
    if msg.photo:
        tg_file = await msg.photo[-1].get_file()
        ext = ".jpg"
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
        tg_file = await msg.document.get_file()
        name = msg.document.file_name or "image.png"
        ext = Path(name).suffix or ".png"
    else:
        return None
    path = JOBS_DIR / f"input-{msg.message_id}-{int(time.time())}{ext}"
    await tg_file.download_to_drive(custom_path=str(path))
    return path


@owner_only
async def image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    browser = await get_browser(context)
    if context.application.bot_data.get("manual_mode", MANUAL_DEFAULT):
        image_path = await _download_telegram_image(update, context)
        if not image_path:
            await msg.reply_text("أرسل الصورة كصورة أو كملف صورة.")
            return
        old_path = context.application.bot_data.get("last_manual_image")
        if old_path and Path(old_path).exists() and Path(old_path) != image_path:
            try:
                Path(old_path).unlink()
            except Exception:
                pass
        context.application.bot_data["last_manual_image"] = str(image_path)
        await msg.reply_text(
            "🧪 خزنت الصورة للتدريب وما ضغطت أي شي بالموقع.\n"
            "افتح 🔢 شبكة الأرقام الذكية. إذا ضغطت رقم زر رفع الصورة راح أستخدم هاي الصورة تلقائياً، أو استخدم 📎 رفع آخر صورة.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔢 شبكة الأرقام الذكية", callback_data="menu:numsmart")],
                [InlineKeyboardButton("📎 رفع آخر صورة", callback_data="menu:upload"), InlineKeyboardButton("📦 إرسال الجلسة", callback_data="menu:session")],
                [InlineKeyboardButton("🏠 القائمة", callback_data="menu:main")],
            ]),
        )
        return
    if browser.lock.locked():
        await msg.reply_text(
            "🟠 عندي توليد شغّال حالياً. ما راح أضيف صورة ثانية حتى لا تتداخل الطلبات أو ينصرف رصيد إضافي.\n"
            "استخدم /status أو /shot لمتابعة الطلب الحالي."
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    image_path = await _download_telegram_image(update, context)
    if not image_path:
        await msg.reply_text("أرسل الصورة كصورة أو كملف صورة.")
        return

    status = await msg.reply_text("📤 استلمت الصورة. أفتح Hunyuan وأرفعها الآن…")

    progress_ticks = 0
    async def progress():
        nonlocal progress_ticks
        progress_ticks += 1
        dots = "." * ((progress_ticks % 3) + 1)
        try:
            await status.edit_text(f"🧊 Hunyuan يعمل على النموذج 3D{dots}\nسأرسل الملف هنا عند اكتماله.")
        except Exception:
            pass

    try:
        result = await browser.generate(image_path, progress_cb=progress)
        await status.edit_text("✅ اكتمل النموذج. جاري إرسال ملف الـ3D…")
        with result.open("rb") as f:
            await msg.reply_document(document=f, filename=result.name, caption="🧊 نموذج Hunyuan 3D")
        await status.edit_text("✅ تم التوليد والتنزيل والإرسال بنجاح.")
    except Exception as e:
        log.exception("Generation failed")
        screen = JOBS_DIR / f"error-{int(time.time())}.png"
        try:
            await browser.screenshot(screen)
            with screen.open("rb") as f:
                await msg.reply_photo(
                    f,
                    caption=(
                        "❌ توقف التنفيذ حتى لا أضغط شيء غلط أو أضيّع الرصيد.\n"
                        f"السبب: {e}\n\n"
                        "شوف الصورة، وإذا كانت جلسة الدخول منتهية استخدم /login."
                    ),
                )
        except Exception:
            await msg.reply_text(f"❌ توقف التنفيذ: {e}\nاستخدم /shot لمعرفة حالة الصفحة.")
        finally:
            screen.unlink(missing_ok=True)
        try:
            await status.edit_text("❌ التوليد توقف. أرسلت لك سبب المشكلة وحالة الشاشة.")
        except Exception:
            pass
    finally:
        image_path.unlink(missing_ok=True)


async def post_init(application: Application):
    browser = HunyuanBrowser()
    await browser.start()
    application.bot_data["hunyuan_browser"] = browser
    application.bot_data["manual_mode"] = MANUAL_DEFAULT
    vp = await browser._viewport()
    application.bot_data["manual_state"] = {"x": int(vp["w"]) // 2, "y": int(vp["h"]) // 2, "step": 75}
    log.info("Browser started. noVNC: %s | manual_mode=%s", novnc_url(), MANUAL_DEFAULT)


async def post_shutdown(application: Application):
    browser = application.bot_data.get("hunyuan_browser")
    if browser:
        await browser.stop()


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is required")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("manual", manual_cmd))
    app.add_handler(CommandHandler("auto", auto_cmd))
    app.add_handler(CommandHandler("control", control_cmd))
    app.add_handler(CommandHandler("grid", grid_cmd))
    app.add_handler(CommandHandler("screen_grid", screen_grid_cmd))
    app.add_handler(CommandHandler("session", session_cmd))
    app.add_handler(CommandHandler("type", type_cmd))
    app.add_handler(CommandHandler("mark", mark_cmd))
    app.add_handler(CommandHandler("exportlog", exportlog_cmd))
    app.add_handler(CommandHandler("clearlog", clearlog_cmd))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("open", open_cmd))
    app.add_handler(CommandHandler("shot", shot_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(control_callback, pattern=r"^ctl:"))
    app.add_handler(CallbackQueryHandler(number_callback, pattern=r"^num:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, image_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, trainer_text_handler))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
