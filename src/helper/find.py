"""`helper find`: postings from the last day, from everywhere helper can read in public (Vatsal, 2026-11-16).

Read-only. It reads each company's public job feed on Greenhouse, Lever, Ashby, Workday and SmartRecruiters, and the
SimplifyJobs lists on GitHub, which cover every system (company sites, Oracle, Eightfold, Taleo, iCIMS...). Companies
come from data/sources.yaml (a public list), Vatsal's tracker history (read at run time, never written anywhere) and
the lists themselves. LinkedIn, Indeed, Glassdoor and Handshake aren't searched: they turn automated readers away and
their terms forbid it.

What comes back is filtered to early-career software, data and ML roles in the US that he hasn't applied to and that
aren't on the job hunt's list, then checked for eligibility the same way `helper apply --dry-run` does. Picking the
best fits is Claude's job in the session (CLAUDE.md, "Unattended job hunt").
"""

from __future__ import annotations

import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

from .ats import clean_url, detect_ats, job_id_from_url, normalize_url
from .config import Paths, load_yaml

# --------------------------------------------------------------------------- what counts

EARLY = re.compile(r"(?i)\bintern(ship)?s?\b|\bco-?op\b|new[- ]grad|university grad|\bgraduate\b|early[- ]career|"
                   r"entry[- ]level|\b20(26|27)\b|\bjunior\b|\bassociate\b|\b(engineer|developer|scientist|analyst)"
                   r"\s*(i|1)\b|\blevel (i|1)\b|apprentice")
FIELD = re.compile(r"(?i)software|developer|programmer|\bdata\b|analytics|machine learning|\bml\b|\bai\b|"
                   r"artificial intelligence|computer vision|\bnlp\b|(data|research|applied|computer) scien|quant|"
                   r"cloud|cyber|security engineer|back[- ]?end|front[- ]?end|full[- ]?stack|platform engineer|"
                   r"infrastructure|devops|\bsre\b|site reliability|\bweb\b|mobile|\bios\b|android|"
                   r"information technology|\bIT\b|digital engineer|automation|robotics software|embedded software|"
                   # autonomy teams write the software that drives or flies (Neros's "Autonomy Intern", 2026-11-22)
                   r"\bautonom(y|ous)\b|"
                   r"forward deployed|solutions? engineer|"
                   # Vatsal, 2026-11-17: "i have a wide range of roles, i can do supply chain as well"
                   r"supply chain|operations analyst|business analyst|product analyst|"
                   # product management: 17 of his sent applications are PM roles; First Resonance's "Product
                   # Management Intern" was dropped by the title (2026-11-23)
                   r"product manag")
NOT_FIELD = re.compile(r"(?i)mechanical|electrical|civil|chemical|environmental|manufactur|technician|propulsion|"
                       r"structural|optical|"
                       # packaging engineers, not Calyx's "Junior Software Developer - Print & Packaging" (2026-11-23)
                       r"packaging (engineer|technician|specialist|design|development)|industrial|nurs|sales|recruit|"
                       r"account (executive|"
                       r"manager)|legal|hardware|fpga|asic|\brf\b|power systems|battery|process engineer|quality "
                       r"engineer|field (service )?engineer|warehouse|construction|mechatronic|geotech|hvac|plumbing|"
                       r"water resources|plant engineer|business development|clinical|photonics|laser|electro-optic|"
                       r"displays engineer|watchlist|technical support|help ?desk|customer success|"
                       # research roles: Vatsal doesn't apply to them (2026-11-17, TRI's "Machine Learning Research")
                       r"\bresearch(er|ers)?\b")
# "(?<!product )manager": an associate product manager or product manager intern is early career (2026-11-23)
SENIOR = re.compile(r"(?i)\b(senior|sr\.?|staff|principal|lead|(?<!product )manager|director|head of|vp|architect|ii|iii|iv|"
                    r"phd|ph\.d|postdoc|post[- ]doc|high school|mba|bachelors?|undergrad(uate)?|secret|clearance|"
                    r"ts/sci|polygraph)\b")
