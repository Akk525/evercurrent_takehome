"""Custom exceptions for the Slack delivery module."""


class SlackDeliveryError(Exception):
    """Raised when delivery to Slack fails."""


class SlackConfigError(SlackDeliveryError):
    """Raised when Slack configuration is missing or invalid."""
