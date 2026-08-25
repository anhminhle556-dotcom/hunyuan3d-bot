import asyncio
import base64
import json
import logging
import os
import re
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("hunyuan-trainer")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.environ.get("OWNER_ID", "").strip()
OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW.isdigit() else 0
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PROFILE_DIR = DATA_DIR / "chrome-profile"
JOBS_DIR = DATA_DIR / "jobs"
DOWNLOADS_DIR = DATA_DIR / "downloads"
TRAIN_DIR = DATA_DIR / "training-v2"
SESSION_DIR = TRAIN_DIR / "current"
SHOTS_DIR = SESSION_DIR / "screens"
EVENTS_FILE = SESSION_DIR / "events.jsonl"
META_FILE = SESSION_DIR / "meta.json"
HUNYUAN_URL = os.environ.get("HUNYUAN_URL", "https://3d.hunyuan.tencent.com/")
WATCH_INTERVAL_SEC = int(os.environ.get("WATCH_INTERVAL_SEC", "10"))
WATCH_SCREENSHOT_EVERY_SEC = int(os.environ.get("WATCH_SCREENSHOT_EVERY_SEC", "30"))
WATCH_MAX_MIN = int(os.environ.get("WATCH_MAX_MIN", "30"))

for p in (PROFILE_DIR, JOBS_DIR, DOWNLOADS_DIR, TRAIN_DIR, SESSION_DIR, SHOTS_DIR):
    p.mkdir(parents=True, exist_ok=True)


def owner_only(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        if OWNER_ID and uid != OWNER_ID:
            if update.effective_message:
                await update.effective_message.reply_text("⛔ هذا البوت خاص بصاحبه.")
            elif update.callback_query:
                await update.callback_query.answer("هذا التحكم خاص بصاحب البوت", show_alert=True)
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


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def safe_slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "")
    return value.strip("._")[:80] or "step"


