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

    # 1. Harian berulang: "setiap hari 08:00" / "harian 08:00" / "tiap hari 08:00"
    m_daily = re.fullmatch(r"(?:setiap\s+hari|tiap\s+hari|harian)\s+(\d{1,2}):(\d{2})", lower)
    if m_daily:
        hour, minute = int(m_daily.group(1)), int(m_daily.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Jam harus antara 00:00 sampai 23:59.")
        return {
            "trigger": "cron",
            "cron": f"{minute} {hour} * * *",
            "label": f"Setiap hari pukul {hour:02d}:{minute:02d} {current.tzname()}",
        }

    # 2. Interval berkala: "setiap 15 menit" / "tiap 2 jam" / "setiap 30 detik" / "setiap 1 hari"
    m_interval = re.fullmatch(
        r"(?:setiap|tiap|interval)\s+(\d+)\s*(detik|dtk|s|sec|menit|mnt|m|min|jam|j|h|hour|hari|d|day)s?",
        lower,
    )
    if m_interval:
        amount = int(m_interval.group(1))
        unit = m_interval.group(2)
        if amount <= 0:
            raise ValueError("Nilai interval harus lebih besar dari 0.")
        if unit in ("detik", "dtk", "s", "sec"):
            return {"trigger": "interval", "seconds": amount, "label": f"Setiap {amount} detik"}
        if unit in ("menit", "mnt", "m", "min"):
            return {"trigger": "interval", "minutes": amount, "label": f"Setiap {amount} menit"}
        if unit in ("jam", "j", "h", "hour"):
            return {"trigger": "interval", "hours": amount, "label": f"Setiap {amount} jam"}
        return {"trigger": "interval", "days": amount, "label": f"Setiap {amount} hari"}

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

    # 6. Ekspresi cron 5 kolom: "0 9 * * 1-5"
    parts = text.split()
    if len(parts) == 5 and all(re.fullmatch(r"[\d\*/,\-]+", p) for p in parts):
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
    mentions: list[str] = field(default_factory=list)
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
        if not self.message and not self.photo:
            raise ValueError(f"Jadwal '{self.id}' harus memiliki 'message' atau 'photo'.")
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
        if self.mentions:
            data["mentions"] = self.mentions
        data["message"] = self.message
        return data


@dataclass
class BotConfig:
    """Konfigurasi utama bot dari environment variables."""

    bot_token: str
    timezone: str = "Asia/Jakarta"
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

    timezone = os.getenv("TIMEZONE", "Asia/Jakarta").strip() or "Asia/Jakarta"
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
            mentions=list(item.get("mentions", [])),
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


def format_mention_token(token: str, parse_mode: str | None = "HTML") -> str:
    """Render satu token mention menjadi format siap kirim Telegram."""
    if ":" in token:
        label, uid_str = token.rsplit(":", 1)
        if parse_mode and parse_mode.upper() == "HTML":
            return f'<a href="tg://user?id={uid_str}">{html.escape(label)}</a>'
        return f"[{label}](tg://user?id={uid_str})"
    return token if token.startswith("@") else f"@{token}"


def render_message_with_mentions(
    message: str,
    mentions: list[str] | None = None,
    parse_mode: str | None = "HTML",
) -> str:
    """Gabungkan daftar mentions dengan isi pesan serta konversi @[Nama](user_id) inline."""
    rendered = message

    # Konversi inline @[Nama](123456789) -> HTML/Markdown mention link
    def _replace_inline(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        uid = match.group(2).strip()
        if parse_mode and parse_mode.upper() == "HTML":
            return f'<a href="tg://user?id={uid}">{html.escape(label)}</a>'
        return f"[{label}](tg://user?id={uid})"

    rendered = re.sub(r"@\[([^\]]+)\]\((\d+)\)", _replace_inline, rendered)

    if mentions:
        tag_line = " ".join(format_mention_token(m, parse_mode) for m in mentions)
        rendered = f"{tag_line}\n{rendered}" if rendered else tag_line

    return rendered
