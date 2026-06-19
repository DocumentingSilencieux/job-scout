#!/usr/bin/env python3
"""
Job Scout — daily entry-level IT job finder for Austin, TX.

Run daily via GitHub Actions. On first run of each week it also crawls the
Built In Austin company directory to discover new Austin startups/companies
and adds ones with IT roles to the permanent watch list.
"""

import hashlib
import json
import os
import re
import smtplib
import time
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup


# ── Static company watch list ─────────────────────────────────────────────────
STATIC_COMPANIES = [
    {"name": "Vintage IT Services",          "base": "https://vintageits.com"},
    {"name": "TPx Communications",            "base": "https://tpx.com"},
    {"name": "Live Oak IT Partners",           "base": "https://liveoakitpartners.com"},
    {"name": "Centre Technologies",            "base": "https://centretechnologies.com"},
    {"name": "UniVista",                       "base": "https://univistait.com"},
    {"name": "Whitehat Virtual Technologies",  "base": "https://whitehatvirtual.com"},
    {"name": "MyITPros",                       "base": "https://myitpros.com"},
    {"name": "Integris IT",                    "base": "https://integrisit.com"},
    {"name": "Gravity Systems",                "base": "https://gravitysystems.com"},
    {"name": "Computek",                       "base": "https://computek.net"},
    {"name": "Texas Systems Group",            "base": "https://texas-systems.com"},
]

# ── Built In Austin discovery config ─────────────────────────────────────────
# Directory pages to crawl when discovering new companies
BUILTIN_COMPANY_PAGES = [
    "https://www.builtinaustin.com/companies",
    "https://www.builtinaustin.com/companies?industry=information-technology",
    "https://www.builtinaustin.com/companies?industry=cybersecurity",
    "https://www.builtinaustin.com/companies?industry=cloud",
]
BUILTIN_MAX_PAGES = 10          # pagination depth per base URL
DISCOVERY_INTERVAL_DAYS = 7     # re-crawl directory at most once per week

# Built In Austin job search URLs (checked every day)
BUILTIN_JOB_SEARCHES = [
    "https://www.builtinaustin.com/jobs?search=help+desk&experience=Entry+Level",
    "https://www.builtinaustin.com/jobs?search=IT+support&experience=Entry+Level",
    "https://www.builtinaustin.com/jobs?search=NOC+technician",
    "https://www.builtinaustin.com/jobs?search=IT+technician&experience=Entry+Level",
    "https://www.builtinaustin.com/jobs?search=desktop+support&experience=Entry+Level",
]

# ── Persistence files ─────────────────────────────────────────────────────────
SEEN_JOBS_FILE        = "seen_jobs.json"
DISCOVERED_FILE       = "discovered_companies.json"

MIN_SCORE = 3   # minimum match score to include in email

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Keywords for IT-company detection (directory crawl) ──────────────────────
IT_COMPANY_SIGNALS = [
    "managed service", "msp", "it service", "it support", "help desk",
    "helpdesk", "network operations", "noc", "infrastructure", "cybersecurity",
    "cyber security", "it staffing", "it consulting", "tech support",
    "system administrator", "cloud services", "data center", "telecom",
    "telecommunications", "voip", "unified communications", "end-user support",
    "desktop support", "field service", "break/fix",
]

# ── Scoring keyword lists ─────────────────────────────────────────────────────
ROLE_KEYWORDS = [
    "help desk", "helpdesk", "it support", "noc", "network operations",
    "it technician", "desktop support", "tier 1", "tier i", "tier1",
    "msp", "managed service", "sysadmin", "system administrator",
    "field technician", "field tech", "it specialist", "support specialist",
    "it analyst", "systems analyst", "technical support", "tech support",
    "service desk", "end user support", "end-user support",
]

ENTRY_LEVEL_KEYWORDS = [
    "entry level", "entry-level", "0-1 year", "0-2 year", "1-2 year",
    "no experience required", "recent graduate", "new grad",
    "junior", "associate", "trainee",
]

CERT_KEYWORDS = [
    "comptia", "a+", "network+", "a plus", "network plus",
]

