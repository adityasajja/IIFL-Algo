"""A daily job runs once a day at its time, records failures, and never stops the others."""

from datetime import datetime

from atr.jobs.daily import DailyJob, JobStore, is_due, run_if_due

MON_1AM = datetime(2026, 9, 21, 1, 0)
MON_3AM = datetime(2026, 9, 21, 3, 0)
SAT_3AM = datetime(2026, 9, 19, 3, 0)


def test_runs_once_after_its_time_and_not_again_the_same_day(tmp_path):
    calls = []
    job = DailyJob("t", lambda: calls.append(1), at=(2, 0))
    store = JobStore(tmp_path)

    assert run_if_due(job, store, MON_1AM) is False
    assert run_if_due(job, store, MON_3AM) is True
    assert run_if_due(job, store, MON_3AM) is False
    assert calls == [1]


def test_a_failure_is_recorded_and_not_raised(tmp_path):
    def boom():
        raise RuntimeError("disk full")

    store = JobStore(tmp_path)
    assert run_if_due(DailyJob("t", boom), store, MON_3AM) is True

    state = store.read("t")
    assert state["ok"] is False and "disk full" in state["error"]


def test_weekday_only_jobs_skip_the_weekend():
    job = DailyJob("t", lambda: None, weekdays_only=True)
    assert is_due(job, {}, SAT_3AM) is False
    assert is_due(job, {}, MON_3AM) is True


def test_state_survives_a_restart(tmp_path):
    job = DailyJob("t", lambda: None)
    run_if_due(job, JobStore(tmp_path), MON_3AM)
    assert run_if_due(job, JobStore(tmp_path), MON_3AM) is False  # a fresh store reads the file


def test_a_failed_job_is_retried_the_same_day_but_only_so_many_times(tmp_path):
    from datetime import timedelta

    calls = []

    def flaky():
        calls.append(1)
        raise RuntimeError("no broker session")

    job = DailyJob("t", flaky, at=(2, 0), retries=2, retry_minutes=30)
    store = JobStore(tmp_path)

    assert run_if_due(job, store, MON_3AM) is True  # first try fails
    assert run_if_due(job, store, MON_3AM + timedelta(minutes=10)) is False  # too soon to retry
    assert run_if_due(job, store, MON_3AM + timedelta(minutes=31)) is True  # retry 1
    assert run_if_due(job, store, MON_3AM + timedelta(minutes=62)) is True  # retry 2
    assert run_if_due(job, store, MON_3AM + timedelta(minutes=93)) is False  # gives up for the day
    assert len(calls) == 3


def test_a_job_that_succeeds_is_not_retried(tmp_path):
    from datetime import timedelta

    job = DailyJob("t", lambda: None, retries=3, retry_minutes=1)
    store = JobStore(tmp_path)

    assert run_if_due(job, store, MON_3AM) is True
    assert run_if_due(job, store, MON_3AM + timedelta(hours=1)) is False