def load_meta() -> dict:
    if META_FILE.exists():
        try:
            data = json.loads(META_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_meta(meta: dict):
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_session() -> dict:
    meta = load_meta()
    if meta.get("session_id") and meta.get("started_at"):
        return meta
    now = time.time()
    meta = {
        "session_id": time.strftime("%Y%m%d-%H%M%S", time.localtime(now)),
        "started_at": now,
        "started_at_utc": iso_utc(now),
        "last_event_at": None,
        "last_meaningful_at": None,
        "event_count": 0,
        "meaningful_count": 0,
        "description": "Hunyuan 3D manual training session",
    }
    save_meta(meta)
    return meta


def read_events() -> list:
    out = []
    if not EVENTS_FILE.exists():
        return out
    for line in EVENTS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def append_event(event: dict):
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    with EVENTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def guess_intent(action: str, element: Optional[dict] = None, extra: Optional[dict] = None) -> str:
    element = element or {}
    extra = extra or {}
    attrs = element.get("attrs") or {}
    blob = " ".join([
        str(element.get("text") or ""),
        str(attrs.get("aria-label") or ""),
        str(attrs.get("title") or ""),
        str(attrs.get("name") or ""),
        str(extra.get("query") or ""),
    ])
    if action == "upload_file" or re.search(r"上传图片|Upload\s*Image|Choose\s*Image|Select\s*Image", blob, re.I):
        return "upload_image"
    if re.search(r"立即生成|开始生成|Generate|Create\s*3D|生成\s*3D", blob, re.I):
        return "generate_3d"
    if re.search(r"下载|导出|Download|Export|\bGLB\b|\bOBJ\b|\bFBX\b", blob, re.I):
        return "download_model"
    if re.search(r"图/文生3D|图生3D|文生3D|立即开始|开始创作|Start\s*Now|Get\s*Started", blob, re.I):
        return "enter_image_to_3d"
    if re.search(r"单张图片|Single", blob, re.I):
        return "single_image_mode"
    if re.search(r"多张图片|Multi", blob, re.I):
        return "multi_image_mode"
    if action == "back":
        return "browser_back"
    if action == "reload":
        return "browser_reload"
    if action == "open_home":
        return "open_hunyuan_home"
    if action == "type_text":
        return "type_text"
    if action == "key":
        return "keyboard_key"
    if action == "snapshot":
        return "observe_page"
    return "click_element" if action in ("click", "double_click", "text_click") else action


class Browser:
    def __init__(self):
        self.pw = None
        self.context = None
        self.page: Optional[Page] = None
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
        self.page = restored[-1] if restored else (pages[0] if pages else await self.context.new_page())
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

    async def viewport(self):
        if not self.page:
            return {"w": 1365, "h": 768}
        try:
            return await self.page.evaluate("() => ({w:innerWidth,h:innerHeight})")
        except Exception:
            return {"w": 1365, "h": 768}

    async def fast_screenshot(self, path: Path):
        """Bounded screenshot with browser and X11 fallbacks."""
        if not self.page:
            raise RuntimeError("Browser not started")
        errors=[]
        try:
            await asyncio.wait_for(
                self.page.screenshot(path=str(path), full_page=False, timeout=3000, animations="disabled", caret="hide"),
                timeout=4.0,
            )
            return
        except Exception as e:
            errors.append(f"playwright:{e}")
        session=None
        try:
            session=await self.context.new_cdp_session(self.page)
            result=await asyncio.wait_for(session.send("Page.captureScreenshot", {
                "format":"png", "fromSurface":True, "captureBeyondViewport":False
            }), timeout=4.0)
            path.write_bytes(base64.b64decode(result["data"]))
            return
        except Exception as e:
            errors.append(f"cdp:{e}")
        finally:
            if session:
                try: await session.detach()
                except Exception: pass
        if shutil.which("scrot"):
            try:
                proc=await asyncio.create_subprocess_exec("scrot", "-o", str(path), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(proc.wait(), timeout=4.0)
                if proc.returncode==0 and path.exists() and path.stat().st_size>0:
                    return
            except Exception as e:
                errors.append(f"scrot:{e}")
        raise RuntimeError("Screenshot failed | "+" | ".join(errors[-3:]))

    async def screenshot_cursor(self, path: Path, x: int, y: int):
        if not self.page:
            raise RuntimeError("Browser not started")
        overlay_id = "__trainer_cursor_v2__"
        try:
            await self.page.evaluate(
                """({id,x,y}) => {
                    document.getElementById(id)?.remove();
                    const d=document.createElement('div'); d.id=id;
                    d.style.cssText=`position:fixed;left:${x-17}px;top:${y-17}px;width:34px;height:34px;border:3px solid #ff3131;border-radius:50%;z-index:2147483647;pointer-events:none;box-sizing:border-box;box-shadow:0 0 0 2px white,0 0 8px #000;`;
                    const h=document.createElement('div'); h.style.cssText='position:absolute;left:14px;top:-10px;width:2px;height:54px;background:#ff3131;';
                    const v=document.createElement('div'); v.style.cssText='position:absolute;left:-10px;top:14px;width:54px;height:2px;background:#ff3131;';
                    d.append(h,v); document.documentElement.appendChild(d);
                }""",
                {"id": overlay_id, "x": int(x), "y": int(y)},
            )
        except Exception:
            pass
        try:
            await self.fast_screenshot(path)
        finally:
            try:
                await self.page.evaluate("id => document.getElementById(id)?.remove()", overlay_id)
            except Exception:
                pass

    async def element_at(self, x: int, y: int):
        if not self.page:
            return None
        script = r"""
        ({x,y}) => {
          const el=document.elementFromPoint(x,y);
          if(!el) return null;
          const r=el.getBoundingClientRect();
          const attrs={};
          for(const n of ['id','class','role','aria-label','name','type','href','title','placeholder','data-testid','data-test','data-cy']){
            const v=el.getAttribute&&el.getAttribute(n); if(v) attrs[n]=String(v).slice(0,300);
          }
          function cssPath(node){
            if(!node||node.nodeType!==1) return '';
            if(node.id) return '#'+CSS.escape(node.id);
            const parts=[]; let cur=node;
            for(let depth=0;cur&&cur.nodeType===1&&depth<7;depth++,cur=cur.parentElement){
              let part=cur.tagName.toLowerCase();
              const cls=[...cur.classList].filter(Boolean).slice(0,2);
              if(cls.length) part+='.'+cls.map(c=>CSS.escape(c)).join('.');
              if(cur.parentElement){
                const same=[...cur.parentElement.children].filter(n=>n.tagName===cur.tagName);
                if(same.length>1) part+=`:nth-of-type(${same.indexOf(cur)+1})`;
              }
              parts.unshift(part); if(cur.id) break;
            }
            return parts.join(' > ');
          }
          return {
            tag:el.tagName.toLowerCase(),
            text:(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim().slice(0,500),
            attrs,
            selector:cssPath(el),
            bbox:{x:r.x,y:r.y,width:r.width,height:r.height},
            outerHTML:(el.outerHTML||'').slice(0,1400)
          };
        }"""
        try:
            return await self.page.evaluate(script, {"x": int(x), "y": int(y)})
        except Exception as e:
            return {"error": str(e)}

    async def page_info(self):
        url = self.page.url if self.page else ""
        title = ""
        if self.page:
            try:
                title = await self.page.title()
            except Exception:
                pass
        return {"url": url, "title": title, "viewport": await self.viewport()}

    async def generation_state(self):
        if not self.page:
            return {"status":"no_page","remaining_sec":None,"queue_count":None,"text":""}
        script=r"""
        () => {
          const txt=(document.body?.innerText||'').replace(/\s+/g,' ').trim();
          const rem=txt.match(/预计还需\s*(\d+)\s*秒/);
          const queue=txt.match(/前方\s*(\d+)\s*个任务/) || txt.match(/大概\s*(\d+)\s*秒后开始/);
          const ready=/(^|\s)(下载|导出|Download|Export)(\s|$)/i.test(txt) || /\b(GLB|OBJ|FBX)\b/i.test(txt);
          let status='unknown';
          if(ready) status='ready';
          else if(rem || /生成中|正在生成|纹理生成|几何生成/.test(txt)) status='generating';
          else if(queue || /排队|队列|等待生成/.test(txt)) status='queued';
          return {status, remaining_sec:rem?Number(rem[1]):null, queue_count:queue?Number(queue[1]):null, text:txt.slice(0,3500)};
        }
        """
        try:
            return await asyncio.wait_for(self.page.evaluate(script), timeout=3.5)
        except Exception as e:
            return {"status":"dom_error","remaining_sec":None,"queue_count":None,"text":"","error":str(e)}

    async def find_text_target(self, query: str):
        if not self.page:
            return None
        script = r"""
        ({query}) => {
          const q=String(query||'').trim().toLowerCase();
          if(!q) return null;
          const clickable='button,a,label,input,[role="button"],[role="link"],[role="menuitem"],[role="tab"],[onclick],[tabindex]';
          function visible(el){
            const r=el.getBoundingClientRect(),s=getComputedStyle(el);
            return r.width>3&&r.height>3&&r.bottom>0&&r.right>0&&r.left<innerWidth&&r.top<innerHeight&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity||1)>.05;
          }
          function cssPath(node){
            if(!node||node.nodeType!==1) return '';
            if(node.id) return '#'+CSS.escape(node.id);
            const parts=[]; let cur=node;
            for(let depth=0;cur&&cur.nodeType===1&&depth<7;depth++,cur=cur.parentElement){
              let part=cur.tagName.toLowerCase();
              const cls=[...cur.classList].filter(Boolean).slice(0,2);
              if(cls.length) part+='.'+cls.map(c=>CSS.escape(c)).join('.');
              if(cur.parentElement){
                const same=[...cur.parentElement.children].filter(n=>n.tagName===cur.tagName);
                if(same.length>1) part+=`:nth-of-type(${same.indexOf(cur)+1})`;
              }
              parts.unshift(part); if(cur.id) break;
            }
            return parts.join(' > ');
          }
          const all=[...document.querySelectorAll('body *')];
          let best=null;
          for(const el of all){
            if(!visible(el)) continue;
            const raw=(el.innerText||el.textContent||el.getAttribute('aria-label')||el.getAttribute('title')||'').replace(/\s+/g,' ').trim();
            if(!raw||!raw.toLowerCase().includes(q)) continue;
            let target=el.closest(clickable);
            if(!target){
              let cur=el;
              for(let i=0;i<4&&cur;i++,cur=cur.parentElement){
                try{ if(getComputedStyle(cur).cursor==='pointer'){target=cur;break;} }catch(_){}
              }
            }
            target=target||el;
            if(!visible(target)) continue;
            const r=target.getBoundingClientRect();
            const exact=raw.toLowerCase()===q?0:1;
            const area=r.width*r.height;
            const score=exact*100000+area;
            if(!best||score<best.score) best={target,raw,score};
          }
          if(!best) return null;
          const el=best.target,r=el.getBoundingClientRect(),attrs={};
          for(const n of ['id','class','role','aria-label','name','type','href','title','placeholder','data-testid','data-test','data-cy']){
            const v=el.getAttribute&&el.getAttribute(n); if(v) attrs[n]=String(v).slice(0,300);
          }
          return {
            x:Math.round(r.left+r.width/2), y:Math.round(r.top+r.height/2),
            tag:el.tagName.toLowerCase(),
            text:(el.innerText||el.textContent||best.raw).replace(/\s+/g,' ').trim().slice(0,500),
            matched_text:best.raw.slice(0,500),
            attrs, selector:cssPath(el),
            bbox:{x:r.x,y:r.y,width:r.width,height:r.height}
          };
        }"""
        try:
            return await self.page.evaluate(script, {"query": query})
        except Exception:
            return None

    async def save_download(self, download):
        name = download.suggested_filename or f"hunyuan-{int(time.time())}.glb"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
        target = DOWNLOADS_DIR / f"{int(time.time())}-{safe}"
        await download.save_as(str(target))
        return target

    async def click_xy(self, x: int, y: int, count: int = 1):
        if not self.page:
            raise RuntimeError("Browser not started")
        before_pages = list(self.context.pages)
        download = None
        try:
            async with self.page.expect_download(timeout=1300) as di:
                await self.page.mouse.click(int(x), int(y), click_count=count, delay=90)
            download = await di.value
        except PlaywrightTimeoutError:
            pass
        await self.page.wait_for_timeout(550)
        new_pages = [p for p in self.context.pages if p not in before_pages]
        if new_pages:
            self.page = new_pages[-1]
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=7000)
            except Exception:
                pass
        if download:
            return await self.save_download(download)
        return None

    async def click_text(self, query: str):
        target = await self.find_text_target(query)
        if not target:
            raise RuntimeError(f"ما لكيت كتابة ظاهرة تطابق: {query}")
        dl = await self.click_xy(int(target["x"]), int(target["y"]), 1)
        return target, dl

    async def upload_last_image(self, image_path: Path):
        if not self.page:
            raise RuntimeError("Browser not started")
        inputs = self.page.locator('input[type="file"]')
        count = await inputs.count()
        if count == 0:
            raise RuntimeError("ما لكيت حقل رفع صورة بالصفحة الحالية.")
        candidates = []
        for i in range(count):
            el = inputs.nth(i)
            try:
                accept = ((await el.get_attribute("accept")) or "").lower()
            except Exception:
                accept = ""
            score = 0 if "image" in accept else (1 if not accept else 2)
            candidates.append((score, i, el, accept))
        candidates.sort(key=lambda t: (t[0], t[1]))
        last_err = None
        for _, i, el, accept in candidates:
            try:
                meta = await el.evaluate("""el=>({tag:el.tagName.toLowerCase(),id:el.id||'',name:el.name||'',accept:el.accept||'',outerHTML:(el.outerHTML||'').slice(0,1400)})""")
                await el.set_input_files(str(image_path))
                await self.page.wait_for_timeout(1200)
                n = await el.evaluate("el=>el.files?el.files.length:0")
                if int(n or 0) > 0:
                    meta["input_index"] = i
                    meta["accept"] = accept
                    meta["file_name"] = image_path.name
                    return meta
            except Exception as e:
                last_err = e
        raise RuntimeError(f"حقل الرفع موجود لكن الصورة ما انقبلت: {last_err or 'unknown'}")


async def get_browser(context):
    return context.application.bot_data["browser"]


def manual_state(context):
    st = context.application.bot_data.setdefault("manual_state", {"x": 680, "y": 380, "step": 15})
    st.setdefault("x", 680)
    st.setdefault("y", 380)
    st.setdefault("step", 15)
    return st


async def capture_session_screenshot(browser: Browser, seq: int, action: str, phase: str, state: dict):
    path = SHOTS_DIR / f"{seq:04d}_{safe_slug(action)}_{phase}.png"
    try:
        await browser.screenshot_cursor(path, int(state["x"]), int(state["y"]))
        return path
    except Exception as e:
        log.warning("Session screenshot failed: %s", e)
        return None


async def record_action(context, action: str, perform, *, element=None, extra=None, screenshot=True, meaningful=True):
    browser = await get_browser(context)
    st = manual_state(context)
    meta = ensure_session()
    now = time.time()
    seq = int(meta.get("event_count", 0)) + 1
    before_info = await browser.page_info()
    if element is None and action in ("click", "double_click", "type_text", "key"):
        element = await browser.element_at(int(st["x"]), int(st["y"]))
    before_path = None
    if screenshot:
        before_path = await capture_session_screenshot(browser, seq, action, "before", st)

    result = None
    error = None
    try:
        result = await perform()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    await asyncio.sleep(0.35)
    after_info = await browser.page_info()
    after_path = None
    if screenshot:
        after_path = await capture_session_screenshot(browser, seq, action, "after", st)

    ended = time.time()
    last_event = meta.get("last_event_at")
    last_meaningful = meta.get("last_meaningful_at")
    intent = guess_intent(action, element, extra)
    event = {
        "seq": seq,
        "action": action,
        "intent_guess": intent,
        "started_at": now,
        "started_at_utc": iso_utc(now),
        "ended_at": ended,
        "duration_sec": round(ended - now, 3),
        "elapsed_from_session_sec": round(now - float(meta["started_at"]), 3),
        "wait_since_previous_event_sec": None if not last_event else round(now - float(last_event), 3),
        "wait_since_previous_meaningful_action_sec": None if not last_meaningful else round(now - float(last_meaningful), 3),
        "cursor": {"x": int(st["x"]), "y": int(st["y"]), "step_px": int(st["step"])},
        "element_before": element,
        "page_before": before_info,
        "page_after": after_info,
        "screenshot_before": str(before_path.relative_to(SESSION_DIR)) if before_path else None,
        "screenshot_after": str(after_path.relative_to(SESSION_DIR)) if after_path else None,
        "extra": extra or {},
        "result": result if isinstance(result, (str, int, float, bool, type(None), dict, list)) else str(result),
        "error": error,
        "meaningful": bool(meaningful),
    }
    append_event(event)
    meta["event_count"] = seq
    meta["last_event_at"] = now
    if meaningful:
        meta["meaningful_count"] = int(meta.get("meaningful_count", 0)) + 1
        meta["last_meaningful_at"] = now
    save_meta(meta)
    if error:
        raise RuntimeError(error)
    return event, result


async def record_observation(context, action="snapshot", extra=None):
    browser = await get_browser(context)
    st = manual_state(context)
    async def noop():
        return None
    return await record_action(context, action, noop, extra=extra or {}, screenshot=True, meaningful=False)


async def record_watch_observation(application, state: dict, *, screenshot=True):
    browser=application.bot_data["browser"]
    st=application.bot_data.setdefault("manual_state", {"x":680,"y":380,"step":15})
    meta=ensure_session(); now=time.time(); seq=int(meta.get("event_count",0))+1
    page_info=await browser.page_info(); shot=None
    if screenshot:
        shot=await capture_session_screenshot(browser, seq, "generation_watch", "after", st)
    ended=time.time()
    event={
        "seq":seq,"action":"generation_watch","intent_guess":"observe_generation",
        "started_at":now,"started_at_utc":iso_utc(now),"ended_at":ended,"duration_sec":round(ended-now,3),
        "elapsed_from_session_sec":round(now-float(meta["started_at"]),3),
        "wait_since_previous_event_sec":None if not meta.get("last_event_at") else round(now-float(meta["last_event_at"]),3),
        "wait_since_previous_meaningful_action_sec":None if not meta.get("last_meaningful_at") else round(now-float(meta["last_meaningful_at"]),3),
        "cursor":{"x":int(st.get("x",680)),"y":int(st.get("y",380)),"step_px":int(st.get("step",15))},
        "element_before":None,"page_before":page_info,"page_after":page_info,
        "screenshot_before":None,"screenshot_after":str(shot.relative_to(SESSION_DIR)) if shot else None,
        "extra":state,"result":state,"error":None,"meaningful":False,
    }
    append_event(event); meta["event_count"]=seq; meta["last_event_at"]=now; save_meta(meta)
    return event


def watch_status_text(state: dict, elapsed: float) -> str:
    status=state.get("status") or "unknown"; rem=state.get("remaining_sec"); q=state.get("queue_count")
    if status=="queued": return f"🟡 بالطابور"+(f" | قدامك {q} مهمة" if q is not None else "")
    if status=="generating": return f"🟠 جاري التوليد"+(f" | تقريباً {rem}ث باقي" if rem is not None else "")
    if status=="ready": return "🟢 النتيجة تبدو جاهزة"
    if status=="dom_error": return "⚠️ الصفحة ثقيلة، المتابعة مستمرة"
    return f"⚪ الحالة غير محسومة | مرّ {int(elapsed)}ث"


async def generation_watch_loop(application, chat_id:int):
    browser=application.bot_data["browser"]
    application.bot_data["generation_watch_stop"]=False
    started=time.time(); last_shot=0.0; seen_active=False; unknown_after_active=0; final_event=None; final_state=None
    msg=await application.bot.send_message(chat_id=chat_id,text="⏳ بدأت متابعة التوليد. ما راح أضغط أي زر؛ فقط أسجل الوقت والحالة ولقطات دورية.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 إيقاف المتابعة",callback_data="menu:watchstop"),InlineKeyboardButton("🎮 فتح الموس",callback_data="menu:control")]]))
    try:
        while time.time()-started < WATCH_MAX_MIN*60:
            if application.bot_data.get("generation_watch_stop"): break
            elapsed=time.time()-started
            async with browser.control_lock:
                state=await browser.generation_state(); state["watch_elapsed_sec"]=round(elapsed,1)
                status=state.get("status")
                if status in ("queued","generating"):
                    seen_active=True; unknown_after_active=0
                elif seen_active and status=="unknown": unknown_after_active+=1
                else: unknown_after_active=0
                do_shot=(time.time()-last_shot>=WATCH_SCREENSHOT_EVERY_SEC) or status=="ready"
                final_event=await record_watch_observation(application,state,screenshot=do_shot)
                if do_shot: last_shot=time.time()
            try:
                await msg.edit_text("⏳ متابعة Hunyuan تلقائياً\n"+watch_status_text(state,elapsed)+f"\n🕒 مرّ {int(elapsed)} ثانية من بدء المتابعة\n📸 اللقطات تنحفظ داخل الجلسة كل {WATCH_SCREENSHOT_EVERY_SEC}ث.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 إيقاف المتابعة",callback_data="menu:watchstop"),InlineKeyboardButton("🎮 فتح الموس",callback_data="menu:control")]]))
            except Exception: pass
            final_state=state
            if status=="ready": break
            if seen_active and unknown_after_active>=3 and elapsed>=30:
                final_state=dict(state); final_state["status"]="probable_ready"; break
            await asyncio.sleep(max(3,WATCH_INTERVAL_SEC))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("Generation watcher failed"); final_state={"status":"watch_error","error":str(e)}
    finally:
        application.bot_data["generation_watch_task"]=None; application.bot_data["generation_watch_stop"]=False
    elapsed=time.time()-started; status=(final_state or {}).get("status")
    if status in ("ready","probable_ready"):
        caption=("✅ خلص وقت الانتظار أو اختفت حالة التوليد.\n"+f"⏱ الوقت المسجل: {elapsed:.1f} ثانية.\n"+"🎮 هسه إنت علّمني التنزيل: افتح الموس واضغط Download/下载، وبعدها اختَر الصيغة إذا ظهرت. كل ضغطة تنحفظ.")
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("🎮 موس التنزيل",callback_data="menu:control"),InlineKeyboardButton("🔎 اضغط كتابة Download",callback_data="menu:textclick")],[InlineKeyboardButton("📦 حفظ وإرسال الجلسة",callback_data="menu:session")]])
        rel=final_event.get("screenshot_after") if final_event else None; path=SESSION_DIR/rel if rel else None
        if path and path.exists():
            try:
                with path.open("rb") as f: await application.bot.send_photo(chat_id=chat_id,photo=f,caption=caption[:1000],reply_markup=kb)
                return
            except Exception: pass
        await application.bot.send_message(chat_id=chat_id,text=caption,reply_markup=kb)
    else:
        await application.bot.send_message(chat_id=chat_id,text=f"⏹ توقفت المتابعة بعد {elapsed:.1f}ث. الجلسة محفوظة، وتگدر تفتح الموس.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎮 فتح الموس",callback_data="menu:control"),InlineKeyboardButton("📦 حفظ الجلسة",callback_data="menu:session")]]))


