"""Ownership bundle shared by a parent agent and read-only delegates."""

from dataclasses import dataclass


@dataclass
class TransactionContext:
    transaction_id: str
    workspace: object
    secret_boundary: object
    sandbox_lease: object
    owns_context: bool = True

    @property
    def execution_root(self):
        return self.workspace.execution_root

    def borrow(self):
        return TransactionContext(
            transaction_id=self.transaction_id,
            workspace=self.workspace,
            secret_boundary=self.secret_boundary,
            sandbox_lease=self.sandbox_lease.borrow(),
            owns_context=False,
        )
