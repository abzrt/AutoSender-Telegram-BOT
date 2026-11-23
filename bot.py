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
from telegram import Bot, BotCommand, ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import ChatMigrated, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
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
    load_extra_admins,
    load_known_groups,
    load_schedules,
    parse_flexible_schedule,
    parse_mentions,
    parse_run_at,
    parse_user_ids,
    render_message_with_mentions,
    save_extra_admins,
    save_known_groups,
    save_schedules,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram-autosender")
logging.getLogger("httpx").setLevel(logging.WARNING)

# State untuk ConversationHandler Custom Menu, Target Book, & Manajemen Admin
(
    ASK_TARGET,
    ASK_DATETIME,
    ASK_MESSAGE,
    ASK_MENTIONS,
    CONFIRM_SAVE,
    EDIT_SELECT_JOB,
    EDIT_ACTION,
    EDIT_NEW_MESSAGE,
    EDIT_NEW_TIME,
    EDIT_NEW_TARGET,
    EDIT_NEW_MENTIONS,
    ADMIN_ADD_ID,
    TARGET_ADD_INPUT,
) = range(13)


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
                InlineKeyboardButton("📇 Kelola Target", callback_data="menu:targets"),
            ],
            [
                InlineKeyboardButton("👑 Kelola Admin", callback_data="menu:admins"),
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


def target_type_icon(target_type: str) -> str:
    return {
        "group": "👥",
        "supergroup": "👥",
        "user": "👤",
        "private": "👤",
        "channel": "📢",
    }.get(target_type.lower(), "📌")


def parse_single_target_id(raw: str) -> int | str:
    val = raw.strip()
    if val.startswith("@"):
        return val
    return int(val)


def parse_target_entry(raw: str) -> tuple[str | None, int | str]:
    """Parse input 'Nama | ID' atau hanya 'ID' / '@username'."""
    text = raw.strip()
    if not text:
        raise ValueError("Input tidak boleh kosong.")
    if "|" in text:
        label_part, id_part = text.split("|", 1)
        label = label_part.strip() or None
        raw_id = id_part.strip()
    else:
        label = None
        raw_id = text

    parsed_ids = parse_chat_ids(raw_id)
    if len(parsed_ids) != 1:
        raise ValueError("Masukkan 1 target saja per kali tambah (contoh: `Grup Tim | -1001234567890`).")
    return label, parsed_ids[0]


def format_targets_menu(known_groups: dict[str, dict[str, str]]) -> tuple[str, InlineKeyboardMarkup]:
    """Susun tampilan menu Kelola Target (Grup, User, Channel, Lainnya)."""
    lines = ["<b>📇 Kelola Target (Grup / User / Channel / Lainnya)</b>\n"]
    if known_groups:
        sorted_items = sorted(known_groups.items(), key=lambda kv: (kv[1].get("type", ""), kv[1].get("title", "").lower()))
        for cid_str, info in sorted_items:
            icon = target_type_icon(info.get("type", "other"))
            title = html.escape(info.get("title") or f"Target {cid_str}")
            lines.append(f"• {icon} <b>{title}</b> (<code>{html.escape(cid_str)}</code>)")
    else:
        lines.append("<i>Belum ada target tersimpan. Tambahkan lewat tombol di bawah!</i>")

    lines.append("\nPilih jenis target yang ingin ditambahkan:")
    buttons: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("➕ 👥 Grup", callback_data="tgtadd:group"),
            InlineKeyboardButton("➕ 👤 User", callback_data="tgtadd:user"),
        ],
        [
            InlineKeyboardButton("➕ 📢 Channel", callback_data="tgtadd:channel"),
            InlineKeyboardButton("➕ 📌 Lainnya", callback_data="tgtadd:other"),
        ],
    ]

    for cid_str, info in sorted(known_groups.items(), key=lambda kv: kv[1].get("title", "").lower()):
        icon = target_type_icon(info.get("type", "other"))
        title = info.get("title") or cid_str
        if len(title) > 22:
            title = title[:19] + "..."
        buttons.append([InlineKeyboardButton(f"🗑️ Hapus {icon} {title}", callback_data=f"tgtdel:{cid_str}")])

    buttons.append([InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="menu:home")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def build_target_picker_keyboard(
    current_chat_id: int,
    known_groups: dict[str, dict[str, str]],
    selected_ids: list[int | str],
    prefix: str = "wiztgt",
) -> InlineKeyboardMarkup:
    """Bangun keyboard checklist untuk memilih 1 atau banyak grup / user / channel target."""
    selected_str = {str(x) for x in selected_ids}
    buttons: list[list[InlineKeyboardButton]] = []

    # Tombol chat pribadi saat ini
    cur_mark = "☑️" if str(current_chat_id) in selected_str else "⬜"
    buttons.append(
        [
            InlineKeyboardButton(
                f"{cur_mark} 📍 Chat ini ({current_chat_id})",
                callback_data=f"{prefix}:toggle:{current_chat_id}",
            )
        ]
    )

    # Daftar grup / user / channel yang tersimpan (diurutkan sesuai nama)
    sorted_groups = sorted(
        known_groups.items(),
        key=lambda kv: kv[1].get("title", "").lower(),
    )
    for cid_str, info in sorted_groups:
        if cid_str == str(current_chat_id):
            continue
        title = info.get("title") or f"Target {cid_str}"
        if len(title) > 28:
            title = title[:25] + "..."
        icon = target_type_icon(info.get("type", "group"))
        mark = "☑️" if cid_str in selected_str else "⬜"
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{mark} {icon} {title}",
                    callback_data=f"{prefix}:toggle:{cid_str}",
                )
            ]
        )

    # Tombol aksi bawah
    action_row: list[InlineKeyboardButton] = []
    if selected_ids:
        action_row.append(
            InlineKeyboardButton(
                f"✅ Lanjut ({len(selected_ids)} dipilih)",
                callback_data=f"{prefix}:done",
            )
        )
    action_row.append(InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel"))
    buttons.append(action_row)
    return InlineKeyboardMarkup(buttons)


def remember_group(
    bot_data: dict[str, Any],
    chat_id: int | str,
    title: str,
    chat_type: str = "group",
) -> None:
    """Simpan atau perbarui target (grup/user/channel/lainnya) ke groups.json."""
    config: BotConfig = bot_data["config"]
    groups: dict[str, dict[str, str]] = bot_data.setdefault("known_groups", {})
    key = str(chat_id)
    new_entry = {"title": title or f"Target {chat_id}", "type": chat_type}
    if groups.get(key) != new_entry:
        groups[key] = new_entry
        save_known_groups(config.resolved_groups_file, groups)


def forget_group(bot_data: dict[str, Any], chat_id: int | str) -> None:
    """Hapus target dari groups.json."""
    config: BotConfig = bot_data["config"]
    groups: dict[str, dict[str, str]] = bot_data.setdefault("known_groups", {})
    if groups.pop(str(chat_id), None) is not None:
        save_known_groups(config.resolved_groups_file, groups)


async def track_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Otomatis catat/hapus grup saat bot ditambahkan atau dikeluarkan."""
    member = update.my_chat_member
    if not member or not member.chat:
        return
    chat = member.chat
    if chat.type not in ("group", "supergroup", "channel"):
        return

    new_status = member.new_chat_member.status
    if new_status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR):
        remember_group(context.bot_data, chat.id, chat.title or f"Grup {chat.id}", chat.type)
        logger.info("Grup tercatat otomatis: %s (%s)", chat.title, chat.id)
    elif new_status in (ChatMember.LEFT, ChatMember.BANNED):
        forget_group(context.bot_data, chat.id)
        logger.info("Grup dihapus dari daftar (bot keluar): %s (%s)", chat.title, chat.id)


async def track_group_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Otomatis catat nama grup saat ada pesan/aktivitas atau migrasi supergroup."""
    msg = update.effective_message
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup", "channel"):
        return

    if msg and msg.migrate_to_chat_id:
        old_id = chat.id
        new_id = msg.migrate_to_chat_id
        forget_group(context.bot_data, old_id)
        remember_group(context.bot_data, new_id, chat.title or f"Grup {new_id}", "supergroup")
        logger.info("Grup bermigrasi ke supergroup: %s -> %s", old_id, new_id)
        return

    remember_group(context.bot_data, chat.id, chat.title or f"Grup {chat.id}", chat.type)


def build_flexible_time_prompt(config: BotConfig, title: str) -> tuple[str, InlineKeyboardMarkup]:
    """Teks panduan + tombol cepat untuk pengaturan waktu bebas."""
    now = datetime.now(config.tzinfo)
    t_5m = now + timedelta(minutes=5)
    t_30m = now + timedelta(minutes=30)
    t_1h = now + timedelta(hours=1)

    text = (
        f"<b>{title}</b>\n\n"
        f"🕒 Waktu realtime: <code>{now.strftime('%Y-%m-%d %H:%M:%S %Z')}</code>\n\n"
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
    final_text = render_message_with_mentions(job.message, job.mentions, job.parse_mode)

    for chat_id in job.chat_ids:
        try:
            if job.photo:
                photo_path = Path(job.photo)
                if photo_path.exists() and photo_path.is_file():
                    with photo_path.open("rb") as photo_file:
                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=photo_file,
                            caption=final_text or None,
                            parse_mode=job.parse_mode,
                        )
                else:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=job.photo,
                        caption=final_text or None,
                        parse_mode=job.parse_mode,
                    )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=final_text,
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


