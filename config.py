"""Konfigurasi dan loader jadwal untuk Telegram AutoSender Bot."""

from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

RUN_AT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
)


def parse_run_at(value: str, tz: ZoneInfo) -> datetime:
    """Parse string tanggal & waktu ('YYYY-MM-DD HH:MM', 'DD-MM-YYYY HH:MM', dll.)."""
    text = value.strip()
    for fmt in RUN_AT_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    raise ValueError(
        f"Format waktu tidak valid: '{value}'. "
        "Contoh yang didukung: '2026-11-23 15:30', '23/11/2026 15:30', '15:30', '+10 menit', 'setiap hari 08:00', 'setiap 30 menit'."
    )


def parse_flexible_schedule(raw: str, tz: ZoneInfo, now: datetime | None = None) -> dict[str, Any]:
    """Parse input waktu bebas dari user menjadi konfigurasi trigger (date / cron / interval).

    Format yang didukung:
    - Tanggal & jam spesifik: '2026-11-23 15:30', '23-11-2026 15:30', '23/11/2026 15:30'
    - Jam saja (hari ini, atau besok jika sudah lewat): '15:30', '15:30:00'
    - Besok + jam: 'besok 08:00'
    - Relatif dari sekarang: '+10 menit', '+2 jam', '+30 detik', '10m', '2h'
    - Harian berulang: 'setiap hari 08:00', 'harian 21:30'
    - Interval berkala: 'setiap 15 menit', 'tiap 2 jam', 'setiap 30 detik', 'setiap 1 hari'
    - Ekspresi cron 5 bagian: '0 9 * * 1-5'
    """
    text = re.sub(r"\s+", " ", raw.strip())
    lower = text.lower()
    current = now or datetime.now(tz)

    # 1. Harian berulang: "setiap hari 08:00" / "harian 08:00" / "tiap hari 08:00" / "per hari 08:00"
    m_daily = re.fullmatch(r"(?:setiap\s+hari|tiap\s+hari|per\s+hari|harian)\s+(\d{1,2}):(\d{2})", lower)
    if m_daily:
        hour, minute = int(m_daily.group(1)), int(m_daily.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Jam harus antara 00:00 sampai 23:59.")
        return {
            "trigger": "cron",
            "cron": f"{minute} {hour} * * *",
            "active_days": None,
            "label": f"Setiap hari pukul {hour:02d}:{minute:02d} {current.tzname()}",
        }

    # 1b. Hari tertentu + jam spesifik (cek sebelum interval agar "setiap hari senin 08:00" tidak tertangkap interval):
    #     "senin 08:00", "setiap hari senin 08:00", "sen,rab,jum 08:00", "setiap sen-jum 09:30", "08:00 sen-jum"
    m_days_time = re.fullmatch(r"(.+?)\s+(\d{1,2}):(\d{2})", lower)
    m_time_days = re.fullmatch(r"(\d{1,2}):(\d{2})\s+(.+)", lower)
    if m_days_time or m_time_days:
        if m_days_time:
            days_raw = m_days_time.group(1).strip()
            d_hour, d_min = int(m_days_time.group(2)), int(m_days_time.group(3))
        else:
            assert m_time_days is not None
            d_hour, d_min = int(m_time_days.group(1)), int(m_time_days.group(2))
            days_raw = m_time_days.group(3).strip()
        if days_raw != "besok" and not re.search(r"\d", days_raw):
            try:
                parsed_days = parse_days_expression(days_raw)
            except ValueError:
                parsed_days = []
            if parsed_days:
                if not (0 <= d_hour <= 23 and 0 <= d_min <= 59):
                    raise ValueError("Jam harus antara 00:00 sampai 23:59.")
                if len(parsed_days) == 7:
                    dow_expr = "*"
                    days_label = "Setiap hari"
                else:
                    dow_expr = ",".join(CRON_DAY_CODES[d] for d in parsed_days)
                    days_label = f"Setiap {format_days_short(parsed_days)}"
                return {
                    "trigger": "cron",
                    "cron": f"{d_min} {d_hour} * * {dow_expr}",
                    "active_days": None,
                    "label": f"{days_label} pukul {d_hour:02d}:{d_min:02d} {current.tzname()}",
                }

    # 2. Interval berkala (termasuk per menit / per jam, opsional filter hari):
    #    "per menit", "permenit", "setiap menit", "setiap 15 menit", "per 2 jam",
    #    "setiap 15 menit sen-jum", "per jam sen,rab,jum"
    m_interval = re.fullmatch(
        r"(?:setiap|tiap|interval|per)\s*(?:(\d+)\s*)?(detik|dtk|s|sec|menit|mnt|m|min|jam|j|h|hour|hari|d|day)s?(?:\s+(.+))?",
        lower,
    )
    if m_interval:
        amount = int(m_interval.group(1)) if m_interval.group(1) else 1
        unit = m_interval.group(2)
        days_tail = (m_interval.group(3) or "").strip()
        if amount <= 0:
            raise ValueError("Nilai interval harus lebih besar dari 0.")
        active_days: list[int] | None = None
        days_suffix = ""
        if days_tail:
            parsed_days = parse_days_expression(days_tail)
            if len(parsed_days) < 7:
                active_days = parsed_days
                days_suffix = f" ({format_days_short(parsed_days)})"
        if unit in ("detik", "dtk", "s", "sec"):
            res: dict[str, Any] = {"trigger": "interval", "seconds": amount, "label": f"Setiap {amount} detik{days_suffix}"}
        elif unit in ("menit", "mnt", "m", "min"):
            res = {"trigger": "interval", "minutes": amount, "label": f"Setiap {amount} menit{days_suffix}"}
        elif unit in ("jam", "j", "h", "hour"):
            res = {"trigger": "interval", "hours": amount, "label": f"Setiap {amount} jam{days_suffix}"}
        else:
            res = {"trigger": "interval", "days": amount, "label": f"Setiap {amount} hari{days_suffix}"}
        if days_tail:
            res["active_days"] = active_days
        return res

    # 3. Relatif sekali kirim: "+10 menit", "+2 jam", "10m", "2h", "+30 detik"
    m_rel = re.fullmatch(
        r"\+?\s*(\d+)\s*(detik|dtk|s|sec|menit|mnt|m|min|jam|j|h|hour|hari|d|day)s?",
        lower,
    )
    if m_rel:
        amount = int(m_rel.group(1))
        unit = m_rel.group(2)
        if amount <= 0:
            raise ValueError("Durasi waktu harus lebih besar dari 0.")
        if unit in ("detik", "dtk", "s", "sec"):
            delta = timedelta(seconds=amount)
        elif unit in ("menit", "mnt", "m", "min"):
            delta = timedelta(minutes=amount)
        elif unit in ("jam", "j", "h", "hour"):
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(days=amount)
        run_dt = (current + delta).replace(microsecond=0)
        return {
            "trigger": "date",
            "run_at": run_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "label": run_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        }

    # 4. Besok + jam: "besok 08:00"
    m_tomorrow = re.fullmatch(r"besok\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", lower)
    if m_tomorrow:
        hour, minute = int(m_tomorrow.group(1)), int(m_tomorrow.group(2))
        sec = int(m_tomorrow.group(3) or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= sec <= 59):
            raise ValueError("Jam tidak valid. Gunakan format HH:MM (00:00 - 23:59).")
        run_dt = (current + timedelta(days=1)).replace(hour=hour, minute=minute, second=sec, microsecond=0)
        return {
            "trigger": "date",
            "run_at": run_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "label": run_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        }

    # 5. Jam saja: "15:30" atau "15:30:00" (hari ini, atau otomatis besok jika sudah lewat)
    m_time = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", lower)
    if m_time:
        hour, minute = int(m_time.group(1)), int(m_time.group(2))
        sec = int(m_time.group(3) or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= sec <= 59):
            raise ValueError("Jam tidak valid. Gunakan format HH:MM (00:00 - 23:59).")
        run_dt = current.replace(hour=hour, minute=minute, second=sec, microsecond=0)
        if run_dt <= current:
            run_dt = run_dt + timedelta(days=1)
        return {
            "trigger": "date",
            "run_at": run_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "label": run_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        }

    # 6. Ekspresi cron 5 kolom: "0 9 * * 1-5" atau "0 9 * * mon,wed,fri"
    parts = text.split()
    if (
        len(parts) == 5
        and all(re.fullmatch(r"[\d\*/,\-]+", p) for p in parts[:4])
        and re.fullmatch(r"[\d\*/,\-a-zA-Z]+", parts[4])
    ):
        return {
            "trigger": "cron",
            "cron": text,
            "label": f"Cron ({text})",
        }

    # 7. Tanggal & jam eksplisit: "2026-11-23 15:30" atau "23/11/2026 15:30"
    run_dt = parse_run_at(text, tz)
    if run_dt <= current:
        raise ValueError(
            f"Waktu {run_dt.strftime('%Y-%m-%d %H:%M:%S %Z')} sudah lewat. "
            f"Waktu sekarang: {current.strftime('%Y-%m-%d %H:%M:%S %Z')}."
        )
    return {
        "trigger": "date",
        "run_at": run_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "label": run_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }


@dataclass
class ScheduleJob:
    """Representasi satu jadwal pengiriman pesan."""

    id: str
    name: str
    trigger: str  # "cron", "interval", atau "date"
    chat_ids: list[int | str]
    message: str
    enabled: bool = True
    parse_mode: str | None = "HTML"
    photo: str | None = None
    document: str | None = None
    thread_id: int | None = None
    auto_delete_prev: bool = False
    require_ack: bool = False
    mentions: list[str] = field(default_factory=list)
    # Jam operasional opsional (contoh: active_hours="08:00-17:00", active_days=[1,2,3,4,5])
    active_hours: str | None = None
    active_days: list[int] | None = None
    # Divisi opsional: "provisioning", "assurance", atau "maintenance"
    division: str | None = None
    # Date field (digunakan jika trigger == "date")
    run_at: str | None = None
    # Cron field (digunakan jika trigger == "cron")
    cron: str | None = None
    # Interval fields (digunakan jika trigger == "interval")
    seconds: int | None = None
    minutes: int | None = None
    hours: int | None = None
    days: int | None = None

    def validate(self) -> None:
        if not self.id or not self.id.strip():
            raise ValueError("Setiap jadwal harus memiliki 'id' yang tidak kosong.")
        if not self.chat_ids:
            raise ValueError(f"Jadwal '{self.id}' harus memiliki minimal satu chat_id.")
        if not self.message and not self.photo and not self.document:
            raise ValueError(f"Jadwal '{self.id}' harus memiliki 'message', 'photo', atau 'document'.")
        if self.trigger == "cron":
            if not self.cron or len(self.cron.split()) != 5:
                raise ValueError(
                    f"Jadwal '{self.id}': 'cron' harus berupa 5 bagian "
                    "(menit jam tanggal bulan hari_minggu), contoh: '0 8 * * *'."
                )
        elif self.trigger == "interval":
            if not any([self.seconds, self.minutes, self.hours, self.days]):
                raise ValueError(
                    f"Jadwal '{self.id}': trigger 'interval' harus menentukan "
                    "minimal satu dari: seconds, minutes, hours, atau days."
                )
        elif self.trigger == "date":
            if not self.run_at:
                raise ValueError(
                    f"Jadwal '{self.id}': trigger 'date' harus menentukan 'run_at' "
                    "(contoh: '2026-11-23 15:30')."
                )
            parse_run_at(self.run_at, ZoneInfo("UTC"))
        else:
            raise ValueError(
                f"Jadwal '{self.id}': trigger '{self.trigger}' tidak dikenal. "
                "Gunakan 'cron', 'interval', atau 'date'."
            )

    def to_dict(self) -> dict[str, Any]:
        """Konversi ke dictionary bersih untuk disimpan ke schedules.json."""
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "trigger": self.trigger,
        }
        if self.trigger == "date" and self.run_at:
            data["run_at"] = self.run_at
        elif self.trigger == "cron" and self.cron:
            data["cron"] = self.cron
        elif self.trigger == "interval":
            if self.days:
                data["days"] = self.days
            if self.hours:
                data["hours"] = self.hours
            if self.minutes:
                data["minutes"] = self.minutes
            if self.seconds:
                data["seconds"] = self.seconds

        data["chat_ids"] = self.chat_ids
        if self.parse_mode is not None:
            data["parse_mode"] = self.parse_mode
        if self.photo:
            data["photo"] = self.photo
        if self.document:
            data["document"] = self.document
        if self.thread_id is not None:
            data["thread_id"] = self.thread_id
        if self.auto_delete_prev:
            data["auto_delete_prev"] = True
        if self.require_ack:
            data["require_ack"] = True
        if self.mentions:
            data["mentions"] = self.mentions
        if self.active_hours:
            data["active_hours"] = self.active_hours
        if self.active_days:
            data["active_days"] = self.active_days
        if self.division:
            data["division"] = self.division
        data["message"] = self.message
        return data


