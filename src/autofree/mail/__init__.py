"""Mail provider factory."""

from __future__ import annotations

from autofree.mail.base import MailProvider
from autofree.settings import get_mail_provider


def get_mail_client(provider: str | None = None) -> MailProvider:
    """Instantiate a mail backend client.

    `provider` overrides the saved settings; otherwise reads `mail.provider`
    from data/settings.json.
    """
    name = (provider or get_mail_provider() or "tempmail").strip().lower()
    if name in ("cf_temp_email", "cloudflare_temp_email"):
        from autofree.mail.cf_temp_email import CfTempEmailClient

        return CfTempEmailClient()
    if name == "maillab":
        from autofree.mail.maillab import MaillabClient

        return MaillabClient()
    if name == "tempmail":
        from autofree.mail.tempmail import TempmailClient

        return TempmailClient()
    raise ValueError(f"未知 MAIL_PROVIDER={name!r}(可选: cf_temp_email | maillab | tempmail)")


__all__ = ["MailProvider", "get_mail_client"]