def start_generation_watch(context, chat_id:int):
    task=context.application.bot_data.get("generation_watch_task")
    if task and not task.done(): return False
    context.application.bot_data["generation_watch_stop"]=False
    context.application.bot_data["generation_watch_task"]=context.application.create_task(generation_watch_loop(context.application,chat_id))
    return True


async def maybe_start_watch_after_event(update:Update, context, event:dict):
    if (event or {}).get("intent_guess")!="generate_3d": return
    msg=update.effective_message or (update.callback_query.message if update.callback_query else None)
    if msg and start_generation_watch(context,msg.chat_id):
        await msg.reply_text("⏱ سجلت ضغطة Generate وبدأت متابعة التوليد تلقائياً بالخلفية. ما راح ألمس أي زر.")


def element_label(event: dict) -> str:
    el = event.get("element_before") or {}
    attrs = el.get("attrs") or {}
    for v in (el.get("text"), attrs.get("aria-label"), attrs.get("title"), attrs.get("name"), el.get("selector")):
        if v:
            return str(v).replace("\n", " ")[:180]
    return ""


def build_replay_plan(events: list) -> str:
    lines = [
        "import asyncio",
        "from playwright.async_api import async_playwright",
        "",
        "# Auto-generated from one manual Hunyuan training session.",
        "# It is a learning/replay plan: review selectors before using it unattended.",
        "# INPUT_IMAGE should point to the image you want to turn into 3D.",
        "INPUT_IMAGE = 'input.jpg'",
        "",
        "async def replay(page):",
    ]
    significant = [e for e in events if e.get("meaningful")]
    if not significant:
        lines.append("    pass")
    prev = None
    for e in significant:
        wait = e.get("wait_since_previous_meaningful_action_sec")
        if prev is not None and isinstance(wait, (int, float)) and wait >= 1.0:
            lines.append(f"    # Observed wait before this step: {wait:.1f} seconds")
            lines.append(f"    await page.wait_for_timeout({int(min(wait, 900)*1000)})")
        intent = e.get("intent_guess") or e.get("action")
        label = element_label(e).replace("\\", "\\\\").replace("'", "\\'")
        lines.append(f"    # step {e.get('seq')}: {intent} | {label}")
        action = e.get("action")
        el = e.get("element_before") or {}
        selector = (el.get("selector") or "").replace("\\", "\\\\").replace("'", "\\'")
        extra = e.get("extra") or {}
        if action == "upload_file":
            result_meta = e.get("result") if isinstance(e.get("result"), dict) else {}
            input_meta = extra.get("input") or result_meta or {}
            idx = int(input_meta.get("input_index", extra.get("input_index", 0)) or 0)
            lines.append(f"    await page.locator('input[type=\"file\"]').nth({idx}).set_input_files(INPUT_IMAGE)")
        elif action == "text_click":
            q = str(extra.get("query") or label).replace("\\", "\\\\").replace("'", "\\'")
            lines.append(f"    await page.get_by_text('{q}', exact=False).first.click()")
        elif action in ("click", "double_click"):
            if selector:
                cc = ", click_count=2" if action == "double_click" else ""
                lines.append(f"    await page.locator('{selector}').first.click({cc.lstrip(', ')})" if cc else f"    await page.locator('{selector}').first.click()")
            else:
                c = e.get("cursor") or {}
                lines.append(f"    await page.mouse.click({int(c.get('x',0))}, {int(c.get('y',0))}{', click_count=2' if action=='double_click' else ''})")
        elif action == "back":
            lines.append("    await page.go_back()")
        elif action == "reload":
            lines.append("    await page.reload()")
        elif action == "open_home":
            lines.append(f"    await page.goto('{HUNYUAN_URL}')")
        elif action == "type_text":
            txt = str(extra.get("text") or "").replace("\\", "\\\\").replace("'", "\\'")
            if selector:
                lines.append(f"    await page.locator('{selector}').first.fill('{txt}')")
            else:
                lines.append(f"    await page.keyboard.insert_text('{txt}')")
        elif action == "key":
            key = str(extra.get("key") or "Enter").replace("'", "")
            lines.append(f"    await page.keyboard.press('{key}')")
        else:
            lines.append("    # observation / unsupported manual action")
        prev = e
    lines += [
        "",
        "async def main():",
        "    async with async_playwright() as p:",
        "        browser = await p.chromium.launch(headless=False)",
        "        page = await browser.new_page()",
        f"        await page.goto('{HUNYUAN_URL}')",
        "        await replay(page)",
        "",
        "if __name__ == '__main__':",
        "    asyncio.run(main())",
        "",
    ]
    return "\n".join(lines)