@dataclass
class BotConfig:
    """Konfigurasi utama bot dari environment variables."""

    bot_token: str
    timezone: str = "Asia/Makassar"
    admin_ids: set[int] = field(default_factory=set)
    schedules_file: Path = Path("schedules.json")
    admins_file: Path | None = None

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def resolved_admins_file(self) -> Path:
        if self.admins_file is not None:
            return self.admins_file
        return self.schedules_file.parent / "admins.json"

    @property
    def resolved_groups_file(self) -> Path:
        return self.schedules_file.parent / "groups.json"

    @property
    def resolved_members_file(self) -> Path:
        return self.schedules_file.parent / "members.json"

    @property
    def resolved_presets_file(self) -> Path:
        return self.schedules_file.parent / "presets.json"

    @property
    def resolved_history_file(self) -> Path:
        return self.schedules_file.parent / "history.json"

    @property
    def resolved_state_file(self) -> Path:
        return self.schedules_file.parent / "state.json"


def load_config(env_path: str | Path | None = None, require_token: bool = True) -> BotConfig:
    """Muat konfigurasi dari file .env dan environment variables."""
    if env_path:
        load_dotenv(dotenv_path=env_path, override=False)
    else:
        load_dotenv(override=False)

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if require_token and not bot_token:
        raise ValueError(
            "BOT_TOKEN belum diatur. Salin .env.example menjadi .env lalu isi BOT_TOKEN dari @BotFather."
        )

    timezone = os.getenv("TIMEZONE", "Asia/Makassar").strip() or "Asia/Makassar"
    ZoneInfo(timezone)

    raw_admins = os.getenv("ADMIN_IDS", "").strip()
    admin_ids: set[int] = set()
    if raw_admins:
        for item in raw_admins.split(","):
            item = item.strip()
            if item:
                admin_ids.add(int(item))

    schedules_file = Path(os.getenv("SCHEDULES_FILE", "schedules.json").strip() or "schedules.json")

    return BotConfig(
        bot_token=bot_token,
        timezone=timezone,
        admin_ids=admin_ids,
        schedules_file=schedules_file,
    )


