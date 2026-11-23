"""Telegram AutoSender Bot berbasis Python + APScheduler dengan Custom Menu Interaktif.

Fitur:
- Menu utama interaktif (/start, /menu) dengan Custom Menu terpadu (buat pesan custom,
  ubah pesan, dan atur waktu bebas).
- Input waktu fleksibel: tanggal & jam bebas, jam saja, 'besok 08:00', '+10 menit',
  'setiap hari 08:00', 'setiap 15 menit', atau cron 5-kolom.
- Mendukung jadwal date (sekali kirim), cron (berulang), dan interval (berkala).
- Perintah cepat: /menu, /schedule, /setpesan, /list, /test, /delete, /reload, /chatid, /cancel.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import (
    BotConfig,
    ScheduleJob,
    ensure_schedules_file,
    load_config,
    load_schedules,
    parse_flexible_schedule,
    parse_run_at,
    save_schedules,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram-autosender")
logging.getLogger("httpx").setLevel(logging.WARNING)

# State untuk ConversationHandler Custom Menu
(
    ASK_TARGET,
    ASK_DATETIME,
    ASK_MESSAGE,
    CONFIRM_SAVE,
    EDIT_SELECT_JOB,
    EDIT_ACTION,
    EDIT_NEW_MESSAGE,
    EDIT_NEW_TIME,
    EDIT_NEW_TARGET,
) = range(9)


def parse_chat_ids(raw: str) -> list[int | str]:
    """Parse daftar Chat ID dipisah koma (angka atau @username channel)."""
    raw_ids = [item.strip() for item in raw.split(",") if item.strip()]
    if not raw_ids:
        raise ValueError("Chat ID tidak boleh kosong.")

    chat_ids: list[int | str] = []
    for item in raw_ids:
        if item.startswith("@"):
            chat_ids.append(item)
        else:
            try:
                chat_ids.append(int(item))
            except ValueError as exc:
                raise ValueError(
                    f"Chat ID '{item}' tidak valid. Masukkan angka Chat ID (contoh: -1001234567890) atau @username."
                ) from exc
    return chat_ids


def build_trigger(job: ScheduleJob, timezone: Any) -> CronTrigger | IntervalTrigger | DateTrigger:
    """Konversi ScheduleJob menjadi APScheduler trigger."""
    if job.trigger == "date":
        assert job.run_at is not None
        run_dt = parse_run_at(job.run_at, timezone)
        return DateTrigger(run_date=run_dt, timezone=timezone)

    if job.trigger == "cron":
        assert job.cron is not None
        minute, hour, day, month, day_of_week = job.cron.split()
        return CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=timezone,
        )

    kwargs: dict[str, int] = {}
    if job.seconds:
        kwargs["seconds"] = job.seconds
    if job.minutes:
        kwargs["minutes"] = job.minutes
    if job.hours:
        kwargs["hours"] = job.hours
    if job.days:
        kwargs["days"] = job.days
    return IntervalTrigger(**kwargs, timezone=timezone)


def describe_job_time(job: ScheduleJob) -> str:
    """Deskripsi singkat jadwal waktu dari sebuah ScheduleJob."""
    if job.trigger == "date":
        return f"Sekali kirim: {job.run_at}"
    if job.trigger == "cron":
        return f"Cron: {job.cron}"
    parts: list[str] = []
    if job.days:
        parts.append(f"{job.days} hari")
    if job.hours:
        parts.append(f"{job.hours} jam")
    if job.minutes:
        parts.append(f"{job.minutes} menit")
    if job.seconds:
        parts.append(f"{job.seconds} detik")
    return "Interval: setiap " + " ".join(parts)


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Keyboard menu utama bot."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✨ Custom Pesan & Atur Waktu", callback_data="menu:custom")],
            [
                InlineKeyboardButton("📋 Daftar Jadwal", callback_data="menu:list"),
                InlineKeyboardButton("🆔 Cek Chat ID", callback_data="menu:chatid"),
            ],
            [InlineKeyboardButton("🔄 Reload schedules.json", callback_data="menu:reload")],
        ]
    )


def build_custom_menu_keyboard() -> InlineKeyboardMarkup:
    """Sub-menu terpadu untuk custom pesan dan pengaturan waktu."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Buat Pesan Custom Baru", callback_data="custom:new")],
            [InlineKeyboardButton("✏️ Edit Pesan & Waktu Jadwal", callback_data="custom:edit")],
            [InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="menu:home")],
        ]
    )


def build_back_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="menu:home")]]
    )


def build_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel")]])


