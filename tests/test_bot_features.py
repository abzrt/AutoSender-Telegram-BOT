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


def test_dynamic_placeholders_and_job_fields_persistence(tmp_path) -> None:
    from datetime import datetime
    from config import render_dynamic_placeholders

    tz = ZoneInfo("Asia/Makassar")
    fixed_dt = datetime(2026, 11, 26, 14, 30, 5, tzinfo=tz)  # Kamis
    out = render_dynamic_placeholders(
        "Laporan {hari}, {tanggal} ({tanggal_panjang}) jam {jam} | {bulan} {tahun} | {unknown}",
        now=fixed_dt,
    )
    assert out == "Laporan Kamis, 26/11/2026 (26 November 2026) jam 14:30 | November 2026 | {unknown}"

    out_file = tmp_path / "schedules.json"
    job = ScheduleJob(
        id="doc_topic",
        name="SOP PDF",
        trigger="interval",
        hours=2,
        chat_ids=[-100123456],
        message="",
        document="BQACAgUAAxkBAAI_fake_doc_id",
        thread_id=42,
        auto_delete_prev=True,
        require_ack=True,
    )
    job.validate()
    save_schedules(out_file, [job])
    reloaded = load_schedules(out_file)
    assert len(reloaded) == 1
    assert reloaded[0].document == "BQACAgUAAxkBAAI_fake_doc_id"
    assert reloaded[0].thread_id == 42
    assert reloaded[0].auto_delete_prev is True
    assert reloaded[0].require_ack is True


@pytest.mark.anyio
async def test_antispam_ack_retry_admin_alert_document_and_thread(tmp_path) -> None:
    from bot import handle_menu_callback
    from config import load_runtime_state

    cfg = BotConfig(bot_token="dummy", admin_ids={217411824}, schedules_file=tmp_path / "schedules.json")
    fake_bot = AsyncMock()
    fake_bot.send_document.side_effect = [
        SimpleNamespace(message_id=101),
        SimpleNamespace(message_id=102),
    ]

    job = ScheduleJob(
        id="sop_1",
        name="Pengingat SOP",
        trigger="interval",
        minutes=30,
        chat_ids=[-100999],
        message="Update tiket hari {hari}",
        document="DOC_FILE_ID",
        thread_id=77,
        auto_delete_prev=True,
        require_ack=True,
    )

    # Kiriman pertama: simpan message_id=101, kirim ke topic 77, ada tombol ack
    ok1, fail1 = await send_scheduled_message(fake_bot, job, config=cfg)
    assert (ok1, fail1) == (1, 0)
    kwargs1 = fake_bot.send_document.call_args_list[0].kwargs
    assert kwargs1["message_thread_id"] == 77
    assert kwargs1["document"] == "DOC_FILE_ID"
    assert "{hari}" not in kwargs1["caption"]
    ack_btn = kwargs1["reply_markup"].inline_keyboard[0][0]
    assert ack_btn.callback_data == "ack:done:sop_1"

    # Kiriman kedua: hapus pesan sebelumnya (101) lalu simpan 102
    ok2, fail2 = await send_scheduled_message(fake_bot, job, config=cfg)
    assert (ok2, fail2) == (1, 0)
    fake_bot.delete_message.assert_awaited_once_with(chat_id=-100999, message_id=101)
    assert load_runtime_state(cfg.resolved_state_file)["last_messages"]["sop_1"]["-100999"] == 102

    # Anggota grup (non-admin) klik tombol "✅ Sudah Dikerjakan"
    fake_query = SimpleNamespace(
        data="ack:done:sop_1",
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
        message=None,
    )
    fake_update = SimpleNamespace(
        callback_query=fake_query,
        effective_user=SimpleNamespace(id=555555, first_name="Nasir", last_name=None, username=None, full_name="Nasir"),
        effective_chat=SimpleNamespace(id=-100999, type="supergroup"),
    )
    fake_ctx = SimpleNamespace(bot_data={"config": cfg, "scheduler": SimpleNamespace(get_jobs=lambda: [])}, bot=fake_bot)
    await handle_menu_callback(fake_update, fake_ctx)
    fake_query.edit_message_reply_markup.assert_awaited_once()

    # Kiriman ketiga di hari yang sama dilewati karena sudah ack
    ok3, fail3 = await send_scheduled_message(fake_bot, job, config=cfg)
    assert (ok3, fail3) == (0, 0)

    # Auto-retry sukses pada percobaan ke-2
    retry_bot = AsyncMock()
    retry_bot.send_message.side_effect = [TelegramError("Temporary timeout"), SimpleNamespace(message_id=200)]
    simple_job = ScheduleJob(id="r1", name="Retry Test", trigger="interval", minutes=10, chat_ids=[-100888], message="Halo")
    ok_r, fail_r = await send_scheduled_message(retry_bot, simple_job, config=cfg, max_retries=2, retry_delay=0.0)
    assert (ok_r, fail_r) == (1, 0)
    assert retry_bot.send_message.await_count == 2

    # Jika semua retry gagal -> kirim notifikasi error ke admin (217411824)
    fail_bot = AsyncMock()
    fail_bot.send_message.side_effect = [
        TelegramError("Bot kicked"),
        TelegramError("Bot kicked"),
        SimpleNamespace(message_id=300),  # alert ke admin
    ]
    ok_f, fail_f = await send_scheduled_message(fail_bot, simple_job, config=cfg, max_retries=2, retry_delay=0.0)
    assert (ok_f, fail_f) == (0, 1)
    assert fail_bot.send_message.await_count == 3
    assert fail_bot.send_message.call_args_list[2].kwargs["chat_id"] == 217411824
    assert "Gagal Kirim Jadwal" in fail_bot.send_message.call_args_list[2].kwargs["text"]


