#!/usr/bin/env python3
"""Export Claude Code session transcripts for this repo into docs/chats/ as Markdown.

Claude Code stores transcripts as JSONL under
``~/.claude/projects/<path-slug>/<session-uuid>.jsonl`` and prunes them on a
retention timer (30 days by default), so anything worth keeping has to be
copied out of there. This script renders each session to a readable Markdown
file named ``YYYYMMDD-HHMMSS-<slug>.md`` (local time of the first message) and
copies the raw JSONL alongside it under ``docs/chats/raw/``.

Sessions are copied, never moved: the originals stay in ``~/.claude`` so
``claude --resume`` keeps working.

Usage:
    python scripts/export_chats.py [--slug <extra-project-slug>] [--out docs/chats]

Progress and results are written to tmp/export_chats.log as well as stdout.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROJECTS = Path.home() / ".claude" / "projects"

# Claude Code's project slug = the repo's absolute path with '/' -> '-'. Derived
# from where this checkout actually lives, so nothing machine-specific is
# hardcoded. If the repo has lived at other paths (each gets its own transcript
# directory), pass those old slugs explicitly via --slug.
DEFAULT_SLUGS = [str(REPO).replace("/", "-")]

# A tool result longer than this is truncated in the Markdown; the raw JSONL
# under docs/chats/raw/ always keeps the full text.
MAX_RESULT_CHARS = 2000

log = logging.getLogger("export_chats")


def setup_logging() -> None:
    tmp = REPO / "tmp"
    tmp.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(tmp / "export_chats.log", mode="w"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def slugify(text: str, limit: int = 48) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (text[:limit].rstrip("-") or "session")


def local_ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()


def block_text(block: dict) -> str:
    kind = block.get("type")
    if kind == "text":
        return block.get("text", "")
    if kind == "thinking":
        return ""  # thinking blocks are not part of the readable record
    if kind == "tool_use":
        args = json.dumps(block.get("input", {}), indent=2, default=str)
        if len(args) > MAX_RESULT_CHARS:
            args = args[:MAX_RESULT_CHARS] + "\n… (truncated)"
        return f"**→ {block.get('name')}**\n\n```json\n{args}\n```"
    if kind == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
        content = str(content or "")
        if len(content) > MAX_RESULT_CHARS:
            content = content[:MAX_RESULT_CHARS] + "\n… (truncated)"
        return f"<details><summary>tool result</summary>\n\n```\n{content}\n```\n\n</details>"
    return ""


def message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [block_text(b) for b in content if isinstance(b, dict)]
        return "\n\n".join(p for p in parts if p.strip())
    return ""


def render(path: Path) -> tuple[str, dict] | None:
    """Render one JSONL transcript to Markdown. Returns (markdown, meta)."""
    records = []
    for line_no, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("%s:%d is not valid JSON — skipped", path.name, line_no)

    meta = {"session": path.stem, "title": "", "first": None, "last": None}
    body: list[str] = []
    turns = 0

    for rec in records:
        if rec.get("type") == "ai-title":
            meta["title"] = rec.get("aiTitle", "")
            continue
        message = rec.get("message")
        if not message or rec.get("isSidechain"):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = message_text(message)
        if not text.strip():
            continue

        ts = rec.get("timestamp")
        if ts:
            meta["first"] = meta["first"] or ts
            meta["last"] = ts
        meta.setdefault("cwd", rec.get("cwd"))
        meta.setdefault("branch", rec.get("gitBranch"))
        meta.setdefault("version", rec.get("version"))

        stamp = local_ts(ts).strftime("%H:%M:%S") if ts else ""
        heading = "## 🧑 Dan" if role == "user" else "## 🤖 Claude"
        body.append(f"{heading}{f'  ·  {stamp}' if stamp else ''}\n\n{text}")
        turns += 1

    if not turns:
        log.info("%s has no conversational content — skipped", path.name)
        return None

    meta["turns"] = turns
    first = local_ts(meta["first"]) if meta["first"] else None
    header = [
        f"# {meta['title'] or 'Claude Code session'}",
        "",
        f"- **Session:** `{meta['session']}`",
        f"- **Started:** {first.isoformat(timespec='seconds') if first else 'unknown'}",
        f"- **Working directory:** `{meta.get('cwd') or 'unknown'}`",
        f"- **Branch:** `{meta.get('branch') or 'unknown'}`",
        f"- **Claude Code:** {meta.get('version') or 'unknown'}",
        f"- **Messages:** {turns}",
        "",
        "---",
        "",
        "",
    ]
    return "\n".join(header) + "\n\n".join(body) + "\n", meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", action="append", dest="slugs", help="project slug (repeatable)")
    ap.add_argument("--out", default="docs/chats", help="output directory, repo-relative")
    args = ap.parse_args()

    setup_logging()
    slugs = args.slugs or DEFAULT_SLUGS
    out = REPO / args.out
    raw = out / "raw"
    out.mkdir(parents=True, exist_ok=True)
    raw.mkdir(exist_ok=True)

    exported = 0
    for slug in slugs:
        src = PROJECTS / slug
        if not src.is_dir():
            log.info("%s: no such transcript directory", slug)
            continue
        sessions = sorted(src.glob("*.jsonl"))
        log.info("%s: %d transcript(s)", slug, len(sessions))
        for session in sessions:
            result = render(session)
            if result is None:
                continue
            markdown, meta = result
            stamp = local_ts(meta["first"]).strftime("%Y%m%d-%H%M%S")
            name = f"{stamp}-{slugify(meta['title'] or meta['session'])}"
            (out / f"{name}.md").write_text(markdown)
            shutil.copy2(session, raw / f"{name}.jsonl")
            log.info("exported %s (%d messages)", f"{name}.md", meta["turns"])
            exported += 1

    log.info("done — %d session(s) written to %s", exported, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