async def send_event_after_photo(update: Update, event: dict, caption: str):
    rel = event.get("screenshot_after")
    if not rel:
        return
    path = SESSION_DIR / rel
    if not path.exists():
        return
    try:
        with path.open("rb") as f:
            await update.effective_message.reply_photo(f, caption=caption[:1000])
    except Exception as e:
        log.warning("Could not send post-action screenshot: %s", e)


async def export_session(update: Update, context):
    events = read_events()
    if not events:
        await update.effective_message.reply_text("📭 بعد ماكو خطوات محفوظة. افتح 🎮 التحكم وابدأ التدريب.")
        return
    meta = ensure_session()
    workflow = []
    for e in events:
        if not e.get("meaningful"):
            continue
        workflow.append({
            "seq": e.get("seq"),
            "time_utc": e.get("started_at_utc"),
            "elapsed_sec": e.get("elapsed_from_session_sec"),
            "wait_from_previous_meaningful_sec": e.get("wait_since_previous_meaningful_action_sec"),
            "action": e.get("action"),
            "intent_guess": e.get("intent_guess"),
            "element": element_label(e),
            "url_before": (e.get("page_before") or {}).get("url"),
            "url_after": (e.get("page_after") or {}).get("url"),
            "screenshot_before": e.get("screenshot_before"),
            "screenshot_after": e.get("screenshot_after"),
            "error": e.get("error"),
        })
    (SESSION_DIR / "workflow.json").write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
    (SESSION_DIR / "replay_plan.py").write_text(build_replay_plan(events), encoding="utf-8")
    summary = {
        "session_id": meta.get("session_id"),
        "started_at_utc": meta.get("started_at_utc"),
        "exported_at_utc": iso_utc(time.time()),
        "event_count": len(events),
        "meaningful_steps": len(workflow),
        "note": "Chrome cookies/profile are NOT included. This file contains only training actions, timings, element metadata and screenshots.",
    }
    (SESSION_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    readme = (
        "Hunyuan 3D Training Session\n"
        "===========================\n"
        "events.jsonl: full machine-readable timeline.\n"
        "workflow.json: compact ordered workflow with observed waits.\n"
        "replay_plan.py: code-like Playwright replay plan generated from the actions.\n"
        "screens/: before/after screenshots for real page actions.\n"
        "summary.json: session metadata.\n\n"
        "Security: browser cookies, passwords and the Chrome profile are NOT exported.\n"
    )
    (SESSION_DIR / "README.txt").write_text(readme, encoding="utf-8")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = JOBS_DIR / f"hunyuan-training-session-{stamp}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in SESSION_DIR.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(SESSION_DIR))
        last = context.application.bot_data.get("last_manual_image")
        if last and Path(last).exists():
            z.write(Path(last), f"input/{Path(last).name}")
    with out.open("rb") as f:
        await update.effective_message.reply_document(
            f,
            filename=out.name,
            caption=(
                f"📦 جلسة التدريب محفوظة\n"
                f"الخطوات: {len(workflow)}\n"
                "بيها الوقت بين الخطوات + العنصر اللي ضغطته + before/after screenshots + replay_plan.py.\n"
                "دز هذا الـZIP إلي حتى أبني الأتمتة النهائية."
            ),
        )


async def clear_session(update: Update, context):
    if SESSION_DIR.exists():
        shutil.rmtree(SESSION_DIR)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ensure_session()
    await update.effective_message.reply_text("🧹 بدأت جلسة تدريب جديدة. من هسه كل ضغطة حقيقية تنحفظ ويا وقتها ولقطاتها.")


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 التحكم بالموس", callback_data="menu:control"), InlineKeyboardButton("🔎 اضغط كتابة", callback_data="menu:textclick")],
        [InlineKeyboardButton("📎 رفع آخر صورة", callback_data="menu:upload"), InlineKeyboardButton("📸 لقطة + حفظ الوقت", callback_data="menu:snapshot")],
        [InlineKeyboardButton("⏳ متابعة التوليد", callback_data="menu:watchstart"), InlineKeyboardButton("🛑 إيقاف المتابعة", callback_data="menu:watchstop")],
        [InlineKeyboardButton("📦 حفظ وإرسال الجلسة", callback_data="menu:session")],
        [InlineKeyboardButton("🧹 جلسة جديدة", callback_data="menu:newsession"), InlineKeyboardButton("📊 الحالة", callback_data="menu:status")],
        [InlineKeyboardButton("🏠 فتح Hunyuan", callback_data="menu:open"), InlineKeyboardButton("🔐 تسجيل الدخول", callback_data="menu:login")],
    ])