def get_all_admin_ids(config: BotConfig, bot_data: dict[str, Any] | None = None) -> set[int]:
    """Gabungkan Owner (ADMIN_IDS dari env) + admin tambahan dari admins.json."""
    extra: set[int] = set()
    if bot_data is not None:
        extra = set(bot_data.get("extra_admins", set()))
    return set(config.admin_ids) | extra


def is_admin(update: Update, config: BotConfig, bot_data: dict[str, Any] | None = None) -> bool:
    """Cek apakah pengirim adalah admin (jika belum ada admin sama sekali, semua diizinkan)."""
    all_admins = get_all_admin_ids(config, bot_data)
    if not all_admins:
        return True
    user = update.effective_user
    return bool(user and user.id in all_admins)


async def deny_access(update: Update) -> None:
    """Kirim pesan penolakan akses untuk non-admin beserta User ID-nya."""
    user = update.effective_user
    uid = user.id if user else "N/A"
    msg = (
        "⛔ <b>Akses ditolak.</b>\n"
        "Bot ini hanya dapat digunakan oleh admin terdaftar.\n\n"
        f"User ID kamu: <code>{uid}</code>"
    )
    if update.callback_query:
        await update.callback_query.answer("Akses ditolak (khusus admin).", show_alert=True)
    elif update.message:
        await update.message.reply_html(msg)


