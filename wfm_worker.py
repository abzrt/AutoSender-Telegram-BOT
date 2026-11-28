"""Standalone WFM FLOW Telkom Akses Worker berbasis Playwright (Headless Chromium).

Fitur:
- Menjaga 1 sesi browser FLOW aktif 24/7 di server (storage_state disimpan ke disk).
- Jika sesi kadaluarsa, otomatis login dan meminta 6 digit OTP ke Admin via Telegram (/otp 123456).
- Membuka langsung https://flow.telkomakses.co.id/EditWorkorder/<WONUM>,
  mengekstrak data asli tanpa sensor bintang (*), dan mengambil screenshot HD 2x Retina
  khusus kartu Workorder (dari tab Workorder s/d Product Name:).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger("telegram-autosender.wfm")

FLOW_BASE_URL = "https://flow.telkomakses.co.id"
FLOW_EDIT_WO_URL = f"{FLOW_BASE_URL}/EditWorkorder/"


def extract_otp_from_message_text(text: str) -> str:
    """Ekstrak 6 digit kode OTP dari teks pesan @HCMTelkomAksesBot."""
    raw = text or ""
    near = re.search(r"(?:otp|kode|code|token|pin)[^\d]{0,30}(?<!\d)(\d{6})(?!\d)", raw, re.IGNORECASE)
    if near:
        return near.group(1)
    any6 = re.search(r"(?<!\d)(\d{6})(?!\d)", raw)
    return any6.group(1) if any6 else ""


def unmask_caption_with_flow_data(raw_caption: str, card_info: dict[str, Any]) -> str:
    """Ganti field bersensor bintang (*) pada teks Salin ADING dengan data asli dari WFM FLOW."""
    if not raw_caption:
        return ""
    flow_cust = str(card_info.get("customer_name") or "").strip()
    flow_addr = str(card_info.get("service_address") or "").strip()
    flow_contact = str(card_info.get("contact_number") or "").strip()
    flow_srv = str(card_info.get("service_number") or "").strip()

    out_lines: list[str] = []
    for line in raw_caption.splitlines():
        upper = line.strip().upper()
        if upper.startswith("CUSTOMER NAME:") and flow_cust and "*" not in flow_cust:
            out_lines.append(f"CUSTOMER NAME: {flow_cust}")
        elif upper.startswith("ALAMAT:") and flow_addr and "*" not in flow_addr:
            out_lines.append(f"ALAMAT: {flow_addr}")
        elif upper.startswith("CONTACT NUMBER:") and "*" in line and flow_contact and "*" not in flow_contact:
            out_lines.append(f"CONTACT NUMBER: {flow_contact}")
        elif upper.startswith("NO INTERNET:") and "*" in line and flow_srv and "*" not in flow_srv:
            out_lines.append(f"NO INTERNET: {flow_srv}")
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def build_flow_summary_caption(wonum: str, card_info: dict[str, Any]) -> str:
    """Susun caption format standar ADING Mobile jika tidak ada teks Salin ADING (misal dari /wo langsung)."""
    def _v(key: str, default: str = "-") -> str:
        val = str(card_info.get(key) or "").strip()
        return val if val else default

    return "\n".join(
        [
            f"NO SC: {_v('sc')}",
            f"WONUM: {_v('wo', wonum)}",
            f"BOOKING DATE: {_v('booking_date')}",
            f"STO: {_v('sto')}",
            f"PAKET: {_v('paket')}",
            f"NO INTERNET: {_v('service_number')}",
            f"CUSTOMER NAME: {_v('customer_name')}",
            f"CONTACT NUMBER: {_v('contact_number')}",
            f"ALAMAT: {_v('service_address')}",
            f"ODP: {_v('odp')}",
            f"KORDINAT: {_v('koordinat')}",
        ]
    )


class WfmFlowWorker:
    """Persistent Playwright Chromium worker untuk WFM FLOW."""

    def __init__(self, state_dir: Path) -> None:
        self.state_file = state_dir / "wfm_storage_state.json"
        self.flow_user = os.getenv("FLOW_USER", "16830482").strip()
        self.flow_pass = os.getenv("FLOW_PASS", "narindo1").strip()
        self._lock = asyncio.Lock()
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._otp_future: asyncio.Future[str] | None = None
        self.waiting_for_otp: bool = False
        self.session_active: bool = False
        self.last_keepalive_at: str = "-"
        self.dispatches_today: int = 0
        self._dispatch_date: str = ""
        self._disconnect_notified: bool = False

    def _now_wita(self) -> datetime:
        return datetime.now(timezone(timedelta(hours=8)))

    def _record_dispatch(self) -> None:
        today = self._now_wita().strftime("%Y-%m-%d")
        if self._dispatch_date != today:
            self._dispatch_date = today
            self.dispatches_today = 0
        self.dispatches_today += 1

    def get_status(self) -> dict[str, Any]:
        today = self._now_wita().strftime("%Y-%m-%d")
        count = self.dispatches_today if self._dispatch_date == today else 0
        return {
            "session_active": self.session_active,
            "waiting_for_otp": self.waiting_for_otp,
            "last_keepalive_at": self.last_keepalive_at,
            "dispatches_today": count,
        }

    def submit_otp(self, code: str) -> bool:
        """Terima kode OTP 6 digit dari perintah /otp Telegram."""
        clean = re.sub(r"\D+", "", code or "")
        if len(clean) < 4:
            return False
        if self._otp_future and not self._otp_future.done():
            self._otp_future.set_result(clean)
            return True
        return False

    async def _ensure_browser(self) -> Any:
        if self._page and not self._page.is_closed():
            return self._page
        from playwright.async_api import async_playwright

        if not self._pw:
            self._pw = await async_playwright().start()
        if not self._browser or not self._browser.is_connected():
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
        ctx_kwargs: dict[str, Any] = {
            "viewport": {"width": 1366, "height": 950},
            "device_scale_factor": 2,
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if self.state_file.exists():
            ctx_kwargs["storage_state"] = str(self.state_file)
        self._context = await self._browser.new_context(**ctx_kwargs)
        self._page = await self._context.new_page()
        return self._page

    async def _save_state(self) -> None:
        if self._context:
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                await self._context.storage_state(path=str(self.state_file))
            except Exception as exc:
                logger.warning("Gagal menyimpan wfm_storage_state.json: %s", exc)

    async def _fetch_otp_via_telethon(self, since_utc: datetime, timeout_seconds: int = 30) -> str:
        """Baca OTP 6 digit otomatis dari @HCMTelkomAksesBot memakai Telethon StringSession."""
        api_id_raw = os.getenv("TG_API_ID", "").strip()
        api_hash = os.getenv("TG_API_HASH", "").strip()
        session_str = os.getenv("TG_STRING_SESSION", "").strip()
        otp_bot = os.getenv("TG_OTP_BOT_USERNAME", "HCMTelkomAksesBot").strip().lstrip("@")
        if not (api_id_raw and api_hash and session_str):
            return ""
        try:
            api_id = int(api_id_raw)
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except Exception as exc:
            logger.warning("[WFM] Telethon tidak siap: %s", exc)
            return ""

        client = TelegramClient(StringSession(session_str), api_id, api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning("[WFM] TG_STRING_SESSION tidak valid / belum authorized.")
                return ""
            deadline = asyncio.get_running_loop().time() + max(5, timeout_seconds)
            while asyncio.get_running_loop().time() < deadline:
                msgs = await client.get_messages(otp_bot, limit=5)
                for msg in msgs:
                    msg_dt = getattr(msg, "date", None)
                    if msg_dt is None:
                        continue
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                    else:
                        msg_dt = msg_dt.astimezone(timezone.utc)
                    if msg_dt < since_utc:
                        continue
                    code = extract_otp_from_message_text(getattr(msg, "message", "") or "")
                    if code:
                        logger.info("[WFM] Auto-OTP tertangkap otomatis dari @%s!", otp_bot)
                        return code
                await asyncio.sleep(1.5)
        except Exception as exc:
            logger.warning("[WFM] Gagal auto-fetch OTP via Telethon: %s", exc)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
        return ""

    async def _perform_login_and_otp(
        self,
        page: Any,
        notify_otp_cb: Callable[[str], Awaitable[None]] | None,
    ) -> None:
        """Lakukan login FLOW dan tunggu kode OTP (auto via Telethon @HCMTelkomAksesBot atau fallback manual)."""
        otp_requested_utc = datetime.now(timezone.utc) - timedelta(seconds=10)
        await page.goto(f"{FLOW_BASE_URL}/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(0.8)

        # Jika ternyata sudah login dan diarahkan ke /home atau /workorder
        if any(p in page.url.lower() for p in ("/home", "/dashboard", "/workorder", "/editworkorder")):
            await self._save_state()
            return

        # Bersihkan stale storage sebelum submit login
        await page.evaluate("() => { try { localStorage.removeItem('menus'); localStorage.removeItem('username'); } catch(e){} }")

        user_inp = page.locator("#inputEmail4, input[name='username']").first
        pass_inp = page.locator("#inputPassword4, input[name='password']").first
        if await user_inp.count() > 0 and await pass_inp.count() > 0:
            await user_inp.fill(self.flow_user)
            await pass_inp.fill(self.flow_pass)
            await page.evaluate(
                """() => {
                    const rem = document.getElementById('gridCheck');
                    if (rem) { rem.setAttribute('name', 'remember'); rem.name = 'remember'; rem.value = '1'; rem.checked = true; }
                }"""
            )
            submit_btn = page.locator("#loginForm button[type='submit'], button.btn-danger[type='submit']").first
            otp_requested_utc = datetime.now(timezone.utc) - timedelta(seconds=5)
            await submit_btn.click()

            # Tunggu sampai pindah ke /otp atau muncul error password salah
            for _ in range(30):
                await asyncio.sleep(0.5)
                cur_url = page.url.lower()
                if "/otp" in cur_url:
                    break
                if any(p in cur_url for p in ("/home", "/dashboard", "/workorder")):
                    await self._save_state()
                    return
                try:
                    err_txt = await page.evaluate(
                        "() => ((document.querySelector('.swal2-html-container, .swal2-content') || {}).innerText || '')"
                    )
                except Exception:
                    continue
                if "password salah" in err_txt.lower():
                    raise RuntimeError(f"Password FLOW salah untuk akun {self.flow_user}! Update env FLOW_PASS.")

        # Jika sekarang di /otp, coba auto-baca dari @HCMTelkomAksesBot via Telethon dulu; jika gagal fallback manual
        if "/otp" in page.url.lower() or await page.locator("#otp, input[name='otp']").count() > 0:
            otp_code = await self._fetch_otp_via_telethon(otp_requested_utc, timeout_seconds=30)
            if not otp_code:
                loop = asyncio.get_running_loop()
                self._otp_future = loop.create_future()
                self.waiting_for_otp = True
                try:
                    if notify_otp_cb:
                        await notify_otp_cb(
                            "🔐 <b>WFM FLOW Membutuhkan Kode OTP!</b>\n\n"
                            "Kode OTP telah dikirim oleh FLOW ke Telegram Anda.\n"
                            "Balas pesan ini dengan perintah:\n"
                            "<code>/otp 123456</code>\n\n"
                            "<i>(Batas waktu 3 menit. Setelah berhasil, sesi akan tersimpan otomatis di server.)</i>"
                        )
                    otp_code = await asyncio.wait_for(self._otp_future, timeout=180)
                finally:
                    self.waiting_for_otp = False
                    self._otp_future = None

            otp_inp = page.locator("#otp, input[name='otp']").first
            await otp_inp.click()
            await otp_inp.fill(otp_code)
            await otp_inp.dispatch_event("input")
            await otp_inp.dispatch_event("change")

            sign_btn = page.locator("#signInButton, button:has-text('Sign In'), button[type='submit']").first
            if await sign_btn.count() > 0:
                await sign_btn.click()
            else:
                await otp_inp.press("Enter")

            # Tunggu sampai benar-benar masuk ke /home atau /dashboard setelah OTP
            otp_verified = False
            last_swal = ""
            for _ in range(40):
                await asyncio.sleep(0.5)
                cur_u = page.url.lower()
                if any(p in cur_u for p in ("/home", "/dashboard", "/workorder", "/editworkorder")):
                    otp_verified = True
                    break
                try:
                    swal_txt = await page.evaluate(
                        "() => ((document.querySelector('.swal2-html-container, .swal2-content, .swal2-title') || {}).innerText || '').trim()"
                    )
                    if swal_txt:
                        last_swal = swal_txt
                        low = swal_txt.lower()
                        if any(k in low for k in ("salah", "invalid", "expired", "gagal", "error")):
                            raise RuntimeError(f"OTP ditolak oleh FLOW: {swal_txt}")
                except RuntimeError:
                    raise
                except Exception:
                    continue

            if not otp_verified:
                raise RuntimeError(f"Verifikasi OTP belum berhasil keluar dari /otp (URL: {page.url}, pesan: {last_swal or '-'}). Coba lagi.")

            # Tunggu /home selesai memuat sesi & localStorage.menus sebelum pindah ke /EditWorkorder
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            has_menus = False
            for _ in range(16):
                try:
                    has_menus = bool(await page.evaluate("() => Boolean(localStorage.getItem('menus'))"))
                    if has_menus:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.4)

            try:
                cookies_cnt = len(await self._context.cookies()) if self._context else 0
            except Exception:
                cookies_cnt = -1
            logger.info("[WFM] Post-OTP ready: url=%s cookies=%d has_menus=%s", page.url, cookies_cnt, has_menus)
            await self._save_state()

    async def capture_workorder(
        self,
        wonum: str,
        notify_otp_cb: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Buka /EditWorkorder/<wonum>, ekstrak data asli tanpa sensor, & ambil SS HD 2x Retina."""
        clean_wo = wonum.strip().upper()
        if not clean_wo:
            raise ValueError("WONUM tidak boleh kosong.")

        async with self._lock:
            page = await self._ensure_browser()
            target_url = f"{FLOW_EDIT_WO_URL}{clean_wo}"

            # Buka /home dulu agar sesi & Referer selalu valid (jangan langsung cold-goto /EditWorkorder)
            cur = page.url.lower()
            if not any(p in cur for p in ("/home", "/dashboard", "/workorder", "/editworkorder")):
                await page.goto(f"{FLOW_BASE_URL}/home", wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(0.8)
                cur = page.url.lower()
                logger.info("[WFM] Check /home -> %s", page.url)

            if "/unauthorize" in cur or cur.rstrip("/") == FLOW_BASE_URL or "/otp" in cur or "/login" in cur or "about:blank" in cur:
                await self._perform_login_and_otp(page, notify_otp_cb)

            # Sekarang sudah pasti di halaman terautentikasi. Kembalikan navbar jika sebelumnya disembunyikan saat SS.
            target_wo_path = f"/editworkorder/{clean_wo.lower()}"
            if target_wo_path not in page.url.lower():
                try:
                    await page.evaluate(
                        "() => document.querySelectorAll('nav, header, .navbar').forEach(n => n.style.display = '')"
                    )
                except Exception:
                    pass

                opened_via_search = False
                try:
                    search_inp = page.locator("#searchWonum").first
                    if await search_inp.count() > 0:
                        await search_inp.click(timeout=5000)
                        await search_inp.fill(clean_wo)
                        await search_inp.press("Enter")
                        for _ in range(16):
                            await asyncio.sleep(0.5)
                            if target_wo_path in page.url.lower():
                                opened_via_search = True
                                break
                            if "/unauthorize" in page.url.lower():
                                break
                        logger.info("[WFM] After #searchWonum(%s) -> %s (ok=%s)", clean_wo, page.url, opened_via_search)
                except Exception as srch_exc:
                    logger.warning("[WFM] #searchWonum error: %s", srch_exc)

                # Jika belum masuk ke /EditWorkorder/<WO_BARU>, navigasi dengan Referer dari halaman saat ini / /home
                if not opened_via_search and target_wo_path not in page.url.lower():
                    try:
                        await page.goto(
                            target_url,
                            wait_until="domcontentloaded",
                            referer=f"{FLOW_BASE_URL}/home",
                            timeout=30000,
                        )
                    except Exception as nav_exc:
                        if "ERR_ABORTED" in str(nav_exc):
                            await asyncio.sleep(1.5)
                            await page.goto(
                                target_url,
                                wait_until="domcontentloaded",
                                referer=f"{FLOW_BASE_URL}/home",
                                timeout=30000,
                            )
                        else:
                            raise
                    await asyncio.sleep(0.8)
                    logger.info("[WFM] Referer goto %s -> %s", target_url, page.url)

            # Tunggu kartu Workorder selesai render
            card_ready = False
            for _ in range(30):
                try:
                    txt = await page.evaluate("() => (document.body && document.body.innerText) || ''")
                    if any(k in txt for k in ("OSS Order ID", "Service Number", "Customer Name")):
                        card_ready = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.4)

            if not card_ready:
                raise RuntimeError(f"Kartu Workorder {clean_wo} tidak muncul di FLOW (URL: {page.url}).")

            await asyncio.sleep(0.3)

            # Tahap 1: Sembunyikan navbar & scroll ke tab Workorder dulu agar paint selesai sebelum ukur clip
            await page.evaluate(
                """() => {
                    document.querySelectorAll('nav, header, .navbar').forEach(n => n.style.display = 'none');
                    let tabBtnEl = null, redBarEl = null;
                    for (const sn of document.querySelectorAll('a, button, li, div, label, span, h5, h6')) {
                        if (sn.children.length > 2) continue;
                        const st = (sn.innerText || '').trim();
                        if (!tabBtnEl && (st === 'Related Record' || st === 'Plans')) tabBtnEl = sn;
                        if (!redBarEl && st === 'Workorder' && sn.getBoundingClientRect().width > 400) redBarEl = sn;
                    }
                    const anchorEl = tabBtnEl || redBarEl || document.querySelector('main form');
                    if (anchorEl) {
                        const absTop = anchorEl.getBoundingClientRect().top + window.scrollY;
                        window.scrollTo(0, Math.max(0, absTop - 12));
                    }
                }"""
            )
            await asyncio.sleep(0.25)

            # Tahap 2: Ekstrak data & hitung koordinat clip setelah scroll benar-benar ter-render
            card_info: dict[str, Any] = await page.evaluate(
                """() => {
                    function getValByLabel(lbl) {
                        const nodes = document.querySelectorAll('label, div, span, td, th');
                        for (const n of nodes) {
                            if (n.children.length > 1) continue;
                            const t = (n.innerText || n.textContent || '').trim();
                            if (t.toLowerCase().includes(lbl.toLowerCase())) {
                                const sib = n.nextElementSibling;
                                if (sib) {
                                    const inp = sib.matches('input,textarea,select') ? sib : sib.querySelector('input,textarea,select,.form-control');
                                    if (inp) return (inp.value || inp.innerText || '').trim();
                                }
                                const p = n.parentElement;
                                if (p) {
                                    const pInps = p.querySelectorAll('input,textarea,select,.form-control');
                                    if (pInps.length === 1 && pInps[0] !== n) return (pInps[0].value || pInps[0].innerText || '').trim();
                                    const psib = p.nextElementSibling;
                                    if (psib) {
                                        const inp3 = psib.matches('input,textarea,select') ? psib : psib.querySelector('input,textarea,select,.form-control');
                                        if (inp3) return (inp3.value || inp3.innerText || '').trim();
                                    }
                                }
                            }
                        }
                        return '';
                    }

                    const wo = getValByLabel('Workorder:');
                    const sc = getValByLabel('SC Order No');
                    const sto = getValByLabel('Workzone');
                    const bookingDate = getValByLabel('Scheduled Start');
                    const paket = getValByLabel('Product Name');
                    const srv = getValByLabel('Service Number');
                    const contact = getValByLabel('Contact Number');
                    const cust = getValByLabel('Customer Name');
                    const addr = getValByLabel('Service Address');

                    let tabBtnEl = null, redBarEl = null, prodLblEl = null, schedEl = null;
                    for (const sn of document.querySelectorAll('a, button, li, div, label, span, h5, h6')) {
                        if (sn.children.length > 2) continue;
                        const st = (sn.innerText || '').trim();
                        if (!tabBtnEl && (st === 'Related Record' || st === 'Plans')) tabBtnEl = sn;
                        if (!redBarEl && st === 'Workorder' && sn.getBoundingClientRect().width > 400) redBarEl = sn;
                        if (!prodLblEl && st.toLowerCase().startsWith('product name')) prodLblEl = sn;
                        if (!schedEl && st === 'Scheduling Information') schedEl = sn;
                    }

                    let clip = null;
                    const anchorEl = tabBtnEl || redBarEl || document.querySelector('main form');
                    if (anchorEl) {
                        const br = (redBarEl || anchorEl).getBoundingClientRect();
                        const topY = Math.max(0, tabBtnEl ? (tabBtnEl.getBoundingClientRect().top - 8) : (br.top - 8));
                        let botY = br.bottom + 420;
                        if (prodLblEl) {
                            let prodBottom = prodLblEl.getBoundingClientRect().bottom;
                            const pSib = prodLblEl.nextElementSibling;
                            if (pSib) prodBottom = Math.max(prodBottom, pSib.getBoundingClientRect().bottom);
                            else if (prodLblEl.parentElement && prodLblEl.parentElement.nextElementSibling) {
                                prodBottom = Math.max(prodBottom, prodLblEl.parentElement.nextElementSibling.getBoundingClientRect().bottom);
                            }
                            botY = prodBottom + 18;
                        }
                        if (schedEl) {
                            const schedTop = schedEl.getBoundingClientRect().top - 10;
                            if (schedTop > topY + 180) botY = prodLblEl ? Math.min(botY, schedTop) : schedTop;
                        }
                        const leftX = Math.max(0, br.left - 16);
                        const rightX = Math.min(window.innerWidth, br.right + 16);
                        clip = {
                            x: leftX,
                            y: topY,
                            width: Math.max(200, rightX - leftX),
                            height: Math.max(200, Math.min(window.innerHeight - topY, botY - topY)),
                        };
                    }
                    return {
                        wo,
                        sc,
                        sto,
                        booking_date: bookingDate,
                        paket,
                        service_number: srv,
                        contact_number: contact,
                        customer_name: cust,
                        service_address: addr,
                        clip,
                    };
                }"""
            )

            await asyncio.sleep(0.15)
            clip = card_info.get("clip")
            if clip:
                png_bytes = await page.screenshot(type="png", clip=clip)
            else:
                png_bytes = await page.screenshot(type="png")

            await self._save_state()
            self.session_active = True
            self._disconnect_notified = False
            self.last_keepalive_at = self._now_wita().strftime("%H:%M WITA")
            self._record_dispatch()
            card_info["png_bytes"] = png_bytes
            return card_info

    async def keep_alive(
        self,
        on_disconnect_cb: Callable[[str], Awaitable[None]] | None = None,
    ) -> bool:
        """Ping ringan ke FLOW /home setiap 10 menit agar sesi OTP tidak idle-timeout."""
        if self.waiting_for_otp or self._lock.locked():
            return False
        if (not self._page or self._page.is_closed()) and not self.state_file.exists():
            return False
        was_active = self.session_active
        try:
            async with self._lock:
                page = await self._ensure_browser()
                cur = page.url.lower()
                if any(p in cur for p in ("/home", "/dashboard", "/workorder", "/editworkorder")):
                    status = await page.evaluate(
                        "() => fetch('/home', {credentials: 'include'}).then(r => r.status).catch(() => 0)"
                    )
                    if status == 200:
                        await self._save_state()
                        self.session_active = True
                        self._disconnect_notified = False
                        self.last_keepalive_at = self._now_wita().strftime("%H:%M WITA")
                        logger.info("[WFM] Keep-alive OK (fetch /home -> 200)")
                        return True
                    self.session_active = False
                elif self.state_file.exists():
                    await page.goto(f"{FLOW_BASE_URL}/home", wait_until="domcontentloaded", timeout=25000)
                    await asyncio.sleep(0.5)
                    if "/home" in page.url.lower():
                        await self._save_state()
                        self.session_active = True
                        self._disconnect_notified = False
                        self.last_keepalive_at = self._now_wita().strftime("%H:%M WITA")
                        logger.info("[WFM] Keep-alive warmup OK (%s)", page.url)
                        return True
                    self.session_active = False
        except Exception as exc:
            logger.debug("[WFM] Keep-alive skipped: %s", exc)
            return False

        if was_active and not self.session_active and not self._disconnect_notified and on_disconnect_cb:
            self._disconnect_notified = True
            try:
                await on_disconnect_cb(
                    "⚠️ <b>Sesi WFM FLOW Terputus!</b>\n\n"
                    "Keep-alive mendeteksi sesi FLOW di server telah logout.\n"
                    "Kirim <code>/wo &lt;WONUM&gt;</code> lalu balas <code>/otp &lt;6 digit&gt;</code> agar siap sebelum jam sibuk."
                )
            except Exception:
                pass
        return False

    async def start_keep_alive_loop(
        self,
        interval_seconds: int = 600,
        on_disconnect_cb: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Loop background untuk menjaga sesi FLOW tetap aktif 24/7."""
        # Warmup singkat 15 detik setelah start jika wfm_storage_state.json sudah ada di volume
        await asyncio.sleep(15)
        await self.keep_alive(on_disconnect_cb=on_disconnect_cb)
        while True:
            await asyncio.sleep(max(120, interval_seconds))
            await self.keep_alive(on_disconnect_cb=on_disconnect_cb)