def build_flexible_time_prompt(config: BotConfig, title: str) -> tuple[str, InlineKeyboardMarkup]:
    """Teks panduan + tombol cepat untuk pengaturan waktu bebas."""
    now = datetime.now(config.tzinfo)
    t_5m = now + timedelta(minutes=5)
    t_30m = now + timedelta(minutes=30)
    t_1h = now + timedelta(hours=1)

    text = (
        f"<b>{title}</b>\n\n"
        f"🕒 Waktu sekarang: <code>{now.strftime('%Y-%m-%d %H:%M %Z')}</code>\n\n"
        "Pilih tombol cepat atau <b>ketik waktu bebas</b> dengan format apa saja:\n"
        "• <b>Tanggal &amp; jam:</b> <code>2026-11-23 15:30</code> atau <code>23/11/2026 15:30</code>\n"
        "• <b>Jam saja:</b> <code>15:30</code> atau <code>besok 08:00</code>\n"
        "• <b>Dari sekarang:</b> <code>+10 menit</code>, <code>+2 jam</code>, <code>+30 detik</code>\n"
        "• <b>Setiap hari:</b> <code>setiap hari 08:00</code>\n"
        "• <b>Berkala:</b> <code>setiap 15 menit</code>, <code>setiap 2 jam</code>\n"
        "• <b>Cron 5-kolom:</b> <code>0 9 * * 1-5</code>"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"⏱️ +5 menit ({t_5m.strftime('%H:%M')})", callback_data="time:quick:+5 menit"),
                InlineKeyboardButton(f"⏱️ +30 menit ({t_30m.strftime('%H:%M')})", callback_data="time:quick:+30 menit"),
            ],
            [
                InlineKeyboardButton(f"🕐 +1 jam ({t_1h.strftime('%H:%M')})", callback_data="time:quick:+1 jam"),
                InlineKeyboardButton("🌅 Besok 08:00", callback_data="time:quick:besok 08:00"),
            ],
            [
                InlineKeyboardButton("🔁 Harian 08:00", callback_data="time:quick:setiap hari 08:00"),
                InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel"),
            ],
        ]
    )
    return text, keyboard


async def send_scheduled_message(bot: Bot, job: ScheduleJob) -> tuple[int, int]:
    """Kirim pesan dari satu ScheduleJob ke semua chat_ids."""
    success = 0
    failed = 0

    for chat_id in job.chat_ids:
        try:
            if job.photo:
                photo_path = Path(job.photo)
                if photo_path.exists() and photo_path.is_file():
                    with photo_path.open("rb") as photo_file:
                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=photo_file,
                            caption=job.message or None,
                            parse_mode=job.parse_mode,
                        )
                else:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=job.photo,
                        caption=job.message or None,
                        parse_mode=job.parse_mode,
                    )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=job.message,
                    parse_mode=job.parse_mode,
                )
            success += 1
            logger.info("Job '%s' terkirim ke chat_id=%s", job.id, chat_id)
        except TelegramError as exc:
            failed += 1
            logger.error("Gagal mengirim job '%s' ke chat_id=%s: %s", job.id, chat_id, exc)

        await asyncio.sleep(0.05)

    return success, failed


def register_jobs(
    scheduler: AsyncIOScheduler,
    bot: Bot | None,
    jobs: list[ScheduleJob],
    config: BotConfig,
) -> int:
    """Daftarkan ulang semua jadwal aktif ke scheduler."""
    scheduler.remove_all_jobs()
    active_count = 0
    now = datetime.now(config.tzinfo)

    for job in jobs:
        if not job.enabled:
            logger.info("Melewati jadwal nonaktif: %s (%s)", job.id, job.name)
            continue

        trigger = build_trigger(job, config.tzinfo)
        if job.trigger == "date":
            next_fire = trigger.get_next_fire_time(None, now)
            if next_fire is None:
                logger.info("Melewati jadwal tanggal yang sudah lewat: %s (%s)", job.id, job.run_at)
                continue

        scheduler.add_job(
            send_scheduled_message,
            trigger=trigger,
            args=[bot, job],
            id=job.id,
            name=job.name,
            replace_existing=True,
            misfire_grace_time=60,
        )
        active_count += 1
        logger.info("Terjadwal: [%s] %s (%s)", job.id, job.name, trigger)

    return active_count


def is_admin(update: Update, config: BotConfig) -> bool:
    """Cek apakah pengirim adalah admin (jika ADMIN_IDS kosong, semua diizinkan)."""
    if not config.admin_ids:
        return True
    user = update.effective_user
    return bool(user and user.id in config.admin_ids)


def format_main_menu_text(update: Update, config: BotConfig, active_count: int) -> str:
    chat = update.effective_chat
    chat_id = chat.id if chat else "N/A"
    now_str = datetime.now(config.tzinfo).strftime("%Y-%m-%d %H:%M %Z")
    return (
        "<b>🤖 Menu Utama — Telegram AutoSender</b>\n\n"
        f"🕒 Waktu server: <code>{now_str}</code>\n"
        f"🆔 Chat ID ini: <code>{chat_id}</code>\n"
        f"📅 Jadwal aktif: <b>{active_count}</b>\n\n"
        "Pilih menu di bawah ini:"
    )


