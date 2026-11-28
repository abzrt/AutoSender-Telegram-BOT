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
import base64
import html
import io
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot, BotCommand, ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, ChatMigrated, RetryAfter, TelegramError
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
    DAY_LABELS,
    BotConfig,
    ScheduleJob,
    append_delivery_history,
    ensure_schedules_file,
    extract_mention_user_ids,
    format_active_window,
    format_days_short,
    is_within_active_window,
    load_config,
    load_delivery_history,
    load_extra_admins,
    load_known_groups,
    load_known_members,
    load_runtime_state,
    load_schedules,
    load_tag_presets,
    parse_active_window,
    parse_flexible_schedule,
    parse_mentions,
    parse_run_at,
    parse_user_ids,
    render_dynamic_placeholders,
    render_message_with_mentions,
    save_extra_admins,
    save_known_groups,
    save_known_members,
    save_runtime_state,
    save_schedules,
    save_tag_presets,
)
from wfm_worker import WfmFlowWorker, build_flow_summary_caption, unmask_caption_with_flow_data

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram-autosender")
logging.getLogger("httpx").setLevel(logging.WARNING)

# State untuk ConversationHandler Custom Menu, Target Book, Preset Tim, & Manajemen Admin
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
    EDIT_NEW_HOURS,
    EDIT_NEW_THREAD,
    ADMIN_ADD_ID,
    TARGET_ADD_INPUT,
    PRESET_ASK_NAME,
    PRESET_PICK_MEMBERS,
    RESTORE_UPLOAD,
) = range(18)


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


DIVISION_LABELS: dict[str, str] = {
    "provisioning": "📦 Provisioning",
    "assurance": "🛠️ Assurance",
    "maintenance": "🔧 Maintenance",
}

ADING_TECH_CHANNELS: dict[str, str] = {
    "ARIFIN": "-1002202217652",
    "MUHAMMAD RISQI": "-1005839757356",
    "AHMAD SYAHRIZAL RAMADHANI": "-1002280689139",
    "ANJELLO": "-1002960116555",
    "ANDI MUHAMMAD RAJAB": "-1003818907805",
    "MUHAMMAD NAZAR ALFATH": "-1004329000380",
    "ALIF JUNAIDI": "-1002975699950",
}

ADING_TECH_MENTIONS: dict[str, str] = {
    "ARIFIN": "@Mimpi_bergadang",
    "ANJELLO": "@embu_mamo_SK",
}


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Keyboard menu awal bot dengan 3 divisi utama + menu Setting."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📦 Provisioning", callback_data="menu:provisioning")],
            [InlineKeyboardButton("🛠️ Assurance", callback_data="menu:assurance")],
            [InlineKeyboardButton("🔧 Maintenance", callback_data="menu:maintenance")],
            [InlineKeyboardButton("⚙️ Setting", callback_data="menu:setting")],
        ]
    )


def build_autosender_menu_keyboard() -> InlineKeyboardMarkup:
    """Keyboard menu Setting (kelola semua jadwal, target, preset tim, admin, backup)."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✨ Custom Pesan & Atur Waktu", callback_data="menu:custom")],
            [
                InlineKeyboardButton("📋 Semua Jadwal", callback_data="menu:list"),
                InlineKeyboardButton("📇 Kelola Target", callback_data="menu:targets"),
            ],
            [
                InlineKeyboardButton("👥 Preset Tim", callback_data="menu:presets"),
                InlineKeyboardButton("📜 Riwayat Kirim", callback_data="menu:history"),
            ],
            [
                InlineKeyboardButton("👑 Kelola Admin", callback_data="menu:admins"),
                InlineKeyboardButton("💾 Backup Data", callback_data="menu:backup"),
            ],
            [
                InlineKeyboardButton("🆔 Cek Chat ID", callback_data="menu:chatid"),
                InlineKeyboardButton("🔄 Reload", callback_data="menu:reload"),
            ],
            [InlineKeyboardButton("⬅️ Kembali ke Menu Awal", callback_data="menu:home")],
        ]
    )


def build_provisioning_menu_keyboard() -> InlineKeyboardMarkup:
    """Sub-menu Provisioning (terintegrasi dengan ADING Mobile -> WFM FLOW)."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📱 ADING Mobile & Web (Auto-Dispatch)", callback_data="menu:ading")],
            [
                InlineKeyboardButton("🖥️ Status WFM & Channel", callback_data="prov:wfmstatus"),
                InlineKeyboardButton("📡 Tes 7 Channel", callback_data="prov:testchannels"),
            ],
            [InlineKeyboardButton("✨ Buat Jadwal Provisioning", callback_data="menu:custom")],
            [
                InlineKeyboardButton("📋 Jadwal Provisioning", callback_data="menu:list:provisioning"),
                InlineKeyboardButton("📜 Riwayat Kirim", callback_data="menu:history"),
            ],
            [
                InlineKeyboardButton("📇 Kelola Target", callback_data="menu:targets"),
                InlineKeyboardButton("🆔 Cek Chat ID / Topic", callback_data="menu:chatid"),
            ],
            [InlineKeyboardButton("⬅️ Kembali ke Menu Awal", callback_data="menu:home")],
        ]
    )


def build_ading_menu_keyboard() -> InlineKeyboardMarkup:
    """Sub-menu khusus integrasi ADING Mobile di dalam Provisioning."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬇️ Download ADING Mobile APK",
                    url="https://github.com/abzrt/ading-android-app/raw/main/Ading%20Mobile.apk",
                )
            ],
            [
                InlineKeyboardButton(
                    "🌐 Download ADING Web Extension (.zip)",
                    url="https://github.com/abzrt/ading-android-app/raw/main/ADING-Web-Extension.zip",
                )
            ],
            [InlineKeyboardButton("🌐 Buka Web ADING (ading.web.id)", url="https://ading.web.id/")],
            [InlineKeyboardButton("👷 Mapping Teknisi (Channel + Pribadi)", callback_data="ading:techmap")],
            [
                InlineKeyboardButton("🧪 Test Kirim ADING", callback_data="ading:test"),
                InlineKeyboardButton("🆔 Cek Chat / Topic ID", callback_data="menu:chatid"),
            ],
            [
                InlineKeyboardButton("⬅️ Kembali ke Provisioning", callback_data="menu:provisioning"),
                InlineKeyboardButton("🏠 Menu Awal", callback_data="menu:home"),
            ],
        ]
    )


def build_assurance_menu_keyboard() -> InlineKeyboardMarkup:
    """Sub-menu Assurance."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✨ Buat Jadwal Assurance", callback_data="menu:custom")],
            [
                InlineKeyboardButton("📋 Jadwal Assurance", callback_data="menu:list:assurance"),
                InlineKeyboardButton("📇 Kelola Target", callback_data="menu:targets"),
            ],
            [
                InlineKeyboardButton("👥 Preset Tim", callback_data="menu:presets"),
                InlineKeyboardButton("📜 Riwayat Kirim", callback_data="menu:history"),
            ],
            [InlineKeyboardButton("⬅️ Kembali ke Menu Awal", callback_data="menu:home")],
        ]
    )


