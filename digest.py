"""
digest.py - a daily digest of new graduate jobs from UK job boards.

    python digest.py              read the boards, score what is new, write
                                  digest.html, email it if set up, mark seen
    python digest.py --dry-run    the same, but send nothing and mark nothing
    python digest.py --backfill   mark every open role as seen, send nothing.
                                  Use to start fresh.

"New" means not seen on any earlier run. The boards list every open role, so
comparing against what was seen before finds everything new, however many
days were missed. Roles are scored by the keywords in profile.yaml.
"""

import argparse
import io
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path

import yaml

import sources

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
SEEN = HERE / "state" / "seen.json"
OUT = HERE / "digest.html"
ARCHIVE = HERE / "digests"
FORGET_AFTER_DAYS = 60      # drop seen ids not on any board for this long
MAX_CATCHUP_HOURS = 168
UNDATED_BOARDS = ("higherin", "prospects")


def now():
    return datetime.now(timezone.utc)


def read(path):
    return io.open(path, encoding="utf-8").read()


def write(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    io.open(path, "w", encoding="utf-8").write(text)


# --------------------------------------------------------------------------
# what has been seen
# --------------------------------------------------------------------------

def load_seen():
    """{"last_run": iso, "ids": {id: iso}, "keys": {key: iso}}, or None."""
    if not SEEN.exists():
        return None
    return json.loads(read(SEEN))


def save_seen(seen, ids, keys):
    """Remember advert ids and role keys. Keys catch the same role
    reappearing on another board under a new id."""
    stamp = now().isoformat()
    cutoff = now() - timedelta(days=FORGET_AFTER_DAYS)
    out = {"last_run": stamp}
    for field, fresh in (("ids", ids), ("keys", keys)):
        kept = {k: at for k, at in (seen or {}).get(field, {}).items()
                if datetime.fromisoformat(at) >= cutoff}
        for k in fresh:
            kept[k] = stamp
        out[field] = kept
    write(SEEN, json.dumps(out, indent=0))
    return len(out["ids"])


# Words that differ between boards for the same role, ignored when matching
# one board's listing against another's.
TITLE_FILLER = {
    "graduate", "graduates", "grad", "programme", "program", "scheme",
    "uk", "united", "kingdom", "full", "time", "fulltime", "start", "intake",
    "career", "path", "and", "the", "of", "in", "for", "a", "an", "to",
    "role", "job", "opportunity"}
COMPANY_FILLER = {"group", "ltd", "limited", "plc", "llp", "uk", "the",
                  "and", "inc", "co"}


def _words(s, drop):
    s = re.sub(r"\b20\d\d\b", " ", (s or "").lower())
    return [w for w in re.sub(r"[^a-z0-9]+", " ", s).split() if w not in drop]


def role_key(r):
    """ "employer|sorted title words", or None when the title is all filler."""
    title = sorted(set(_words(r["title"], TITLE_FILLER)))
    if not title:
        return None
    return " ".join(_words(r["company"], COMPANY_FILLER)) + "|" + " ".join(title)


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------

def parse_date(value):
    """Aware UTC datetime from an ISO timestamp or a bare date, else None."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def age_hours(row):
    d = parse_date(row.get("posted_at"))
    return None if d is None else max((now() - d).total_seconds(), 0) / 3600


def window_hours(configured, seen):
    """The age limit: the configured one, stretched to cover missed days."""
    last = parse_date((seen or {}).get("last_run"))
    if last is None:
        return configured
    gap = (now() - last).total_seconds() / 3600 + 12
    return int(min(max(configured, gap), MAX_CATCHUP_HOURS))


def dedupe(rows):
    """One row per id, then one per employer and title across boards. Keep
    the copy with an exact timestamp when there is one."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    best = {}
    for r in {r["id"]: r for r in rows}.values():
        k = norm(r["company"]), norm(r["title"])
        prev = best.get(k)
        if prev is None or (len(r["posted_at"]) > 10 >= len(prev["posted_at"])):
            best[k] = r
    return list(best.values())


# --------------------------------------------------------------------------
# finding what is new
# --------------------------------------------------------------------------

def new_roles(rows, seen, cfg):
    if seen is None:
        # First run. Every open role is unseen, but almost all are old. Only
        # boards with a date on the listing can say which are fresh; the rest
        # are marked seen without opening hundreds of job pages.
        print("  first run: showing recently posted roles only")
        new = [r for r in rows if r["posted_at"]]
    else:
        # A board read for the first time is treated the same way, so adding
        # a board does not send its whole open list as "new".
        known = {i.split(":", 1)[0] for i in seen["ids"]}
        new = [r for r in rows if r["id"] not in seen["ids"]
               and (r["source"] in known or r["posted_at"])]
    new = dedupe(new)
    # A new id can still be an old role that moved boards.
    seen_keys = set((seen or {}).get("keys", {}))
    new = [r for r in new if role_key(r) not in seen_keys]
    print(f"  {len(new)} not seen before")

    # Boards whose listing has no date: open the job page for new roles only.
    errors = []
    for r in new:
        if not r["posted_at"] and r["source"] in UNDATED_BOARDS:
            try:
                r["posted_at"], desc = sources.job_page_details(r["url"])
                r["description"] = r["description"] or desc
            except Exception as e:
                errors.append(f"{r['source']} job page: {type(e).__name__}")

    limit = window_hours(cfg.get("max_age_hours", 48), seen)
    fresh = []
    for r in new:
        r["age_hours"] = age_hours(r)
        # An undated role is kept: a broken date must not hide a board.
        if r["age_hours"] is None or r["age_hours"] <= limit:
            fresh.append(r)
    print(f"  {len(fresh)} posted within {limit}h")
    return fresh, errors


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def _has(text, word):
    return re.search(r"\b" + re.escape(word.lower()) + r"\b", text) is not None


def score(r, profile):
    """0 to 100 from keyword matches. Title matches count most. A role whose
    title has an avoid word scores 0. Returns (score, matched words)."""
    title = (r["title"] or "").lower()
    body = " ".join([r["description"] or "", r["company"] or ""]).lower()
    if any(_has(title, w) for w in profile.get("avoid") or []):
        return 0, []
    words = [str(w) for w in profile.get("keywords") or []]
    in_title = [w for w in words if _has(title, w)]
    # Skills from your CV count like a keyword found in the advert text.
    extra = [w for w in _skills(profile) if w not in words]
    in_body = [w for w in words + extra if w not in in_title and _has(body, w)]
    s = (40 + 10 * (len(in_title) - 1) if in_title else 0)
    s = min(s, 60) + min(5 * len(in_body), 30)
    if r.get("age_hours") is not None and r["age_hours"] < 24:
        s += 10
    return min(s, 100), in_title + in_body


# --------------------------------------------------------------------------
# degree class (optional)
# --------------------------------------------------------------------------
#
# Many graduate adverts ask for a minimum degree class. If you set yours in
# profile.yaml, roles asking for more are hidden. The class is read from the
# advert text, so it errs towards showing a role: when an advert names more
# than one class, the lowest counts, and a sentence that waives the class
# ("2:1 not required") counts as no minimum.

DEGREE_RANK = {"first": 4, "2:1": 3, "2:2": 2, "third": 1}
_DEGREE_CLASS = [
    ("first", r"\bfirst[- ]class\b"),
    ("2:1", r"\b2[:.]\s?1\b|\bupper second\b"),
    ("2:2", r"\b2[:.]\s?2\b|\blower second\b"),
    ("third", r"\bthird[- ]class\b"),
]
# Words that show a sentence is about a degree, so "version 2.1" is ignored.
_DEGREE_CONTEXT = re.compile(
    r"degree|honours|hons|classification|class|grade|graduat|achiev|predicted"
    r"|minimum|required|equivalent", re.I)
_DEGREE_WAIVED = re.compile(
    r"not required|no minimum|regardless of|any degree class|all degree "
    r"classes|don'?t need a 2|do not need a 2|not essential", re.I)


def min_degree(text):
    """The lowest degree class an advert asks for, "waived" if it says no
    class is needed, or None if it names none."""
    found = []
    for sentence in re.split(r"(?<=[.!?\n])\s+", text or ""):
        if not _DEGREE_CONTEXT.search(sentence):
            continue
        if _DEGREE_WAIVED.search(sentence):
            return "waived"
        found += [c for c, rx in _DEGREE_CLASS if re.search(rx, sentence, re.I)]
    return min(found, key=DEGREE_RANK.get) if found else None


def degree_blocked(r, mine, hide_unstated):
    """True if the role asks for a better class than `mine`."""
    need = min_degree(r.get("description"))
    r["min_degree"] = need
    if need is None:
        return hide_unstated
    if need == "waived":        # open to every class, so never hidden
        return False
    return DEGREE_RANK[need] > DEGREE_RANK.get(mine, 0)


# Russell Group. "FTSE Russell" is an index business, so only the full phrase
# counts. A sentence that only prefers it, or waives it, never hides a role.
_RUSSELL = re.compile(r"\brussell[- ]group\b", re.I)
_PREFERRED = re.compile(r"prefer|ideally|desirable|advantage|bonus|a plus|"
                        r"nice to have", re.I)
_UNI_WAIVED = re.compile(r"not required|regardless of|don'?t need|do not need|"
                         r"any university|all universities|not essential", re.I)


def russell_group(text):
    """"required", "preferred", or None if the advert does not ask for a
    Russell Group university."""
    found = None
    for sentence in re.split(r"(?<=[.!?\n])\s+", text or ""):
        if not _RUSSELL.search(sentence):
            continue
        if _UNI_WAIVED.search(sentence):
            return None
        if not _PREFERRED.search(sentence):
            return "required"
        found = "preferred"
    return found


def pick(roles, profile, cfg):
    """Score every role, keep those at or above the threshold, best first."""
    for r in roles:
        r["russell"] = russell_group(r.get("description"))
    # YAML reads yes and no as true and false.
    if profile.get("russell_group") is False:
        before = len(roles)
        roles = [r for r in roles if r["russell"] != "required"]
        cfg["_uni_hidden"] = before - len(roles)
    mine = profile.get("degree_class")
    # YAML reads an unquoted 2:1 as the base-60 number 121.
    mine = {121: "2:1", 122: "2:2"}.get(mine, str(mine or "").strip().lower())
    if mine:
        if mine not in DEGREE_RANK:
            sys.exit(f"degree_class in profile.yaml must be one of "
                     f"{', '.join(DEGREE_RANK)}, or blank")
        hide = bool(profile.get("hide_if_class_not_stated"))
        before = len(roles)
        roles = [r for r in roles if not degree_blocked(r, mine, hide)]
        cfg["_degree_hidden"] = before - len(roles)
    if not profile.get("keywords"):
        # No keywords set: show the newest roles instead of nothing.
        for r in roles:
            r["score"], r["matched"] = None, []
        return sorted(roles, key=_age_key)[:cfg.get("roles_per_digest", 10)]
    for r in roles:
        r["score"], r["matched"] = score(r, profile)
        r["why"] = why(r, profile)
        r["watch"] = watch(r, profile)
    kept = [r for r in roles if r["score"] >= cfg.get("min_score", 40)]
    kept.sort(key=lambda r: (-r["score"], _age_key(r)))
    return kept[:cfg.get("roles_per_digest", 10)]


def _list(words):
    words = [str(w) for w in words]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _skills(profile):
    """Every skill in profile.yaml: the skills list plus those under each
    experience entry, without repeats."""
    out = [str(w) for w in profile.get("skills") or []]
    for e in profile.get("experience") or []:
        out += [str(w) for w in (e or {}).get("skills") or [] if str(w) not in out]
    return out


def why(r, profile):
    """Plain sentences on why a role fits, built from the same matches the
    score uses, so the reason and the number always agree."""
    title = (r["title"] or "").lower()
    body = " ".join([r["description"] or "", r["company"] or ""]).lower()
    words = [str(w) for w in profile.get("keywords") or []]
    skills = _skills(profile)
    target = [w for w in words if _has(title, w)]
    have = [w for w in skills if w not in target and _has(body, w)]
    also = [w for w in words if w not in target and w not in have
            and _has(body, w)]
    out = []
    q = lambda ws: _list([f'"{w}"' for w in ws])
    if target:
        out.append(f"The title has {q(target)} from your targets.")
    # Past jobs and projects whose skills the advert asks for, best two.
    # Their skills are then left out of the general skills sentence.
    used = []
    for e in profile.get("experience") or []:
        e = e or {}
        hits = [str(w) for w in e.get("skills") or [] if _has(body, str(w))]
        if hits and e.get("what"):
            used.append((len(hits), str(e["what"]).strip().rstrip("."), hits))
    covered = set()
    for _, what, hits in sorted(used, key=lambda u: -u[0])[:2]:
        out.append(f"Your {what[0].lower() + what[1:]} used {_list(hits)}, "
                   f"which the advert asks for.")
        covered.update(hits)
    have = [w for w in have if w not in covered]
    if have:
        tail = {1: "which is", 2: "both"}.get(len(have), "all")
        out.append(f"It also asks for {_list(have)}, {tail} on your CV.")
    also = [w for w in also if w not in covered]
    if also:
        out.append(f"It mentions {_list(also)} too.")
    if r.get("age_hours") is not None and r["age_hours"] < 24:
        out.append("Posted in the last day, so you can apply early.")
    return " ".join(out)


# Things in an advert a graduate may want to know before applying. Read
# from the advert text, so a downside worded another way is missed.
_WATCH = [
    (re.compile(r"\b(\d+)\s*\+?\s*(?:-\s*\d+\s*)?years?'?\s+(?:of\s+)?"
                r"(?:relevant\s+|professional\s+|commercial\s+|work\s+)?"
                r"experience", re.I),
     lambda m: f"Asks for {m.group(1)} year{'s' * (m.group(1) != '1')} of experience."),
    (re.compile(r"\b(\d+)[- ]month (?:fixed[- ]term )?contract|\bfixed[- ]term\b"
                r"|\btemporary\b", re.I),
     lambda m: (f"{m.group(1)}-month contract, not permanent." if m.group(1)
                else "Fixed-term or temporary, not permanent.")),
    (re.compile(r"\b([1-5])\s+days?\s+(?:a|per)\s+week\s+(?:in|on[- ]site|at)\b", re.I),
     lambda m: f"In the office {m.group(1)} days a week."),
    (re.compile(r"commission[- ]only|\bOTE\b"),
     lambda m: "Part of the pay is commission."),
    (re.compile(r"driving licen[cs]e", re.I),
     lambda m: "Needs a driving licence."),
    (re.compile(r"\bweekend work|\bwork(?:ing)? (?:some |alternate )?weekends\b|\bshift (?:work|pattern)|\bnight shifts?\b", re.I),
     lambda m: "Involves shift or weekend work."),
    (re.compile(r"\bunpaid\b", re.I),
     lambda m: "Unpaid."),
]


def watch(r, profile):
    """Downsides found in the advert, plus any of your own watch_for words."""
    text = r["description"] or ""
    out = []
    for rx, say in _WATCH:
        m = rx.search(text)
        if m and say(m) not in out:
            out.append(say(m))
    if r.get("russell") == "required":
        out.append("Asks for a Russell Group university.")
    elif r.get("russell") == "preferred":
        out.append("Prefers a Russell Group university.")
    mine = [str(w) for w in profile.get("watch_for") or [] if _has(text.lower(), str(w))]
    if mine:
        out.append(f"Mentions {_list([chr(34) + w + chr(34) for w in mine])}, "
                   f"which you flagged.")
    return " ".join(out)


def _age_key(r):
    return r["age_hours"] if r.get("age_hours") is not None else 1e9


# --------------------------------------------------------------------------
# the digest
# --------------------------------------------------------------------------

FONT = "font-family:Segoe UI,Helvetica,Arial,sans-serif;"
INK, MUTED, LINE, GOOD = "#1d1d1f", "#6e6e73", "#e5e5ea", "#1f7a4d"
AMBER = "#e0a030"


def when(age):
    if age is None:
        return ""
    if age < 1:
        return "posted under 1h ago"
    if age < 48:
        return f"posted {int(round(age))}h ago"
    return f"posted {int(age // 24)} days ago"


def card(p):
    salary = sources.salary_text(p["salary"]).strip()
    if salary.lower() in ("unspecified", "competitive", "n/a"):
        salary = ""
    meta = " &middot; ".join(escape(x) for x in [
        p["company"], p["location"], salary,
        f"closes {p['deadline']}" if p["deadline"] else "",
        when(p.get("age_hours")), p["source"]] if x)
    # Only web links go in the page, so a bad listing cannot plant a
    # javascript: link.
    url = p["url"] or ""
    url = escape(url if url.lower().startswith(("https://", "http://")) else "#")
    badge = (f"<td width='60' valign='top' style='padding:18px 0 18px 18px'>"
             f"<div style='width:46px;height:46px;line-height:46px;"
             f"border-radius:23px;background:{GOOD};color:#fff;"
             f"text-align:center;{FONT}font-size:17px;font-weight:700'>"
             f"{p['score']}</div></td>") if p["score"] is not None else ""
    # Degree class as a chip: only when the advert says something about it.
    need = p.get("min_degree")
    chip = ("Any degree class" if need == "waived"
            else f"Asks for a {need} or above" if need else "")
    chip = (f"<span style='display:inline-block;margin:0 0 0 10px;padding:3px 8px;"
            f"border:1px solid {LINE};border-radius:10px;{FONT}font-size:12px;"
            f"color:{MUTED}'>{escape(chip)}</span>") if chip else ""
    matched = (f"<p style='margin:12px 0 0;padding:0 0 0 10px;border-left:3px "
               f"solid {GOOD};{FONT}font-size:14px;line-height:1.5;color:{INK}'>"
               f"{escape(p['why'])}</p>" if p.get("why") else "")
    matched += (f"<p style='margin:10px 0 0;padding:0 0 0 10px;border-left:3px "
                f"solid {AMBER};{FONT}font-size:13px;line-height:1.45;"
                f"color:{MUTED}'><strong style='color:{INK}'>Watch:</strong> "
                f"{escape(p['watch'])}</p>" if p.get("watch") else "")
    return f"""
<tr><td style="padding:0 0 14px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
 style="background:#fff;border:1px solid {LINE};border-radius:10px"><tr>
 {badge}
 <td valign="top" style="padding:16px 18px 18px 14px">
  <a href="{url}" style="{FONT}font-size:16px;font-weight:600;line-height:1.3;
   color:{INK};text-decoration:none">{escape(p['title'])}</a>
  <p style="margin:4px 0 0;{FONT}font-size:13px;line-height:1.4;color:{MUTED}">{meta}</p>
  {matched}
  <p style="margin:14px 0 0"><a href="{url}" style="display:inline-block;
   padding:8px 16px;background:{INK};border-radius:6px;{FONT}font-size:13px;
   font-weight:600;color:#fff;text-decoration:none">View advert</a>{chip}</p>
 </td>
</tr></table>
</td></tr>"""


def render(picks, counts, errors, n_new, hidden=()):
    date = now().strftime("%A %d %B")
    n = len(picks)
    lead = (f"{n} new role{'s' * (n != 1)} worth a look" if picks
            else "Nothing new matched your profile today.")
    # The footer matters: a broken board and a quiet day would otherwise
    # look the same.
    foot = (f"Checked {sum(counts.values()):,} roles across {len(counts)} "
            f"boards. {n_new} were new, {n} shown."
            + "".join(" " + h for h in hidden))
    problems = (f"<p style='margin:8px 0 0;color:#c0392b;font-weight:600'>"
                f"Problems: {escape('; '.join(errors))}</p>" if errors else "")
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Graduate jobs, {date}</title></head>
<body style="margin:0;padding:0;background:#f2f2f5">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
 style="background:#f2f2f5"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
 style="max-width:600px">
<tr><td style="padding:0 4px 18px">
 <p style="margin:0;{FONT}font-size:13px;color:{MUTED}">{date}</p>
 <p style="margin:2px 0 0;{FONT}font-size:22px;font-weight:700;color:{INK}">Graduate jobs</p>
 <p style="margin:4px 0 0;{FONT}font-size:14px;color:{MUTED}">{escape(lead)}</p>
</td></tr>
{''.join(card(p) for p in picks)}
<tr><td style="padding:6px 4px 0;{FONT}font-size:12px;line-height:1.5;color:{MUTED}">
 {escape(foot)}{problems}
</td></tr>
</table></td></tr></table>
</body></html>"""


# --------------------------------------------------------------------------
# email (optional)
# --------------------------------------------------------------------------

def load_env():
    """Settings from .env, if there is one, then the real environment."""
    env, path = {}, HERE / ".env"
    if path.exists():
        for line in read(path).splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("\"'")
    env.update({k: v for k, v in os.environ.items() if k.startswith("SMTP_")
                or k in ("EMAIL_TO", "APIFY_TOKEN")})
    return env


def send_email(html, subject):
    """Returns None when email is not set up, True on a send, False on a
    failure. Passwords are never printed."""
    env = load_env()
    user, pw, to = env.get("SMTP_USER"), env.get("SMTP_PASSWORD"), env.get("EMAIL_TO")
    if not (user and pw and to):
        return None
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content("This digest is HTML. View it in an HTML-capable client.")
    msg.add_alternative(html, subtype="html")
    try:
        host = env.get("SMTP_HOST") or "smtp.gmail.com"
        with smtplib.SMTP_SSL(host, int(env.get("SMTP_PORT") or 465),
                              timeout=30) as s:
            # Google shows app passwords in spaced blocks; drop the spaces.
            s.login(user, pw.replace(" ", ""))
            s.send_message(msg)
    except Exception as e:
        print(f"  email failed ({type(e).__name__})")
        return False
    print("  email sent")
    return True


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def run(cfg, profile, dry_run=False):
    seen = load_seen()
    print("reading the boards")
    rows, counts, errors = sources.fetch_all(cfg)
    fresh, page_errors = new_roles(rows, seen, cfg)
    errors += page_errors
    picks = pick(fresh, profile, cfg)
    hidden = []
    if cfg.get("_degree_hidden") is not None:
        print(f"  {cfg['_degree_hidden']} hidden for asking a higher degree class")
        if cfg["_degree_hidden"]:
            hidden.append(f"{cfg['_degree_hidden']} hidden for asking a higher "
                          f"degree class.")
    if cfg.get("_uni_hidden") is not None:
        print(f"  {cfg['_uni_hidden']} hidden for asking for a Russell Group "
              f"university")
        if cfg["_uni_hidden"]:
            hidden.append(f"{cfg['_uni_hidden']} hidden for asking for a "
                          f"Russell Group university.")

    html = render(picks, counts, errors, len(fresh), hidden)
    write(OUT, html)
    print(f"  {len(picks)} shown, digest written to {OUT}")
    if dry_run:
        print("  dry run: nothing sent, nothing marked seen")
        return
    n = len(picks)
    subject = (f"Graduate jobs: {n} new role{'s' * (n != 1)}" if picks
               else "Graduate jobs: nothing new today")
    if send_email(html, subject) is False:
        print("  nothing marked seen, so the next run includes these again")
        return
    write(ARCHIVE / f"{now():%Y-%m-%d}.html", html)
    # Every role read today is marked seen, not only those shown: a role
    # passed over today must not come back tomorrow.
    total = save_seen(seen, [r["id"] for r in rows],
                      {k for k in map(role_key, rows) if k})
    print(f"  marked seen, {total} ids on record")


def backfill(cfg):
    rows, counts, errors = sources.fetch_all(cfg)
    if errors:
        print("  a board failed; nothing marked seen. Fix it and rerun.")
        return
    n = save_seen(load_seen(), [r["id"] for r in rows],
                  {k for k in map(role_key, rows) if k})
    print(f"  every open role marked seen, {n} ids on record")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load(read(HERE / "config.yaml")) or {}
    path = HERE / "profile.yaml"
    if not path.exists():
        sys.exit("profile.yaml not found. Copy profile.example.yaml to "
                 "profile.yaml and add your keywords.")
    profile = yaml.safe_load(read(path)) or {}
    # For the LinkedIn reader. Held in memory only, never printed or written.
    cfg["_apify_token"] = load_env().get("APIFY_TOKEN", "")
    last = parse_date((load_seen() or {}).get("last_run"))
    cfg["_since_hours"] = (now() - last).total_seconds() / 3600 if last else None
    if args.backfill:
        backfill(cfg)
    else:
        run(cfg, profile, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
