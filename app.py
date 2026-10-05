from flask import Flask, request

app = Flask(__name__)

VERIFY_TOKEN = "Ghasaq_MangaDar_Verify_2026"

@app.get("/")
def home():
    return "MangaDar Bot is running!"

@app.get("/webhook")
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Verification failed", 403

@app.post("/webhook")
def receive_webhook():
    data = request.get_json(silent=True)
    print("Received:", data)
    return "EVENT_RECEIVED", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
