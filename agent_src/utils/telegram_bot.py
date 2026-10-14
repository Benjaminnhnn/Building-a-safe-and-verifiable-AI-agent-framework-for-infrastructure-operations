# Gửi tin nhắn thông báo tới qtv về sự cố, đính kèm các nút thao tác để 
# quản trị viên phê duyêt hoặc từ chối hành động sửa lỗi do AI đề xuất.
# Sử dụng Telegram Bot API để gửi tin nhắn và nhận phản hồi từ quản trị
import requests
import os
from dotenv import load_dotenv
from datetime import datetime

# Load credentials from .env file
load_dotenv()

def valid_env_value(value):
    if not value:
        return None

    value = value.strip()
    placeholders = ("your_", "change_me", "_here")
    if not value or any(marker in value for marker in placeholders):
        return None

    return value


TELEGRAM_TOKEN = valid_env_value(os.getenv("TELEGRAM_TOKEN"))
TELEGRAM_CHAT_ID = valid_env_value(os.getenv("TELEGRAM_CHAT_ID"))


def get_chat_id():
    """
    Fetch actual Chat ID from Telegram API getUpdates.
    Use when Chat ID in .env is incorrect or 403 error occurs.
    Note: Must send at least one message to the bot first.
    """
    if not TELEGRAM_TOKEN:
        print("Error: TELEGRAM_TOKEN not found in .env")
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()

        if not data.get("ok"):
            print(f"Invalid token or bot error: {data.get('description')}")
            return None

        results = data.get("result", [])
        if not results:
            print("No messages found.")
            print("Open Telegram, find your bot, send /start or any message, then retry.")
            return None

        # Get chat_id from latest message
        latest = results[-1]
        chat = latest.get("message", {}).get("chat", {})
        chat_id = chat.get("id")
        chat_name = chat.get("username") or chat.get("first_name") or "Unknown"

        print(f"Found Chat ID: {chat_id}  (from user: {chat_name})")
        print(f"Update TELEGRAM_CHAT_ID={chat_id} in your .env file")
        return chat_id

    except requests.exceptions.RequestException as e:
        print(f"❌ Connection error calling getUpdates: {e}")
        return None


def set_telegram_webhook(webhook_url, secret_token=None):
    """Registers the FastAPI endpoint as a Telegram Webhook."""
    if not TELEGRAM_TOKEN:
        print("❌ Error: TELEGRAM_TOKEN not found")
        return False
    if not webhook_url or not webhook_url.startswith("https://"):
        print("⚠️ Skipping Telegram Webhook: AI_AGENT_PUBLIC_URL must use HTTPS")
        return False
    if not secret_token or not (1 <= len(secret_token) <= 256) or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
        for character in secret_token
    ):
        print("⚠️ Skipping Telegram Webhook: TELEGRAM_WEBHOOK_SECRET must be configured")
        return False
    
    # We want to point Telegram to our /telegram/webhook endpoint
    full_url = f"{webhook_url.rstrip('/')}/telegram/webhook"
    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    
    print(f"🌐 Registering Telegram Webhook: {full_url}")
    try:
        response = requests.post(
            api_url,
            json={
                "url": full_url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "edited_message", "callback_query"],
            },
            timeout=10,
        )
        data = response.json()
        if data.get("ok"):
            print("✅ Telegram Webhook registered successfully!")
            return True
        else:
            print(f"❌ Failed to register Webhook: {data.get('description')}")
            return False
    except Exception as e:
        print(f"❌ Error setting webhook: {e}")
        return False


def split_message(text, max_length=4000):
    """Split message into chunks for Telegram (max 4096 chars per message)."""
    if len(text) <= max_length:
        return [text]
    
    # Split on newlines if possible
    lines = text.split('\n')
    chunks = []
    current_chunk = []
    current_length = 0
    
    for line in lines:
        line_len = len(line) + 1  # +1 for newline
        if current_length + line_len > max_length and current_chunk:
            chunks.append('\n'.join(current_chunk))
            current_chunk = [line]
            current_length = line_len
        else:
            current_chunk.append(line)
            current_length += line_len
    
    if current_chunk:
        chunks.append('\n'.join(current_chunk))
    
    return chunks


