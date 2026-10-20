import os, re, requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib import robotparser
from urllib.parse import urlparse, urljoin, quote
try:
    from scrapling.fetchers import DynamicFetcher as BrowserFetcher
except ImportError:
    from scrapling.fetchers import PlayWrightFetcher as BrowserFetcher
try:
    import scrapling
    print("Scrapling version", getattr(scrapling, "__version__", "inconnue"))
except Exception:
    pass

class DynamicSession:
    def __init__(self, **kw):
        self.kw = kw
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def fetch(self, url):
        try:
            return BrowserFetcher.fetch(url, **self.kw)
        except TypeError:
            return BrowserFetcher().fetch(url, **self.kw)
            
SB = os.environ["SUPABASE_URL"].strip().rstrip("/") + "/rest/v1"
KEY = os.environ["SUPABASE_SERVICE_KEY"].strip()
H = {"apikey": KEY, "Content-Type": "application/json"}
COMPETITOR_ID = os.environ.get("COMPETITOR_ID", "").strip()
USER_ID = os.environ.get("USER_ID", "").strip()

UA = "Mozilla/5.0 (compatible; DiscountCodeSniffer/1.0)"
SOURCE = "scrapling"
MAX_PAGES = 15
STATUS_OK = "ok"
STATUS_ERR = "error"
ALERT_STATUS = "pending"

KEYWORDS = re.compile(r"promo|soldes?|offres?|coupon|code|bons?-plans?|deals?|sale|reduction|remise|black-friday", re.I)
SKIP = re.compile(r"/(go|out|redirect|redir|click|track|aff|affiliate|link)/|[?&](url|redirect|ref|aff)=", re.I)
BLOCK_RE = re.compile(r"captcha-delivery|datadome|cf-chl|challenge-platform|just a moment|attention required|access denied", re.I)
QUAL = r"(?:\s+(?:promo|marraine|parrainage|réduction|de\s+réduction))?"
CODE_RE = re.compile(
    r"(?P<intro>\b(?:avec|utilise[rz]?|saisi(?:r|s|ssez)?|entre[rz]?|tape[rz]?|mon|ton|votre|use)\s+(?:le\s+|the\s+)?)?"
    r"(?:code" + QUAL + r"|promo\s*code|discount\s*code|coupon)"
    r"(?P<sep>\s*(?:est\b|:|=|-|–)?[\s«»\"“”'‘’*_`]*)"
    r"(?P<code>[A-Za-z0-9][A-Za-z0-9_-]{3,19})", re.I)
LABEL_RE = re.compile(
    r"\bcode" + QUAL + r"\s+(?:[^\s:]+\s+){0,3}?[^\s:]+\s*:\s*[«»\"“”'‘’*_`\s]*(?P<code>[A-Za-z0-9][A-Za-z0-9_-]{3,19})", re.I)
AFTER_RE = re.compile(r"\s*[:“\"«]\s*[«»\"“”'‘’*_`]*(?P<code>[A-Za-z0-9][A-Za-z0-9_-]{3,19})")
STRONG = set(":=«»\"“”'‘’*`")
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

def sb_patch(path, data):
    r = requests.patch(f"{SB}/{path}", headers=H, json=data, timeout=30)
    if r.status_code >= 300:
        print("Erreur Supabase", path, r.status_code, r.text[:300])
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

def ok_code(code):
    return code == code.upper() and re.search("[A-Z]", code) and code not in STOP

def add(found, md, code, start, end):
    ctx = re.sub(r"\s+", " ", md[max(0, start - 60): end + 140])
    p = re.search(r"(\d{1,2})\s?%", ctx)
    found.setdefault(code, {"code": code, "percent": int(p.group(1)) if p else None})

def extract(md):
    found = {}
    for m in CODE_RE.finditer(md):
        nxt = AFTER_RE.match(md, m.end())
        if nxt and ok_code(nxt.group("code")):
            add(found, md, nxt.group("code"), m.start(), nxt.end())
            continue
        code = m.group("code")
        if not ok_code(code):
            continue
        sep = m.group("sep")
        if (m.group("intro") or any(ch in STRONG for ch in sep)
                or "est" in sep.lower() or re.search(r"\d", code)):
            add(found, md, code, m.start(), m.end())
    for m in LABEL_RE.finditer(md):
        if ok_code(m.group("code")):
            add(found, md, m.group("code"), m.start(), m.end())
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

def page_text(page):
    try:
        md = page.markdown()
        if md:
            return str(md)
    except Exception:
        pass
    try:
        return str(page.get_all_text(ignore_tags=("script", "style")))
    except Exception:
        return ""