def format_jobs_list_and_keyboard(
    scheduler: AsyncIOScheduler,
) -> tuple[str, InlineKeyboardMarkup]:
    """Susun teks daftar jadwal aktif beserta tombol Edit, Tes, & Hapus."""
    scheduled = scheduler.get_jobs()
    if not scheduled:
        return (
            "Belum ada jadwal aktif.\nKlik <b>✨ Custom Pesan &amp; Atur Waktu</b> untuk menambahkan.",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✨ Custom Pesan & Atur Waktu", callback_data="menu:custom")],
                    [InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="menu:home")],
                ]
            ),
        )

    lines = ["<b>📋 Daftar Jadwal Aktif:</b>\n"]
    buttons: list[list[InlineKeyboardButton]] = []

    for ap_job in scheduled:
        next_run = (
            ap_job.next_run_time.strftime("%Y-%m-%d %H:%M:%S %Z")
            if ap_job.next_run_time
            else "-"
        )
        lines.append(
            f"• <b>{html.escape(ap_job.name)}</b> (<code>{ap_job.id}</code>)\n"
            f"  ⏰ Berikutnya: <code>{next_run}</code>"
        )
        buttons.append(
            [
                InlineKeyboardButton(f"✏️ Edit {ap_job.id}", callback_data=f"editpick:{ap_job.id}"),
                InlineKeyboardButton("🚀 Tes", callback_data=f"job:test:{ap_job.id}"),
                InlineKeyboardButton("🗑️ Hapus", callback_data=f"job:del:{ap_job.id}"),
            ]
        )

    buttons.append([InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="menu:home")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def apply_schedule_spec(job: ScheduleJob, spec: dict[str, Any]) -> None:
    """Terapkan hasil parse_flexible_schedule ke sebuah ScheduleJob."""
    job.trigger = spec["trigger"]
    job.run_at = spec.get("run_at")
    job.cron = spec.get("cron")
    job.days = spec.get("days")
    job.hours = spec.get("hours")
    job.minutes = spec.get("minutes")
    job.seconds = spec.get("seconds")
    job.enabled = True
    job.validate()


def save_new_custom_job(
    context: ContextTypes.DEFAULT_TYPE,
    chat_ids: list[int | str],
    spec: dict[str, Any],
    custom_msg: str,
) -> ScheduleJob:
    """Buat, simpan ke schedules.json, dan daftarkan jadwal custom baru."""
    config: BotConfig = context.bot_data["config"]
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    existing_ids = {j.id for j in jobs}

    stamp = datetime.now(config.tzinfo).strftime("%Y%m%d_%H%M")
    base_id = f"custom_{stamp}"
    job_id = base_id
    counter = 2
    while job_id in existing_ids:
        job_id = f"{base_id}_{counter}"
        counter += 1

    new_job = ScheduleJob(
        id=job_id,
        name=f"Custom ({spec['label'][:28]})",
        trigger=spec["trigger"],
        run_at=spec.get("run_at"),
        cron=spec.get("cron"),
        days=spec.get("days"),
        hours=spec.get("hours"),
        minutes=spec.get("minutes"),
        seconds=spec.get("seconds"),
        chat_ids=chat_ids,
        message=custom_msg,
        enabled=True,
        parse_mode="HTML",
    )
    new_job.validate()

    jobs.append(new_job)
    save_schedules(config.schedules_file, jobs)

    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    register_jobs(scheduler, context.bot, jobs, config)
    context.bot_data["jobs"] = jobs
    return new_job


# ==================== COMMAND HANDLERS ====================


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    config: BotConfig = context.bot_data["config"]
    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    text = format_main_menu_text(update, config, len(scheduler.get_jobs()))
    await update.message.reply_html(text, reply_markup=build_main_menu_keyboard())


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    user = update.effective_user
    user_id = user.id if user else "N/A"
    await update.message.reply_html(
        f"🆔 Chat ID: <code>{update.effective_chat.id}</code>\n"
        f"📌 Tipe: <code>{update.effective_chat.type}</code>\n"
        f"👤 User ID: <code>{user_id}</code>",
        reply_markup=build_back_menu_keyboard(),
    )


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shortcut cepat: /schedule <waktu bebas> | <pesan custom>."""
    if not update.message or not update.effective_chat:
        return
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config):
        await update.message.reply_text("Maaf, perintah ini khusus admin.")
        return

    raw_text = update.message.text or ""
    parts = raw_text.split(None, 1)
    if len(parts) < 2 or "|" not in parts[1]:
        await update.message.reply_html(
            "<b>Format cepat /schedule:</b>\n"
            "<code>/schedule 2026-11-23 15:30 | Pesan custom kamu</code>\n"
            "<code>/schedule +10 menit | Pengingat 10 menit lagi</code>\n"
            "<code>/schedule setiap hari 08:00 | Selamat pagi!</code>\n"
            "<code>/schedule setiap 30 menit | Minum air putih</code>\n\n"
            "Atau klik tombol <b>✨ Custom Pesan &amp; Atur Waktu</b> di /menu!",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    when_raw, custom_msg = parts[1].split("|", 1)
    custom_msg = custom_msg.strip()
    if not custom_msg:
        await update.message.reply_text("Pesan custom tidak boleh kosong.")
        return

    try:
        spec = parse_flexible_schedule(when_raw, config.tzinfo)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    new_job = save_new_custom_job(context, [update.effective_chat.id], spec, custom_msg)
    await update.message.reply_html(
        "<b>✅ Jadwal pesan custom tersimpan!</b>\n\n"
        f"ID: <code>{new_job.id}</code>\n"
        f"⏰ Waktu: <code>{html.escape(spec['label'])}</code>\n"
        f"🎯 Target Chat ID: <code>{update.effective_chat.id}</code>\n"
        f"✉️ Pesan:\n{html.escape(custom_msg)}",
        reply_markup=build_back_menu_keyboard(),
    )


async def cmd_setpesan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Perintah cepat: /setpesan <job_id> | <pesan custom baru>."""
    if not update.message:
        return
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config):
        await update.message.reply_text("Maaf, perintah ini khusus admin.")
        return

    raw_text = update.message.text or ""
    parts = raw_text.split(None, 1)
    if len(parts) < 2 or "|" not in parts[1]:
        await update.message.reply_html(
            "Gunakan format:\n<code>/setpesan &lt;job_id&gt; | Pesan custom baru</code>\n\n"
            "Atau gunakan <b>✨ Custom Pesan &amp; Atur Waktu</b> di /menu.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    target_id, new_text = parts[1].split("|", 1)
    target_id = target_id.strip()
    new_text = new_text.strip()
    if not new_text:
        await update.message.reply_text("Pesan custom baru tidak boleh kosong.")
        return

    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)
    if not matched:
        await update.message.reply_html(f"Job ID <code>{html.escape(target_id)}</code> tidak ditemukan.")
        return

    matched.message = new_text
    save_schedules(config.schedules_file, jobs)
    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    register_jobs(scheduler, context.bot, jobs, config)
    context.bot_data["jobs"] = jobs

    await update.message.reply_html(
        f"<b>✅ Pesan untuk <code>{matched.id}</code> berhasil diubah!</b>\n\n"
        f"{html.escape(new_text)}",
        reply_markup=build_back_menu_keyboard(),
    )


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config):
        await update.message.reply_text("Maaf, perintah ini khusus admin.")
        return

    if not context.args:
        await update.message.reply_html("Gunakan: <code>/delete &lt;job_id&gt;</code>")
        return

    target_id = context.args[0].strip()
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    remaining = [j for j in jobs if j.id != target_id]
    if len(remaining) == len(jobs):
        await update.message.reply_html(f"Job ID <code>{html.escape(target_id)}</code> tidak ditemukan.")
        return

    save_schedules(config.schedules_file, remaining)
    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    register_jobs(scheduler, context.bot, remaining, config)
    context.bot_data["jobs"] = remaining

    await update.message.reply_html(
        f"🗑️ Jadwal <code>{html.escape(target_id)}</code> berhasil dihapus.",
        reply_markup=build_back_menu_keyboard(),
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config):
        await update.message.reply_text("Maaf, perintah ini khusus admin.")
        return

    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    text, markup = format_jobs_list_and_keyboard(scheduler)
    await update.message.reply_html(text, reply_markup=markup)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config):
        await update.message.reply_text("Maaf, perintah ini khusus admin.")
        return

    if not context.args:
        await update.message.reply_html("Gunakan: <code>/test &lt;job_id&gt;</code>")
        return

    target_id = context.args[0].strip()
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)
    if not matched:
        await update.message.reply_html(f"Job ID <code>{html.escape(target_id)}</code> tidak ditemukan.")
        return

    await update.message.reply_html(f"🚀 Mengirim tes untuk job <code>{matched.id}</code>...")
    success, failed = await send_scheduled_message(context.bot, matched)
    await update.message.reply_html(
        f"Selesai tes <code>{matched.id}</code>: {success} terkirim, {failed} gagal.",
        reply_markup=build_back_menu_keyboard(),
    )


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config):
        await update.message.reply_text("Maaf, perintah ini khusus admin.")
        return

    try:
        jobs = load_schedules(config.schedules_file)
        scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
        count = register_jobs(scheduler, context.bot, jobs, config)
        context.bot_data["jobs"] = jobs
        await update.message.reply_html(
            f"🔄 Berhasil memuat ulang <code>{config.schedules_file}</code>. "
            f"Total jadwal aktif: <b>{count}</b>.",
            reply_markup=build_back_menu_keyboard(),
        )
    except Exception as exc:
        logger.exception("Gagal memuat ulang jadwal")
        await update.message.reply_text(f"Gagal memuat ulang jadwal: {exc}")


