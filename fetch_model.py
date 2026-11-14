"""
fetch_model.py — download a Hugging Face model repo into models/<name>/ fast and resumably:
every large file is fetched over several parallel HTTP range connections, and an interrupted
download continues where it stopped (each part is checked by size).

    python3 fetch_model.py mflux-community/z-image-turbo-mflux-q4 models/z-image-turbo-q4
"""
import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.request

PARTS = 8                      # connections per large file
CHUNK = 1 << 20


def _get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "faceless-studio-fetch", **(headers or {})})
    return urllib.request.urlopen(req, timeout=60)


def _files(repo):
    with _get(f"https://huggingface.co/api/models/{repo}/tree/main?recursive=true") as r:
        return [(f["path"], f["size"]) for f in json.load(r) if f.get("type") == "file"]


def _part(url, path, start, end, done):
    """Download bytes [start, end] into path (resuming from what is already there)."""
    have = os.path.getsize(path) if os.path.exists(path) else 0
    want = end - start + 1
    for attempt in range(40):                  # patient: survives Wi-Fi drops and sleep
        if have >= want:
            return
        try:
            with _get(url, {"Range": f"bytes={start + have}-{end}"}) as r, open(path, "ab") as f:
                while True:
                    b = r.read(CHUNK)
                    if not b:
                        break
                    f.write(b)
                    have += len(b)
                    done[0] += len(b)
        except Exception:
            time.sleep(min(5 + attempt * 5, 60))
    if have < want:
        raise RuntimeError(f"part {path} incomplete ({have}/{want})")


def fetch(repo, dest):
    files = _files(repo)
    total = sum(s for _, s in files)
    done = [0]
    t0 = time.time()
    for rel, size in files:
        out = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.exists(out) and os.path.getsize(out) == size:
            done[0] += size
            continue
        url = f"https://huggingface.co/{repo}/resolve/main/{rel}"
        n = PARTS if size > 64 * CHUNK else 1
        step = size // n
        ranges = [(i * step, (size - 1) if i == n - 1 else (i + 1) * step - 1) for i in range(n)]
        parts = [f"{out}.part{i}" for i in range(n)]
        done[0] += sum(os.path.getsize(p) for p in parts if os.path.exists(p))
        with cf.ThreadPoolExecutor(n) as ex:
            futs = [ex.submit(_part, url, p, a, b, done) for p, (a, b) in zip(parts, ranges)]
            while not all(f.done() for f in futs):
                time.sleep(5)
                mb = done[0] / 1e6
                print(f"  {rel}: {mb / 1e3:.2f}/{total / 1e9:.2f} GB · "
                      f"{mb / max(time.time() - t0, 1):.1f} MB/s", flush=True)
            for f in futs:
                f.result()
        with open(out, "wb") as w:
            for p in parts:
                with open(p, "rb") as r:
                    while True:
                        b = r.read(16 * CHUNK)
                        if not b:
                            break
                        w.write(b)
                os.remove(p)
        if os.path.getsize(out) != size:
            raise RuntimeError(f"{rel}: size mismatch")
    print(f"DONE {repo} → {dest} ({total / 1e9:.2f} GB)", flush=True)


if __name__ == "__main__":
    fetch(sys.argv[1], sys.argv[2])
