import pytest
import contextlib
from datetime import datetime, timedelta, timezone

import psycopg2

from dashboard.monitoring import alert_fingerprint, compare_quality
from dashboard.monitor import is_healthy, run_once

COHORT = ("service-assistant", "mock-small", "relevance", "1", "default")


def compare(old, new, **kwargs):
    return compare_quality(old, new, baseline_cohort=COHORT, current_cohort=COHORT, **kwargs)


def test_detects_known_synthetic_quality_degradation():
    result = compare([1.0] * 60, [0.1] * 60)
    assert result.status == "quality_deterioration"
    assert result.lower_confidence_bound > 0


def test_stable_scores_and_random_variation_do_not_trigger():
    assert compare([0.8] * 60, [0.8] * 60).status == "no_supported_deterioration"
    assert compare([1, 0] * 60, [0, 1] * 60).status == "no_supported_deterioration"


def test_small_samples_are_not_called_drift():
    assert compare([1.0] * 29, [0.0] * 29).status == "insufficient_data"


def test_changes_in_model_or_evaluator_cohort_are_not_compared():
    result = compare_quality([1] * 100, [0] * 100, baseline_cohort=COHORT,
                             current_cohort=(*COHORT[:-1], "changed-judge-config"))
    assert result.status == "request_mix_changed"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, 1.1, True])
def test_invalid_scores_are_explicit(bad):
    assert compare([1] * 60, [bad] * 60).status == "invalid_data"


def test_rule_requires_real_sample_floor_and_fingerprints_are_idempotent():
    with pytest.raises(ValueError):
        compare([1] * 60, [0] * 60, min_samples=1)
    assert alert_fingerprint(COHORT, "2026-01-01") == alert_fingerprint(COHORT, "2026-01-01")
    assert alert_fingerprint(COHORT, "2026-01-01") != alert_fingerprint(COHORT, "2026-01-02")


def test_alert_fingerprint_includes_monitor_threshold_configuration():
    default = alert_fingerprint(COHORT, "2026-01-01", rule_config={"min_drop": .15, "alpha": .01})
    assert default == alert_fingerprint(COHORT, "2026-01-01", rule_config={"alpha": .01, "min_drop": .15})
    assert default != alert_fingerprint(COHORT, "2026-01-01", rule_config={"min_drop": .3, "alpha": .01})


@pytest.mark.parametrize("floor", [float("nan"), float("inf"), 30.5, "30"])
def test_noninteger_sample_floors_are_rejected(floor):
    with pytest.raises(ValueError):
        compare([], [], min_samples=floor)


def seed_history(dsn, *, trusted_cohort="repair-documents-v1", samples=60):
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    with contextlib.closing(psycopg2.connect(dsn)) as conn, conn, conn.cursor() as cur:
        for age, score in [(40, 1.0), (10, 0.1)]:
            for _ in range(samples):
                cur.execute("""INSERT INTO traces(agent_id,model,eval_cohort,created_at,eval_status)
                    VALUES ('synthetic-service-agent','mock-small',%s,%s,'scored') RETURNING id""",
                    (trusted_cohort, end-timedelta(hours=age)))
                trace_id = cur.fetchone()[0]
                for attempt in [0, 1]:
                    cur.execute("""INSERT INTO evaluation_runs(trace_id,evaluator_name,evaluator_version,
                        config_version,attempt,status,score,reason) VALUES (%s,'reference_exact','1','test',%s,
                        'scored',%s,'synthetic trusted reference agreement')""", (trace_id, attempt, score))
    return end


def test_monitor_persists_deduplicated_alerts_and_heartbeat(db):
    end = seed_history(db)
    with contextlib.closing(psycopg2.connect(db)) as conn:
        conn.autocommit = True
        first = run_once(conn, now=end)
        second = run_once(conn, now=end)
        assert first["alerts_inserted"] == 1 and second["alerts_inserted"] == 0
        assert first["scored_rows"] == 120  # Retry histories are not independent samples.
        with conn.cursor() as cur:
            cur.execute("SELECT count(*),min(metric) FROM alerts WHERE metric='candidate_quality_deterioration'")
            assert cur.fetchone() == (1, "candidate_quality_deterioration")
            cur.execute("SELECT details FROM worker_heartbeats WHERE worker_id='quality-monitor'")
            assert cur.fetchone()[0]["outcomes"] == {"quality_deterioration": 1}


def test_monitor_refuses_to_compare_unknown_request_mix(db):
    end = seed_history(db, trusted_cohort=None)
    with contextlib.closing(psycopg2.connect(db)) as conn:
        conn.autocommit = True
        outcome = run_once(conn, now=end)
        assert outcome["alerts_inserted"] == 0
        assert outcome["outcomes"]["unknown_request_mix_samples"] == 120


def test_monitor_reports_insufficient_cohort_samples(db):
    end = seed_history(db, samples=5)
    with contextlib.closing(psycopg2.connect(db)) as conn:
        conn.autocommit = True
        outcome = run_once(conn, now=end)
        assert outcome["alerts_inserted"] == 0
        assert outcome["outcomes"]["insufficient_data"] == 1


def test_monitor_reports_missing_scored_data(db):
    with contextlib.closing(psycopg2.connect(db)) as conn:
        conn.autocommit = True
        outcome = run_once(conn)
        assert outcome["alerts_inserted"] == 0
        assert outcome["outcomes"] == {"no_scored_data": 1}


def test_worker_health_requires_recent_matching_monitor_heartbeat(db):
    with contextlib.closing(psycopg2.connect(db)) as conn:
        conn.autocommit = True
        assert not is_healthy(conn, worker_id="synthetic-health-monitor")
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO worker_heartbeats(worker_id,worker_type,last_seen)
                VALUES ('synthetic-health-monitor','monitoring',now())
                ON CONFLICT(worker_id) DO UPDATE SET last_seen=excluded.last_seen""")
        assert is_healthy(conn, worker_id="synthetic-health-monitor")
        with conn.cursor() as cur:
            cur.execute("UPDATE worker_heartbeats SET last_seen=now()-interval '10 minutes' "
                        "WHERE worker_id='synthetic-health-monitor'")
        assert not is_healthy(conn, worker_id="synthetic-health-monitor")