def load_schedules(path: str | Path) -> list[ScheduleJob]:
    """Muat dan validasi daftar jadwal dari file JSON."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"File jadwal tidak ditemukan: {file_path}")

    with file_path.open("r", encoding="utf-8") as f:
        data: Any = json.load(f)

    if not isinstance(data, list):
        raise ValueError("Format schedules.json harus berupa JSON array (list of objects).")

    jobs: list[ScheduleJob] = []
    seen_ids: set[str] = set()

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Item jadwal indeks {idx} harus berupa JSON object.")

        job = ScheduleJob(
            id=str(item.get("id", "")).strip(),
            name=str(item.get("name") or item.get("id", f"job_{idx}")),
            trigger=str(item.get("trigger", "")).strip().lower(),
            chat_ids=list(item.get("chat_ids", [])),
            message=str(item.get("message", "")),
            enabled=bool(item.get("enabled", True)),
            parse_mode=item.get("parse_mode", "HTML"),
            photo=item.get("photo"),
            document=item.get("document"),
            thread_id=int(item["thread_id"]) if item.get("thread_id") is not None else None,
            auto_delete_prev=bool(item.get("auto_delete_prev", False)),
            require_ack=bool(item.get("require_ack", False)),
            mentions=list(item.get("mentions", [])),
            active_hours=item.get("active_hours"),
            active_days=list(item["active_days"]) if item.get("active_days") else None,
            division=(str(item["division"]).strip().lower() or None) if item.get("division") else None,
            run_at=item.get("run_at"),
            cron=item.get("cron"),
            seconds=item.get("seconds"),
            minutes=item.get("minutes"),
            hours=item.get("hours"),
            days=item.get("days"),
        )
        job.validate()

        if job.id in seen_ids:
            raise ValueError(f"Duplikat ID jadwal ditemukan: '{job.id}'.")
        seen_ids.add(job.id)
        jobs.append(job)

    return jobs


def save_schedules(path: str | Path, jobs: list[ScheduleJob]) -> None:
    """Simpan daftar ScheduleJob ke file JSON."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [job.to_dict() for job in jobs]
    with file_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")


