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
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger("telegram-autosender.wfm")

FLOW_BASE_URL = "https://flow.telkomakses.co.id"
FLOW_EDIT_WO_URL = f"{FLOW_BASE_URL}/EditWorkorder/"


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
    """Susun caption ringkas jika tidak ada teks Salin ADING (misal dari perintah /wo langsung)."""
    return "\n".join(
        [
            f"WONUM: {card_info.get('wo') or wonum}",
            f"NO SC: {card_info.get('sc') or '-'}",
            f"NO INTERNET: {card_info.get('service_number') or '-'}",
            f"CUSTOMER NAME: {card_info.get('customer_name') or '-'}",
            f"CONTACT NUMBER: {card_info.get('contact_number') or '-'}",
            f"ALAMAT: {card_info.get('service_address') or '-'}",
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

    async def _perform_login_and_otp(
        self,
        page: Any,
        notify_otp_cb: Callable[[str], Awaitable[None]] | None,
    ) -> None:
        """Lakukan login FLOW dan tunggu kode OTP dari Telegram Admin jika diperlukan."""
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

        # Jika sekarang di /otp, minta kode OTP dari Admin via Telegram
        if "/otp" in page.url.lower() or await page.locator("#otp, input[name='otp']").count() > 0:
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
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(0.7)

            cur = page.url.lower()
            logger.info("[WFM] Initial goto %s -> %s", target_url, page.url)
            if "/unauthorize" in cur or cur.rstrip("/") == FLOW_BASE_URL or "/otp" in cur or "/login" in cur:
                await self._perform_login_and_otp(page, notify_otp_cb)
                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                except Exception as nav_exc:
                    if "ERR_ABORTED" in str(nav_exc):
                        await asyncio.sleep(1.5)
                        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                    else:
                        raise
                await asyncio.sleep(0.8)
                logger.info("[WFM] Post-login goto %s -> %s", target_url, page.url)
                if "/unauthorize" in page.url.lower():
                    # Fallback: buka /home dan cari lewat #searchWonum (global search FLOW)
                    await page.goto(f"{FLOW_BASE_URL}/home", wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(1.0)
                    logger.info("[WFM] Fallback /home -> %s", page.url)
                    search_inp = page.locator("#searchWonum").first
                    if await search_inp.count() > 0:
                        await search_inp.fill(clean_wo)
                        await search_inp.press("Enter")
                        await asyncio.sleep(2.5)
                        logger.info("[WFM] After #searchWonum -> %s", page.url)

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
                    const srv = getValByLabel('Service Number');
                    const contact = getValByLabel('Contact Number');
                    const cust = getValByLabel('Customer Name');
                    const addr = getValByLabel('Service Address');

                    document.querySelectorAll('nav, header, .navbar').forEach(n => n.style.display = 'none');

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
                        const absTop = anchorEl.getBoundingClientRect().top + window.scrollY;
                        window.scrollTo(0, Math.max(0, absTop - 12));
                        const br = (redBarEl || anchorEl).getBoundingClientRect();
                        const topY = tabBtnEl ? (tabBtnEl.getBoundingClientRect().top - 8) : (br.top - 8);
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
                            y: Math.max(0, topY),
                            width: Math.max(200, rightX - leftX),
                            height: Math.max(200, botY - Math.max(0, topY)),
                        };
                    }
                    return { wo, sc, service_number: srv, contact_number: contact, customer_name: cust, service_address: addr, clip };
                }"""
            )

            await asyncio.sleep(0.15)
            clip = card_info.get("clip")
            if clip:
                png_bytes = await page.screenshot(type="png", clip=clip)
            else:
                png_bytes = await page.screenshot(type="png")

            await self._save_state()
            card_info["png_bytes"] = png_bytes
            return card_info
