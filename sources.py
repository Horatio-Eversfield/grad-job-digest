"""
sources.py - read graduate roles from UK graduate job boards.

The job boards need no API keys. Each is read one polite request at a time,
within what its robots.txt allows, with a user agent that says what this tool
is. LinkedIn is the exception: it is read through Apify with your own token,
and is off until you switch it on. Every reader returns rows of the same shape:

    {source, id, company, title, type, location, url, posted_at, deadline,
     salary, description}

posted_at is an ISO timestamp, a bare date, or "" when the listing has none.
Nothing here filters or ranks. A board that fails is reported, not fatal.

Boards change their pages without notice. When one breaks, its count in the
digest footer drops to zero and the footer names the error. The reader below
is the only thing to fix.
"""

import html
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

UA = "grad-job-digest/1.0 (personal job-search tool)"
PAUSE = 0.5
TIMEOUT = 30


def _get(url, body=None, headers=None):
    h = {"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode("utf-8", "replace")
    finally:
        time.sleep(PAUSE)


def _text(s, limit=8000):
    """HTML to plain text. Unescape first: some boards send escaped markup."""
    if not s:
        return ""
    s = html.unescape(html.unescape(s))
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:limit]


def salary_text(s):
    """TARGETjobs gives salary as {lower, upper} or {ranges: [...]}."""
    if not isinstance(s, dict):
        return str(s or "")
    if s.get("ranges"):
        return ", ".join(s["ranges"])
    lo, hi = s.get("lower"), s.get("upper")
    fmt = lambda v: f"£{int(float(v)):,}"
    if lo and hi and lo != hi:
        return f"{fmt(lo)} to {fmt(hi)}"
    return fmt(lo or hi) if (lo or hi) else ""


def _row(**kw):
    row = {"source": "", "id": "", "company": "", "title": "", "type": "",
           "location": "", "url": "", "posted_at": "", "deadline": "",
           "salary": "", "description": ""}
    row.update({k: (v if v is not None else "") for k, v in kw.items()})
    return row


def _embedded_json(page, marker):
    """Decode the JSON object that follows `marker` in a page."""
    i = page.find(marker)
    if i < 0:
        raise ValueError(f"marker {marker!r} not found, page layout changed")
    i = page.index("=", i) + 1
    return json.JSONDecoder().raw_decode(page[i:].lstrip())[0]


# --------------------------------------------------------------------------
# TARGETjobs. The search endpoint its own page loads from. Exact dates.
# --------------------------------------------------------------------------
#
# The search page is JavaScript-only and loads its data from this endpoint.
# It answers only with the site's own Origin header, and "fields": null is
# needed or the dates are dropped. Its date sort is unreliable, so every open
# role for the city is pulled (a few requests) and ordered locally.

TJ_URL = "https://targetjobs.co.uk/ext/svc/inferno-search-service-1-0/search"


def targetjobs(city, types):
    groups = [
        {"conjunction": "OR", "conditions": [
            {"name": "opportunity_type", "value": t, "operator": "="}
            for t in types]},
        {"conjunction": "OR", "conditions": [
            {"name": "city", "value": city, "operator": "="}]},
        # Open roles only: deadline not already past.
        {"conjunction": "OR", "conditions": [
            {"name": "application_deadline_date", "value": ["0", "NOW"],
             "operator": "NOT BETWEEN"}]},
        {"conjunction": "OR", "conditions": [
            {"name": "type", "value": "opportunity", "operator": "="}]},
    ]
    out, offset, total = [], 0, None
    while total is None or offset < total:
        body = {"fields": None, "keys": [""], "groupBy": None,
                "conditionGroup": {"conjunction": "AND", "groups": groups},
                "facets": [], "sort": [{"field": "publish_date", "value": "desc"}],
                "conditions": [], "limit": 50, "offset": offset,
                "includePromoted": False}
        res = json.loads(_get(TJ_URL, body,
                              {"Origin": "https://targetjobs.co.uk"}))["search"]
        total = res["result_count"]
        docs = res["documents"]
        if not docs:
            break
        for d in docs:
            org = d.get("organisation") or {}
            out.append(_row(
                source="targetjobs", id=f"targetjobs:{d.get('nid')}",
                company=org.get("title") or d.get("sourceOrganisationName"),
                title=d.get("title"),
                type=", ".join(d.get("opportunityType") or []),
                location=d.get("location") or ", ".join(d.get("city") or []),
                url="https://targetjobs.co.uk" + (d.get("path") or ""),
                posted_at=d.get("createdAt"),
                deadline=(d.get("applicationDeadline") or "")[:10]
                if isinstance(d.get("applicationDeadline"), str) else "",
                salary=salary_text(d.get("salary")),
                description=_text(d.get("body")),
            ))
        offset += len(docs)
    return out


# --------------------------------------------------------------------------
# Milkround. Exact dates, but only page 1 of each search.
# --------------------------------------------------------------------------
#
# robots.txt allows /jobs/{search}/in-{place} and forbids the sort, page and
# posted-within parameters. So several narrow searches are read, 25 roles
# each. The search list lives in config.yaml.

