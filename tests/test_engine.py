"""Motor de processamento com adaptador simulado (sem navegador)."""
from __future__ import annotations

import threading
import time

import pytest

from core.engine import EngineState, ProcessingEngine
from core.locking import RunLock
from core.models import (
    AlreadyRunningError,
    HumanVerificationRequired,
    ItemStatus,
    PermanentError,
    RateLimitedError,
    RunStatus,
    SearchResult,
    SessionExpiredError,
    TransientError,
)
from tests.conftest import FakeAdapter, fast_settings, make_import, wait_until


def _engine(db, run_id, adapter, events=None, **kwargs):
    return ProcessingEngine(db, run_id, adapter_factory=lambda s: adapter,
                            on_event=(events.append if events is not None else None), **kwargs)


def _create(repo, tmp_path, ids, **settings):
    return repo.create_run("t", make_import(tmp_path, ids), fast_settings(**settings))


def test_processes_all_records_and_classifies(repo, db, tmp_path):
    run_id = _create(repo, tmp_path, ["A", "B", "C", "A", ""])
    adapter = FakeAdapter({"B": [SearchResult.not_found("Nada")]})
    events = []
    status = _engine(db, run_id, adapter, events, interactive=False).run()

    assert status == RunStatus.COMPLETED
    assert adapter.called() == ["A", "B", "C"]  # duplicado e vazio não são consultados
    stats = repo.stats(run_id)
    assert (stats.found, stats.not_found, stats.error, stats.duplicate, stats.invalid) == (2, 1, 0, 1, 1)
    item_b = next(i for i in repo.items(run_id) if i.identifier == "B")
    assert item_b.status == ItemStatus.NOT_FOUND and item_b.message == "Nada"  # sem resultado não é erro
    kinds = [e.kind for e in events]
    assert kinds.count("item_finished") == 3 and kinds[-1] == "finished"
    assert repo.get_run(run_id).output_file  # Excel gerado ao final


def test_transient_errors_retry_with_backoff_then_succeed(repo, db, tmp_path):
    run_id = _create(repo, tmp_path, ["X"], max_retries=3, backoff_base_seconds=0.05, backoff_max_seconds=1)
    adapter = FakeAdapter({"X": [TransientError("rede"), TimeoutError("lento"), SearchResult.found_with({"nome": "ok"})]})
    assert _engine(db, run_id, adapter, interactive=False).run() == RunStatus.COMPLETED
    item = repo.items(run_id)[0]
    assert item.status == ItemStatus.FOUND and item.attempts == 3
    gaps = [b[1] - a[1] for a, b in zip(adapter.calls, adapter.calls[1:])]
    assert gaps[0] >= 0.045 and gaps[1] >= 0.095  # espera progressiva: 0,05 s e 0,10 s
    outcomes = [a.outcome for a in repo.attempts(item.id)]
    assert outcomes == ["transient_error", "transient_error", "found"]


def test_transient_errors_exhausted_become_error(repo, db, tmp_path):
    run_id = _create(repo, tmp_path, ["X", "Y"], max_retries=2)
    adapter = FakeAdapter({"X": [TransientError("500")] * 5})
    assert _engine(db, run_id, adapter, interactive=False).run() == RunStatus.COMPLETED
    x, y = repo.items(run_id)
    assert x.status == ItemStatus.ERROR and x.attempts == 3 and "500" in x.message
    assert y.status == ItemStatus.FOUND  # o erro de um registro não interrompe os demais


def test_permanent_error_is_not_retried(repo, db, tmp_path):
    run_id = _create(repo, tmp_path, ["X"], max_retries=3)
    adapter = FakeAdapter({"X": [PermanentError("recusado")]})
    _engine(db, run_id, adapter, interactive=False).run()
    assert adapter.called() == ["X"]
    assert repo.items(run_id)[0].status == ItemStatus.ERROR


def test_rate_limit_waits_retry_after_without_consuming_retries(repo, db, tmp_path):
    run_id = _create(repo, tmp_path, ["X"], max_retries=0)
    adapter = FakeAdapter({"X": [RateLimitedError("429", retry_after=0.2), SearchResult.found_with({})]})
    _engine(db, run_id, adapter, interactive=False).run()
    assert repo.items(run_id)[0].status == ItemStatus.FOUND
    assert adapter.calls[1][1] - adapter.calls[0][1] >= 0.19


def test_interval_between_queries(repo, db, tmp_path):
    run_id = _create(repo, tmp_path, ["A", "B", "C"], interval_seconds=0.15)
    adapter = FakeAdapter()
    _engine(db, run_id, adapter, interactive=False).run()
    gaps = [b[1] - a[1] for a, b in zip(adapter.calls, adapter.calls[1:])]
    assert all(g >= 0.14 for g in gaps)


def test_pause_and_resume(repo, db, tmp_path):
    ids = [f"R{i}" for i in range(8)]
    run_id = _create(repo, tmp_path, ids)
    adapter = FakeAdapter(delay=0.03)
    engine = _engine(db, run_id, adapter)
    engine.start()
    assert wait_until(lambda: len(adapter.calls) >= 2)
    engine.pause()
    assert wait_until(lambda: engine.state == EngineState.PAUSED)
    frozen = len(adapter.calls)
    threading.Event().wait(0.3)
    assert len(adapter.calls) == frozen  # nada é consultado enquanto pausado
    assert repo.get_run(run_id).status == RunStatus.PAUSED
    engine.resume()
    assert engine.join(10)
    assert engine.final_status == RunStatus.COMPLETED
    assert adapter.called() == ids  # sem repetir nem pular registros


