"""Map freeform location strings onto ISO country codes.

Deliberately small: it only needs to resolve the places you would actually move
to, plus the handful of phrasings boards use for "remote, but only from here".
"""

from __future__ import annotations

import re

from carodi.models import Opportunity, Remote, slug

# country code -> phrases that imply it. Cities included because postings very
# often say "Berlin" and never say "Germany".
COUNTRY_HINTS: dict[str, tuple[str, ...]] = {
    "GB": ("united kingdom", "uk", "england", "scotland", "wales", "london", "manchester",
           "cambridge", "oxford", "edinburgh", "bristol", "leeds", "glasgow"),
    "IE": ("ireland", "dublin", "cork", "galway"),
    "DE": ("germany", "deutschland", "berlin", "munich", "münchen", "hamburg", "frankfurt",
           "cologne", "köln", "stuttgart", "karlsruhe", "leipzig"),
    "NL": ("netherlands", "holland", "amsterdam", "rotterdam", "utrecht", "eindhoven",
           "the hague", "den haag", "delft"),
    "SE": ("sweden", "stockholm", "gothenburg", "göteborg", "malmö", "lund"),
    "DK": ("denmark", "copenhagen", "københavn", "aarhus"),
    "NO": ("norway", "oslo", "bergen", "trondheim"),
    "FI": ("finland", "helsinki", "espoo", "tampere"),
    "FR": ("france", "paris", "lyon", "toulouse", "grenoble", "nantes", "sophia antipolis"),
    "ES": ("spain", "madrid", "barcelona", "valencia", "seville", "málaga"),
    "PT": ("portugal", "lisbon", "lisboa", "porto", "braga"),
    "IT": ("italy", "milan", "milano", "rome", "roma", "turin", "torino", "bologna"),
    "CH": ("switzerland", "zurich", "zürich", "geneva", "lausanne", "basel", "bern"),
    "AT": ("austria", "vienna", "wien", "graz", "linz"),
    "BE": ("belgium", "brussels", "antwerp", "ghent", "leuven"),
    "PL": ("poland", "warsaw", "krakow", "kraków", "wroclaw", "gdansk"),
    "CZ": ("czech", "prague", "praha", "brno"),
    "EE": ("estonia", "tallinn", "tartu"),
    "LU": ("luxembourg",),
    "US": ("united states", "usa", "u.s.", "new york", "san francisco", "seattle", "austin",
           "boston", "chicago", "denver", "los angeles", "california", "texas", "remote us",
           "us remote", "nyc", "bay area"),
    "CA": ("canada", "toronto", "vancouver", "montreal", "ottawa", "waterloo"),
    "TR": ("turkey", "türkiye", "istanbul", "ankara", "izmir"),
    "AE": ("uae", "united arab emirates", "dubai", "abu dhabi"),
    "JO": ("jordan", "amman"),
    "CY": ("cyprus", "nicosia", "lefkosa", "lefkoşa", "limassol", "larnaca", "famagusta"),
    # Not targets, but recognizing them matters: an unrecognized location
    # otherwise falls through to a description scan and can be mislabelled.
    "CN": ("china", "beijing", "shanghai", "shenzhen", "hangzhou", "guangzhou"),
    "IN": ("india", "bengaluru", "bangalore", "hyderabad", "mumbai", "pune", "gurgaon",
           "gurugram", "chennai", "delhi", "noida"),
    "JP": ("japan", "tokyo", "osaka", "kyoto"),
    "KR": ("south korea", "seoul"),
    "SG": ("singapore",),
    "AU": ("australia", "sydney", "melbourne", "brisbane", "perth"),
    "NZ": ("new zealand", "auckland", "wellington"),
    "BR": ("brazil", "brasil", "são paulo", "sao paulo", "rio de janeiro", "belo horizonte"),
    "MX": ("mexico", "méxico", "mexico city", "guadalajara", "monterrey"),
    "AR": ("argentina", "buenos aires", "córdoba"),
    "IL": ("israel", "tel aviv", "herzliya", "haifa"),
    "ZA": ("south africa", "cape town", "johannesburg"),
    "NG": ("nigeria", "lagos", "abuja"),
    "KE": ("kenya", "nairobi"),
    "EG": ("egypt", "cairo", "alexandria"),
    "SA": ("saudi arabia", "riyadh", "jeddah"),
    "UA": ("ukraine", "kyiv", "kiev", "lviv"),
    "RO": ("romania", "bucharest", "cluj", "cluj napoca", "iasi", "timisoara"),
    "BG": ("bulgaria", "sofia", "plovdiv"),
    "HU": ("hungary", "budapest"),
    "GR": ("greece", "athens", "thessaloniki"),
    "HR": ("croatia", "zagreb", "split"),
    "RS": ("serbia", "belgrade", "novi sad"),
    "LT": ("lithuania", "vilnius", "kaunas"),
    "LV": ("latvia", "riga"),
    "SK": ("slovakia", "bratislava", "kosice"),
    "SI": ("slovenia", "ljubljana"),
    "PH": ("philippines", "manila", "cebu"),
    "ID": ("indonesia", "jakarta"),
    "VN": ("vietnam", "hanoi", "ho chi minh"),
    "PK": ("pakistan", "lahore", "karachi", "islamabad"),
}

