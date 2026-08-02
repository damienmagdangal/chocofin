"""Domain error types shared across core.

Ledger operations raise; the parser never does — see `core.parser`.
"""


class ChocoFinError(Exception):
    """Base for every domain error."""


class LedgerError(ChocoFinError):
    """A ledger operation was rejected before touching the database."""


class EntryNotFoundError(LedgerError):
    """No live entry with that id in this household."""


class EntryAlreadyVoidedError(LedgerError):
    """The entry is already voided; append-only history forbids voiding twice."""


class AccountNotFoundError(LedgerError):
    """No account with that id in this household."""


class InvalidAmountError(LedgerError):
    """Amount is not a positive integer number of centavos."""


class SameAccountTransferError(LedgerError):
    """A transfer's source and destination are the same account."""


class PeriodError(ChocoFinError):
    """A period could not be resolved."""