_STATES = ("AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|"
           "OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC")
# states written out: Garmin's "Salem, Oregon" read as outside the U.S. (its Software Engineer 1, 2026-11-22)
_STATE_NAMES = ("alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|"
                "idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|"
                "minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
                "north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
                "south dakota|tennessee|texas|utah|vermont|virginia|west virginia|wisconsin|wyoming|"
                "district of columbia")
# "SF" and "Bay Area": the lists write Benchling's San Francisco internship as "SF" (2026-11-23)
US = re.compile(rf"(?i)\b(united states|usa|u\.s\.|us\b|remote|new york|nyc|san francisco|sf|bay area|seattle|"
                rf"bellevue|redmond|"
                rf"boston|austin|chicago|los angeles|mountain view|palo alto|menlo park|sunnyvale|san jose|san mateo|"
                rf"redwood city|denver|atlanta|washington|miami|philadelphia|pittsburgh|portland|salt lake|"
                rf"\d+ locations|{_STATE_NAMES})\b|\b({_STATES})\b|US-")
NOT_US = re.compile(r"(?i)\b(canada|toronto|vancouver|montreal|ottawa|united kingdom|\buk\b|london|ireland|dublin|"
                    r"germany|berlin|munich|poland|india|bengaluru|bangalore|hyderabad|pune|singapore|china|shanghai|"
                    r"beijing|japan|tokyo|israel|tel aviv|netherlands|amsterdam|brazil|australia|sydney|france|"
                    r"(?<!new )mexico|"  # New Mexico is a state
                    r"paris|spain|taiwan|korea|seoul|philippines|malaysia|vietnam|romania|czech|hungary|costa rica|"
                    r"switzerland|zurich|sweden|denmark|uae|dubai|edinburgh|glasgow|manchester|belfast|cork|krakow|"
                    r"warsaw|prague|bucharest|budapest|madrid|barcelona|milan|lisbon|stockholm|copenhagen|vienna|"
                    r"brussels|chennai|mumbai|delhi|gurgaon|gurugram|noida|kolkata|manila|kuala lumpur|shenzhen|"
                    r"hangzhou|taipei|hsinchu|osaka|melbourne|auckland|sao paulo|bogota|buenos aires|guadalajara|"
                    r"monterrey|heredia|"
                    # NVIDIA's "Armenia, Yerevan; Armenia, Remote" read as remote U.S. (2026-11-23). Names that are
                    # also U.S. places (Georgia, Athens, Lima, Lebanon, Belgrade) stay out
                    r"armenia|yerevan|ukraine|kyiv|kiev|lviv|serbia|bulgaria|sofia|croatia|zagreb|slovakia|bratislava|"
                    r"slovenia|ljubljana|estonia|tallinn|latvia|riga|lithuania|vilnius|finland|helsinki|norway|oslo|"
                    r"austria|belgium|luxembourg|portugal|porto|italy|rome|turkey|türkiye|istanbul|ankara|egypt|"
                    r"south africa|cape town|johannesburg|nigeria|lagos|kenya|nairobi|pakistan|karachi|lahore|"
                    r"bangladesh|dhaka|sri lanka|colombo|nepal|indonesia|jakarta|thailand|bangkok|hong kong|"
                    r"new zealand|argentina|chile|colombia|medellin|peru|uruguay|montevideo|saudi arabia|riyadh|"
                    r"qatar|doha|kazakhstan|almaty)\b")
# a Workday link names its location first: /job/GB---Edinburgh/..., /job/CA-ON---Toronto/..., /job/IND-Hyderabad/...
NOT_US_PATH = re.compile(r"(?i)/job/(GB|UK|IN|IND|CN|CHN|DE|FR|IE|PL|SG|JP|IL|NL|MX|BR|AU|CA|CR|PH|MY|RO|HU|CZ|ES|IT|"
                         r"KR|TW|VN|CH|SE|DK|AE|ARG|COL|CHL)[-_]")
