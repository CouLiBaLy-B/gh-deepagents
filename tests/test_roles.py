"""Role store + effective role resolution."""
from __future__ import annotations

import fakeredis
import pytest

from gh_deepagent.webhook.auth_tokens import UserContext
from gh_deepagent.webhook.roles import Role, RoleStore, effective_role


@pytest.fixture()
def store(monkeypatch):
    s = RoleStore(client=fakeredis.FakeRedis())
    monkeypatch.setattr("gh_deepagent.webhook.roles._store", s)
    return s


# ---- Role parsing & rank

def test_role_parse_accepts_strings():
    assert Role.parse("viewer") == Role.VIEWER
    assert Role.parse("OPERATOR") == Role.OPERATOR
    assert Role.parse("  admin ") == Role.ADMIN


def test_role_parse_rejects_garbage():
    assert Role.parse(None) is None
    assert Role.parse("") is None
    assert Role.parse("god") is None


def test_role_rank_ordering():
    assert Role.ADMIN.can(Role.OPERATOR)
    assert Role.OPERATOR.can(Role.VIEWER)
    assert not Role.VIEWER.can(Role.OPERATOR)
    assert not Role.OPERATOR.can(Role.ADMIN)


# ---- Store CRUD

def test_set_and_get(store):
    store.set(10, "ALICE", Role.OPERATOR, granted_by="bob")
    assert store.get(10, "alice") == Role.OPERATOR
    assert store.get(10, "Alice") == Role.OPERATOR     # case-insensitive
    assert store.get(10, "carol") is None


def test_list(store):
    store.set(10, "alice", Role.ADMIN, granted_by="root")
    store.set(10, "bob", Role.VIEWER, granted_by="alice")
    out = store.list(10)
    assert out == {"alice": Role.ADMIN, "bob": Role.VIEWER}


def test_remove(store):
    store.set(10, "alice", Role.OPERATOR, granted_by="root")
    assert store.remove(10, "alice", removed_by="root") is True
    assert store.get(10, "alice") is None
    # second remove returns False
    assert store.remove(10, "alice", removed_by="root") is False


def test_audit_trail_records_changes(store):
    store.set(10, "alice", Role.ADMIN, granted_by="root")
    store.set(10, "bob", Role.OPERATOR, granted_by="alice")
    store.remove(10, "alice", removed_by="root")
    log = store.audit(10)
    assert len(log) == 3
    actions = [e["action"] for e in log]
    assert "remove" in actions
    assert "set" in actions


# ---- Effective role

def _u(login: str, iids: set[int] = frozenset(), admin: bool = False) -> UserContext:
    return UserContext(login=login, installation_ids=frozenset(iids),
                       is_admin=admin, via="github")


def test_effective_role_global_admin(store):
    user = _u("god", iids={1}, admin=True)
    # Global admin is admin even on unknown installations.
    assert effective_role(user, 999) == Role.ADMIN


def test_effective_role_default_viewer_with_install_access(store):
    user = _u("alice", iids={10})
    # No explicit role → viewer (because she has access via GitHub App).
    assert effective_role(user, 10) == Role.VIEWER


def test_effective_role_explicit_overrides_default(store):
    store.set(10, "alice", Role.OPERATOR, granted_by="root")
    user = _u("alice", iids={10})
    assert effective_role(user, 10) == Role.OPERATOR


def test_effective_role_none_without_install_access(store):
    user = _u("alice", iids={10})
    assert effective_role(user, 99) is None


def test_explicit_role_alone_is_not_enough(store):
    """A role assignment is useless if the user can't reach the installation
    via GitHub — that's the whole point of layering."""
    store.set(99, "alice", Role.ADMIN, granted_by="root")
    user = _u("alice", iids={10})   # no access to 99
    assert effective_role(user, 99) is None