def test_duplicate_cleanup_and_restore_backup(tmp_path) -> None:
    import json
    from datetime import datetime
    from bot import (
        duplicate_schedule_job,
        format_jobs_list_and_keyboard,
        purge_expired_date_jobs,
        restore_backup_file,
    )

    tz = ZoneInfo("Asia/Makassar")
    cfg = BotConfig(bot_token="dummy", schedules_file=tmp_path / "schedules.json")
    past_job = ScheduleJob(id="old_1", name="Lama", trigger="date", run_at="2025-01-01 08:00:00", chat_ids=[123], message="Lama")
    future_job = ScheduleJob(id="fut_1", name="Masa Depan", trigger="date", run_at="2027-01-01 08:00:00", chat_ids=[123], message="Nanti")
    active_job = ScheduleJob(id="int_1", name="Berkala", trigger="interval", minutes=30, chat_ids=[123], message="Rutin", thread_id=9, auto_delete_prev=True)

    # 1. Cleanup jadwal kadaluarsa
    kept, removed = purge_expired_date_jobs([past_job, future_job, active_job], tz, now=datetime(2026, 11, 26, 14, 0, tzinfo=tz))
    assert [j.id for j in removed] == ["old_1"]
    assert [j.id for j in kept] == ["fut_1", "int_1"]

    # Tombol cleanup muncul jika ada jadwal date kadaluarsa
    fake_scheduler = SimpleNamespace(get_jobs=lambda: [], remove_all_jobs=lambda: None, add_job=lambda *a, **kw: None)
    _, markup = format_jobs_list_and_keyboard(fake_scheduler, [past_job, active_job])
    cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "job:cleanup" in cbs

    # 2. Duplikat jadwal 1 klik
    bot_data = {"config": cfg, "jobs": [active_job], "scheduler": fake_scheduler, "known_groups": {}, "tag_presets": {}}
    fake_ctx = SimpleNamespace(bot_data=bot_data, bot=None)
    cloned = duplicate_schedule_job(fake_ctx, "int_1")
    assert cloned is not None
    assert cloned.id == "int_1_copy"
    assert cloned.enabled is False
    assert cloned.thread_id == 9
    assert cloned.auto_delete_prev is True
    assert len(load_schedules(cfg.schedules_file)) == 2

    # 3. Restore backup JSON (schedules, groups, admins, presets)
    sched_payload = json.dumps([active_job.to_dict()]).encode("utf-8")
    msg_s = restore_backup_file(cfg, "schedules.json", sched_payload, bot_data)
    assert "1 jadwal" in msg_s
    assert len(bot_data["jobs"]) == 1

    groups_payload = json.dumps({"-1001": {"title": "Grup QC", "type": "supergroup"}}).encode("utf-8")
    msg_g = restore_backup_file(cfg, "groups.json", groups_payload, bot_data)
    assert "1 target" in msg_g
    assert "-1001" in bot_data["known_groups"]

    admins_payload = json.dumps([111, 222]).encode("utf-8")
    msg_a = restore_backup_file(cfg, "admins.json", admins_payload, bot_data)
    assert "2 admin" in msg_a
    assert bot_data["extra_admins"] == {111, 222}

    presets_payload = json.dumps({"Shift Malam": ["@budi"]}).encode("utf-8")
    msg_p = restore_backup_file(cfg, "presets.json", presets_payload, bot_data)
    assert "1 preset" in msg_p
    assert bot_data["tag_presets"] == {"Shift Malam": ["@budi"]}

    with pytest.raises(ValueError, match="tidak didukung"):
        restore_backup_file(cfg, "random.txt", b"{}", bot_data)



