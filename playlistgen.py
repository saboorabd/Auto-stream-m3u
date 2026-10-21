import json

def generate_m3u():
    # Token parameters extracted from your current session
    hdnea_token = "st=1792572197~exp=1792575797~acl=/bpk-tv/ZeeThiraiHD_BTS/WDVLive/*~hmac=f18cc023bf34c508140e33ef802cce2aaa2b39aedfd1aa438ac330fa66af6fa0"
    mpd_url = f"https://jiotvpllive.cdn.jio.com/bpk-tv/ZeeThiraiHD_BTS/WDVLive/index.mpd?__hdnea__={hdnea_token}"
    
    # Clearkey mapping dictionary
    clearkey_keys = {
        "58d0716748ee52f4a55cf7baa6e072bb": "8fc185a1015f026fec82236bb0e096f4",
        "c2d762acbea256c0bd67b92d59f69757": "46c59697457af3a687426a4691393774"
    }
    
    license_key_json = json.dumps(clearkey_keys)
    
    m3u_content = f"""#EXTM3U
#EXTINF:-1 tvg-id="zeethirai" tvg-name="Zee Thirai HD" tvg-logo="https://jiotvimages.cdn.jio.com/dare_images/images/ZeeThiraiHD.png" group-title="Movies",Zee Thirai HD
#KODIPROP:inputstream.adaptive.license_type=clearkey
#KODIPROP:inputstream.adaptive.license_key={json.dumps(license_key_json)}
#EXTVLCOPT:http-referrer=https://jjtvxweb.pages.dev/
#EXTVLCOPT:http-origin=https://jjtvxweb.pages.dev
#EXTVLCOPT:http-user-agent="Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36"
#EXTHTTP:{json.dumps({"cookie": f"__hdnea__={hdnea_token}"})}
{mpd_url}
"""

    with open("zeethirai_output.m3u", "w", encoding="utf-8") as f:
        f.write(m3u_content)
    
    print("M3U file generated successfully as 'zeethirai_output.m3u'")

if __name__ == "__main__":
    generate_m3u()
