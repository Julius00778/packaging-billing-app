"""Telegram Bot API ka patla sa wrapper.

Yahan koi Flask ya DB nahi hai — sirf Telegram se baat. Isse test karna asaan
rehta hai: `send` ko badal do aur poora bot bina network ke chal jaata hai.

Bot ka kaam jaan-boojh ke chhota rakha gaya hai: order dikhana aur do button
lena. Galat order theek karna ya cancel karna app me hota hai, Telegram pe nahi.
"""

import io
import json
import os

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    """Setup ki galti ya Telegram ka mana karna — message operator ko dikhta hai."""


def token_from_env(env_var="TELEGRAM_BOT_TOKEN"):
    token = os.environ.get(env_var, "").strip()
    if not token:
        raise TelegramError(
            f"{env_var} set nahi hai. BotFather se mila token Railway ke variables "
            "me is naam se daalna padega."
        )
    return token


def call(method, payload=None, files=None, token=None, timeout=20):
    """Telegram ko ek call. Kuch bhi phate toh saaf message ke saath uthta hai."""
    try:
        import requests
    except ImportError:
        raise TelegramError("requests install nahi hai — requirements.txt dekho.")
    url = API.format(token=token or token_from_env(), method=method)
    try:
        if files:
            resp = requests.post(url, data=payload or {}, files=files, timeout=timeout)
        else:
            resp = requests.post(url, json=payload or {}, timeout=timeout)
    except Exception as exc:
        raise TelegramError(f"Telegram se baat nahi ho payi: {exc}")

    try:
        body = resp.json()
    except ValueError:
        raise TelegramError(f"Telegram ne ajeeb jawab diya (HTTP {resp.status_code}).")
    if not body.get("ok"):
        raise TelegramError(body.get("description") or "Telegram ne mana kar diya.")
    return body.get("result")


def send_message(chat_id, text, buttons=None, token=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    return call("sendMessage", payload, token=token)


def send_photo(chat_id, photo_bytes, caption, buttons=None, filename="product.jpg",
               token=None):
    """Photo ke saath order card. Photo na ho toh sirf text bhej do."""
    if not photo_bytes:
        return send_message(chat_id, caption, buttons, token=token)
    payload = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    files = {"photo": (filename, io.BytesIO(photo_bytes), "image/jpeg")}
    return call("sendPhoto", payload, files=files, token=token)


def edit_message(chat_id, message_id, text, buttons=None, token=None):
    """Button dabne ke baad wahi card update kar do — naya msg na bheje.

    Har order ka aakhri (button wala) card text hota hai, photo nahi — isliye
    seedha editMessageText.
    """
    payload = {"chat_id": chat_id, "message_id": message_id,
               "text": text, "parse_mode": "HTML"}
    if buttons is not None:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    return call("editMessageText", payload, token=token)


def answer_callback(callback_id, text="", alert=False, token=None):
    """Button dabane wale ko chhota sa jawab — warna Telegram pe ghadi ghoomti rehti hai."""
    return call("answerCallbackQuery",
                {"callback_query_id": callback_id, "text": text[:200],
                 "show_alert": bool(alert)}, token=token)


def set_webhook(url, secret_token=None, token=None):
    payload = {"url": url, "allowed_updates": ["message", "callback_query",
                                               "my_chat_member"]}
    if secret_token:
        payload["secret_token"] = secret_token
    return call("setWebhook", payload, token=token)


def delete_webhook(token=None):
    return call("deleteWebhook", {}, token=token)


def get_me(token=None):
    return call("getMe", {}, token=token)


def chat_from_update(update):
    """Update kisi bhi shakal ka ho, uska chat nikaal lo."""
    for key in ("message", "edited_message", "channel_post", "my_chat_member"):
        if key in update:
            return update[key].get("chat")
    if "callback_query" in update:
        msg = update["callback_query"].get("message") or {}
        return msg.get("chat")
    return None


def user_from_update(update):
    for key in ("message", "edited_message"):
        if key in update:
            return update[key].get("from")
    if "callback_query" in update:
        return update["callback_query"].get("from")
    if "my_chat_member" in update:
        return update["my_chat_member"].get("from")
    return None


def person_name(user):
    if not user:
        return ""
    bits = [user.get("first_name") or "", user.get("last_name") or ""]
    name = " ".join(b for b in bits if b).strip()
    return name or (user.get("username") or str(user.get("id") or ""))