def build_maintenance_menu_keyboard() -> InlineKeyboardMarkup:
    """Sub-menu Maintenance."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✨ Buat Jadwal Maintenance", callback_data="menu:custom")],
            [
                InlineKeyboardButton("📋 Jadwal Maintenance", callback_data="menu:list:maintenance"),
                InlineKeyboardButton("📇 Kelola Target", callback_data="menu:targets"),
            ],
            [
                InlineKeyboardButton("👥 Preset Tim", callback_data="menu:presets"),
                InlineKeyboardButton("📜 Riwayat Kirim", callback_data="menu:history"),
            ],
            [InlineKeyboardButton("⬅️ Kembali ke Menu Awal", callback_data="menu:home")],
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
    if known_groups:
        buttons.append([InlineKeyboardButton("🔄 Refresh Nama Grup/Target", callback_data="tgtrefresh")])

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
    username: str | None = None,
) -> None:
    """Simpan atau perbarui target (grup/user/channel/lainnya) ke groups.json."""
    config: BotConfig = bot_data["config"]
    groups: dict[str, dict[str, str]] = bot_data.setdefault("known_groups", {})
    key = str(chat_id)
    prev = groups.get(key) or {}
    new_entry: dict[str, str] = {"title": title or f"Target {chat_id}", "type": chat_type}
    resolved_uname = (username if username is not None else prev.get("username", "") or "").lstrip("@").strip()
    if resolved_uname:
        new_entry["username"] = resolved_uname
    if prev != new_entry:
        groups[key] = new_entry
        save_known_groups(config.resolved_groups_file, groups)


def forget_group(bot_data: dict[str, Any], chat_id: int | str) -> None:
    """Hapus target dari groups.json."""
    config: BotConfig = bot_data["config"]
    groups: dict[str, dict[str, str]] = bot_data.setdefault("known_groups", {})
    if groups.pop(str(chat_id), None) is not None:
        save_known_groups(config.resolved_groups_file, groups)


def remember_member_user(bot_data: dict[str, Any], chat_id: int | str, user: Any) -> None:
    """Catat satu user Telegram (non-bot) ke daftar anggota grup untuk checklist tag."""
    if not user or getattr(user, "is_bot", False):
        return
    uid = getattr(user, "id", None)
    if not uid:
        return
    username = getattr(user, "username", None)
    first_name = (getattr(user, "first_name", None) or "").strip()
    last_name = (getattr(user, "last_name", None) or "").strip()
    full_name = " ".join(p for p in [first_name, last_name] if p).strip()

    if username:
        token = f"@{username}"
        label = f"{full_name} (@{username})" if full_name else f"@{username}"
    else:
        clean_name = (full_name or f"User{uid}").replace(":", " ").strip()
        token = f"{clean_name}:{uid}"
        label = clean_name

    config: BotConfig = bot_data["config"]
    members: dict[str, dict[str, dict[str, str]]] = bot_data.setdefault("known_members", {})
    scope_dict = members.setdefault(str(chat_id), {})
    entry = {"label": label, "token": token}
    if scope_dict.get(str(uid)) != entry:
        scope_dict[str(uid)] = entry
        save_known_members(config.resolved_members_file, members)


def remember_manual_mentions(bot_data: dict[str, Any], mentions: list[str]) -> None:
    """Simpan tag manual yang diketik user ke scope '_saved' agar bisa diklik lagi lain kali."""
    if not mentions:
        return
    config: BotConfig = bot_data["config"]
    members: dict[str, dict[str, dict[str, str]]] = bot_data.setdefault("known_members", {})
    saved = members.setdefault("_saved", {})
    changed = False
    for token in mentions:
        key = token.lower()
        label = token.split(":", 1)[0] if ":" in token else token
        entry = {"label": label, "token": token}
        if saved.get(key) != entry:
            saved[key] = entry
            changed = True
    if changed:
        save_known_members(config.resolved_members_file, members)


async def collect_mention_candidates(
    bot: Bot,
    bot_data: dict[str, Any],
    chat_ids: list[int | str],
) -> list[dict[str, str]]:
    """Kumpulkan HANYA user yang disimpan sendiri di Kelola Target (👤 User) & tag manual tersimpan."""
    candidates: list[dict[str, str]] = []
    seen_tokens: set[str] = set()

    # 1. Ambil user yang disimpan sendiri di Kelola Target (type == user / private)
    known_groups: dict[str, dict[str, str]] = bot_data.get("known_groups", {})
    for cid_str, info in known_groups.items():
        ttype = (info.get("type") or "").lower()
        if ttype not in ("user", "private"):
            continue
        title = (info.get("title") or cid_str).replace(":", " ").strip()
        saved_uname = (info.get("username") or "").lstrip("@").strip()
        if cid_str.startswith("@"):
            token = cid_str
            label = f"{title} ({cid_str})" if title.lower() != cid_str.lower() else cid_str
        elif saved_uname:
            token = f"@{saved_uname}"
            label = f"{title} (@{saved_uname})"
        else:
            token = f"{title}:{cid_str}"
            label = title
        if token.lower() not in seen_tokens:
            seen_tokens.add(token.lower())
            candidates.append({"label": label, "token": token})

    # 2. Ambil tag manual yang pernah diketik user sendiri (_saved)
    members: dict[str, dict[str, dict[str, str]]] = bot_data.get("known_members", {})
    for info in members.get("_saved", {}).values():
        token = info.get("token", "")
        if token and token.lower() not in seen_tokens:
            seen_tokens.add(token.lower())
            candidates.append({"label": info.get("label") or token, "token": token})

    candidates.sort(key=lambda item: item["label"].lower())
    return candidates[:24]


def build_mentions_picker_keyboard(
    candidates: list[dict[str, str]],
    selected_tokens: list[str],
    prefix: str = "wiztag",
    allow_clear: bool = False,
    preset_names: list[str] | None = None,
) -> InlineKeyboardMarkup:
    """Bangun keyboard checklist untuk memilih siapa saja (atau preset tim) yang akan di-tag."""
    selected_lower = {t.lower() for t in selected_tokens}
    buttons: list[list[InlineKeyboardButton]] = []

    if preset_names:
        prow: list[InlineKeyboardButton] = []
        for pidx, pname in enumerate(preset_names[:8]):
            short = pname if len(pname) <= 18 else pname[:15] + "..."
            prow.append(InlineKeyboardButton(f"👥 Tim: {short}", callback_data=f"{prefix}:p:{pidx}"))
            if len(prow) == 2:
                buttons.append(prow)
                prow = []
        if prow:
            buttons.append(prow)

    row: list[InlineKeyboardButton] = []
    for idx, item in enumerate(candidates):
        label = item["label"]
        if len(label) > 22:
            label = label[:19] + "..."
        mark = "☑️" if item["token"].lower() in selected_lower else "⬜"
        row.append(InlineKeyboardButton(f"{mark} {label}", callback_data=f"{prefix}:t:{idx}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    action_row: list[InlineKeyboardButton] = []
    if selected_tokens:
        action_row.append(
            InlineKeyboardButton(f"✅ Selesai ({len(selected_tokens)} di-tag)", callback_data=f"{prefix}:done")
        )
    else:
        action_row.append(
            InlineKeyboardButton("⏭️ Lewati (Tanpa Tag)", callback_data=f"{prefix}:done")
        )
    if allow_clear and selected_tokens:
        action_row.append(InlineKeyboardButton("🧹 Kosongkan", callback_data=f"{prefix}:clear"))
    buttons.append(action_row)
    buttons.append([InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel")])
    return InlineKeyboardMarkup(buttons)


def format_presets_menu(presets: dict[str, list[str]]) -> tuple[str, InlineKeyboardMarkup]:
    """Susun tampilan menu Preset Tim / Shift Tag."""
    lines = ["<b>👥 Preset Tim / Shift Tag</b>\n"]
    if presets:
        for name, tokens in sorted(presets.items(), key=lambda kv: kv[0].lower()):
            compact = "".join(name.split())
            preview = ", ".join(t.split(":", 1)[0] for t in tokens)
            lines.append(f"• <b>{html.escape(name)}</b> (ketik <code>@{html.escape(compact)}</code>)\n  Anggota: <code>{html.escape(preview)}</code>")
    else:
        lines.append("<i>Belum ada preset tim. Buat preset supaya bisa tag 1 tim sekaligus dalam 1 klik!</i>")

    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("➕ Buat Preset Tim Baru", callback_data="preset:new")]
    ]
    for idx, name in enumerate(sorted(presets.keys(), key=str.lower)):
        buttons.append([InlineKeyboardButton(f"🗑️ Hapus Tim: {name[:22]}", callback_data=f"presetdel:{idx}")])
    buttons.append([InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="menu:home")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def format_history_menu(history: list[dict[str, Any]]) -> tuple[str, InlineKeyboardMarkup]:
    """Susun tampilan Riwayat Pengiriman terakhir."""
    lines = ["<b>📜 Riwayat Pengiriman Terakhir</b>\n"]
    if not history:
        lines.append("<i>Belum ada riwayat pengiriman tercatat.</i>")
    else:
        for entry in history[:15]:
            icon = "✅" if entry.get("status") == "ok" else "❌"
            ts = html.escape(str(entry.get("timestamp", "-")))
            jid = html.escape(str(entry.get("job_id", "-")))
            cid = html.escape(str(entry.get("chat_id", "-")))
            err = entry.get("error")
            line = f"{icon} <code>{ts}</code> | <code>{jid}</code> → <code>{cid}</code>"
            if err:
                line += f"\n    <i>{html.escape(str(err)[:80])}</i>"
            lines.append(line)

    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Refresh Riwayat", callback_data="menu:history")],
            [InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="menu:home")],
        ]
    )
    return "\n".join(lines), markup


def build_active_hours_prompt(job_id: str, current_label: str) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"<b>🕒 Atur Jam Operasional</b> (<code>{html.escape(job_id)}</code>)\n\n"
        f"Saat ini: <code>{html.escape(current_label)}</code>\n\n"
        "Pilih tombol cepat di bawah atau <b>ketik bebas</b>:\n"
        "• <code>08:00-17:00</code> (setiap hari jam 08:00–17:00 WITA)\n"
        "• <code>08:00-17:00 sen-jum</code> (Senin–Jumat saja)\n"
        "• <code>08:00-17:00 sen,rab,jum</code> (hari tertentu)\n"
        "• <code>24 jam</code> (nonaktifkan batasan jam)"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏢 Jam Kerja (08:00-17:00 Sen-Jum)", callback_data="hours:q:08:00-17:00 sen-jum")],
            [InlineKeyboardButton("☀️ Harian (08:00-21:00 Setiap Hari)", callback_data="hours:q:08:00-21:00 semua")],
            [
                InlineKeyboardButton("🌐 24 Jam (Tanpa Batas)", callback_data="hours:q:24 jam"),
                InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel"),
            ],
        ]
    )
    return text, keyboard


def extract_chat_title(chat_obj: Any) -> str:
    """Ambil nama terbaik dari objek Chat Telegram."""
    title = getattr(chat_obj, "title", None)
    if title:
        return str(title)
    full_name = getattr(chat_obj, "full_name", None)
    if full_name:
        return str(full_name)
    first_name = getattr(chat_obj, "first_name", None)
    last_name = getattr(chat_obj, "last_name", None)
    combined = " ".join(p for p in [first_name, last_name] if p).strip()
    if combined:
        return combined
    username = getattr(chat_obj, "username", None)
    if username:
        return f"@{username}"
    return str(getattr(chat_obj, "id", ""))


async def resolve_chat_target_details(
    bot: Bot,
    target_id: int | str,
    default_type: str = "other",
    known_groups: dict[str, dict[str, str]] | None = None,
) -> tuple[int | str, str, str, bool, str | None]:
    """Resolve nama asli, tipe, & username dari Telegram (mendukung migrasi supergroup, fallback -100, & get_chat_member).

    Mengembalikan (resolved_id, resolved_title, resolved_type, fetched_ok, username).
    """
    candidates: list[int | str] = [target_id]
    if isinstance(target_id, int):
        s_id = str(target_id)
        if default_type in ("group", "channel") and not s_id.startswith("-100"):
            candidates.append(int(f"-100{abs(target_id)}"))
        elif target_id < 0 and not s_id.startswith("-100"):
            candidates.append(int(f"-100{abs(target_id)}"))

    for cand in candidates:
        try:
            chat_obj = await bot.get_chat(cand)
            resolved_type = getattr(chat_obj, "type", None) or default_type
            uname = getattr(chat_obj, "username", None)
            return chat_obj.id, extract_chat_title(chat_obj), str(resolved_type), True, uname
        except ChatMigrated as migrated:
            try:
                chat_obj = await bot.get_chat(migrated.new_chat_id)
                resolved_type = getattr(chat_obj, "type", None) or "supergroup"
                uname = getattr(chat_obj, "username", None)
                return chat_obj.id, extract_chat_title(chat_obj), str(resolved_type), True, uname
            except TelegramError:
                return migrated.new_chat_id, str(migrated.new_chat_id), "supergroup", False, None
        except TelegramError as exc:
            logger.info("get_chat(%s) gagal: %s", cand, exc)
            continue

    # Fallback untuk user numerik yang belum pernah /start bot: cari di grup yang dikenal bot
    if isinstance(target_id, int) and target_id > 0 and known_groups:
        for gid_str, info in known_groups.items():
            gtype = (info.get("type") or "").lower()
            if gtype not in ("group", "supergroup"):
                continue
            try:
                gid = int(gid_str)
            except ValueError:
                continue
            try:
                member = await bot.get_chat_member(gid, target_id)
                user_obj = getattr(member, "user", None)
                if user_obj:
                    uname = getattr(user_obj, "username", None)
                    return target_id, extract_chat_title(user_obj), "user", True, uname
            except TelegramError:
                continue

    return target_id, str(target_id), default_type, False, None


async def resolve_chat_target(
    bot: Bot,
    target_id: int | str,
    default_type: str = "other",
) -> tuple[int | str, str, str, bool]:
    """Resolve nama asli & ID final dari Telegram (kompatibel dengan signature lama)."""
    final_id, title, rtype, fetched_ok, _ = await resolve_chat_target_details(bot, target_id, default_type)
    return final_id, title, rtype, fetched_ok


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

    # Pasif perbarui @username jika pengirim sudah tersimpan di Kelola Target (tanpa menambah user baru)
    user = update.effective_user
    if user and not getattr(user, "is_bot", False) and getattr(user, "username", None):
        uid_str = str(user.id)
        known_groups: dict[str, dict[str, str]] = context.bot_data.get("known_groups", {})
        if uid_str in known_groups:
            info = known_groups[uid_str]
            remember_group(
                context.bot_data,
                uid_str,
                info.get("title") or extract_chat_title(user),
                info.get("type", "user"),
                username=user.username,
            )


def build_flexible_time_prompt(config: BotConfig, title: str) -> tuple[str, InlineKeyboardMarkup]:
    """Teks panduan + tombol cepat untuk pengaturan waktu bebas."""
    now = datetime.now(config.tzinfo)
    t_5m = now + timedelta(minutes=5)
    t_30m = now + timedelta(minutes=30)
    t_1h = now + timedelta(hours=1)

    text = (
        f"<b>{title}</b>\n\n"
        f"🕒 Waktu realtime: <code>{now.strftime('%Y-%m-%d %H:%M:%S %Z')}</code>\n\n"
        "Pilih menu pengaturan (<b>Per Menit</b>, <b>Per Jam</b>, <b>Per Hari</b>), tombol cepat, atau <b>ketik waktu bebas</b>:\n"
        "• <b>Per menit:</b> <code>per menit</code>, <code>setiap 5 menit</code>, <code>per 15 menit sen-jum</code>\n"
        "• <b>Per jam:</b> <code>per jam</code>, <code>setiap 2 jam</code>, <code>per 3 jam sen,rab,jum</code>\n"
        "• <b>Per hari tertentu:</b> <code>sen,rab,jum 08:00</code>, <code>sen-jum 09:30</code>, <code>setiap hari 08:00</code>\n"
        "• <b>Tanggal &amp; jam:</b> <code>2026-11-23 15:30</code>, <code>15:30</code>, <code>besok 08:00</code>\n"
        "• <b>Dari sekarang:</b> <code>+10 menit</code>, <code>+2 jam</code>, <code>+30 detik</code>\n"
        "• <b>Cron 5-kolom:</b> <code>0 9 * * 1-5</code>"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏱️ Atur Per Menit", callback_data="time:mode:minute"),
                InlineKeyboardButton("🕐 Atur Per Jam", callback_data="time:mode:hour"),
            ],
            [
                InlineKeyboardButton("📅 Pilih Hari (Per Hari Apa Saja)", callback_data="time:mode:days"),
            ],
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


def build_minute_schedule_prompt(title: str) -> tuple[str, InlineKeyboardMarkup]:
    """Sub-menu pengaturan jadwal kirim per menit."""
    text = (
        f"<b>⏱️ {title} — Kirim Per Menit</b>\n\n"
        "Pilih interval menit di bawah, atau <b>ketik angka menit</b> (contoh: <code>7</code>, <code>setiap 12 menit</code>, "
        "atau <code>setiap 10 menit sen-jum</code>):"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Setiap 1 Menit", callback_data="time:quick:setiap 1 menit"),
                InlineKeyboardButton("Setiap 5 Menit", callback_data="time:quick:setiap 5 menit"),
            ],
            [
                InlineKeyboardButton("Setiap 10 Menit", callback_data="time:quick:setiap 10 menit"),
                InlineKeyboardButton("Setiap 15 Menit", callback_data="time:quick:setiap 15 menit"),
            ],
            [
                InlineKeyboardButton("Setiap 30 Menit", callback_data="time:quick:setiap 30 menit"),
                InlineKeyboardButton("Setiap 45 Menit", callback_data="time:quick:setiap 45 menit"),
            ],
            [
                InlineKeyboardButton("⬅️ Kembali", callback_data="time:mode:back"),
                InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel"),
            ],
        ]
    )
    return text, keyboard


def build_hour_schedule_prompt(title: str) -> tuple[str, InlineKeyboardMarkup]:
    """Sub-menu pengaturan jadwal kirim per jam."""
    text = (
        f"<b>🕐 {title} — Kirim Per Jam</b>\n\n"
        "Pilih interval jam di bawah, atau <b>ketik angka jam</b> (contoh: <code>5</code>, <code>setiap 4 jam</code>, "
        "atau <code>setiap 2 jam sen-jum</code>):"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Setiap 1 Jam", callback_data="time:quick:setiap 1 jam"),
                InlineKeyboardButton("Setiap 2 Jam", callback_data="time:quick:setiap 2 jam"),
            ],
            [
                InlineKeyboardButton("Setiap 3 Jam", callback_data="time:quick:setiap 3 jam"),
                InlineKeyboardButton("Setiap 4 Jam", callback_data="time:quick:setiap 4 jam"),
            ],
            [
                InlineKeyboardButton("Setiap 6 Jam", callback_data="time:quick:setiap 6 jam"),
                InlineKeyboardButton("Setiap 12 Jam", callback_data="time:quick:setiap 12 jam"),
            ],
            [
                InlineKeyboardButton("⬅️ Kembali", callback_data="time:mode:back"),
                InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel"),
            ],
        ]
    )
    return text, keyboard


def build_day_picker_prompt(title: str, selected_days: list[int]) -> tuple[str, InlineKeyboardMarkup]:
    """Checklist interaktif untuk memilih hari apa saja jadwal akan dikirim."""
    selected_set = set(selected_days)
    chosen_text = format_days_short(selected_days) if selected_days else "(belum dipilih)"
    text = (
        f"<b>📅 {title} — Pilih Hari Pengiriman</b>\n\n"
        f"Hari dipilih: <code>{html.escape(chosen_text)}</code>\n\n"
        "Centang hari apa saja yang diinginkan lalu klik <b>✅ Lanjut</b>, "
        "atau langsung ketik (contoh: <code>sen,rab,jum 08:00</code>):"
    )
    row1 = [
        InlineKeyboardButton(
            f"{'☑️' if d in selected_set else '⬜'} {DAY_LABELS[d]}",
            callback_data=f"time:day:t:{d}",
        )
        for d in (1, 2, 3, 4)
    ]
    row2 = [
        InlineKeyboardButton(
            f"{'☑️' if d in selected_set else '⬜'} {DAY_LABELS[d]}",
            callback_data=f"time:day:t:{d}",
        )
        for d in (5, 6, 7)
    ]
    buttons: list[list[InlineKeyboardButton]] = [
        row1,
        row2,
        [
            InlineKeyboardButton("🏢 Sen–Jum", callback_data="time:day:p:work"),
            InlineKeyboardButton("🌴 Sab–Min", callback_data="time:day:p:weekend"),
            InlineKeyboardButton("📆 Semua", callback_data="time:day:p:all"),
        ],
    ]
    if selected_days:
        buttons.append(
            [InlineKeyboardButton(f"✅ Lanjut Atur Jam/Interval ({len(selected_set)} hari)", callback_data="time:day:next")]
        )
    buttons.append(
        [
            InlineKeyboardButton("⬅️ Kembali", callback_data="time:mode:back"),
            InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel"),
        ]
    )
    return text, InlineKeyboardMarkup(buttons)


def build_day_time_prompt(title: str, selected_days: list[int]) -> tuple[str, InlineKeyboardMarkup]:
    """Pilih jam tetap / per menit / per jam untuk hari-hari yang sudah dicentang."""
    chosen_text = format_days_short(selected_days)
    text = (
        f"<b>📅 {title} — Atur Jam / Interval</b>\n\n"
        f"Hari dipilih: <code>{html.escape(chosen_text)}</code>\n\n"
        "Pilih jam atau interval di bawah, atau <b>ketik bebas</b>:\n"
        "• Jam tertentu: <code>08:30</code> atau <code>14:00</code>\n"
        "• Per menit di hari tsb: <code>setiap 10 menit</code> atau <code>10 menit</code>\n"
        "• Per jam di hari tsb: <code>setiap 2 jam</code> atau <code>2 jam</code>"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🕗 08:00", callback_data="time:dayapply:08:00"),
                InlineKeyboardButton("🕘 09:00", callback_data="time:dayapply:09:00"),
                InlineKeyboardButton("🕛 12:00", callback_data="time:dayapply:12:00"),
                InlineKeyboardButton("🕔 17:00", callback_data="time:dayapply:17:00"),
            ],
            [
                InlineKeyboardButton("⏱️ Tiap 15 Menit", callback_data="time:dayapply:setiap 15 menit"),
                InlineKeyboardButton("⏱️ Tiap 30 Menit", callback_data="time:dayapply:setiap 30 menit"),
            ],
            [
                InlineKeyboardButton("🕐 Tiap 1 Jam", callback_data="time:dayapply:setiap 1 jam"),
                InlineKeyboardButton("🕐 Tiap 2 Jam", callback_data="time:dayapply:setiap 2 jam"),
            ],
            [
                InlineKeyboardButton("⬅️ Ubah Hari", callback_data="time:mode:days"),
                InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel"),
            ],
        ]
    )
    return text, keyboard


def _resolve_time_text_with_mode(raw_text: str, user_data: dict[str, Any]) -> str:
    """Sesuaikan input teks bebas sesuai submenu waktu (per menit, per jam, atau per hari)."""
    text = raw_text.strip()
    mode = user_data.get("time_input_mode")
    if mode == "minute" and text.isdigit():
        return f"setiap {text} menit"
    if mode == "hour" and text.isdigit():
        return f"setiap {text} jam"
    if mode == "days_time":
        selected_days: list[int] = sorted(set(user_data.get("time_selected_days", [])))
        if selected_days:
            days_csv = ",".join(DAY_LABELS[d].lower() for d in selected_days if d in DAY_LABELS)
            low = re.sub(r"\s+", " ", text.lower())
            if text.isdigit():
                return f"setiap {text} menit {days_csv}"
            if re.fullmatch(r"\d{1,2}:\d{2}", low):
                return f"{days_csv} {text}"
            if low in ("permenit", "per menit", "setiap menit", "tiap menit", "perjam", "per jam", "setiap jam", "tiap jam"):
                return f"{text} {days_csv}"
            if re.fullmatch(r"(?:(?:setiap|tiap|interval|per)\s+)?\d+\s*(?:detik|dtk|menit|mnt|jam|hari)", low):
                prefix = "" if any(low.startswith(p) for p in ("setiap ", "tiap ", "interval ", "per ")) else "setiap "
                return f"{prefix}{text} {days_csv}"
    return text


async def resolve_mention_usernames_for_chat(
    bot: Bot,
    chat_id: int | str,
    user_ids: list[str],
    saved_targets: dict[str, dict[str, str]] | None = None,
    config: BotConfig | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Resolve @username Telegram untuk setiap User ID numerik di grup target via get_chat_member.

    Jika user memiliki @username di Telegram, otomatis upgrade ke @username agar mention
    100% aktif (biru, bisa diklik, dan mengirim notifikasi) walaupun user belum pernah /start bot.
    """
    uid_usernames: dict[str, str] = {}
    warnings: list[str] = []
    targets_updated = False

    for uid_str in user_ids:
        if saved_targets and uid_str in saved_targets:
            cached_uname = (saved_targets[uid_str].get("username") or "").lstrip("@").strip()
            if cached_uname:
                uid_usernames[uid_str] = cached_uname

        if not uid_str.isdigit():
            continue
        uid_int = int(uid_str)

        resolved_user: Any = None
        if isinstance(chat_id, int) and chat_id < 0:
            try:
                member = await bot.get_chat_member(chat_id, uid_int)
                resolved_user = getattr(member, "user", None)
                status = getattr(member, "status", "")
                if status in (ChatMember.LEFT, ChatMember.BANNED):
                    warnings.append(f"User ID {uid_str} bukan anggota aktif grup {chat_id} (status: {status}).")
            except TelegramError as exc:
                logger.info("get_chat_member(%s, %s) gagal: %s", chat_id, uid_int, exc)
                if uid_str not in uid_usernames:
                    warnings.append(f"User ID {uid_str} tidak ditemukan di grup {chat_id}.")

        if resolved_user is None and uid_str not in uid_usernames:
            try:
                resolved_user = await bot.get_chat(uid_int)
            except TelegramError:
                pass

        raw_uname = getattr(resolved_user, "username", None) if resolved_user else None
        uname = raw_uname.lstrip("@").strip() if isinstance(raw_uname, str) else ""
        if uname:
            uid_usernames[uid_str] = uname
            if saved_targets is not None and uid_str in saved_targets:
                if saved_targets[uid_str].get("username") != uname:
                    saved_targets[uid_str]["username"] = uname
                    targets_updated = True

    if targets_updated and config is not None and saved_targets is not None:
        save_known_groups(config.resolved_groups_file, saved_targets)

    return uid_usernames, warnings


