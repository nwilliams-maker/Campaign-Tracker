"""Campaign Tracker — Streamlit app.

Renders a single-page dashboard:
  - Top: KPIs + filter chips + search
  - Middle: Custom HTML table with inline-expandable rows
  - Each row shows: status pill, days since art approved, kiosk + venue, install date
  - Click row → expand to see photo gallery, status timeline, current WO + worker

All filtering/sorting/expansion happens client-side in the embedded component.
Data refresh is server-side, cached for 5 minutes via @st.cache_data.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import streamlit as st
import streamlit.components.v1 as components

import data

st.set_page_config(
    page_title="Campaign Tracker",
    page_icon="📍",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Hide Streamlit's chrome
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap');
[data-testid="stHeader"], footer, #MainMenu, [data-testid="stToolbar"] { display: none !important; }
.block-container { padding-top: 0 !important; padding-bottom: 0 !important; max-width: 100% !important; }
.stApp { background: #f5f5f7; font-family: 'Roboto', 'Helvetica Neue', Arial, sans-serif !important; }
section[data-testid="stSidebar"] { background: #ffffff; border-right: 1px solid rgba(0,0,0,0.12); }
section[data-testid="stSidebar"] button { background: #5300B2 !important; color: #fff !important; border: none !important; }
section[data-testid="stSidebar"] button:hover { background: #6a1ed1 !important; }
</style>
""", unsafe_allow_html=True)


# --- Data load ---------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False)
def _load_bundle():
    """Pulls everything in stages, showing progress for each. Cached for 5 min,
    so this body only runs on cache miss — subsequent loads are instant."""
    with st.status("Loading data — first load takes 2-3 min...", expanded=True) as status:
        status.update(label="[1/7] Logging in to Terraboost + fetching status names...")
        status_names = data.fetch_status_names()
        st.write(f"✓ {len(status_names)} status types")

        status.update(label="[2/7] Fetching active-pipeline campaigns (status 6/8/9/90)...")
        campaigns = data.fetch_active_campaigns()
        st.write(f"✓ {len(campaigns)} campaigns in active install pipeline")
        cids = tuple(c["id"] for c in campaigns)

        status.update(label="[3/7] Fetching art-approved dates (Sept 2025+ logs)...")
        art_dates = data.fetch_art_approved_dates()
        st.write(f"✓ {len(art_dates)} campaigns with art-approved log entries")

        status.update(label="[4/7] Fetching status timelines for active campaigns...")
        all_logs = data.fetch_all_logs(cids)
        log_count = sum(len(v) for v in all_logs.values())
        st.write(f"✓ {log_count} status-log entries across {len(all_logs)} campaigns")

        status.update(label=f"[5/7] Fetching kiosks for {len(campaigns)} campaigns (in batches of 50)...")
        kiosks_by_cid = data.fetch_kiosks_for_campaigns(cids)
        kiosk_count = sum(len(v or []) for v in kiosks_by_cid.values())
        st.write(f"✓ {kiosk_count} campaign-kiosks")

        status.update(label="[6/7] Pulling open Onfleet tasks + worker directory...")
        open_tasks = data.fetch_open_tasks(_progress=st.write)
        workers = data.fetch_workers()
        of_by_kid = data.index_open_tasks_by_kid(open_tasks)
        of_by_sio = data.index_open_tasks_by_sio(open_tasks)
        st.write(f"✓ {len(open_tasks)} open Onfleet tasks · {len(workers)} workers in directory")

        # Build kid universe for the photo-history pull
        kid_universe = set()
        for cks in kiosks_by_cid.values():
            for ck in cks or []:
                k = ((ck.get("kiosk") or {}).get("importKioskId") or "").strip().upper()
                if k:
                    kid_universe.add(k)

        status.update(label=f"[7/7] Pulling completed Onfleet tasks for photo history ({len(kid_universe)} kiosks, Sept 2025+) — slowest step, give it 1-2 min...")
        completed = data.fetch_completed_tasks_for_kids(tuple(sorted(kid_universe)), _progress=st.write)
        photos_by_kid = data.index_photos_by_kid(completed)
        photo_count = sum(sum(len(e["ids"]) for e in lst) for lst in photos_by_kid.values())
        st.write(f"✓ {len(completed)} completed tasks · {photo_count} photos across {len(photos_by_kid)} kiosks")

        status.update(label="✓ Loaded", state="complete", expanded=False)

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