def format_admins_menu(config: BotConfig, extra_admins: set[int]) -> tuple[str, InlineKeyboardMarkup]:
    """Susun tampilan menu Kelola Admin."""
    lines = ["<b>👑 Kelola Admin Bot</b>\n"]
    if config.admin_ids:
        lines.append("<b>Owner (dari ADMIN_IDS):</b>")
        for uid in sorted(config.admin_ids):
            lines.append(f"• 👑 <code>{uid}</code> <i>(permanen)</i>")
    else:
        lines.append("⚠️ <i>ADMIN_IDS di env masih kosong.</i>")

    lines.append("")
    if extra_admins:
        lines.append("<b>Admin Tambahan:</b>")
        for uid in sorted(extra_admins):
            lines.append(f"• 👤 <code>{uid}</code>")
    else:
        lines.append("<i>Belum ada admin tambahan.</i>")

    if not config.admin_ids and not extra_admins:
        lines.append("\n⚠️ <b>Mode Terbuka:</b> Tambahkan minimal 1 admin agar bot terkunci hanya untuk admin!")

    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("➕ Tambah Admin (User ID)", callback_data="admin:add")]
    ]
    for uid in sorted(extra_admins):
        if uid not in config.admin_ids:
            buttons.append([InlineKeyboardButton(f"🗑️ Hapus Admin {uid}", callback_data=f"admin:del:{uid}")])
    buttons.append([InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="menu:home")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def format_main_menu_text(update: Update, config: BotConfig, active_count: int) -> str:
    chat = update.effective_chat
    chat_id = chat.id if chat else "N/A"
    now_str = datetime.now(config.tzinfo).strftime("%Y-%m-%d %H:%M:%S %Z")
    return (
        "<b>🤖 Menu Utama — Telegram AutoSender</b>\n\n"
        f"🕒 Waktu realtime: <code>{now_str}</code>\n"
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
    mentions: list[str] | None = None,
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
        mentions=list(mentions or []),
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
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return
    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    text = format_main_menu_text(update, config, len(scheduler.get_jobs()))
    await update.message.reply_html(text, reply_markup=build_main_menu_keyboard())


async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return
    extra_admins: set[int] = context.bot_data.get("extra_admins", set())
    text, markup = format_admins_menu(config, extra_admins)
    await update.message.reply_html(text, reply_markup=markup)


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
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
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
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
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
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
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
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
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return

    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    text, markup = format_jobs_list_and_keyboard(scheduler)
    await update.message.reply_html(text, reply_markup=markup)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
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
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
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
    """Langkah 1 (Buat Baru): Pilih target grup/chat dari checklist atau ketik manual."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return ConversationHandler.END

    chat = update.effective_chat
    chat_id = chat.id if chat else 0
    context.user_data["wiz"] = {"selected_ids": []}

    known_groups: dict[str, dict[str, str]] = context.bot_data.get("known_groups", {})
    keyboard = build_target_picker_keyboard(chat_id, known_groups, [], prefix="wiztgt")
    hint_groups = (
        f"Tersedia <b>{len(known_groups)} grup</b> siap pilih."
        if known_groups
        else "<i>Belum ada grup terdeteksi (kirim 1 pesan di grup tempat bot berada agar muncul otomatis di sini).</i>"
    )
    await query.edit_message_text(
        "<b>➕ Buat Pesan Custom Baru (1/4) — Pilih Target</b>\n\n"
        f"{hint_groups}\n\n"
        "Centang 1 atau lebih grup/chat di bawah lalu klik <b>✅ Lanjut</b>, "
        "atau langsung <b>ketik Chat ID manual</b> (pisah koma):",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return ASK_TARGET


async def wiz_target_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    target_id = parse_single_target_id(query.data.split(":", 2)[2])
    wiz = context.user_data.setdefault("wiz", {})
    selected: list[int | str] = list(wiz.get("selected_ids", []))
    if target_id in selected:
        selected.remove(target_id)
    else:
        selected.append(target_id)
    wiz["selected_ids"] = selected

    chat = update.effective_chat
    chat_id = chat.id if chat else 0
    known_groups: dict[str, dict[str, str]] = context.bot_data.get("known_groups", {})
    keyboard = build_target_picker_keyboard(chat_id, known_groups, selected, prefix="wiztgt")
    await query.edit_message_reply_markup(reply_markup=keyboard)
    return ASK_TARGET


async def wiz_target_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    wiz = context.user_data.setdefault("wiz", {})
    selected: list[int | str] = list(wiz.get("selected_ids", []))
    if not selected:
        await query.answer("Pilih minimal 1 grup atau chat terlebih dahulu!", show_alert=True)
        return ASK_TARGET

    await query.answer()
    wiz["chat_ids"] = selected
    config: BotConfig = context.bot_data["config"]
    text, keyboard = build_flexible_time_prompt(config, "⏰ Atur Waktu Kirim Bebas (2/4)")
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
    text, keyboard = build_flexible_time_prompt(config, "⏰ Atur Waktu Kirim Bebas (2/4)")
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
        "<b>✉️ Input Pesan Custom (3/4)</b>\n\n"
        f"⏰ Waktu dipilih: <code>{html.escape(spec['label'])}</code>\n\n"
        "Silakan ketik isi pesan custom yang ingin dikirim otomatis.\n"
        "<i>Tips: kamu juga bisa langsung tulis <code>@username</code> atau <code>@[Nama](UserID)</code> di dalam pesan!</i>",
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
        "<b>✉️ Input Pesan Custom (3/4)</b>\n\n"
        f"⏰ Waktu dipilih: <code>{html.escape(spec['label'])}</code>\n\n"
        "Silakan ketik isi pesan custom yang ingin dikirim otomatis.\n"
        "<i>Tips: boleh langsung tulis <code>@username</code> atau <code>@[Nama](UserID)</code> di dalam pesan!</i>",
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

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⏭️ Lewati (Tanpa Tag Tambahan)", callback_data="wiz:mentions:skip")],
            [InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel")],
        ]
    )
    await update.message.reply_html(
        "<b>🏷️ Tag / Mention Username (4/4 — Opsional)</b>\n\n"
        "Ketik username siapa saja yang ingin di-tag di atas pesan (pisahkan spasi atau koma):\n"
        "• <b>Dengan username:</b> <code>@budi @ani @siti</code>\n"
        "• <b>Tanpa username (pakai User ID):</b> <code>Budi:123456789</code>\n\n"
        "Atau klik <b>⏭️ Lewati</b> jika tidak perlu tag tambahan.",
        reply_markup=keyboard,
    )
    return ASK_MENTIONS


async def _show_wiz_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    wiz = context.user_data.setdefault("wiz", {})
    spec: dict[str, Any] = wiz["spec"]
    chat_ids = wiz["chat_ids"]
    custom_msg: str = wiz["message"]
    mentions: list[str] = wiz.get("mentions", [])
    tags_preview = " ".join(mentions) if mentions else "(tidak ada)"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Simpan Jadwal", callback_data="wiz:save"),
                InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel"),
            ]
        ]
    )
    text = (
        "<b>🔍 Konfirmasi Pesan Terjadwal</b>\n\n"
        f"🎯 Target: <code>{chat_ids}</code>\n"
        f"⏰ Waktu: <code>{html.escape(spec['label'])}</code>\n"
        f"🏷️ Tag: <code>{html.escape(tags_preview)}</code>\n"
        f"✉️ Pesan:\n{html.escape(custom_msg)}\n\n"
        "Klik <b>✅ Simpan Jadwal</b> untuk mengaktifkan."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    elif update.message:
        await update.message.reply_html(text, reply_markup=keyboard)
    return CONFIRM_SAVE


async def wiz_mentions_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    context.user_data.setdefault("wiz", {})["mentions"] = []
    return await _show_wiz_confirmation(update, context)


async def wiz_mentions_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ASK_MENTIONS

    try:
        mentions = parse_mentions(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return ASK_MENTIONS

    context.user_data.setdefault("wiz", {})["mentions"] = mentions
    return await _show_wiz_confirmation(update, context)


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
    mentions: list[str] = wiz.get("mentions", [])

    new_job = save_new_custom_job(context, chat_ids, spec, custom_msg, mentions=mentions)
    tags_label = " ".join(mentions) if mentions else "-"
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
        f"🎯 Target: <code>{chat_ids}</code>\n"
        f"🏷️ Tag: <code>{html.escape(tags_label)}</code>",
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
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
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
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
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
    tags_display = " ".join(matched.mentions) if matched.mentions else "(tidak ada)"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Ubah Pesan", callback_data="editact:msg"),
                InlineKeyboardButton("⏰ Ubah Waktu", callback_data="editact:time"),
            ],
            [
                InlineKeyboardButton("🎯 Ubah Chat ID", callback_data="editact:target"),
                InlineKeyboardButton("🏷️ Ubah Tag", callback_data="editact:mentions"),
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
        f"🏷️ <b>Tag:</b> <code>{html.escape(tags_display)}</code>\n"
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
    current_targets: list[int | str] = list(matched.chat_ids) if matched else []
    context.user_data["edit_selected_ids"] = list(current_targets)

    chat = update.effective_chat
    chat_id = chat.id if chat else 0
    known_groups: dict[str, dict[str, str]] = context.bot_data.get("known_groups", {})
    keyboard = build_target_picker_keyboard(chat_id, known_groups, current_targets, prefix="edittgt")

    await query.edit_message_text(
        f"<b>🎯 Ubah Target Chat ID</b> (<code>{html.escape(target_id)}</code>)\n\n"
        f"Target saat ini: <code>{current_targets}</code>\n\n"
        "Centang grup/chat tujuan di bawah lalu klik <b>✅ Lanjut</b>, "
        "atau <b>ketik Chat ID baru</b> (pisah koma):",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return EDIT_NEW_TARGET


async def edit_target_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    cid = parse_single_target_id(query.data.split(":", 2)[2])
    selected: list[int | str] = list(context.user_data.get("edit_selected_ids", []))
    if cid in selected:
        selected.remove(cid)
    else:
        selected.append(cid)
    context.user_data["edit_selected_ids"] = selected

    chat = update.effective_chat
    chat_id = chat.id if chat else 0
    known_groups: dict[str, dict[str, str]] = context.bot_data.get("known_groups", {})
    keyboard = build_target_picker_keyboard(chat_id, known_groups, selected, prefix="edittgt")
    await query.edit_message_reply_markup(reply_markup=keyboard)
    return EDIT_NEW_TARGET


async def edit_target_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    selected: list[int | str] = list(context.user_data.pop("edit_selected_ids", []))
    if not selected:
        await query.answer("Pilih minimal 1 grup atau chat terlebih dahulu!", show_alert=True)
        return EDIT_NEW_TARGET
    await query.answer()
    return await _apply_edited_target(update, context, selected)


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


async def edit_ask_mentions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    target_id = context.user_data.get("edit_job_id", "")
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)
    current_tags = " ".join(matched.mentions) if (matched and matched.mentions) else "(tidak ada)"

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧹 Hapus Semua Tag", callback_data="editmentions:clear")],
            [InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel")],
        ]
    )
    await query.edit_message_text(
        f"<b>🏷️ Ubah Tag / Mention</b> (<code>{html.escape(target_id)}</code>)\n\n"
        f"Tag saat ini: <code>{html.escape(current_tags)}</code>\n\n"
        "Ketik username baru yang ingin di-tag (pisahkan spasi atau koma):\n"
        "• <code>@budi @ani</code>\n"
        "• <code>Budi:123456789</code> (jika akun tidak punya username)",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return EDIT_NEW_MENTIONS


async def _apply_edited_mentions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mentions: list[str],
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

    matched.mentions = mentions
    save_schedules(config.schedules_file, jobs)

    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    register_jobs(scheduler, context.bot, jobs, config)
    context.bot_data["jobs"] = jobs

    tags_label = " ".join(mentions) if mentions else "(kosong / dihapus)"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🚀 Tes Kirim {matched.id} Sekarang", callback_data=f"job:test:{matched.id}")],
            [InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="menu:home")],
        ]
    )
    msg = (
        f"<b>✅ Tag / Mention untuk <code>{matched.id}</code> berhasil diperbarui!</b>\n\n"
        f"🏷️ <b>Tag baru:</b> <code>{html.escape(tags_label)}</code>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=keyboard)
    elif update.message:
        await update.message.reply_html(msg, reply_markup=keyboard)
    return ConversationHandler.END


async def edit_clear_mentions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    return await _apply_edited_mentions(update, context, [])


async def edit_save_mentions_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return EDIT_NEW_MENTIONS

    try:
        mentions = parse_mentions(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return EDIT_NEW_MENTIONS

    return await _apply_edited_mentions(update, context, mentions)


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


async def wiz_fallback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Reset sesi wizard saat user mengirim /menu atau /start di tengah proses."""
    context.user_data.pop("wiz", None)
    context.user_data.pop("edit_job_id", None)
    await cmd_menu(update, context)
    return ConversationHandler.END


async def wiz_fallback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Reset sesi wizard saat user menekan tombol navigasi menu/tes/hapus di tengah proses."""
    context.user_data.pop("wiz", None)
    context.user_data.pop("edit_job_id", None)
    await handle_menu_callback(update, context)
    return ConversationHandler.END


# ==================== CALLBACK MENU UMUM ====================


async def target_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mulai input target baru (Grup / User / Channel / Lainnya)."""
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return ConversationHandler.END
    await query.answer()

    target_type = query.data.split(":", 1)[1]
    context.user_data["target_add_type"] = target_type

    hints = {
        "group": ("👥 Tambah ID Grup", "<code>-1001234567890</code> atau <code>Grup Kantor | -1001234567890</code>"),
        "user": ("👤 Tambah ID User", "<code>217411824</code> atau <code>Budi | 217411824</code>"),
        "channel": ("📢 Tambah ID / Username Channel", "<code>@namachannel</code>, <code>-1001234567890</code>, atau <code>Info Promo | @namachannel</code>"),
        "other": ("📌 Tambah Target Lainnya", "<code>Nama Bebas | ID_atau_@username</code>"),
    }
    title, example = hints.get(target_type, hints["other"])

    await query.edit_message_text(
        f"<b>{title}</b>\n\n"
        f"Ketik ID target (atau <code>Nama | ID</code>):\n"
        f"Contoh: {example}\n\n"
        "<i>Tips: Jika tidak menulis nama, bot otomatis mengambil nama aslinya dari Telegram jika terjangkau.</i>",
        parse_mode="HTML",
        reply_markup=build_cancel_keyboard(),
    )
    return TARGET_ADD_INPUT


async def target_add_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return TARGET_ADD_INPUT

    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return ConversationHandler.END

    try:
        custom_label, target_id = parse_target_entry(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return TARGET_ADD_INPUT

    target_type = str(context.user_data.pop("target_add_type", "other"))
    resolved_title = custom_label

    if not resolved_title:
        try:
            chat_obj = await context.bot.get_chat(target_id)
            resolved_title = chat_obj.title or chat_obj.full_name or chat_obj.username or str(target_id)
            if target_type == "other" and chat_obj.type:
                target_type = chat_obj.type
        except TelegramError:
            resolved_title = str(target_id)

    remember_group(context.bot_data, target_id, resolved_title, target_type)
    known_groups: dict[str, dict[str, str]] = context.bot_data.get("known_groups", {})
    text, markup = format_targets_menu(known_groups)

    await update.message.reply_html(
        f"<b>✅ Target tersimpan!</b> {target_type_icon(target_type)} "
        f"<b>{html.escape(resolved_title)}</b> (<code>{html.escape(str(target_id))}</code>)\n\n{text}",
        reply_markup=markup,
    )
    return ConversationHandler.END


async def cmd_targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return
    known_groups: dict[str, dict[str, str]] = context.bot_data.get("known_groups", {})
    text, markup = format_targets_menu(known_groups)
    await update.message.reply_html(text, reply_markup=markup)


async def admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return ConversationHandler.END
    await query.answer()

    await query.edit_message_text(
        "<b>➕ Tambah Admin Baru</b>\n\n"
        "Ketik <b>User ID Telegram</b> orang yang ingin diberi akses bot "
        "(boleh lebih dari satu dipisah koma, contoh: <code>123456789, 987654321</code>).\n\n"
        "<i>Tips: Suruh orang tersebut klik Start di bot — bot akan menampilkan User ID mereka di pesan akses ditolak.</i>",
        parse_mode="HTML",
        reply_markup=build_cancel_keyboard(),
    )
    return ADMIN_ADD_ID


async def admin_add_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ADMIN_ADD_ID

    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return ConversationHandler.END

    try:
        new_ids = parse_user_ids(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return ADMIN_ADD_ID

    extra_admins: set[int] = set(context.bot_data.get("extra_admins", set()))
    extra_admins.update(new_ids)
    save_extra_admins(config.resolved_admins_file, extra_admins)
    context.bot_data["extra_admins"] = extra_admins

    text, markup = format_admins_menu(config, extra_admins)
    await update.message.reply_html(
        f"<b>✅ Berhasil menambahkan admin:</b> <code>{new_ids}</code>\n\n{text}",
        reply_markup=markup,
    )
    return ConversationHandler.END


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return

    await query.answer()
    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    data = query.data

    if data == "menu:home":
        text = format_main_menu_text(update, config, len(scheduler.get_jobs()))
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_main_menu_keyboard())
        return

    if data == "menu:targets":
        known_groups: dict[str, dict[str, str]] = context.bot_data.get("known_groups", {})
        text, markup = format_targets_menu(known_groups)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return

    if data.startswith("tgtdel:"):
        cid_str = data.split(":", 1)[1]
        forget_group(context.bot_data, cid_str)
        known_groups = context.bot_data.get("known_groups", {})
        text, markup = format_targets_menu(known_groups)
        await query.edit_message_text(
            f"🗑️ Target <code>{html.escape(cid_str)}</code> dihapus.\n\n{text}",
            parse_mode="HTML",
            reply_markup=markup,
        )
        return

    if data == "menu:admins":
        extra_admins: set[int] = context.bot_data.get("extra_admins", set())
        text, markup = format_admins_menu(config, extra_admins)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return

    if data.startswith("admin:del:"):
        uid = int(data.split(":", 2)[2])
        if uid in config.admin_ids:
            await query.answer("Owner dari ADMIN_IDS tidak bisa dihapus.", show_alert=True)
            return
        extra_admins = set(context.bot_data.get("extra_admins", set()))
        extra_admins.discard(uid)
        save_extra_admins(config.resolved_admins_file, extra_admins)
        context.bot_data["extra_admins"] = extra_admins
        text, markup = format_admins_menu(config, extra_admins)
        await query.edit_message_text(
            f"🗑️ Admin <code>{uid}</code> dihapus.\n\n{text}",
            parse_mode="HTML",
            reply_markup=markup,
        )
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
    extra_admins = load_extra_admins(config.resolved_admins_file)
    known_groups = load_known_groups(config.resolved_groups_file)

    # Otomatis ambil nama grup dari chat_ids negatif yang sudah ada di schedules.json
    groups_updated = False
    for job in jobs:
        for cid in job.chat_ids:
            if isinstance(cid, int) and cid < 0 and str(cid) not in known_groups:
                try:
                    chat_obj = await application.bot.get_chat(cid)
                    known_groups[str(chat_obj.id)] = {
                        "title": chat_obj.title or f"Grup {chat_obj.id}",
                        "type": chat_obj.type,
                    }
                    groups_updated = True
                except ChatMigrated as migrated:
                    try:
                        chat_obj = await application.bot.get_chat(migrated.new_chat_id)
                        known_groups[str(chat_obj.id)] = {
                            "title": chat_obj.title or f"Grup {chat_obj.id}",
                            "type": chat_obj.type,
                        }
                        groups_updated = True
                    except TelegramError:
                        pass
                except TelegramError:
                    pass
    if groups_updated:
        save_known_groups(config.resolved_groups_file, known_groups)

    scheduler = AsyncIOScheduler(timezone=config.tzinfo)
    register_jobs(scheduler, application.bot, jobs, config)
    scheduler.start()

    application.bot_data["scheduler"] = scheduler
    application.bot_data["jobs"] = jobs
    application.bot_data["extra_admins"] = extra_admins
    application.bot_data["known_groups"] = known_groups

    await application.bot.set_my_commands(
        [
            BotCommand("menu", "Buka menu utama interaktif"),
            BotCommand("targets", "Kelola ID Grup/User/Channel"),
            BotCommand("admin", "Kelola daftar admin bot"),
            BotCommand("schedule", "Buat jadwal pesan cepat"),
            BotCommand("setpesan", "Ubah custom pesan jadwal"),
            BotCommand("list", "Lihat daftar jadwal aktif"),
            BotCommand("chatid", "Cek Chat ID saat ini"),
            BotCommand("reload", "Muat ulang schedules.json"),
            BotCommand("cancel", "Batalkan proses saat ini"),
        ]
    )
    all_admins = get_all_admin_ids(config, application.bot_data)
    logger.info(
        "Bot siap, menu perintah terdaftar, admin=%s, scheduler berjalan (timezone=%s).",
        sorted(all_admins) if all_admins else "TERBUKA",
        config.timezone,
    )


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
            CallbackQueryHandler(admin_add_start, pattern=r"^admin:add$"),
            CallbackQueryHandler(target_add_start, pattern=r"^tgtadd:.+$"),
        ],
        states={
            ASK_TARGET: [
                CallbackQueryHandler(wiz_target_toggle, pattern=r"^wiztgt:toggle:.+$"),
                CallbackQueryHandler(wiz_target_done, pattern=r"^wiztgt:done$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_target_text),
            ],
            ASK_DATETIME: [
                CallbackQueryHandler(wiz_datetime_quick, pattern=r"^time:quick:.+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_datetime_text),
            ],
            ASK_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_message_text),
            ],
            ASK_MENTIONS: [
                CallbackQueryHandler(wiz_mentions_skip, pattern=r"^wiz:mentions:skip$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_mentions_text),
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
                CallbackQueryHandler(edit_ask_mentions, pattern=r"^editact:mentions$"),
            ],
            EDIT_NEW_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_save_message),
            ],
            EDIT_NEW_TIME: [
                CallbackQueryHandler(edit_save_time_quick, pattern=r"^time:quick:.+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_save_time_text),
            ],
            EDIT_NEW_TARGET: [
                CallbackQueryHandler(edit_target_toggle, pattern=r"^edittgt:toggle:.+$"),
                CallbackQueryHandler(edit_target_done, pattern=r"^edittgt:done$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_save_target_text),
            ],
            EDIT_NEW_MENTIONS: [
                CallbackQueryHandler(edit_clear_mentions, pattern=r"^editmentions:clear$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_save_mentions_text),
            ],
            ADMIN_ADD_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_save),
            ],
            TARGET_ADD_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, target_add_save),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(wiz_cancel, pattern=r"^wiz:cancel$"),
            CommandHandler("cancel", wiz_cancel),
            CommandHandler(["start", "help", "menu"], wiz_fallback_menu),
            CallbackQueryHandler(wiz_fallback_callback, pattern=r"^(menu:|job:|admin:del:|tgtdel:)"),
        ],
        allow_reentry=True,
        per_message=False,
    )

    # Passive tracker grup/channel agar daftar grup selalu up-to-date otomatis
    application.add_handler(ChatMemberHandler(track_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER), group=-1)
    application.add_handler(MessageHandler(filters.ChatType.GROUPS, track_group_activity), group=-1)

    application.add_handler(wizard_handler)
    application.add_handler(CommandHandler(["start", "help", "menu"], cmd_menu))
    application.add_handler(CommandHandler(["targets", "target"], cmd_targets))
    application.add_handler(CommandHandler(["admin", "admins"], cmd_admins))
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
