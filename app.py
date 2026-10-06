import os
import requests
from flask import Flask, request

app = Flask(__name__)

VERIFY_TOKEN = "Ghasaq_MangaDar_Verify_2026"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

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


@app.get("/test-ai")
def test_ai():
    if not OPENROUTER_API_KEY:
        return "OPENROUTER_API_KEY is missing", 500

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
                "role": "user",
                "content": "Reply briefly in Arabic: مرحباً، هل تعمل؟"
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
            return f"OpenRouter error: {response.status_code}<br>{response.text}", 500

        data = response.json()
        reply = data["choices"][0]["message"]["content"]

        return f"AI Response: {reply}", 200

    except Exception as e:
        print("AI error:", str(e))
        return f"AI connection error: {str(e)}", 500


@app.post("/webhook")
def receive_webhook():
    data = request.get_json(silent=True)
    print("Received:", data)
    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