def build_rows(bundle: dict) -> tuple[list, dict]:
    """Returns (rows, photos_by_kid)."""
    today = datetime.now(timezone.utc).date()
    status_names = bundle["status_names"]
    workers = bundle["workers"]
    of_by_kid = bundle["of_by_kid"]
    of_by_sio = bundle["of_by_sio"]
    art_dates = bundle["art_dates"]
    all_logs = bundle["all_logs"]
    kiosks_by_cid = bundle["kiosks_by_cid"]
    photos_by_kid = bundle["photos_by_kid"]

    rows = []
    for c in bundle["campaigns"]:
        cid = c["id"]
        camp_name = c.get("name") or ""
        sio = str(c.get("orderNumber") or "").strip().upper()
        status_id = c.get("statusId")
        status_name = status_names.get(status_id, f"statusId={status_id}")
        art_iso = art_dates.get(cid) or ""
        try:
            art_date = datetime.fromisoformat(art_iso[:19] + ("+00:00" if "+" not in art_iso else "")).date() if art_iso else None
        except Exception:
            art_date = None
        days_old = (today - art_date).days if art_date else None

        # Status timeline for this campaign
        timeline = []
        for ev in all_logs.get(cid, []):
            timeline.append({
                "date": (ev.get("createdDate") or "")[:10],
                "datetime": ev.get("createdDate") or "",
                "status_id": ev.get("statusId"),
                "status_name": status_names.get(ev.get("statusId"), ""),
            })

        for ck in kiosks_by_cid.get(cid, []) or []:
            kiosk = ck.get("kiosk") or {}
            venue = kiosk.get("venue") or {}
            pc = ck.get("printCollection") or {}
            kid = (kiosk.get("importKioskId") or "").strip().upper()
            install_date = (ck.get("installDate") or "")[:10]

            # Onfleet match
            of_match = []
            match_kind = ""
            if kid and kid in of_by_kid:
                of_match = of_by_kid[kid]; match_kind = "kid"
            elif sio and sio in of_by_sio:
                of_match = of_by_sio[sio]; match_kind = "sio"

            # Build worker assignment info from open tasks
            assignments = []
            for t in of_match:
                ts = t.get("timeLastModified") or t.get("timeCreated") or 0
                try:
                    assigned_at = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else ""
                except Exception:
                    assigned_at = ""
                cf = data._meta(t)
                assignments.append({
                    "wo_number": cf.get("woNumber") or cf.get("WO Number") or "",
                    "task_type": cf.get("taskType") or cf.get("Task Type") or "",
                    "short_id": t.get("shortId") or "",
                    "state": t.get("state"),
                    "worker_id": t.get("worker") or "",
                    "worker_name": workers.get(t.get("worker"), "") if t.get("worker") else "",
                    "assigned_at": assigned_at,
                    "complete_after": t.get("completeAfter") or 0,
                    "complete_before": t.get("completeBefore") or 0,
                    "notes": t.get("notes") or "",
                })

            if of_match:
                states = [t.get("state") for t in of_match]
                best = max([s for s in states if s is not None], default=None)
                onfleet_state = {0: "unassigned", 1: "assigned", 2: "active"}.get(best, str(best) if best is not None else "")
            else:
                onfleet_state = "NOT_IN_ONFLEET"

            rows.append({
                "kid": kid,
                "sio": sio,
                "campaign_name": camp_name,
                "campaign_id": cid,
                "current_status": status_name,
                "art_approved_date": art_iso[:10] if art_iso else "",
                "days_since_art_approved": days_old,
                "venue_name": venue.get("venueName", ""),
                "venue_city": venue.get("city", ""),
                "venue_state": venue.get("state", ""),
                "kiosk_type": ((kiosk.get("kioskType") or {}).get("typeName") or ""),
                "is_digital": bool(kiosk.get("isDigital")),
                "boosted": bool(ck.get("boosted")),
                "reservation_start": (ck.get("reservationStart") or "")[:10],
                "reservation_end": (ck.get("reservationEnd") or "")[:10],
                "install_date": install_date,
                "installed_image_url": ck.get("installedImageUrl") or "",
                "art_collection_name": pc.get("collectionName") or "",
                "art_top_url": pc.get("topFileUrl") or "",
                "art_bottom_url": pc.get("bottomFileUrl") or "",
                "onfleet_state": onfleet_state,
                "onfleet_match_kind": match_kind,
                "assignments": assignments,
                "timeline": timeline,
            })

    # Sort: NOT_IN_ONFLEET first, then oldest art-approved
    rows.sort(key=lambda r: (
        0 if r["onfleet_state"] == "NOT_IN_ONFLEET" else 1,
        -(r["days_since_art_approved"] or 0),
        r["sio"], r["kid"]
    ))
    return rows, photos_by_kid


# --- Sidebar / refresh -------------------------------------------------------
if st.sidebar.button("🔄 Refresh data now"):
    st.cache_data.clear()
    st.rerun()

# --- Load data ---------------------------------------------------------------
try:
    bundle = _load_bundle()
    rows, photos_by_kid = build_rows(bundle)
except Exception as e:
    st.error(f"Failed to load data: {e}")
    st.stop()

# --- KPIs ---------------------------------------------------------------------
total = len(rows)
not_in_of = sum(1 for r in rows if r["onfleet_state"] == "NOT_IN_ONFLEET")
unassigned = sum(1 for r in rows if r["onfleet_state"] == "unassigned")
assigned_n = sum(1 for r in rows if r["onfleet_state"] == "assigned")
installed_n = sum(1 for r in rows if r["install_date"])

last_refreshed = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# --- Render the entire UI as one HTML component for full styling control -----
INITIAL_DATA = {
    "rows": rows,
    "photos": photos_by_kid,
    "kpis": {
        "total": total,
        "not_in_of": not_in_of,
        "unassigned": unassigned,
        "assigned": assigned_n,
        "installed": installed_n,
        "open_tasks": bundle.get("open_task_count", 0),
        "completed_tasks": bundle.get("completed_task_count", 0),
    },
    "last_refreshed": last_refreshed,
}

try:
    import os as _os
    html_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "frontend.html")
    if not _os.path.exists(html_path):
        raise FileNotFoundError(f"frontend.html missing at {html_path}; dir contents: {_os.listdir(_os.path.dirname(html_path))}")
    with open(html_path, "r", encoding="utf-8") as f:
        html_template = f.read()
    html = html_template.replace("__APP_DATA__", json.dumps(INITIAL_DATA, default=str))
    st.markdown(f"### Campaign Tracker — {total} kiosks · {not_in_of} need attention · last refreshed {last_refreshed}")
    components.html(html, height=2400, scrolling=True)
except Exception as e:
    import traceback
    st.error(f"Render failed: {type(e).__name__}: {e}")
    st.code(traceback.format_exc())
