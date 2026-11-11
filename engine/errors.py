"""Fail-fast error types shared across the engine."""


class MissingInputError(RuntimeError):
    """A required business input from plan §25 (I-x) is missing or still a placeholder.

    §0 rule 1: never invent the value — stop and ask Govinda.
    """

    def __init__(self, input_id: str, detail: str) -> None:
        self.input_id = input_id
        super().__init__(f"[{input_id}] {detail} (see docs/BUILD_PLAN.md §25)")


class ConfigError(RuntimeError):
    """Configuration is present but invalid (e.g. seed cap above a HARD_* ceiling)."""


class CredentialsUnavailableError(RuntimeError):
    """The requested credential is not configured (e.g. write token before its phase gate)."""
