"""Blind-test the keychain-derived API key. Prints status + model output only."""
import json
import subprocess
import urllib.request
import urllib.error


def get_api_key():
    raw = subprocess.run(
        ["security", "find-generic-password", "-s", "ai.meta.dev.credentials",
         "-a", "meta", "-w"],
        capture_output=True, text=True, timeout=25)
    creds = json.loads(raw.stdout)
    return creds["api_key"]


def main():
    key = get_api_key()
    print("key_shape: len=%d prefix=%r" % (len(key), key[:6]))
    for label, url, data in [
        ("models", "https://api.meta.ai/v1/models", None),
        ("responses", "https://api.meta.ai/v1/responses",
         {"model": "muse-spark-1.2", "input": "reply with exactly: DIRECT_KEY_OK"}),
    ]:
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(
            url, data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     "Authorization": "Bearer %s" % key, "User-Agent": "Muse/1.0.3"},
            method="GET" if data is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                print(label, "->", r.status, r.read()[:400].decode())
        except urllib.error.HTTPError as e:
            print(label, "->", e.code, e.read()[:150].decode())
    del key


if __name__ == "__main__":
    main()