# Gender tags that German, Austrian, Swiss and French postings put in titles: "(m/f/d)", "(m/w/d)", "(H/F)"
NOT_US_TITLE = re.compile(r"(?i)\((?:[mwfdhx]\s*/\s*){1,3}[mwfdhx]\)|\(all genders\)")
# Errors that say nothing about the posting: it stays unchecked (not skipped) and the next search finds it again
TRANSIENT = re.compile(r"(?i)couldn't reach|connection|ssl|timed? ?out|timeout|temporar|try again|too many requests"
                       r"|\b429\b|\b50[0-4]\b")
REUSED_AFTER_DAYS = 180  # a Greenhouse posting first published this long ago and refreshed today was reused (Octaura)
WORKDAY_TERMS = ("intern", "new grad", "early career", "entry level", "university")  # ~5 searches a company site
SMARTRECRUITERS_TERMS = ("intern", "graduate", "entry", "associate")
YOU_APPLY = {"smartrecruiters", "taleo", "successfactors", "ukg",  # helper never fills these (rule 8): `helper assist`
             "lever"}  # Lever's bot check rejects submits from helper's window (TRI, Quest; Vatsal, 2026-11-17)


@dataclass
class Found:
    url: str
    company: str
    title: str
    location: str = ""
    ats: str = ""
    source: str = ""
    posted_at: Optional[str] = None
    job_id: Optional[str] = None
    how: str = ""  # "helper fills" or "you apply"
    eligibility: dict[str, Any] = field(default_factory=dict)
    error: str = ""  # why it couldn't be read this time (a network hiccup): left unchecked, so it comes back


@dataclass
class Sources:
    greenhouse: set[str] = field(default_factory=set)
    lever: set[str] = field(default_factory=set)
    ashby: set[str] = field(default_factory=set)
    workday: set[tuple[str, str, str]] = field(default_factory=set)  # (host, tenant, site)
    smartrecruiters: set[str] = field(default_factory=set)
    lists: list[str] = field(default_factory=list)

    def add_link(self, url: str) -> None:
        """Learn a company's feed from one of its posting links."""
        from .adapters import ashby, greenhouse, lever, smartrecruiters, workday

        try:
            if (ref := greenhouse.parse_ref(url)) and ref.board:
                self.greenhouse.add(ref.board.lower())
            elif (lv := lever.parse_ref(url)):
                self.lever.add(lv[0].lower())
            elif (ab := ashby.parse_ref(url)) and ab[0]:
                self.ashby.add(ab[0])
            elif (wd := workday.parse_ref(url)) and not wd.recruiting:
                self.workday.add((wd.host, wd.tenant, wd.site))
            elif (sr := smartrecruiters.parse_ref(url)):
                self.smartrecruiters.add(sr.company)
        except Exception:
            return

    def counts(self) -> str:
        return (f"{len(self.greenhouse)} Greenhouse boards, {len(self.lever)} Lever, {len(self.ashby)} Ashby, "
                f"{len(self.workday)} Workday sites, {len(self.smartrecruiters)} SmartRecruiters, {len(self.lists)} lists")


def load_sources(paths: Paths, tracker_urls: Iterable[str]) -> Sources:
    data = load_yaml(paths.root / "data" / "sources.yaml") or {}
    src = Sources(greenhouse={b.lower() for b in data.get("greenhouse", [])}, lever=set(data.get("lever", [])),
                  ashby=set(data.get("ashby", [])), smartrecruiters=set(data.get("smartrecruiters", [])),
                  lists=list(data.get("lists", [])))
    for w in data.get("workday", []):
        src.workday.add((w["host"], w["tenant"], w["site"]))
    for url in tracker_urls:  # the companies he has applied to before (read here, never written anywhere)
        if url:
            src.add_link(url)
    return src


# --------------------------------------------------------------------------- filters