def send_telegram_message(message=None, chat_id=None, reply_markup=None, parse_mode="Markdown"):
    """Sends a message to the configured Telegram chat with error handling."""

    effective_chat_id = chat_id or TELEGRAM_CHAT_ID

    if message is None or str(message).strip() == "":
        print(" Warning: Empty message. Using default content...")
        message = " *AI Ops Alert*: Issue detected but no details provided."

    if not TELEGRAM_TOKEN or not effective_chat_id:
        print("❌ Error: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not found in .env")
        return False

    # Split long messages (Telegram limit: 4096 chars)
    chunks = split_message(str(message))
    if len(chunks) > 1:
        print(f"📝 Message too long ({len(message)} chars), splitting into {len(chunks)} parts...")

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    try:
        all_sent = True
        for i, chunk in enumerate(chunks):
            payload = {
                "chat_id": effective_chat_id,
                "text": chunk,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            
            # Add buttons to the LAST chunk
            if i == len(chunks) - 1 and reply_markup:
                payload["reply_markup"] = reply_markup
            
            if len(chunks) > 1:
                print(f"   📤 Sending part {i+1}/{len(chunks)}...")

            response = requests.post(url, json=payload, timeout=10)

            # Handle 403: Bot blocked or wrong chat_id
            if response.status_code == 403:
                print(" Error 403: Bot blocked or wrong chat_id.")
                print(" Trying to fetch correct Chat ID from getUpdates...\n")
                fetched_id = get_chat_id()
                if fetched_id and str(fetched_id) != str(effective_chat_id):
                    print(f"\n Chat ID mismatch detected!")
                    print(f"   .env value     : {effective_chat_id}")
                    print(f"   Correct ID     : {fetched_id}")
                    print(f" Update TELEGRAM_CHAT_ID={fetched_id} in .env and retry.")
                return False

            # Handle 400: Bad request (invalid Markdown or too long)
            elif response.status_code == 400:
                error_detail = response.json().get("description", response.text)
                print(f"❌ Error 400: Bad Request — {error_detail}")
                
                # If entity parsing error, retry without Markdown
                if "entities" in error_detail or "parse" in error_detail:
                    print("💡 Markdown parsing error - retrying without Markdown...")
                    payload.pop("parse_mode", None)
                    retry = requests.post(url, json=payload, timeout=10)
                    if retry.ok:
                        print("✅ Message sent (plain text, no Markdown)")
                    else:
                        print(f"❌ Still failed: {retry.json().get('description', 'Unknown error')}")
                        all_sent = False
                # If too long, truncate
                elif "too long" in error_detail.lower():
                    print(f"💡 Message still too long, truncating...")
                    truncated = chunk[:3500] + "\n\n[... message truncated ...]"
                    payload["text"] = truncated
                    payload.pop("parse_mode", None)
                    retry = requests.post(url, json=payload, timeout=10)
                    if retry.ok:
                        print("✅ Message sent (truncated)")
                    else:
                        all_sent = False
                else:
                    all_sent = False

            elif response.status_code != 200:
                print(f"❌ Unexpected error {response.status_code}: {response.text}")
                all_sent = False
            else:
                print("✅ Message part sent successfully")

        if all_sent and len(chunks) > 0:
            print(f"✅ All {len(chunks)} message parts sent to Telegram!")
        return all_sent

    except requests.exceptions.RequestException as e:
        print(f"❌ Connection error: {e}")
        return False



if __name__ == "__main__":
    # Simple test: Send a working test message
    print("📝 Testing Telegram integration...")
    test_msg = "*🧪 AI Ops System Test*\n\nIf you see this, Telegram integration is working!"
    send_telegram_message(test_msg)
