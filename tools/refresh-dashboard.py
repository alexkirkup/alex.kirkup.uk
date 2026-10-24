#!/usr/bin/env python3
"""Rebuild the Echo sessions dashboard data from Cloudflare.

Usage: refresh-dashboard.py OUT_DIR
Writes OUT_DIR/echo_sessions.json and OUT_DIR/web_analytics_hits.json.
Needs outbound HTTPS to api.cloudflare.com with credentials injected by the proxy.
Deterministic: fixed queries, fixed join rules, rows sorted by time.
"""
import json, subprocess, sys
from datetime import datetime, timezone, timedelta

ACCOUNT = "ad019283c7a5bbad7911a1dfef0e6f87"
WA_SITE = "9782b69e43664cd1b9756cce9936f066"
API = "https://api.cloudflare.com/client/v4"
SINCE = "2026-10-17T00:00:00Z"

def curl(path, body, json_body=False):
    cmd = ["curl", "-sS", "-X", "POST", API + path, "-d", body]
    if json_body: cmd += ["-H", "Content-Type: application/json"]
    return json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)

def sql(query):
    d = curl(f"/accounts/{ACCOUNT}/analytics_engine/sql", query + " FORMAT JSON")
    return d["data"]

def ts(s): return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")

def empty(**kw):
    base = dict(visit="", pairing="", offset_min=None, local_time=None, pixel_view="no", left="no", state=None, fonts=None,
                reduced_motion=None, touch=None, moved=None, fps=None, clicks=None, seconds=None, width=None, height=None,
                dpr=None, web_analytics="no")
    base.update(kw); return base

def local(t, off): return None if off is None else (t + timedelta(minutes=off)).strftime("%Y-%m-%d %H:%M:%S")

# ---- echo_events: exact pairing by visit id -------------------------------------------------
E = sql("SELECT timestamp, blob1 type, blob2 visit, blob3 site, blob4 path, blob5 ref, blob6 country, blob7 region, blob8 city, "
        "blob9 tz, blob10 lang, blob11 device, blob12 os, blob13 browser, blob14 bot, blob15 state, blob16 fonts, blob17 rm, "
        "blob18 touch, blob19 moved, double1 off, double2 secs, double3 clicks, double4 fps, double5 w, double6 h, double7 dpr "
        "FROM echo_events ORDER BY timestamp ASC LIMIT 10000")
new = {}; views = []
for r in E:
    t = ts(r["timestamp"])
    if r["type"] == "view": views.append((t, r)); continue
    s = new.get(r["visit"])
    if s is None:
        s = new[r["visit"]] = empty(id=r["visit"], visit=r["visit"], pairing="exact", _t=t, started=iso(t), site=r["site"], path=r["path"],
            referrer=r["ref"] or "direct", country=r["country"], region=r["region"], city=r["city"], timezone=r["tz"], language=r["lang"],
            device=r["device"], browser=r["browser"], os=r["os"], bot=r["bot"] or "no")
    if r["type"] == "arrive":
        s.update(_t=t, started=iso(t), offset_min=int(r["off"])); s["local_time"] = local(t, s["offset_min"])
    if r["type"] == "leave":
        s.update(left="yes", state=r["state"] or None, fonts=r["fonts"], reduced_motion=r["rm"], touch=r["touch"], moved=r["moved"],
                 fps=r["fps"], clicks=r["clicks"], seconds=r["secs"], width=r["w"], height=r["h"], dpr=r["dpr"])
        if s["offset_min"] is None: s["offset_min"] = int(r["off"]); s["local_time"] = local(s["_t"], s["offset_min"])
# a pixel view has no visit id: attach to the nearest arrive within 5s with the same fingerprint
fp = lambda x: (x["site"], x["path"], x["city"], x["device"], x["browser"], x["os"])
for t, v in views:
    for s in new.values():
        if s["pixel_view"] == "no" and fp(s) == fp(v) and abs((s["_t"] - t).total_seconds()) <= 5:
            s["pixel_view"] = "yes"; break