def title_blocked(title: str) -> bool:
    """A word in the title that rules the posting out: seniority or a degree level, another discipline, a non-U.S.
    gender tag."""
    return bool(SENIOR.search(title) or NOT_FIELD.search(title) or NOT_US_TITLE.search(title))


def wanted(title: str, location: str, url: str = "") -> bool:
    if not title or title_blocked(title):
        return False
    if url and (NOT_US_PATH.search(url) or NOT_US.search(urlparse(url).path.replace("-", " "))):
        return False
    if not (EARLY.search(title) and FIELD.search(title)):
        return False
    if location and NOT_US.search(location) and not re.search(r"(?i)united states|\bUSA\b|\bUS\b", location):
        return False
    return not location or bool(US.search(location))


def _ts(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):  # Lever: milliseconds
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        text = str(value).replace("Z", "+00:00")
        when = datetime.fromisoformat(text)
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    except (ValueError, OSError):
        return None


def workday_req(path: str) -> str:
    """'.../Intern---Data-Science--SMAI_JR117444' -> 'jr117444' (the req id helper stores, lowercased)."""
    m = re.search(r"_([A-Za-z0-9]+)(?:-\d+)?/?$", path or "")
    return m.group(1).lower() if m else ""


# --------------------------------------------------------------------------- sources


def scan_greenhouse(board: str, since: datetime, get: Callable[..., Any]) -> list[Found]:
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")
    if r is None:
        return []
    out = []
    for j in r.get("jobs", []):
        title = j.get("title") or ""
        first, updated = _ts(j.get("first_published")), _ts(j.get("updated_at"))
        when = first
        if (first and updated and updated >= since and (updated - first).days >= REUSED_AFTER_DAYS
                and EARLY.search(title)):
            when = updated  # reused for a new season: Octaura's 2027 intern posting kept its 2025 date
        if not when or when < since:
            continue
        out.append(Found(url=f"https://job-boards.greenhouse.io/{board}/jobs/{j['id']}",
                         company=j.get("company_name") or board, title=title,
                         location=(j.get("location") or {}).get("name") or "", ats="greenhouse", source="greenhouse",
                         posted_at=when.isoformat(), job_id=str(j["id"])))
    return out


def scan_lever(company: str, since: datetime, get: Callable[..., Any]) -> list[Found]:
    items = get(f"https://api.lever.co/v0/postings/{company}?mode=json")
    if not isinstance(items, list):
        return []
    out = []
    for j in items:
        when = _ts(j.get("createdAt"))
        if not when or when < since:
            continue
        cats = j.get("categories") or {}
        out.append(Found(url=j.get("hostedUrl") or f"https://jobs.lever.co/{company}/{j.get('id')}",
                         company=company, title=j.get("text") or "", location=cats.get("location") or "",
                         ats="lever", source="lever", posted_at=when.isoformat(), job_id=j.get("id")))
    return out