def ensure_schedules_file(path: str | Path, seed_path: str | Path | None = None) -> Path:
    """Pastikan file schedules.json tersedia (berguna saat deploy dengan volume kosong di cloud)."""
    file_path = Path(path)
    if file_path.exists():
        return file_path

    file_path.parent.mkdir(parents=True, exist_ok=True)
    seed = Path(seed_path) if seed_path else Path(__file__).resolve().parent / "schedules.json"
    if seed.exists() and seed.resolve() != file_path.resolve():
        file_path.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        file_path.write_text("[]\n", encoding="utf-8")
    return file_path


def load_extra_admins(path: str | Path) -> set[int]:
    """Muat daftar User ID admin tambahan dari admins.json."""
    file_path = Path(path)
    if not file_path.exists():
        return set()
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(data, list):
        return set()
    return {int(x) for x in data if isinstance(x, int) or (isinstance(x, str) and x.strip().lstrip("-").isdigit())}


def save_extra_admins(path: str | Path, admin_ids: set[int]) -> None:
    """Simpan daftar User ID admin tambahan ke admins.json."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_ids = sorted(admin_ids)
    file_path.write_text(json.dumps(sorted_ids, indent=2) + "\n", encoding="utf-8")


def load_known_groups(path: str | Path) -> dict[str, dict[str, str]]:
    """Muat daftar grup/channel yang diketahui bot dari groups.json."""
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for cid, info in data.items():
        if isinstance(info, dict):
            entry: dict[str, str] = {
                "title": str(info.get("title") or f"Grup {cid}"),
                "type": str(info.get("type") or "group"),
            }
            uname = str(info.get("username") or "").lstrip("@").strip()
            if uname:
                entry["username"] = uname
            result[str(cid)] = entry
    return result


def save_known_groups(path: str | Path, groups: dict[str, dict[str, str]]) -> None:
    """Simpan daftar grup/channel ke groups.json."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(groups, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_known_members(path: str | Path) -> dict[str, dict[str, dict[str, str]]]:
    """Muat daftar anggota grup & kontak mention dari members.json."""
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, dict[str, str]]] = {}
    for scope, members in data.items():
        if isinstance(members, dict):
            cleaned: dict[str, dict[str, str]] = {}
            for key, info in members.items():
                if isinstance(info, dict) and info.get("token"):
                    cleaned[str(key)] = {
                        "label": str(info.get("label") or info["token"]),
                        "token": str(info["token"]),
                    }
            result[str(scope)] = cleaned
    return result


