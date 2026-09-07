#!/usr/bin/env python3
"""Refresh the Gamexpo badges dashboard from live data and write final HTML.

Credentials are read from environment variables, never hardcoded here:
  SSH_HOST, SSH_USER, SSH_PASS, DB_USER, DB_PASS

Usage: python3 update_dashboard.py
Reads template.html from the same directory, writes badges_dashboard.html.
"""
import datetime
import json
import os
import re
import select
import socketserver
import subprocess
import sys
import threading
from collections import defaultdict


def ensure_deps():
    for mod, pip_name in (("pymysql", "pymysql"), ("paramiko", "paramiko")):
        try:
            __import__(mod)
        except ImportError:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--user", "--quiet", pip_name],
                check=True,
            )


ensure_deps()
import pymysql
import paramiko

SSH_HOST = os.environ["SSH_HOST"]
SSH_USER = os.environ["SSH_USER"]
SSH_PASS = os.environ["SSH_PASS"]
DB_USER = os.environ["DB_USER"]
DB_PASS = os.environ["DB_PASS"]

DB_HOST = "127.0.0.1"
DB_PORT_REMOTE = 3306
DB_NAME = "gamexpo_prod"
LOCAL_PORT = 13306

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "template.html")
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "badges_dashboard.html")
ARTIFACT_URL = "https://claude.ai/code/artifact/788bac42-4582-4efe-a8b9-a958cd9bc91c"

CITY_NAMES = [
    "paris", "lyon", "lille", "marseille", "bordeaux", "rennes",
    "toulouse", "metz", "strasbourg", "reims", "le havre", "lehavre",
]
SPECIAL_VENUES = [
    (r"puy\s+du\s+fou", "Puy du Fou"),
    (r"portaventura", "PortAventura"),
    (r"hippodrome\s+d.?auteuil", "Paris"),
]

CATEGORY_CODES = {
    "Mandrill": "M", "Mailchimp": "C", "GAM": "G", "Web": "W",
    "Call center": "A", "Salon CSE (digital)": "S", "Badge en ligne": "B",
    "Welcomites (partenaire)": "P", "Site public": "T",
    "Campagne partenaire": "X", "Non renseigné": "N",
}


def infer_city(nom, ville_col):
    n = (nom or "").lower()
    for pattern, city in SPECIAL_VENUES:
        if re.search(pattern, n):
            return city
    for city in CITY_NAMES:
        if re.search(r"\b" + city.replace(" ", r"\s+") + r"\b", n):
            return "Le Havre" if city in ("le havre", "lehavre") else city.title()
    if ville_col:
        v = ville_col.strip().lower()
        return "Le Havre" if v in ("le havre", "lehavre") else v.title()
    return None


def infer_semester(nom):
    m = re.search(r"(\d{4})-(\d)\s*$", (nom or "").strip())
    return f"{m.group(1)}-{m.group(2)}" if m else None


def extract_campaign(o):
    if not o or not o.strip():
        return "non_renseigne"
    ol = o.lower().strip()
    if ol == "www.salonscse.digital":
        return "salonscse"
    slug = ol.split(".")[0]
    if slug == "" or slug == "www":
        slug = ol.replace(".", "_")
    m = re.match(r"^[a-z]{2,5}-\d{4}-\d-(man|chimp|gam)(\d*)$", slug)
    if m:
        return m.group(1) + m.group(2)
    for city in CITY_NAMES:
        csl = city.replace(" ", "")
        if slug.endswith("-" + csl):
            slug = slug[: -(len(csl) + 1)]
            break
    return slug if slug else "non_renseigne"


def categorize_campaign(c):
    if c == "non_renseigne":
        return "N"
    if re.match(r"^man\d*$", c) or re.search(r"-man\d*$", c):
        return "M"
    if re.match(r"^chimp\d*$", c):
        return "C"
    if re.match(r"^gam\d*$", c) or re.search(r"-gam\d*$", c):
        return "G"
    if c.startswith("web"):
        return "W"
    if c.startswith("call"):
        return "A"
    if "saloncse" in c or "salonscse" in c:
        return "S"
    if c.startswith("badge"):
        return "B"
    if "welcomites" in c:
        return "P"
    if c.startswith("site"):
        return "T"
    return "X"