def fetch(session, url):
    page = session.fetch(url)
    status = getattr(page, "status", 200) or 200
    final = getattr(page, "url", None) or url
    text = page_text(page)
    blocked = status in (401, 403, 429, 503) or (len(text) < 3000 and bool(BLOCK_RE.search(text)))
    return page, final, text, status, blocked

def scan(session, url):
    if not allowed(url):
        return None, 0, "robots.txt interdit"
    page, final, text, status, blocked = fetch(session, url)
    if blocked:
        return None, 0, f"Site protégé contre les robots (HTTP {status}) : non scanné"
    if status >= 400:
        return None, 0, f"HTTP {status}"
    p = urlparse(final)
    origin = f"{p.scheme}://{p.netloc}"
    site = host(final)
    pages = [text]
    inner = []
        hrefs = []
    for a in page.css("a"):
        try:
            v = a.attrib.get("href")
        except Exception:
            v = None
        if v:
            hrefs.append(v)
    for h in hrefs:
        h = urljoin(final, str(h)).split("#")[0]
        
        if h.startswith("http") and host(h) == site and h not in (url, final):
            inner.append(h)
    cand = [h for h in sitemap_urls(origin) if host(h) == site and h not in (url, final)]
    prio = [h for h in cand + inner if KEYWORDS.search(h)]
    others = [h for h in inner + cand if h not in prio]
    seen, targets = set(), []
    for h in prio + others:
        if h not in seen and not SKIP.search(h) and allowed(h):
            seen.add(h)
            targets.append(h)
    for h in targets[: MAX_PAGES - 1]:
        try:
            _, f2, t2, s2, b2 = fetch(session, h)
            if not b2 and s2 < 400 and host(f2) == site:
                pages.append(t2)
        except Exception as e:
            print("page ignorée", h, e)
    print(site, ":", len(pages), "page(s) lue(s)")
    out = {}
    for md in pages:
        for code, d in extract(md).items():
            out.setdefault(code, d)
    return list(out.values()), len(pages), None

def log(c, status, action):
    try:
        sb_post("scan_logs", {"user_id": c["user_id"], "site": c["name"], "status": status,
                              "action": f"[scrapling] {action}"[:300]})
    except Exception as e:
        print("log impossible:", e)

def main():
    sel = "competitors?select=id,user_id,name,url"
    if COMPETITOR_ID:
        if not COMPETITOR_ID.isdigit():
            print("competitor_id invalide:", COMPETITOR_ID)
            return
        comps = sb_get(f"{sel}&id=eq.{COMPETITOR_ID}")
    elif USER_ID:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", USER_ID):
            print("user_id invalide:", USER_ID)
            return
        comps = sb_get(f"{sel}&user_id=eq.{USER_ID}&enabled=eq.true")
    else:
        comps = sb_get(f"{sel}&enabled=eq.true")
    print(len(comps), "concurrent(s) à scanner")
    with DynamicSession(headless=True, network_idle=True) as session:
        for c in comps:
            url = site_url(c)
            if not url:
                log(c, STATUS_ERR, "URL du concurrent manquante")
                continue
            try:
                codes, n_pages, err = scan(session, url)
                if err:
                    print(c["name"], "ECHEC", err)
                    log(c, STATUS_ERR, err)
                    continue
                now = datetime.now(timezone.utc).isoformat()
                old = sb_get(f"alerts?select=id,code&user_id=eq.{quote(str(c['user_id']))}&site=eq.{quote(c['name'])}")
                known = {o["code"]: o["id"] for o in old}
                n_new = 0
                for d in codes:
                    if d["code"] in known:
                        sb_patch(f"alerts?id=eq.{known[d['code']]}", {"last_seen_at": now})
                    else:
                        sb_post("alerts", {"user_id": c["user_id"], "site": c["name"], "code": d["code"],
                                           "discount": d["percent"] or 0, "status": ALERT_STATUS,
                                           "applied": False, "source": SOURCE, "last_seen_at": now})
                        n_new += 1
                print(c["name"], "OK", n_pages, "page(s),", len(codes), "code(s) trouvé(s), dont", n_new, "nouveau(x)")
                print("  codes :", ", ".join(d["code"] for d in codes) or "aucun")
                log(c, "found" if codes else STATUS_OK,
                    f"{len(codes)} code(s) trouvé(s), dont {n_new} nouveau(x), {n_pages} page(s) lue(s)")
            except Exception as e:
                print(c["name"], "ERREUR", e)
                log(c, STATUS_ERR, str(e))

if __name__ == "__main__":
    main()
