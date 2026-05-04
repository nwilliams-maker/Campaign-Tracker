"""Data layer — Terraboost GraphQL + Onfleet API.

All public functions are wrapped in @st.cache_data with TTL so the Streamlit
app stays fast on subsequent loads. The cache can be cleared with the Refresh
button on the dashboard.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

import streamlit as st

GQL_URL = "https://be-terraboost-v3.terraboost.com/graphql"
ONFLEET_BASE = "https://onfleet.com/api/v2"
HTTP_TIMEOUT = 60

# Cutoff: Sept 1 2025 UTC, in ms epoch (for Onfleet) and ISO (for Terraboost)
CUTOFF_DT = datetime(2025, 9, 1, tzinfo=timezone.utc)
CUTOFF_MS = int(CUTOFF_DT.timestamp() * 1000)
CUTOFF_ISO = CUTOFF_DT.strftime("%Y-%m-%dT00:00:00.000Z")

# Active-pipeline campaign statuses
ACTIVE_PIPELINE_STATUS_IDS = [6, 8, 9, 90]
ART_APPROVED_STATUS_ID = 6


# -----------------------------------------------------------------------------
# HTTP
# -----------------------------------------------------------------------------
def _env(name: str) -> str:
    v = os.environ.get(name) or ""
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def _post(url: str, payload: dict, headers: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8")
    h = {"content-type": "application/json", "accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _get(url: str, headers: dict | None = None) -> dict:
    h = {"accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, method="GET")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


# -----------------------------------------------------------------------------
# Terraboost
# -----------------------------------------------------------------------------
LOGIN_M = """mutation Login($email: String!, $password: String!) {
  login(input: {email: $email, password: $password}) { token }
}"""


def _tb_login() -> str:
    body = _post(GQL_URL, {
        "query": LOGIN_M,
        "variables": {"email": _env("TERRABOOST_EMAIL"), "password": _env("TERRABOOST_PASSWORD")},
    })
    if body.get("errors"):
        raise RuntimeError(f"Terraboost login failed: {body['errors']}")
    tok = ((body.get("data") or {}).get("login") or {}).get("token")
    if not tok:
        raise RuntimeError("Terraboost login: empty token")
    return tok


def _tb_query(token: str, query: str, variables: dict | None = None) -> dict:
    body = _post(GQL_URL, {"query": query, "variables": variables or {}},
                 headers={"authorization": f"Bearer {token}"})
    if body.get("errors"):
        raise RuntimeError(f"Terraboost GraphQL error: {body['errors'][0].get('message','')}")
    return body.get("data") or {}


Q_STATUSES = "query Statuses { statuses { id statusName statusGroup } }"

Q_ACTIVE_CAMPAIGNS = """query ActiveCampaigns($sids: [Int!]) {
  campaigns(where: {statusId: {in: $sids}}) {
    id name orderNumber statusId
    signedDate estimatedStartDate estimatedEndDate createdDate
  }
}"""

# Filter logs to Sept 2025 onward — keeps the response small.
Q_ART_LOGS = """query ArtLogs($sid: Int!, $cutoff: DateTime!) {
  campaignStatusLogs(
    where: {statusId: {eq: $sid}, createdDate: {gte: $cutoff}}
    order: {createdDate: DESC}
  ) {
    campaignId createdDate statusId
  }
}"""

# All status logs for active campaigns (so we can show a timeline per campaign)
Q_ALL_LOGS = """query AllLogs($cids: [Int!], $cutoff: DateTime!) {
  campaignStatusLogs(
    where: {campaignId: {in: $cids}, createdDate: {gte: $cutoff}}
    order: {createdDate: ASC}
  ) {
    campaignId createdDate statusId
  }
}"""

Q_VENUES_BY_KIDS = """query VenuesByKids($kids: [String!]) {
  kiosks(where: {importKioskId: {in: $kids}}) {
    importKioskId
    venueId
    venue { id venueName address1 address2 city state zip }
  }
}"""

Q_CAMPAIGN_KIOSKS = """query CK($cids: [Int!]) {
  campaigns(where: {id: {in: $cids}}) {
    id
    campaignKiosks {
      id reservationStart reservationEnd boosted installDate installedImageUrl
      printCollection { id collectionName topFileUrl bottomFileUrl }
      kiosk {
        importKioskId isDigital
        kioskType { typeName }
        kioskLocation { typeName }
        venue { id venueName address1 address2 city state zip }
      }
    }
  }
}"""


@st.cache_data(ttl=300, show_spinner=False)
def fetch_status_names() -> dict:
    token = _tb_login()
    data = _tb_query(token, Q_STATUSES)
    return {s["id"]: s["statusName"] for s in (data.get("statuses") or [])}


@st.cache_data(ttl=300, show_spinner=False)
def fetch_active_campaigns() -> list:
    token = _tb_login()
    data = _tb_query(token, Q_ACTIVE_CAMPAIGNS, {"sids": ACTIVE_PIPELINE_STATUS_IDS})
    return data.get("campaigns") or []


@st.cache_data(ttl=300, show_spinner=False)
def fetch_art_approved_dates() -> dict:
    """{campaignId: latest art-approved iso datetime}."""
    token = _tb_login()
    data = _tb_query(token, Q_ART_LOGS, {"sid": ART_APPROVED_STATUS_ID, "cutoff": CUTOFF_ISO})
    out = {}
    for log in data.get("campaignStatusLogs") or []:
        cid = log.get("campaignId")
        cd = log.get("createdDate")
        if cid is None or not cd:
            continue
        if cid not in out:  # DESC order, first wins
            out[cid] = cd
    return out


@st.cache_data(ttl=300, show_spinner=False)
def fetch_all_logs(campaign_ids: tuple) -> dict:
    """Returns {campaignId: [{createdDate, statusId}, ...]} for timeline display."""
    if not campaign_ids:
        return {}
    token = _tb_login()
    data = _tb_query(token, Q_ALL_LOGS, {"cids": list(campaign_ids), "cutoff": CUTOFF_ISO})
    out = defaultdict(list)
    for log in data.get("campaignStatusLogs") or []:
        cid = log.get("campaignId")
        if cid is None:
            continue
        out[cid].append({"createdDate": log.get("createdDate"), "statusId": log.get("statusId")})
    return dict(out)


@st.cache_data(ttl=300, show_spinner=False)
@st.cache_data(ttl=300, show_spinner=False)
def fetch_venues_for_kids(kids: tuple, batch: int = 200) -> dict:
    """Returns {importKioskId: venue_dict}. Bypasses the campaigns->kiosk->venue
    path which Terraboost returns as null; queries kiosks-by-KID directly where
    venue data is populated."""
    if not kids:
        return {}
    token = _tb_login()
    out: dict = {}
    kid_list = list(kids)
    for i in range(0, len(kid_list), batch):
        chunk = kid_list[i:i + batch]
        data = _tb_query(token, Q_VENUES_BY_KIDS, {"kids": chunk})
        for k in data.get("kiosks") or []:
            kid = (k.get("importKioskId") or "").strip().upper()
            if kid:
                out[kid] = k.get("venue") or {}
                if k.get("venueId") and not out[kid].get("id"):
                    out[kid]["id"] = k.get("venueId")
    return out


def fetch_kiosks_for_campaigns(campaign_ids: tuple, batch: int = 50) -> dict:
    if not campaign_ids:
        return {}
    token = _tb_login()
    out: dict = {}
    cids = list(campaign_ids)
    for i in range(0, len(cids), batch):
        chunk = cids[i:i + batch]
        data = _tb_query(token, Q_CAMPAIGN_KIOSKS, {"cids": chunk})
        for c in data.get("campaigns") or []:
            out[c["id"]] = c.get("campaignKiosks") or []

    # Terraboost schema quirk: campaign->kiosk->venue returns null. Hydrate via
    # a direct kiosks-by-KID lookup which DOES populate the venue.
    all_kids = []
    for cks in out.values():
        for ck in cks or []:
            kk = ((ck.get("kiosk") or {}).get("importKioskId") or "").strip().upper()
            if kk:
                all_kids.append(kk)
    venue_by_kid = fetch_venues_for_kids(tuple(sorted(set(all_kids))))
    for cks in out.values():
        for ck in cks or []:
            kiosk = ck.get("kiosk") or {}
            kk = (kiosk.get("importKioskId") or "").strip().upper()
            if kk in venue_by_kid:
                existing = kiosk.get("venue") or {}
                if not existing.get("venueName"):
                    kiosk["venue"] = venue_by_kid[kk]
    return out


# -----------------------------------------------------------------------------
# Persistent photo cache — survives Streamlit reruns / dyno restarts within a
# deployment. Stored as a single JSON blob on disk. Each refresh runs as an
# INCREMENTAL pull (only Onfleet tasks completed since last fetch) and merges
# into the existing cache.
# -----------------------------------------------------------------------------
PHOTOS_CACHE_FILE = "/tmp/campaign_tracker_photos.json"

def _load_photo_cache() -> dict:
    """Returns {"photos_by_kid": {...}, "last_fetch_ms": int}."""
    try:
        with open(PHOTOS_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"photos_by_kid": {}, "last_fetch_ms": 0}


def _save_photo_cache(photos_by_kid: dict, last_fetch_ms: int) -> None:
    try:
        with open(PHOTOS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"photos_by_kid": photos_by_kid, "last_fetch_ms": last_fetch_ms}, f)
    except Exception:
        pass


def _merge_photos(existing: dict, new: dict) -> dict:
    """Merge two {kid: [events]} dicts, dedupe by (ts_ms, ids[0])."""
    out = {k: list(v) for k, v in existing.items()}
    for kid, events in new.items():
        if kid not in out:
            out[kid] = events
            continue
        seen = {(e.get("ts_ms"), tuple(e.get("ids", []))) for e in out[kid]}
        for e in events:
            key = (e.get("ts_ms"), tuple(e.get("ids", [])))
            if key not in seen:
                out[kid].append(e)
                seen.add(key)
        out[kid].sort(key=lambda r: r.get("ts_ms", 0), reverse=True)
    return out


# -----------------------------------------------------------------------------
# Onfleet
# -----------------------------------------------------------------------------
def _onfleet_headers() -> dict:
    return {"Authorization": f"Basic {base64.b64encode((_env('ONFLEET_KEY') + ':').encode()).decode()}"}


def _meta(task: dict) -> dict:
    out = {}
    for cf in task.get("customFields") or []:
        if isinstance(cf, dict) and cf.get("key") is not None:
            out[str(cf["key"])] = cf.get("value")
    for m in task.get("metadata") or []:
        if isinstance(m, dict) and m.get("name") is not None:
            out[str(m["name"])] = m.get("value")
    return out


def _kid_of(task: dict) -> str:
    md = _meta(task)
    for k in ("kioskId", "kiosk_id", "KioskId", "KID"):
        v = md.get(k)
        if v:
            return str(v).strip().upper()
    return ""


def _sio_of(task: dict) -> str:
    md = _meta(task)
    for k in ("sio", "SIO", "Order Number", "orderNumber"):
        v = md.get(k)
        if v:
            return str(v).strip().upper()
    return ""


@st.cache_data(ttl=300, show_spinner=False)
def fetch_workers() -> dict:
    """Returns {worker_id: name}."""
    headers = _onfleet_headers()
    workers = _get(f"{ONFLEET_BASE}/workers", headers=headers)
    if isinstance(workers, list):
        return {w.get("id"): w.get("name") or "" for w in workers if w.get("id")}
    return {}


@st.cache_data(ttl=300, show_spinner=False)
def fetch_open_tasks(_progress=None) -> list:
    """state 0=unassigned, 1=assigned, 2=active."""
    headers = _onfleet_headers()
    out, last, page = [], None, 0
    while True:
        page += 1
        params = {"state": "0,1,2", "from": str(CUTOFF_MS)}
        if last:
            params["lastId"] = last
        url = f"{ONFLEET_BASE}/tasks/all?" + urllib.parse.urlencode(params)
        body = _get(url, headers=headers)
        tasks = body.get("tasks") or []
        out.extend(tasks)
        last = body.get("lastId")
        if _progress and (page % 10 == 0 or not last or not tasks):
            try: _progress(f"  · page {page} → {len(out)} tasks so far")
            except Exception: pass
        if not last or not tasks or page > 200:
            break
    return out


@st.cache_data(ttl=300, show_spinner=False)
def fetch_completed_tasks_for_kids(kids: tuple, max_pages: int = 2000, _progress=None) -> list:
    """Pull state=3 tasks since CUTOFF, keep only those whose kioskId is in kids.
    Incremental: uses last cached fetch timestamp as `from` so subsequent calls
    only return NEW completed tasks. Initial call uses CUTOFF_MS (Sept 2025)."""
    needed = {str(k).strip().upper() for k in kids if k}
    if not needed:
        return []
    cache = _load_photo_cache()
    from_ms = max(int(cache.get("last_fetch_ms") or 0), CUTOFF_MS)
    if _progress:
        try:
            if from_ms > CUTOFF_MS:
                _progress(f"  · incremental — fetching only since {datetime.fromtimestamp(from_ms/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
            else:
                _progress(f"  · first fetch — pulling everything since Sept 2025")
        except Exception: pass
    headers = _onfleet_headers()
    out, last, page = [], None, 0
    while True:
        page += 1
        params = {"state": "3", "from": str(from_ms)}
        if last:
            params["lastId"] = last
        url = f"{ONFLEET_BASE}/tasks/all?" + urllib.parse.urlencode(params)
        body = _get(url, headers=headers)
        tasks = body.get("tasks") or []
        for t in tasks:
            if _kid_of(t) in needed:
                out.append(t)
        last = body.get("lastId")
        if _progress and (page % 20 == 0 or not last or not tasks):
            try: _progress(f"  · page {page} → kept {len(out)} new matching photos")
            except Exception: pass
        if not last or not tasks or page > max_pages:
            break
    return out


# -----------------------------------------------------------------------------
# Indexers
# -----------------------------------------------------------------------------
def index_open_tasks_by_kid(open_tasks: list) -> dict:
    """{kid: [task, ...]}."""
    out = defaultdict(list)
    for t in open_tasks:
        kid = _kid_of(t)
        if kid:
            out[kid].append(t)
    return dict(out)


def index_open_tasks_by_sio(open_tasks: list) -> dict:
    out = defaultdict(list)
    for t in open_tasks:
        sio = _sio_of(t)
        if sio:
            out[sio].append(t)
    return dict(out)


def index_photos_by_kid(completed_tasks: list) -> dict:
    """Build {kid: [event,...]} from new tasks, merge with on-disk cache, persist back."""
    fresh = defaultdict(list)
    max_ts = 0
    for t in completed_tasks:
        kid = _kid_of(t)
        if not kid:
            continue
        cd = t.get("completionDetails") or {}
        ids = list(cd.get("photoUploadIds") or [])
        if not ids and cd.get("photoUploadId"):
            ids = [cd["photoUploadId"]]
        if not ids:
            continue
        ts_ms = cd.get("time") or t.get("timeLastModified") or 0
        try:
            dt_iso = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).isoformat()
        except Exception:
            dt_iso = ""
        md = _meta(t)
        fresh[kid].append({
            "ts_ms": int(ts_ms) if ts_ms else 0,
            "dt_iso": dt_iso,
            "ids": ids,
            "task_id": t.get("id") or "",
            "short_id": t.get("shortId") or "",
            "task_type": md.get("taskType") or md.get("Task Type") or "",
            "wo_number": md.get("woNumber") or md.get("WO Number") or "",
        })
        if ts_ms and int(ts_ms) > max_ts:
            max_ts = int(ts_ms)
    # Merge fresh with existing cache
    cache = _load_photo_cache()
    existing = cache.get("photos_by_kid") or {}
    merged = _merge_photos(existing, dict(fresh))
    # Persist back. Bump last_fetch_ms to either max_ts of new tasks or now (so next
    # incremental fetch starts where this one ended).
    new_last = max(max_ts, int(cache.get("last_fetch_ms") or 0)) or int(datetime.now(timezone.utc).timestamp() * 1000)
    _save_photo_cache(merged, new_last)
    return merged


# -----------------------------------------------------------------------------
# Big bundled fetcher — what app.py calls
# -----------------------------------------------------------------------------
def fetch_all() -> dict:
    """Pulls everything in dependency order. Each piece is cached with TTL=5min."""
    status_names = fetch_status_names()
    campaigns = fetch_active_campaigns()
    cids = tuple(c["id"] for c in campaigns)
    art_dates = fetch_art_approved_dates()
    all_logs = fetch_all_logs(cids)
    kiosks_by_cid = fetch_kiosks_for_campaigns(cids)

    # Hydrate venue data via direct kiosks-by-KID query (the campaign->kiosk
    # path returns venue=null for active-pipeline kiosks; this is a Terraboost
    # schema quirk.)
    all_kids = []
    for cks in kiosks_by_cid.values():
        for ck in cks or []:
            kk = ((ck.get("kiosk") or {}).get("importKioskId") or "").strip().upper()
            if kk:
                all_kids.append(kk)
    venue_by_kid = fetch_venues_for_kids(tuple(sorted(set(all_kids))))
    for cks in kiosks_by_cid.values():
        for ck in cks or []:
            kiosk = ck.get("kiosk") or {}
            kk = (kiosk.get("importKioskId") or "").strip().upper()
            if kk in venue_by_kid and not (kiosk.get("venue") or {}).get("venueName"):
                kiosk["venue"] = venue_by_kid[kk]

    open_tasks = fetch_open_tasks()
    workers = fetch_workers()
    of_by_kid = index_open_tasks_by_kid(open_tasks)
    of_by_sio = index_open_tasks_by_sio(open_tasks)

    # Build the kid universe from kiosks
    kid_universe = set()
    for cks in kiosks_by_cid.values():
        for ck in cks or []:
            k = ((ck.get("kiosk") or {}).get("importKioskId") or "").strip().upper()
            if k:
                kid_universe.add(k)

    completed = fetch_completed_tasks_for_kids(tuple(sorted(kid_universe)))
    photos_by_kid = index_photos_by_kid(completed)

    return {
        "status_names": status_names,
        "campaigns": campaigns,
        "art_dates": art_dates,
        "all_logs": all_logs,
        "kiosks_by_cid": kiosks_by_cid,
        "of_by_kid": of_by_kid,
        "of_by_sio": of_by_sio,
        "workers": workers,
        "photos_by_kid": photos_by_kid,
        "open_task_count": len(open_tasks),
        "completed_task_count": len(completed),
    }
