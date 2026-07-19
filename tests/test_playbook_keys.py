from __future__ import annotations

import unittest


# Reserved knowledge-card hooks. They are intentionally not registered scanner ids yet.
PLAYBOOK_KEY_ALLOWLIST = {
    "反包警示": "reserved setup taxonomy for low-odds reversal warnings",
    "relay_new_king": "reserved setup hook for future relay/new-king scanner work",
}


class PlaybookKeyTests(unittest.TestCase):
    def test_real_knowledge_cards_load_without_errors_and_include_subdirectories(self) -> None:
        from mining.playbook import load_playbooks

        playbooks = load_playbooks("knowledge/cards")
        card_ids = {card["id"] for card in playbooks["cards"]}

        self.assertEqual(playbooks["errors"], 0)
        self.assertIn("IND-adv-count-emotion-bottom", card_ids)

    def test_playbook_index_keys_have_consumers(self) -> None:
        from mining.playbook import load_playbooks
        from mining.scanners import get_registered_scanners
        from mining.watchlist import WATCHLIST_STATES

        scanner_ids = {scanner_cls().strategy_id for scanner_cls in get_registered_scanners()}
        allowed = set(WATCHLIST_STATES) | scanner_ids | set(PLAYBOOK_KEY_ALLOWLIST)
        playbooks = load_playbooks("knowledge/cards")
        unknown = sorted(set(playbooks["index"]) - allowed)

        self.assertEqual(unknown, [])


if __name__ == "__main__":
    unittest.main()