def save_known_members(path: str | Path, members: dict[str, dict[str, dict[str, str]]]) -> None:
    """Simpan daftar anggota grup & kontak mention ke members.json."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(members, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_tag_presets(path: str | Path) -> dict[str, list[str]]:
    """Muat daftar preset tim/shift tag dari presets.json."""
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(name): [str(t) for t in tags]
        for name, tags in data.items()
        if isinstance(tags, list) and tags
    }


def save_tag_presets(path: str | Path, presets: dict[str, list[str]]) -> None:
    """Simpan daftar preset tim/shift tag ke presets.json."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(presets, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_delivery_history(path: str | Path) -> list[dict[str, Any]]:
    """Muat riwayat pengiriman dari history.json."""
    file_path = Path(path)
    if not file_path.exists():
        return []
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def append_delivery_history(path: str | Path, entry: dict[str, Any], max_items: int = 30) -> None:
    """Tambahkan satu catatan pengiriman ke history.json (simpan maksimal max_items terakhir)."""
    history = load_delivery_history(path)
    history.insert(0, entry)
    history = history[:max_items]
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_runtime_state(path: str | Path) -> dict[str, Any]:
    """Muat state runtime (last_messages untuk anti-spam & ack_dates untuk tombol selesai)."""
    file_path = Path(path)
    default_state: dict[str, Any] = {"last_messages": {}, "ack_dates": {}}
    if not file_path.exists():
        return default_state
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_state
    if not isinstance(data, dict):
        return default_state
    return {
        "last_messages": data.get("last_messages") if isinstance(data.get("last_messages"), dict) else {},
        "ack_dates": data.get("ack_dates") if isinstance(data.get("ack_dates"), dict) else {},
    }