# ==================== WIZARD CUSTOM PESAN & ATUR WAKTU ====================


async def wiz_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Langkah 1 (Buat Baru): Pilih target Chat ID."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config):
        await query.edit_message_text("Maaf, fitur ini khusus admin.")
        return ConversationHandler.END

    chat = update.effective_chat
    chat_id = chat.id if chat else 0
    context.user_data.pop("wiz", None)

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"📍 Kirim ke Chat ini ({chat_id})", callback_data=f"wiz:target:{chat_id}")],
            [InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel")],
        ]
    )
    await query.edit_message_text(
        "<b>➕ Buat Pesan Custom Baru (1/3)</b>\n\n"
        "Klik tombol di bawah untuk mengirim ke chat ini, atau <b>ketik Chat ID tujuan</b> "
        "(boleh lebih dari satu dipisah koma, contoh: <code>217411824, -1001234567890</code>):",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return ASK_TARGET


async def wiz_target_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    target_id = int(query.data.split(":")[2])
    context.user_data["wiz"] = {"chat_ids": [target_id]}

    config: BotConfig = context.bot_data["config"]
    text, keyboard = build_flexible_time_prompt(config, "⏰ Atur Waktu Kirim Bebas (2/3)")
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    return ASK_DATETIME


async def wiz_target_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ASK_TARGET

    try:
        chat_ids = parse_chat_ids(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return ASK_TARGET

    context.user_data["wiz"] = {"chat_ids": chat_ids}
    config: BotConfig = context.bot_data["config"]
    text, keyboard = build_flexible_time_prompt(config, "⏰ Atur Waktu Kirim Bebas (2/3)")
    await update.message.reply_html(text, reply_markup=keyboard)
    return ASK_DATETIME


async def wiz_datetime_quick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    expr = query.data.split(":", 2)[2]
    config: BotConfig = context.bot_data["config"]
    spec = parse_flexible_schedule(expr, config.tzinfo)
    context.user_data.setdefault("wiz", {})["spec"] = spec

    await query.edit_message_text(
        "<b>✉️ Input Pesan Custom (3/3)</b>\n\n"
        f"⏰ Waktu dipilih: <code>{html.escape(spec['label'])}</code>\n\n"
        "Silakan ketik isi pesan custom yang ingin dikirim otomatis "
        "(mendukung HTML seperti <code>&lt;b&gt;tebal&lt;/b&gt;</code>):",
        parse_mode="HTML",
        reply_markup=build_cancel_keyboard(),
    )
    return ASK_MESSAGE


async def wiz_datetime_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ASK_DATETIME

    config: BotConfig = context.bot_data["config"]
    try:
        spec = parse_flexible_schedule(update.message.text, config.tzinfo)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return ASK_DATETIME

    context.user_data.setdefault("wiz", {})["spec"] = spec
    await update.message.reply_html(
        "<b>✉️ Input Pesan Custom (3/3)</b>\n\n"
        f"⏰ Waktu dipilih: <code>{html.escape(spec['label'])}</code>\n\n"
        "Silakan ketik isi pesan custom yang ingin dikirim otomatis:",
        reply_markup=build_cancel_keyboard(),
    )
    return ASK_MESSAGE


async def wiz_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ASK_MESSAGE

    custom_msg = update.message.text.strip()
    if not custom_msg:
        await update.message.reply_text("Pesan tidak boleh kosong. Ketik pesanmu:", reply_markup=build_cancel_keyboard())
        return ASK_MESSAGE

    wiz = context.user_data.setdefault("wiz", {})
    wiz["message"] = custom_msg
    spec: dict[str, Any] = wiz["spec"]
    chat_ids = wiz["chat_ids"]

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Simpan Jadwal", callback_data="wiz:save"),
                InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel"),
            ]
        ]
    )
    await update.message.reply_html(
        "<b>🔍 Konfirmasi Pesan Terjadwal</b>\n\n"
        f"🎯 Target: <code>{chat_ids}</code>\n"
        f"⏰ Waktu: <code>{html.escape(spec['label'])}</code>\n"
        f"✉️ Pesan:\n{html.escape(custom_msg)}\n\n"
        "Klik <b>✅ Simpan Jadwal</b> untuk mengaktifkan.",
        reply_markup=keyboard,
    )
    return CONFIRM_SAVE


