"""Unit tests untuk konfigurasi, menu, dan parser waktu bebas Telegram AutoSender Bot."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bot import (
    build_custom_menu_keyboard,
    build_main_menu_keyboard,
    build_mentions_picker_keyboard,
    build_target_picker_keyboard,
    build_trigger,
    format_targets_menu,
    get_all_admin_ids,
    parse_chat_ids,
    parse_target_entry,
)
from config import (
    BotConfig,
    ScheduleJob,
    extract_mention_user_ids,
    load_config,
    load_extra_admins,
    load_known_groups,
    load_known_members,
    load_schedules,
    parse_flexible_schedule,
    parse_mentions,
    parse_user_ids,
    render_message_with_mentions,
    save_extra_admins,
    save_known_groups,
    save_known_members,
    save_schedules,
)


def test_known_members_and_mentions_picker(tmp_path: Path) -> None:
    members_file = tmp_path / "members.json"
    sample = {
        "-1003977827975": {
            "217411824": {"label": "ABZRT (@abzrt)", "token": "@abzrt"},
            "172640153": {"label": "Nasir", "token": "Nasir:172640153"},
        }
    }
    save_known_members(members_file, sample)
    assert load_known_members(members_file) == sample

    candidates = [
        {"label": "ABZRT (@abzrt)", "token": "@abzrt"},
        {"label": "Nasir", "token": "Nasir:172640153"},
    ]
    markup = build_mentions_picker_keyboard(candidates, ["@abzrt"], prefix="wiztag")
    cbs = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "wiztag:t:0" in cbs
    assert "wiztag:t:1" in cbs
    assert "wiztag:done" in cbs


def test_known_groups_and_target_picker(tmp_path: Path) -> None:
    groups_file = tmp_path / "groups.json"
    sample = {
        "-100111": {"title": "Grup Tim Alpha", "type": "supergroup"},
        "-100222": {"title": "Channel Info", "type": "channel"},
    }
    save_known_groups(groups_file, sample)
    loaded = load_known_groups(groups_file)
    assert loaded == sample

    markup = build_target_picker_keyboard(217411824, loaded, [-100111], prefix="wiztgt")
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "wiztgt:toggle:217411824" in callbacks
    assert "wiztgt:toggle:-100111" in callbacks
    assert "wiztgt:toggle:-100222" in callbacks
    assert "wiztgt:done" in callbacks

    assert parse_target_entry("Grup Tim | -1003977827975") == ("Grup Tim", -1003977827975)
    assert parse_target_entry("@mychannel") == (None, "@mychannel")
    _, targets_markup = format_targets_menu(loaded)
    target_cbs = [btn.callback_data for row in targets_markup.inline_keyboard for btn in row]
    assert "tgtadd:group" in target_cbs
    assert "tgtadd:user" in target_cbs
    assert "tgtadd:channel" in target_cbs
    assert "tgtadd:other" in target_cbs


def test_mentions_parsing_and_rendering() -> None:
    tags = parse_mentions("@budi, ani Budi:123456789 @budi")
    assert tags == ["@budi", "@ani", "Budi:123456789"]

    rendered = render_message_with_mentions("Halo @[Siti](987654)!", tags, "HTML")
    assert rendered.endswith('\n\n@budi @ani <a href="tg://user?id=123456789">Budi</a>')
    assert '<a href="tg://user?id=987654">Siti</a>' in rendered

    # Auto-resolve @Nama dan @all dari user yang disimpan di Kelola Target
    saved = {
        "172640153": {"title": "Nasir QC", "type": "user"},
        "@budi_kerja": {"title": "Budi", "type": "user"},
        "-1003977827975": {"title": "QC Assurance", "type": "supergroup"},
    }
    auto_msg = render_message_with_mentions("Tolong update @Nasir dan @Budi ya!", parse_mode="HTML", saved_targets=saved)
    assert '<a href="tg://user?id=172640153">Nasir QC</a>' in auto_msg
    assert "@budi_kerja" in auto_msg

    all_msg = render_message_with_mentions("Cek @all sekarang!", parse_mode="HTML", saved_targets=saved)
    assert '<a href="tg://user?id=172640153">Nasir QC</a>' in all_msg
    assert "@budi_kerja" in all_msg

    with pytest.raises(ValueError, match="tidak valid"):
        parse_mentions("Budi:abc")


def test_mentions_upgrade_to_username_and_extract_uids(tmp_path: Path) -> None:
    saved = {
        "101": {"title": "Agus", "type": "user", "username": "agus_ok"},
        "102": {"title": "Rafi", "type": "user"},
        "103": {"title": "Sahril", "type": "user"},
    }
    groups_file = tmp_path / "groups.json"
    save_known_groups(groups_file, saved)
    reloaded = load_known_groups(groups_file)
    assert reloaded["101"]["username"] == "agus_ok"
    assert "username" not in reloaded["102"]

    mentions = ["Agus:101", "Rafi:102", "Sahril:103", "Roy:104"]
    uids = extract_mention_user_ids("Halo tim!", mentions=mentions, saved_targets=reloaded)
    # 101 sudah pakai @agus_ok di saved_targets, sisanya numerik
    assert uids == ["102", "103", "104"]

    # Saat uid_usernames ditemukan dari get_chat_member, otomatis upgrade ke @username
    rendered = render_message_with_mentions(
        "Halo tim!",
        mentions=mentions,
        parse_mode="HTML",
        saved_targets=reloaded,
        uid_usernames={"102": "rafi_tg", "104": "roy_tg"},
    )
    assert "@agus_ok" in rendered
    assert "@rafi_tg" in rendered
    assert '<a href="tg://user?id=103">Sahril</a>' in rendered
    assert "@roy_tg" in rendered


def test_admin_persistence_and_parsing(tmp_path: Path) -> None:
    admins_file = tmp_path / "admins.json"
    save_extra_admins(admins_file, {111, 222})
    assert load_extra_admins(admins_file) == {111, 222}

    cfg = BotConfig(bot_token="x", admin_ids={217411824}, schedules_file=tmp_path / "schedules.json")
    assert get_all_admin_ids(cfg, {"extra_admins": {111}}) == {217411824, 111}

    assert parse_user_ids("217411824, 123456") == [217411824, 123456]
    with pytest.raises(ValueError, match="tidak valid"):
        parse_user_ids("-100123")


def test_parse_chat_ids() -> None:
    assert parse_chat_ids("217411824, -1001234567890, @mychannel") == [
        217411824,
        -1001234567890,
        "@mychannel",
    ]
    with pytest.raises(ValueError, match="tidak valid"):
        parse_chat_ids("bukan_angka")
from config import (
    ScheduleJob,
    load_config,
    load_schedules,
    parse_flexible_schedule,
    save_schedules,
)


def test_main_and_custom_menu_keyboards() -> None:
    from bot import build_autosender_menu_keyboard

    main_callbacks = [btn.callback_data for row in build_main_menu_keyboard().inline_keyboard for btn in row]
    assert "menu:provisioning" in main_callbacks
    assert "menu:assurance" in main_callbacks
    assert "menu:maintenance" in main_callbacks
    assert "menu:setting" in main_callbacks

    autosender_callbacks = [btn.callback_data for row in build_autosender_menu_keyboard().inline_keyboard for btn in row]
    assert "menu:custom" in autosender_callbacks
    assert "menu:list" in autosender_callbacks
    assert "menu:targets" in autosender_callbacks
    assert "menu:admins" in autosender_callbacks
    assert "menu:chatid" in autosender_callbacks
    assert "menu:reload" in autosender_callbacks

    custom_callbacks = [btn.callback_data for row in build_custom_menu_keyboard().inline_keyboard for btn in row]
    assert "custom:new" in custom_callbacks
    assert "custom:edit" in custom_callbacks


def test_parse_flexible_schedule_variants() -> None:
    tz = ZoneInfo("Asia/Jakarta")
    fixed_now = datetime(2026, 11, 23, 10, 0, 0, tzinfo=tz)

    # Relatif (+10 menit)
    s_rel = parse_flexible_schedule("+10 menit", tz, now=fixed_now)
    assert s_rel["trigger"] == "date"
    assert s_rel["run_at"] == "2026-11-23 10:10:00"

    # Jam saja hari ini
    s_time = parse_flexible_schedule("15:30", tz, now=fixed_now)
    assert s_time["trigger"] == "date"
    assert s_time["run_at"] == "2026-11-23 15:30:00"

    # Besok + jam
    s_tom = parse_flexible_schedule("besok 08:00", tz, now=fixed_now)
    assert s_tom["trigger"] == "date"
    assert s_tom["run_at"] == "2026-11-24 08:00:00"

    # Tanggal format DD/MM/YYYY HH:MM
    s_dmy = parse_flexible_schedule("25/11/2026 09:15", tz, now=fixed_now)
    assert s_dmy["trigger"] == "date"
    assert s_dmy["run_at"] == "2026-11-25 09:15:00"

    # Harian berulang
    s_daily = parse_flexible_schedule("setiap hari 08:00", tz, now=fixed_now)
    assert s_daily["trigger"] == "cron"
    assert s_daily["cron"] == "0 8 * * *"

    # Interval berkala
    s_int = parse_flexible_schedule("setiap 15 menit", tz, now=fixed_now)
    assert s_int["trigger"] == "interval"
    assert s_int["minutes"] == 15

    # Cron 5 kolom
    s_cron = parse_flexible_schedule("0 9 * * 1-5", tz, now=fixed_now)
    assert s_cron["trigger"] == "cron"
    assert s_cron["cron"] == "0 9 * * 1-5"


def test_load_sample_schedules() -> None:
    jobs = load_schedules(Path(__file__).resolve().parent.parent / "schedules.json")
    assert isinstance(jobs, list)
    assert all(isinstance(j, ScheduleJob) for j in jobs)


def test_build_date_cron_and_interval_triggers() -> None:
    cfg = load_config(require_token=False)
    now = datetime.now(cfg.tzinfo)
    future = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")

    date_job = ScheduleJob(
        id="d1",
        name="Date Test",
        trigger="date",
        run_at=future,
        chat_ids=[123],
        message="Halo custom",
    )
    assert build_trigger(date_job, cfg.tzinfo).get_next_fire_time(None, now) is not None

    cron_job = ScheduleJob(
        id="c1",
        name="Cron Test",
        trigger="cron",
        cron="0 8 * * *",
        chat_ids=[123],
        message="Halo",
    )
    assert build_trigger(cron_job, cfg.tzinfo).get_next_fire_time(None, now) is not None

    interval_job = ScheduleJob(
        id="i1",
        name="Interval Test",
        trigger="interval",
        minutes=15,
        chat_ids=[123],
        message="Halo",
    )
    assert build_trigger(interval_job, cfg.tzinfo).get_next_fire_time(None, now) is not None


def test_save_and_reload_date_schedule(tmp_path: Path) -> None:
    out_file = tmp_path / "schedules.json"
    job = ScheduleJob(
        id="custom_1",
        name="Custom Test",
        trigger="date",
        run_at="2026-12-01 09:30",
        chat_ids=[217411824],
        message="Pesan custom!",
    )
    save_schedules(out_file, [job])
    reloaded = load_schedules(out_file)
    assert len(reloaded) == 1
    assert reloaded[0].id == "custom_1"
    assert reloaded[0].run_at == "2026-12-01 09:30"
    assert reloaded[0].message == "Pesan custom!"


def test_invalid_cron_raises(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        json.dumps(
            [
                {
                    "id": "bad_cron",
                    "trigger": "cron",
                    "cron": "0 8 *",
                    "chat_ids": [123],
                    "message": "Halo",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="5 bagian"):
        load_schedules(bad_file)


def test_duplicate_id_raises(tmp_path: Path) -> None:
    dup_file = tmp_path / "dup.json"
    dup_file.write_text(
        json.dumps(
            [
                {
                    "id": "same",
                    "trigger": "interval",
                    "minutes": 10,
                    "chat_ids": [123],
                    "message": "A",
                },
                {
                    "id": "same",
                    "trigger": "interval",
                    "minutes": 20,
                    "chat_ids": [123],
                    "message": "B",
                },
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplikat ID"):
        load_schedules(dup_file)


def test_per_minute_per_hour_and_per_day_schedules() -> None:
    from bot import (
        _resolve_time_text_with_mode,
        apply_schedule_spec,
        build_day_picker_prompt,
        build_day_time_prompt,
        build_flexible_time_prompt,
        build_hour_schedule_prompt,
        build_minute_schedule_prompt,
    )
    from config import parse_active_window

    tz = ZoneInfo("Asia/Makassar")
    fixed_now = datetime(2026, 11, 23, 10, 0, 0, tzinfo=tz)  # Senin

    # Per menit
    s_pm = parse_flexible_schedule("per menit", tz, now=fixed_now)
    assert s_pm["trigger"] == "interval" and s_pm["minutes"] == 1
    s_pm2 = parse_flexible_schedule("permenit", tz, now=fixed_now)
    assert s_pm2["trigger"] == "interval" and s_pm2["minutes"] == 1
    s_pm5 = parse_flexible_schedule("per 5 menit", tz, now=fixed_now)
    assert s_pm5["trigger"] == "interval" and s_pm5["minutes"] == 5

    # Per jam
    s_pj = parse_flexible_schedule("per jam", tz, now=fixed_now)
    assert s_pj["trigger"] == "interval" and s_pj["hours"] == 1
    s_pj2 = parse_flexible_schedule("perjam", tz, now=fixed_now)
    assert s_pj2["trigger"] == "interval" and s_pj2["hours"] == 1
    s_pj3 = parse_flexible_schedule("per 3 jam", tz, now=fixed_now)
    assert s_pj3["trigger"] == "interval" and s_pj3["hours"] == 3

    # Per menit & per jam dengan filter hari tertentu
    s_pm_days = parse_flexible_schedule("setiap 15 menit sen,rab,jum", tz, now=fixed_now)
    assert s_pm_days["trigger"] == "interval"
    assert s_pm_days["minutes"] == 15
    assert s_pm_days["active_days"] == [1, 3, 5]

    s_pj_days = parse_flexible_schedule("per 2 jam sen-jum", tz, now=fixed_now)
    assert s_pj_days["trigger"] == "interval"
    assert s_pj_days["hours"] == 2
    assert s_pj_days["active_days"] == [1, 2, 3, 4, 5]

    # Per hari tertentu + jam spesifik (cron)
    s_days = parse_flexible_schedule("sen,rab,jum 08:30", tz, now=fixed_now)
    assert s_days["trigger"] == "cron"
    assert s_days["cron"] == "30 8 * * mon,wed,fri"
    assert s_days["active_days"] is None

    s_days_rev = parse_flexible_schedule("09:00 sen-jum", tz, now=fixed_now)
    assert s_days_rev["trigger"] == "cron"
    assert s_days_rev["cron"] == "0 9 * * mon,tue,wed,thu,fri"

    # Pastikan "setiap hari senin 08:00" & "per hari 08:00" tidak tertangkap aturan interval
    s_hari_sen = parse_flexible_schedule("setiap hari senin 08:00", tz, now=fixed_now)
    assert s_hari_sen["trigger"] == "cron"
    assert s_hari_sen["cron"] == "0 8 * * mon"

    s_per_hari = parse_flexible_schedule("per hari 08:00", tz, now=fixed_now)
    assert s_per_hari["trigger"] == "cron"
    assert s_per_hari["cron"] == "0 8 * * *"

    # Pastikan APScheduler CronTrigger valid dengan day_of_week nama hari
    cron_job = ScheduleJob(
        id="days_1",
        name="Hari Tertentu",
        trigger="cron",
        cron=s_days["cron"],
        chat_ids=[123],
        message="Halo",
    )
    cron_job.validate()
    next_fire = build_trigger(cron_job, tz).get_next_fire_time(None, fixed_now)
    assert next_fire is not None
    assert next_fire.isoweekday() in (1, 3, 5)
    assert (next_fire.hour, next_fire.minute) == (8, 30)

    # apply_schedule_spec menerapkan active_days dari interval berfilter hari
    apply_schedule_spec(cron_job, s_pm_days)
    assert cron_job.trigger == "interval"
    assert cron_job.minutes == 15
    assert cron_job.active_days == [1, 3, 5]

    # Mengubah kembali ke cron hari tertentu mereset active_days lama agar tidak saling blokir
    apply_schedule_spec(cron_job, s_days)
    assert cron_job.trigger == "cron"
    assert cron_job.active_days is None

    # parse_active_window mendukung hari saja dan "semua"
    assert parse_active_window("sen,rab,jum") == (None, [1, 3, 5])
    assert parse_active_window("08:00-21:00 semua") == ("08:00-21:00", None)

    # Menu tombol interaktif untuk per menit, per jam, dan pilih hari (tanpa tombol duplikat)
    cfg = BotConfig(bot_token="dummy")
    _, main_time_kb = build_flexible_time_prompt(cfg, "Test")
    main_cbs = [b.callback_data for row in main_time_kb.inline_keyboard for b in row]
    assert "time:mode:minute" in main_cbs
    assert "time:mode:hour" in main_cbs
    assert "time:mode:days" in main_cbs

    _, min_kb = build_minute_schedule_prompt("Test")
    min_cbs = [b.callback_data for row in min_kb.inline_keyboard for b in row]
    assert "time:quick:setiap 1 menit" in min_cbs
    assert "time:quick:setiap 15 menit" in min_cbs
    assert "time:mode:days" not in min_cbs

    _, hr_kb = build_hour_schedule_prompt("Test")
    hr_cbs = [b.callback_data for row in hr_kb.inline_keyboard for b in row]
    assert "time:quick:setiap 1 jam" in hr_cbs
    assert "time:quick:setiap 6 jam" in hr_cbs
    assert "time:mode:days" not in hr_cbs

    _, day_kb = build_day_picker_prompt("Test", [1, 3, 5])
    day_cbs = [b.callback_data for row in day_kb.inline_keyboard for b in row]
    assert "time:day:t:1" in day_cbs
    assert "time:day:t:7" in day_cbs
    assert "time:day:next" in day_cbs

    _, day_time_kb = build_day_time_prompt("Test", [1, 3, 5])
    dt_cbs = [b.callback_data for row in day_time_kb.inline_keyboard for b in row]
    assert "time:dayapply:08:00" in dt_cbs
    assert "time:dayapply:setiap 15 menit" in dt_cbs
    assert "time:dayapply:setiap 1 jam" in dt_cbs

    # Mode-aware text input helper
    assert _resolve_time_text_with_mode("7", {"time_input_mode": "minute"}) == "setiap 7 menit"
    assert _resolve_time_text_with_mode("4", {"time_input_mode": "hour"}) == "setiap 4 jam"
    assert _resolve_time_text_with_mode("08:30", {"time_input_mode": "days_time", "time_selected_days": [1, 3, 5]}) == "sen,rab,jum 08:30"
    assert _resolve_time_text_with_mode("10 menit", {"time_input_mode": "days_time", "time_selected_days": [1, 5]}) == "setiap 10 menit sen,jum"
