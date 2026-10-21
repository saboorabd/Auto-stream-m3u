from flask import Flask, jsonify, request
import os

app = Flask(__name__)

# Sample token & stream configuration (playlistgen.py logic incorporated)
HDNEA_TOKEN = "st=1792572197~exp=1792575797~acl=/bpk-tv/ZeeThiraiHD_BTS/WDVLive/*~hmac=f18cc023bf34c508140e33ef802cce2aaa2b39aedfd1aa438ac330fa66af6fa0"
MPD_URL = f"https://jiotvpllive.cdn.jio.com/bpk-tv/ZeeThiraiHD_BTS/WDVLive/index.mpd?__hdnea__={HDNEA_TOKEN}"

CLEARKEY_KEYS = {
    "58d0716748ee52f4a55cf7baa6e072bb": "8fc185a1015f026fec82236bb0e096f4",
    "c2d762acbea256c0bd67b92d59f69757": "46c59697457af3a687426a4691393774"
}

@app.route('/')
def home():
    return "Auto Stream M3U Service is Running Successfully! 🎉", 200

@app.route('/api/status')
def status():
    return jsonify({"status": "online", "message": "App is up and running"}), 200

@app.route('/api/stream')
def stream():
    # Playlistgen logic data output for the client
    return jsonify({
        "status": "success",
        "channel_name": "Zee Thirai HD",
        "stream_url": MPD_URL,
        "keys": CLEARKEY_KEYS,
        "headers": {
            "Referer": "https://jjtvxweb.pages.dev/",
            "Origin": "https://jjtvxweb.pages.dev",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Cookie": f"__hdnea__={HDNEA_TOKEN}"
        }
    }), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
