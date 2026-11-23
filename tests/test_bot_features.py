"""Pengujian end-to-end untuk seluruh fitur Telegram AutoSender Bot."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from telegram.error import ChatMigrated, TelegramError

from bot import (
    build_mentions_picker_keyboard,
    build_target_picker_keyboard,
    collect_mention_candidates,
    format_admins_menu,
    format_targets_menu,
    is_admin,
    remember_group,
    remember_manual_mentions,
    resolve_chat_target,
    send_scheduled_message,
)
from config import (
    BotConfig,
    ScheduleJob,
    ensure_schedules_file,
    load_schedules,
    parse_flexible_schedule,
    render_message_with_mentions,
    save_schedules,
)


def _make_update(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id))


def test_wita_timezone_default() -> None:
    cfg = BotConfig(bot_token="dummy")
    assert cfg.timezone == "Asia/Makassar"
    assert cfg.tzinfo == ZoneInfo("Asia/Makassar")
    from datetime import datetime

    assert datetime.now(cfg.tzinfo).strftime("%Z") == "WITA"


def test_admin_access_control() -> None:
    cfg = BotConfig(bot_token="dummy", admin_ids={217411824})
    bot_data = {"extra_admins": {172640153}}

    # Owner & extra admin diizinkan
    assert is_admin(_make_update(217411824), cfg, bot_data) is True
    assert is_admin(_make_update(172640153), cfg, bot_data) is True
    # Orang asing ditolak
    assert is_admin(_make_update(999999999), cfg, bot_data) is False

    # Owner tidak punya tombol hapus, admin tambahan punya
    _, markup = format_admins_menu(cfg, {172640153})
    cbs = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "admin:del:172640153" in cbs
    assert "admin:del:217411824" not in cbs


def test_ensure_schedules_file_on_empty_volume(tmp_path) -> None:
    target = tmp_path / "data" / "schedules.json"
    seed = tmp_path / "seed.json"
    seed.write_text("[]\n", encoding="utf-8")

    created = ensure_schedules_file(target, seed_path=seed)
    assert created.exists()
    assert load_schedules(created) == []


@pytest.mark.anyio
async def test_resolve_chat_target_migration_and_supergroup_fallback() -> None:
    # 1. Kasus migrasi grup ke supergroup
    bot_migrated = AsyncMock()
    bot_migrated.get_chat.side_effect = [
        ChatMigrated(-1003977827975),
        SimpleNamespace(id=-1003977827975, title="QC Assurance", type="supergroup"),
    ]
    fid, title, rtype, ok = await resolve_chat_target(bot_migrated, -608483981, "group")
    assert (fid, title, rtype, ok) == (-1003977827975, "QC Assurance", "supergroup", True)

    # 2. Kasus ID grup tanpa awalan -100 -> fallback ke -100...
    bot_fallback = AsyncMock()
    bot_fallback.get_chat.side_effect = [
        TelegramError("Chat not found"),
        SimpleNamespace(id=-100999888, title="Grup Operasi", type="supergroup"),
    ]
    fid2, title2, rtype2, ok2 = await resolve_chat_target(bot_fallback, -999888, "group")
    assert (fid2, title2, rtype2, ok2) == (-100999888, "Grup Operasi", "supergroup", True)


@pytest.mark.anyio
async def test_collect_mention_candidates_only_saved_users(tmp_path) -> None:
    cfg = BotConfig(bot_token="dummy", schedules_file=tmp_path / "schedules.json")
    bot_data = {
        "config": cfg,
        "known_groups": {
            "-1003977827975": {"title": "QC Assurance", "type": "supergroup"},
            "172640153": {"title": "Nasir", "type": "user"},
            "@budi_qc": {"title": "Budi", "type": "user"},
        },
        "known_members": {
            # Anggota grup acak TIDAK boleh muncul lagi
            "-1003977827975": {
                "555": {"label": "Orang Asing", "token": "@asing"}
            },
            "_saved": {},
        },
    }
    remember_manual_mentions(bot_data, ["@ani_manual"])

    fake_bot = AsyncMock()
    candidates = await collect_mention_candidates(fake_bot, bot_data, [-1003977827975])
    tokens = {c["token"] for c in candidates}

    assert "Nasir:172640153" in tokens
    assert "@budi_qc" in tokens
    assert "@ani_manual" in tokens
    assert "@asing" not in tokens
    fake_bot.get_chat_administrators.assert_not_called()


@pytest.mark.anyio
async def test_send_scheduled_message_renders_mentions_below() -> None:
    fake_bot = AsyncMock()
    saved_targets = {
        "172640153": {"title": "Nasir", "type": "user"},
        "@budi_qc": {"title": "Budi", "type": "user"},
    }
    job = ScheduleJob(
        id="uji_1",
        name="Uji Kirim",
        trigger="interval",
        minutes=30,
        chat_ids=[-1003977827975],
        message="JANGAN LUPA UPDATE VALINS @Nasir!",
        mentions=["@budi_qc"],
        parse_mode="HTML",
    )

    ok, fail = await send_scheduled_message(fake_bot, job, saved_targets=saved_targets)
    assert (ok, fail) == (1, 0)

    sent_kwargs = fake_bot.send_message.call_args.kwargs
    assert sent_kwargs["chat_id"] == -1003977827975
    assert sent_kwargs["text"] == 'JANGAN LUPA UPDATE VALINS <a href="tg://user?id=172640153">Nasir</a>!\n\n@budi_qc'


def test_full_target_and_schedule_persistence(tmp_path) -> None:
    cfg = BotConfig(bot_token="dummy", schedules_file=tmp_path / "schedules.json")
    bot_data = {"config": cfg, "known_groups": {}}
    remember_group(bot_data, -1003977827975, "QC Assurance", "supergroup")
    remember_group(bot_data, 172640153, "Nasir", "user")

    text, markup = format_targets_menu(bot_data["known_groups"])
    assert "QC Assurance" in text
    assert "Nasir" in text
    cbs = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "tgtrefresh" in cbs

    picker = build_target_picker_keyboard(217411824, bot_data["known_groups"], [-1003977827975])
    picker_cbs = [btn.callback_data for row in picker.inline_keyboard for btn in row]
    assert "wiztgt:toggle:-1003977827975" in picker_cbs
    assert "wiztgt:done" in picker_cbs

    spec = parse_flexible_schedule("setiap 30 menit", cfg.tzinfo)
    job = ScheduleJob(
        id="j1",
        name="Test",
        trigger=spec["trigger"],
        minutes=spec["minutes"],
        chat_ids=[-1003977827975],
        message="Halo @all",
        mentions=["Nasir:172640153"],
    )
    save_schedules(cfg.schedules_file, [job])
    reloaded = load_schedules(cfg.schedules_file)
    assert len(reloaded) == 1
    assert reloaded[0].mentions == ["Nasir:172640153"]

    rendered = render_message_with_mentions(reloaded[0].message, reloaded[0].mentions, "HTML", bot_data["known_groups"])
    assert rendered.startswith('Halo <a href="tg://user?id=172640153">Nasir</a>')
    assert rendered.endswith('\n\n<a href="tg://user?id=172640153">Nasir</a>')

    m_picker = build_mentions_picker_keyboard([{"label": "Nasir", "token": "Nasir:172640153"}], ["Nasir:172640153"])
    assert any(btn.callback_data == "wiztag:done" for row in m_picker.inline_keyboard for btn in row)


def test_active_hours_window_parsing_and_checking() -> None:
    from datetime import datetime
    from config import format_active_window, is_within_active_window, parse_active_window

    ah, ad = parse_active_window("08:00-17:00 sen-jum")
    assert ah == "08:00-17:00"
    assert ad == [1, 2, 3, 4, 5]
    assert format_active_window(ah, ad) == "08:00-17:00 (Sen–Jum)"

    job = ScheduleJob(
        id="work",
        name="Jam Kerja",
        trigger="interval",
        minutes=30,
        chat_ids=[123],
        message="Halo",
        active_hours=ah,
        active_days=ad,
    )
    tz = ZoneInfo("Asia/Makassar")
    # Senin jam 10:00 WITA -> masuk jam kerja
    assert is_within_active_window(job, datetime(2026, 11, 23, 10, 0, tzinfo=tz)) is True
    # Senin jam 22:00 WITA -> di luar jam kerja
    assert is_within_active_window(job, datetime(2026, 11, 23, 22, 0, tzinfo=tz)) is False
    # Minggu jam 10:00 WITA -> di luar hari kerja
    assert is_within_active_window(job, datetime(2026, 11, 29, 10, 0, tzinfo=tz)) is False

    # Reset ke 24 jam
    assert parse_active_window("24 jam") == (None, None)


def test_presets_history_and_photo_job(tmp_path) -> None:
    from bot import format_history_menu, format_presets_menu
    from config import (
        append_delivery_history,
        load_delivery_history,
        load_tag_presets,
        save_tag_presets,
    )

    # Preset Tim
    presets_file = tmp_path / "presets.json"
    presets = {"Shift Pagi": ["Nasir:172640153", "@budi_qc"]}
    save_tag_presets(presets_file, presets)
    loaded_presets = load_tag_presets(presets_file)
    assert loaded_presets == presets

    ptext, pmarkup = format_presets_menu(loaded_presets)
    assert "Shift Pagi" in ptext
    assert any(btn.callback_data == "preset:new" for row in pmarkup.inline_keyboard for btn in row)

    # Auto-mention @ShiftPagi di pesan
    rendered = render_message_with_mentions("Cek update @ShiftPagi!", parse_mode="HTML", tag_presets=loaded_presets)
    assert '<a href="tg://user?id=172640153">Nasir</a>' in rendered
    assert "@budi_qc" in rendered

    # Riwayat pengiriman
    hist_file = tmp_path / "history.json"
    append_delivery_history(hist_file, {"timestamp": "23/11 14:45 WITA", "job_id": "j1", "chat_id": "-1001", "status": "ok"})
    hist = load_delivery_history(hist_file)
    assert len(hist) == 1
    htext, _ = format_history_menu(hist)
    assert "j1" in htext


@pytest.mark.anyio
async def test_send_photo_and_quiet_hours_skip(tmp_path) -> None:
    cfg = BotConfig(bot_token="dummy", schedules_file=tmp_path / "schedules.json")
    fake_bot = AsyncMock()
    photo_job = ScheduleJob(
        id="foto_1",
        name="Kirim Foto",
        trigger="interval",
        minutes=60,
        chat_ids=[-1003977827975],
        message="SOP terbaru",
        photo="AgACAgUAAxkBAAI_fake_file_id",
        mentions=["@budi_qc"],
    )
    ok, fail = await send_scheduled_message(fake_bot, photo_job, config=cfg, ignore_window=True)
    assert (ok, fail) == (1, 0)
    fake_bot.send_photo.assert_called_once()
    assert fake_bot.send_photo.call_args.kwargs["photo"] == "AgACAgUAAxkBAAI_fake_file_id"
    assert fake_bot.send_photo.call_args.kwargs["caption"] == "SOP terbaru\n\n@budi_qc"


@pytest.mark.anyio
async def test_send_self_preview_message_and_button() -> None:
    from types import SimpleNamespace
    from bot import format_jobs_list_and_keyboard, send_self_preview_message

    fake_bot = AsyncMock()
    fake_bot.get_chat_member.return_value = SimpleNamespace(
        status="member",
        user=SimpleNamespace(id=101, username="agus_ok", first_name="Agus"),
    )
    saved_targets = {
        "101": {"title": "Agus", "type": "user"},
    }
    ok, err, warnings = await send_self_preview_message(
        fake_bot,
        self_chat_id=217411824,
        message="Tes pesan custom",
        mentions=["Agus:101"],
        target_chat_ids=[-1003977827975],
        saved_targets=saved_targets,
    )
    assert ok is True
    assert err is None
    assert warnings == []
    fake_bot.send_message.assert_called_once()
    assert fake_bot.send_message.call_args.kwargs["chat_id"] == 217411824
    assert fake_bot.send_message.call_args.kwargs["text"] == "Tes pesan custom\n\n@agus_ok"

    # Pastikan tombol Tes Diri muncul di daftar jadwal
    fake_scheduler = SimpleNamespace(get_jobs=lambda: [])
    sample_job = ScheduleJob(
        id="custom_1",
        name="Pesan Custom",
        trigger="interval",
        minutes=30,
        chat_ids=[-1003977827975],
        message="Halo",
    )
    _, markup = format_jobs_list_and_keyboard(fake_scheduler, [sample_job])
    cbs = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "job:testself:custom_1" in cbs
    assert "job:test:custom_1" in cbs

