# Panduan Deploy Telegram AutoSender ke cPanel (PHP)

Versi ini dibuat dengan **PHP murni** (tanpa Composer/Docker/Python), sehingga bisa langsung berjalan 24/7 di hampir semua shared hosting cPanel.

## Langkah 1: Upload File ke cPanel
1. Login ke **cPanel** → buka **File Manager**.
2. Buat folder baru:
   - **Disarankan (lebih aman):** di luar `public_html`, misalnya `/home/USERNAME/telegram-autosender/` (jika hanya pakai Cron Job).
   - **Jika ingin pakai `webhook.php` (/chatid):** buat di dalam `public_html`, misalnya `/home/USERNAME/public_html/bot/`.
3. Upload semua file dari folder `php-cpanel/` ini:
   - `config.php`
   - `schedules.json`
   - `autosender.php`
   - `webhook.php` *(opsional)*
   - `.htaccess` *(pastikan opsi **Show Hidden Files** aktif di File Manager)*

## Langkah 2: Edit `config.php` dan `schedules.json`
1. Buka `config.php` → isi `bot_token` dengan token dari `@BotFather`, lalu ganti `cron_secret` dengan kode acak.
2. Buka `schedules.json` → isi `chat_ids`, pesan (`message`), dan jadwal (`cron` atau `interval`).

## Langkah 3: Pasang Cron Job di cPanel
1. Di halaman utama cPanel, buka menu **Cron Jobs**.
2. Pada bagian **Common Settings**, pilih **Once Per Minute (`* * * * *`)**.
3. Pada kolom **Command**, isi (sesuaikan `USERNAME` dan path folder kamu):
   ```bash
   /usr/local/bin/php /home/USERNAME/telegram-autosender/autosender.php >/dev/null 2>&1
   ```
   *(Jika folder berada di dalam `public_html/bot`, ganti path-nya menjadi `/home/USERNAME/public_html/bot/autosender.php`.)*
4. Klik **Add New Cron Job**.

> **Tips:** Bot akan dicek setiap menit oleh cPanel, lalu hanya mengirim pesan sesuai jadwal di `schedules.json` (dan otomatis mencegah pengiriman ganda lewat `state.json`).

## Langkah 4: Cara Tes Pengiriman
- **Lewat Terminal cPanel / SSH:**
  ```bash
  php autosender.php --dry-run
  php autosender.php --force=sapaan_pagi
  ```
- **Lewat Browser (jika file ada di `public_html`):**
  - Dry-run: `https://domainkamu.com/bot/autosender.php?key=KODE_RAHASIA_KAMU&dry_run=1`
  - Kirim langsung 1 job: `https://domainkamu.com/bot/autosender.php?key=KODE_RAHASIA_KAMU&force=sapaan_pagi`

## Opsional: Aktifkan Perintah `/chatid` via Webhook
Jika ingin tahu `chat_id` grup/chat langsung dari Telegram, upload `webhook.php` ke `public_html/bot/` (harus HTTPS), lalu buka URL ini sekali di browser:
```text
https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://domainkamu.com/bot/webhook.php
```
Setelah itu kirim `/chatid` ke bot atau grup tempat bot berada.
