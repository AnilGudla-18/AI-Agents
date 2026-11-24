"""No real email provider is needed: emails are recorded in `outbox` instead of being sent."""

outbox: list[dict] = []


def send_email(to: str, link: str) -> None:
    outbox.append({"to": to, "link": link})
