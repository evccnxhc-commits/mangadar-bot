import os
import requests
from flask import Flask, request

app = Flask(__name__)

VERIFY_TOKEN = "Ghasaq_MangaDar_Verify_2026"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")


@app.get("/")
def home():
    return "MangaDar Bot is running!"


@app.get("/privacy")
def privacy():
    return """
    <html>
    <head>
        <title>Privacy Policy - MangaDar Bot</title>
    </head>
    <body>
        <h1>Privacy Policy</h1>
        <p>MangaDar Bot respects your privacy.</p>
        <p>This service is used to respond to messages sent to the MangaDar Bot Facebook Page.</p>
        <p>We do not sell or share personal information with third parties.</p>
        <p>Messages may be processed only as necessary to provide the requested bot service.</p>
        <p>For questions, contact evccnxhc@gmail.com.</p>
    </body>
    </html>
    """


@app.get("/webhook")
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Verification failed", 403


def ask_ai(user_message):
    if not OPENROUTER_API_KEY:
        print("OPENROUTER_API_KEY is missing")
        return "عذرًا، خدمة الذكاء الاصطناعي غير متاحة حاليًا."

    url = "https://openrouter.ai/api/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://mangadar-bot-1.onrender.com",
        "X-Title": "MangaDar Bot"
    }

    payload = {
        "model": "openrouter/free",
        "messages": [
            {
                "role": "system",
                "content": (
                    "أنت مساعد صفحة غَسَق المتخصصة في المانجا والمانهوا. "
                    "أجب بالعربية بشكل طبيعي ومختصر. "
                    "إذا طلب المستخدم مانهوا أو فصلًا، افهم اسم العمل ورقم الفصل "
                    "ولا تدّعِ أنك أرسلت ملفًا ما لم يتم ذلك فعليًا."
                )
            },
            {
                "role": "user",
                "content": user_message
            }
        ]
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30
        )

        print("OpenRouter status:", response.status_code)
        print("OpenRouter response:", response.text)

        if response.status_code != 200:
            return "عذرًا، حدث خطأ أثناء معالجة رسالتك."

        data = response.json()
        return data["choices"][0]["message"]["content"]

    except Exception as e:
        print("AI error:", str(e))
        return "عذرًا، حدث خطأ مؤقت. حاول مرة أخرى."


def send_message(recipient_id, message_text):
    if not PAGE_ACCESS_TOKEN:
        print("PAGE_ACCESS_TOKEN is missing")
        return False

    url = "https://graph.facebook.com/v26.0/me/messages"

    params = {
        "access_token": PAGE_ACCESS_TOKEN
    }

    payload = {
        "recipient": {
            "id": recipient_id
        },
        "message": {
            "text": message_text
        }
    }

    try:
        response = requests.post(
            url,
            params=params,
            json=payload,
            timeout=30
        )

        print("Messenger status:", response.status_code)
        print("Messenger response:", response.text)

        return response.status_code == 200

    except Exception as e:
        print("Messenger error:", str(e))
        return False


@app.get("/test-ai")
def test_ai():
    if not OPENROUTER_API_KEY:
        return "OPENROUTER_API_KEY is missing", 500

    reply = ask_ai("Reply briefly in Arabic: مرحباً، هل تعمل؟")
    return f"AI Response: {reply}", 200


@app.post("/webhook")
def receive_webhook():
    data = request.get_json(silent=True)

    print("Received:", data)

    if not data:
        return "EVENT_RECEIVED", 200

    try:
        for entry in data.get("entry", []):
            for event in entry.get("messaging", []):

                sender = event.get("sender", {})
                sender_id = sender.get("id")

                message = event.get("message", {})
                text = message.get("text")

                # نتجاهل الأحداث التي ليست رسائل نصية
                if not sender_id or not text:
                    continue

                print("User ID:", sender_id)
                print("User message:", text)

                # إرسال الرسالة إلى OpenRouter
                reply = ask_ai(text)

                print("AI reply:", reply)

                # إرسال الرد إلى Messenger
                send_message(sender_id, reply)

    except Exception as e:
        print("Webhook processing error:", str(e))

    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