@pytest.mark.anyio
async def test_wo_anti_spam_deletes_previous_message(tmp_path) -> None:
    from bot import extract_wo_key, replace_previous_wo_messages

    assert extract_wo_key("wo069272106") == "WO069272106"
    assert extract_wo_key("", "NO SC: 123\nWONUM: WO069325687\nALAMAT: Test") == "WO069325687"
    assert extract_wo_key("AOi4261128103031812876ff0", "NO SC: AOi4261128103031812876ff0\nWONUM: WO069339598") == "WO069339598"

    fake_bot = AsyncMock()
    # Kiriman pertama: tidak ada yang dihapus
    deleted1 = await replace_previous_wo_messages(
        fake_bot,
        tmp_path,
        "WO069272106",
        [{"chat_id": -1002202217652, "message_id": 101}],
    )
    assert deleted1 == 0
    fake_bot.delete_message.assert_not_called()

    # Kiriman kedua untuk WONUM yang sama: pesan lama (101) otomatis dihapus
    deleted2 = await replace_previous_wo_messages(
        fake_bot,
        tmp_path,
        "WO069272106",
        [{"chat_id": -1002202217652, "message_id": 202}],
    )
    assert deleted2 == 1
    fake_bot.delete_message.assert_awaited_once_with(chat_id=-1002202217652, message_id=101)


def test_format_dispatch_caption_and_markup() -> None:
    from bot import format_dispatch_caption_and_markup

    raw = (
        "NO SC: DGPS261128103031345874681-AOi4261128103031812876ff0_116190712-914518116~2026112810410846563836~3~WSA\n"
        "WONUM: WO069308951\n"
        "NO INTERNET: 161502308344\n"
        "CUSTOMER NAME: Yordan Yusak\n"
        "CONTACT NUMBER: 081228103431\n"
        "ALAMAT: Jl. Sei Fatimah Nunukan\n"
        "ODP: ODP-NNK-FL/012\n"
        "KORDINAT: 4.111723, 117.62325"
    )
    html_cap, markup = format_dispatch_caption_and_markup(raw)
    assert "NO SC: <code>AOi4261128103031812876ff0</code>" in html_cap
    assert "<code>WO069308951</code>" in html_cap
    assert "+6281228103431" in html_cap
    assert 'href="https://wa.me/6281228103431"' in html_cap
    assert 'href="https://maps.google.com/?q=4.111723,117.62325"' in html_cap
    assert markup is not None
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert any(b.url == "https://wa.me/6281228103431" for b in buttons)
    assert any(b.url == "https://maps.google.com/?q=4.111723,117.62325" for b in buttons)
    copy_btns = [b for b in buttons if b.copy_text is not None]
    assert len(copy_btns) == 1
    assert len(copy_btns[0].copy_text.text) <= 250


