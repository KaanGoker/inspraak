"""Live check of the outreach facts against the Have Your Say backend: deadline, counts by
submitter type, organisation rows, and whether the organisation already answered any stage
of the same initiative. Citizen rows are counted, never named. One request per 1.1 s.
Usage: python scripts/outreach_check.py out.json"""

import collections
import datetime
import json
import sys
import time
import zoneinfo

import httpx

B = "https://ec.europa.eu/info/law/better-regulation"
c = httpx.Client(timeout=60, headers={"User-Agent": "inspraak/0.1 (research snapshot, 1 req/s)"})


def get(url, params=None):
    time.sleep(1.1)
    r = c.get(url, params=params)
    r.raise_for_status()
    return r.json()


targets = {  # tr_id: (publication, initiative)
    "58744833263-19": (25794, 14549),
    "1414929419-24": (25794, 14549),
    "16311905144-06": (26173, 19418),
    "79360311166-84": (26173, 19418),
    "92545571128-74": (26170, 16352),
    "47806848200-34": (26097, 17712),
    "9505781573-45": (23919, 15252),
    "501222919-71": (25981, 15492),
    "12493392840-79": (25981, 15492),
    "68004524380-10": (24198, 14628),
}
start = datetime.datetime.now(zoneinfo.ZoneInfo("Europe/Brussels"))
out = {"checked_from": start.isoformat(timespec="minutes"), "initiatives": {}, "publications": {}}
feedback_cache = {}


def all_feedback(pid):
    if pid in feedback_cache:
        return feedback_cache[pid]
    rows, page, pages = [], 0, None
    while pages is None or page < pages:
        p = get(
            f"{B}/api/allFeedback",
            {"publicationId": pid, "size": 100, "page": page, "sort": "dateFeedback,ASC"},
        )
        pages = int(p.get("totalPages") or 0)
        total = p.get("totalElements")
        for r in p.get("content") or []:
            citizen = r.get("userType") in ("EU_CITIZEN", "NON_EU_CITIZEN")
            rows.append(
                {
                    "type": r.get("userType"),
                    "date": r.get("dateFeedback"),
                    "org": None if citizen else r.get("organization"),
                    "tr": None if citizen else r.get("trNumber"),
                    "country": r.get("country"),
                }
            )
        page += 1
    feedback_cache[pid] = (rows, total)
    return rows, total


for ini in sorted({v[1] for v in targets.values()}):
    d = get(f"{B}/brpapi/groupInitiatives/{ini}")
    pubs = [
        {
            "id": p["id"],
            "type": p.get("type"),
            "end": p.get("endDate"),
            "total": p.get("totalFeedback"),
            "status": p.get("receivingFeedbackStatus") or p.get("frontEndStage"),
        }
        for p in d.get("publications", [])
    ]
    out["initiatives"][ini] = {"short_title": d.get("shortTitle"), "publications": pubs}
for tr, (pid, ini) in targets.items():
    rows, total = all_feedback(pid)
    types = collections.Counter(r["type"] for r in rows)
    orgs = [
        (r["date"][:10] if r["date"] else None, r["type"], r["org"], r["tr"], r["country"])
        for r in rows
        if r["org"] is not None
    ]
    # own submissions on any publication of the same initiative with feedback
    own = []
    for p in out["initiatives"][ini]["publications"]:
        if (p["total"] or 0) == 0:
            continue
        if (p["total"] or 0) > 3000:
            continue
        prow, _ = all_feedback(p["id"])
        own += [(p["id"], r["date"][:10] if r["date"] else None) for r in prow if r["tr"] == tr]
    out["publications"][str(pid)] = {"total": total, "types": dict(types), "orgs": orgs}
    out.setdefault("own", {})[tr] = own
out["checked_to"] = datetime.datetime.now(zoneinfo.ZoneInfo("Europe/Brussels")).isoformat(
    timespec="minutes"
)
json.dump(out, open(sys.argv[1], "w"), indent=1, ensure_ascii=False, default=str)
print("ok", out["checked_from"], out["checked_to"])
