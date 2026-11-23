# Telegram AutoSender Bot (Python)

Bot Telegram untuk mengirim pesan otomatis secara terjadwal menggunakan **cron** atau **interval** berkala.

## Fitur Utama
- **Jadwal Cron & Interval**: Kirim pesan pada jam tertentu (misalnya setiap hari jam 08:00) atau setiap beberapa menit/jam.
- **Multi Target**: Bisa mengirim ke banyak `chat_id` sekaligus (chat pribadi, grup, maupun channel).
- **Teks & Gambar**: Mendukung format `HTML` / `Markdown` serta lampiran foto (`photo` berupa URL atau path file lokal).
- **Perintah Telegram**:
  - `/start` atau `/help` — Info bot, Chat ID, dan User ID.
  - `/chatid` — Menampilkan Chat ID grup/chat saat ini.
  - `/list` — Melihat daftar jadwal yang sedang aktif.
  - `/test <job_id>` — Mengirim pesan tes langsung untuk jadwal tertentu.
  - `/reload` — Memuat ulang `schedules.json` tanpa perlu restart bot.

## Cara Setup & Menjalankan

### 1. Buat Bot di Telegram
1. Buka [@BotFather](https://t.me/BotFather) di Telegram.
2. Kirim `/newbot`, lalu ikuti instruksi sampai mendapatkan **Bot Token**.
3. Jika ingin mengirim ke grup/channel, masukkan bot ke grup/channel tersebut (untuk channel, jadikan bot sebagai **Admin** dengan izin *Post Messages*).

### 2. Konfigurasi `.env`
Salin `.env.example` menjadi `.env`, lalu isi `BOT_TOKEN`:
```powershell
Copy-Item .env.example .env
```

### 3. Atur Jadwal di `schedules.json`
Edit `schedules.json` sesuai kebutuhan. Untuk mengetahui `chat_id` grup atau chat pribadi, jalankan bot lalu kirim `/chatid` di chat tujuan.

Contoh jadwal **cron** (`menit jam tanggal bulan hari_minggu`):
- `"0 8 * * *"` → Setiap hari pukul 08:00
- `"30 9 * * 1-5"` → Senin–Jumat pukul 09:30
- `"0 */2 * * *"` → Setiap 2 jam

### 4. Cek Jadwal (Dry-Run)
```powershell
.\.venv\Scripts\python.exe bot.py --check
```

### 5. Jalankan Bot
```powershell
.\.venv\Scripts\python.exe bot.py
```

## Menjalankan 24/7 dengan Docker

Pastikan file `.env` dan `schedules.json` sudah siap, lalu jalankan:

### Menggunakan Docker Compose (Disarankan)
```powershell
docker compose up -d --build
```
Untuk melihat log:
```powershell
docker compose logs -f
```
Untuk menghentikan:
```powershell
docker compose down
```

### Menggunakan Docker CLI langsung
```powershell
docker build -t telegram-autosender .
docker run -d --name telegram-autosender --restart unless-stopped --env-file .env -v "${PWD}/schedules.json:/app/schedules.json:ro" telegram-autosender
```