def milkround(place, pages):
    out = []
    for slug in pages:
        page = _get(f"https://www.milkround.com/jobs/{slug}/in-{place.lower()}")
        state = _embedded_json(
            page, 'window.__PRELOADED_STATE__["app-unifiedResultlist"]')
        for x in state["searchResults"]["items"]:
            out.append(_row(
                source="milkround", id=f"milkround:{x.get('id')}",
                company=x.get("companyName"), title=x.get("title"),
                location=x.get("location"), url=x.get("url"),
                posted_at=x.get("datePosted"), salary=x.get("salary"),
                description=_text(x.get("textSnippet")),
            ))
    return out


# --------------------------------------------------------------------------
# Higherin. Newest first, 20 a page, no date on the listing.
# --------------------------------------------------------------------------
#
# The date is on each job's own page, fetched later for new roles only.

def higherin(place, types, max_pages=15):
    out = []
    for n in range(1, max_pages + 1):
        url = (f"https://higherin.com/search-jobs/graduates/{place.lower()}"
               f"?sort-by=date-desc" + (f"&page={n}" if n > 1 else ""))
        state = _embedded_json(_get(url), "__RMP_SEARCH_RESULTS_INITIAL_STATE__")
        items = state.get("data") or []
        for x in items:
            if types and x.get("jobTypeName") not in types:
                continue
            out.append(_row(
                source="higherin", id=f"higherin:{x.get('jobId')}",
                company=x.get("companyName"), title=x.get("jobTitle"),
                type=x.get("jobTypeName"), location=x.get("jobLocationNames"),
                url=x.get("url"), salary=x.get("salary"),
                deadline=x.get("deadline"),
            ))
        total = (state.get("meta") or {}).get("totalResults") or 0
        if not items or n * 20 >= total:
            break
    return out


# --------------------------------------------------------------------------
# Prospects. Every role for a city on one HTML page, no date on the listing.
# --------------------------------------------------------------------------
#
# Each city has its own page slug. Find yours by browsing to the city on
# prospects.ac.uk/browse-graduate-jobs and copying the last part of the URL.

PROSPECTS_PLACES = {"london": "london-53"}

_CARD = re.compile(
    r'card-secondary-kicker">([^<]*)<.*?card-secondary-title">([^<]*)<'
    r'.*?card-secondary-meta">(.*?)</ul>.*?href="(/graduate-jobs/[^"]+)"', re.S)


def prospects(place, types, slug=None):
    slug = slug or PROSPECTS_PLACES.get(place.lower())
    if not slug:
        raise ValueError(f"no Prospects page known for {place!r}; set "
                         f"prospects_page in config.yaml")
    page = _get(f"https://www.prospects.ac.uk/browse-graduate-jobs/"
                f"all-sectors/{slug}")
    out = []
    for kind, title, meta, path in _CARD.findall(page):
        kind = html.unescape(kind).strip()
        if types and kind.lower() not in {t.lower() for t in types}:
            continue
        bits = [_text(b) for b in re.findall(r"<li>(.*?)</li>", meta, re.S)]
        out.append(_row(
            source="prospects", id=f"prospects:{path.rsplit('-', 1)[-1]}",
            company=bits[0] if bits else "", title=_text(title), type=kind,
            location=bits[1] if len(bits) > 1 else "",
            salary=bits[2] if len(bits) > 2 else "",
            url="https://www.prospects.ac.uk" + path,
        ))
    return out


# --------------------------------------------------------------------------
# Wellfound. Startup roles, exact dates, page 1 of each role page.
# --------------------------------------------------------------------------
#
# Each /role/l/{role}/{place} page embeds about 40 listings, full advert and
# go-live time included, in __NEXT_DATA__. robots.txt allows these pages. The
# role list lives in config.yaml.
#
# Wellfound is not a graduate board, so roles that are plainly senior are left
# out here: a senior title word, two or more years' experience asked for, or
# an internship. Everything else is kept and scored.

WF_SENIOR = re.compile(r"\b(senior|sr\.?|director|head|lead|principal|vp|"
                       r"vice president|chief|staff|founding)\b", re.I)


def wellfound(place, pages):
    out, seen = [], set()
    for slug in pages:
        page = _get(f"https://wellfound.com/role/l/{slug}/{place.lower()}")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
        if not m:
            raise ValueError("__NEXT_DATA__ not found, page layout changed")
        data = (json.loads(m.group(1))["props"]["pageProps"]
                .get("apolloState", {}).get("data", {}))
        company = {}
        for v in data.values():
            if isinstance(v, dict) and v.get("__typename") == "StartupResult":
                for ref in v.get("highlightedJobListings") or []:
                    company[ref.get("__ref", "").split(":")[-1]] = v.get("name")
        for v in data.values():
            if not (isinstance(v, dict)
                    and v.get("__typename") == "JobListingSearchResult"):
                continue
            jid = str(v.get("id"))
            places = v.get("locationNames") or []
            if (jid in seen or place not in places
                    or v.get("jobType") == "internship"
                    or WF_SENIOR.search(v.get("title") or "")
                    or (v.get("yearsExperienceMin") or 0) >= 2):
                continue
            seen.add(jid)
            live = v.get("liveStartAt")
            out.append(_row(
                source="wellfound", id=f"wellfound:{jid}",
                company=company.get(jid, ""), title=v.get("title"),
                type=v.get("primaryRoleTitle"), location=", ".join(places),
                url=f"https://wellfound.com/jobs/{jid}-{v.get('slug', '')}",
                posted_at=datetime.fromtimestamp(live, timezone.utc).isoformat()
                if live else "",
                salary=v.get("compensation"),
                description=_text(v.get("description")),
            ))
    return out


