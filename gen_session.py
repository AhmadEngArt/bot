#!/usr/bin/env python3
"""
ساخت TG_SESSION برای حالت کامل ربات (MTProto).

این فایل را روی کامپیوتر خودت اجرا کن (نه روی گیتهاب):
    pip install telethon
    python gen_session.py

API_ID و API_HASH را از my.telegram.org بگیر (با همان حسابی که ربات با آن کار می‌کند).
در پایان یک متن طولانی چاپ می‌شود: آن را در Secrets مخزن با نام TG_SESSION بگذار.
این متن مثل رمز حساب توست؛ به هیچ‌کس نده و جایی منتشرش نکن.
"""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()

# شماره تلفن (با کد کشور، مثلا +98912...) و کد تأیید تلگرام را خودش می‌پرسد
with TelegramClient(StringSession(), api_id, api_hash) as client:
    me = client.get_me()
    print(f"\nوارد شدی: {me.first_name} (@{me.username or '-'})")
    print("\n===== TG_SESSION (همه‌ی خط زیر را کپی کن) =====\n")
    print(client.session.save())
    print("\n===============================================")