SKILL_KEYWORDS = [
    "active directory", "tcp/ip", "hardware", "troubleshoot",
    "windows", "ticketing", "chromebook", "printer", "remote support",
    "subnetting", "networking", "vpn", "microsoft 365", "office 365",
]

LOCATION_KEYWORDS = [
    "austin", "remote", "hybrid", "work from home", "wfh", "texas",
]

TITLE_DISQUALIFY = re.compile(
    r"\b(senior|sr\.?\s+|lead\s+|principal|staff\s+|"
    r"manager|director|vp\b|vice\s+president|cto|ciso|coo|cfo|"
    r"software\s+engineer|software\s+developer|devops|"
    r"data\s+scientist|data\s+engineer|machine\s+learning)\b",
    re.IGNORECASE,
)

YEARS_REQUIRED = re.compile(
    r"(\d+)\+?\s*(?:to\s*\d+\s*)?years?\s+(?:of\s+)?(?:experience|exp)",
    re.IGNORECASE,
)

CAREERS_PATH_GUESSES = [
    "/careers", "/jobs", "/about/careers", "/about-us/careers",
    "/join-us", "/join-our-team", "/work-with-us", "/hiring",
    "/openings", "/opportunities", "/apply",
]

# ── Persistence helpers ───────────────────────────────────────────────────────

def load_seen() -> dict:
    if os.path.exists(SEEN_JOBS_FILE):
        with open(SEEN_JOBS_FILE) as f:
            return json.load(f)
    return {}


def save_seen(seen: dict) -> None:
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump(seen, f, indent=2)


def load_discovered() -> dict:
    if os.path.exists(DISCOVERED_FILE):
        with open(DISCOVERED_FILE) as f:
            return json.load(f)
    return {"last_directory_crawl": None, "companies": []}


def save_discovered(data: dict) -> None:
    with open(DISCOVERED_FILE, "w") as f:
        json.dump(data, f, indent=2)


def job_id(title: str, company: str, url: str) -> str:
    key = f"{title.lower().strip()}|{company.lower().strip()}|{url.strip()}"
    return hashlib.md5(key.encode()).hexdigest()


# ── HTTP helper ───────────────────────────────────────────────────────────────

def fetch(url: str, timeout: int = 12) -> requests.Response | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code == 200 and len(r.text) > 200:
            return r
    except Exception:
        pass
    return None


