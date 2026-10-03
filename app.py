from flask import Flask, request

app = Flask(__name__)

@app.get("/")
def home():
    return "MangaDar Bot is running!"

@app.get("/webhook")
def verify_webhook():
    return "Webhook endpoint ready"

@app.post("/webhook")
def receive_webhook():
    data = request.get_json(silent=True)
    print("Received:", data)
    return "EVENT_RECEIVED", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
