import os, re, asyncio, requests
import xml.etree.ElementTree as ET
from urllib import robotparser
from urllib.parse import urlparse, quote
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode

SB = os.environ["SUPABASE_URL"].strip().rstrip("/") + "/rest/v1"
KEY = os.environ["SUPABASE_SERVICE_KEY"].strip()
H = {"apikey": KEY, "Content-Type": "application/json"}
COMPETITOR_ID = os.environ.get("COMPETITOR_ID", "").strip()

UA = "Mozilla/5.0 (compatible; DiscountCodeSniffer/1.0)"
SOURCE = "crawl4ai"
MAX_PAGES = 15
STATUS_OK = "success"
STATUS_ERR = "error"
ALERT_STATUS = "pending"

KEYWORDS = re.compile(r"promo|soldes?|offres?|coupon|code|bons?-plans?|deals?|sale|reduction|remise|black-friday", re.I)
CODE_RE = re.compile(r"(?:code(?:\s+promo)?|promo\s*code|discount\s*code|coupon)\s*(?:est|:|=|-|–)?[\s«»\"“”'‘’*_`]*([A-Za-z0-9][A-Za-z0-9_-]{3,19})", re.I)
STOP = {"PROMO", "CODE", "COUPON", "SOLDES", "OFFRE", "VALIDE", "PANIER", "MERCI", "FRANCE", "LIVRAISON", "GRATUIT", "OFFERT", "DISCOUNT"}

def sb_get(path):
    r = requests.get(f"{SB}/{path}", headers=H, timeout=30)
    r.raise_for_status()
    return r.json()

def sb_post(table, data):
    r = requests.post(f"{SB}/{table}", headers=H, json=data, timeout=30)
    if r.status_code >= 300:
        print("Erreur Supabase", table, r.status_code, r.text[:300])
    r.raise_for_status()

RP = {}
def allowed(url):
    u = urlparse(url)
    key = f"{u.scheme}://{u.netloc}"
    if key not in RP:
        rp = None
        try:
            r = requests.get(key + "/robots.txt", timeout=15, headers={"User-Agent": UA})
            print("robots.txt", key, "->", r.status_code)
            if r.status_code == 200:
                rp = robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
        except Exception as e:
            print("robots.txt", key, "illisible:", e)
        RP[key] = rp
    return True if RP[key] is None else RP[key].can_fetch("*", url)

def md_of(r):
    return getattr(r.markdown, "raw_markdown", None) or str(r.markdown or "")

def extract(md):
    found = {}
    for m in CODE_RE.finditer(md):
        code = m.group(1)
        if code != code.upper() or not re.search("[A-Z]", code) or code in STOP:
            continue
        ctx = re.sub(r"\s+", " ", md[max(0, m.start() - 60): m.end() + 140])
        p = re.search(r"(\d{1,2})\s?%", ctx)
        found[code] = {"code": code, "percent": int(p.group(1)) if p else None}
    return found

def sitemap_urls(origin, limit=300):
    urls = []
    def read(u, depth=0):
        if depth > 2 or len(urls) >= limit:
            return
        try:
            r = requests.get(u, timeout=20, headers={"User-Agent": UA})
            if r.status_code != 200:
                return
            root = ET.fromstring(r.content)
        except Exception:
            return
        for loc in root.iter():
            if loc.tag.endswith("loc") and loc.text:
                t = loc.text.strip()
                if t.endswith(".xml"):
                    read(t, depth + 1)
                else:
                    urls.append(t)
    read(origin + "/sitemap.xml")
    return urls[:limit]

def site_url(c):
    u = (c.get("url") or "").strip()
    if not u and "." in (c.get("name") or ""):
        u = c["name"].strip()
    if u and not u.startswith("http"):
        u = "https://" + u
    return u or None

def host(u):
    return urlparse(u).netloc.lower().removeprefix("www.")

async def scan(crawler, url):
    if not allowed(url):
        return None, 0, "robots.txt interdit"
    cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)
    first = await crawler.arun(url=url, config=cfg)
    if not first.success:
        return None, 0, (first.error_message or "crawl échoué")[:300]
    final = getattr(first, "redirected_url", None) or url
    p = urlparse(final)
    origin = f"{p.scheme}://{p.netloc}"
    site = host(final)
    pages = [(final, md_of(first))]
    inner = []
    for l in (first.links or {}).get("internal", []):
        h = l.get("href") if isinstance(l, dict) else l
        if h and h.startswith("http") and host(h) == site and h not in (url, final):
            inner.append(h)
    cand = [h for h in sitemap_urls(origin) if host(h) == site and h not in (url, final)]
    prio = [h for h in cand + inner if KEYWORDS.search(h)]
    others = [h for h in inner + cand if h not in prio]
    seen, targets = set(), []
    for h in prio + others:
        if h not in seen and allowed(h):
            seen.add(h)
            targets.append(h)
    targets = targets[: MAX_PAGES - 1]
    if targets:
        for r in await crawler.arun_many(urls=targets, config=cfg):
            if r.success:
                pages.append((r.url, md_of(r)))
    print(site, ":", len(pages), "page(s) lue(s)")
    out = {}
    for _, md in pages:
        for code, d in extract(md).items():
            out.setdefault(code, d)
    return list(out.values()), len(pages), None

def log(c, status, action):
    try:
        sb_post("scan_logs", {"user_id": c["user_id"], "site": c["name"], "status": status,
                              "action": f"[crawl4ai] {action}"[:300]})
    except Exception as e:
        print("log impossible:", e)

async def main():
    if COMPETITOR_ID:
        if not COMPETITOR_ID.isdigit():
            print("competitor_id invalide:", COMPETITOR_ID)
            return
        comps = sb_get(f"competitors?select=id,user_id,name,url&id=eq.{COMPETITOR_ID}")
    else:
        comps = sb_get("competitors?select=id,user_id,name,url&enabled=eq.true")
    print(len(comps), "concurrent(s) à scanner")
    async with AsyncWebCrawler() as crawler:
        for c in comps:
            url = site_url(c)
            if not url:
                log(c, STATUS_ERR, "URL du concurrent manquante")
                continue
            try:
                codes, n_pages, err = await scan(crawler, url)
                if err:
                    print(c["name"], "ECHEC", err)
                    log(c, STATUS_ERR, err)
                    continue
                old = sb_get(f"alerts?select=code&user_id=eq.{quote(str(c['user_id']))}&site=eq.{quote(c['name'])}")
                known = {o["code"] for o in old}
                new = [d for d in codes if d["code"] not in known]
                for d in new:
                    sb_post("alerts", {"user_id": c["user_id"], "site": c["name"], "code": d["code"],
                                       "discount": d["percent"] or 0, "status": ALERT_STATUS,
                                       "applied": False, "source": SOURCE})
                print(c["name"], "OK", n_pages, "page(s),", len(new), "nouveau(x) code(s)")
                log(c, STATUS_OK, f"{len(new)} nouveau(x) code(s), {n_pages} page(s) lue(s)")
            except Exception as e:
                print(c["name"], "ERREUR", e)
                log(c, STATUS_ERR, str(e))

asyncio.run(main())