#: Location strings that name no place at all. Only for these is it safe to
#: fall back to scanning the description for a country.
_UNINFORMATIVE = frozenset(
    {
        "", "remote", "in office", "in-office", "hybrid", "onsite", "on site", "on-site",
        "various", "various locations", "multiple locations", "multiple", "global",
        "worldwide", "anywhere", "flexible", "distributed", "tbd", "n a", "unspecified",
        "other", "office", "hq",
    }
)

# Phrases meaning "remote but geographically restricted", which is the single
# most common way a promising listing turns out to be inapplicable.
_EU_WIDE = ("europe", "eu ", "eu-", "emea", "european union", "eea", "cet timezone", "cest")

_ANYWHERE = ("worldwide", "anywhere in the world", "global", "any location", "fully distributed")

_PHRASE_CACHE = [(cc, tuple(slug(p) for p in hints)) for cc, hints in COUNTRY_HINTS.items()]


def detect_countries(text: str) -> list[str]:
    """Return every country code plausibly referenced by a location string."""
    if not text:
        return []
    hay = f" {slug(text)} "
    found = []
    for code, phrases in _PHRASE_CACHE:
        for phrase in phrases:
            # Word-boundary match so 'uk' does not fire inside 'ukraine'.
            if re.search(rf"(?<![\w]){re.escape(phrase)}(?![\w])", hay):
                found.append(code)
                break
    return found


def uninformative_location(location: str) -> bool:
    """True when the location field names no place, only a work arrangement."""
    return slug(location) in _UNINFORMATIVE


def annotate(opp: Opportunity) -> None:
    """Fill in `countries` and flag geographic openness of remote roles."""
    text = f"{opp.location_raw} {opp.title}"
    codes = detect_countries(text)

    # Fall back to the description only when the location field named no place
    # at all. If it says "Beijing, China" and we simply do not recognize it,
    # scanning the description would find the company's US offices and
    # confidently mislabel a Beijing role as American.
    if not codes and opp.description and uninformative_location(opp.location_raw):
        codes = detect_countries(opp.description[:600])

    opp.countries = sorted(set(codes))

    hay = f" {slug(text)} "
    opp.enrichment["region_eu_wide"] = any(p in hay for p in _EU_WIDE)
    opp.enrichment["region_anywhere"] = any(p in hay for p in _ANYWHERE)

    # A remote role with no country signal at all is usually genuinely open.
    if opp.remote is Remote.REMOTE and not codes:
        opp.enrichment["region_anywhere"] = True
