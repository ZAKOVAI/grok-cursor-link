"""Watcher card pings and stall detection. Read-only: nothing here clicks.

approval_needed stays the run-card (and unknown-card) event, with card_id, kind,
options, risk, and suggest_always. Question and plan cards get their own events.
Edit rows are identified and never pinged. A busy composer with no transcript
growth emits chat_stalled once per quiet stretch.
"""
import gc_risk
import gc_send


def pick_card(cards):
    """First card that is not an edit row. None when every card is an edit."""
    for card in cards or []:
        if card.get("kind") != "edit":
            return card
    return None


def tracked_command(cards):
    """Dedup key. None = edits only (do not ping). '' = badge with no card text."""
    if not cards:
        return ""
    card = pick_card(cards)
    if card is None:
        return None
    return gc_send.norm(card.get("command"))


def event_name(card):
    kind = (card or {}).get("kind") or ""
    if kind == "question":
        return "question_needed"
    if kind == "plan":
        return "plan_needed"
    if kind == "edit":
        return ""
    return "approval_needed"


def is_busy(status):
    """Composer status that is still working. Idle and error statuses are not busy."""
    text = str(status or "").lower()
    if not text or text in gc_send._IDLE_OK or text in gc_send._BAD_STATUS:
        return False
    return True


def stall_seconds(cfg):
    """config stall_minutes, default 10. Negative or junk falls back to 10."""
    try:
        minutes = float((cfg or {}).get("stall_minutes", 10))
    except (TypeError, ValueError):
        minutes = 10.0
    if minutes < 0:
        minutes = 10.0
    return minutes * 60


def approval_payload(cid, info, name, workspace, now, redact):
    """Ping dict, or None when the only cards are edits."""
    cards = info.get("cards") or []
    card = pick_card(cards)
    if cards and card is None:
        return None
    cmd = gc_send.norm(card.get("command")) if card else ""
    if card:
        note = card.get("note") or ""
        if len(cards) > 1:
            note = ("%d cards; approve refuses until one is left. " % len(cards) + note).strip()
    else:
        note = "card is in a background tab; read it with `grok-cursor pending --to %s`" % cid
    risk, _why = gc_risk.tag(cmd)
    return {
        "event": event_name(card),
        "minor": False,
        "chat_id": cid,
        "chat_name": name or info.get("title") or "",
        "workspace": workspace or info.get("workspace") or "",
        "snippet": redact(cmd)[:500],
        "card": card,
        "buttons": (card or {}).get("buttons") or [],
        "note": note,
        "expect": cmd[:60],
        "card_id": (card or {}).get("card_id") or "",
        "kind": (card or {}).get("kind") or "",
        "options": list((card or {}).get("options") or []),
        "risk": risk,
        "suggest_always": risk == "low",
        "timestamp": now,
        "cdp_down": False,
    }


def record_tab(found, busy, cid, title, workspace, cards, status, waiting, selected):
    """Fill the approval map and the busy map for one chat tab."""
    if is_busy(status):
        busy[cid] = {"title": title, "workspace": workspace, "status": status}
    if cards or waiting:
        found[cid] = {"title": title, "workspace": workspace, "cards": cards,
                      "in_view": bool(selected), "status": status or ""}


def consider_stalls(stalls, busy, sizes, now, window):
    """Update stalls in place. Returns chat ids that just crossed the quiet window.

    A size change starts the clock over. A chat that leaves busy is forgotten.
    Each quiet stretch emits once.
    """
    emit = []
    for cid in busy:
        size = int(sizes.get(cid, 0))
        prev = stalls.get(cid)
        if not prev or prev.get("size") != size:
            stalls[cid] = {"since": now, "size": size, "pinged": False}
            continue
        if prev.get("pinged"):
            continue
        if now - float(prev.get("since") or now) >= window:
            prev["pinged"] = True
            emit.append(cid)
    for cid in [c for c in stalls if c not in busy]:
        del stalls[cid]
    return emit


def stall_payload(cid, info, name, workspace, now):
    return {
        "event": "chat_stalled",
        "minor": False,
        "chat_id": cid,
        "chat_name": name or info.get("title") or "",
        "workspace": workspace or info.get("workspace") or "",
        "snippet": "",
        "status": info.get("status") or "",
        "timestamp": now,
        "cdp_down": False,
    }