async def send_self_preview_message(
    bot: Bot,
    self_chat_id: int,
    message: str,
    mentions: list[str] | None = None,
    photo: str | None = None,
    parse_mode: str | None = "HTML",
    target_chat_ids: list[int | str] | None = None,
    saved_targets: dict[str, dict[str, str]] | None = None,
    tag_presets: dict[str, list[str]] | None = None,
    config: BotConfig | None = None,
    document: str | None = None,
    require_ack: bool = False,
) -> tuple[bool, str | None, list[str]]:
    """Kirim preview hasil pesan custom langsung ke diri sendiri (chat pribadi admin)."""
    now = datetime.now(config.tzinfo) if config else datetime.now()
    expanded_message = render_dynamic_placeholders(message, now)
    uid_usernames: dict[str, str] = {}
    warnings: list[str] = []

    numeric_uids = extract_mention_user_ids(
        expanded_message,
        mentions=mentions,
        saved_targets=saved_targets,
        tag_presets=tag_presets,
    )
    if numeric_uids:
        group_ids = [cid for cid in (target_chat_ids or []) if isinstance(cid, int) and cid < 0]
        if group_ids:
            for gid in group_ids:
                resolved_map, w = await resolve_mention_usernames_for_chat(
                    bot,
                    gid,
                    numeric_uids,
                    saved_targets=saved_targets,
                    config=config,
                )
                uid_usernames.update(resolved_map)
                for item in w:
                    if item not in warnings:
                        warnings.append(item)
        else:
            resolved_map, _ = await resolve_mention_usernames_for_chat(
                bot,
                self_chat_id,
                numeric_uids,
                saved_targets=saved_targets,
                config=config,
            )
            uid_usernames.update(resolved_map)

    final_text = render_message_with_mentions(
        expanded_message,
        mentions,
        parse_mode,
        saved_targets=saved_targets,
        tag_presets=tag_presets,
        uid_usernames=uid_usernames,
    )
    reply_markup = (
        InlineKeyboardMarkup([[InlineKeyboardButton("✅ Sudah Dikerjakan (Preview)", callback_data="ack:noop")]])
        if require_ack
        else None
    )

    try:
        if photo:
            photo_path = Path(photo)
            if photo_path.exists() and photo_path.is_file():
                with photo_path.open("rb") as photo_file:
                    await bot.send_photo(
                        chat_id=self_chat_id,
                        photo=photo_file,
                        caption=final_text or None,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                    )
            else:
                await bot.send_photo(
                    chat_id=self_chat_id,
                    photo=photo,
                    caption=final_text or None,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
        elif document:
            doc_path = Path(document)
            if doc_path.exists() and doc_path.is_file():
                with doc_path.open("rb") as doc_file:
                    await bot.send_document(
                        chat_id=self_chat_id,
                        document=doc_file,
                        caption=final_text or None,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                    )
            else:
                await bot.send_document(
                    chat_id=self_chat_id,
                    document=document,
                    caption=final_text or None,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
        else:
            await bot.send_message(
                chat_id=self_chat_id,
                text=final_text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        return True, None, warnings
    except TelegramError as exc:
        logger.error("Gagal mengirim preview ke diri sendiri (%s): %s", self_chat_id, exc)
        return False, str(exc), warnings


async def _notify_admins_delivery_failure(
    bot: Bot,
    config: BotConfig,
    job: ScheduleJob,
    failures: list[tuple[int | str, str]],
) -> None:
    """Kirim notifikasi ke admin jika ada pengiriman jadwal yang gagal."""
    if not failures:
        return
    extra = load_extra_admins(config.resolved_admins_file)
    admin_ids = set(config.admin_ids) | extra
    if not admin_ids:
        return
    failed_chat_ids = {cid for cid, _ in failures}
    detail_lines = "\n".join(
        f"• <code>{html.escape(str(cid))}</code>: <i>{html.escape(err[:120])}</i>"
        for cid, err in failures[:5]
    )
    alert_text = (
        f"⚠️ <b>Gagal Kirim Jadwal</b> (<code>{html.escape(job.id)}</code>)\n"
        f"Nama: <b>{html.escape(job.name)}</b>\n\n"
        f"{detail_lines}"
    )
    for admin_id in sorted(admin_ids):
        if admin_id in failed_chat_ids:
            continue
        try:
            await bot.send_message(chat_id=admin_id, text=alert_text, parse_mode="HTML")
        except TelegramError:
            continue


async def send_scheduled_message(
    bot: Bot,
    job: ScheduleJob,
    saved_targets: dict[str, dict[str, str]] | None = None,
    tag_presets: dict[str, list[str]] | None = None,
    config: BotConfig | None = None,
    ignore_window: bool = False,
    diagnostics: list[str] | None = None,
    max_retries: int = 2,
    retry_delay: float = 1.0,
) -> tuple[int, int]:
    """Kirim pesan dari satu ScheduleJob ke semua chat_ids."""
    now = datetime.now(config.tzinfo) if config else datetime.now()
    if not ignore_window and not is_within_active_window(job, now):
        logger.info(
            "Melewati job '%s' karena di luar jam operasional (%s)",
            job.id,
            format_active_window(job.active_hours, job.active_days),
        )
        return 0, 0

    runtime_state: dict[str, Any] | None = None
    if config is not None and (job.auto_delete_prev or job.require_ack):
        runtime_state = load_runtime_state(config.resolved_state_file)
        if not ignore_window and job.require_ack:
            today_str = now.strftime("%Y-%m-%d")
            if runtime_state.get("ack_dates", {}).get(job.id) == today_str:
                logger.info("Melewati job '%s' karena sudah ditandai selesai hari ini (%s)", job.id, today_str)
                return 0, 0

    expanded_message = render_dynamic_placeholders(job.message, now)
    success = 0
    failed = 0
    failures: list[tuple[int | str, str]] = []
    state_changed = False
    numeric_uids = extract_mention_user_ids(
        expanded_message,
        mentions=job.mentions,
        saved_targets=saved_targets,
        tag_presets=tag_presets,
    )
    ack_markup = (
        InlineKeyboardMarkup([[InlineKeyboardButton("✅ Sudah Dikerjakan", callback_data=f"ack:done:{job.id}")]])
        if job.require_ack
        else None
    )

    for chat_id in job.chat_ids:
        err_msg: str | None = None
        uid_usernames: dict[str, str] = {}
        chat_warnings: list[str] = []
        if numeric_uids:
            uid_usernames, chat_warnings = await resolve_mention_usernames_for_chat(
                bot,
                chat_id,
                numeric_uids,
                saved_targets=saved_targets,
                config=config,
            )
            if chat_warnings:
                for w in chat_warnings:
                    logger.warning("Mention warning [%s]: %s", job.id, w)
                if diagnostics is not None:
                    diagnostics.extend(chat_warnings)

        final_text = render_message_with_mentions(
            expanded_message,
            job.mentions,
            job.parse_mode,
            saved_targets=saved_targets,
            tag_presets=tag_presets,
            uid_usernames=uid_usernames,
        )

        # Anti-spam: hapus pesan pengingat sebelumnya di chat ini jika diaktifkan
        if job.auto_delete_prev and runtime_state is not None:
            prev_msg_id = runtime_state.get("last_messages", {}).get(job.id, {}).get(str(chat_id))
            if isinstance(prev_msg_id, int):
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=prev_msg_id)
                except TelegramError:
                    pass

        extra_kwargs: dict[str, Any] = {}
        if job.thread_id is not None:
            extra_kwargs["message_thread_id"] = job.thread_id
        if ack_markup is not None:
            extra_kwargs["reply_markup"] = ack_markup

        sent_msg: Any = None
        attempts = max(1, max_retries)
        for attempt in range(attempts):
            try:
                if job.photo:
                    photo_path = Path(job.photo)
                    if photo_path.exists() and photo_path.is_file():
                        with photo_path.open("rb") as photo_file:
                            sent_msg = await bot.send_photo(
                                chat_id=chat_id,
                                photo=photo_file,
                                caption=final_text or None,
                                parse_mode=job.parse_mode,
                                **extra_kwargs,
                            )
                    else:
                        sent_msg = await bot.send_photo(
                            chat_id=chat_id,
                            photo=job.photo,
                            caption=final_text or None,
                            parse_mode=job.parse_mode,
                            **extra_kwargs,
                        )
                elif job.document:
                    doc_path = Path(job.document)
                    if doc_path.exists() and doc_path.is_file():
                        with doc_path.open("rb") as doc_file:
                            sent_msg = await bot.send_document(
                                chat_id=chat_id,
                                document=doc_file,
                                caption=final_text or None,
                                parse_mode=job.parse_mode,
                                **extra_kwargs,
                            )
                    else:
                        sent_msg = await bot.send_document(
                            chat_id=chat_id,
                            document=job.document,
                            caption=final_text or None,
                            parse_mode=job.parse_mode,
                            **extra_kwargs,
                        )
                else:
                    sent_msg = await bot.send_message(
                        chat_id=chat_id,
                        text=final_text,
                        parse_mode=job.parse_mode,
                        **extra_kwargs,
                    )
                err_msg = None
                break
            except RetryAfter as exc:
                err_msg = str(exc)
                if attempt + 1 < attempts:
                    wait_sec = min(float(getattr(exc, "retry_after", retry_delay)), 5.0)
                    await asyncio.sleep(max(0.0, wait_sec))
                else:
                    logger.error("Gagal mengirim job '%s' ke chat_id=%s (rate-limit): %s", job.id, chat_id, exc)
            except TelegramError as exc:
                err_msg = str(exc)
                if attempt + 1 < attempts:
                    await asyncio.sleep(max(0.0, retry_delay))
                else:
                    logger.error("Gagal mengirim job '%s' ke chat_id=%s: %s", job.id, chat_id, exc)

        if err_msg is None:
            success += 1
            logger.info("Job '%s' terkirim ke chat_id=%s", job.id, chat_id)

            if job.auto_delete_prev and runtime_state is not None and sent_msg is not None:
                sent_mid = getattr(sent_msg, "message_id", None)
                if isinstance(sent_mid, int):
                    runtime_state.setdefault("last_messages", {}).setdefault(job.id, {})[str(chat_id)] = sent_mid
                    state_changed = True

            # Cek apakah Telegram me-strip sebagian tg://user?id= (biasanya karena user tanpa @username & privasi/belum /start)
            remaining_id_links = len(
                [uid for uid in numeric_uids if uid not in uid_usernames]
            )
            if remaining_id_links > 0 and sent_msg is not None:
                raw_entities = (
                    getattr(sent_msg, "caption_entities", None)
                    if (job.photo or job.document)
                    else getattr(sent_msg, "entities", None)
                )
                if isinstance(raw_entities, (list, tuple)):
                    active_text_mentions = sum(1 for e in raw_entities if getattr(e, "type", "") == "text_mention")
                    if active_text_mentions < remaining_id_links:
                        stripped = remaining_id_links - active_text_mentions
                        warn_msg = (
                            f"{stripped} user tanpa @username tidak ter-mention oleh Telegram "
                            "(minta user tersebut pasang @username Telegram atau klik /start ke bot)."
                        )
                        logger.warning("Mention stripped [%s -> %s]: %s", job.id, chat_id, warn_msg)
                        if diagnostics is not None and warn_msg not in diagnostics:
                            diagnostics.append(warn_msg)
        else:
            failed += 1
            failures.append((chat_id, err_msg))

        if config is not None:
            append_delivery_history(
                config.resolved_history_file,
                {
                    "timestamp": now.strftime("%d/%m %H:%M:%S %Z"),
                    "job_id": job.id,
                    "job_name": job.name,
                    "chat_id": str(chat_id),
                    "status": "ok" if err_msg is None else "failed",
                    "error": err_msg,
                },
            )

        await asyncio.sleep(0.05)

    if state_changed and config is not None and runtime_state is not None:
        save_runtime_state(config.resolved_state_file, runtime_state)

    if failures and config is not None and not ignore_window:
        await _notify_admins_delivery_failure(bot, config, job, failures)

    return success, failed


def register_jobs(
    scheduler: AsyncIOScheduler,
    bot: Bot | None,
    jobs: list[ScheduleJob],
    config: BotConfig,
    saved_targets: dict[str, dict[str, str]] | None = None,
    tag_presets: dict[str, list[str]] | None = None,
) -> int:
    """Daftarkan ulang semua jadwal aktif ke scheduler."""
    scheduler.remove_all_jobs()
    active_count = 0
    now = datetime.now(config.tzinfo)

    for job in jobs:
        if not job.enabled:
            logger.info("Melewati jadwal nonaktif/dijeda: %s (%s)", job.id, job.name)
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
            args=[bot, job, saved_targets, tag_presets, config, False],
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


def purge_expired_date_jobs(
    jobs: list[ScheduleJob],
    tz: Any,
    now: datetime | None = None,
) -> tuple[list[ScheduleJob], list[ScheduleJob]]:
    """Pisahkan jadwal sekali kirim (date) yang waktunya sudah lewat."""
    current = now or datetime.now(tz)
    kept: list[ScheduleJob] = []
    removed: list[ScheduleJob] = []
    for job in jobs:
        if job.trigger == "date" and job.run_at:
            try:
                if parse_run_at(job.run_at, tz) <= current:
                    removed.append(job)
                    continue
            except ValueError:
                pass
        kept.append(job)
    return kept, removed


def duplicate_schedule_job(context: ContextTypes.DEFAULT_TYPE, source_job_id: str) -> ScheduleJob | None:
    """Duplikat jadwal yang sudah ada dalam 1 klik (status awal dijeda agar aman diedit dulu)."""
    config: BotConfig = context.bot_data["config"]
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    source = next((j for j in jobs if j.id == source_job_id), None)
    if not source:
        return None

    existing_ids = {j.id for j in jobs}
    base_id = f"{source.id}_copy"
    new_id = base_id
    counter = 2
    while new_id in existing_ids:
        new_id = f"{base_id}{counter}"
        counter += 1

    cloned = ScheduleJob(
        id=new_id,
        name=f"{source.name[:24]} (Salinan)",
        trigger=source.trigger,
        chat_ids=list(source.chat_ids),
        message=source.message,
        enabled=False,
        parse_mode=source.parse_mode,
        photo=source.photo,
        document=source.document,
        thread_id=source.thread_id,
        auto_delete_prev=source.auto_delete_prev,
        require_ack=source.require_ack,
        mentions=list(source.mentions),
        active_hours=source.active_hours,
        active_days=list(source.active_days) if source.active_days else None,
        division=source.division,
        run_at=source.run_at,
        cron=source.cron,
        seconds=source.seconds,
        minutes=source.minutes,
        hours=source.hours,
        days=source.days,
    )
    cloned.validate()
    jobs.append(cloned)
    save_schedules(config.schedules_file, jobs)

    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    register_jobs(
        scheduler,
        context.bot,
        jobs,
        config,
        context.bot_data.get("known_groups"),
        context.bot_data.get("tag_presets"),
    )
    context.bot_data["jobs"] = jobs
    return cloned


def format_jobs_list_and_keyboard(
    scheduler: AsyncIOScheduler,
    jobs: list[ScheduleJob] | None = None,
    division: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Susun teks daftar jadwal (aktif & dijeda) beserta tombol Edit, Tes, & Hapus, opsional difilter per divisi."""
    scheduled_map = {ap_job.id: ap_job for ap_job in scheduler.get_jobs()}
    all_jobs = jobs if jobs is not None else []
    div_key = division.strip().lower() if division else None
    div_label = DIVISION_LABELS.get(div_key, div_key.title()) if div_key else None
    if div_key:
        display_jobs = [j for j in all_jobs if (j.division or "").strip().lower() == div_key]
    else:
        display_jobs = all_jobs

    back_cb = f"menu:{div_key}" if div_key in DIVISION_LABELS else "menu:home"
    back_text = f"⬅️ Kembali ke {div_label}" if div_label else "⬅️ Kembali ke Menu Utama"

    if not display_jobs and (div_key or not scheduled_map):
        empty_msg = (
            f"Belum ada jadwal tersimpan untuk divisi <b>{div_label}</b>.\n"
            "Klik <b>✨ Custom Pesan &amp; Atur Waktu</b> untuk menambahkan."
            if div_label
            else "Belum ada jadwal tersimpan.\nKlik <b>✨ Custom Pesan &amp; Atur Waktu</b> untuk menambahkan."
        )
        return (
            empty_msg,
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✨ Custom Pesan & Atur Waktu", callback_data="menu:custom")],
                    [InlineKeyboardButton(back_text, callback_data=back_cb)],
                ]
            ),
        )

    header = f"<b>📋 Daftar Jadwal — {div_label}:</b>\n" if div_label else "<b>📋 Semua Daftar Jadwal:</b>\n"
    lines = [header]
    buttons: list[list[InlineKeyboardButton]] = []
    expired_count = 0

    if display_jobs:
        for job in display_jobs:
            ap_job = scheduled_map.get(job.id)
            if not job.enabled:
                status_icon = "⏸️"
                next_run = "Dijeda (Pause)"
            elif ap_job and ap_job.next_run_time:
                status_icon = "🟢"
                next_run = ap_job.next_run_time.strftime("%Y-%m-%d %H:%M:%S %Z")
            else:
                status_icon = "⚪"
                next_run = "Sudah lewat"
                if job.trigger == "date":
                    expired_count += 1

            win_str = ""
            if job.active_hours or job.active_days:
                win_str = f"\n  🕒 Jam aktif: <code>{format_active_window(job.active_hours, job.active_days)}</code>"
            div_badge = f" [{DIVISION_LABELS[job.division]}]" if (not div_key and job.division in DIVISION_LABELS) else ""

            lines.append(
                f"{status_icon} <b>{html.escape(job.name)}</b>{div_badge} (<code>{job.id}</code>)\n"
                f"  ⏰ Berikutnya: <code>{next_run}</code>{win_str}"
            )
            buttons.append(
                [
                    InlineKeyboardButton(f"⚙️ Kelola {job.id}", callback_data=f"editpick:{job.id}"),
                    InlineKeyboardButton("👤 Tes Diri", callback_data=f"job:testself:{job.id}"),
                    InlineKeyboardButton("🚀 Tes", callback_data=f"job:test:{job.id}"),
                    InlineKeyboardButton("🗑️ Hapus", callback_data=f"job:del:{job.id}"),
                ]
            )
    else:
        for ap_job in scheduled_map.values():
            next_run = (
                ap_job.next_run_time.strftime("%Y-%m-%d %H:%M:%S %Z")
                if ap_job.next_run_time
                else "-"
            )
            lines.append(
                f"🟢 <b>{html.escape(ap_job.name)}</b> (<code>{ap_job.id}</code>)\n"
                f"  ⏰ Berikutnya: <code>{next_run}</code>"
            )
            buttons.append(
                [
                    InlineKeyboardButton(f"⚙️ Kelola {ap_job.id}", callback_data=f"editpick:{ap_job.id}"),
                    InlineKeyboardButton("👤 Tes Diri", callback_data=f"job:testself:{ap_job.id}"),
                    InlineKeyboardButton("🚀 Tes", callback_data=f"job:test:{ap_job.id}"),
                    InlineKeyboardButton("🗑️ Hapus", callback_data=f"job:del:{ap_job.id}"),
                ]
            )

    if expired_count > 0:
        buttons.append(
            [InlineKeyboardButton(f"🧹 Bersihkan Jadwal Kadaluarsa ({expired_count})", callback_data="job:cleanup")]
        )
    buttons.append([InlineKeyboardButton(back_text, callback_data=back_cb)])
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
    if "active_days" in spec:
        job.active_days = spec.get("active_days")
    job.enabled = True
    job.validate()


def save_new_custom_job(
    context: ContextTypes.DEFAULT_TYPE,
    chat_ids: list[int | str],
    spec: dict[str, Any],
    custom_msg: str,
    mentions: list[str] | None = None,
    photo: str | None = None,
    document: str | None = None,
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

    active_division = (
        str(context.user_data.get("active_division")).strip().lower()
        if (context.user_data and context.user_data.get("active_division"))
        else None
    )
    if active_division not in DIVISION_LABELS:
        active_division = None

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
        photo=photo,
        document=document,
        mentions=list(mentions or []),
        active_days=spec.get("active_days"),
        division=active_division,
        enabled=True,
        parse_mode="HTML",
    )
    new_job.validate()

    jobs.append(new_job)
    save_schedules(config.schedules_file, jobs)

    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    register_jobs(
        scheduler,
        context.bot,
        jobs,
        config,
        context.bot_data.get("known_groups"),
        context.bot_data.get("tag_presets"),
    )
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
    thread_id = getattr(update.message, "message_thread_id", None)
    thread_line = f"\n🧵 Topic ID: <code>{thread_id}</code>" if thread_id is not None else ""
    await update.message.reply_html(
        f"🆔 Chat ID: <code>{update.effective_chat.id}</code>\n"
        f"📌 Tipe: <code>{update.effective_chat.type}</code>\n"
        f"👤 User ID: <code>{user_id}</code>{thread_line}",
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
    register_jobs(
        scheduler,
        context.bot,
        jobs,
        config,
        context.bot_data.get("known_groups"),
        context.bot_data.get("tag_presets"),
    )
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
    register_jobs(
        scheduler,
        context.bot,
        remaining,
        config,
        context.bot_data.get("known_groups"),
        context.bot_data.get("tag_presets"),
    )
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
    text, markup = format_jobs_list_and_keyboard(scheduler, context.bot_data.get("jobs", []))
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
    diag: list[str] = []
    success, failed = await send_scheduled_message(
        context.bot,
        matched,
        context.bot_data.get("known_groups"),
        context.bot_data.get("tag_presets"),
        config,
        ignore_window=True,
        diagnostics=diag,
    )
    diag_text = ""
    if diag:
        diag_lines = "\n".join(f"• {html.escape(d)}" for d in diag[:5])
        diag_text = f"\n\n⚠️ <b>Catatan Mention:</b>\n{diag_lines}"
    await update.message.reply_html(
        f"Selesai tes <code>{matched.id}</code>: {success} terkirim, {failed} gagal.{diag_text}",
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
        count = register_jobs(
            scheduler,
            context.bot,
            jobs,
            config,
            context.bot_data.get("known_groups"),
            context.bot_data.get("tag_presets"),
        )
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
    context.user_data.pop("time_input_mode", None)
    context.user_data.pop("time_selected_days", None)
    config: BotConfig = context.bot_data["config"]
    text, keyboard = build_flexible_time_prompt(config, "⏰ Atur Waktu Kirim Bebas (2/4)")
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    return ASK_DATETIME


async def wiz_target_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ASK_TARGET

    try:
        raw_chat_ids = parse_chat_ids(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return ASK_TARGET

    resolved_ids: list[int | str] = []
    for cid in raw_chat_ids:
        default_type = "channel" if isinstance(cid, str) else ("group" if cid < 0 else "user")
        final_id, title, rtype, fetched_ok = await resolve_chat_target(context.bot, cid, default_type)
        resolved_ids.append(final_id)
        if fetched_ok or (isinstance(final_id, int) and final_id < 0):
            remember_group(context.bot_data, final_id, title, rtype)

    context.user_data["wiz"] = {"chat_ids": resolved_ids}
    context.user_data.pop("time_input_mode", None)
    context.user_data.pop("time_selected_days", None)
    config: BotConfig = context.bot_data["config"]
    text, keyboard = build_flexible_time_prompt(config, "⏰ Atur Waktu Kirim Bebas (2/4)")
    await update.message.reply_html(text, reply_markup=keyboard)
    return ASK_DATETIME


async def _handle_time_mode_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    base_title: str,
    stay_state: int,
) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    mode = query.data.split(":", 2)[2]
    config: BotConfig = context.bot_data["config"]
    if mode == "minute":
        context.user_data["time_input_mode"] = "minute"
        text, keyboard = build_minute_schedule_prompt(base_title)
    elif mode == "hour":
        context.user_data["time_input_mode"] = "hour"
        text, keyboard = build_hour_schedule_prompt(base_title)
    elif mode == "days":
        context.user_data["time_input_mode"] = "days"
        selected_days = list(context.user_data.get("time_selected_days", []))
        text, keyboard = build_day_picker_prompt(base_title, selected_days)
    else:
        context.user_data.pop("time_input_mode", None)
        text, keyboard = build_flexible_time_prompt(config, base_title)

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    return stay_state


async def _handle_time_day_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    base_title: str,
    stay_state: int,
) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END

    action = query.data.split(":", 2)[2]
    selected: list[int] = sorted(set(context.user_data.get("time_selected_days", [])))

    if action == "next":
        if not selected:
            await query.answer("Pilih minimal 1 hari terlebih dahulu!", show_alert=True)
            return stay_state
        await query.answer()
        context.user_data["time_input_mode"] = "days_time"
        text, keyboard = build_day_time_prompt(base_title, selected)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        return stay_state

    await query.answer()
    if action.startswith("t:"):
        day_num = int(action.split(":", 1)[1])
        if day_num in selected:
            selected.remove(day_num)
        else:
            selected.append(day_num)
            selected.sort()
    elif action == "p:work":
        selected = [1, 2, 3, 4, 5]
    elif action == "p:weekend":
        selected = [6, 7]
    elif action == "p:all":
        selected = [1, 2, 3, 4, 5, 6, 7]

    context.user_data["time_selected_days"] = selected
    text, keyboard = build_day_picker_prompt(base_title, selected)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    return stay_state


def _build_day_apply_expression(raw_apply: str, selected_days: list[int]) -> str:
    days_csv = ",".join(DAY_LABELS[d].lower() for d in sorted(set(selected_days)) if d in DAY_LABELS)
    if not days_csv:
        return raw_apply
    if re.fullmatch(r"\d{1,2}:\d{2}", raw_apply.strip()):
        return f"{days_csv} {raw_apply.strip()}"
    return f"{raw_apply.strip()} {days_csv}"


async def wiz_time_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _handle_time_mode_menu(update, context, "⏰ Atur Waktu Kirim Bebas (2/4)", ASK_DATETIME)


async def wiz_time_day_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _handle_time_day_menu(update, context, "⏰ Atur Waktu Kirim Bebas (2/4)", ASK_DATETIME)


async def wiz_time_day_apply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    raw_apply = query.data.split(":", 2)[2]
    selected_days: list[int] = list(context.user_data.get("time_selected_days", []))
    expr = _build_day_apply_expression(raw_apply, selected_days)
    config: BotConfig = context.bot_data["config"]
    spec = parse_flexible_schedule(expr, config.tzinfo)
    context.user_data.pop("time_input_mode", None)
    context.user_data.pop("time_selected_days", None)
    context.user_data.setdefault("wiz", {})["spec"] = spec

    await query.edit_message_text(
        "<b>✉️ Input Pesan Custom (3/4)</b>\n\n"
        f"⏰ Waktu dipilih: <code>{html.escape(spec['label'])}</code>\n\n"
        "Silakan ketik isi pesan custom (atau kirim foto/dokumen) yang ingin dikirim otomatis.\n"
        "<i>Tips: dukung <code>@username</code>, <code>@[Nama](UserID)</code>, serta variabel dinamis "
        "<code>{hari}</code>, <code>{tanggal}</code>, <code>{jam}</code>!</i>",
        parse_mode="HTML",
        reply_markup=build_cancel_keyboard(),
    )
    return ASK_MESSAGE


async def wiz_datetime_quick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    expr = query.data.split(":", 2)[2]
    config: BotConfig = context.bot_data["config"]
    spec = parse_flexible_schedule(expr, config.tzinfo)
    context.user_data.pop("time_input_mode", None)
    context.user_data.pop("time_selected_days", None)
    context.user_data.setdefault("wiz", {})["spec"] = spec

    await query.edit_message_text(
        "<b>✉️ Input Pesan Custom (3/4)</b>\n\n"
        f"⏰ Waktu dipilih: <code>{html.escape(spec['label'])}</code>\n\n"
        "Silakan ketik isi pesan custom (atau kirim foto/dokumen) yang ingin dikirim otomatis.\n"
        "<i>Tips: dukung <code>@username</code>, <code>@[Nama](UserID)</code>, serta variabel dinamis "
        "<code>{hari}</code>, <code>{tanggal}</code>, <code>{jam}</code>!</i>",
        parse_mode="HTML",
        reply_markup=build_cancel_keyboard(),
    )
    return ASK_MESSAGE


async def wiz_datetime_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ASK_DATETIME

    config: BotConfig = context.bot_data["config"]
    resolved_input = _resolve_time_text_with_mode(update.message.text, context.user_data)
    try:
        spec = parse_flexible_schedule(resolved_input, config.tzinfo)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return ASK_DATETIME

    context.user_data.pop("time_input_mode", None)
    context.user_data.pop("time_selected_days", None)
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
    if not update.message:
        return ASK_MESSAGE

    photo_id: str | None = None
    document_id: str | None = None
    if update.message.photo:
        photo_id = update.message.photo[-1].file_id
        custom_msg = (update.message.caption or "").strip()
    elif update.message.document:
        document_id = update.message.document.file_id
        custom_msg = (update.message.caption or "").strip()
    elif update.message.text:
        custom_msg = update.message.text.strip()
    else:
        return ASK_MESSAGE

    if not custom_msg and not photo_id and not document_id:
        await update.message.reply_text("Pesan, foto, atau dokumen tidak boleh kosong:", reply_markup=build_cancel_keyboard())
        return ASK_MESSAGE

    wiz = context.user_data.setdefault("wiz", {})
    wiz["message"] = custom_msg
    wiz["photo"] = photo_id
    wiz["document"] = document_id
    wiz["mentions"] = []

    candidates = await collect_mention_candidates(context.bot, context.bot_data, wiz.get("chat_ids", []))
    presets: dict[str, list[str]] = context.bot_data.get("tag_presets", {})
    preset_names = sorted(presets.keys(), key=str.lower)
    wiz["mention_candidates"] = candidates
    wiz["preset_names"] = preset_names

    keyboard = build_mentions_picker_keyboard(candidates, [], prefix="wiztag", preset_names=preset_names)
    hint = (
        f"Tersedia <b>{len(candidates)} user tersimpan</b>"
        + (f" &amp; <b>{len(preset_names)} preset tim</b>" if preset_names else "")
        + " yang siap dipilih."
        if (candidates or preset_names)
        else "<i>Belum ada user tersimpan — tambahkan lewat 📇 Kelola Target (👤 User) atau ketik manual di bawah.</i>"
    )
    await update.message.reply_html(
        "<b>🏷️ Tag / Mention Username (4/4 — Opsional)</b>\n\n"
        f"{hint}\n\n"
        "Klik centang nama / preset tim di bawah lalu klik <b>✅ Selesai</b>, "
        "atau ketik manual (contoh: <code>@budi @ani</code> / <code>Budi:123456789</code>):",
        reply_markup=keyboard,
    )
    return ASK_MENTIONS


async def wiz_mentions_preset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    pidx = int(query.data.split(":", 2)[2])
    wiz = context.user_data.setdefault("wiz", {})
    preset_names: list[str] = wiz.get("preset_names", [])
    if not (0 <= pidx < len(preset_names)):
        return ASK_MENTIONS

    pname = preset_names[pidx]
    presets: dict[str, list[str]] = context.bot_data.get("tag_presets", {})
    team_tokens = presets.get(pname, [])

    selected: list[str] = list(wiz.get("mentions", []))
    sel_lower = {t.lower() for t in selected}
    if all(t.lower() in sel_lower for t in team_tokens):
        # Toggle off jika semua anggota tim sudah tercentang
        team_lower = {t.lower() for t in team_tokens}
        selected = [t for t in selected if t.lower() not in team_lower]
    else:
        for t in team_tokens:
            if t.lower() not in sel_lower:
                sel_lower.add(t.lower())
                selected.append(t)
    wiz["mentions"] = selected

    candidates: list[dict[str, str]] = wiz.get("mention_candidates", [])
    keyboard = build_mentions_picker_keyboard(candidates, selected, prefix="wiztag", preset_names=preset_names)
    await query.edit_message_reply_markup(reply_markup=keyboard)
    return ASK_MENTIONS


async def wiz_mentions_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    idx = int(query.data.split(":", 2)[2])
    wiz = context.user_data.setdefault("wiz", {})
    candidates: list[dict[str, str]] = wiz.get("mention_candidates", [])
    if not (0 <= idx < len(candidates)):
        return ASK_MENTIONS

    token = candidates[idx]["token"]
    selected: list[str] = list(wiz.get("mentions", []))
    existing = next((t for t in selected if t.lower() == token.lower()), None)
    if existing:
        selected.remove(existing)
    else:
        selected.append(token)
    wiz["mentions"] = selected

    keyboard = build_mentions_picker_keyboard(
        candidates,
        selected,
        prefix="wiztag",
        preset_names=wiz.get("preset_names", []),
    )
    await query.edit_message_reply_markup(reply_markup=keyboard)
    return ASK_MENTIONS


async def wiz_mentions_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    return await _show_wiz_confirmation(update, context)


async def wiz_mentions_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ASK_MENTIONS

    try:
        typed = parse_mentions(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return ASK_MENTIONS

    remember_manual_mentions(context.bot_data, typed)
    wiz = context.user_data.setdefault("wiz", {})
    merged: list[str] = list(wiz.get("mentions", []))
    seen = {t.lower() for t in merged}
    for token in typed:
        if token.lower() not in seen:
            seen.add(token.lower())
            merged.append(token)
    wiz["mentions"] = merged
    return await _show_wiz_confirmation(update, context)


async def _show_wiz_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    wiz = context.user_data.setdefault("wiz", {})
    spec: dict[str, Any] = wiz["spec"]
    chat_ids = wiz["chat_ids"]
    custom_msg: str = wiz["message"]
    photo_id: str | None = wiz.get("photo")
    document_id: str | None = wiz.get("document")
    mentions: list[str] = wiz.get("mentions", [])
    tags_preview = " ".join(mentions) if mentions else "(tidak ada)"
    photo_line = "🖼️ Foto: <code>Terlampir</code>\n" if photo_id else ("📎 Dokumen: <code>Terlampir</code>\n" if document_id else "")

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👤 Tes ke Diri Sendiri", callback_data="wiz:testself")],
            [
                InlineKeyboardButton("✅ Simpan Jadwal", callback_data="wiz:save"),
                InlineKeyboardButton("❌ Batal", callback_data="wiz:cancel"),
            ],
        ]
    )
    text = (
        "<b>🔍 Konfirmasi Pesan Terjadwal</b>\n\n"
        f"🎯 Target: <code>{chat_ids}</code>\n"
        f"⏰ Waktu: <code>{html.escape(spec['label'])}</code>\n"
        f"{photo_line}"
        f"🏷️ Tag: <code>{html.escape(tags_preview)}</code>\n"
        f"✉️ Pesan:\n{html.escape(custom_msg or '(tanpa teks)')}\n\n"
        "Klik <b>👤 Tes ke Diri Sendiri</b> untuk melihat hasil jadinya dulu, atau <b>✅ Simpan Jadwal</b> untuk mengaktifkan."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    elif update.message:
        await update.message.reply_html(text, reply_markup=keyboard)
    return CONFIRM_SAVE


async def wiz_confirm_test_self(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Kirim preview pesan custom yang sedang dibuat ke diri sendiri tanpa keluar dari konfirmasi."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END

    wiz = context.user_data.get("wiz")
    if not wiz or "message" not in wiz:
        await query.answer("Sesi pembuatan jadwal kedaluwarsa.", show_alert=True)
        return ConversationHandler.END

    user = update.effective_user
    chat = update.effective_chat
    self_chat_id = user.id if user else (chat.id if chat else 0)
    if not self_chat_id:
        await query.answer("Chat ID pribadi tidak ditemukan.", show_alert=True)
        return CONFIRM_SAVE

    config: BotConfig = context.bot_data["config"]
    ok, err_msg, warnings = await send_self_preview_message(
        context.bot,
        self_chat_id=self_chat_id,
        message=wiz["message"],
        mentions=wiz.get("mentions", []),
        photo=wiz.get("photo"),
        document=wiz.get("document"),
        parse_mode="HTML",
        target_chat_ids=wiz.get("chat_ids", []),
        saved_targets=context.bot_data.get("known_groups"),
        tag_presets=context.bot_data.get("tag_presets"),
        config=config,
    )
    if ok:
        await query.answer("✅ Preview pesan terkirim ke chat kamu!", show_alert=False)
        if warnings:
            warn_lines = "\n".join(f"• {html.escape(w)}" for w in warnings[:5])
            await context.bot.send_message(
                chat_id=self_chat_id,
                text=f"⚠️ <b>Catatan Preview Mention:</b>\n{warn_lines}",
                parse_mode="HTML",
            )
    else:
        await query.answer(f"Gagal kirim preview: {(err_msg or '')[:150]}", show_alert=True)
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
    photo_id: str | None = wiz.get("photo")
    document_id: str | None = wiz.get("document")
    mentions: list[str] = wiz.get("mentions", [])

    new_job = save_new_custom_job(
        context,
        chat_ids,
        spec,
        custom_msg,
        mentions=mentions,
        photo=photo_id,
        document=document_id,
    )
    tags_label = " ".join(mentions) if mentions else "-"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👤 Tes ke Diri Sendiri", callback_data=f"job:testself:{new_job.id}"),
                InlineKeyboardButton("🚀 Tes ke Target", callback_data=f"job:test:{new_job.id}"),
            ],
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
    status_display = "🟢 Aktif" if matched.enabled else "⏸️ Dijeda (Pause)"
    toggle_label = "⏸️ Pause Jadwal" if matched.enabled else "▶️ Aktifkan Jadwal"
    hours_display = format_active_window(matched.active_hours, matched.active_days)
    media_display = "🖼️ Foto" if matched.photo else ("📎 Dokumen" if matched.document else "-")
    thread_display = f"<code>{matched.thread_id}</code>" if matched.thread_id is not None else "General"
    autodel_label = "🧹 Anti-Spam: ON" if matched.auto_delete_prev else "🧹 Anti-Spam: OFF"
    ack_label = "✅ Tombol Selesai: ON" if matched.require_ack else "✅ Tombol Selesai: OFF"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Ubah Pesan/Media", callback_data="editact:msg"),
                InlineKeyboardButton("⏰ Ubah Waktu", callback_data="editact:time"),
            ],
            [
                InlineKeyboardButton("🎯 Ubah Chat ID", callback_data="editact:target"),
                InlineKeyboardButton("🏷️ Ubah Tag", callback_data="editact:mentions"),
            ],
            [
                InlineKeyboardButton("🕒 Jam Operasional", callback_data="editact:hours"),
                InlineKeyboardButton("🧵 Topic ID", callback_data="editact:thread"),
            ],
            [
                InlineKeyboardButton(autodel_label, callback_data="editact:autodel"),
                InlineKeyboardButton(ack_label, callback_data="editact:ack"),
            ],
            [
                InlineKeyboardButton(toggle_label, callback_data="editact:toggle"),
                InlineKeyboardButton("📋 Duplikat Jadwal", callback_data="editact:dup"),
            ],
            [
                InlineKeyboardButton("📦 Ke Provisioning", callback_data="editdiv:provisioning"),
                InlineKeyboardButton("🛠️ Ke Assurance", callback_data="editdiv:assurance"),
                InlineKeyboardButton("🔧 Ke Maintenance", callback_data="editdiv:maintenance"),
            ],
            [
                InlineKeyboardButton("👤 Tes ke Diri Sendiri", callback_data=f"job:testself:{matched.id}"),
                InlineKeyboardButton("🚀 Tes ke Target", callback_data=f"job:test:{matched.id}"),
            ],
            [
                InlineKeyboardButton("❌ Tutup", callback_data="wiz:cancel"),
            ],
        ]
    )
    await query.edit_message_text(
        f"<b>⚙️ Kelola Jadwal: {html.escape(matched.name)}</b> (<code>{matched.id}</code>)\n\n"
        f"📌 <b>Status:</b> {status_display}\n"
        f"⏰ <b>Waktu:</b> <code>{html.escape(describe_job_time(matched))}</code>\n"
        f"🕒 <b>Jam Operasional:</b> <code>{html.escape(hours_display)}</code>\n"
        f"🎯 <b>Target:</b> <code>{matched.chat_ids}</code> | 🧵 <b>Topic:</b> {thread_display}\n"
        f"📎 <b>Lampiran:</b> {media_display}\n"
        f"🏷️ <b>Tag:</b> <code>{html.escape(tags_display)}</code>\n"
        f"✉️ <b>Pesan:</b>\n{html.escape(matched.message or '(kosong)')}\n\n"
        "Apa yang ingin kamu ubah?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return EDIT_ACTION


async def edit_toggle_enabled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pause atau aktifkan kembali jadwal dalam 1 klik."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    target_id = context.user_data.get("edit_job_id", "")
    config: BotConfig = context.bot_data["config"]
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)
    if not matched:
        await query.edit_message_text("Jadwal tidak ditemukan.", reply_markup=build_back_menu_keyboard())
        return ConversationHandler.END

    matched.enabled = not matched.enabled
    save_schedules(config.schedules_file, jobs)
    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    register_jobs(
        scheduler,
        context.bot,
        jobs,
        config,
        context.bot_data.get("known_groups"),
        context.bot_data.get("tag_presets"),
    )
    context.bot_data["jobs"] = jobs

    # Refresh tampilan detail jadwal
    query.data = f"editpick:{matched.id}"
    return await edit_show_actions(update, context)


async def edit_toggle_autodel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Aktifkan/matikan anti-spam (auto-hapus pesan sebelumnya sebelum kirim baru)."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    target_id = context.user_data.get("edit_job_id", "")
    config: BotConfig = context.bot_data["config"]
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)
    if not matched:
        await query.edit_message_text("Jadwal tidak ditemukan.", reply_markup=build_back_menu_keyboard())
        return ConversationHandler.END

    matched.auto_delete_prev = not matched.auto_delete_prev
    save_schedules(config.schedules_file, jobs)
    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    register_jobs(scheduler, context.bot, jobs, config, context.bot_data.get("known_groups"), context.bot_data.get("tag_presets"))
    context.bot_data["jobs"] = jobs

    query.data = f"editpick:{matched.id}"
    return await edit_show_actions(update, context)


async def edit_toggle_ack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Aktifkan/matikan tombol konfirmasi '✅ Sudah Dikerjakan' di pesan grup."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    target_id = context.user_data.get("edit_job_id", "")
    config: BotConfig = context.bot_data["config"]
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)
    if not matched:
        await query.edit_message_text("Jadwal tidak ditemukan.", reply_markup=build_back_menu_keyboard())
        return ConversationHandler.END

    matched.require_ack = not matched.require_ack
    # Reset status selesai hari ini saat toggle ulang agar bisa diuji/dipakai lagi
    state = load_runtime_state(config.resolved_state_file)
    if state.get("ack_dates", {}).pop(matched.id, None) is not None:
        save_runtime_state(config.resolved_state_file, state)

    save_schedules(config.schedules_file, jobs)
    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    register_jobs(scheduler, context.bot, jobs, config, context.bot_data.get("known_groups"), context.bot_data.get("tag_presets"))
    context.bot_data["jobs"] = jobs

    query.data = f"editpick:{matched.id}"
    return await edit_show_actions(update, context)


async def edit_duplicate_job(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Duplikat jadwal terpilih lalu langsung buka menu kelola salinannya."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END

    target_id = context.user_data.get("edit_job_id", "")
    cloned = duplicate_schedule_job(context, target_id)
    if not cloned:
        await query.answer("Jadwal tidak ditemukan.", show_alert=True)
        return ConversationHandler.END

    await query.answer(f"📋 Salinan '{cloned.id}' dibuat (status Pause — silakan ubah waktu lalu aktifkan)!", show_alert=True)
    query.data = f"editpick:{cloned.id}"
    return await edit_show_actions(update, context)


async def edit_ask_thread(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Minta Topic / Thread ID untuk grup Forum."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    target_id = context.user_data.get("edit_job_id", "")
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)
    cur = str(matched.thread_id) if (matched and matched.thread_id is not None) else "General (tanpa topic)"
    await query.edit_message_text(
        f"<b>🧵 Atur Topic / Thread ID</b> (<code>{html.escape(target_id)}</code>)\n\n"
        f"Saat ini: <code>{html.escape(cur)}</code>\n\n"
        "Ketik angka <b>Topic ID</b> dari grup Forum (kirim <code>/chatid</code> di dalam topik tujuan untuk melihat ID-nya), "
        "atau ketik <code>off</code> untuk mengirim ke General:",
        parse_mode="HTML",
        reply_markup=build_cancel_keyboard(),
    )
    return EDIT_NEW_THREAD


async def edit_save_thread(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return EDIT_NEW_THREAD
    raw = update.message.text.strip().lower()
    if raw in ("off", "hapus", "none", "general", "0", "-"):
        new_thread: int | None = None
    else:
        try:
            new_thread = int(raw)
            if new_thread <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Topic ID harus berupa angka positif (contoh: 42) atau ketik 'off' untuk General:",
                reply_markup=build_cancel_keyboard(),
            )
            return EDIT_NEW_THREAD

    target_id = context.user_data.pop("edit_job_id", None)
    config: BotConfig = context.bot_data["config"]
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)
    if not matched:
        await update.message.reply_text("Sesi edit kedaluwarsa. Buka /menu lagi.", reply_markup=build_main_menu_keyboard())
        return ConversationHandler.END

    matched.thread_id = new_thread
    save_schedules(config.schedules_file, jobs)
    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    register_jobs(scheduler, context.bot, jobs, config, context.bot_data.get("known_groups"), context.bot_data.get("tag_presets"))
    context.bot_data["jobs"] = jobs

    label = str(new_thread) if new_thread is not None else "General (tanpa topic)"
    await update.message.reply_html(
        f"<b>✅ Topic ID untuk <code>{matched.id}</code> diperbarui!</b>\n\n"
        f"🧵 <b>Topic:</b> <code>{html.escape(label)}</code>",
        reply_markup=build_back_menu_keyboard(),
    )
    return ConversationHandler.END


async def edit_ask_hours(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    target_id = context.user_data.get("edit_job_id", "")
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)
    current = format_active_window(matched.active_hours, matched.active_days) if matched else "24 Jam"
    text, keyboard = build_active_hours_prompt(target_id, current)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    return EDIT_NEW_HOURS


async def _apply_edited_hours(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    active_hours: str | None,
    active_days: list[int] | None,
    raw_expr: str | None = None,
) -> int:
    target_id = context.user_data.pop("edit_job_id", None)
    config: BotConfig = context.bot_data["config"]
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)
    if not matched:
        if update.callback_query:
            await update.callback_query.edit_message_text("Sesi edit kedaluwarsa.", reply_markup=build_main_menu_keyboard())
        elif update.message:
            await update.message.reply_text("Sesi edit kedaluwarsa.", reply_markup=build_main_menu_keyboard())
        return ConversationHandler.END

    # Pertahankan field yang tidak disebut saat user hanya mengubah jam saja atau hari saja
    if active_hours is None and active_days is not None:
        matched.active_days = active_days
    elif active_hours is not None and active_days is None and raw_expr and len(raw_expr.strip().split()) == 1:
        matched.active_hours = active_hours
    else:
        matched.active_hours = active_hours
        matched.active_days = active_days
    save_schedules(config.schedules_file, jobs)
    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    register_jobs(
        scheduler,
        context.bot,
        jobs,
        config,
        context.bot_data.get("known_groups"),
        context.bot_data.get("tag_presets"),
    )
    context.bot_data["jobs"] = jobs

    label = format_active_window(matched.active_hours, matched.active_days)
    msg = (
        f"<b>✅ Jam operasional untuk <code>{matched.id}</code> diperbarui!</b>\n\n"
        f"🕒 <b>Jam Aktif:</b> <code>{html.escape(label)}</code>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=build_back_menu_keyboard())
    elif update.message:
        await update.message.reply_html(msg, reply_markup=build_back_menu_keyboard())
    return ConversationHandler.END


async def edit_save_hours_quick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()
    expr = query.data.split(":", 2)[2]
    ah, ad = parse_active_window(expr)
    return await _apply_edited_hours(update, context, ah, ad, raw_expr=expr)


async def edit_save_hours_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return EDIT_NEW_HOURS
    try:
        ah, ad = parse_active_window(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return EDIT_NEW_HOURS
    return await _apply_edited_hours(update, context, ah, ad, raw_expr=update.message.text)


async def edit_ask_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    target_id = context.user_data.get("edit_job_id", "")
    await query.edit_message_text(
        f"<b>✏️ Input Pesan Custom Baru</b> (<code>{html.escape(target_id)}</code>)\n\n"
        "Silakan <b>ketik pesan baru, kirim foto, atau kirim dokumen</b> (mendukung HTML, "
        "variabel dinamis <code>{hari}</code> <code>{tanggal}</code> <code>{jam}</code>, serta <code>@username</code>):",
        parse_mode="HTML",
        reply_markup=build_cancel_keyboard(),
    )
    return EDIT_NEW_MESSAGE


async def edit_ask_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    context.user_data.pop("time_input_mode", None)
    context.user_data.pop("time_selected_days", None)
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
    register_jobs(
        scheduler,
        context.bot,
        jobs,
        config,
        context.bot_data.get("known_groups"),
        context.bot_data.get("tag_presets"),
    )
    context.bot_data["jobs"] = jobs

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📦 Ke Provisioning", callback_data="editdiv:provisioning"),
                InlineKeyboardButton("🛠️ Ke Assurance", callback_data="editdiv:assurance"),
                InlineKeyboardButton("🔧 Ke Maintenance", callback_data="editdiv:maintenance"),
            ],
            [
                InlineKeyboardButton("👤 Tes ke Diri Sendiri", callback_data=f"job:testself:{matched.id}"),
                InlineKeyboardButton("🚀 Tes ke Target", callback_data=f"job:test:{matched.id}"),
            ],
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
        raw_chat_ids = parse_chat_ids(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return EDIT_NEW_TARGET

    resolved_ids: list[int | str] = []
    for cid in raw_chat_ids:
        default_type = "channel" if isinstance(cid, str) else ("group" if cid < 0 else "user")
        final_id, title, rtype, fetched_ok = await resolve_chat_target(context.bot, cid, default_type)
        resolved_ids.append(final_id)
        if fetched_ok or (isinstance(final_id, int) and final_id < 0):
            remember_group(context.bot_data, final_id, title, rtype)

    return await _apply_edited_target(update, context, resolved_ids)


async def edit_ask_mentions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    target_id = context.user_data.get("edit_job_id", "")
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)
    current_mentions: list[str] = list(matched.mentions) if matched else []
    chat_ids: list[int | str] = list(matched.chat_ids) if matched else []

    candidates = await collect_mention_candidates(context.bot, context.bot_data, chat_ids)
    # Pastikan tag yang sudah ada di job juga muncul di kandidat
    seen_c = {c["token"].lower() for c in candidates}
    for tok in current_mentions:
        if tok.lower() not in seen_c:
            candidates.append({"label": tok.split(":", 1)[0] if ":" in tok else tok, "token": tok})

    context.user_data["edit_mention_candidates"] = candidates
    context.user_data["edit_selected_mentions"] = list(current_mentions)

    current_tags = " ".join(current_mentions) if current_mentions else "(tidak ada)"
    keyboard = build_mentions_picker_keyboard(
        candidates,
        current_mentions,
        prefix="edittag",
        allow_clear=True,
    )
    await query.edit_message_text(
        f"<b>🏷️ Ubah Tag / Mention</b> (<code>{html.escape(target_id)}</code>)\n\n"
        f"Tag saat ini: <code>{html.escape(current_tags)}</code>\n\n"
        "Klik centang nama di bawah lalu klik <b>✅ Selesai</b>, "
        "atau ketik manual (contoh: <code>@budi @ani</code> / <code>Budi:123456789</code>):",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return EDIT_NEW_MENTIONS


async def edit_mentions_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    idx = int(query.data.split(":", 2)[2])
    candidates: list[dict[str, str]] = context.user_data.get("edit_mention_candidates", [])
    if not (0 <= idx < len(candidates)):
        return EDIT_NEW_MENTIONS

    token = candidates[idx]["token"]
    selected: list[str] = list(context.user_data.get("edit_selected_mentions", []))
    existing = next((t for t in selected if t.lower() == token.lower()), None)
    if existing:
        selected.remove(existing)
    else:
        selected.append(token)
    context.user_data["edit_selected_mentions"] = selected

    keyboard = build_mentions_picker_keyboard(candidates, selected, prefix="edittag", allow_clear=True)
    await query.edit_message_reply_markup(reply_markup=keyboard)
    return EDIT_NEW_MENTIONS


async def edit_mentions_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    selected: list[str] = list(context.user_data.pop("edit_selected_mentions", []))
    context.user_data.pop("edit_mention_candidates", None)
    return await _apply_edited_mentions(update, context, selected)


async def edit_clear_mentions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    context.user_data.pop("edit_selected_mentions", None)
    context.user_data.pop("edit_mention_candidates", None)
    return await _apply_edited_mentions(update, context, [])


async def edit_save_mentions_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return EDIT_NEW_MENTIONS

    try:
        mentions = parse_mentions(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return EDIT_NEW_MENTIONS

    remember_manual_mentions(context.bot_data, mentions)
    context.user_data.pop("edit_selected_mentions", None)
    context.user_data.pop("edit_mention_candidates", None)
    return await _apply_edited_mentions(update, context, mentions)


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
    register_jobs(
        scheduler,
        context.bot,
        jobs,
        config,
        context.bot_data.get("known_groups"),
        context.bot_data.get("tag_presets"),
    )
    context.bot_data["jobs"] = jobs

    tags_label = " ".join(mentions) if mentions else "(kosong / dihapus)"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📦 Ke Provisioning", callback_data="editdiv:provisioning"),
                InlineKeyboardButton("🛠️ Ke Assurance", callback_data="editdiv:assurance"),
                InlineKeyboardButton("🔧 Ke Maintenance", callback_data="editdiv:maintenance"),
            ],
            [
                InlineKeyboardButton("👤 Tes ke Diri Sendiri", callback_data=f"job:testself:{matched.id}"),
                InlineKeyboardButton("🚀 Tes ke Target", callback_data=f"job:test:{matched.id}"),
            ],
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


async def edit_save_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return EDIT_NEW_MESSAGE

    photo_id: str | None = None
    document_id: str | None = None
    if update.message.photo:
        photo_id = update.message.photo[-1].file_id
        new_text = (update.message.caption or "").strip()
    elif update.message.document:
        document_id = update.message.document.file_id
        new_text = (update.message.caption or "").strip()
    elif update.message.text:
        new_text = update.message.text.strip()
    else:
        return EDIT_NEW_MESSAGE

    if not new_text and not photo_id and not document_id:
        await update.message.reply_text("Pesan, foto, atau dokumen tidak boleh kosong:", reply_markup=build_cancel_keyboard())
        return EDIT_NEW_MESSAGE

    target_id = context.user_data.pop("edit_job_id", None)
    config: BotConfig = context.bot_data["config"]
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == target_id), None)
    if not matched:
        await update.message.reply_text("Sesi edit kedaluwarsa. Buka /menu lagi.", reply_markup=build_main_menu_keyboard())
        return ConversationHandler.END

    matched.message = new_text
    matched.photo = photo_id
    matched.document = document_id
    save_schedules(config.schedules_file, jobs)

    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    register_jobs(
        scheduler,
        context.bot,
        jobs,
        config,
        context.bot_data.get("known_groups"),
        context.bot_data.get("tag_presets"),
    )
    context.bot_data["jobs"] = jobs

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📦 Ke Provisioning", callback_data="editdiv:provisioning"),
                InlineKeyboardButton("🛠️ Ke Assurance", callback_data="editdiv:assurance"),
                InlineKeyboardButton("🔧 Ke Maintenance", callback_data="editdiv:maintenance"),
            ],
            [
                InlineKeyboardButton("👤 Tes ke Diri Sendiri", callback_data=f"job:testself:{matched.id}"),
                InlineKeyboardButton("🚀 Tes ke Target", callback_data=f"job:test:{matched.id}"),
            ],
            [InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="menu:home")],
        ]
    )
    photo_note = (
        "\n🖼️ <b>Foto baru terlampir!</b>"
        if photo_id
        else ("\n📎 <b>Dokumen baru terlampir!</b>" if document_id else "")
    )
    await update.message.reply_html(
        f"<b>✅ Pesan custom untuk <code>{matched.id}</code> berhasil diperbarui!</b>{photo_note}\n\n"
        f"<b>Pesan baru:</b>\n{html.escape(new_text or '(tanpa teks)')}",
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
    register_jobs(
        scheduler,
        context.bot,
        jobs,
        config,
        context.bot_data.get("known_groups"),
        context.bot_data.get("tag_presets"),
    )
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


async def edit_time_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target_id = context.user_data.get("edit_job_id", "")
    return await _handle_time_mode_menu(
        update,
        context,
        f"⏰ Atur Waktu Baru untuk {html.escape(target_id)}",
        EDIT_NEW_TIME,
    )


async def edit_time_day_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target_id = context.user_data.get("edit_job_id", "")
    return await _handle_time_day_menu(
        update,
        context,
        f"⏰ Atur Waktu Baru untuk {html.escape(target_id)}",
        EDIT_NEW_TIME,
    )


async def edit_time_day_apply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    raw_apply = query.data.split(":", 2)[2]
    selected_days: list[int] = list(context.user_data.get("time_selected_days", []))
    expr = _build_day_apply_expression(raw_apply, selected_days)
    config: BotConfig = context.bot_data["config"]
    spec = parse_flexible_schedule(expr, config.tzinfo)
    context.user_data.pop("time_input_mode", None)
    context.user_data.pop("time_selected_days", None)
    return await _apply_edited_time(update, context, spec)


async def edit_save_time_quick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()

    expr = query.data.split(":", 2)[2]
    config: BotConfig = context.bot_data["config"]
    spec = parse_flexible_schedule(expr, config.tzinfo)
    context.user_data.pop("time_input_mode", None)
    context.user_data.pop("time_selected_days", None)
    return await _apply_edited_time(update, context, spec)


async def edit_save_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return EDIT_NEW_TIME

    config: BotConfig = context.bot_data["config"]
    resolved_input = _resolve_time_text_with_mode(update.message.text, context.user_data)
    try:
        spec = parse_flexible_schedule(resolved_input, config.tzinfo)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return EDIT_NEW_TIME

    context.user_data.pop("time_input_mode", None)
    context.user_data.pop("time_selected_days", None)
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
        "user": (
            "👤 Tambah User (untuk Tag/Mention)",
            "<code>@username</code>, <code>Budi | @username</code>, atau <code>Budi | 217411824</code>\n"
            "<i>(Disarankan pakai @username agar mention selalu aktif 100%)</i>",
        ),
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
    known_groups_now: dict[str, dict[str, str]] = context.bot_data.get("known_groups", {})
    final_id, auto_title, auto_type, fetched_ok, fetched_uname = await resolve_chat_target_details(
        context.bot,
        target_id,
        target_type,
        known_groups=known_groups_now,
    )
    resolved_title = custom_label or auto_title
    resolved_type = auto_type if (fetched_ok and target_type == "other") else (auto_type if fetched_ok else target_type)

    remember_group(context.bot_data, final_id, resolved_title, resolved_type, username=fetched_uname)
    known_groups: dict[str, dict[str, str]] = context.bot_data.get("known_groups", {})
    text, markup = format_targets_menu(known_groups)

    uname_note = f" (@{html.escape(fetched_uname)})" if fetched_uname else ""
    note = ""
    if not custom_label and not fetched_ok:
        note = (
            "\n⚠️ <i>Nama asli belum terbaca oleh Telegram (pastikan bot sudah masuk ke grup/channel "
            "atau user sudah pernah klik /start, lalu tekan 🔄 Refresh Nama).</i>"
        )

    await update.message.reply_html(
        f"<b>✅ Target tersimpan!</b> {target_type_icon(resolved_type)} "
        f"<b>{html.escape(resolved_title)}</b>{uname_note} (<code>{html.escape(str(final_id))}</code>){note}\n\n{text}",
        reply_markup=markup,
    )
    return ConversationHandler.END


async def preset_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return ConversationHandler.END
    await query.answer()
    context.user_data.pop("new_preset", None)
    await query.edit_message_text(
        "<b>👥 Buat Preset Tim Baru (1/2)</b>\n\n"
        "Ketik <b>nama tim/shift</b> (contoh: <code>Shift Pagi</code> atau <code>Tim QC</code>):",
        parse_mode="HTML",
        reply_markup=build_cancel_keyboard(),
    )
    return PRESET_ASK_NAME


async def preset_name_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return PRESET_ASK_NAME
    name = update.message.text.strip()
    if not name or len(name) > 40:
        await update.message.reply_text("Nama tim harus 1–40 karakter:", reply_markup=build_cancel_keyboard())
        return PRESET_ASK_NAME

    candidates = await collect_mention_candidates(context.bot, context.bot_data, [])
    context.user_data["new_preset"] = {"name": name, "candidates": candidates, "selected": []}
    keyboard = build_mentions_picker_keyboard(candidates, [], prefix="prpick")
    await update.message.reply_html(
        f"<b>👥 Pilih Anggota untuk '{html.escape(name)}' (2/2)</b>\n\n"
        "Klik centang user di bawah lalu klik <b>✅ Selesai</b>, "
        "atau ketik manual (contoh: <code>@budi @ani</code> / <code>Budi:123456789</code>):",
        reply_markup=keyboard,
    )
    return PRESET_PICK_MEMBERS


async def preset_pick_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()
    idx = int(query.data.split(":", 2)[2])
    np = context.user_data.setdefault("new_preset", {})
    candidates: list[dict[str, str]] = np.get("candidates", [])
    if not (0 <= idx < len(candidates)):
        return PRESET_PICK_MEMBERS
    tok = candidates[idx]["token"]
    selected: list[str] = list(np.get("selected", []))
    existing = next((t for t in selected if t.lower() == tok.lower()), None)
    if existing:
        selected.remove(existing)
    else:
        selected.append(tok)
    np["selected"] = selected
    await query.edit_message_reply_markup(reply_markup=build_mentions_picker_keyboard(candidates, selected, prefix="prpick"))
    return PRESET_PICK_MEMBERS


async def _save_new_preset(update: Update, context: ContextTypes.DEFAULT_TYPE, tokens: list[str]) -> int:
    np = context.user_data.pop("new_preset", None)
    if not np or not tokens:
        if update.callback_query:
            await update.callback_query.answer("Pilih minimal 1 anggota tim!", show_alert=True)
            return PRESET_PICK_MEMBERS
        if update.message:
            await update.message.reply_text("Masukkan minimal 1 anggota tim:", reply_markup=build_cancel_keyboard())
            return PRESET_PICK_MEMBERS
        return ConversationHandler.END

    name: str = np["name"]
    config: BotConfig = context.bot_data["config"]
    presets: dict[str, list[str]] = dict(context.bot_data.get("tag_presets", {}))
    presets[name] = tokens
    save_tag_presets(config.resolved_presets_file, presets)
    context.bot_data["tag_presets"] = presets

    # Update scheduler agar @NamaTim langsung aktif di job berjalan
    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    register_jobs(scheduler, context.bot, jobs, config, context.bot_data.get("known_groups"), presets)

    text, markup = format_presets_menu(presets)
    msg = f"<b>✅ Preset Tim '{html.escape(name)}' tersimpan!</b>\n\n{text}"
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=markup)
    elif update.message:
        await update.message.reply_html(msg, reply_markup=markup)
    return ConversationHandler.END


async def preset_pick_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    np = context.user_data.get("new_preset", {})
    selected: list[str] = list(np.get("selected", []))
    if not selected:
        await query.answer("Pilih minimal 1 anggota tim terlebih dahulu!", show_alert=True)
        return PRESET_PICK_MEMBERS
    await query.answer()
    return await _save_new_preset(update, context, selected)


async def preset_pick_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return PRESET_PICK_MEMBERS
    try:
        typed = parse_mentions(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=build_cancel_keyboard())
        return PRESET_PICK_MEMBERS
    remember_manual_mentions(context.bot_data, typed)
    np = context.user_data.setdefault("new_preset", {})
    merged: list[str] = list(np.get("selected", []))
    seen = {t.lower() for t in merged}
    for tok in typed:
        if tok.lower() not in seen:
            seen.add(tok.lower())
            merged.append(tok)
    return await _save_new_preset(update, context, merged)


async def send_backup_files(chat_id: int, bot: Bot, config: BotConfig) -> int:
    """Kirim file JSON backup ke chat admin."""
    files = [
        config.schedules_file,
        config.resolved_groups_file,
        config.resolved_admins_file,
        config.resolved_presets_file,
    ]
    sent = 0
    for fpath in files:
        if fpath.exists() and fpath.is_file():
            with fpath.open("rb") as fh:
                await bot.send_document(chat_id=chat_id, document=fh, filename=fpath.name)
            sent += 1
    return sent


def restore_backup_file(
    config: BotConfig,
    filename: str,
    raw_bytes: bytes,
    bot_data: dict[str, Any],
    bot: Bot | None = None,
) -> str:
    """Validasi dan pulihkan satu file backup JSON (schedules/groups/admins/presets)."""
    clean_name = Path(filename).name.lower()
    allowed = {"schedules.json", "groups.json", "admins.json", "presets.json"}
    if clean_name not in allowed:
        raise ValueError(
            f"Nama file '{filename}' tidak didukung. Upload salah satu: schedules.json, groups.json, admins.json, atau presets.json."
        )

    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"File JSON tidak valid: {exc}") from exc

    scheduler: AsyncIOScheduler | None = bot_data.get("scheduler")

    if clean_name == "schedules.json":
        if not isinstance(parsed, list):
            raise ValueError("Format schedules.json harus berupa JSON array.")
        tmp_path = config.schedules_file.with_suffix(".restore.tmp")
        try:
            tmp_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            validated_jobs = load_schedules(tmp_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
        save_schedules(config.schedules_file, validated_jobs)
        bot_data["jobs"] = validated_jobs
        if scheduler is not None:
            register_jobs(
                scheduler,
                bot,
                validated_jobs,
                config,
                bot_data.get("known_groups"),
                bot_data.get("tag_presets"),
            )
        return f"schedules.json dipulihkan ({len(validated_jobs)} jadwal)."

    if clean_name == "groups.json":
        if not isinstance(parsed, dict):
            raise ValueError("Format groups.json harus berupa JSON object.")
        save_known_groups(config.resolved_groups_file, parsed)
        reloaded_groups = load_known_groups(config.resolved_groups_file)
        bot_data["known_groups"] = reloaded_groups
        if scheduler is not None:
            register_jobs(scheduler, bot, bot_data.get("jobs", []), config, reloaded_groups, bot_data.get("tag_presets"))
        return f"groups.json dipulihkan ({len(reloaded_groups)} target)."

    if clean_name == "admins.json":
        if not isinstance(parsed, list):
            raise ValueError("Format admins.json harus berupa JSON array berisi User ID.")
        restored_admins = {int(x) for x in parsed if isinstance(x, int) or (isinstance(x, str) and x.strip().isdigit())}
        save_extra_admins(config.resolved_admins_file, restored_admins)
        bot_data["extra_admins"] = restored_admins
        return f"admins.json dipulihkan ({len(restored_admins)} admin tambahan)."

    if not isinstance(parsed, dict):
        raise ValueError("Format presets.json harus berupa JSON object.")
    cleaned_presets = {
        str(k): [str(t) for t in v]
        for k, v in parsed.items()
        if isinstance(v, list) and v
    }
    save_tag_presets(config.resolved_presets_file, cleaned_presets)
    bot_data["tag_presets"] = cleaned_presets
    if scheduler is not None:
        register_jobs(scheduler, bot, bot_data.get("jobs", []), config, bot_data.get("known_groups"), cleaned_presets)
    return f"presets.json dipulihkan ({len(cleaned_presets)} preset tim)."


async def restore_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mulai sesi restore backup JSON via tombol atau perintah /restore."""
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return ConversationHandler.END

    prompt = (
        "<b>♻️ Restore Backup Konfigurasi</b>\n\n"
        "Kirim/upload salah satu file JSON hasil backup ke chat ini:\n"
        "• <code>schedules.json</code>\n"
        "• <code>groups.json</code>\n"
        "• <code>admins.json</code>\n"
        "• <code>presets.json</code>\n\n"
        "<i>Bot akan memvalidasi format JSON terlebih dahulu sebelum menimpa data saat ini.</i>"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(prompt, parse_mode="HTML", reply_markup=build_cancel_keyboard())
    elif update.message:
        await update.message.reply_html(prompt, reply_markup=build_cancel_keyboard())
    return RESTORE_UPLOAD


async def restore_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.document:
        if update.message:
            await update.message.reply_text(
                "Silakan upload file dokumen JSON (contoh: schedules.json):",
                reply_markup=build_cancel_keyboard(),
            )
        return RESTORE_UPLOAD

    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return ConversationHandler.END

    doc = update.message.document
    filename = doc.file_name or ""
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        raw_bytes = bytes(await tg_file.download_as_bytearray())
        summary = restore_backup_file(config, filename, raw_bytes, context.bot_data, context.bot)
    except (ValueError, TelegramError) as exc:
        await update.message.reply_text(f"❌ Restore gagal: {exc}", reply_markup=build_cancel_keyboard())
        return RESTORE_UPLOAD

    await update.message.reply_html(
        f"<b>✅ Restore berhasil!</b>\n{html.escape(summary)}",
        reply_markup=build_back_menu_keyboard(),
    )
    return ConversationHandler.END


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return
    count = await send_backup_files(update.effective_chat.id, context.bot, config)
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("♻️ Restore Backup (Upload JSON)", callback_data="backup:restore")],
            [InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="menu:home")],
        ]
    )
    await update.message.reply_html(
        f"💾 <b>Backup selesai!</b> {count} file konfigurasi telah dikirim.",
        reply_markup=markup,
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return
    history = load_delivery_history(config.resolved_history_file)
    text, markup = format_history_menu(history)
    await update.message.reply_html(text, reply_markup=markup)


async def cmd_presets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return
    presets: dict[str, list[str]] = context.bot_data.get("tag_presets", {})
    text, markup = format_presets_menu(presets)
    await update.message.reply_html(text, reply_markup=markup)


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
    data = query.data

    # Tombol konfirmasi '✅ Sudah Dikerjakan' di grup boleh diklik oleh anggota grup (non-admin sekalipun)
    if data == "ack:noop":
        await query.answer("Tugas ini sudah ditandai selesai.")
        return

    if data.startswith("ack:done:"):
        job_id = data.split(":", 2)[2]
        user = update.effective_user
        actor = extract_chat_title(user) if user else "Anggota"
        now = datetime.now(config.tzinfo)
        today_str = now.strftime("%Y-%m-%d")

        state = load_runtime_state(config.resolved_state_file)
        state.setdefault("ack_dates", {})[job_id] = today_str
        save_runtime_state(config.resolved_state_file, state)

        append_delivery_history(
            config.resolved_history_file,
            {
                "timestamp": now.strftime("%d/%m %H:%M:%S %Z"),
                "job_id": job_id,
                "job_name": f"Selesai oleh {actor}",
                "chat_id": str(update.effective_chat.id if update.effective_chat else "-"),
                "status": "ok",
                "error": f"Ditandai selesai oleh {actor}",
            },
        )
        done_label = f"✅ Dikerjakan oleh {actor[:18]} ({now.strftime('%H:%M')})"
        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(done_label, callback_data="ack:noop")]])
            )
        except TelegramError:
            pass
        await query.answer(f"✅ Ditandai selesai oleh {actor}! Pengingat hari ini dijeda.", show_alert=False)
        return

    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return

    await query.answer()
    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]

    if data == "menu:home":
        if context.user_data is not None:
            context.user_data["active_division"] = None
        text = format_main_menu_text(update, config, len(scheduler.get_jobs()))
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_main_menu_keyboard())
        return

    if data in ("menu:setting", "menu:autosender"):
        if context.user_data is not None:
            context.user_data["active_division"] = None
        await query.edit_message_text(
            "<b>⚙️ Setting — Telegram AutoSender</b>\n\n"
            f"📅 Total jadwal aktif: <b>{len(scheduler.get_jobs())}</b>\n"
            "Kelola semua jadwal, target grup, preset tim, admin, dan backup di bawah ini:",
            parse_mode="HTML",
            reply_markup=build_autosender_menu_keyboard(),
        )
        return

    if data == "menu:provisioning":
        if context.user_data is not None:
            context.user_data["active_division"] = "provisioning"
        chat_id = update.effective_chat.id if update.effective_chat else "N/A"
        div_count = sum(1 for j in context.bot_data.get("jobs", []) if (j.division or "") == "provisioning")
        await query.edit_message_text(
            "<b>📦 Menu Provisioning</b>\n\n"
            "Terintegrasi dengan aplikasi <b>ADING Mobile → WFM FLOW</b>.\n"
            f"📅 Jadwal tersimpan (Provisioning): <b>{div_count}</b>\n"
            f"🆔 Chat ID aktif: <code>{chat_id}</code>\n\n"
            "Pilih menu Provisioning di bawah ini:",
            parse_mode="HTML",
            reply_markup=build_provisioning_menu_keyboard(),
        )
        return

    if data == "prov:wfmstatus":
        worker: WfmFlowWorker | None = context.bot_data.get("wfm_worker")
        st = worker.get_status() if worker else {"session_active": False, "waiting_for_otp": False, "last_keepalive_at": "-", "dispatches_today": 0}
        if st.get("waiting_for_otp"):
            ses_lbl = "🟡 Menunggu /otp"
        elif st.get("session_active"):
            ses_lbl = "🟢 Aktif (Terkunci 24/7)"
        else:
            ses_lbl = "🔴 Belum Login / Butuh /otp"

        known_groups = context.bot_data.get("known_groups", {})
        ch_lines: list[str] = []
        for cid_str, info in known_groups.items():
            ctype = str(info.get("type") or "").lower()
            if ctype in ("channel", "group", "supergroup") or str(cid_str).startswith("-"):
                title = html.escape(str(info.get("title") or cid_str))
                ch_lines.append(f"• {title} (<code>{cid_str}</code>)")
        ch_preview = "\n".join(ch_lines[:20]) if ch_lines else "<i>Belum ada channel terdeteksi.</i>"
        if len(ch_lines) > 20:
            ch_preview += f"\n<i>...dan {len(ch_lines) - 20} channel lainnya.</i>"

        await query.edit_message_text(
            "<b>🖥️ Status WFM FLOW Standalone</b>\n\n"
            f"• Sesi FLOW: <b>{ses_lbl}</b>\n"
            f"• Keep-Alive Terakhir: <code>{html.escape(str(st.get('last_keepalive_at', '-')))}</code>\n"
            f"• Dispatch Hari Ini: <b>{st.get('dispatches_today', 0)}</b> WO\n"
            f"• Channel/Grup Terdeteksi: <b>{len(ch_lines)}</b>\n\n"
            f"<b>Daftar Channel Teknisi:</b>\n{ch_preview}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Refresh Status", callback_data="prov:wfmstatus")],
                    [
                        InlineKeyboardButton("⬅️ Kembali ke Provisioning", callback_data="menu:provisioning"),
                        InlineKeyboardButton("🏠 Menu Awal", callback_data="menu:home"),
                    ],
                ]
            ),
        )
        return

    if data == "menu:ading":
        chat_id = update.effective_chat.id if update.effective_chat else "N/A"
        thread_id = getattr(query.message, "message_thread_id", None) if query.message else None
        thread_line = f"\n🧵 Topic/Thread ID: <code>{thread_id}</code>" if thread_id is not None else ""
        await query.edit_message_text(
            "<b>📱 ADING Mobile — Auto-Dispatch WFM FLOW</b>\n\n"
            "<b>Alur Otomatis:</b>\n"
            "1. Klik <b>Dispatch / Redispatch</b> di aplikasi ADING Mobile\n"
            "2. Aplikasi otomatis klik <b>Salin</b> &amp; ambil <b>WONUM</b>\n"
            "3. Aplikasi buka <b>FLOW Telkom Akses</b> (sesi OTP tersimpan di background)\n"
            "4. Crop kartu <b>Workorder</b> &amp; kirim otomatis <b>SS + Data Salin</b> ke Telegram ini\n\n"
            "<b>Konfigurasi Tujuan Chat Ini:</b>\n"
            f"🆔 Chat ID: <code>{chat_id}</code>{thread_line}\n"
            "🤖 Bot: <code>@biarndalupa_bot</code>\n\n"
            "Gunakan tombol di bawah untuk download APK terbaru atau test koneksi:",
            parse_mode="HTML",
            reply_markup=build_ading_menu_keyboard(),
        )
        return

    if data == "prov:testchannels":
        await query.edit_message_text(
            "⏳ <b>Mengirim pesan tes ke 7 channel teknisi...</b>",
            parse_mode="HTML",
        )
        ok_count = 0
        result_lines = ["<b>📡 Hasil Tes Kirim 7 Channel Teknisi:</b>\n"]
        for tech_name, ch_id in ADING_TECH_CHANNELS.items():
            mention = ADING_TECH_MENTIONS.get(tech_name, "")
            mention_line = f"\n👷 {mention}" if mention else ""
            msg_text = (
                "✅ <b>Tes Koneksi Bot Auto-Dispatch ADING Mobile</b>\n"
                f"Channel Teknisi: <b>{html.escape(tech_name)}</b>\n"
                f"Status: Terhubung &amp; siap menerima order WFM FLOW.{mention_line}"
            )
            try:
                sent_msg = await context.bot.send_message(
                    chat_id=int(ch_id),
                    text=msg_text,
                    parse_mode="HTML",
                )
                ok_count += 1
                ch_title = sent_msg.chat.title or tech_name
                result_lines.append(
                    f"✅ <b>{html.escape(tech_name)}</b> (<code>{ch_id}</code>)\n"
                    f"   └ {html.escape(ch_title)} • msg #{sent_msg.message_id}"
                )
            except TelegramError as exc:
                result_lines.append(
                    f"❌ <b>{html.escape(tech_name)}</b> (<code>{ch_id}</code>)\n"
                    f"   └ <i>{html.escape(str(exc))}</i>"
                )
        result_lines.append(f"\n<b>Total Berhasil: {ok_count}/{len(ADING_TECH_CHANNELS)}</b>")
        await query.edit_message_text(
            "\n".join(result_lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Tes Ulang 7 Channel", callback_data="prov:testchannels")],
                    [
                        InlineKeyboardButton("⬅️ Kembali ke Provisioning", callback_data="menu:provisioning"),
                        InlineKeyboardButton("🏠 Menu Awal", callback_data="menu:home"),
                    ],
                ]
            ),
        )
        return

    if data == "ading:techmap":
        known_groups: dict[str, dict[str, str]] = context.bot_data.get("known_groups", {})
        known_members: dict[str, dict[str, dict[str, str]]] = context.bot_data.get("known_members", {})
        personal_candidates: list[tuple[str, str]] = []
        for cid_str, info in known_groups.items():
            if info.get("type") in ("user", "private") or (cid_str.isdigit()):
                personal_candidates.append((str(info.get("title") or "").upper(), cid_str))
        for _scope, mems in known_members.items():
            for mkey, minfo in mems.items():
                lbl = str(minfo.get("label") or "").upper()
                tok = str(minfo.get("token") or "")
                m_uid = re.search(r"tg://user\?id=(\d+)", tok)
                uid_val = m_uid.group(1) if m_uid else (mkey if str(mkey).isdigit() else "")
                if uid_val:
                    personal_candidates.append((lbl, uid_val))

        mapping_lines: list[str] = []
        for tech_name, ch_id in ADING_TECH_CHANNELS.items():
            found_uid = ""
            t_tokens = [w for w in tech_name.upper().split() if len(w) >= 3]
            for cand_label, cand_uid in personal_candidates:
                if tech_name.upper() in cand_label or (t_tokens and all(tok in cand_label for tok in t_tokens[:2])):
                    found_uid = cand_uid
                    break
            if found_uid:
                mapping_lines.append(f"{tech_name}={ch_id},{found_uid}")
            else:
                mapping_lines.append(f"{tech_name}={ch_id}")

        block = "\n".join(mapping_lines)
        await query.edit_message_text(
            "<b>👷 Mapping Channel &amp; ID Pribadi Teknisi (ADING)</b>\n\n"
            "Salin blok di bawah ke menu <b>Pengaturan WFM → Mapping Teknisi</b> di ADING Mobile "
            "(format: <code>NAMA=CHANNEL_ID,ID_PRIBADI</code>):\n\n"
            f"<pre>{html.escape(block)}</pre>\n\n"
            "<i>Tips: pastikan <code>@biarndalupa_bot</code> sudah jadi Admin di semua channel teknisi "
            "dan masing-masing teknisi sudah klik /start di bot agar pesan pribadi bisa terkirim.</i>",
            parse_mode="HTML",
            reply_markup=build_ading_menu_keyboard(),
        )
        return

    if data == "ading:test":
        chat = update.effective_chat
        if chat:
            thread_id = getattr(query.message, "message_thread_id", None) if query.message else None
            send_kwargs: dict[str, Any] = {
                "chat_id": chat.id,
                "text": (
                    "✅ <b>Test Integrasi ADING Mobile → Provisioning Berhasil!</b>\n\n"
                    f"🆔 Chat ID: <code>{chat.id}</code>\n"
                    "Bot siap menerima kirim otomatis <b>SS Workorder FLOW + Data Salin</b> dari ADING Mobile."
                ),
                "parse_mode": "HTML",
            }
            if thread_id is not None:
                send_kwargs["message_thread_id"] = thread_id
            await context.bot.send_message(**send_kwargs)
        return

    if data == "menu:assurance":
        if context.user_data is not None:
            context.user_data["active_division"] = "assurance"
        div_count = sum(1 for j in context.bot_data.get("jobs", []) if (j.division or "") == "assurance")
        await query.edit_message_text(
            "<b>🛠️ Menu Assurance</b>\n\n"
            "Kelola jadwal pengingat, target grup, dan laporan operasional divisi <b>Assurance</b>.\n"
            f"📅 Jadwal tersimpan (Assurance): <b>{div_count}</b>\n\n"
            "Pilih menu di bawah ini:",
            parse_mode="HTML",
            reply_markup=build_assurance_menu_keyboard(),
        )
        return

    if data == "menu:maintenance":
        if context.user_data is not None:
            context.user_data["active_division"] = "maintenance"
        div_count = sum(1 for j in context.bot_data.get("jobs", []) if (j.division or "") == "maintenance")
        await query.edit_message_text(
            "<b>🔧 Menu Maintenance</b>\n\n"
            "Kelola jadwal pengingat rutin, preset tim, dan target grup divisi <b>Maintenance</b>.\n"
            f"📅 Jadwal tersimpan (Maintenance): <b>{div_count}</b>\n\n"
            "Pilih menu di bawah ini:",
            parse_mode="HTML",
            reply_markup=build_maintenance_menu_keyboard(),
        )
        return

    if data == "menu:targets":
        known_groups: dict[str, dict[str, str]] = context.bot_data.get("known_groups", {})
        text, markup = format_targets_menu(known_groups)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return

    if data == "tgtrefresh":
        known_groups = dict(context.bot_data.get("known_groups", {}))
        updated_count = 0
        for cid_str, info in list(known_groups.items()):
            parsed_id = parse_single_target_id(cid_str)
            final_id, title, rtype, fetched_ok, fetched_uname = await resolve_chat_target_details(
                context.bot,
                parsed_id,
                info.get("type", "other"),
                known_groups=known_groups,
            )
            if fetched_ok:
                if str(final_id) != cid_str:
                    forget_group(context.bot_data, cid_str)
                # Pertahankan custom title untuk user jika sudah diberi nama sendiri
                final_title = info.get("title") if (rtype in ("user", "private") and info.get("title") and info.get("title") != cid_str) else title
                remember_group(context.bot_data, final_id, final_title, rtype, username=fetched_uname)
                updated_count += 1
        refreshed = context.bot_data.get("known_groups", {})
        text, markup = format_targets_menu(refreshed)
        try:
            await query.edit_message_text(
                f"🔄 Refresh selesai! <b>{updated_count}</b> target &amp; username berhasil diperbarui dari Telegram.\n\n{text}",
                parse_mode="HTML",
                reply_markup=markup,
            )
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
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
        thread_id = getattr(query.message, "message_thread_id", None) if query.message else None
        thread_line = f"\n🧵 Topic ID: <code>{thread_id}</code>" if thread_id is not None else ""
        await query.edit_message_text(
            f"🆔 Chat ID: <code>{chat.id if chat else 'N/A'}</code>\n"
            f"📌 Tipe: <code>{chat.type if chat else 'N/A'}</code>\n"
            f"👤 User ID: <code>{user.id if user else 'N/A'}</code>{thread_line}",
            parse_mode="HTML",
            reply_markup=build_back_menu_keyboard(),
        )
        return

    if data == "menu:presets":
        presets: dict[str, list[str]] = context.bot_data.get("tag_presets", {})
        text, markup = format_presets_menu(presets)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return

    if data.startswith("presetdel:"):
        idx = int(data.split(":", 1)[1])
        presets = dict(context.bot_data.get("tag_presets", {}))
        names = sorted(presets.keys(), key=str.lower)
        if 0 <= idx < len(names):
            removed = names[idx]
            presets.pop(removed, None)
            save_tag_presets(config.resolved_presets_file, presets)
            context.bot_data["tag_presets"] = presets
            jobs_list: list[ScheduleJob] = context.bot_data.get("jobs", [])
            register_jobs(scheduler, context.bot, jobs_list, config, context.bot_data.get("known_groups"), presets)
            text, markup = format_presets_menu(presets)
            await query.edit_message_text(
                f"🗑️ Preset tim <b>{html.escape(removed)}</b> dihapus.\n\n{text}",
                parse_mode="HTML",
                reply_markup=markup,
            )
        return

    if data == "menu:history":
        history = load_delivery_history(config.resolved_history_file)
        text, markup = format_history_menu(history)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
        return

    if data == "menu:backup":
        chat = update.effective_chat
        if chat:
            count = await send_backup_files(chat.id, context.bot, config)
            markup = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("♻️ Restore Backup (Upload JSON)", callback_data="backup:restore")],
                    [InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="menu:home")],
                ]
            )
            await query.edit_message_text(
                f"💾 <b>Backup selesai!</b> {count} file konfigurasi telah dikirim ke chat ini.",
                parse_mode="HTML",
                reply_markup=markup,
            )
        return

    if data == "menu:list" or data.startswith("menu:list:"):
        div_filter = data.split(":", 2)[2] if data.startswith("menu:list:") else None
        text, markup = format_jobs_list_and_keyboard(
            scheduler,
            context.bot_data.get("jobs", []),
            division=div_filter,
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return

    if data == "menu:reload":
        try:
            jobs = load_schedules(config.schedules_file)
            count = register_jobs(
                scheduler,
                context.bot,
                jobs,
                config,
                context.bot_data.get("known_groups"),
                context.bot_data.get("tag_presets"),
            )
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

    if data.startswith("job:testself:"):
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
        user = update.effective_user
        chat = update.effective_chat
        self_chat_id = user.id if user else (chat.id if chat else 0)
        if not self_chat_id:
            return
        ok, err_msg, warnings = await send_self_preview_message(
            context.bot,
            self_chat_id=self_chat_id,
            message=matched.message,
            mentions=matched.mentions,
            photo=matched.photo,
            document=matched.document,
            require_ack=matched.require_ack,
            parse_mode=matched.parse_mode,
            target_chat_ids=matched.chat_ids,
            saved_targets=context.bot_data.get("known_groups"),
            tag_presets=context.bot_data.get("tag_presets"),
            config=config,
        )
        if not ok:
            await context.bot.send_message(
                chat_id=self_chat_id,
                text=f"❌ Gagal mengirim preview <code>{html.escape(matched.id)}</code>:\n<code>{html.escape(err_msg or 'Unknown error')}</code>",
                parse_mode="HTML",
            )
        elif warnings:
            warn_lines = "\n".join(f"• {html.escape(w)}" for w in warnings[:5])
            await context.bot.send_message(
                chat_id=self_chat_id,
                text=f"⚠️ <b>Catatan Preview Mention ({html.escape(matched.id)}):</b>\n{warn_lines}",
                parse_mode="HTML",
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
        diag: list[str] = []
        success, failed = await send_scheduled_message(
            context.bot,
            matched,
            context.bot_data.get("known_groups"),
            context.bot_data.get("tag_presets"),
            config,
            ignore_window=True,
            diagnostics=diag,
        )
        diag_text = ""
        if diag:
            diag_lines = "\n".join(f"• {html.escape(d)}" for d in diag[:5])
            diag_text = f"\n\n⚠️ <b>Catatan Mention:</b>\n{diag_lines}"
        await query.edit_message_text(
            f"🚀 Tes kirim <code>{matched.id}</code> selesai:\n"
            f"✅ Terkirim: <b>{success}</b> | ❌ Gagal: <b>{failed}</b>{diag_text}",
            parse_mode="HTML",
            reply_markup=build_back_menu_keyboard(),
        )
        return

    if data == "job:cleanup":
        jobs = context.bot_data.get("jobs", [])
        kept, removed = purge_expired_date_jobs(jobs, config.tzinfo)
        save_schedules(config.schedules_file, kept)
        register_jobs(
            scheduler,
            context.bot,
            kept,
            config,
            context.bot_data.get("known_groups"),
            context.bot_data.get("tag_presets"),
        )
        context.bot_data["jobs"] = kept
        text, markup = format_jobs_list_and_keyboard(scheduler, kept)
        await query.edit_message_text(
            f"🧹 <b>{len(removed)}</b> jadwal kadaluarsa berhasil dibersihkan.\n\n{text}",
            parse_mode="HTML",
            reply_markup=markup,
        )
        return

    if data.startswith("job:del:"):
        target_id = data.split(":", 2)[2]
        jobs = context.bot_data.get("jobs", [])
        remaining = [j for j in jobs if j.id != target_id]
        save_schedules(config.schedules_file, remaining)
        register_jobs(
            scheduler,
            context.bot,
            remaining,
            config,
            context.bot_data.get("known_groups"),
            context.bot_data.get("tag_presets"),
        )
        context.bot_data["jobs"] = remaining

        text, markup = format_jobs_list_and_keyboard(scheduler, remaining)
        await query.edit_message_text(
            f"🗑️ Jadwal <code>{html.escape(target_id)}</code> dihapus.\n\n{text}",
            parse_mode="HTML",
            reply_markup=markup,
        )


async def edit_set_division(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return ConversationHandler.END
    await query.answer()
    target_div = query.data.split(":", 1)[1].strip().lower()
    if target_div not in DIVISION_LABELS:
        return EDIT_ACTION
    job_id = context.user_data.get("edit_job_id") if context.user_data else None
    config: BotConfig = context.bot_data["config"]
    jobs: list[ScheduleJob] = context.bot_data.get("jobs", [])
    matched = next((j for j in jobs if j.id == job_id), None)
    if not matched:
        await query.edit_message_text("Jadwal tidak ditemukan.", reply_markup=build_main_menu_keyboard())
        return ConversationHandler.END
    matched.division = target_div
    save_schedules(config.schedules_file, jobs)
    context.bot_data["jobs"] = jobs
    await query.edit_message_text(
        f"✅ Jadwal <b>{html.escape(matched.name)}</b> (<code>{matched.id}</code>) "
        f"berhasil dipindahkan ke divisi <b>{DIVISION_LABELS[target_div]}</b>!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"📋 Lihat Jadwal {DIVISION_LABELS[target_div]}", callback_data=f"menu:list:{target_div}")],
                [InlineKeyboardButton("⬅️ Kembali ke Menu Awal", callback_data="menu:home")],
            ]
        ),
    )
    return ConversationHandler.END


def find_matching_technician_channels(
    known_groups: dict[str, dict[str, str]],
    leader_name: str = "",
    leader_nik: str = "",
    nama_tim: str = "",
) -> list[tuple[str, str]]:
    """Cari otomatis channel/grup teknisi dari known_groups berdasarkan NIK atau Nama Teknisi."""
    nik_candidates: list[str] = []
    clean_nik = re.sub(r"\D+", "", leader_nik or "")
    if len(clean_nik) >= 5:
        nik_candidates.append(clean_nik)
    for m in re.findall(r"\b\d{5,12}\b", nama_tim or ""):
        if m not in nik_candidates:
            nik_candidates.append(m)

    raw_name = (leader_name or "").strip()
    if not raw_name and nama_tim:
        raw_name = nama_tim.split("(")[0].strip()
    name_upper = re.sub(r"[^A-Z0-9\s]", " ", raw_name.upper())
    stop_words = {"MUHAMMAD", "MUH", "MOH", "AHMAD", "ANDI", "BIN", "TIM", "TEAM", "SNY", "NNK", "TRK", "KLT"}
    all_tokens = [w for w in name_upper.split() if len(w) >= 3]
    distinct_tokens = [w for w in all_tokens if w not in stop_words] or all_tokens

    # Gabungkan known_groups + seed channel yang sudah dikenal
    candidates: dict[str, str] = {}
    for cid_str, info in (known_groups or {}).items():
        ctype = str(info.get("type") or "").lower()
        if ctype in ("channel", "group", "supergroup") or str(cid_str).startswith("-"):
            candidates[str(cid_str)] = str(info.get("title") or "")
    for tname, cid_str in ADING_TECH_CHANNELS.items():
        if cid_str not in candidates:
            candidates[cid_str] = tname

    # Prioritas 1: Cocokkan NIK persis (batas digit) di judul channel agar aman lintas Witel
    if nik_candidates:
        nik_matches: list[tuple[str, str]] = []
        for cid_str, title in candidates.items():
            for nik in nik_candidates:
                if re.search(rf"(?<!\d){re.escape(nik)}(?!\d)", title):
                    nik_matches.append((cid_str, title))
                    break
        if nik_matches:
            return nik_matches

    # Prioritas 2: Cocokkan nama teknisi di judul channel
    if all_tokens:
        scored: list[tuple[int, str, str]] = []
        for cid_str, title in candidates.items():
            t_clean = re.sub(r"[^A-Z0-9\s]", " ", title.upper())
            t_words = set(t_clean.split())
            if name_upper.strip() and name_upper.strip() in t_clean:
                scored.append((100, cid_str, title))
                continue
            distinct_hits = sum(1 for w in distinct_tokens if w in t_words or w in t_clean)
            if distinct_hits >= 2 or (len(distinct_tokens) == 1 and distinct_hits == 1):
                scored.append((50 + distinct_hits * 10, cid_str, title))
            elif distinct_hits == 1 and any(len(w) >= 5 and w in t_words for w in distinct_tokens):
                scored.append((30, cid_str, title))
        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            top_score = scored[0][0]
            return [(cid, title) for sc, cid, title in scored if sc == top_score]

    return []


def start_ading_http_bridge(application: Application, loop: asyncio.AbstractEventLoop) -> None:
    """Jalankan HTTP bridge ringan di $PORT untuk menerima dispatch SS dari ADING Mobile & Chrome Extension."""
    port = int(os.getenv("PORT", "8080"))
    expected_api_key = os.getenv("ADING_API_KEY", "ading-wfm-2026-telkom").strip()

    class AdingBridgeHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            logger.info("HTTP %s", format % args)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Ading-Key")
            self.end_headers()

        def do_GET(self) -> None:
            if self.path.startswith("/health") or self.path == "/":
                kg = application.bot_data.get("known_groups", {})
                worker = application.bot_data.get("wfm_worker")
                wfm_st = worker.get_status() if worker else {}
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "service": "ading-telegram-bridge",
                        "known_chats": len(kg),
                        "wfm_session_active": bool(wfm_st.get("session_active")),
                        "dispatches_today": int(wfm_st.get("dispatches_today", 0)),
                    },
                )
                return
            self._send_json(404, {"ok": False, "error": "Not found"})

        def do_POST(self) -> None:
            if not (self.path.startswith("/api/ading/dispatch") or self.path.startswith("/api/wfm/dispatch")):
                self._send_json(404, {"ok": False, "error": "Not found"})
                return
            if expected_api_key:
                req_key = (self.headers.get("X-Ading-Key") or "").strip()
                if req_key != expected_api_key:
                    self._send_json(401, {"ok": False, "error": "Unauthorized: X-Ading-Key tidak valid"})
                    return
            try:
                content_len = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(content_len)
                data = json.loads(raw.decode("utf-8"))

                leader_name = str(data.get("leader_name") or "").strip()
                leader_nik = str(data.get("leader_nik") or "").strip()
                nama_tim = str(data.get("nama_tim") or "").strip()
                caption = str(data.get("caption") or "").strip()
                photo_b64 = str(data.get("photo_base64") or "").strip()
                wonum = str(data.get("wonum") or "").strip().upper()
                fallback_cid = str(data.get("fallback_chat_id") or "217411824").strip()
                is_standalone_wfm = self.path.startswith("/api/wfm/dispatch") or (not photo_b64 and bool(wonum))

                if not is_standalone_wfm and not photo_b64:
                    self._send_json(400, {"ok": False, "error": "photo_base64 atau wonum wajib diisi"})
                    return

                async def _dispatch_async() -> dict[str, Any]:
                    nonlocal caption
                    if is_standalone_wfm:
                        worker: WfmFlowWorker = application.bot_data["wfm_worker"]

                        async def _notify_admin_otp(msg_html: str) -> None:
                            for adm in (application.bot_data["config"].admin_ids or {int(fallback_cid)}):
                                try:
                                    await application.bot.send_message(chat_id=adm, text=msg_html, parse_mode="HTML")
                                except TelegramError:
                                    pass

                        card = await worker.capture_workorder(wonum, _notify_admin_otp)
                        png_bytes_local = card["png_bytes"]
                        caption = (
                            unmask_caption_with_flow_data(caption, card)
                            if caption
                            else build_flow_summary_caption(wonum, card)
                        )
                    else:
                        png_bytes_local = base64.b64decode(photo_b64)

                    known_groups = application.bot_data.get("known_groups", {})
                    matched = find_matching_technician_channels(known_groups, leader_name, leader_nik, nama_tim)

                    hay_upper = f"{leader_name} {nama_tim}".upper()
                    mentions = [m for k, m in ADING_TECH_MENTIONS.items() if k in hay_upper]
                    final_caption = f"{caption}\n\n👷 {' '.join(mentions)}" if mentions else caption
                    if len(final_caption) > 1020:
                        final_caption = final_caption[:1020]

                    sent_to: list[str] = []
                    matched_titles: list[str] = []
                    sent_records: list[dict[str, int]] = []
                    for cid_str, title in matched:
                        try:
                            bio = io.BytesIO(png_bytes_local)
                            bio.name = "workorder.png"
                            sent_msg = await application.bot.send_photo(chat_id=int(cid_str), photo=bio, caption=final_caption)
                            sent_to.append(cid_str)
                            matched_titles.append(title)
                            sent_records.append({"chat_id": int(cid_str), "message_id": sent_msg.message_id})
                        except TelegramError as exc:
                            logger.warning("Gagal kirim ke channel %s (%s): %s", title, cid_str, exc)

                    used_fallback = False
                    if not sent_to and fallback_cid:
                        tech_label = leader_name or nama_tim or leader_nik or "Teknisi"
                        fb_cap = (
                            f"⚠️ Channel teknisi ({tech_label}) belum ditemukan / bot belum Admin.\n"
                            f"Masukkan @biarndalupa_bot sebagai Admin di channel teknisi (judul memuat Nama/NIK).\n\n"
                            f"{final_caption}"
                        )[:1020]
                        try:
                            bio = io.BytesIO(png_bytes_local)
                            bio.name = "workorder.png"
                            sent_msg = await application.bot.send_photo(chat_id=int(fallback_cid), photo=bio, caption=fb_cap)
                            sent_to.append(fallback_cid)
                            matched_titles.append("Fallback Chat")
                            sent_records.append({"chat_id": int(fallback_cid), "message_id": sent_msg.message_id})
                            used_fallback = True
                        except TelegramError as exc:
                            logger.error("Fallback send gagal: %s", exc)

                    deleted_prev = 0
                    wo_key = extract_wo_key(wonum, final_caption)
                    if sent_records and wo_key:
                        deleted_prev = await replace_previous_wo_messages(
                            application.bot,
                            application.bot_data["config"].schedules_file.parent,
                            wo_key,
                            sent_records,
                        )

                    return {
                        "ok": len(sent_to) > 0,
                        "sent_to": sent_to,
                        "matched_titles": matched_titles,
                        "fallback": used_fallback,
                        "deleted_previous": deleted_prev,
                    }

                fut = asyncio.run_coroutine_threadsafe(_dispatch_async(), loop)
                res = fut.result(timeout=210 if is_standalone_wfm else 30)
                self._send_json(200 if res.get("ok") else 502, res)
            except Exception as exc:
                logger.exception("Error di /api/ading/dispatch")
                self._send_json(500, {"ok": False, "error": str(exc)})

    def _serve() -> None:
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), AdingBridgeHandler)
            logger.info("ADING Auto-Discovery HTTP Bridge aktif di port %s", port)
            server.serve_forever()
        except Exception as exc:
            logger.warning("Gagal menjalankan ADING HTTP Bridge di port %s: %s", port, exc)

    threading.Thread(target=_serve, daemon=True).start()


