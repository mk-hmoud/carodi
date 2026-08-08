"""Telegram callback handler: triage from your phone.

Recording what you did about a match previously meant SSHing into a server and
pasting a hex fingerprint, which is a feature that exists rather than a feature
anyone uses. This turns it into a tap.

Design: the digest stays one message with a single Triage button. Tapping it
opens one card, and every action edits that same card in place. One notification
per day, no matter how many matches -- attaching buttons to twenty separate
messages is how you learn to mute a bot.

Long polling rather than a webhook: no inbound port, no TLS, no reverse proxy.
"""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass

import httpx

from carodi.models import Opportunity
from carodi.sinks.base import location_line, sponsor_badge
from carodi.store import Store

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

#: Callback payloads are capped at 64 bytes by Telegram. A 16-char fingerprint
#: plus a small index fits comfortably; actions carry the fingerprint rather
#: than a position so a stale button can never mark the wrong job.
CB_TRIAGE = "tri"
CB_APPLIED = "a"
CB_SKIPPED = "s"
CB_LATER = "l"
CB_CLOSE = "x"


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


@dataclass
class Card:
    text: str
    markup: dict


def render_card(opp: Opportunity, index: int, total: int) -> Card:
    """One opportunity, with the actions that resolve it."""
    lines = [
        f"<b>{index + 1} of {total}</b> · score {opp.score:g}",
        "",
        f'<a href="{esc(str(opp.url))}">{esc(opp.title)}</a>',
        f"<b>{esc(opp.org)}</b> · {esc(location_line(opp))}",
    ]
    if badge := sponsor_badge(opp):
        lines.append(esc(badge))
    if opp.deadline:
        lines.append(f"⏳ closes {opp.deadline.isoformat()}")
    if opp.reasons:
        lines.append(f"\n<i>{esc(', '.join(opp.reasons))}</i>")

    fp = opp.fingerprint
    markup = {
        "inline_keyboard": [
            [{"text": "🔗 Open posting", "url": str(opp.url)}],
            [
                {"text": "✅ Applied", "callback_data": f"{CB_APPLIED}|{fp}|{index}"},
                {"text": "🚫 Not for me", "callback_data": f"{CB_SKIPPED}|{fp}|{index}"},
            ],
            [
                {"text": "⏭ Later", "callback_data": f"{CB_LATER}|{fp}|{index}"},
                {"text": "✖ Close", "callback_data": CB_CLOSE},
            ],
        ]
    }
    return Card("\n".join(lines), markup)


def render_done(store: Store) -> Card:
    stats = store.accountability()
    remaining = store.count_undecided()
    if remaining:
        head = f"⏸ Paused — {remaining} still undecided."
        markup = {
            "inline_keyboard": [[{"text": f"🗂 Resume ({remaining})", "callback_data": CB_TRIAGE}]]
        }
    else:
        head = "🎉 Nothing left to triage."
        markup = {"inline_keyboard": []}
    return Card(
        f"{head}\n\n<i>Last {stats['days']}d: {stats['delivered']} delivered · "
        f"{stats['applied']} applied · {stats['skipped']} skipped</i>",
        markup,
    )


class Bot:
    def __init__(self, token: str, chat_id: str, db_path: str, poll_timeout: int = 50):
        if not token or not chat_id:
            raise ValueError(
                "carodi bot needs both a bot token and a chat id "
                "(set CARODI_TELEGRAM_TOKEN and CARODI_TELEGRAM_CHAT_ID)"
            )
        self.token = token
        self.chat_id = str(chat_id)
        self.db_path = db_path
        self.poll_timeout = poll_timeout
        self._offset: int | None = None

    # -- transport ------------------------------------------------------------

    def _call(self, method: str, payload: dict | None = None, http_timeout: float = 30.0) -> dict:
        """Call a Bot API method.

        The payload is an explicit dict rather than **kwargs: Telegram's
        getUpdates takes a field named `timeout`, which collides with any
        transport timeout sharing the keyword namespace.
        """
        with httpx.Client(timeout=http_timeout) as c:
            r = c.post(API.format(token=self.token, method=method), json=payload or {})
            if r.status_code >= 400:
                log.error("telegram %s failed: %s %s", method, r.status_code, r.text[:300])
            if r.headers.get("content-type", "").startswith("application/json"):
                return r.json()
            return {}

    def _edit(self, message_id: int, card: Card) -> None:
        self._call(
            "editMessageText",
            {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "text": card.text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": card.markup,
            },
        )

    # -- handling -------------------------------------------------------------

    def _show(self, message_id: int, store: Store, index: int) -> None:
        """Render the item at `index`, or the done card if we've run off the end."""
        pending = store.undecided()
        if not pending:
            self._edit(message_id, render_done(store))
            return
        index = max(0, min(index, len(pending) - 1))
        self._edit(message_id, render_card(pending[index], index, len(pending)))

    def handle_callback(self, query: dict, store: Store) -> None:
        data = query.get("data") or ""
        message = query.get("message") or {}
        message_id = message.get("message_id")
        parts = data.split("|")
        action = parts[0]
        notice = ""

        if action == CB_CLOSE:
            self._edit(message_id, render_done(store))

        elif action == CB_TRIAGE:
            self._show(message_id, store, 0)

        elif action in (CB_APPLIED, CB_SKIPPED, CB_LATER) and len(parts) == 3:
            fingerprint, index = parts[1], int(parts[2])

            if action == CB_LATER:
                # Leave the status alone and step past it.
                self._show(message_id, store, index + 1)
            else:
                status = "applied" if action == CB_APPLIED else "skipped"
                # Keyed by fingerprint, never by position: an old button from
                # yesterday's card must not resolve to whatever now sits at
                # that index.
                if store.set_status(fingerprint, status):
                    notice = "Marked applied ✅" if status == "applied" else "Skipped"
                else:
                    notice = "That one is already gone"
                # The list shrank by one, so the same index is the next item.
                self._show(message_id, store, index)
        else:
            log.warning("unrecognized callback payload: %r", data)

        self._call(
            "answerCallbackQuery", {"callback_query_id": query["id"], "text": notice}
        )

    # -- loop -----------------------------------------------------------------

    def poll_once(self, store: Store) -> int:
        payload = {
            "timeout": self.poll_timeout,
            "allowed_updates": ["callback_query"],
        }
        if self._offset is not None:
            payload["offset"] = self._offset

        # Read timeout must exceed the long-poll timeout or every poll aborts.
        result = self._call("getUpdates", payload, http_timeout=self.poll_timeout + 15)
        updates = result.get("result") or []

        for update in updates:
            self._offset = update["update_id"] + 1
            if query := update.get("callback_query"):
                try:
                    self.handle_callback(query, store)
                except Exception:  # noqa: BLE001 - one bad tap must not kill the bot
                    log.exception("callback failed: %r", query.get("data"))
        return len(updates)

    def run_forever(self) -> None:
        log.info("carodi bot polling for callbacks")
        while True:
            try:
                with Store(self.db_path) as store:
                    self.poll_once(store)
            except KeyboardInterrupt:
                raise
            except Exception:  # noqa: BLE001 - survive transient network failure
                log.exception("poll failed; retrying in 15s")
                time.sleep(15)
