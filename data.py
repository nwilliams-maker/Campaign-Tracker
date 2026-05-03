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
        venue { id venueName city state }
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
def fetch_open_tasks() -> list:
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
        if not last or not tasks or page > 200:
            break
    return out


@st.cache_data(ttl=300, show_spinner=False)
def fetch_completed_tasks_for_kids(kids: tuple, max_pages: int = 2000) -> list:
    """Pull state=3 tasks since CUTOFF, keep only those whose kioskId is in kids."""
    needed = {str(k).strip().upper() for k in kids if k}
    if not needed:
        return []
    headers = _onfleet_headers()
    out, last, page = [], None, 0
    while True:
        page += 1
        params = {"state": "3", "from": str(CUTOFF_MS)}
        if last:
            params["lastId"] = last
        url = f"{ONFLEET_BASE}/tasks/all?" + urllib.parse.urlencode(params)
        body = _get(url, headers=headers)
        tasks = body.get("tasks") or []
        for t in tasks:
            if _kid_of(t) in needed:
                out.append(t)
        last = body.get("lastId")
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
    """{kid: [{ts_ms, dt_iso, ids, task_id, short_id, task_type, wo_number}, ...]} newest first."""
    by = defaultdict(list)
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
        by[kid].append({
            "ts_ms": int(ts_ms) if ts_ms else 0,
            "dt_iso": dt_iso,
            "ids": ids,
            "task_id": t.get("id") or "",
            "short_id": t.get("shortId") or "",
            "task_type": md.get("taskType") or md.get("Task Type") or "",
            "wo_number": md.get("woNumber") or md.get("WO Number") or "",
        })
    for kid, lst in by.items():
        lst.sort(key=lambda r: r["ts_ms"], reverse=True)
    return dict(by)


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
