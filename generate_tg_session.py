"""Helper 1-kali untuk membuat TG_STRING_SESSION Telethon (Auto-OTP @HCMTelkomAksesBot).

Jalankan secara lokal di PC Anda:
    .venv\\Scripts\\python.exe generate_tg_session.py

Hasil StringSession JANGAN disimpan di GitHub — masukkan hanya ke Railway Variables:
    TG_API_ID
    TG_API_HASH
    TG_STRING_SESSION
    TG_OTP_BOT_USERNAME=HCMTelkomAksesBot
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession


def main() -> None:
    print("=== Generator TG_STRING_SESSION untuk Auto-OTP @HCMTelkomAksesBot ===")
    api_id = int(input("Masukkan TG_API_ID (dari https://my.telegram.org): ").strip())
    api_hash = input("Masukkan TG_API_HASH (dari https://my.telegram.org): ").strip()
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        session_str = client.session.save()
        print("\n✅ BERHASIL! Salin baris di bawah ke Railway Variables (TG_STRING_SESSION):\n")
        print(session_str)
        print("\n⚠️ RAHASIA: Jangan bagikan string ini atau commit ke GitHub!")


if __name__ == "__main__":
    main()
