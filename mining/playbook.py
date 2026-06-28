from __future__ import annotations

from pathlib import Path
from typing import Any

SECTION_NAMES = ("模式定义", "看法", "案例", "原文")


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    raw_meta = parts[1]
    body = parts[2].lstrip("\r\n")
    meta: dict[str, Any] = {}
    lines = raw_meta.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            i += 1
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value.startswith("[") and raw_value.endswith("]"):
            value = [item.strip().strip("'\"") for item in raw_value[1:-1].split(",") if item.strip()]
        elif raw_value == "":
            values: list[str] = []
            j = i + 1
            while j < len(lines) and (lines[j].startswith(" ") or lines[j].startswith("\t")):
                child = lines[j].strip()
                if child.startswith("-"):
                    values.append(child[1:].strip().strip("'\""))
                j += 1
            value = values
            if values:
                i = j - 1
        else:
            value = raw_value.strip("'\"")
        meta[key] = value
        i += 1
    return meta, body


def _parse_sections(body: str) -> dict[str, str]:
    sections = {name: "" for name in SECTION_NAMES}
    current: str | None = None
    buffers = {name: [] for name in SECTION_NAMES}
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            heading = stripped[3:].strip()
            current = heading if heading in sections else None
            continue
        if current is not None:
            buffers[current].append(line.rstrip())
    for name, lines in buffers.items():
        sections[name] = "\n".join(lines).strip()
    return sections


def _split_doc_ids(value: Any, section_text: str) -> list[str]:
    docs: list[str] = []
    if isinstance(value, list):
        docs.extend(str(item).strip() for item in value if str(item).strip())
    elif isinstance(value, str) and value.strip():
        docs.extend(item.strip() for item in value.strip("[]").split(",") if item.strip())
    for line in section_text.splitlines():
        item = line.strip().lstrip("-").strip()
        if item and item not in docs:
            docs.append(item)
    return docs


def _raw_paths(cards_dir: Path, doc_ids: list[str]) -> dict[str, str]:
    raw_dir = cards_dir.parent / "_raw"
    paths: dict[str, str] = {}
    if not raw_dir.exists():
        return paths
    for doc_id in doc_ids:
        matches = sorted(raw_dir.glob(f"{doc_id}_*.txt"))
        if matches:
            try:
                paths[doc_id] = str(matches[0].relative_to(Path.cwd()))
            except ValueError:
                paths[doc_id] = str(matches[0])
    return paths


def _card_paths(cards_dir: Path) -> list[Path]:
    setup_dir = cards_dir / "setups"
    if setup_dir.exists():
        return sorted(setup_dir.glob("*.md"))
    return sorted(cards_dir.glob("**/*.md"))


def load_playbooks(cards_dir: str | Path = "knowledge/cards") -> dict[str, Any]:
    """Load setup cards and build a state/scanner index.

    Cards are the single source of truth. Bad cards are skipped and counted in
    ``errors`` so the panel can keep rendering even when one note is malformed.
    """
    root = Path(cards_dir)
    cards: list[dict[str, Any]] = []
    index: dict[str, list[dict[str, Any]]] = {}
    errors = 0

    for path in _card_paths(root):
        try:
            text = path.read_text(encoding="utf-8-sig")
            meta, body = _parse_frontmatter(text)
            maps_to = meta.get("maps_to", [])
            if isinstance(maps_to, str):
                maps_to = [item.strip() for item in maps_to.strip("[]").split(",") if item.strip()]
            if not isinstance(maps_to, list):
                maps_to = []
            sections = _parse_sections(body)
            card_id = str(meta.get("id") or path.stem).strip()
            if not card_id:
                errors += 1
                continue
            doc_ids = _split_doc_ids(meta.get("source_docs", []), sections["原文"])
            card = {
                "id": card_id,
                "title": str(meta.get("title") or card_id).strip(),
                "path": str(path),
                "maps_to": [str(item).strip() for item in maps_to if str(item).strip()],
                "source_docs": doc_ids,
                "raw_paths": _raw_paths(root, doc_ids),
            }
            card.update(sections)
            cards.append(card)
            for key in card["maps_to"]:
                index.setdefault(key, []).append(card)
        except Exception:
            errors += 1
            continue

    return {"cards": cards, "index": index, "errors": errors}


def lookup_playbook(state_or_scanner: str, cards_dir: str | Path = "knowledge/cards") -> list[dict[str, Any]]:
    key = str(state_or_scanner or "").strip()
    if not key:
        return []
    playbooks = load_playbooks(cards_dir)
    return list(playbooks["index"].get(key, []))