def resolve_url(href: str, base: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        parts = base.split("/")
        return parts[0] + "//" + parts[2] + href
    return base.rstrip("/") + "/" + href


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_job(title: str, description: str = "") -> int:
    title_lower = title.lower()
    full_text = (title + " " + description).lower()

    if TITLE_DISQUALIFY.search(title_lower):
        return 0

    for m in YEARS_REQUIRED.finditer(full_text):
        if int(m.group(1)) >= 3:
            return 0

    score = 0

    if any(kw in full_text for kw in ROLE_KEYWORDS):
        score += 3

    if any(kw in full_text for kw in ENTRY_LEVEL_KEYWORDS):
        score += 2

    if any(kw in full_text for kw in CERT_KEYWORDS):
        score += 2

    skill_hits = sum(1 for kw in SKILL_KEYWORDS if kw in full_text)
    score += min(skill_hits, 2)

    if any(kw in full_text for kw in LOCATION_KEYWORDS):
        score += 1

    return min(score, 10)


# ── Career page discovery ─────────────────────────────────────────────────────

def find_careers_url(base_url: str) -> str | None:
    base = base_url.rstrip("/")
    base_domain = base.split("//")[-1].split("/")[0]

    r = fetch(base)
    if r:
        soup = BeautifulSoup(r.text, "lxml")
        career_terms = {"career", "job", "hiring", "join", "openings", "opportunity"}
        for a in soup.find_all("a", href=True):
            href_lower = a["href"].lower()
            text_lower = a.get_text().lower().strip()
            if any(t in href_lower or t in text_lower for t in career_terms):
                full = resolve_url(a["href"], base)
                if base_domain in full or any(
                    ats in full for ats in [
                        "lever.co", "greenhouse.io", "bamboohr.com",
                        "jazzhr", "workable.com", "recruitee.com",
                        "smartrecruiters.com", "myworkdayjobs.com",
                    ]
                ):
                    return full

    for path in CAREERS_PATH_GUESSES:
        r = fetch(base + path)
        if r and len(r.text) > 800:
            return base + path

    return None


# ── Job extraction ────────────────────────────────────────────────────────────

def extract_jobs(company_name: str, careers_url: str) -> list[dict]:
    r = fetch(careers_url, timeout=15)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "lxml")
    jobs: list[dict] = []
    seen_titles: set[str] = set()

    def add_job(title: str, href: str, desc: str) -> None:
        key = title.lower().strip()
        if not title or len(title) < 5 or len(title) > 120 or key in seen_titles:
            return
        seen_titles.add(key)
        url = resolve_url(href, careers_url) if href else careers_url
        jobs.append({"title": title, "url": url, "description": desc[:400], "company": company_name})

    for sel in [
        "article", ".job", ".position", ".opening", ".career",
        "[class*='job-']", "[class*='-job']", "[class*='position']",
        "[class*='role']", "[class*='listing']", "[class*='vacancy']",
    ]:
        for el in soup.select(sel):
            heading = el.find(["h1", "h2", "h3", "h4"])
            link_el = el.find("a", href=True)
            if heading:
                add_job(heading.get_text(strip=True),
                        link_el["href"] if link_el else "",
                        el.get_text(" ", strip=True))
            elif link_el:
                add_job(link_el.get_text(strip=True),
                        link_el["href"],
                        el.get_text(" ", strip=True))

    # Fallback: anchor text that reads like an IT job title
    if not jobs:
        it_terms = {
            "technician", "support", "help desk", "helpdesk", "noc",
            "analyst", "specialist", "administrator", "coordinator",
            "consultant", "desk", "network",
        }
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if any(t in text.lower() for t in it_terms):
                add_job(text, a["href"], "")

    return jobs


# ── Built In Austin: daily job search ────────────────────────────────────────

def search_builtin_jobs() -> list[dict]:
    """Search Built In Austin job listings (runs every day)."""
    jobs: list[dict] = []
    seen_titles: set[str] = set()

    for url in BUILTIN_JOB_SEARCHES:
        r = fetch(url)
        if not r:
            continue

        soup = BeautifulSoup(r.text, "lxml")
        for card in soup.select(
            "[data-id], .job-card, [class*='job-card'], "
            "[class*='JobCard'], [class*='job_card'], article"
        ):
            heading = card.find(["h2", "h3", "h4"])
            link_el = card.find("a", href=True)
            if not heading and not link_el:
                continue

            title = (heading or link_el).get_text(strip=True)
            key = title.lower().strip()
            if not title or len(title) < 5 or key in seen_titles:
                continue
            seen_titles.add(key)

            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://www.builtinaustin.com" + href

            desc = card.get_text(" ", strip=True)[:400]
            jobs.append({
                "title": title,
                "url": href or url,
                "description": desc,
                "company": "Built In Austin",
            })

        time.sleep(2)

    return jobs


# ── Built In Austin: weekly company directory crawl ──────────────────────────

def _is_it_relevant_company(name: str, description: str, tags: list[str]) -> bool:
    """Return True if a company likely has IT support/ops roles."""
    text = (name + " " + description + " " + " ".join(tags)).lower()
    return any(signal in text for signal in IT_COMPANY_SIGNALS)


def _extract_company_website(card: BeautifulSoup) -> str:
    """Try to pull the company's external website from a Built In Austin company card."""
    # Look for a link that goes off-site (not back to builtinaustin.com)
    for a in card.find_all("a", href=True):
        href = a["href"]
        if href.startswith("http") and "builtinaustin.com" not in href:
            return href
    return ""