def save_runtime_state(path: str | Path, state: dict[str, Any]) -> None:
    """Simpan state runtime ke state.json."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


DAY_FULL_LABELS: dict[int, str] = {
    1: "Senin", 2: "Selasa", 3: "Rabu", 4: "Kamis", 5: "Jumat", 6: "Sabtu", 7: "Minggu",
}
MONTH_LABELS: dict[int, str] = {
    1: "Januari", 2: "Februari", 3: "Maret", 4: "April", 5: "Mei", 6: "Juni",
    7: "Juli", 8: "Agustus", 9: "September", 10: "Oktober", 11: "November", 12: "Desember",
}


def render_dynamic_placeholders(message: str, now: datetime | None = None) -> str:
    """Ganti variabel dinamis {hari}, {tanggal}, {tanggal_panjang}, {jam}, {waktu}, {bulan}, {tahun}."""
    if not message or "{" not in message:
        return message
    current = now or datetime.now()
    tz_part = f" {current.tzname()}" if current.tzname() else ""
    replacements = {
        "hari": DAY_FULL_LABELS.get(current.isoweekday(), ""),
        "tanggal": current.strftime("%d/%m/%Y"),
        "tanggal_panjang": f"{current.day} {MONTH_LABELS.get(current.month, '')} {current.year}",
        "jam": current.strftime("%H:%M"),
        "waktu": f"{current.strftime('%H:%M:%S')}{tz_part}",
        "bulan": MONTH_LABELS.get(current.month, ""),
        "tahun": str(current.year),
    }

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        return replacements.get(key, match.group(0))

    return re.sub(r"\{([a-zA-Z_]+)\}", _sub, message)


DAY_NAME_MAP: dict[str, int] = {
    "sen": 1, "senin": 1, "mon": 1, "monday": 1,
    "sel": 2, "selasa": 2, "tue": 2, "tuesday": 2,
    "rab": 3, "rabu": 3, "wed": 3, "wednesday": 3,
    "kam": 4, "kamis": 4, "thu": 4, "thursday": 4,
    "jum": 5, "jumat": 5, "jum'at": 5, "fri": 5, "friday": 5,
    "sab": 6, "sabtu": 6, "sat": 6, "saturday": 6,
    "min": 7, "minggu": 7, "ahad": 7, "sun": 7, "sunday": 7,
}
DAY_LABELS: dict[int, str] = {
    1: "Sen", 2: "Sel", 3: "Rab", 4: "Kam", 5: "Jum", 6: "Sab", 7: "Min",
}
CRON_DAY_CODES: dict[int, str] = {
    1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat", 7: "sun",
}


def format_days_short(days: list[int]) -> str:
    """Format daftar ISO weekday [1..7] menjadi label singkat bahasa Indonesia."""
    ordered = sorted(set(days))
    if len(ordered) == 7:
        return "Setiap Hari"
    if ordered == [1, 2, 3, 4, 5]:
        return "Sen–Jum"
    if ordered == [6, 7]:
        return "Sab–Min"
    return ", ".join(DAY_LABELS.get(d, str(d)) for d in ordered)


def parse_days_expression(raw: str) -> list[int]:
    """Parse daftar hari seperti 'sen-jum', 'senin, rabu, jumat', 'sab-min', 'semua'."""
    day_str = re.sub(r"\s*-\s*", "-", raw.strip().lower())
    day_str = re.sub(r"^(?:setiap|tiap|pada)?\s*(?:hari\s+)?", "", day_str).strip()
    day_str = re.sub(r"\bdan\b", ",", day_str)
    if not day_str:
        raise ValueError("Nama hari tidak valid. Gunakan: sen, sel, rab, kam, jum, sab, min.")
    if day_str in ("semua", "setiap hari", "tiap hari", "harian", "all", "everyday"):
        return [1, 2, 3, 4, 5, 6, 7]
    if day_str in ("weekday", "weekdays", "hari kerja", "kerja"):
        return [1, 2, 3, 4, 5]
    if day_str in ("weekend", "weekends", "akhir pekan", "libur"):
        return [6, 7]

    days_set: set[int] = set()
    for chunk in re.split(r"[,]+", day_str):
        chunk = chunk.strip()
        if not chunk:
            continue
        for tok in chunk.split():
            tok = tok.strip()
            if not tok:
                continue
            if "-" in tok:
                a, b = [x.strip() for x in tok.split("-", 1)]
                if a not in DAY_NAME_MAP or b not in DAY_NAME_MAP:
                    raise ValueError("Nama hari tidak valid. Gunakan: sen, sel, rab, kam, jum, sab, min.")
                da, db = DAY_NAME_MAP[a], DAY_NAME_MAP[b]
                if da <= db:
                    days_set.update(range(da, db + 1))
                else:
                    days_set.update(range(da, 8))
                    days_set.update(range(1, db + 1))
            else:
                if tok not in DAY_NAME_MAP:
                    raise ValueError(f"Hari '{tok}' tidak valid. Gunakan: sen, sel, rab, kam, jum, sab, min.")
                days_set.add(DAY_NAME_MAP[tok])
    if not days_set:
        raise ValueError("Nama hari tidak valid. Gunakan: sen, sel, rab, kam, jum, sab, min.")
    return sorted(days_set)


def parse_active_window(raw: str) -> tuple[str | None, list[int] | None]:
    """Parse input jam operasional, contoh:
    - '24 jam' / 'off' / 'hapus' -> (None, None)
    - '08:00-17:00' -> ('08:00-17:00', None)
    - '08:00-17:00 sen-jum' -> ('08:00-17:00', [1,2,3,4,5])
    - '08:00-17:00 sen,rab,jum' -> ('08:00-17:00', [1,3,5])
    - 'sen,rab,jum' -> (None, [1,3,5])
    """
    text = re.sub(r"\s+", " ", raw.strip().lower())
    if text in ("24 jam", "24jam", "off", "hapus", "none", "bebas", "semua"):
        return None, None

    parts = text.split(" ", 1)
    hour_part = parts[0]
    m_hour = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", hour_part)
    if not m_hour:
        try:
            days_only = parse_days_expression(text)
            return None, (None if len(days_only) == 7 else days_only)
        except ValueError:
            pass
        raise ValueError(
            "Format jam operasional tidak valid. Contoh: '08:00-17:00', '08:00-17:00 sen-jum', 'sen,rab,jum', atau '24 jam'."
        )
    sh, sm, eh, em = (int(m_hour.group(i)) for i in range(1, 5))
    if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
        raise ValueError("Jam harus antara 00:00 sampai 23:59.")
    active_hours = f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"

    active_days: list[int] | None = None
    if len(parts) > 1 and parts[1].strip():
        parsed_days = parse_days_expression(parts[1].strip())
        active_days = None if len(parsed_days) == 7 else parsed_days

    return active_hours, active_days


def format_active_window(active_hours: str | None, active_days: list[int] | None) -> str:
    if not active_hours and not active_days:
        return "24 Jam (Setiap Hari)"
    hours_part = active_hours or "00:00-23:59"
    if not active_days or len(active_days) == 7:
        return f"{hours_part} (Setiap Hari)"
    if active_days == [1, 2, 3, 4, 5]:
        return f"{hours_part} (Sen–Jum)"
    days_label = ", ".join(DAY_LABELS.get(d, str(d)) for d in sorted(active_days))
    return f"{hours_part} ({days_label})"


def is_within_active_window(job: ScheduleJob, now: datetime) -> bool:
    """Cek apakah waktu `now` berada di dalam jam & hari operasional jadwal."""
    if job.active_days:
        if now.isoweekday() not in job.active_days:
            return False
    if job.active_hours:
        m = re.fullmatch(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})", job.active_hours.strip())
        if m:
            start_min = int(m.group(1)) * 60 + int(m.group(2))
            end_min = int(m.group(3)) * 60 + int(m.group(4))
            cur_min = now.hour * 60 + now.minute
            if start_min <= end_min:
                if not (start_min <= cur_min <= end_min):
                    return False
            else:
                # Lintas tengah malam (misal 22:00-06:00)
                if not (cur_min >= start_min or cur_min <= end_min):
                    return False
    return True


def parse_user_ids(raw: str) -> list[int]:
    """Parse daftar User ID Telegram (angka positif dipisah koma)."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("User ID tidak boleh kosong.")
    result: list[int] = []
    for part in parts:
        try:
            uid = int(part)
            if uid <= 0:
                raise ValueError
            result.append(uid)
        except ValueError as exc:
            raise ValueError(
                f"User ID '{part}' tidak valid. Masukkan angka User ID Telegram (contoh: 217411824)."
            ) from exc
    return result