def parse_d(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


class ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(transport):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                chan = transport.open_channel(
                    "direct-tcpip", (DB_HOST, DB_PORT_REMOTE), self.request.getpeername()
                )
            except Exception:
                return
            if chan is None:
                return
            while True:
                r, w, x = select.select([self.request, chan], [], [])
                if self.request in r:
                    data = self.request.recv(4096)
                    if len(data) == 0:
                        break
                    chan.send(data)
                if chan in r:
                    data = chan.recv(4096)
                    if len(data) == 0:
                        break
                    self.request.send(data)
            chan.close()
            self.request.close()

    return Handler


def open_tunnel():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(SSH_HOST, username=SSH_USER, password=SSH_PASS, timeout=20)
    transport = client.get_transport()
    server = ForwardServer(("127.0.0.1", LOCAL_PORT), make_handler(transport))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return client, server


def main():
    print("Opening SSH tunnel...")
    ssh_client, server = open_tunnel()
    try:
        conn = pymysql.connect(
            host="127.0.0.1", port=LOCAL_PORT, user=DB_USER, password=DB_PASS,
            connect_timeout=15, cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cur:
            print("Fetching events...")
            cur.execute(f"""
                SELECT e.id, e.nom, e.ville, e.date_debut, e.date_fin
                FROM {DB_NAME}.exp_evenements e
                WHERE EXISTS (SELECT 1 FROM {DB_NAME}.vis_badges b WHERE b.evenementId = CAST(e.id AS CHAR))
                ORDER BY e.date_debut
            """)
            events = cur.fetchall()
            for e in events:
                e["city"] = infer_city(e["nom"], e["ville"])
                e["semester"] = infer_semester(e["nom"])

            print("Fetching badge rows...")
            cur.execute(f"""
                SELECT evenementId AS event_id, DATE(date_creation) AS d, origine, COUNT(*) AS n
                FROM {DB_NAME}.vis_badges
                WHERE evenementId IS NOT NULL AND evenementId <> ''
                GROUP BY evenementId, DATE(date_creation), origine
            """)
            raw = cur.fetchall()
        conn.close()
    finally:
        server.shutdown()
        ssh_client.close()

    print(f"{len(events)} events, {len(raw)} raw grouped rows. Aggregating...")
    event_dates = {str(e["id"]): e["date_debut"] for e in events}
    agg = defaultdict(int)
    campaign_agg = defaultdict(int)
    for r in raw:
        camp = extract_campaign(r["origine"])
        fam_code = categorize_campaign(camp)
        agg[(r["event_id"], str(r["d"]), fam_code)] += r["n"]
        campaign_agg[(r["event_id"], camp)] += r["n"]

    daily = []
    for (eid, dstr, cat), n in agg.items():
        db_ = parse_d(event_dates.get(eid))
        d = parse_d(dstr)
        if db_ is None or d is None:
            continue
        offset = (d - db_).days
        daily.append({"e": eid, "o": offset, "c": cat, "n": n})

    campaigns = []
    for (eid, camp), n in campaign_agg.items():
        if eid not in event_dates:
            continue
        campaigns.append({"e": eid, "camp": camp, "fam": categorize_campaign(camp), "n": n})

    events_out = [
        {
            "id": str(e["id"]), "nom": e["nom"], "ville": e["city"], "semestre": e["semester"],
            "date_debut": str(e["date_debut"]),
            "date_fin": str(e["date_fin"]) if e["date_fin"] else None,
        }
        for e in events
    ]

    print(f"{len(daily)} daily rows, {len(campaigns)} campaign rows. Rendering HTML...")
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()

    html = (
        template.replace("__EVENTS__", json.dumps(events_out, ensure_ascii=False))
        .replace("__DAILY__", json.dumps(daily, ensure_ascii=False, separators=(",", ":")))
        .replace("__CATEGORIES__", json.dumps(CATEGORY_CODES, ensure_ascii=False))
        .replace("__CAMPAIGNS__", json.dumps(campaigns, ensure_ascii=False, separators=(",", ":")))
    )

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {OUTPUT_PATH} ({len(html) / 1024:.0f} KB)")
    print(f"Next step: publish {OUTPUT_PATH} as an Artifact update with url={ARTIFACT_URL}")


if __name__ == "__main__":
    main()
