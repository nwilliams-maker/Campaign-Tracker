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

POD_CONFIGS = {
    "Blue": {"AL","AR","FL","IL","IA","LA","MI","MN","MS","MO","NC","SC","WI"},
    "Green": {"CO","DC","GA","IN","KY","MD","NJ","OH","UT"},
    "Orange": {"AK","AZ","CA","HI","ID","NV","OR","WA"},
    "Purple": {"KS","MT","NE","NM","ND","OK","SD","TN","TX","WY"},
    "Red": {"CT","DE","ME","MA","NH","NY","PA","RI","VT","VA","WV"},
}
import re as _re_state
_STATE_RE = _re_state.compile(r",\s*([A-Z]{2})\s+\d{5}")
def state_from_address(*parts) -> str:
    blob = " ".join(p or "" for p in parts)
    m = _STATE_RE.search(blob.upper())
    return m.group(1) if m else ""


def pod_for_state(state: str) -> str:
    s = (state or "").strip().upper()
    for pod, states in POD_CONFIGS.items():
        if s in states:
            return pod
    return "Other"


st.set_page_config(
    page_title="Campaign Tracker",
    page_icon="📍",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Hide Streamlit's chrome
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
[data-testid="stHeader"], footer, #MainMenu, [data-testid="stToolbar"] { display: none !important; }
html, body, .stApp, [data-testid="stAppViewContainer"] {
  background: #f1f5f9 !important;
  color: #0f172a !important;
  font-family: 'Inter', -apple-system, sans-serif !important;
}
.block-container { padding-top: 12px !important; padding-bottom: 0 !important; max-width: 1700px !important; margin: 0 auto !important; }
section[data-testid="stSidebar"] { background: #ffffff !important; border-right: 1px solid #cbd5e1 !important; }
section[data-testid="stSidebar"] button { background: #633094 !important; color: #fff !important; border: none !important; box-shadow: 0 2px 8px rgba(99,48,148,0.3); }
section[data-testid="stSidebar"] button:hover { background: #4c2671 !important; }
/* Status block: light card, dark text */
[data-testid="stStatus"], [data-testid="stStatusContainer"], details, .stStatus {
  background: #ffffff !important;
  color: #0f172a !important;
  border: 1px solid #cbd5e1 !important;
  border-radius: 6px !important;
  padding: 10px 14px !important;
  font-size: 13px !important;
  box-shadow: 0 1px 2px rgba(0,0,0,0.04) !important;
}
[data-testid="stStatus"] *, [data-testid="stStatusContainer"] *, details *, .stStatus * { color: #0f172a !important; }
[data-testid="stStatus"] summary, details summary { color: #633094 !important; font-weight: 600 !important; }
[data-testid="stMarkdownContainer"] *, .stMarkdown * { color: #0f172a !important; }
[data-testid="stMarkdownContainer"] h3 { font-size: 16px !important; font-weight: 700 !important; margin: 6px 0 8px !important; color: #0f172a !important; }
.stAlert, [data-testid="stAlertContainer"] { color: #0f172a !important; }
h1, h2, h3, h4, h5, h6 { color: #0f172a !important; }
</style>
""", unsafe_allow_html=True)


# --- Data load ---------------------------------------------------------------
# NOTE: deliberately NOT @st.cache_data — Streamlit suppresses UI side effects
# (st.status, st.write) inside cached functions, which made the page look frozen.
# The individual fetchers in data.py are each @st.cache_data, so reruns of this
# wrapper are still fast on cache hit (just function returns, no API calls).
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
        open_tasks = data.fetch_open_tasks()
        workers = data.fetch_workers()
        of_by_kid = data.index_open_tasks_by_kid(open_tasks)
        of_by_sio = data.index_open_tasks_by_sio(open_tasks)
        st.write(f"✓ {len(open_tasks)} open Onfleet tasks · {len(workers)} workers in directory")

        # Step 7 (photo history) is OPT-IN — too slow for first load.
        # Toggle the sidebar checkbox to enable it for the next refresh.
        if st.session_state.get("load_photos", False):
            kid_universe = set()
            for cks in kiosks_by_cid.values():
                for ck in cks or []:
                    k = ((ck.get("kiosk") or {}).get("importKioskId") or "").strip().upper()
                    if k:
                        kid_universe.add(k)
            status.update(label=f"[7/7] Pulling completed Onfleet tasks for photo history ({len(kid_universe)} kiosks) — slow, ~2 min...")
            completed = data.fetch_completed_tasks_for_kids(tuple(sorted(kid_universe)), max_pages=250, _progress=st.write)
            photos_by_kid = data.index_photos_by_kid(completed)
            photo_count = sum(sum(len(e["ids"]) for e in lst) for lst in photos_by_kid.values())
            st.write(f"✓ {len(completed)} completed tasks · {photo_count} photos across {len(photos_by_kid)} kiosks")
        else:
            completed = []
            photos_by_kid = {}
            st.write("⊘ Photo history skipped (toggle 'Load photos' in sidebar to enable — adds ~2 min)")

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
    skipped_tests = 0
    for c in bundle["campaigns"]:
        cid = c["id"]
        # Skip obvious test/internal campaigns: orderNumber 0 or name containing "test"/"ignore"
        nm = (c.get("name") or "").lower()
        if c.get("orderNumber") in (0, None, "") or "test" in nm or "ignore" in nm or "ignroe" in nm:
            skipped_tests += 1
            continue
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

            # Build a one-line full address + derive state from address if venue.state is blank
            v_addr1 = (venue.get("address1") or "").strip()
            v_addr2 = (venue.get("address2") or "").strip()
            v_city = (venue.get("city") or "").strip()
            v_state = (venue.get("state") or "").strip().upper()
            v_zip = (venue.get("zip") or "").strip()
            if not v_state:
                v_state = state_from_address(v_addr1, v_addr2, f"{v_city}, {v_zip}")
            addr_parts = [p for p in [v_addr1, v_addr2] if p]
            csz = ", ".join(p for p in [v_city, (v_state + (" " + v_zip if v_zip else "")).strip()] if p.strip(", "))
            if csz:
                addr_parts.append(csz)
            full_address = ", ".join(addr_parts)

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
                container = t.get("container") or {}
                assignments.append({
                    "wo_number": cf.get("WO_NAME") or cf.get("woNumber") or cf.get("WO Number") or "",
                    "due_date": cf.get("DUE_DATE") or "",
                    "pay_per_task": cf.get("PAY_PER_TASK") or "",
                    "task_type": cf.get("taskType") or cf.get("Task Type") or "",
                    "short_id": t.get("shortId") or "",
                    "task_id": t.get("id") or "",
                    "state": t.get("state"),
                    "worker_id": t.get("worker") or "",
                    "worker_name": workers.get(t.get("worker"), "") if t.get("worker") else "",
                    "assigned_at": assigned_at,
                    "complete_after": t.get("completeAfter") or 0,
                    "complete_before": t.get("completeBefore") or 0,
                    "notes": t.get("notes") or "",
                    "container_type": container.get("type") or "",
                    "container_worker": container.get("worker") or "",
                    "container_team": container.get("team") or "",
                    "tracking_url": t.get("trackingURL") or "",
                })

            if of_match:
                states = [t.get("state") for t in of_match]
                best = max([s for s in states if s is not None], default=None)
                onfleet_state = {0: "unassigned", 1: "assigned", 2: "active"}.get(best, str(best) if best is not None else "")
            else:
                onfleet_state = "NOT_IN_ONFLEET"

            # Pull due_date from the first assignment that has one
            row_due_date = ""
            for a in assignments:
                if a.get("due_date"):
                    row_due_date = a["due_date"][:10] if isinstance(a["due_date"], str) else str(a["due_date"])
                    break
            rows.append({
                "pod": pod_for_state(v_state),
                "venue_id": venue.get("id") or "",
                "venue_address": full_address,
                "due_date": row_due_date,
                "kid": kid,
                "sio": sio,
                "campaign_name": camp_name,
                "campaign_id": cid,
                "current_status": status_name,
                "art_approved_date": art_iso[:10] if art_iso else "",
                "days_since_art_approved": days_old,
                "venue_name": venue.get("venueName", ""),
                "venue_city": venue.get("city", ""),
                "venue_state": v_state,
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
# Sidebar just has the master refresh
if st.sidebar.button("🔄 Refresh ALL data (clears cache)"):
    st.cache_data.clear()
    st.session_state["load_photos"] = False
    st.rerun()

# --- Load data ---------------------------------------------------------------
# --- Photo loader UI: button shown FIRST so user can trigger the load.
# Status banner is rendered AFTER the load completes so it reflects reality.
photo_btn_col1, photo_btn_col2 = st.columns([3, 1])
with photo_btn_col2:
    if not st.session_state.get("load_photos", False):
        if st.button("📷 Load photos now (~2 min)", use_container_width=True, type="primary", key="load_photos_btn"):
            st.session_state["load_photos"] = True
            st.rerun()
    else:
        if st.button("🚫 Drop loaded photos", use_container_width=True, key="drop_photos_btn"):
            st.session_state["load_photos"] = False
            try: data.fetch_completed_tasks_for_kids.clear()
            except Exception: pass
            st.rerun()

try:
    bundle = _load_bundle()
    rows, photos_by_kid = build_rows(bundle)
except Exception as e:
    st.error(f"Failed to load data: {e}")
    st.stop()

# Now we know the actual photo state — render an honest banner.
photo_n = sum(sum(len(e["ids"]) for e in lst) for lst in photos_by_kid.values()) if photos_by_kid else 0
kiosk_n = len(photos_by_kid) if photos_by_kid else 0
with photo_btn_col1:
    if photo_n > 0:
        st.success(f"📷 Photo history loaded — {photo_n} photos across {kiosk_n} kiosks. Expand any row to see them.")
    elif st.session_state.get("load_photos", False):
        st.warning("📷 Photo load ran but found no matching photos. (None of our active-pipeline kiosks have completed Onfleet tasks with photos since Sept 2025.)")
    else:
        st.info("📷 Photo history NOT loaded yet. Click → to pull every kiosk's install-photo history (~2 min, runs only on first click).")

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
    # Escape HTML-significant characters in the JSON so a stray </script>
    # substring in the data can never break out of the <script> tag.
    safe_json = (json.dumps(INITIAL_DATA, default=str)
                  .replace("<", "\\u003c")
                  .replace(">", "\\u003e")
                  .replace("&", "\\u0026")
                  .replace("\u2028", "\\u2028")
                  .replace("\u2029", "\\u2029"))
    html = html_template.replace("__APP_DATA__", safe_json)
    st.markdown(f"### Campaign Tracker — {total} kiosks · {not_in_of} need attention · last refreshed {last_refreshed}")
    components.html(html, height=2400, scrolling=True)
except Exception as e:
    import traceback
    st.error(f"Render failed: {type(e).__name__}: {e}")
    st.code(traceback.format_exc())