# ---- legacy datasets (before echo_events): pairing is a best guess --------------------------
A = sql("SELECT timestamp, blob1 site, blob2 path, blob3 ref, blob4 country, blob5 region, blob6 city, blob7 tz, blob8 lang, blob9 device, "
        "blob10 browser, blob11 os, blob12 bot FROM echo_arrivals ORDER BY timestamp ASC LIMIT 1000")
X = sql("SELECT timestamp, blob4 country, blob6 city, blob9 device, blob10 browser, blob11 os, blob13 state, blob14 fonts, blob15 rm, "
        "blob16 touch, blob17 moved, double2 fps, double3 clicks, double4 secs, double5 w, double6 h, double7 dpr FROM echo_experiences "
        "ORDER BY timestamp ASC LIMIT 1000")
V = {r["timestamp"] for r in sql("SELECT timestamp FROM echo_views ORDER BY timestamp ASC LIMIT 1000")}
legacy = []
cutover = min((ts(r["timestamp"]) for r in E), default=None)  # echo_events took over here; older rows only exist in the legacy datasets
A = [a for a in A if cutover is None or ts(a["timestamp"]) < cutover]
for i, a in enumerate(A):
    t = ts(a["timestamp"])
    legacy.append(empty(id=f"legacy-{i+1}", pairing="guessed", _t=t, started=iso(t), site=a["site"], path=a["path"], referrer=a["ref"] or "direct",
        country=a["country"], region=a["region"], city=a["city"], timezone=a["tz"], language=a["lang"], device=a["device"], browser=a["browser"],
        os=a["os"], bot=a["bot"] or "no", pixel_view="yes" if a["timestamp"] in V else "no"))
used = set()
for e in X:
    t = ts(e["timestamp"]); best = None
    for s in legacy:
        if s["id"] in used: continue
        if (s["city"], s["device"], s["browser"], s["os"]) == (e["city"], e["device"], e["browser"], e["os"]) and s["_t"] <= t + timedelta(seconds=2):
            best = s
    if best:
        used.add(best["id"])
        best.update(left="yes", state=e["state"] or None, fonts=e["fonts"], reduced_motion=e["rm"], touch=e["touch"], moved=e["moved"],
                    fps=e["fps"], clicks=e["clicks"], seconds=e["secs"], width=e["w"], height=e["h"], dpr=e["dpr"])

sessions = sorted(legacy + list(new.values()), key=lambda s: (s["_t"], s["id"]))

# ---- Cloudflare Web Analytics, matched by minute, country and device (live host only) -------
q = ('query{viewer{accounts(filter:{accountTag:"%s"}){g:rumPageloadEventsAdaptiveGroups(limit:1000,filter:{siteTag:"%s",datetime_geq:"%s",'
     'datetime_leq:"2099-01-01T00:00:00Z"},orderBy:[datetimeMinute_ASC]){count dimensions{datetimeMinute countryName deviceType userAgentBrowser '
     'userAgentOS requestHost requestPath}}}}}') % (ACCOUNT, WA_SITE, SINCE)
groups = curl("/graphql", json.dumps({"query": q}), True)["data"]["viewer"]["accounts"][0]["g"]
hits = []
for g in groups:
    x = g["dimensions"]; mt = datetime.strptime(x["datetimeMinute"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc); match = None
    if x["requestHost"] != "localhost":
        for s in sessions:
            if s["web_analytics"] == "no" and s["device"] == x["deviceType"] and s["country"] == x["countryName"] and abs((s["_t"] - mt).total_seconds()) < 120:
                match = s; break
    if match: match["web_analytics"] = "yes"
    hits.append(dict(minute=x["datetimeMinute"], host=x["requestHost"], path=x["requestPath"], country=x["countryName"], device=x["deviceType"],
                     browser=x["userAgentBrowser"], os=x["userAgentOS"], matched_echo_session=match["id"] if match else ""))
for s in sessions: s.pop("_t")
out = sys.argv[1]
json.dump(sessions, open(f"{out}/echo_sessions.json", "w"), indent=1)
json.dump(hits, open(f"{out}/web_analytics_hits.json", "w"), indent=1)
print(f"{len(sessions)} sessions ({len(new)} exact, {len(legacy)} guessed), {len(hits)} Web Analytics hits")
