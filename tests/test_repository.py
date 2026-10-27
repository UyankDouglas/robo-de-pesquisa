"""Histórico e auditoria persistidos no SQLite."""
from __future__ import annotations

from core.models import ItemStatus, RunStatus
from database.connection import Database
from database.repository import Repository, now_iso
from tests.conftest import fast_settings, make_import


def test_history_persists_across_connections(db, tmp_path):
    repo = Repository(db.connect())
    run_id = repo.create_run("auditoria", make_import(tmp_path, ["111", "222"]), fast_settings(interval_seconds=7))
    item = repo.items(run_id)[0]
    started = now_iso()
    repo.record_attempt(item.id, run_id, "fake", started, "transient_error", "timeout")
    repo.record_attempt(item.id, run_id, "fake", started, "found")
    repo.complete_item(item.id, ItemStatus.FOUND, {"nome": "Ana"}, None, 1234)
    repo.add_event(run_id, "teste", "evento qualquer")
    repo.set_run_status(run_id, RunStatus.COMPLETED, "ok")
    repo.close()

    reopened = Repository(Database(db.path).connect())  # nova conexão = como reabrir o programa
    run = reopened.get_run(run_id)
    assert run.status == RunStatus.COMPLETED and run.finished_at
    assert run.settings.interval_seconds == 7  # configuração usada fica registrada
    assert run.columns == ["ID", "DOC"]
    stored = reopened.items(run_id)[0]
    assert stored.identifier == "111" and stored.result == {"nome": "Ana"}
    assert stored.attempts == 2 and stored.queried_at and stored.duration_ms == 1234
    attempts = reopened.attempts(stored.id)
    assert [(a.attempt_no, a.outcome, a.message) for a in attempts] == [(1, "transient_error", "timeout"), (2, "found", None)]
    kinds = [e["kind"] for e in reopened.events(run_id)]
    assert kinds[0] == "created" and "teste" in kinds
    assert [r.id for r in reopened.list_runs()] == [run_id]
    reopened.close()


def test_stats_and_resumable_lookup(repo, tmp_path):
    imported = make_import(tmp_path, ["A", "B", "A", ""])
    run_id = repo.create_run("x", imported, fast_settings())
    stats = repo.stats(run_id)
    assert (stats.total, stats.pending, stats.duplicate, stats.invalid) == (2, 2, 1, 1)
    assert repo.find_resumable_run(imported.file, imported.sheet, "DOC", "fake").id == run_id
    assert repo.find_resumable_run(imported.file, imported.sheet, "DOC", "outro") is None
    for item in repo.items(run_id, [ItemStatus.PENDING]):
        repo.complete_item(item.id, ItemStatus.FOUND, {})
    assert repo.stats(run_id).percent == 100
    assert repo.find_resumable_run(imported.file, imported.sheet, "DOC", "fake") is None


def test_duplicate_points_to_first_occurrence(repo, tmp_path):
    run_id = repo.create_run("x", make_import(tmp_path, ["A", "B", "A"]), fast_settings())
    first, _, dup = repo.items(run_id)
    assert dup.status == ItemStatus.DUPLICATE and dup.duplicate_of == first.id