def control_keyboard(step: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↖️", callback_data="ctl:ul"), InlineKeyboardButton("⬆️", callback_data="ctl:u"), InlineKeyboardButton("↗️", callback_data="ctl:ur")],
        [InlineKeyboardButton("⬅️", callback_data="ctl:l"), InlineKeyboardButton("🎯 اضغط", callback_data="ctl:click"), InlineKeyboardButton("➡️", callback_data="ctl:r")],
        [InlineKeyboardButton("↙️", callback_data="ctl:dl"), InlineKeyboardButton("⬇️", callback_data="ctl:d"), InlineKeyboardButton("↘️", callback_data="ctl:dr")],
        [
            InlineKeyboardButton(("✅ " if step == 5 else "") + "5px", callback_data="ctl:step5"),
            InlineKeyboardButton(("✅ " if step == 15 else "") + "15px", callback_data="ctl:step15"),
            InlineKeyboardButton(("✅ " if step == 40 else "") + "40px", callback_data="ctl:step40"),
            InlineKeyboardButton(("✅ " if step == 100 else "") + "100px", callback_data="ctl:step100"),
        ],
        [InlineKeyboardButton("🔎 ضغط بالنص", callback_data="ctl:textclick"), InlineKeyboardButton("📎 رفع الصورة", callback_data="ctl:upload")],
        [InlineKeyboardButton("⇧ سكرول", callback_data="ctl:su"), InlineKeyboardButton("⇩ سكرول", callback_data="ctl:sd"), InlineKeyboardButton("📸 لقطة", callback_data="ctl:snapshot")],
        [InlineKeyboardButton("⬅️ رجوع", callback_data="ctl:back"), InlineKeyboardButton("🔄 تحديث", callback_data="ctl:reload"), InlineKeyboardButton("🏠 Hunyuan", callback_data="ctl:home")],
        [InlineKeyboardButton("⌨️ كتابة", callback_data="ctl:type"), InlineKeyboardButton("↵ Enter", callback_data="ctl:enter"), InlineKeyboardButton("Esc", callback_data="ctl:esc")],
        [InlineKeyboardButton("⏳ تابع التوليد", callback_data="ctl:watchstart"), InlineKeyboardButton("🛑 وقف المتابعة", callback_data="ctl:watchstop")],
        [InlineKeyboardButton("📦 حفظ الجلسة", callback_data="ctl:session"), InlineKeyboardButton("🏠 القائمة", callback_data="ctl:menu")],
    ])


