from __future__ import annotations

import sqlite3

from . import Candidate, Scanner


class CupHandleScanner(Scanner):
    strategy_id = "cup_handle"
    version = "v1.0"
    kind = "stock"
    description = "Reserved placeholder for a future cup-handle scanner."
    default_params: dict[str, object] = {}

    def run(self, conn: sqlite3.Connection, trade_date: str) -> list[Candidate]:
        self.last_universe_size = 0
        return []
