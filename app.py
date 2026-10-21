from flask import Flask, Response, jsonify
import os

app = Flask(__name__)

@app.route('/playlist.m3u')
def generate_playlist():
    hdnea_token = "st=1792572197~exp=1792575797~acl=/bpk-tv/ZeeThiraiHD_BTS/WDVLive/*~hmac=f18cc023bf34c508140e33ef802cce2aaa2b39aedfd1aa438ac330fa66af6fa0"
    mpd_url = f"https://jiotvpllive.cdn.jio.com/bpk-tv/ZeeThiraiHD_BTS/WDVLive/index.mpd?__hdnea__={hdnea_token}"
    
    m3u_content = f"""#EXTM3U
#EXTINF:-1 tvg-id="3051" tvg-logo="https://jiotvimages.cdn.jio.com/dare_images/images/ZeeThiraiHD.png" group-title="Tamil" http-user-agent="Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",Zee Thirai HD
#KODIPROP:inputstream.adaptive.license_type=clearkey
#KODIPROP:inputstream.adaptive.license_key={{"58d0716748ee52f4a55cf7baa6e072bb":"8fc185a1015f026fec82236bb0e096f4","c2d762acbea256c0bd67b92d59f69757":"46c59697457af3a687426a4691393774"}}
#EXTVLCOPT:http-user-agent=Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36
#EXTHTTP:{{"cookie":"__hdnea__={hdnea_token}"}}
{mpd_url}
"""
    return Response(m3u_content, mimetype="audio/x-mpegurl")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
