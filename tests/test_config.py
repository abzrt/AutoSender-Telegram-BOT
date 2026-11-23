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
    build_trigger,
    get_all_admin_ids,
    parse_chat_ids,
)
from config import (
    BotConfig,
    ScheduleJob,
    load_config,
    load_extra_admins,
    load_schedules,
    parse_flexible_schedule,
    parse_mentions,
    parse_user_ids,
    render_message_with_mentions,
    save_extra_admins,
    save_schedules,
)


def test_mentions_parsing_and_rendering() -> None:
    tags = parse_mentions("@budi, ani Budi:123456789 @budi")
    assert tags == ["@budi", "@ani", "Budi:123456789"]

    rendered = render_message_with_mentions("Halo @[Siti](987654)!", tags, "HTML")
    assert rendered.startswith('@budi @ani <a href="tg://user?id=123456789">Budi</a>\n')
    assert '<a href="tg://user?id=987654">Siti</a>' in rendered

    with pytest.raises(ValueError, match="tidak valid"):
        parse_mentions("Budi:abc")


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
    main_callbacks = [btn.callback_data for row in build_main_menu_keyboard().inline_keyboard for btn in row]
    assert "menu:custom" in main_callbacks
    assert "menu:list" in main_callbacks
    assert "menu:admins" in main_callbacks
    assert "menu:chatid" in main_callbacks
    assert "menu:reload" in main_callbacks

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