# ==================== WO ANTI-SPAM (AUTO-DELETE DUPLICATE) ====================


def extract_wo_key(wonum: str, caption: str = "") -> str:
    """Ambil kunci WONUM unik (contoh: WO069272106) untuk anti-spam auto-delete kiriman lama."""
    clean = (wonum or "").strip().upper()
    if clean:
        return clean
    m = re.search(r"\bWO\d{6,}\b", (caption or "").upper())
    return m.group(0) if m else ""


async def replace_previous_wo_messages(
    bot: Any,
    state_dir: Path,
    wo_key: str,
    new_sent: list[dict[str, int]],
) -> int:
    """Hapus otomatis kiriman Telegram sebelumnya untuk WONUM yang sama, lalu simpan message_id terbaru."""
    if not wo_key or not new_sent:
        return 0
    store_path = state_dir / "wo_dispatch_messages.json"
    data: dict[str, list[dict[str, int]]] = {}
    if store_path.exists():
        try:
            loaded = json.loads(store_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    new_pairs = {(int(r["chat_id"]), int(r["message_id"])) for r in new_sent}
    prev_list = data.pop(wo_key, [])
    deleted = 0
    if isinstance(prev_list, list):
        for item in prev_list:
            try:
                cid = int(item["chat_id"])
                mid = int(item["message_id"])
            except Exception:
                continue
            if (cid, mid) in new_pairs:
                continue
            try:
                await bot.delete_message(chat_id=cid, message_id=mid)
                deleted += 1
            except TelegramError:
                pass

    data[wo_key] = [{"chat_id": int(r["chat_id"]), "message_id": int(r["message_id"])} for r in new_sent]
    # Batasi maksimal 500 WONUM terakhir agar file tetap ringan
    if len(data) > 500:
        keys_to_drop = list(data.keys())[:-500]
        for k in keys_to_drop:
            data.pop(k, None)
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("Gagal menyimpan wo_dispatch_messages.json: %s", exc)
    return deleted


# ==================== LIFECYCLE & CLI ====================


async def cmd_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Masukkan 6 digit kode OTP untuk WFM FLOW Standalone Worker."""
    if not update.message:
        return
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return
    worker: WfmFlowWorker | None = context.bot_data.get("wfm_worker")
    code = " ".join(context.args or []).strip()
    if not code:
        await update.message.reply_html("Gunakan format: <code>/otp 123456</code>")
        return
    if worker and worker.submit_otp(code):
        await update.message.reply_html("✅ Kode OTP diterima! Melanjutkan proses WFM FLOW...")
    else:
        await update.message.reply_html("ℹ️ Saat ini tidak ada sesi WFM FLOW yang sedang menunggu OTP.")


async def cmd_wo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Standalone ambil SS HD Workorder langsung dari WFM FLOW: /wo WO069272106 [opsional: nama/NIK teknisi]."""
    if not update.message or not update.effective_chat:
        return
    config: BotConfig = context.bot_data["config"]
    if not is_admin(update, config, context.bot_data):
        await deny_access(update)
        return
    args = context.args or []
    if not args:
        await update.message.reply_html(
            "<b>🖥️ WFM FLOW Standalone</b>\n\n"
            "Gunakan:\n"
            "• <code>/wo WO069272106</code> — ambil SS HD &amp; kirim ke chat ini\n"
            "• <code>/wo WO069272106 ARIFIN</code> — ambil SS HD &amp; kirim otomatis ke channel teknisi"
        )
        return
    wonum = args[0].strip().upper()
    tech_query = " ".join(args[1:]).strip()
    worker: WfmFlowWorker = context.bot_data["wfm_worker"]
    chat_id = update.effective_chat.id

    status_msg = await update.message.reply_html(f"⏳ Mengambil SS HD <b>{html.escape(wonum)}</b> dari WFM FLOW...")

    async def _notify_otp(msg_html: str) -> None:
        await context.bot.send_message(chat_id=chat_id, text=msg_html, parse_mode="HTML")

    async def _run_wo_task() -> None:
        try:
            card = await worker.capture_workorder(wonum, _notify_otp)
            caption = build_flow_summary_caption(wonum, card)
            bio = io.BytesIO(card["png_bytes"])
            bio.name = f"{wonum}.png"

            if tech_query:
                known_groups = context.bot_data.get("known_groups", {})
                matched = find_matching_technician_channels(known_groups, tech_query, tech_query, tech_query)
                if matched:
                    sent_titles: list[str] = []
                    sent_records: list[dict[str, int]] = []
                    for cid_str, title in matched:
                        b2 = io.BytesIO(card["png_bytes"])
                        b2.name = f"{wonum}.png"
                        sent_msg = await context.bot.send_photo(chat_id=int(cid_str), photo=b2, caption=caption)
                        sent_titles.append(title)
                        sent_records.append({"chat_id": int(cid_str), "message_id": sent_msg.message_id})
                    await replace_previous_wo_messages(
                        context.bot,
                        config.schedules_file.parent,
                        wonum,
                        sent_records,
                    )
                    await status_msg.edit_text(
                        f"✅ SS HD <b>{html.escape(wonum)}</b> berhasil dikirim ke channel: <b>{html.escape(', '.join(sent_titles))}</b>!",
                        parse_mode="HTML",
                    )
                    return

            sent_msg = await context.bot.send_photo(chat_id=chat_id, photo=bio, caption=caption)
            await replace_previous_wo_messages(
                context.bot,
                config.schedules_file.parent,
                wonum,
                [{"chat_id": chat_id, "message_id": sent_msg.message_id}],
            )
            await status_msg.delete()
        except Exception as exc:
            err_html = f"❌ Gagal mengambil <b>{html.escape(wonum)}</b>:\n<code>{html.escape(str(exc))}</code>"
            try:
                await status_msg.delete()
            except TelegramError:
                pass
            await context.bot.send_message(chat_id=chat_id, text=err_html, parse_mode="HTML")

    asyncio.create_task(_run_wo_task())


async def post_init(application: Application) -> None:
    config: BotConfig = application.bot_data["config"]
    application.bot_data["wfm_worker"] = WfmFlowWorker(config.schedules_file.parent)

    async def _on_wfm_disconnect(msg_html: str) -> None:
        for adm in (config.admin_ids or {217411824}):
            try:
                await application.bot.send_message(chat_id=adm, text=msg_html, parse_mode="HTML")
            except TelegramError:
                pass

    asyncio.create_task(
        application.bot_data["wfm_worker"].start_keep_alive_loop(600, on_disconnect_cb=_on_wfm_disconnect)
    )
    start_ading_http_bridge(application, asyncio.get_running_loop())
    ensure_schedules_file(config.schedules_file)
    jobs = load_schedules(config.schedules_file)
    default_divisions = {
        "custom_20261123_1319": "assurance",
        "custom_20261123_1438": "provisioning",
        "custom_20261123_1731": "maintenance",
        "custom_20261125_1730": "maintenance",
        "custom_20261125_1737": "maintenance",
    }
    divisions_migrated = False
    for job in jobs:
        if not job.division and job.id in default_divisions:
            job.division = default_divisions[job.id]
            divisions_migrated = True
    if divisions_migrated:
        save_schedules(config.schedules_file, jobs)
    extra_admins = load_extra_admins(config.resolved_admins_file)
    known_groups = load_known_groups(config.resolved_groups_file)
    known_members = load_known_members(config.resolved_members_file)
    tag_presets = load_tag_presets(config.resolved_presets_file)

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
    register_jobs(scheduler, application.bot, jobs, config, known_groups, tag_presets)
    scheduler.start()

    application.bot_data["scheduler"] = scheduler
    application.bot_data["jobs"] = jobs
    application.bot_data["extra_admins"] = extra_admins
    application.bot_data["known_groups"] = known_groups
    application.bot_data["known_members"] = known_members
    application.bot_data["tag_presets"] = tag_presets

    await application.bot.set_my_commands(
        [
            BotCommand("menu", "Buka menu utama interaktif"),
            BotCommand("targets", "Kelola ID Grup/User/Channel"),
            BotCommand("presets", "Kelola preset tim/shift tag"),
            BotCommand("history", "Lihat riwayat pengiriman"),
            BotCommand("backup", "Backup file konfigurasi"),
            BotCommand("restore", "Restore file JSON backup"),
            BotCommand("admin", "Kelola daftar admin bot"),
            BotCommand("schedule", "Buat jadwal pesan cepat"),
            BotCommand("list", "Lihat daftar jadwal"),
            BotCommand("chatid", "Cek Chat ID saat ini"),
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
            CallbackQueryHandler(preset_new_start, pattern=r"^preset:new$"),
            CallbackQueryHandler(restore_start, pattern=r"^backup:restore$"),
            CommandHandler("restore", restore_start),
        ],
        states={
            ASK_TARGET: [
                CallbackQueryHandler(wiz_target_toggle, pattern=r"^wiztgt:toggle:.+$"),
                CallbackQueryHandler(wiz_target_done, pattern=r"^wiztgt:done$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_target_text),
            ],
            ASK_DATETIME: [
                CallbackQueryHandler(wiz_time_mode, pattern=r"^time:mode:.+$"),
                CallbackQueryHandler(wiz_time_day_action, pattern=r"^time:day:.+$"),
                CallbackQueryHandler(wiz_time_day_apply, pattern=r"^time:dayapply:.+$"),
                CallbackQueryHandler(wiz_datetime_quick, pattern=r"^time:quick:.+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_datetime_text),
            ],
            ASK_MESSAGE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO | (filters.TEXT & ~filters.COMMAND), wiz_message_text),
            ],
            ASK_MENTIONS: [
                CallbackQueryHandler(wiz_mentions_preset, pattern=r"^wiztag:p:\d+$"),
                CallbackQueryHandler(wiz_mentions_toggle, pattern=r"^wiztag:t:\d+$"),
                CallbackQueryHandler(wiz_mentions_done, pattern=r"^wiztag:done$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_mentions_text),
            ],
            CONFIRM_SAVE: [
                CallbackQueryHandler(wiz_confirm_test_self, pattern=r"^wiz:testself$"),
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
                CallbackQueryHandler(edit_ask_hours, pattern=r"^editact:hours$"),
                CallbackQueryHandler(edit_ask_thread, pattern=r"^editact:thread$"),
                CallbackQueryHandler(edit_toggle_autodel, pattern=r"^editact:autodel$"),
                CallbackQueryHandler(edit_toggle_ack, pattern=r"^editact:ack$"),
                CallbackQueryHandler(edit_duplicate_job, pattern=r"^editact:dup$"),
                CallbackQueryHandler(edit_toggle_enabled, pattern=r"^editact:toggle$"),
                CallbackQueryHandler(edit_set_division, pattern=r"^editdiv:(provisioning|assurance|maintenance)$"),
            ],
            EDIT_NEW_MESSAGE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO | (filters.TEXT & ~filters.COMMAND), edit_save_message),
            ],
            EDIT_NEW_TIME: [
                CallbackQueryHandler(edit_time_mode, pattern=r"^time:mode:.+$"),
                CallbackQueryHandler(edit_time_day_action, pattern=r"^time:day:.+$"),
                CallbackQueryHandler(edit_time_day_apply, pattern=r"^time:dayapply:.+$"),
                CallbackQueryHandler(edit_save_time_quick, pattern=r"^time:quick:.+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_save_time_text),
            ],
            EDIT_NEW_TARGET: [
                CallbackQueryHandler(edit_target_toggle, pattern=r"^edittgt:toggle:.+$"),
                CallbackQueryHandler(edit_target_done, pattern=r"^edittgt:done$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_save_target_text),
            ],
            EDIT_NEW_MENTIONS: [
                CallbackQueryHandler(edit_mentions_toggle, pattern=r"^edittag:t:\d+$"),
                CallbackQueryHandler(edit_mentions_done, pattern=r"^edittag:done$"),
                CallbackQueryHandler(edit_clear_mentions, pattern=r"^edittag:clear$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_save_mentions_text),
            ],
            EDIT_NEW_HOURS: [
                CallbackQueryHandler(edit_save_hours_quick, pattern=r"^hours:q:.+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_save_hours_text),
            ],
            EDIT_NEW_THREAD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_save_thread),
            ],
            ADMIN_ADD_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_save),
            ],
            TARGET_ADD_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, target_add_save),
            ],
            PRESET_ASK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, preset_name_text),
            ],
            PRESET_PICK_MEMBERS: [
                CallbackQueryHandler(preset_pick_toggle, pattern=r"^prpick:t:\d+$"),
                CallbackQueryHandler(preset_pick_done, pattern=r"^prpick:done$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, preset_pick_text),
            ],
            RESTORE_UPLOAD: [
                MessageHandler(filters.Document.ALL | (filters.TEXT & ~filters.COMMAND), restore_receive_file),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(wiz_cancel, pattern=r"^wiz:cancel$"),
            CommandHandler("cancel", wiz_cancel),
            CommandHandler(["start", "help", "menu"], wiz_fallback_menu),
            CallbackQueryHandler(wiz_fallback_callback, pattern=r"^(menu:|job:|admin:del:|tgtdel:|tgtrefresh|presetdel:)"),
        ],
        allow_reentry=True,
        per_message=False,
    )

    # Passive tracker grup/channel agar daftar grup selalu up-to-date otomatis
    application.add_handler(ChatMemberHandler(track_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER), group=-1)
    application.add_handler(MessageHandler(filters.ChatType.GROUPS | filters.ChatType.CHANNEL, track_group_activity), group=-1)

    application.add_handler(wizard_handler)
    application.add_handler(CommandHandler(["start", "help", "menu"], cmd_menu))
    application.add_handler(CommandHandler(["targets", "target"], cmd_targets))
    application.add_handler(CommandHandler(["presets", "preset"], cmd_presets))
    application.add_handler(CommandHandler(["history", "riwayat"], cmd_history))
    application.add_handler(CommandHandler("backup", cmd_backup))
    application.add_handler(CommandHandler(["admin", "admins"], cmd_admins))
    application.add_handler(CommandHandler("chatid", cmd_chatid))
    application.add_handler(CommandHandler(["schedule", "jadwalkan"], cmd_schedule))
    application.add_handler(CommandHandler("setpesan", cmd_setpesan))
    application.add_handler(CommandHandler(["delete", "hapus"], cmd_delete))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("test", cmd_test))
    application.add_handler(CommandHandler("reload", cmd_reload))
    application.add_handler(CommandHandler("otp", cmd_otp))
    application.add_handler(CommandHandler("wo", cmd_wo))
    application.add_handler(CallbackQueryHandler(handle_menu_callback))

    logger.info("Menjalankan Telegram AutoSender Bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
