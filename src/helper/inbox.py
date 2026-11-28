"""helper inbox: what the application emails say (Vatsal, 2026-11-27).

helper reads Gmail over IMAP, read-only, with the app password in .env (GMAIL_ADDRESS, GMAIL_APP_PASSWORD). It keeps
only mail that may be about an application: mail from a job site (Greenhouse, Workday, Ashby, HackerRank...), mail from
or naming a company Vatsal applied to, or a subject about an application or interview. Sign-in and verification-code
emails are left out: those codes are Vatsal's (CLAUDE.md).

Claude reads the rest (inbox_input.json -> inbox_output.json) and says what each email is. helper decides the status
changes itself, by fixed rules:
- a status Vatsal set by hand (in helper or in Notion) is never changed;
- "applied", or a status an earlier email set, moves forward: online assessment, phone screen, interview, offer, or
  rejected;
- a "we received your application" email confirms an application helper couldn't confirm (drafted, filled,
  submitted_unverified).
With `inbox_review: before_apply` (config.yaml) the changes wait for Vatsal's OK: `helper continue <run> --approve-inbox`.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional, Protocol

from .config import Paths, env_value
from .errors import HelperError, NeedsUser, WaitingForClaude
from .htmltext import html_to_text
from .models import Config, RunState
from .runs import load_state, read_json, rel, run_dir, save_state, set_step, write_json
from .tracker import repo
from .tracker.db import now_iso

INPUT, OUTPUT = "inbox_input.json", "inbox_output.json"
VALIDATION, REPORT = "inbox_validation.json", "inbox_report.md"
BODY_CHARS = 3500
MAX_CANDIDATES = 12
_LAST_CHECK = "inbox_last_check"

CATEGORIES: dict[str, str] = {
    "rejection": "the company says no (not moving forward, the role is filled, other candidates were chosen)",
    "oa": "an online assessment, coding challenge or take-home to complete (HackerRank, CodeSignal, Codility...)",
    "phone_screen": "a recruiter call or phone screen to book or attend",
    "interview": "an interview to book or attend (technical, video, on-site, final round)",
    "offer": "an offer",
    "received": "the company got the application (thank you for applying, application received)",
    "next_step": "something else Vatsal must do for an application (a form, his availability, documents, a reply)",
    "other": "about jobs but no news on any application (job alerts, newsletters, events, marketing, unrelated)",
}
STATUS_FOR = {"rejection": "rejected", "oa": "oa", "phone_screen": "phone_screen", "interview": "interview",
              "offer": "offer"}
ACTION_CATEGORIES = ("oa", "phone_screen", "interview", "offer", "next_step")
_RANK = {"drafted": 0, "filled": 0, "submitted_unverified": 0, "applied": 0, "oa": 1, "phone_screen": 2,
         "interview": 3, "offer": 4}
_UNCONFIRMED = ("drafted", "filled", "submitted_unverified")

# Mail sent by application systems and assessment sites (the sender's domain ends with one of these).
_ATS_DOMAINS = (
    "greenhouse.io", "greenhouse-mail.io", "myworkday.com", "myworkdayjobs.com", "workday.com", "ashbyhq.com",
    "lever.co", "icims.com", "smartrecruiters.com", "smartrecruiters-mail.com", "taleo.net", "oraclecloud.com",
    "successfactors.com", "successfactors.eu", "sapsf.com", "jobvite.com", "workable.com", "workablemail.com",
    "bamboohr.com", "eightfold.ai", "avature.net", "phenompeople.com", "paradox.ai", "hirevue.com", "codesignal.com",
    "hackerrank.com", "hackerrankforwork.com", "karat.com", "karat.io", "codility.com", "mettl.com", "shl.com",
    "pymetrics.ai", "harver.com", "modernhire.com", "gem.com", "rippling.com", "dover.com", "breezy.hr",
    "applytojob.com", "recruitee.com", "teamtailor.com", "pinpointhq.com", "comeet.co", "ultipro.com", "ukg.com",
    "brassring.com", "csod.com", "dayforcehcm.com", "paylocity.com", "jazzhr.com", "otta.com", "welcometothejungle.com",
)
# Job-alert and digest senders: never news on an application.
_ALERT_SENDERS = (
    "jobalerts-noreply@linkedin.com", "jobs-listings@linkedin.com", "alert@indeed.com", "noreply@glassdoor.com",
    "alerts@ziprecruiter.com", "jobs-noreply@glassdoor.com", "invitationtoapply@match.indeed.com",
)
# Sign-in, account and verification-code mail stays out of Claude's file: those steps are Vatsal's. Oracle's "Confirm
# your identity" carries its one-time code only in the body (2026-11-27), so the body is checked too.
_CODE_WORDS = re.compile(
    r"verif|security code|one[- ]time|pass ?code|password|identity|\bsign[- ]?in\b|\blog[- ]?in\b|confirm your email|"
    r"activate your|your account|account (created|activation|confirmation)|\bOTP\b", re.I)
_CODE_BODY = re.compile(r"one[- ]time pass ?code|verification code|security code|passcode|confirm your identity|"
                        r"verify your (email|identity)", re.I)
# A company's own job alerts, "X is hiring", and surveys about the application experience: no news on an application.
_ALERT_SUBJECT = re.compile(r"job alert|new jobs\b|new job\(s\)|jobs? (that )?match|is hiring|recommended (jobs|for you)|"
                            r"jobs you may|similar jobs|application experience|survey|talent community|newsletter", re.I)
# Subjects that are about an application even when no company is recognized.
_JOB_WORDS = re.compile(r"application|applying|applied|candida|interview|assessment|coding challenge|"
                        r"your interest|next steps?|offer letter|hackerrank|codesignal", re.I)
# A company's own mail counts when it comes from hiring: "Acme Recruiting", careers@, talent@, university@...
_RECRUITING = re.compile(r"career|recruit|talent|hiring|\bjobs?\b|universit|campus|\bhr\b|people|staffing|acquisition",
                         re.I)
# Company names that are everyday words or job platforms: matched only from the sender's own domain (generic), or
# never (platforms; every Workday or LinkedIn email names them). Claude can still pick such an application when the
# email names it. "Also", "Match" and "Current" matched unrelated mail on 2026-11-27.
_GENERIC = {"also", "match", "current", "box", "block", "scale", "square", "target", "state", "general", "applied",
            "national", "united", "first", "global", "digital", "data", "capital", "partners", "solutions", "systems",
            "energy", "health", "bank", "labs", "one", "next", "core", "american", "interactive", "advance", "advanced",
            "great", "north", "south", "east", "west", "central", "international", "smart", "open", "blue", "green",
            "pacific", "atlantic", "western", "eastern", "southern", "northern", "royal", "standard", "premier",
            "universal", "integrated", "liberty", "prime", "summit", "alpha", "delta", "omega", "trust", "union",
            "mutual", "insurance", "financial", "group", "technologies", "technology", "software", "services",
            "consulting", "engineering", "research", "media", "network", "networks", "motors", "foods", "brands",
            "notion", "simple", "clear", "bright", "quantum", "vision", "future", "modern", "public", "people"}
_PLATFORMS = {"workday", "myworkday", "linkedin", "greenhouse", "lever", "ashby", "icims", "indeed", "glassdoor",
              "handshake", "simplify", "hackerrank", "codesignal", "oracle", "google"}
_SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "plc", "lp", "llp",
             "the", "group", "holdings", "usa", "us", "na"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --------------------------------------------------------------------------- reading Gmail


class MailSource(Protocol):
    def headers(self, since: date) -> list[tuple[str, bytes]]: ...
    def message(self, uid: str) -> bytes: ...
    def close(self) -> None: ...


def imap_date(day: date) -> str:
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"  # IMAP wants English month names, whatever the locale


def fetched(data: Any) -> list[tuple[str, bytes]]:
    """(uid, bytes) pairs from an IMAP UID FETCH response."""
    out = []
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            head = item[0].decode("ascii", "replace") if isinstance(item[0], bytes) else str(item[0])
            m = re.search(r"\bUID (\d+)", head)
            if m and isinstance(item[1], bytes):
                out.append((m.group(1), item[1]))
    return out


class Gmail:
    """Gmail over IMAP, read-only: the mailbox is opened with EXAMINE and messages are read with BODY.PEEK, so nothing
    is marked read, moved or deleted. All Mail, so archived and filtered mail counts too."""

    HOST = "imap.gmail.com"
    BOX = '"[Gmail]/All Mail"'

    def __init__(self, address: str, password: str, connect: Callable[..., Any] = imaplib.IMAP4_SSL) -> None:
        try:
            self.imap = connect(self.HOST, 993, timeout=60)
        except OSError as exc:
            raise HelperError(f"couldn't reach Gmail ({exc}); check the internet connection and try again") from None
        try:
            self.imap.login(address, password)
        except imaplib.IMAP4.error:
            raise HelperError("Gmail refused the sign-in: check GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env. The "
                              "password is an app password from https://myaccount.google.com/apppasswords, not the "
                              "Google account password.") from None
        typ, _ = self.imap.select(self.BOX, readonly=True)
        if typ != "OK":
            raise HelperError("Gmail didn't open its All Mail folder over IMAP")

    def headers(self, since: date) -> list[tuple[str, bytes]]:
        typ, data = self.imap.uid("SEARCH", None, "SINCE", imap_date(since))
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        out: list[tuple[str, bytes]] = []
        for i in range(0, len(uids), 200):
            chunk = b",".join(uids[i:i + 200]).decode()
            typ, got = self.imap.uid("FETCH", chunk, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
            out += fetched(got)
        return out

    def message(self, uid: str) -> bytes:
        typ, got = self.imap.uid("FETCH", uid, "(BODY.PEEK[])")
        pairs = fetched(got)
        if not pairs:
            raise HelperError(f"Gmail didn't return message {uid}")
        return pairs[0][1]

    def close(self) -> None:
        try:
            self.imap.logout()
        except Exception:
            pass


def gmail_from_env(paths: Paths) -> tuple[Gmail, str]:
    address = env_value(paths, "GMAIL_ADDRESS")
    password = (env_value(paths, "GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not address or not password:
        raise HelperError("Gmail isn't set up: put GMAIL_ADDRESS and GMAIL_APP_PASSWORD (an app password from "
                          "https://myaccount.google.com/apppasswords) in .env")
    return Gmail(address, password), address


# --------------------------------------------------------------------------- which mail is about applications


@dataclass
class Mail:
    uid: str
    message_id: str
    received: str  # ISO date and time
    sender_name: str
    sender_addr: str
    subject: str
    body: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def sender(self) -> str:
        return f"{self.sender_name} <{self.sender_addr}>" if self.sender_name else self.sender_addr

    @property
    def domain(self) -> str:
        return self.sender_addr.rpartition("@")[2].lower()


def _header(msg: Any, name: str) -> str:
    try:
        return str(msg.get(name, "") or "").strip()
    except Exception:  # a malformed header: keep what can be read
        return ""


def parse_headers(uid: str, raw: bytes) -> Mail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = email.utils.parseaddr(_header(msg, "From"))
    received = ""
    try:
        when = email.utils.parsedate_to_datetime(_header(msg, "Date"))
        received = (when if when.tzinfo else when.astimezone()).astimezone().isoformat(timespec="minutes")
    except (TypeError, ValueError, IndexError):
        pass
    subject = re.sub(r"\s+", " ", _header(msg, "Subject"))
    return Mail(uid=uid, message_id=_header(msg, "Message-ID") or f"uid:{uid}", received=received,
                sender_name=name.strip(), sender_addr=addr.strip().lower(), subject=subject)


def body_text(raw: bytes, limit: int = BODY_CHARS) -> str:
    """The email's words: its plain-text part, or its HTML part as text when the plain one is missing or a stub."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    texts: dict[str, str] = {}
    for kind in ("plain", "html"):
        part = msg.get_body(preferencelist=(kind,))
        if part is None:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, AssertionError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
        texts[kind] = html_to_text(content) if kind == "html" else content
    text = texts.get("plain", "")
    if len(text.strip()) < 200 and texts.get("html"):
        text = texts["html"]
    text = re.sub(r"[ \t ​‌‍͏]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]


def _norm(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", text.lower().replace("&", " and ")).strip() + " "


def company_key(name: str) -> str:
    """A company's name for matching: lower case, no punctuation, no Inc / LLC / Corporation."""
    return " ".join(w for w in _norm(name).split() if w not in _SUFFIXES)


def company_keys(name: str) -> list[str]:
    """The names an email may use for a company: its whole name, and its first word when that word is distinctive
    ("Fanatics" for "Fanatics Collectibles", 2026-11-27)."""
    key = company_key(name)
    words = key.split()
    keys = [key] if len(key.replace(" ", "")) >= 3 else []
    if len(words) > 1 and len(words[0]) >= 5 and words[0] not in _GENERIC | _PLATFORMS:
        keys.append(words[0])
    return keys


def company_index(apps: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    index: dict[str, list[sqlite3.Row]] = {}
    for row in apps:
        for key in company_keys(row["company"] or ""):
            index.setdefault(key, []).append(row)
    return index


def companies_in(text: str, index: dict[str, list[sqlite3.Row]], labels: Any = ()) -> set[str]:
    """Companies named in the text, or whose name is one of the sender's labels (its domain's parts, and for a job
    site's mail the name before the @, which is the company: dexcom@myworkday.com)."""
    hay = _norm(text)
    found = set()
    for key in index:
        if key in _PLATFORMS:
            continue
        if key.replace(" ", "") in labels or (key not in _GENERIC and f" {key} " in hay):
            found.add(key)
    return found


def sender_labels(mail: "Mail", ats: bool) -> set[str]:
    labels = set(re.split(r"[.\-]", mail.domain))
    if ats:
        labels |= set(re.split(r"[._\-+]", mail.sender_addr.partition("@")[0]))
    return labels - {"", "com", "www", "mail", "email", "no", "reply", "noreply"}


def _set_by(conn: sqlite3.Connection, app_id: int, status: str) -> str:
    """Who set the application's current status: email (an earlier inbox check), helper (a run), or you."""
    row = conn.execute("SELECT note FROM status_history WHERE application_id = ? AND status = ? ORDER BY id DESC LIMIT 1",
                       (app_id, status)).fetchone()
    note = (row["note"] if row else None) or ""
    if note.startswith("email:"):
        return "email"
    if status in ("drafted", "filled", "applied", "submitted_unverified", "skipped", "failed"):
        return "helper" if note != "from Notion" else "you (Notion)"
    return "you (Notion)" if note == "from Notion" else "you"


def _candidate(conn: sqlite3.Connection, row: sqlite3.Row, by_job_id: bool = False) -> dict[str, Any]:
    out = {"application_id": row["id"], "company": row["company"], "role_title": row["role_title"],
           "status": row["status"], "set_by": _set_by(conn, row["id"], row["status"]),
           "date_applied": row["date_applied"]}
    if by_job_id:
        out["job_id_in_email"] = row["job_id"]  # the email names this application's requisition: the surest match
    return out


def job_index(apps: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    """Applications by job id, for ids an email can name: at least 5 characters with a digit (R5049957, JR003160,
    768636WD, J-00357971). The tracker keeps legal names ("PRICEWATERHOUSECOOPERS LLP", "Citigroup Global Markets")
    while the email says PwC or Citi, so the requisition number is often the only link (2026-11-27)."""
    index: dict[str, list[sqlite3.Row]] = {}
    for row in apps:
        job = (row["job_id"] or "").strip().lower()
        if len(job) >= 5 and re.search(r"\d", job) and not re.fullmatch(r"req-?\d{4}", job):
            index.setdefault(job, []).append(row)
    return index


def job_ids_in(text: str, index: dict[str, list[sqlite3.Row]]) -> list[sqlite3.Row]:
    tokens = {tok.lower() for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{3,}[A-Za-z0-9]", text)}
    return [row for job in index if job in tokens for row in index[job]]


def relevant(mail: Mail, index: dict[str, list[sqlite3.Row]]) -> tuple[str, set[str]]:
    """('keep' | 'code' | 'alert' | 'no', companies named in the sender or subject). Mail from a company Vatsal applied
    to counts only from its recruiting side, so an Amazon order or a Google security notice stays out."""
    ats = any(mail.domain == d or mail.domain.endswith("." + d) for d in _ATS_DOMAINS)
    if mail.sender_addr in _ALERT_SENDERS or _ALERT_SUBJECT.search(mail.subject):
        return ("alert", set()) if ats or _JOB_WORDS.search(mail.subject) or "job" in mail.subject.lower() \
            or companies_in(mail.sender_name, index, sender_labels(mail, ats)) else ("no", set())
    job = bool(_JOB_WORDS.search(mail.subject))
    from_companies = companies_in(mail.sender_name, index, sender_labels(mail, ats))
    recruiting = bool(_RECRUITING.search(f"{mail.sender_name} {mail.sender_addr.partition('@')[0]}"))
    if not (ats or job or (from_companies and recruiting)):
        return "no", set()
    if _CODE_WORDS.search(mail.subject):
        return "code", set()
    return "keep", from_companies | companies_in(mail.subject, index)


def collect(conn: sqlite3.Connection, source: MailSource, since: date, own: Optional[str]) -> tuple[list[Mail], Counter]:
    """The mail since `since` that may be about an application, with the applications it may be about."""
    seen = {r[0] for r in conn.execute("SELECT message_id FROM inbox_messages")}
    apps = conn.execute("SELECT * FROM applications WHERE status NOT IN ('skipped', 'failed') "
                        "ORDER BY COALESCE(date_applied, substr(created_at, 1, 10)) DESC, id DESC").fetchall()
    index = company_index(apps)
    jobs = job_index(apps)
    counts: Counter = Counter()
    kept: list[Mail] = []
    for uid, head in source.headers(since):
        mail = parse_headers(uid, head)
        counts["read"] += 1
        if mail.message_id in seen:
            counts["already checked"] += 1
            continue
        if own and mail.sender_addr == own.lower():
            continue
        verdict, keys = relevant(mail, index)
        if verdict != "keep":
            counts[{"alert": "job alerts and surveys", "code": "sign-in or verification emails (yours)"}.get(verdict, "not about jobs")] += 1
            continue
        mail.body = body_text(source.message(uid))
        if _CODE_BODY.search(mail.body[:1500]):
            counts["sign-in or verification emails (yours)"] += 1
            continue
        if not keys:
            keys = companies_in(mail.body[:1500], index)
        named = job_ids_in(f"{mail.subject}\n{mail.body}", jobs)
        rows = named + [r for key in sorted(keys, key=len, reverse=True) for r in index[key]]
        unique = list({r["id"]: r for r in rows}.values())[:MAX_CANDIDATES]
        by_job = {r["id"] for r in named}
        mail.candidates = [_candidate(conn, r, r["id"] in by_job) for r in unique]
        kept.append(mail)
        seen.add(mail.message_id)  # Gmail can hold one message twice (a label copy)
    kept.sort(key=lambda m: m.received)
    return kept, counts


# --------------------------------------------------------------------------- the handoff


def default_since(conn: sqlite3.Connection, today: Optional[date] = None) -> date:
    """A day before the last check (the mail already checked is skipped by its id), or a week back the first time."""
    last = repo.get_state(conn, _LAST_CHECK)
    today = today or date.today()
    return date.fromisoformat(last) - timedelta(days=1) if last else today - timedelta(days=7)


def _instructions() -> list[str]:
    return [
        "Write inbox_output.json as {\"emails\": [...]}, one entry for every email below, in any order:",
        "{\"message_id\", \"category\", \"application_id\", \"summary\", \"action\", \"deadline\"}.",
        "category: one of the keys of `categories`. Pick by what the email says, not by its subject alone.",
        "application_id: the candidate this email is about. A candidate with job_id_in_email is named by its "
        "requisition number: that's the one, unless the email is plainly about something else. Otherwise match the "
        "role the email names. When several candidates "
        "fit and the email doesn't say which, or none fits, use null and say so in the summary.",
        "summary: one plain line of what the email says (no em dashes).",
        "action: for oa, phone_screen, interview, offer and next_step, what Vatsal has to do, in one line. Leave it out "
        "for the other categories.",
        "done: true when the step is already behind him (\"thanks for completing the assessment\", an interview he "
        "had); it still moves the status, but nothing goes on his to-do list and no action is needed.",
        "deadline: YYYY-MM-DD when the email gives a date to act by; otherwise leave it out.",
        "helper decides the status changes by its own rules: you only say what each email is.",
    ]


def start(paths: Paths, cfg: Config, *, since: Optional[date] = None, source: Optional[MailSource] = None,
          own: Optional[str] = None, now: Optional[datetime] = None) -> str:
    """Read the mail and hand the application emails to Claude (exit 3), or say there's nothing new."""
    from .tracker.commands import open_db

    now = now or datetime.now()
    conn = open_db(paths, cfg)
    try:
        since = since or default_since(conn, now.date())
        opened = source is None
        if source is None:
            source, own = gmail_from_env(paths)
        try:
            mails, counts = collect(conn, source, since, own)
        finally:
            if opened:
                source.close()
        left_out = {k: v for k, v in counts.items() if k != "read"}
        if not mails:
            repo.set_state(conn, _LAST_CHECK, now.date().isoformat())
            conn.commit()
            return (f"No new application emails since {since.isoformat()} ({counts['read']} messages read"
                    + "".join(f", {v} {k}" for k, v in left_out.items()) + ").")
        run_id = f"inbox-{now:%Y%m%d-%H%M%S}"
        folder = run_dir(paths, run_id)
        write_json(folder / INPUT, {
            "since": since.isoformat(), "checked_on": now.date().isoformat(), "instructions": _instructions(),
            "categories": CATEGORIES, "left_out": left_out,
            "emails": [{"message_id": m.message_id, "received": m.received, "from": m.sender, "subject": m.subject,
                        "body": m.body, "candidates": m.candidates} for m in mails],
        })
        state = RunState(run_id=run_id, created_at=now_iso(), command="inbox", options={"since": since.isoformat()})
        set_step(state, "classify", "waiting_for_claude")
        save_state(paths, state)
    finally:
        conn.close()
    raise WaitingForClaude(run_id, rel(paths, folder / INPUT), rel(paths, folder / OUTPUT),
                           note=f"{len(mails)} application email(s) since {since.isoformat()}.")


@dataclass
class Entry:
    message_id: str
    category: str
    application_id: Optional[int]
    summary: str
    action: Optional[str] = None
    deadline: Optional[str] = None
    done: bool = False


def validate(inp: dict[str, Any], out: Any, conn: sqlite3.Connection) -> tuple[list[Entry], list[str]]:
    """Every email answered once, with a known category, one of its candidates (or an application whose company the
    email names), a summary, an action where one is due, and a real date."""
    problems: list[str] = []
    emails = {e["message_id"]: e for e in inp["emails"]}
    items = out.get("emails") if isinstance(out, dict) else None
    if not isinstance(items, list):
        return [], ['inbox_output.json must be {"emails": [...]}']
    entries: list[Entry] = []
    answered: Counter = Counter()
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict):
            problems.append(f"entry {n}: not an object")
            continue
        unknown = set(item) - {"message_id", "category", "application_id", "summary", "action", "deadline", "done"}
        if unknown:
            problems.append(f"entry {n}: unknown field(s) {', '.join(sorted(unknown))}")
        mid = item.get("message_id")
        if mid not in emails:
            problems.append(f"entry {n}: message_id {mid!r} isn't one of the emails in {INPUT}")
            continue
        answered[mid] += 1
        where = f"entry {n} ({emails[mid]['subject'][:60]!r})"
        category = item.get("category")
        if category not in CATEGORIES:
            problems.append(f"{where}: category {category!r} isn't one of {', '.join(CATEGORIES)}")
        app_id = item.get("application_id")
        if app_id is not None:
            ids = {c["application_id"] for c in emails[mid]["candidates"]}
            if not isinstance(app_id, int) or isinstance(app_id, bool):
                problems.append(f"{where}: application_id must be a number or null")
            elif app_id not in ids:
                row = conn.execute("SELECT company FROM applications WHERE id = ?", (app_id,)).fetchone()
                text = " ".join((emails[mid]["from"], emails[mid]["subject"], emails[mid]["body"]))
                if row is None:
                    problems.append(f"{where}: there's no application #{app_id}")
                elif not any(f" {key} " in _norm(text) for key in company_keys(row["company"])):
                    problems.append(f"{where}: application #{app_id} ({row['company']}) isn't a candidate and the email "
                                    "doesn't name that company")
        summary = item.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            problems.append(f"{where}: summary is missing")
        action = item.get("action")
        if not isinstance(item.get("done", False), bool):
            problems.append(f"{where}: done must be true or false")
        if category in ACTION_CATEGORIES and not item.get("done") and not (isinstance(action, str) and action.strip()):
            problems.append(f"{where}: a {category} email needs an action (what Vatsal has to do), or done: true")
        for key in ("summary", "action"):
            if isinstance(item.get(key), str) and "—" in item[key]:
                problems.append(f"{where}: {key} has an em dash; use a comma or a period")
        deadline = item.get("deadline")
        if deadline is not None:
            try:
                date.fromisoformat(str(deadline))
            except ValueError:
                problems.append(f"{where}: deadline {deadline!r} isn't a YYYY-MM-DD date")
        entries.append(Entry(message_id=mid, category=str(category), application_id=app_id if isinstance(app_id, int)
                             else None, summary=str(summary or "").strip(),
                             action=action.strip() if isinstance(action, str) and action.strip() else None,
                             deadline=str(deadline) if deadline else None, done=item.get("done") is True))
    for mid, e in emails.items():
        if answered[mid] == 0:
            problems.append(f"no entry for the email {e['subject'][:60]!r} ({e['received'][:10]})")
        elif answered[mid] > 1:
            problems.append(f"the email {e['subject'][:60]!r} has {answered[mid]} entries; give it one")
    return entries, problems


def decide(current: str, set_by: str, category: str) -> tuple[Optional[str], str]:
    """(new status or None, why not) for an email of this category about an application in this status."""
    if category == "received":
        return ("applied", "") if current in _UNCONFIRMED else (None, "")
    target = STATUS_FOR.get(category)
    if target is None or current == target:
        return None, ""
    if current not in _RANK:
        return None, f"left as {current}"
    if current not in ("applied",) + _UNCONFIRMED and set_by != "email":
        return None, f"left as {current} (set by {set_by})"
    if target == "rejected":
        return (None, "left as offer") if current == "offer" else ("rejected", "")
    if _RANK[target] > _RANK[current]:
        return target, ""
    return None, f"left as {current} (a later step than {target})"


@dataclass
class Plan:
    changes: list[dict[str, Any]] = field(default_factory=list)
    todo: list[dict[str, Any]] = field(default_factory=list)
    unchanged: list[dict[str, Any]] = field(default_factory=list)
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    other: int = 0
    confirmations: int = 0


def make_plan(conn: sqlite3.Connection, inp: dict[str, Any], entries: list[Entry]) -> Plan:
    emails = {e["message_id"]: e for e in inp["emails"]}
    plan = Plan()
    current: dict[int, str] = {}  # statuses this check already changed
    for entry in sorted(entries, key=lambda e: emails[e.message_id]["received"]):
        mail = emails[entry.message_id]
        base = {"message_id": entry.message_id, "received": mail["received"], "from": mail["from"],
                "subject": mail["subject"], "category": entry.category, "summary": entry.summary,
                "action": entry.action, "deadline": entry.deadline, "application_id": entry.application_id}
        if entry.application_id is None:
            if entry.category == "other":
                plan.other += 1
            elif entry.category == "received":
                plan.confirmations += 1
            else:
                plan.unmatched.append(base)
            continue
        row = repo.get(conn, entry.application_id)
        base |= {"company": row["company"], "role_title": row["role_title"]}
        status = current.get(row["id"], row["status"])
        by = "email" if row["id"] in current else _set_by(conn, row["id"], status)
        new, why = decide(status, by, entry.category)
        if new:
            plan.changes.append(base | {"before": status, "after": new,
                                        "applied_on": mail["received"][:10] if status in _UNCONFIRMED
                                        and not row["date_applied"] else None})
            current[row["id"]] = new
        elif why:
            plan.unchanged.append(base | {"status": status, "why": why})
        elif entry.category == "received":
            plan.confirmations += 1
        elif entry.category == "other":
            plan.other += 1
        if entry.category in ACTION_CATEGORIES and not entry.done:
            plan.todo.append(base)
    return plan


def _day(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%b %d").replace(" 0", " ")
    except ValueError:
        return iso[:10]


def _app(item: dict[str, Any]) -> str:
    return f"#{item['application_id']} {item['company']}, {item['role_title']}"


def render_report(inp: dict[str, Any], plan: Plan) -> str:
    left = inp.get("left_out") or {}
    head = f"Inbox since {inp['since']}: {len(inp['emails'])} application email(s) read"
    if left.get("sign-in or verification emails (yours)"):
        head += f"; {left['sign-in or verification emails (yours)']} sign-in or verification email(s) left out (yours)"
    lines = [head + "."]
    if plan.changes:
        lines += ["", f"Status changes ({len(plan.changes)}):"]
        lines += [f"  {_app(c)}: {c['before']} -> {c['after']}. {_day(c['received'])}: {c['summary']}" for c in plan.changes]
    if plan.todo:
        lines += ["", f"To do ({len(plan.todo)}):"]
        for t in plan.todo:
            due = f" (by {t['deadline']})" if t["deadline"] else ""
            who = _app(t) if t.get("company") else t["from"]
            lines.append(f"  {who}: {t['action'].rstrip('.')}{due}. Email {_day(t['received'])}: \"{t['subject']}\"")
    if plan.unchanged:
        lines += ["", f"No change ({len(plan.unchanged)}):"]
        lines += [f"  {_app(u)}: a {u['category']} email, {u['why']}. {_day(u['received'])}: {u['summary']}"
                  for u in plan.unchanged]
    if plan.unmatched:
        lines += ["", f"Not matched to an application ({len(plan.unmatched)}):"]
        lines += [f"  {_day(u['received'])} {u['category']}: \"{u['subject']}\" from {u['from']}. {u['summary']}"
                  for u in plan.unmatched]
    rest = []
    if plan.confirmations:
        rest.append(f"{plan.confirmations} confirmation(s) of applications already marked sent")
    if plan.other:
        rest.append(f"{plan.other} other job email(s) (alerts, newsletters, marketing)")
    if rest:
        lines += ["", "Also: " + "; ".join(rest) + "."]
    if not (plan.changes or plan.todo or plan.unchanged or plan.unmatched):
        lines += ["", "Nothing changes."]
    return "\n".join(lines)


def _record(conn: sqlite3.Connection, inp: dict[str, Any], entries: list[Entry], run_id: str) -> None:
    emails = {e["message_id"]: e for e in inp["emails"]}
    stamp = now_iso()
    for entry in sorted(entries, key=lambda e: emails[e.message_id]["received"]):
        mail = emails[entry.message_id]
        conn.execute(
            "INSERT OR IGNORE INTO inbox_messages (message_id, received_at, sender, subject, category, application_id, "
            "summary, action, deadline, run_id, processed_at, done) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry.message_id, mail["received"], mail["from"], mail["subject"], entry.category, entry.application_id,
             entry.summary, entry.action, entry.deadline, run_id, stamp,
             0 if entry.category in ACTION_CATEGORIES and not entry.done else 1))
        if entry.application_id is not None and entry.category in ACTION_CATEGORIES + ("rejection",):
            # a newer step, or a no, ends the application's earlier to-dos
            conn.execute("UPDATE inbox_messages SET done = 1 WHERE application_id = ? AND received_at < ? AND done = 0",
                         (entry.application_id, mail["received"]))
        if entry.application_id is not None and entry.category == "rejection":
            conn.execute("UPDATE inbox_messages SET done = 1 WHERE application_id = ? AND done = 0",
                         (entry.application_id,))


def continue_inbox(paths: Paths, cfg: Config, run_id: str, *, approve: bool = False) -> str:
    """Check Claude's answers, show the changes (and wait for Vatsal's OK with inbox_review: before_apply), then apply
    them: statuses in SQLite and Notion, to-dos on the job hunt's list."""
    from .hunt import on_marked, refresh
    from .tracker.commands import open_db, push_one

    state = load_state(paths, run_id)
    folder = run_dir(paths, run_id)
    done = state.steps.get("apply")
    if done is not None and done.status == "done":
        report = (folder / REPORT).read_text(encoding="utf-8") if (folder / REPORT).is_file() else ""
        return f"Inbox run {run_id} is already applied.\n{report}".rstrip()
    inp = read_json(folder / INPUT)
    if not (folder / OUTPUT).is_file():
        raise WaitingForClaude(run_id, rel(paths, folder / INPUT), rel(paths, folder / OUTPUT))
    conn = open_db(paths, cfg)
    try:
        entries, problems = validate(inp, read_json(folder / OUTPUT), conn)
        write_json(folder / VALIDATION, {"ok": not problems, "problems": problems})
        if problems:
            raise HelperError(f"{OUTPUT} has {len(problems)} problem(s) (also in {rel(paths, folder / VALIDATION)}):\n  - "
                              + "\n  - ".join(problems) + f"\nFix {OUTPUT} and run `helper continue {run_id}` again.")
        plan = make_plan(conn, inp, entries)
        report = render_report(inp, plan)
        (folder / REPORT).write_text(report + "\n", encoding="utf-8")
        if cfg.inbox_review == "before_apply" and not approve:
            set_step(state, "classify", "done")
            set_step(state, "apply", "waiting_for_user")
            save_state(paths, state)
            raise NeedsUser(f"{report}\n\nPaused for Vatsal (exit 4): nothing is changed yet. Show him these changes; "
                            f"after he approves, run `helper continue {run_id} --approve-inbox`.")
        notion: Counter = Counter()
        for change in plan.changes:
            repo.set_status(conn, change["application_id"], change["after"], applied_on=change["applied_on"],
                            note=f"email: {change['summary']} ({change['received'][:10]})")
            conn.commit()
            notion[push_one(paths, conn, change["application_id"])] += 1
            on_marked(paths, conn, change["application_id"], change["after"])
        _record(conn, inp, entries, run_id)
        repo.set_state(conn, _LAST_CHECK, inp["checked_on"])
        conn.commit()
        refresh(paths, conn)
    finally:
        conn.close()
    set_step(state, "classify", "done")
    set_step(state, "apply", "done", message=f"{len(plan.changes)} status change(s)")
    save_state(paths, state)
    tail = [f"  {k}" + (f" ({n})" if n > 1 else "") for k, n in notion.items()]
    return "\n".join([report, "", "Applied." + (" Notion:" if tail else "")] + tail)


# --------------------------------------------------------------------------- to-dos on the list


def open_todos(conn: sqlite3.Connection, today: Optional[date] = None, days: int = 30) -> list[dict[str, Any]]:
    """Steps the emails asked of Vatsal that are still open: newest first, none past their deadline, none older
    than `days`."""
    today = today or date.today()
    rows = conn.execute(
        "SELECT m.id, m.received_at, m.category, m.action, m.deadline, m.subject, m.application_id, "
        "a.company, a.role_title FROM inbox_messages m LEFT JOIN applications a ON a.id = m.application_id "
        "WHERE m.done = 0 ORDER BY m.received_at DESC").fetchall()
    out = []
    for r in rows:
        if r["deadline"] and r["deadline"] < today.isoformat():
            continue
        if (r["received_at"] or "")[:10] < (today - timedelta(days=days)).isoformat():
            continue
        out.append({"id": r["id"], "company": r["company"] or "", "role": r["role_title"] or "",
                    "category": r["category"], "action": r["action"] or "", "deadline": r["deadline"],
                    "received": (r["received_at"] or "")[:10], "subject": r["subject"] or ""})
    return out


def close_for(conn: sqlite3.Connection, application_id: int, status: str) -> int:
    """`helper mark <id> rejected` (or withdrawn, declined, accepted): the application's open to-dos are over."""
    if status not in ("rejected", "withdrawn", "declined", "accepted", "ghosted"):
        return 0
    return conn.execute("UPDATE inbox_messages SET done = 1 WHERE application_id = ? AND done = 0",
                        (application_id,)).rowcount


def mark_done(conn: sqlite3.Connection, todo_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM inbox_messages WHERE id = ?", (todo_id,)).fetchone()
    if row is None:
        raise HelperError(f"no inbox to-do #{todo_id} (see `helper queue`)")
    conn.execute("UPDATE inbox_messages SET done = 1 WHERE id = ?", (todo_id,))
    return row  # type: ignore[no-any-return]
