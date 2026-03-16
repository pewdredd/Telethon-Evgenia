"""Incoming message listener for tracking lead replies.

Parameterized functions — no module-level state. Each account gets its own
event handler registered on its TelegramClient.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from telethon import TelegramClient, events

logger = logging.getLogger(__name__)

_http_client: httpx.AsyncClient | None = None


def register_listener(
    account_id: str,
    client: TelegramClient,
    manager,  # AccountManager — avoid circular import
    webhook_url: str,
) -> Callable[[], None]:
    """Register a NewMessage handler on the client. Returns a cleanup callable."""

    async def handler(event: events.NewMessage.Event) -> None:
        await _on_new_message(event, account_id, manager, webhook_url)

    client.add_event_handler(
        handler,
        events.NewMessage(incoming=True, outgoing=False, func=lambda e: e.is_private),
    )
    logger.info("Listener registered for account %s", account_id)

    def cleanup() -> None:
        client.remove_event_handler(handler)
        logger.info("Listener removed for account %s", account_id)

    return cleanup


async def _on_new_message(event: events.NewMessage.Event, account_id: str, manager, webhook_url: str) -> None:
    sender = await event.get_sender()
    if sender is None:
        return

    text = event.message.text
    if not text:
        return

    sender_id = sender.id
    sender_username = getattr(sender, "username", None)

    if not await manager.is_known_recipient(account_id, sender_id, sender_username):
        return

    row_id = await manager.log_incoming(
        account_id=account_id,
        sender_id=str(sender_id),
        sender_username=sender_username,
        message_text=text,
        telegram_message_id=event.message.id,
        chat_id=event.chat_id,
    )

    if webhook_url:
        await _forward_to_webhook(
            {
                "id": row_id,
                "account_id": account_id,
                "sender_id": str(sender_id),
                "sender_username": sender_username,
                "message_text": text,
                "telegram_message_id": event.message.id,
                "chat_id": event.chat_id,
                "received_at": datetime.now(UTC).isoformat(),
                "processed": False,
            },
            webhook_url,
        )


async def _forward_to_webhook(msg: dict, webhook_url: str) -> None:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=10)
    try:
        await _http_client.post(webhook_url, json=msg)
    except Exception:
        logger.exception("Error forwarding to webhook %s", webhook_url)