def scan_ashby(org: str, since: datetime, get: Callable[..., Any]) -> list[Found]:
    data = get(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("jobs", []):
        when = _ts(j.get("publishedAt"))
        if not when or when < since or j.get("isListed") is False:
            continue
        out.append(Found(url=j.get("jobUrl") or f"https://jobs.ashbyhq.com/{org}/{j.get('id')}", company=org,
                         title=j.get("title") or "", location=j.get("location") or "", ats="ashby", source="ashby",
                         posted_at=when.isoformat(), job_id=j.get("id")))
    return out


def scan_workday(site: tuple[str, str, str], since: datetime, post: Callable[..., Any]) -> list[Found]:
    host, tenant, name = site
    today = datetime.now(timezone.utc)
    out: dict[str, Found] = {}
    for term in WORKDAY_TERMS:
        data = post(f"https://{host}/wday/cxs/{tenant}/{name}/jobs",
                    {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term})
        if not isinstance(data, dict):
            return list(out.values())
        for j in data.get("jobPostings", []):
            posted = (j.get("postedOn") or "").lower()
            if "today" in posted:
                when = today
            elif "yesterday" in posted:
                when = today - timedelta(days=1)
            else:
                continue
            if when < since - timedelta(days=1):
                continue
            path = j.get("externalPath") or ""
            url = f"https://{host}/{name}{path}"
            out.setdefault(url, Found(url=url, company=tenant, title=j.get("title") or "",
                                      location=j.get("locationsText") or "", ats="workday", source="workday",
                                      posted_at=when.date().isoformat(), job_id=workday_req(path) or None))
    return list(out.values())


def scan_smartrecruiters(company: str, since: datetime, get: Callable[..., Any]) -> list[Found]:
    out: dict[str, Found] = {}
    for term in SMARTRECRUITERS_TERMS:
        data = get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&q={term}")
        if not isinstance(data, dict):
            return list(out.values())
        for j in data.get("content", []):
            when = _ts(j.get("releasedDate"))
            if not when or when < since:
                continue
            loc = j.get("location") or {}
            url = f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}"
            out.setdefault(url, Found(url=url, company=(j.get("company") or {}).get("name") or company,
                                      title=j.get("name") or "", location=loc.get("fullLocation") or ", ".join(
                                          x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x),
                                      ats="smartrecruiters", source="smartrecruiters", posted_at=when.isoformat(),
                                      job_id=str(j.get("id"))))
    return list(out.values())


def parse_list(text: str, source: str) -> tuple[list[Found], list[str]]:
    """SimplifyJobs-style README tables: rows aged 0d or 1d, and every link (to learn feeds). A list can lag: New-Grad
    didn't update from Nov 18 to Nov 21 4:30pm, so Friday's postings came in already 1d old and no search saw them
    (2026-11-22). The search drops the rows an earlier one saw (dedupe)."""
    found, links = [], []
    company = ""
    for row in re.findall(r"<tr>(.*?)</tr>", text, re.S):
        cells = re.findall(r"<td>(.*?)</td>", row, re.S)
        if len(cells) < 5:
            continue
        first = re.sub(r"<[^>]+>", "", cells[0]).replace("🔥", "").strip()
        if first != "↳":
            company = html.unescape(first)
        m = re.search(r'href="([^"]+)"[^>]*>\s*<img[^>]+alt="Apply"', cells[3])
        if not m:
            continue  # closed postings have no Apply link
        link = clean_url(html.unescape(m.group(1)))
        links.append(link)
        age = re.fullmatch(r"([01])d", re.sub(r"<[^>]+>", "", cells[-1]).strip())
        if not age:
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", cells[1])).strip()
        # cities are split by <br>, </br> or <br/>: JP Morgan's "Columbus, OH</br>NYC" read as "OHNYC" (2026-11-23)
        location = re.sub(r"<[^>]+>", "", html.unescape(re.sub(r"</?br\s*/?>", "; ", cells[2]))).strip()
        posted = datetime.now(timezone.utc).date() - timedelta(days=int(age.group(1)))
        found.append(Found(url=link, company=company, title=re.sub(r"\s*🎓|\s*🛂|\s*🇺🇸", "", title).strip(),
                           location=location, ats=detect_ats(link), source=source, posted_at=posted.isoformat()))
    return found, links


# --------------------------------------------------------------------------- the search


class Http:
    """Plain, honest requests (helper never disguises itself), with failures read as 'nothing there'."""

    def __init__(self) -> None:
        import requests

        self.session = requests.Session()

    def get(self, url: str) -> Any:
        try:
            r = self.session.get(url, timeout=25)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    def post(self, url: str, payload: dict[str, Any]) -> Any:
        try:
            r = self.session.post(url, json=payload, headers={"Accept": "application/json"}, timeout=25)
        except Exception:
            return None
        return r.json() if r.status_code == 200 else None

    def text(self, url: str) -> Optional[str]:
        try:
            r = self.session.get(url, timeout=40)
        except Exception:
            return None
        return r.text if r.status_code == 200 else None


