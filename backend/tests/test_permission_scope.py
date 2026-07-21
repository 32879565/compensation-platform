"""Permission-aware organization scope regression tests without a database."""

from app.auth.service import Principal, resolve_permission_org_scope
from app.routers import employee as employee_router


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class _ScopeSession:
    def __init__(self, *, global_permission: bool) -> None:
        self.global_permission = global_permission

    def execute(self, statement):
        sql = str(statement)
        if "role_permission" in sql:
            return _Result([(1,)] if self.global_permission else [])
        if "user_org_scope" in sql:
            return _Result([(10,)])
        if "org_unit" in sql:
            return _Result([(10, None), (11, 10), (12, None)])
        raise AssertionError(f"unexpected statement: {sql}")


def test_unrelated_global_role_does_not_widen_a_scoped_permission() -> None:
    # Principal.org_scope=None models an account that has an unrelated global
    # role.  The requested permission itself is only scoped, so explicit
    # user_org_scope must still win.
    principal = Principal(1, "mixed", frozenset({"export:data"}), None)

    assert resolve_permission_org_scope(
        _ScopeSession(global_permission=False), principal, "export:data"
    ) == frozenset({10, 11})


def test_global_role_that_grants_the_requested_permission_is_unrestricted() -> None:
    principal = Principal(1, "global-exporter", frozenset({"export:data"}), frozenset({10}))

    assert (
        resolve_permission_org_scope(
            _ScopeSession(global_permission=True), principal, "export:data"
        )
        is None
    )


def test_employee_pii_reveal_uses_the_permission_specific_scope(monkeypatch) -> None:
    """A global reader must not unmask records outside its local PII grant."""
    principal = Principal(1, "mixed", frozenset({"employee:read", "employee:pii"}), None)

    monkeypatch.setattr(
        employee_router,
        "resolve_permission_org_scope",
        lambda _session, _principal, permission: (
            frozenset({10}) if permission == "employee:pii" else None
        ),
    )

    pii_scope = employee_router._pii_scope(object(), principal)
    assert employee_router._reveal_pii(10, pii_scope)
    assert not employee_router._reveal_pii(20, pii_scope)


def test_employee_pii_reveal_fails_closed_without_pii_permission(monkeypatch) -> None:
    principal = Principal(1, "reader", frozenset({"employee:read"}), None)

    def _unexpected_scope_lookup(*_args):
        raise AssertionError("PII scope must not be resolved without employee:pii")

    monkeypatch.setattr(employee_router, "resolve_permission_org_scope", _unexpected_scope_lookup)

    assert not employee_router._reveal_pii(10, employee_router._pii_scope(object(), principal))
