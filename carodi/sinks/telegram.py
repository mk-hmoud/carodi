"""Telegram digest.

One message per run (chunked to fit the 4096-character limit), not one message
per match -- twenty separate notifications is how you learn to mute a bot.
"""

from __future__ import annotations

import html
import logging

import httpx

from carodi.models import Opportunity
from carodi.sinks.base import (
    Sink,
    accountability_line,
    group_by_kind,
    location_line,
    source_label,
    sponsor_badge,
)

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"
LIMIT = 3900  # under Telegram's 4096, leaving room for the chunk header

KIND_TITLES = {
    "scholarship": "🎓 Scholarships",
    "fellowship": "🎓 Fellowships",
    "internship": "🧪 Internships",
    "job": "💼 Jobs",
}


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


class TelegramSink(Sink):
    name = "telegram"

    def __init__(self, token: str, chat_id: str, disable_preview: bool = True):
        if not token or not chat_id:
            raise ValueError(
                "Telegram sink needs both a bot token and a chat id "
                "(set CARODI_TELEGRAM_TOKEN and CARODI_TELEGRAM_CHAT_ID)"
            )
        self.token = token
        self.chat_id = chat_id
        self.disable_preview = disable_preview

    def render(self, items: list[Opportunity], accountability: dict, errors: dict) -> str:
        if not items:
            lines = ["<b>carodi</b> — no new matches today."]
        else:
            lines = [f"<b>carodi</b> — {len(items)} new match{'es' if len(items) != 1 else ''}"]
            for kind, group in group_by_kind(items).items():
                lines.append(f"\n<b>{KIND_TITLES.get(kind, kind.title())}</b>")
                for opp in group:
                    head = f'• <a href="{esc(str(opp.url))}">{esc(opp.title)}</a>'
                    lines.append(head)
                    meta = [esc(opp.org), esc(location_line(opp))]
                    if badge := sponsor_badge(opp):
                        meta.append(esc(badge))
                    if opp.deadline:
                        meta.append(f"⏳ {opp.deadline.isoformat()}")
                    lines.append(f"  <i>{' · '.join(m for m in meta if m)}</i>")
                    lines.append(
                        f"  <code>{opp.fingerprint}</code> · score {opp.score:g}"
                        f" · via {esc(source_label(opp))}"
                    )

        lines.append(f"\n<i>{esc(accountability_line(accountability))}</i>")
        if errors:
            failed = ", ".join(sorted(errors))
            lines.append(f"<i>⚠️ sources failed: {esc(failed)}</i>")
        return "\n".join(lines)

    def _chunks(self, text: str) -> list[str]:
        chunks, current = [], ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > LIMIT:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)
        return chunks

    def deliver(self, items: list[Opportunity], accountability: dict, errors: dict) -> None:
        text = self.render(items, accountability, errors)
        chunks = self._chunks(text)

        with httpx.Client(timeout=30.0) as client:
            for i, chunk in enumerate(chunks):
                payload = {
                    "chat_id": self.chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": self.disable_preview,
                }
                # Only the final chunk carries the button, so a digest split
                # across three messages does not offer three Triage buttons.
                if items and i == len(chunks) - 1:
                    pending = accountability.get("undecided", 0) + len(items)
                    payload["reply_markup"] = {
                        "inline_keyboard": [
                            [{"text": f"🗂 Triage ({pending})", "callback_data": "tri"}]
                        ]
                    }

                r = client.post(API.format(token=self.token), json=payload)
                if r.status_code >= 400:
                    log.error("telegram rejected message: %s %s", r.status_code, r.text[:300])
                    r.raise_for_status()