def parse_mentions(raw: str) -> list[str]:
    """Parse daftar tag/mention dari input user.

    Format yang didukung (dipisah koma atau spasi):
    - '@username' atau 'username' -> '@username'
    - 'Nama:123456789' -> 'Nama:123456789' (mention berdasarkan User ID)
    """
    tokens = [t.strip() for t in re.split(r"[\s,]+", raw.strip()) if t.strip()]
    if not tokens:
        raise ValueError("Daftar tag tidak boleh kosong.")

    parsed: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if ":" in token:
            label, uid_str = token.rsplit(":", 1)
            label = label.strip().lstrip("@")
            uid_str = uid_str.strip()
            if not label or not uid_str.isdigit():
                raise ValueError(
                    f"Format tag ID '{token}' tidak valid. Gunakan 'Nama:UserID' (contoh: Budi:123456789)."
                )
            normalized = f"{label}:{uid_str}"
        else:
            uname = token.lstrip("@")
            if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", uname):
                raise ValueError(
                    f"Username '{token}' tidak valid. Gunakan huruf, angka, atau underscore (contoh: @budi_123)."
                )
            normalized = f"@{uname}"

        key = normalized.lower()
        if key not in seen:
            seen.add(key)
            parsed.append(normalized)

    return parsed


def format_mention_token(
    token: str,
    parse_mode: str | None = "HTML",
    uid_usernames: dict[str, str] | None = None,
) -> str:
    """Render satu token mention menjadi format siap kirim Telegram.

    Jika token berbentuk 'Nama:UserID' dan username Telegram user tersebut diketahui
    (via uid_usernames), otomatis gunakan '@username' karena jauh lebih reliabel
    di Telegram dibanding tg://user?id= (yang sering di-strip Telegram jika user
    belum pernah /start bot atau membatasi privasi akun).
    """
    if ":" in token:
        label, uid_str = token.rsplit(":", 1)
        uid_clean = uid_str.strip()
        if uid_usernames:
            uname = (uid_usernames.get(uid_clean) or "").lstrip("@").strip()
            if uname:
                return f"@{uname}"
        if parse_mode and parse_mode.upper() == "HTML":
            return f'<a href="tg://user?id={uid_clean}">{html.escape(label)}</a>'
        return f"[{label}](tg://user?id={uid_clean})"
    return token if token.startswith("@") else f"@{token}"