def search(paths: Paths, hours: float, known_urls: set[str], known_ids: set[str], tracker_urls: Iterable[str],
           http: Optional[Http] = None, workers: int = 8) -> tuple[list[Found], Sources]:
    http = http or Http()
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    sources = load_sources(paths, tracker_urls)
    found: list[Found] = []
    for url in sources.lists:
        text = http.text(url)
        if not text:
            continue
        rows, links = parse_list(text, source="list: " + "/".join(urlparse(url).path.split("/")[1:3]))
        found += rows
        for link in links:
            sources.add_link(link)
    jobs: list[Callable[[], list[Found]]] = []
    jobs += [lambda b=b: scan_greenhouse(b, since, http.get) for b in sorted(sources.greenhouse)]
    jobs += [lambda c=c: scan_lever(c, since, http.get) for c in sorted(sources.lever)]
    jobs += [lambda o=o: scan_ashby(o, since, http.get) for o in sorted(sources.ashby)]
    jobs += [lambda s=s: scan_workday(s, since, http.post) for s in sorted(sources.workday)]
    jobs += [lambda c=c: scan_smartrecruiters(c, since, http.get) for c in sorted(sources.smartrecruiters)]
    with ThreadPoolExecutor(workers) as pool:
        for batch in pool.map(lambda job: _safe(job), jobs):
            found += batch
    return dedupe([f for f in found if wanted(f.title, f.location, f.url)], known_urls, known_ids), sources


def _safe(job: Callable[[], list[Found]]) -> list[Found]:
    try:
        return job()
    except Exception:
        return []


_JOB_IN_PATH = re.compile(r"\d{4,}|[0-9a-f]{8}-[0-9a-f]{4}-", re.I)


def _path_key(url: str) -> str:
    """The link's path when it names one job (a job number or id in it), else ''. A page address that every job on a
    site shares, with the job in the query (/embed/job_app?token=, /careers?gh_jid=, /hr/job?jid=), matched any
    posting on any site with that address, so one known link hid every new one (0 new postings, 2026-11-18)."""
    path = urlparse(url).path.rstrip("/").lower()
    return path if _JOB_IN_PATH.search(path) else ""


def dedupe(found: list[Found], known_urls: set[str], known_ids: set[str]) -> list[Found]:
    """Drop what he applied to or listed already (by job id or link), and repeats across sources (a feed's link wins
    over a list's)."""
    ids = {i.lower() for i in known_ids}
    paths_known = {key for key in (_path_key(u) for u in known_urls if u) if key}
    links_known = {normalize_url(u) for u in known_urls if u}
    out: dict[str, Found] = {}
    taken_ids: set[str] = set()
    taken_links: set[str] = set()
    for f in sorted(found, key=lambda f: f.source.startswith("list")):
        # a list's link carries no id of its own, and its ".../application?embed=true" ending hid the Ashby posting
        # the feed had already found (DriveWealth, 2026-11-18): read the id from the link and compare clean links
        key_id = (f.job_id or job_id_from_url(f.url) or "").lower()
        link = normalize_url(f.url) or ""
        path = _path_key(f.url)
        if (f.url in known_urls or (key_id and key_id in ids) or (path and path in paths_known)
                or (link and link in links_known)):
            continue
        if (key_id and key_id in taken_ids) or (link and link in taken_links):
            continue
        key = f"{f.company.lower()}|{f.title.lower()}|{f.location.lower()}"
        if key not in out and not any(f.url == g.url for g in out.values()):
            out[key] = f
            taken_ids.add(key_id)
            taken_links.add(link)
    for f in out.values():
        f.how = "you apply" if f.ats in YOU_APPLY else "helper fills"
    return list(out.values())


# --------------------------------------------------------------------------- eligibility