async def send_main_menu(update: Update, context, text=None):
    msg = update.effective_message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return
    ensure_session()
    await msg.reply_text(
        text or (
            "🧠 وضع تعليم Hunyuan\n\n"
            "ماكو أرقام وماكو شبكة مزدحمة.\n"
            "🎮 حرّك المؤشر بخطوات صغيرة، ومن تضغط على شي بالموقع أنا أحفظ:\n"
            "• العنصر الحقيقي وCSS selector\n"
            "• مكان الضغط\n"
            "• رابط الصفحة قبل وبعد\n"
            "• لقطة قبل الضغط ولقطة بعده\n"
            "• وقت الضغطة ومدة الانتظار من الخطوة السابقة\n\n"
            "من تضغط Generate، متابعة التوليد تشتغل تلقائياً وتسجل الوقت ولقطات دورية.\nمن يخلص، يرجعلك موس التنزيل حتى إنت تعلّمني زر Download.\n\nمن تكمل التنزيل اضغط 📦 حفظ وإرسال الجلسة."
        ),
        reply_markup=main_menu(),
    )


async def render_control(update: Update, context, query=None, note=""):
    browser = await get_browser(context)
    st = manual_state(context)
    vp = await browser.viewport()
    st["x"] = max(0, min(int(vp["w"]) - 1, int(st["x"])))
    st["y"] = max(0, min(int(vp["h"]) - 1, int(st["y"])))
    path = JOBS_DIR / f"control-{int(time.time()*1000)}.png"
    await browser.screenshot_cursor(path, int(st["x"]), int(st["y"]))
    meta = ensure_session()
    elapsed = time.time() - float(meta["started_at"])
    caption = (
        f"🎮 موس التدريب\n"
        f"📍 X={st['x']} Y={st['y']} | دقة الحركة {st['step']}px\n"
        f"⏱ الجلسة: {elapsed/60:.1f} دقيقة | خطوات محفوظة: {meta.get('meaningful_count',0)}\n"
        f"🌐 {(browser.page.url if browser.page else '')[:220]}"
    )
    if note:
        caption += "\n" + note[:700]
    kb = control_keyboard(int(st["step"]))
    try:
        if query:
            with path.open("rb") as f:
                try:
                    await query.edit_message_media(InputMediaPhoto(media=f, caption=caption), reply_markup=kb)
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
async def start_cmd(update: Update, context):
    await send_main_menu(update, context)


@owner_only
async def control_cmd(update: Update, context):
    await render_control(update, context)