async def wiz_confirm_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    wiz = context.user_data.pop("wiz", None)
    if not wiz or "spec" not in wiz or "message" not in wiz:
        await query.edit_message_text(
            "Sesi pembuatan jadwal kedaluwarsa. Silakan mulai lagi dari /menu.",
            reply_markup=build_main_menu_keyboard(),
        )
        return ConversationHandler.END

    spec: dict[str, Any] = wiz["spec"]
    chat_ids: list[int | str] = wiz["chat_ids"]
    custom_msg: str = wiz["message"]

    new_job = save_new_custom_job(context, chat_ids, spec, custom_msg)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🚀 Tes Kirim {new_job.id} Sekarang", callback_data=f"job:test:{new_job.id}")],
            [InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="menu:home")],
        ]
    )
    await query.edit_message_text(
        "<b>✅ Jadwal berhasil disimpan &amp; aktif!</b>\n\n"
        f"ID: <code>{new_job.id}</code>\n"
        f"⏰ Waktu: <code>{html.escape(spec['label'])}</code>\n"
        f"🎯 Target: <code>{chat_ids}</code>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return ConversationHandler.END


# ==================== EDIT PESAN & WAKTU JADWAL ====================


async def edit_list_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Tampilkan daftar semua jadwal agar user memilih jadwal yang ingin diedit pesan/waktunya."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config):
        await query.edit_message_text("Maaf, fitur ini khusus admin.")
        return ConversationHandler.END

    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    if not jobs:
        await query.edit_message_text(
            "Belum ada jadwal tersimpan. Buat dulu lewat <b>➕ Buat Pesan Custom Baru</b>.",
            parse_mode="HTML",
            reply_markup=build_custom_menu_keyboard(),
        )
        return ConversationHandler.END

    buttons = [
        [
            InlineKeyboardButton(
                f"{'🟢' if j.enabled else '⚪'} {j.name} ({j.id})",
                callback_data=f"editpick:{j.id}",
            )
        ]
        for j in jobs
    ]
    buttons.append([InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel")])

    await query.edit_message_text(
        "<b>✏️ Edit Pesan &amp; Waktu Jadwal</b>\n\n"
        "Pilih jadwal yang ingin kamu ubah:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_SELECT_JOB


async def edit_show_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Tampilkan detail jadwal terpilih dengan tombol Ubah Pesan & Ubah Waktu."""
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config):
        await query.edit_message_text("Maaf, fitur ini khusus admin.")
        return ConversationHandler.END

    target_id = query.data.split(":", 1)[1]
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)
    if not matched:
        await query.edit_message_text(
            f"Jadwal <code>{html.escape(target_id)}</code> tidak ditemukan.",
            parse_mode="HTML",
            reply_markup=build_back_menu_keyboard(),
        )
        return ConversationHandler.END

    context.user_data["edit_job_id"] = matched.id
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Ubah Pesan", callback_data="editact:msg"),
                InlineKeyboardButton("⏰ Ubah Waktu", callback_data="editact:time"),
            ],
            [
                InlineKeyboardButton("🎯 Ubah Target Chat ID", callback_data="editact:target"),
            ],
            [
                InlineKeyboardButton("🚀 Tes Kirim", callback_data=f"job:test:{matched.id}"),
                InlineKeyboardButton("❌ Tutup", callback_data="wiz:cancel"),
            ],
        ]
    )
    await query.edit_message_text(
        f"<b>⚙️ Kelola Jadwal: {html.escape(matched.name)}</b> (<code>{matched.id}</code>)\n\n"
        f"⏰ <b>Waktu saat ini:</b> <code>{html.escape(describe_job_time(matched))}</code>\n"
        f"🎯 <b>Target:</b> <code>{matched.chat_ids}</code>\n"
        f"✉️ <b>Pesan saat ini:</b>\n{html.escape(matched.message or '(kosong)')}\n\n"
        "Apa yang ingin kamu ubah?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return EDIT_ACTION


async def edit_ask_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    target_id = context.user_data.get("edit_job_id", "")
    await query.edit_message_text(
        f"<b>✏️ Input Pesan Custom Baru</b> (<code>{html.escape(target_id)}</code>)\n\n"
        "Silakan <b>ketik pesan custom baru</b> sekarang (mendukung format HTML seperti <code>&lt;b&gt;tebal&lt;/b&gt;</code>):",
        parse_mode="HTML",
        reply_markup=build_cancel_keyboard(),
    )
    return EDIT_NEW_MESSAGE


async def edit_ask_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    target_id = context.user_data.get("edit_job_id", "")
    config: BotConfig = context.bot_data["config"]
    text, keyboard = build_flexible_time_prompt(
        config,
        f"⏰ Atur Waktu Baru untuk {html.escape(target_id)}",
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    return EDIT_NEW_TIME


async def edit_ask_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    target_id = context.user_data.get("edit_job_id", "")
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)
    current_targets = matched.chat_ids if matched else []
    chat = update.effective_chat
    chat_id = chat.id if chat else 0

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"📍 Gunakan Chat ini ({chat_id})", callback_data=f"edittarget:{chat_id}")],
            [InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel")],
        ]
    )
    await query.edit_message_text(
        f"<b>🎯 Ubah Target Chat ID</b> (<code>{html.escape(target_id)}</code>)\n\n"
        f"Target saat ini: <code>{current_targets}</code>\n\n"
        "Klik tombol di bawah untuk memakai chat ini, atau <b>ketik Chat ID baru</b> "
        "(boleh lebih dari satu dipisah koma, contoh: <code>-1001234567890</code> atau <code>217411824, -1001234567890</code>):",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return EDIT_NEW_TARGET


async def _apply_edited_target(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_ids: list[int | str],
) -> int:
    target_id = context.user_data.pop("edit_job_id", None)
    config: BotConfig = context.bot_data["config"]
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)

    if not matched:
        if update.callback_query:
            await update.callback_query.edit_message_text(
                "Sesi edit kedaluwarsa. Buka /menu lagi.",
                reply_markup=build_main_menu_keyboard(),
            )
        elif update.message:
            await update.message.reply_text("Sesi edit kedaluwarsa. Buka /menu lagi.", reply_markup=build_main_menu_keyboard())
        return ConversationHandler.END

    matched.chat_ids = chat_ids
    save_schedules(config.schedules_file, jobs)

    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    register_jobs(scheduler, context.bot, jobs, config)
    context.bot_data["jobs"] = jobs

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🚀 Tes Kirim {matched.id} Sekarang", callback_data=f"job:test:{matched.id}")],
            [InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="menu:home")],
        ]
    )
    msg = (
        f"<b>✅ Target Chat ID untuk <code>{matched.id}</code> berhasil diubah!</b>\n\n"
        f"🎯 <b>Target baru:</b> <code>{chat_ids}</code>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=keyboard)
    elif update.message:
        await update.message.reply_html(msg, reply_markup=keyboard)
    return ConversationHandler.END


async def edit_save_target_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    chat_id = int(query.data.split(":", 1)[1])
    return await _apply_edited_target(update, context, [chat_id])


async def edit_save_target_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return EDIT_NEW_TARGET

    try:
        chat_ids = parse_chat_ids(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return EDIT_NEW_TARGET

    return await _apply_edited_target(update, context, chat_ids)


async def edit_save_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return EDIT_NEW_MESSAGE

    new_text = update.message.text.strip()
    if not new_text:
        await update.message.reply_text("Pesan tidak boleh kosong. Ketik pesan baru:", reply_markup=build_cancel_keyboard())
        return EDIT_NEW_MESSAGE

    target_id = context.user_data.pop("edit_job_id", None)
    config: BotConfig = context.bot_data["config"]
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)
    if not matched:
        await update.message.reply_text("Sesi edit kedaluwarsa. Buka /menu lagi.", reply_markup=build_main_menu_keyboard())
        return ConversationHandler.END

    matched.message = new_text
    save_schedules(config.schedules_file, jobs)

    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    register_jobs(scheduler, context.bot, jobs, config)
    context.bot_data["jobs"] = jobs

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🚀 Tes Kirim {matched.id} Sekarang", callback_data=f"job:test:{matched.id}")],
            [InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="menu:home")],
        ]
    )
    await update.message.reply_html(
        f"<b>✅ Pesan custom untuk <code>{matched.id}</code> berhasil diperbarui!</b>\n\n"
        f"<b>Pesan baru:</b>\n{html.escape(new_text)}",
        reply_markup=keyboard,
    )
    return ConversationHandler.END


async def _apply_edited_time(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    spec: dict[str, Any],
) -> int:
    target_id = context.user_data.pop("edit_job_id", None)
    config: BotConfig = context.bot_data["config"]
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)

    if not matched:
        if update.callback_query:
            await update.callback_query.edit_message_text(
                "Sesi edit kedaluwarsa. Buka /menu lagi.",
                reply_markup=build_main_menu_keyboard(),
            )
        elif update.message:
            await update.message.reply_text("Sesi edit kedaluwarsa. Buka /menu lagi.", reply_markup=build_main_menu_keyboard())
        return ConversationHandler.END

    apply_schedule_spec(matched, spec)
    save_schedules(config.schedules_file, jobs)

    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    register_jobs(scheduler, context.bot, jobs, config)
    context.bot_data["jobs"] = jobs

    msg = (
        f"<b>✅ Waktu kirim untuk <code>{matched.id}</code> berhasil diperbarui!</b>\n\n"
        f"⏰ <b>Jadwal baru:</b> <code>{html.escape(spec['label'])}</code>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=build_back_menu_keyboard())
    elif update.message:
        await update.message.reply_html(msg, reply_markup=build_back_menu_keyboard())
    return ConversationHandler.END


async def edit_save_time_quick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    expr = query.data.split(":", 2)[2]
    config: BotConfig = context.bot_data["config"]
    spec = parse_flexible_schedule(expr, config.tzinfo)
    return await _apply_edited_time(update, context, spec)


async def edit_save_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return EDIT_NEW_TIME

    config: BotConfig = context.bot_data["config"]
    try:
        spec = parse_flexible_schedule(update.message.text, config.tzinfo)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return EDIT_NEW_TIME

    return await _apply_edited_time(update, context, spec)


async def wiz_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("wiz", None)
    context.user_data.pop("edit_job_id", None)
    if update.callback_query:
        await update.callback_query.answer("Dibatalkan")
        await update.callback_query.edit_message_text(
            "❌ Proses dibatalkan.",
            reply_markup=build_main_menu_keyboard(),
        )
    elif update.message:
        await update.message.reply_text(
            "❌ Proses dibatalkan.",
            reply_markup=build_main_menu_keyboard(),
        )
    return ConversationHandler.END


# ==================== CALLBACK MENU UMUM ====================


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    config: BotConfig = context.bot_data["config"]
    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    data = query.data

    if data == "menu:home":
        text = format_main_menu_text(update, config, len(scheduler.get_jobs()))
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_main_menu_keyboard())
        return

    if data == "menu:custom":
        await query.edit_message_text(
            "<b>✨ Custom Pesan &amp; Atur Waktu</b>\n\n"
            "Pilih aksi yang ingin kamu lakukan:\n"
            "• <b>➕ Buat Pesan Custom Baru</b> — input target, waktu bebas, dan isi pesan.\n"
            "• <b>✏️ Edit Pesan &amp; Waktu Jadwal</b> — ubah pesan atau waktu dari jadwal yang sudah ada.",
            parse_mode="HTML",
            reply_markup=build_custom_menu_keyboard(),
        )
        return

    if data == "menu:chatid":
        chat = update.effective_chat
        user = update.effective_user
        await query.edit_message_text(
            f"🆔 Chat ID: <code>{chat.id if chat else 'N/A'}</code>\n"
            f"📌 Tipe: <code>{chat.type if chat else 'N/A'}</code>\n"
            f"👤 User ID: <code>{user.id if user else 'N/A'}</code>",
            parse_mode="HTML",
            reply_markup=build_back_menu_keyboard(),
        )
        return

    if not is_admin(update, config):
        await query.edit_message_text("Maaf, menu ini khusus admin.")
        return

    if data == "menu:list":
        text, markup = format_jobs_list_and_keyboard(scheduler)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return

    if data == "menu:reload":
        try:
            jobs = load_schedules(config.schedules_file)
            count = register_jobs(scheduler, context.bot, jobs, config)
            context.bot_data["jobs"] = jobs
            await query.edit_message_text(
                f"🔄 Berhasil memuat ulang <code>{config.schedules_file}</code>.\n"
                f"Total jadwal aktif: <b>{count}</b>.",
                parse_mode="HTML",
                reply_markup=build_back_menu_keyboard(),
            )
        except Exception as exc:
            await query.edit_message_text(
                f"Gagal memuat ulang jadwal: {exc}",
                reply_markup=build_back_menu_keyboard(),
            )
        return

    if data.startswith("job:test:"):
        target_id = data.split(":", 2)[2]
        jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
        matched = next((j for j in jobs if j.id == target_id), None)
        if not matched:
            await query.edit_message_text(
                f"Job ID <code>{html.escape(target_id)}</code> tidak ditemukan.",
                parse_mode="HTML",
                reply_markup=build_back_menu_keyboard(),
            )
            return
        success, failed = await send_scheduled_message(context.bot, matched)
        await query.edit_message_text(
            f"🚀 Tes kirim <code>{matched.id}</code> selesai:\n"
            f"✅ Terkirim: <b>{success}</b> | ❌ Gagal: <b>{failed}</b>",
            parse_mode="HTML",
            reply_markup=build_back_menu_keyboard(),
        )
        return

    if data.startswith("job:del:"):
        target_id = data.split(":", 2)[2]
        jobs = context.bot_data.get("jobs", [])
        remaining = [j for j in jobs if j.id != target_id]
        save_schedules(config.schedules_file, remaining)
        register_jobs(scheduler, context.bot, remaining, config)
        context.bot_data["jobs"] = remaining

        text, markup = format_jobs_list_and_keyboard(scheduler)
        await query.edit_message_text(
            f"🗑️ Jadwal <code>{html.escape(target_id)}</code> dihapus.\n\n{text}",
            parse_mode="HTML",
            reply_markup=markup,
        )


# ==================== LIFECYCLE & CLI ====================


async def post_init(application: Application) -> None:
    config: BotConfig = application.bot_data["config"]
    ensure_schedules_file(config.schedules_file)
    jobs = load_schedules(config.schedules_file)
    scheduler = AsyncIOScheduler(timezone=config.tzinfo)
    register_jobs(scheduler, application.bot, jobs, config)
    scheduler.start()

    application.bot_data["scheduler"] = scheduler
    application.bot_data["jobs"] = jobs

    await application.bot.set_my_commands(
        [
            BotCommand("menu", "Buka menu utama interaktif"),
            BotCommand("schedule", "Buat jadwal pesan cepat"),
            BotCommand("setpesan", "Ubah custom pesan jadwal"),
            BotCommand("list", "Lihat daftar jadwal aktif"),
            BotCommand("chatid", "Cek Chat ID saat ini"),
            BotCommand("reload", "Muat ulang schedules.json"),
            BotCommand("cancel", "Batalkan proses saat ini"),
        ]
    )
    logger.info("Bot siap, menu perintah terdaftar, dan scheduler berjalan (timezone=%s).", config.timezone)


async def post_shutdown(application: Application) -> None:
    scheduler: AsyncIOScheduler | None = application.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler dihentikan.")


def run_check(schedules_override: str | None = None) -> int:
    """Validasi konfigurasi & tampilkan jadwal berikutnya tanpa koneksi ke Telegram."""
    config = load_config(require_token=False)
    schedules_path = Path(schedules_override) if schedules_override else config.schedules_file
    jobs = load_schedules(schedules_path)
    now = datetime.now(config.tzinfo)

    print(f"Timezone : {config.timezone}")
    print(f"File     : {schedules_path}")
    print(f"Token    : {'Terisi' if config.bot_token else 'BELUM DIISI (isi di .env sebelum run)'}")
    print(f"Total    : {len(jobs)} jadwal ditemukan\n")

    for job in jobs:
        trigger = build_trigger(job, config.tzinfo)
        next_fire = trigger.get_next_fire_time(None, now)
        status = "AKTIF" if job.enabled else "NONAKTIF"
        next_str = next_fire.strftime("%Y-%m-%d %H:%M:%S %Z") if next_fire else "SUDAH LEWAT"
        print(f"[{status}] {job.id} ({job.name})")
        print(f"  Trigger   : {trigger}")
        print(f"  Target    : {job.chat_ids}")
        print(f"  Berikutnya: {next_str}\n")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram AutoSender Bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validasi schedules.json dan tampilkan jadwal berikutnya tanpa menjalankan bot.",
    )
    parser.add_argument(
        "--schedules",
        type=str,
        default=None,
        help="Path opsional ke file schedules.json.",
    )
    args = parser.parse_args()

    if args.check:
        raise SystemExit(run_check(args.schedules))

    config = load_config(require_token=True)
    if args.schedules:
        config.schedules_file = Path(args.schedules)

    application = (
        ApplicationBuilder()
        .token(config.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["config"] = config

    wizard_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(wiz_new_start, pattern=r"^custom:new$"),
            CallbackQueryHandler(edit_list_jobs, pattern=r"^custom:edit$"),
            CallbackQueryHandler(edit_show_actions, pattern=r"^editpick:.+$"),
        ],
        states={
            ASK_TARGET: [
                CallbackQueryHandler(wiz_target_button, pattern=r"^wiz:target:-?\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_target_text),
            ],
            ASK_DATETIME: [
                CallbackQueryHandler(wiz_datetime_quick, pattern=r"^time:quick:.+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_datetime_text),
            ],
            ASK_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_message_text),
            ],
            CONFIRM_SAVE: [
                CallbackQueryHandler(wiz_confirm_save, pattern=r"^wiz:save$"),
            ],
            EDIT_SELECT_JOB: [
                CallbackQueryHandler(edit_show_actions, pattern=r"^editpick:.+$"),
            ],
            EDIT_ACTION: [
                CallbackQueryHandler(edit_ask_message, pattern=r"^editact:msg$"),
                CallbackQueryHandler(edit_ask_time, pattern=r"^editact:time$"),
                CallbackQueryHandler(edit_ask_target, pattern=r"^editact:target$"),
            ],
            EDIT_NEW_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_save_message),
            ],
            EDIT_NEW_TIME: [
                CallbackQueryHandler(edit_save_time_quick, pattern=r"^time:quick:.+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_save_time_text),
            ],
            EDIT_NEW_TARGET: [
                CallbackQueryHandler(edit_save_target_button, pattern=r"^edittarget:-?\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_save_target_text),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(wiz_cancel, pattern=r"^wiz:cancel$"),
            CommandHandler("cancel", wiz_cancel),
        ],
        per_message=False,
    )

    application.add_handler(wizard_handler)
    application.add_handler(CommandHandler(["start", "help", "menu"], cmd_menu))
    application.add_handler(CommandHandler("chatid", cmd_chatid))
    application.add_handler(CommandHandler(["schedule", "jadwalkan"], cmd_schedule))
    application.add_handler(CommandHandler("setpesan", cmd_setpesan))
    application.add_handler(CommandHandler(["delete", "hapus"], cmd_delete))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("test", cmd_test))
    application.add_handler(CommandHandler("reload", cmd_reload))
    application.add_handler(CallbackQueryHandler(handle_menu_callback))

    logger.info("Menjalankan Telegram AutoSender Bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
