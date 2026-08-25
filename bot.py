import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Page
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
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

for p in (PROFILE_DIR, JOBS_DIR, DOWNLOADS_DIR):
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
            ],
        )
        pages = self.context.pages
        self.page = pages[0] if pages else await self.context.new_page()
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

    async def prepare_image_to_3d(self):
        assert self.page
        # If the upload control is already present, do not navigate away.
        if await self.page.locator('input[type="file"]').count():
            return

        await self._click_text(r"3D\s*Creation|Creation|3D创作|3D\s*创作|创作")
        await self._click_text(r"Image\s*(?:to|[-→])\s*3D|Image.*3D|图生\s*3D|图片.*3D")

        # Give SPA transitions time to render.
        await self.page.wait_for_timeout(1800)

    async def upload_image(self, image_path: Path):
        assert self.page
        await self.prepare_image_to_3d()
        inputs = self.page.locator('input[type="file"]')
        count = await inputs.count()
        if count == 0:
            raise RuntimeError("لم أجد مربع رفع الصورة في الصفحة.")

        # Prefer an image-accepting input, otherwise use the first file input.
        chosen = None
        for i in range(count):
            el = inputs.nth(i)
            try:
                accept = (await el.get_attribute("accept")) or ""
                if "image" in accept.lower() or accept == "":
                    chosen = el
                    break
            except Exception:
                pass
        chosen = chosen or inputs.first
        await chosen.set_input_files(str(image_path))
        await self.page.wait_for_timeout(2500)

    async def click_generate(self) -> bool:
        patterns = [
            r"Generate\s*Immediately",
            r"Generate",
            r"Create\s*3D",
            r"生成\s*3D",
            r"立即生成",
            r"开始生成",
            r"生成",
        ]
        for p in patterns:
            if await self._click_text(p, timeout=5000):
                return True
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

    async def generate(self, image_path: Path, progress_cb=None) -> Path:
        async with self.lock:
            await self.open_home()
            await self.upload_image(image_path)
            clicked = await self.click_generate()
            if not clicked:
                # Some versions auto-start once an image is uploaded. Wait briefly before declaring failure.
                await self.page.wait_for_timeout(2500)
                if not await self._download_button_visible():
                    body = ""
                    try:
                        body = await self.page.locator("body").inner_text(timeout=3000)
                    except Exception:
                        pass
                    if re.search(r"Login|Log in|Sign in|登录|登入", body, re.I) and any(
                        x in self.page.url.lower() for x in ("login", "signin", "auth")
                    ):
                        raise RuntimeError("تحتاج تسجّل دخول من واجهة المتصفح أولاً.")
            await self.wait_until_ready(progress_cb=progress_cb)
            return await self.download_result()


async def get_browser(context: ContextTypes.DEFAULT_TYPE) -> HunyuanBrowser:
    return context.application.bot_data["hunyuan_browser"]


@owner_only
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 بوت Hunyuan 3D — بدون API\n\n"
        "1) افتح /login وسجّل دخولك في Hunyuan مرة واحدة.\n"
        "2) بعد تسجيل الدخول ارجع للبوت وأرسل صورة.\n"
        "3) البوت يرفعها للموقع وينتظر التوليد ثم يرسل ملف 3D.\n\n"
        "الأوامر:\n"
        "/login — فتح رابط المتصفح لتسجيل الدخول\n"
        "/shot — لقطة شاشة لحالة الموقع\n"
        "/open — إعادة فتح Hunyuan\n"
        "/status — حالة البوت"
    )
    await update.effective_message.reply_text(text)


@owner_only
async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    browser = await get_browser(context)
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
        with path.open("rb") as f:
            await update.effective_message.reply_photo(f, caption=f"🖥 حالة المتصفح\n{browser.page.url if browser.page else ''}")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ فشل أخذ اللقطة: {e}")
    finally:
        path.unlink(missing_ok=True)


@owner_only
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    browser = await get_browser(context)
    busy = browser.lock.locked()
    url = browser.page.url if browser.page else "غير متاح"
    await update.effective_message.reply_text(
        f"{'🟠 يوجد توليد قيد التنفيذ' if busy else '🟢 جاهز'}\n🌐 الصفحة الحالية: {url}"
    )


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
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    image_path = await _download_telegram_image(update, context)
    if not image_path:
        await msg.reply_text("أرسل الصورة كصورة أو كملف صورة.")
        return

    browser = await get_browser(context)
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
    log.info("Browser started. noVNC: %s", novnc_url())


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
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("open", open_cmd))
    app.add_handler(CommandHandler("shot", shot_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, image_handler))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