def crawl_builtin_directory(known_bases: set[str]) -> list[dict]:
    """
    Crawl Built In Austin's company directory pages to find new Austin companies
    that might have entry-level IT roles. Returns a list of newly discovered
    company dicts (name, base, source, builtin_url, first_seen).
    """
    discovered: list[dict] = []
    today_str = date.today().isoformat()

    for base_url in BUILTIN_COMPANY_PAGES:
        print(f"  [Directory] Crawling {base_url}")
        for page_num in range(1, BUILTIN_MAX_PAGES + 1):
            page_url = f"{base_url}{'&' if '?' in base_url else '?'}page={page_num}" if page_num > 1 else base_url
            r = fetch(page_url, timeout=20)
            if not r:
                break

            soup = BeautifulSoup(r.text, "lxml")

            # Detect company cards — Built In Austin uses various class patterns
            cards = soup.select(
                "[class*='company-card'], [class*='CompanyCard'], "
                "[class*='company_card'], [class*='company-tile'], "
                "article, [data-company-id], [data-id]"
            )

            if not cards:
                break   # no results on this page → stop paginating

            found_any_new = False
            for card in cards:
                # Extract company name
                name_el = card.find(["h2", "h3", "h4", "strong"])
                if not name_el:
                    continue
                name = name_el.get_text(strip=True)
                if not name or len(name) > 100:
                    continue

                # Extract description / tags
                desc_el = card.find("p")
                desc = desc_el.get_text(strip=True) if desc_el else ""
                tags = [t.get_text(strip=True) for t in card.select("[class*='tag'], [class*='Tag'], li")]

                # Built In Austin profile link
                builtin_link = card.find("a", href=True)
                builtin_url = ""
                if builtin_link:
                    href = builtin_link["href"]
                    builtin_url = href if href.startswith("http") else "https://www.builtinaustin.com" + href

                # External website
                website = _extract_company_website(card)

                if not website and not builtin_url:
                    continue

                # Skip if already in our watch list
                if website and any(
                    known.rstrip("/") in website or website in known
                    for known in known_bases
                ):
                    continue

                if not _is_it_relevant_company(name, desc, tags):
                    continue

                # New, relevant company — add it
                discovered.append({
                    "name": name,
                    "base": website,
                    "builtin_url": builtin_url,
                    "source": "builtinaustin_directory",
                    "first_seen": today_str,
                    "last_checked": None,
                    "has_it_roles": False,   # will be set True when a qualifying job is found
                })
                found_any_new = True

            if not found_any_new:
                break   # page had no new companies → stop paginating

            time.sleep(2)
        time.sleep(1)

    return discovered