@owner_only
async def login_cmd(update: Update, context):
    browser = await get_browser(context)
    if not browser.page or browser.page.url in ("", "about:blank"):
        try:
            await browser.page.goto(HUNYUAN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
    await update.effective_message.reply_text(
        "🔐 افتح شاشة المتصفح. إذا طلب تسجيل دخول كمله يدويًا.\n"
        f"{novnc_url()}\n\n"
        "جلسة Chrome نفسها تبقى داخل Volume /data، لكن ملف التدريب ما يصدّر كلمات السر أو الكوكيز."
    )


@owner_only
async def open_cmd(update: Update, context):
    browser = await get_browser(context)
    try:
        await browser.page.goto(HUNYUAN_URL, wait_until="domcontentloaded", timeout=60000)
        await browser.page.wait_for_timeout(900)
        await update.effective_message.reply_text("✅ فتحت Hunyuan.")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ تعذر فتح Hunyuan: {e}")


@owner_only
async def shot_cmd(update: Update, context):
    browser = await get_browser(context)
    st = manual_state(context)
    event, _ = await record_observation(context, "snapshot", {"source": "shot_command"})
    p = SESSION_DIR / str(event.get("screenshot_after") or "")
    if not p.exists():
        p = JOBS_DIR / f"shot-{int(time.time())}.png"
        await browser.screenshot_cursor(p, int(st["x"]), int(st["y"]))
    with p.open("rb") as f:
        await update.effective_message.reply_photo(f, caption=f"📸 لقطة محفوظة بالجلسة\n🌐 {browser.page.url if browser.page else ''}")


@owner_only
async def status_cmd(update: Update, context):
    browser = await get_browser(context)
    meta = ensure_session()
    elapsed = time.time() - float(meta["started_at"])
    last = meta.get("last_meaningful_at")
    wait = time.time() - float(last) if last else 0
    task = context.application.bot_data.get("generation_watch_task")
    watch = "شغالة 🟠" if task and not task.done() else "متوقفة ⚪"
    await update.effective_message.reply_text(
        f"🟢 وضع التدريب شغال\n"
        f"🧠 الجلسة: {meta.get('session_id')}\n"
        f"🧾 الخطوات المهمة: {meta.get('meaningful_count',0)}\n"
        f"⏱ مدة الجلسة: {elapsed/60:.1f} دقيقة\n"
        f"⏳ منذ آخر خطوة مهمة: {wait:.1f} ثانية\n"
        f"👁 متابعة التوليد: {watch}\n"
        f"🌐 {browser.page.url if browser.page else ''}",
        reply_markup=main_menu(),
    )


@owner_only
async def session_cmd(update: Update, context):
    await export_session(update, context)


@owner_only
async def new_session_cmd(update: Update, context):
    await clear_session(update, context)


@owner_only
async def control_callback(update: Update, context):
    q = update.callback_query
    if not q:
        return
    uid = q.from_user.id if q.from_user else 0
    if OWNER_ID and uid != OWNER_ID:
        await q.answer("هذا التحكم خاص بصاحب البوت", show_alert=True)
        return
    await q.answer()
    browser = await get_browser(context)
    st = manual_state(context)
    action = (q.data or "").split(":", 1)[-1]
    step = int(st["step"])
    move = {
        "u": (0, -step), "d": (0, step), "l": (-step, 0), "r": (step, 0),
        "ul": (-step, -step), "ur": (step, -step), "dl": (-step, step), "dr": (step, step),
    }
    note = ""
    async with browser.control_lock:
        try:
            if action in move:
                vp = await browser.viewport()
                dx, dy = move[action]
                old_x, old_y = int(st["x"]), int(st["y"])
                st["x"] = max(0, min(int(vp["w"]) - 1, int(st["x"]) + dx))
                st["y"] = max(0, min(int(vp["h"]) - 1, int(st["y"]) + dy))
                async def no_op_move():
                    return {"from": {"x": old_x, "y": old_y}, "to": {"x": int(st["x"]), "y": int(st["y"])}}
                await record_action(context, "cursor_move", no_op_move, extra={"dx": dx, "dy": dy}, screenshot=False, meaningful=False)
            elif action.startswith("step"):
                st["step"] = int(action.replace("step", ""))
                async def no_op_step():
                    return {"step_px": int(st["step"])}
                await record_action(context, "cursor_step", no_op_step, extra={"step_px": int(st["step"])}, screenshot=False, meaningful=False)
                note = f"🎯 الدقة صارت {st['step']}px"
            elif action in ("click", "dbl"):
                x, y = int(st["x"]), int(st["y"])
                el = await browser.element_at(x, y)
                async def do_click():
                    dl = await browser.click_xy(x, y, 2 if action == "dbl" else 1)
                    return {"download": str(dl) if dl else None}
                event, result = await record_action(
                    context,
                    "double_click" if action == "dbl" else "click",
                    do_click,
                    element=el,
                    extra={"click_count": 2 if action == "dbl" else 1},
                    screenshot=True,
                    meaningful=True,
                )
                label = element_label(event)
                note = (
                    f"✅ انحفظت الخطوة #{event['seq']} | 🎯 {event['intent_guess']}\n"
                    f"⏱ انتظار من آخر خطوة مهمة: {event.get('wait_since_previous_meaningful_action_sec') or 0:.1f}ث\n"
                    f"🔎 {label[:180]}"
                )
                await maybe_start_watch_after_event(update, context, event)
                dl = (result or {}).get("download") if isinstance(result, dict) else None
                if dl and Path(dl).exists():
                    with Path(dl).open("rb") as f:
                        await q.message.reply_document(f, filename=Path(dl).name, caption="📦 التقطت ملف التنزيل وسجلت الخطوة.")
                    await q.message.reply_text("✅ هسه الجلسة كاملة تقريباً. اضغط حفظ وإرسال الجلسة حتى تدزها إلي.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 حفظ وإرسال الجلسة", callback_data="menu:session")]]))
            elif action == "su" or action == "sd":
                delta = -520 if action == "su" else 520
                async def do_scroll():
                    await browser.page.mouse.move(int(st["x"]), int(st["y"]))
                    await browser.page.mouse.wheel(0, delta)
                    await browser.page.wait_for_timeout(350)
                    return {"delta": delta}
                event, _ = await record_action(context, "scroll", do_scroll, extra={"delta": delta}, screenshot=True, meaningful=False)
                note = f"✅ سكرول محفوظ كحدث #{event['seq']}"
            elif action == "snapshot":
                event, _ = await record_observation(context, "snapshot", {"source": "control"})
                wait = event.get("wait_since_previous_meaningful_action_sec")
                note = f"📸 لقطة محفوظة | ⏳ من آخر خطوة مهمة: {(wait or 0):.1f}ث"
            elif action == "back":
                async def do_back():
                    try:
                        await browser.page.go_back(wait_until="domcontentloaded", timeout=12000)
                    except Exception:
                        pass
                    return None
                event, _ = await record_action(context, "back", do_back, screenshot=True, meaningful=True)
                note = f"✅ رجوع محفوظ #{event['seq']}"
            elif action == "reload":
                async def do_reload():
                    try:
                        await browser.page.reload(wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    return None
                event, _ = await record_action(context, "reload", do_reload, screenshot=True, meaningful=True)
                note = f"✅ تحديث محفوظ #{event['seq']}"
            elif action == "home":
                async def do_home():
                    await browser.page.goto(HUNYUAN_URL, wait_until="domcontentloaded", timeout=60000)
                    return None
                event, _ = await record_action(context, "open_home", do_home, extra={"target": HUNYUAN_URL}, screenshot=True, meaningful=True)
                note = f"✅ فتح Hunyuan محفوظ #{event['seq']}"
            elif action == "upload":
                last = context.application.bot_data.get("last_manual_image")
                if not last or not Path(last).exists():
                    note = "⚠️ أرسل صورة للبوت أولاً."
                else:
                    async def do_upload():
                        return await browser.upload_last_image(Path(last))
                    event, result = await record_action(
                        context, "upload_file", do_upload,
                        extra={"file_name": Path(last).name, "input": {}},
                        screenshot=True, meaningful=True,
                    )
                    if isinstance(result, dict):
                        event["extra"]["input"] = result
                        # rewrite latest line is intentionally avoided; result already contains full input metadata.
                    note = (
                        f"📎 الصورة انرفعت وانحفظت الخطوة #{event['seq']}\n"
                        f"⏱ الانتظار من آخر خطوة: {event.get('wait_since_previous_meaningful_action_sec') or 0:.1f}ث"
                    )
            elif action == "textclick":
                context.user_data["awaiting_text"] = "text_click"
                await q.message.reply_text("🔎 اكتب النص الظاهر على Hunyuan، مثلاً: 立即生成 أو 下载")
                return
            elif action == "type":
                context.user_data["awaiting_text"] = "type_text"
                await q.message.reply_text("⌨️ ارسل النص هسه، راح أكتبه بمكان المؤشر وأسجل الخطوة.")
                return
            elif action in ("enter", "esc"):
                key = "Enter" if action == "enter" else "Escape"
                el = await browser.element_at(int(st["x"]), int(st["y"]))
                async def do_key():
                    await browser.page.keyboard.press(key)
                    return {"key": key}
                event, _ = await record_action(context, "key", do_key, element=el, extra={"key": key}, screenshot=True, meaningful=True)
                note = f"⌨️ {key} محفوظ #{event['seq']}"
            elif action == "watchstart":
                note = "⏳ بدأت متابعة التوليد بالخلفية." if start_generation_watch(context, q.message.chat_id) else "ℹ️ المتابعة شغالة أصلاً."
            elif action == "watchstop":
                context.application.bot_data["generation_watch_stop"] = True
                note = "🛑 طلبت إيقاف المتابعة. الجلسة تبقى محفوظة."
            elif action == "session":
                await export_session(update, context)
                return
            elif action == "menu":
                await send_main_menu(update, context, "🏠 القائمة")
                return
        except Exception as e:
            log.exception("Control action failed")
            note = f"❌ {e}"
    try:
        await render_control(update, context, query=q, note=note)
    except Exception as e:
        log.warning("Control refresh failed after action: %s", e)
        await q.message.reply_text(note + "\n⚠️ الضغطة نفسها انحفظت، بس تحديث صورة التحكم تأخر.")


@owner_only
async def menu_callback(update: Update, context):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    action = (q.data or "").split(":", 1)[-1]
    if action == "control":
        await render_control(update, context, query=q)
    elif action == "textclick":
        context.user_data["awaiting_text"] = "text_click"
        await q.message.reply_text("🔎 اكتب أي نص ظاهر على الموقع، وأنا أضغط أفضل تطابق وأسجل before/after والوقت.")
    elif action == "upload":
        browser = await get_browser(context)
        last = context.application.bot_data.get("last_manual_image")
        if not last or not Path(last).exists():
            await q.message.reply_text("⚠️ أرسل صورة للبوت أولاً.")
        else:
            async def do_upload():
                return await browser.upload_last_image(Path(last))
            try:
                event, _ = await record_action(context, "upload_file", do_upload, extra={"file_name": Path(last).name}, screenshot=True, meaningful=True)
                await q.message.reply_text(f"📎 تم الرفع وحفظ الخطوة #{event['seq']}.", reply_markup=main_menu())
                await send_event_after_photo(update, event, f"📸 بعد الخطوة #{event['seq']} — {event['intent_guess']}")
            except Exception as e:
                await q.message.reply_text(f"❌ الرفع فشل: {e}", reply_markup=main_menu())
    elif action == "snapshot":
        event, _ = await record_observation(context, "snapshot", {"source": "menu"})
        wait = event.get("wait_since_previous_meaningful_action_sec") or 0
        await q.message.reply_text(f"📸 حفظت لقطة. مرّ {wait:.1f} ثانية من آخر خطوة مهمة.", reply_markup=main_menu())
    elif action == "watchstart":
        if start_generation_watch(context, q.message.chat_id):
            await q.message.reply_text("⏳ بدأت متابعة التوليد. أسجل الوقت ولقطات دورية بدون أي ضغط على الموقع.", reply_markup=main_menu())
        else:
            await q.message.reply_text("ℹ️ متابعة التوليد شغالة أصلاً.", reply_markup=main_menu())
    elif action == "watchstop":
        context.application.bot_data["generation_watch_stop"] = True
        await q.message.reply_text("🛑 راح أوقف المتابعة بأقرب دورة فحص. الجلسة ما تنمسح.", reply_markup=main_menu())
    elif action == "session":
        await export_session(update, context)
    elif action == "newsession":
        await clear_session(update, context)
    elif action == "status":
        await status_cmd(update, context)
    elif action == "open":
        await open_cmd(update, context)
    elif action == "login":
        await login_cmd(update, context)


@owner_only
async def text_handler(update: Update, context):
    text = (update.effective_message.text or "").strip()
    mode = context.user_data.pop("awaiting_text", None)
    if not mode:
        await update.effective_message.reply_text("استخدم الأزرار من /start. إذا تريد تضغط كتابة اضغط 🔎 اضغط كتابة.", reply_markup=main_menu())
        return
    browser = await get_browser(context)
    st = manual_state(context)
    if mode == "text_click":
        target = await browser.find_text_target(text)
        if not target:
            await update.effective_message.reply_text(f"❌ ما لكيت النص: {text}\nجرّب جزء أقصر من الكتابة.", reply_markup=main_menu())
            return
        async def do_text_click():
            dl = await browser.click_xy(int(target["x"]), int(target["y"]), 1)
            return {"download": str(dl) if dl else None}
        try:
            event, result = await record_action(
                context, "text_click", do_text_click,
                element=target, extra={"query": text, "matched_text": target.get("matched_text")},
                screenshot=True, meaningful=True,
            )
            note = (
                f"✅ ضغطت «{(target.get('matched_text') or target.get('text') or text)[:160]}»\n"
                f"🧠 انحفظت خطوة #{event['seq']} كـ {event['intent_guess']}\n"
                f"⏱ الانتظار من آخر خطوة مهمة: {event.get('wait_since_previous_meaningful_action_sec') or 0:.1f}ث"
            )
            await update.effective_message.reply_text(note, reply_markup=main_menu())
            await maybe_start_watch_after_event(update, context, event)
            await send_event_after_photo(update, event, f"📸 بعد الخطوة #{event['seq']} — {event['intent_guess']}")
            dl = (result or {}).get("download") if isinstance(result, dict) else None
            if dl and Path(dl).exists():
                with Path(dl).open("rb") as f:
                    await update.effective_message.reply_document(f, filename=Path(dl).name, caption="📦 التقطت التنزيل وسجلته.")
                await update.effective_message.reply_text("✅ حفظت التنزيل. هسه اضغط حفظ وإرسال الجلسة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 حفظ وإرسال الجلسة", callback_data="menu:session")]]))
        except Exception as e:
            await update.effective_message.reply_text(f"❌ الضغط فشل: {e}", reply_markup=main_menu())
    elif mode == "type_text":
        el = await browser.element_at(int(st["x"]), int(st["y"]))
        async def do_type():
            await browser.page.mouse.click(int(st["x"]), int(st["y"]))
            await browser.page.keyboard.insert_text(text)
            return {"text_length": len(text)}
        try:
            event, _ = await record_action(context, "type_text", do_type, element=el, extra={"text": text[:500]}, screenshot=True, meaningful=True)
            await update.effective_message.reply_text(f"⌨️ تمت الكتابة وحفظ الخطوة #{event['seq']}.", reply_markup=main_menu())
            await send_event_after_photo(update, event, f"📸 بعد الخطوة #{event['seq']} — كتابة")
        except Exception as e:
            await update.effective_message.reply_text(f"❌ الكتابة فشلت: {e}", reply_markup=main_menu())