def test_extract_otp_from_hcm_message() -> None:
    from wfm_worker import extract_otp_from_message_text

    assert extract_otp_from_message_text("Kode OTP login FLOW Anda: 482910. Berlaku 3 menit.") == "482910"
    assert extract_otp_from_message_text("NIK 16830482 meminta OTP: 903112") == "903112"
    assert extract_otp_from_message_text("Token akses: 123456") == "123456"
    assert extract_otp_from_message_text("Pesan tanpa kode") == ""


def test_psb_daily_list_auto_record_and_redispatch(tmp_path) -> None:
    from datetime import timezone, timedelta
    from bot import format_psb_daily_list, record_psb_daily_dispatch, resolve_psb_tech_label

    wita = timezone(timedelta(hours=8))
    assert resolve_psb_tech_label(["ARIFIN - 16985859"], "", "") == "ARIFIN"
    assert resolve_psb_tech_label(["AHMAD SYAHRIZAL RAMADHANI - 16013401"], "", "") == "DHANI"
    assert resolve_psb_tech_label(["ANJELLO"], "", "") == "ANJELO"

    record_psb_daily_dispatch(tmp_path, wita, "ARIFIN", "WO069308951", "NANI SATRIANI", date_key="2026-11-17")
    txt1 = format_psb_daily_list(tmp_path, wita, date_key="2026-11-17")
    assert txt1.startswith("LIST PSB NNK SNY 17/11/2026\n\nARIFIN\n1. NANI SATRIANI\n2. ")
    assert "\n\nRISQI\n1. \n2. " in txt1
    assert "\n\nDHANI\n1. \n2. " in txt1
    assert "\n\nALIF\n1. \n2. " in txt1

    # Re-dispatch WONUM yang sama ke DHANI: otomatis pindah dari ARIFIN ke DHANI tanpa duplikat
    record_psb_daily_dispatch(tmp_path, wita, "DHANI", "WO069308951", "NANI SATRIANI", date_key="2026-11-17")
    txt2 = format_psb_daily_list(tmp_path, wita, date_key="2026-11-17")
    assert "ARIFIN\n1. \n2. " in txt2
    assert "DHANI\n1. NANI SATRIANI\n2. " in txt2


def test_parse_assurance_closed_message() -> None:
    from bot import parse_assurance_closed_message

    sample = (
        "LIST WO 🎯 A6NNK2 (CLOSED HARI INI)\n\n"
        "• OHI [Non SLA]\n"
        "  1. INC55411910 | 162505301549 | 28/11 02:42 | 28/11 09:02\n"
        "     ODP-NNK-FJ/033\n"
        "• SQM [Non SLA]\n"
        "  1. INC55400267 | 162505211749 | 27/11 20:05 | 28/11 11:21\n"
        "     ODP-NNK-FAG/046\n"
        "• PL-TSEL / INDIHOME\n"
        "  • Gold [12 Jam]\n"
        "    1. INC55397441 | 162505207255 | 27/11 17:28 | 28/11 10:50 | 👎\n"
        "       ODP-NNK-FM/059\n"
        "    2. INC55428957 | 162505210713 | 28/11 08:55 | 28/11 10:00 | 👍\n"
        "       ODP-NNK-FAF/021\n"
        "TOTAL CLOSED HARI INI : 4"
    )
    parsed = parse_assurance_closed_message(sample)
    assert parsed["reported_total"] == 4
    assert len(parsed["tickets"]) == 4
    assert len(parsed["categories"]["OHI [Non SLA]"]) == 1
    assert len(parsed["categories"]["SQM [Non SLA]"]) == 1
    gold = parsed["categories"]["PL-TSEL / INDIHOME — Gold [12 Jam]"]
    assert len(gold) == 2
    assert gold[0]["sla"] == "out" and gold[0]["odp"] == "ODP-NNK-FM/059"
    assert gold[1]["sla"] == "in" and gold[1]["odp"] == "ODP-NNK-FAF/021"