def check_all(paths: Paths, cfg: Any, found: list[Found], limit: int = 100) -> None:
    """The same checks as `helper apply --dry-run`, for up to `limit` postings; the real company, title and location
    replace what the feed showed."""
    from .apply import fetch_job
    from .eligibility import check_eligibility, load_applications
    from .keywords import extract_keywords
    from .models import CapsFile, Profile
    from .validate import RunOptions, load_context

    run = RunOptions(projects_per_resume=cfg.projects_per_resume, bullets_per_item=cfg.bullets_per_item,
                     grad_date=cfg.default_grad_date)
    ctx = load_context(paths, cfg, run)
    profile = Profile.model_validate(load_yaml(paths.profile))
    caps = CapsFile.model_validate(load_yaml(paths.caps))
    applications = load_applications(paths.db(cfg))

    def one(f: Found) -> None:
        try:
            job = fetch_job(f.url, purpose="resume" if f.how == "you apply" else "apply")
        except Exception as exc:
            message = (str(exc).splitlines() or [type(exc).__name__])[0][:160]
            if TRANSIENT.search(message):  # Nelnet's SSLError (2026-11-17) isn't a reason to skip the posting
                f.error = message
            else:  # closed, or not a posting at all
                f.eligibility = {"recommendation": "unreadable", "blockers": [message]}
            return
        keywords = extract_keywords(job.description_text, ctx.master, ctx.vocab, ctx.project_sets, ctx.families)
        e = check_eligibility(job, cfg, caps=caps, applications=applications, profile=profile, keywords=keywords)
        # a list's company stays when the page gave only a placeholder (UKG's "Unknown", 2026-11-20)
        f.company = job.company if job.company and job.company.strip().lower() not in ("unknown", "?") else f.company
        f.title = job.role_title or f.title
        f.location = job.location or f.location
        f.eligibility = {"recommendation": e.recommendation, "grad_date": e.grad_date,
                         "blockers": [x.message for x in e.flags if x.level == "blocker"],
                         "warnings": [x.message for x in e.flags if x.level == "warning"]}

    feeds = [f for f in found[:limit] if f.ats in ("greenhouse", "lever", "ashby", "workday", "smartrecruiters")]
    pages = [f for f in found[:limit] if f not in feeds]  # company sites open a hidden browser: one at a time
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(one, feeds))
    for f in pages:
        one(f)


def save(folder: Path, found: list[Found]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "candidates.json"
    path.write_text(json.dumps([asdict(f) for f in found], indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def summary(found: list[Found], sources: Sources, hours: float, path: str) -> str:
    def blocked(f: Found) -> bool:
        return bool(f.eligibility.get("blockers"))

    def line(i: int, f: Found) -> str:
        warn = f" [{'; '.join(w[:60] for w in f.eligibility.get('warnings', []))}]" if f.eligibility.get("warnings") else ""
        return (f"  {i:>2}. {f.company} | {f.title} | {f.location or '?'} | {f.ats} | {str(f.posted_at or '')[:16]}"
                f"{warn}\n      {f.url}")

    checked = [f for f in found if f.eligibility]
    fills = [f for f in checked if not blocked(f) and f.how == "helper fills"]
    yours = [f for f in checked if not blocked(f) and f.how == "you apply"]
    skipped = [f for f in checked if blocked(f)]
    unchecked = [f for f in found if not f.eligibility]
    out = [f"{len(found)} new postings from the last {hours:g} hours ({sources.counts()})."]
    n = 0
    for name, group in (("helper fills", fills), ("you apply (helper builds the resume; `helper assist`)", yours)):
        out.append(f"\nEligible, {name}: {len(group)}")
        for f in group:
            n += 1
            out.append(line(n, f))
    if skipped:
        out.append(f"\nIneligible (on the list as skipped): {len(skipped)}")
        out += [f"  - {f.company} | {f.title}: {f.eligibility['blockers'][0][:110]}" for f in skipped]
    if unchecked:
        out.append(f"\nNot checked (unreadable this time, or beyond --limit; run `helper apply <url> --dry-run` "
                   f"first): {len(unchecked)}")
        for f in unchecked:
            n += 1
            out.append(line(n, f) + (f"\n      couldn't read it: {f.error}" if f.error else ""))
    out.append(f"\nSaved: {path}")
    return "\n".join(out)
