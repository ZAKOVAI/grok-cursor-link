cd ~/gc-test/grok-cursor && python3 - <<'EOF'
p='lib/gc_send.py'; s=open(p).read()
old='''def _card_line(chat, card):'''
new='''ACTIONABLE = {b for labels in CARD_ACTIONS.values() for b in labels}

def actionable(cards):
    """Cards `approve` can act on: a live Run/Allow/Skip/Accept button. Finished edit rows and
    plan cards have none and must not count ("14 approval cards; refusing to guess")."""
    return [c for c in cards or [] if isinstance(c, dict) and ACTIONABLE & set(c.get("buttons") or [])]

def _card_line(chat, card):'''
assert s.count(old)==1; s=s.replace(old,new)
old='''            cards = _read_cards(ws, chat["id"], 3 if prev else 0)
            if decision is not None and len(cards) != 1:'''
new='''            cards = actionable(_read_cards(ws, chat["id"], 3 if prev else 0))
            if decision is not None and len(cards) != 1:'''
assert s.count(old)==1; s=s.replace(old,new)
open(p,'w').write(s); print("ok")
EOF
cat >> tests/test_cards.py.new <<'EOF'
EOF
rm tests/test_cards.py.new; python3 - <<'EOF'
p='tests/test_cards.py'; s=open(p).read()
old='''class PendingIdentity(test_send.SendBase):'''
new='''class Actionable(unittest.TestCase):
    """approve counts only cards with a live Run/Skip/Accept button, not finished edit rows."""
    def test_finished_edits_and_plans_do_not_count(self):
        run = {"kind": "run", "buttons": ["Run", "Skip", "Always run"], "command": "npm test"}
        cards = [{"kind": "edit", "buttons": [], "command": "edited a.py"} for _ in range(13)] + [run]
        self.assertEqual(gc_send.actionable(cards), [run])
        self.assertEqual(gc_send.actionable([{"kind": "plan", "buttons": ["View Plan", "Build"]}]), [])
        self.assertEqual(gc_send.actionable([{"kind": "question", "buttons": ["Skip", "Continue"]}])[0]["kind"], "question")
        self.assertEqual(gc_send.actionable([{"kind": "edit", "buttons": ["Accept"]}])[0]["kind"], "edit")
        self.assertEqual(gc_send.actionable([]), [])
        self.assertEqual(gc_send.actionable([None, "x", {"kind": "run"}]), [])


class PendingIdentity(test_send.SendBase):'''
assert s.count(old)==1; open(p,'w').write(s.replace(old,new)); print("ok")
EOF
python3 -m unittest tests.test_cards 2>&1 | tail -3