async def download_image(update: Update, context) -> Optional[Path]:
    msg = update.effective_message
    if msg.photo:
        tg = await msg.photo[-1].get_file()
        ext = ".jpg"
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
        tg = await msg.document.get_file()
        ext = Path(msg.document.file_name or "image.png").suffix or ".png"
    else:
        return None
    path = JOBS_DIR / f"input-{msg.message_id}-{int(time.time())}{ext}"
    await tg.download_to_drive(custom_path=str(path))
    return path


@owner_only
async def image_handler(update: Update, context):
    path = await download_image(update, context)
    if not path:
        return
    old = context.application.bot_data.get("last_manual_image")
    if old and Path(old).exists() and Path(old) != path:
        try:
            Path(old).unlink()
        except Exception:
            pass
    context.application.bot_data["last_manual_image"] = str(path)
    ensure_session()
    await update.effective_message.reply_text(
        "✅ خزنت الصورة فقط. ما ضغطت أي شي بالموقع.\n"
        "هسه افتح 🎮 التحكم واضغط 📎 رفع الصورة، وبعدها كمل خطواتك يدويًا.\n"
        "كل ضغطة حقيقية راح تنحفظ ويا الوقت ولقطة قبل/بعد.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 التحكم", callback_data="menu:control"), InlineKeyboardButton("📎 رفع آخر صورة", callback_data="menu:upload")],
            [InlineKeyboardButton("📦 حفظ الجلسة", callback_data="menu:session")],
        ]),
    )


async def post_init(app: Application):
    browser = Browser()
    await browser.start()
    app.bot_data["browser"] = browser
    vp = await browser.viewport()
    app.bot_data["manual_state"] = {"x": int(vp["w"]) // 2, "y": int(vp["h"]) // 2, "step": 15}
    app.bot_data["generation_watch_task"] = None
    app.bot_data["generation_watch_stop"] = False
    ensure_session()
    log.info("Trainer started | noVNC=%s", novnc_url())


async def post_shutdown(app: Application):
    app.bot_data["generation_watch_stop"] = True
    task=app.bot_data.get("generation_watch_task")
    if task and not task.done():
        task.cancel()
        try: await task
        except BaseException: pass
    browser = app.bot_data.get("browser")
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
    app.add_handler(CommandHandler("control", control_cmd))
    app.add_handler(CommandHandler("session", session_cmd))
    app.add_handler(CommandHandler("newsession", new_session_cmd))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("open", open_cmd))
    app.add_handler(CommandHandler("shot", shot_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(control_callback, pattern=r"^ctl:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, image_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