def should_run_discovery(last_crawl: str | None) -> bool:
    if last_crawl is None:
        return True
    try:
        last_date = datetime.fromisoformat(last_crawl).date()
        return (date.today() - last_date).days >= DISCOVERY_INTERVAL_DAYS
    except ValueError:
        return True


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(
    jobs: list[dict],
    new_company_count: int,
    sender: str,
    password: str,
    recipient: str,
) -> None:
    today = date.today().strftime("%B %d, %Y")
    count = len(jobs)

    discovery_note = (
        f'<p style="color:#1565c0;font-size:13px;background:#e3f2fd;'
        f'padding:10px 14px;border-radius:6px;margin-bottom:16px;">'
        f'<strong>New this week:</strong> {new_company_count} Austin '
        f'company{"s" if new_company_count != 1 else ""} added to your watch list '
        f'from Built In Austin\'s company directory.'
        f'</p>'
    ) if new_company_count > 0 else ""

    if not jobs:
        subject = f"Job Scout – No new listings today ({today})"
        html = f"""
        <html><body style="font-family:Arial,sans-serif;max-width:660px;margin:0 auto;padding:24px;">
          <h2 style="color:#333;">Job Scout – {today}</h2>
          {discovery_note}
          <p style="color:#666;">No new entry-level IT listings found today. Check back tomorrow!</p>
        </body></html>
        """
    else:
        subject = f"Job Scout – {count} new IT listing{'s' if count != 1 else ''} ({today})"

        rows = ""
        for j in sorted(jobs, key=lambda x: x["score"], reverse=True):
            if j["score"] >= 7:
                badge_bg, badge_label = "#2e7d32", "Strong"
            elif j["score"] >= 4:
                badge_bg, badge_label = "#e65100", "Good"
            else:
                badge_bg, badge_label = "#757575", "Possible"

            desc = j.get("description", "")
            snippet = (desc[:180] + "…") if len(desc) > 180 else desc

            source_tag = (
                '<span style="font-size:11px;color:#1565c0;background:#e3f2fd;'
                'padding:2px 7px;border-radius:10px;margin-left:6px;">new company</span>'
            ) if j.get("newly_discovered") else ""

            rows += f"""
            <tr>
              <td style="padding:14px 12px;border-bottom:1px solid #f0f0f0;vertical-align:top;">
                <a href="{j['url']}"
                   style="font-size:15px;font-weight:600;color:#1a73e8;text-decoration:none;">
                  {j['title']}
                </a>{source_tag}<br>
                <span style="font-size:13px;color:#555;margin-top:3px;display:inline-block;">
                  {j['company']}
                </span><br>
                <span style="font-size:12px;color:#888;margin-top:5px;display:inline-block;">
                  {snippet}
                </span>
              </td>
              <td style="padding:14px 12px;border-bottom:1px solid #f0f0f0;
                         text-align:center;vertical-align:top;white-space:nowrap;">
                <span style="background:{badge_bg};color:#fff;padding:5px 11px;
                             border-radius:14px;font-size:12px;font-weight:700;
                             display:inline-block;margin-bottom:4px;">
                  {j['score']}/10
                </span><br>
                <span style="font-size:11px;color:{badge_bg};font-weight:600;">
                  {badge_label}
                </span>
              </td>
            </tr>
            """

        html = f"""
        <html><body style="font-family:Arial,sans-serif;max-width:660px;margin:0 auto;padding:24px;">
          <h2 style="color:#1a1a1a;margin-bottom:4px;">Job Scout – {today}</h2>
          <p style="color:#666;margin-top:6px;">
            Found <strong>{count}</strong> new entry-level IT
            listing{'s' if count != 1 else ''} matching your profile.
            Sorted by match score. Already-seen listings won't repeat.
          </p>
          {discovery_note}
          <table style="width:100%;border-collapse:collapse;margin-top:8px;">
            <thead>
              <tr style="background:#f7f7f7;border-bottom:2px solid #e0e0e0;">
                <th style="padding:10px 12px;text-align:left;
                           font-size:12px;color:#555;font-weight:700;letter-spacing:.5px;">
                  POSITION
                </th>
                <th style="padding:10px 12px;text-align:center;
                           font-size:12px;color:#555;font-weight:700;letter-spacing:.5px;">
                  MATCH
                </th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
          <p style="font-size:11px;color:#bbb;margin-top:20px;line-height:1.6;">
            Score (0–10): role type match + entry-level signals + CompTIA A+/Network+ cert
            mentions + skill overlap + Austin/remote location.
            Jobs requiring 3+ years or senior/lead titles are filtered automatically.
            Watch list grows weekly as new Austin companies are discovered on Built In Austin.
          </p>
        </body></html>
        """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())

    print(f"Email sent → {subject}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    seen = load_seen()
    discovered_data = load_discovered()
    today_str = date.today().isoformat()
    new_company_count = 0

    # Build the full company list: static + previously discovered
    all_companies = list(STATIC_COMPANIES)
    known_bases = {c["base"].rstrip("/") for c in all_companies}

    for dc in discovered_data.get("companies", []):
        if dc.get("base") and dc["base"].rstrip("/") not in known_bases:
            all_companies.append({"name": dc["name"], "base": dc["base"]})
            known_bases.add(dc["base"].rstrip("/"))

    # ── Weekly directory crawl ────────────────────────────────────────────────
    if should_run_discovery(discovered_data.get("last_directory_crawl")):
        print("=== Weekly Built In Austin directory crawl ===")
        newly_found = crawl_builtin_directory(known_bases)
        print(f"  → {len(newly_found)} new IT-relevant company candidate(s) found")

        # Check each new candidate for actual IT job postings
        confirmed = []
        for candidate in newly_found:
            if not candidate["base"]:
                # No external website — use the Built In Austin profile as the jobs source
                candidate["has_it_roles"] = True  # discovered via IT-tagged search anyway
                confirmed.append(candidate)
                continue

            print(f"  Checking career page for {candidate['name']}…")
            careers_url = find_careers_url(candidate["base"])
            if careers_url:
                jobs = extract_jobs(candidate["name"], careers_url)
                has_it = any(score_job(j["title"], j.get("description", "")) >= MIN_SCORE for j in jobs)
                if has_it:
                    candidate["has_it_roles"] = True
                    confirmed.append(candidate)
                    print(f"    ✓ IT roles found — added to watch list")
                else:
                    print(f"    – No relevant IT roles right now (skipping)")
            else:
                # Can't confirm now but keep as candidate for future checks
                confirmed.append(candidate)
                print(f"    ? No career page found — adding anyway for future monitoring")

            time.sleep(1)

        # Persist new companies and update crawl timestamp
        existing_names = {c["name"].lower() for c in discovered_data.get("companies", [])}
        for c in confirmed:
            if c["name"].lower() not in existing_names:
                discovered_data["companies"].append(c)
                existing_names.add(c["name"].lower())
                # Add to today's run immediately
                if c.get("base") and c["base"].rstrip("/") not in known_bases:
                    all_companies.append({"name": c["name"], "base": c["base"]})
                    known_bases.add(c["base"].rstrip("/"))

        new_company_count = len(confirmed)
        discovered_data["last_directory_crawl"] = today_str
        save_discovered(discovered_data)
        print(f"=== {new_company_count} company(s) added to watch list ===\n")

    # Update last_checked for discovered companies
    discovered_names = {c["name"].lower() for c in discovered_data.get("companies", [])}

    # ── Daily job scraping ────────────────────────────────────────────────────
    raw_jobs: list[dict] = []

    for company in all_companies:
        print(f"[{company['name']}] Finding careers page…")
        careers_url = find_careers_url(company["base"])
        if careers_url:
            print(f"  → {careers_url}")
            found = extract_jobs(company["name"], careers_url)
            is_newly_discovered = company["name"].lower() in discovered_names
            for j in found:
                j["newly_discovered"] = is_newly_discovered
            print(f"  → {len(found)} listing(s)")
            raw_jobs.extend(found)
        else:
            print(f"  → No careers page found")
        time.sleep(1)

    # ── Built In Austin job search (daily) ───────────────────────────────────
    print("[Built In Austin] Searching job listings…")
    builtin_jobs = search_builtin_jobs()
    print(f"  → {len(builtin_jobs)} listing(s)")
    raw_jobs.extend(builtin_jobs)

    # ── Score, deduplicate, filter ────────────────────────────────────────────
    new_jobs: list[dict] = []

    for job in raw_jobs:
        jid = job_id(job["title"], job["company"], job["url"])
        if jid in seen:
            continue

        s = score_job(job["title"], job.get("description", ""))

        # Mark as seen regardless of score (avoids re-scoring low matches daily)
        seen[jid] = {
            "title": job["title"],
            "company": job["company"],
            "score": s,
            "date": today_str,
        }

        if s >= MIN_SCORE:
            job["score"] = s
            new_jobs.append(job)

            # Update has_it_roles flag on discovered companies that produce matches
            for dc in discovered_data.get("companies", []):
                if dc["name"].lower() == job["company"].lower() and not dc.get("has_it_roles"):
                    dc["has_it_roles"] = True

    save_seen(seen)
    save_discovered(discovered_data)
    print(f"New qualifying jobs: {len(new_jobs)} (of {len(raw_jobs)} total found)")

    send_email(
        new_jobs,
        new_company_count=new_company_count,
        sender=os.environ["GMAIL_USER"],
        password=os.environ["GMAIL_APP_PASSWORD"],
        recipient=os.environ.get("RECIPIENT_EMAIL", os.environ["GMAIL_USER"]),
    )


if __name__ == "__main__":
    main()