def test_stop_is_safe_and_resumable(repo, db, tmp_path):
    run_id = _create(repo, tmp_path, [f"S{i}" for i in range(20)])
    adapter = FakeAdapter(delay=0.03)
    engine = _engine(db, run_id, adapter)
    engine.start()
    assert wait_until(lambda: len(adapter.calls) >= 3)
    engine.stop()
    assert engine.join(10)
    assert engine.final_status == RunStatus.STOPPED
    stats = repo.stats(run_id)
    assert stats.processing == 0 and stats.found == len(adapter.calls) and stats.pending > 0
    assert engine.output_path and engine.output_path.exists()  # Excel salvo na parada


def test_captcha_without_operator_stops_for_operator(repo, db, tmp_path):
    run_id = _create(repo, tmp_path, ["A", "CAPTCHA", "C"])
    adapter = FakeAdapter({"CAPTCHA": [HumanVerificationRequired("captcha")]})
    status = _engine(db, run_id, adapter, interactive=False).run()
    assert status == RunStatus.NEEDS_OPERATOR
    statuses = {i.identifier: i.status for i in repo.items(run_id)}
    assert statuses == {"A": ItemStatus.FOUND, "CAPTCHA": ItemStatus.PENDING, "C": ItemStatus.PENDING}


def test_captcha_with_operator_waits_and_continues(repo, db, tmp_path):
    run_id = _create(repo, tmp_path, ["A", "CAPTCHA", "C"])
    adapter = FakeAdapter({"CAPTCHA": [HumanVerificationRequired("captcha")]})
    events = []
    engine = _engine(db, run_id, adapter, events)
    engine.start()
    assert wait_until(lambda: engine.state == EngineState.WAITING_OPERATOR)
    assert any(e.kind == "operator" and e.data["reason"] == "captcha" for e in events)
    threading.Event().wait(0.2)
    assert adapter.called() == ["A", "CAPTCHA"]  # nada avança sem o operador
    engine.resume()  # operador resolveu e clicou em CONTINUAR
    assert engine.join(10) and engine.final_status == RunStatus.COMPLETED
    assert adapter.called() == ["A", "CAPTCHA", "CAPTCHA", "C"]


def test_session_expired_requests_login_again(repo, db, tmp_path):
    run_id = _create(repo, tmp_path, ["A", "B"])
    adapter = FakeAdapter({"B": [SessionExpiredError("expirou")]}, login_required=True)
    adapter.logged_in = True
    engine = _engine(db, run_id, adapter)
    engine.start()
    assert wait_until(lambda: engine.state == EngineState.WAITING_OPERATOR)
    assert adapter.login_prompts == 1
    engine.resume()
    assert engine.join(10) and engine.final_status == RunStatus.COMPLETED
    assert repo.stats(run_id).found == 2


def test_login_required_without_operator(repo, db, tmp_path):
    run_id = _create(repo, tmp_path, ["A"])
    adapter = FakeAdapter(login_required=True)
    assert _engine(db, run_id, adapter, interactive=False).run() == RunStatus.NEEDS_OPERATOR
    assert adapter.called() == []


def test_autosave_during_processing(repo, db, tmp_path):
    run_id = _create(repo, tmp_path, [f"A{i}" for i in range(5)], autosave_every=2)
    saves = []
    _engine(db, run_id, FakeAdapter(delay=0.3), saves, interactive=False).run()
    kinds = [e.kind for e in saves]
    last_item = max(i for i, k in enumerate(kinds) if k == "item_finished")
    assert kinds.count("exported") >= 2 and kinds.index("exported") < last_item  # salva durante, não só no fim


def test_autosave_is_throttled_when_export_is_slow(repo, db, tmp_path, monkeypatch):
    import core.engine as engine_module

    real_export = engine_module.export_run

    def slow_export(*args, **kwargs):
        time.sleep(0.05)  # simula planilha enorme
        return real_export(*args, **kwargs)

    monkeypatch.setattr(engine_module, "export_run", slow_export)
    run_id = _create(repo, tmp_path, [f"A{i}" for i in range(30)], autosave_every=1)
    saves = []
    _engine(db, run_id, FakeAdapter(delay=0.01), saves, interactive=False).run()
    exports = len([e for e in saves if e.kind == "exported"])
    assert 2 <= exports < 15  # não regrava a cada registro quando exportar é lento
    assert repo.stats(run_id).found == 30


def test_single_instance_per_run(repo, db, tmp_path):
    run_id = _create(repo, tmp_path, ["A"])
    with RunLock(run_id):
        with pytest.raises(AlreadyRunningError):
            _engine(db, run_id, FakeAdapter(), interactive=False).run()
    assert _engine(db, run_id, FakeAdapter(), interactive=False).run() == RunStatus.COMPLETED


def test_unexpected_adapter_failure_marks_run_failed(repo, db, tmp_path):
    run_id = _create(repo, tmp_path, ["A"])

    def broken_factory(settings):
        raise RuntimeError("navegador não abriu")

    engine = ProcessingEngine(db, run_id, adapter_factory=broken_factory, interactive=False)
    assert engine.run() == RunStatus.FAILED
    assert "navegador não abriu" in repo.get_run(run_id).message
    assert repo.stats(run_id).pending == 1