def extract_mention_user_ids(
    message: str,
    mentions: list[str] | None = None,
    saved_targets: dict[str, dict[str, str]] | None = None,
    tag_presets: dict[str, list[str]] | None = None,
) -> list[str]:
    """Ekstrak semua User ID numerik yang akan di-mention oleh sebuah pesan/jadwal."""
    rendered_html = render_message_with_mentions(
        message,
        mentions=mentions,
        parse_mode="HTML",
        saved_targets=saved_targets,
        tag_presets=tag_presets,
    )
    found: list[str] = []
    seen: set[str] = set()
    for uid in re.findall(r'tg://user\?id=(\d+)', rendered_html):
        if uid not in seen:
            seen.add(uid)
            found.append(uid)
    return found


def render_message_with_mentions(
    message: str,
    mentions: list[str] | None = None,
    parse_mode: str | None = "HTML",
    saved_targets: dict[str, dict[str, str]] | None = None,
    tag_presets: dict[str, list[str]] | None = None,
    uid_usernames: dict[str, str] | None = None,
) -> str:
    """Gabungkan daftar mentions dengan isi pesan, serta auto-resolve @Nama, @NamaTim, @all/@semua, dan @[Nama](user_id)."""
    rendered = message

    # Gabungkan peta uid -> username dari saved_targets + uid_usernames
    merged_usernames: dict[str, str] = {}
    for cid_str, info in (saved_targets or {}).items():
        uname = (info.get("username") or "").lstrip("@").strip()
        if uname and cid_str.lstrip("-").isdigit():
            merged_usernames[cid_str] = uname
    for uid_k, uname_v in (uid_usernames or {}).items():
        uname = (uname_v or "").lstrip("@").strip()
        if uname:
            merged_usernames[str(uid_k)] = uname

    # 1. Konversi inline @[Nama](123456789) -> @username (jika ada) atau HTML/Markdown mention link
    def _replace_inline(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        uid = match.group(2).strip()
        if uid in merged_usernames:
            return f"@{merged_usernames[uid]}"
        if parse_mode and parse_mode.upper() == "HTML":
            return f'<a href="tg://user?id={uid}">{html.escape(label)}</a>'
        return f"[{label}](tg://user?id={uid})"

    rendered = re.sub(r"@\[([^\]]+)\]\((\d+)\)", _replace_inline, rendered)

    # 2. Bangun lookup dari user tersimpan (Kelola Target) & preset tim
    if saved_targets or tag_presets:
        alias_map: dict[str, str] = {}
        all_user_mentions: list[str] = []
        seen_all: set[str] = set()

        for cid_str, info in (saved_targets or {}).items():
            ttype = (info.get("type") or "").lower()
            if ttype not in ("user", "private"):
                continue
            title = (info.get("title") or "").strip()
            if cid_str.startswith("@"):
                mention_str = cid_str
                alias_map[cid_str.lstrip("@").lower()] = mention_str
            elif cid_str.lstrip("-").isdigit():
                display = title or cid_str
                mention_str = format_mention_token(
                    f"{display}:{cid_str}",
                    parse_mode,
                    uid_usernames=merged_usernames,
                )
            else:
                continue

            if mention_str not in seen_all:
                seen_all.add(mention_str)
                all_user_mentions.append(mention_str)

            if title:
                compact = re.sub(r"\s+", "", title).lower()
                first_word = title.split()[0].lstrip("@").lower()
                if compact:
                    alias_map.setdefault(compact, mention_str)
                if first_word and len(first_word) >= 2:
                    alias_map.setdefault(first_word, mention_str)

        for preset_name, preset_tokens in (tag_presets or {}).items():
            compact_preset = re.sub(r"\s+", "", preset_name).lower()
            if compact_preset and preset_tokens:
                rendered_preset = " ".join(
                    format_mention_token(t, parse_mode, uid_usernames=merged_usernames)
                    for t in preset_tokens
                )
                alias_map[compact_preset] = rendered_preset

        all_joined = " ".join(all_user_mentions)

        def _replace_at_token(match: re.Match[str]) -> str:
            token = match.group(1)
            key = token.lower()
            if key in ("all", "semua") and all_joined:
                return all_joined
            if key in alias_map:
                return alias_map[key]
            return match.group(0)

        rendered = re.sub(r"(?<![A-Za-z0-9_./])@([A-Za-z0-9_]{2,64})", _replace_at_token, rendered)

    if mentions:
        tag_line = " ".join(
            format_mention_token(m, parse_mode, uid_usernames=merged_usernames)
            for m in mentions
        )
        rendered = f"{rendered}\n\n{tag_line}" if rendered else tag_line

    return rendered