# --------------------------------------------------------------------------
# LinkedIn, through Apify. Optional, needs your own Apify token.
# --------------------------------------------------------------------------
#
# LinkedIn's robots.txt forbids crawlers, so this tool never reads it
# directly. Apify's harvestapi/linkedin-job-search Actor runs the search as a
# logged-out visitor, so no LinkedIn login is involved. It is paid per role
# found (about $0.001 each) from Apify's free monthly credit, and
# max_charge_usd in config.yaml caps each run.
#
# The token is read from APIFY_TOKEN in .env and sent in a header, never the
# URL, so it cannot appear in an error message.

APIFY_RUN = ("https://api.apify.com/v2/acts/harvestapi~linkedin-job-search/"
             "run-sync-get-dataset-items")
# Boards that repost their own adverts on LinkedIn. They are read directly.
LI_REPOSTERS = {"targetjobs uk"}


def linkedin(place, cfg, token, since_hours=None):
    if not token:
        raise ValueError("APIFY_TOKEN missing from .env, see the README")
    li = cfg.get("linkedin") or {}
    # After a missed day, look back a week instead of a day, so nothing posted
    # in the gap is lost. Costs more that once, still under the cap.
    posted = "24h" if since_hours is None or since_hours <= 30 else "week"
    body = {"jobTitles": li.get("searches") or ["graduate"],
            "locations": [place], "postedLimit": posted,
            "experienceLevel": li.get("experience") or [],
            "sortBy": "date", "maxItems": int(li.get("max_items", 150))}
    url = (f"{APIFY_RUN}?maxTotalChargeUsd={float(li.get('max_charge_usd', 0.25))}"
           f"&timeout=240")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            items = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")[:160]
        except Exception:
            msg = ""
        raise RuntimeError(f"Apify HTTP {e.code} {msg}".rstrip()) from None
    out, seen = [], set()
    for x in items if isinstance(items, list) else []:
        company = (x.get("company") or {}).get("name") or ""
        jid = str(x.get("id") or "")
        if not jid or jid in seen or company.lower() in LI_REPOSTERS:
            continue
        seen.add(jid)
        out.append(_row(
            source="linkedin", id=f"linkedin:{jid}", company=company,
            title=x.get("title"), type=x.get("employmentType"),
            location=(x.get("location") or {}).get("linkedinText"),
            url=x.get("linkedinUrl"), posted_at=x.get("postedDate"),
            salary=(x.get("salary") or {}).get("text"),
            description=_text(x.get("descriptionHtml") or x.get("descriptionText")),
        ))
    return out


# --------------------------------------------------------------------------
# Job page details, for boards whose listing has no date.
# --------------------------------------------------------------------------

def job_page_details(url):
    """Read datePosted and the description from a job page's JSON-LD."""
    page = _get(url)
    for block in re.findall(
            r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            # strict=False: Prospects puts raw line breaks inside strings.
            data = json.loads(block, strict=False)
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if isinstance(d, dict) and d.get("@type") == "JobPosting":
                return d.get("datePosted") or "", _text(d.get("description"))
    return "", ""


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def fetch_all(cfg, log=print):
    """Read every enabled board. Returns (rows, counts, errors)."""
    place = cfg.get("location", "London")
    types = cfg.get("role_types", {})
    on = cfg.get("sources", {})
    readers = {
        "targetjobs": lambda: targetjobs(place, types.get("targetjobs", [])),
        "milkround": lambda: milkround(place, cfg.get("milkround_pages", [])),
        "higherin": lambda: higherin(place, types.get("higherin", [])),
        "prospects": lambda: prospects(place, types.get("prospects", []),
                                       cfg.get("prospects_page")),
        "wellfound": lambda: wellfound(place, cfg.get("wellfound_pages", [])),
        "linkedin": lambda: linkedin(place, cfg, cfg.get("_apify_token"),
                                     cfg.get("_since_hours")),
    }
    rows, counts, errors = [], {}, []
    for name, read in readers.items():
        # LinkedIn costs money, so it stays off unless switched on.
        if not on.get(name, name != "linkedin"):
            continue
        try:
            got = read()
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            counts[name] = 0
            log(f"  {name}: FAILED ({type(e).__name__}: {e})")
            continue
        counts[name] = len(got)
        rows.extend(got)
        log(f"  {name}: {len(got)}")
    return rows, counts, errors
