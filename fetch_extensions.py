"""
Build-time downloader for the browser extensions.

  video_link_extractor : GitHub source ZIP (jrcramos/video_link_extractor)
  stream_detector      : Chrome Web Store CRX
  m3u8_sniffer         : Chrome Web Store CRX

Each extension is unpacked into /opt/extensions/<name>/.
A failed download is reported and skipped, so the build still succeeds.
The app runs without any of them (network and page-source sniffing still work).
"""

import io
import shutil
import struct
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path("/opt/extensions")
CHROME_VERSION = "130.0.0.0"

SOURCES = {
    "video_link_extractor": {
        "github_zip": "https://github.com/jrcramos/video_link_extractor/archive/refs/heads/main.zip",
    },
    "stream_detector": {"store_id": "iakkmkmhhckcmoiibcfjnooibphlobak"},
    "m3u8_sniffer": {"store_id": "akkncdpkjlfanomlnpmmolafofpnpjgn"},
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read()


def store_crx_url(ext_id):
    query = urllib.parse.urlencode({
        "response": "redirect",
        "prodversion": CHROME_VERSION,
        "acceptformat": "crx2,crx3",
        "x": f"id={ext_id}&installsource=ondemand&uc",
    })
    return "https://clients2.google.com/service/update2/crx?" + query


def strip_crx_header(data):
    """A CRX file is a small header followed by a normal ZIP. Return the ZIP part."""
    if data[:4] != b"Cr24":
        raise ValueError("not a CRX file")
    version = struct.unpack("<I", data[4:8])[0]
    if version == 2:
        pub_len, sig_len = struct.unpack("<II", data[8:16])
        offset = 16 + pub_len + sig_len
    elif version == 3:
        header_len = struct.unpack("<I", data[8:12])[0]
        offset = 12 + header_len
    else:
        raise ValueError(f"unsupported CRX version {version}")
    return data[offset:]


def unpack_zip(zip_bytes, dest):
    tmp = Path(tempfile.mkdtemp())
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(tmp)
        entries = list(tmp.iterdir())
        # GitHub ZIPs wrap everything in one folder. Use that folder if so.
        source = entries[0] if len(entries) == 1 and entries[0].is_dir() else tmp
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    ready = 0
    for name, spec in SOURCES.items():
        try:
            if "github_zip" in spec:
                zip_bytes = fetch(spec["github_zip"])
            else:
                zip_bytes = strip_crx_header(fetch(store_crx_url(spec["store_id"])))
            dest = ROOT / name
            unpack_zip(zip_bytes, dest)
            if not (dest / "manifest.json").is_file():
                raise ValueError("no manifest.json after unpack")
            print(f"[fetch] OK {name}", flush=True)
            ready += 1
        except Exception as exc:
            print(f"[fetch] SKIPPED {name}: {type(exc).__name__}: {exc}", flush=True)
    print(f"[fetch] {ready}/{len(SOURCES)} extensions ready", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
