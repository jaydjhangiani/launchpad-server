import os
import threading
import urllib.request

_INTERVAL = 14 * 60  # 14 minutes in seconds


def _ping():
    url = os.environ.get("API_URL", "")
    if not url:
        print("[keep_alive] API_URL not set, skipping ping")
        return
    try:
        with urllib.request.urlopen(url, timeout=10) as res:
            if res.status == 200:
                print("[keep_alive] GET request sent successfully")
            else:
                print(f"[keep_alive] GET request failed: {res.status}")
    except Exception as e:
        print(f"[keep_alive] Error while sending request: {e}")


def _loop():
    while True:
        threading.Event().wait(_INTERVAL)
        _ping()


def start():
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
