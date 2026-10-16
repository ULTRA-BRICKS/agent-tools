"""Real policy/SQLite tests: never contact ChatGPT or use account credentials."""

import asyncio
import concurrent.futures
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agenttools_chatgpt.core import Guardian, Observation, Store


def test_failure_must_be_stable_before_reservation(tmp_path):
    guardian = Guardian(Store(tmp_path / "state.sqlite"), apply=True)
    failure = Observation(
        "chat",
        "turn",
        "generation",
        status="Thinking failed",
        active=True,
        verified=True,
        model="Astra",
        ambiguous=False,
    )
    assert guardian.consider(failure, 0, 1000) == "stabilizing"
    for tick in (5, 10, 15):
        assert guardian.consider(failure, tick, 1000 + tick) == "stabilizing"
    assert guardian.consider(failure, 19.999, 1019.999) == "stabilizing"
    assert guardian.consider(failure, 20, 1020) == "reserved"


def failure(chat="chat", turn="turn", generation="generation", **changes):
    return replace(
        Observation(
            chat,
            turn,
            generation,
            status="Thinking failed",
            active=True,
            verified=True,
            model="Astra",
            ambiguous=False,
        ),
        **changes,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "ordinary Thinking failed text"},
        {"active": False},
        {"verified": False},
        {"running": True},
        {"draft": True},
        {"challenge": True},
        {"ambiguous": True},
        {"chat": ""},
        {"generation": ""},
    ],
)
def test_unsafe_observation_cannot_reserve(tmp_path, changes):
    store = Store(tmp_path / "state.sqlite")
    guardian = Guardian(store, apply=True)
    obs = failure(**changes)
    assert guardian.consider(obs, 0, 1000) == "unsafe-or-not-active-failure"
    assert guardian.consider(obs, 50, 1050) == "unsafe-or-not-active-failure"
    assert not store.status()["attempts"]


def test_restart_nonfailure_and_generation_change_reset_stability(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    guardian = Guardian(store, apply=True)
    obs = failure()
    guardian.consider(obs, 0, 1000)
    guardian.consider(replace(obs, running=True), 19, 1019)
    assert guardian.consider(obs, 20, 1020) == "stabilizing"
    assert guardian.consider(replace(obs, generation="next"), 40, 1040) == "stabilizing"
    assert Guardian(store, apply=True).consider(obs, 100, 1100) == "stabilizing"


def test_crash_pending_intent_blocks_other_workers_and_generations(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path)
    assert store.reserve(failure(), 1000) == "reserved"
    assert Store(path).reserve(failure(generation="next"), 1080) == "unresolved-send"
    store.resolve("chat", "turn", "generation")
    assert store.reserve(failure(), 1080) == "deduplicated"


def test_concurrent_workers_only_one_reservation(tmp_path):
    path = tmp_path / "state.sqlite"
    Store(path)
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(
            pool.map(lambda _: Store(path).reserve(failure(), 1000), range(8))
        )
    assert results.count("reserved") == 1
    assert len(Store(path).status()["attempts"]) == 1


def test_cooldown_and_hourly_limit_count_all_intents(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    first = failure()
    assert store.reserve(first, 1000) == "reserved"
    store.finish(first, True)
    assert store.reserve(failure(generation="b"), 1059.999) == "cooldown"
    for generation, wall in [("b", 1060), ("c", 1120)]:
        obs = failure(generation=generation)
        assert store.reserve(obs, wall) == "reserved"
        store.finish(obs, True)
    assert store.reserve(failure(generation="d"), 1180) == "hourly-limit"
    assert store.reserve(failure(generation="d"), 4600) == "reserved"


def test_clock_rollback_persistently_stops(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    obs = failure()
    store.reserve(obs, 1000)
    store.finish(obs, True)
    assert store.reserve(failure(generation="next"), 999) == "clock-backwards"
    assert store.reserve(failure(generation="next"), 2000) == "stopped"


def test_quota_is_global_across_profiles_and_sol_consent_exact(tmp_path):
    path = tmp_path / "shared.sqlite"
    home, glide = Store(path), Store(path)
    Guardian(home).consider(failure(quota=True), 0, 1000)
    assert glide.reserve(failure(chat="glide"), 1000) == "astra-quota"
    sol = failure(
        chat="https://chatgpt.com/c/sol-chat", model="Sol", effort="Extra High"
    )
    assert glide.reserve(sol, 1000) == "sol-consent-required"
    glide.opt_in_sol("https://chatgpt.com/c/other")
    assert glide.reserve(sol, 1000) == "sol-consent-required"
    glide.opt_in_sol("https://chatgpt.com/c/sol-chat")
    assert glide.reserve(replace(sol, effort="High"), 1000) == "unverified-model-effort"
    assert glide.reserve(sol, 1000) == "reserved"


class ChangingAdapter:
    def __init__(self, before, after):
        self.before, self.after, self.calls, self.sent = before, after, 0, 0

    async def observe(self):
        self.calls += 1
        return self.before if self.calls == 1 else self.after

    async def send(self, expected):
        self.sent += 1
        return True


def ready_guardian(store, obs, apply=True):
    guardian = Guardian(store, apply=apply)
    guardian.stability[obs.chat] = (obs, 0)
    import time

    guardian.last_seen[obs.chat] = time.monotonic()
    return guardian


def test_final_revalidation_and_observe_only(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    obs = failure()
    adapter = ChangingAdapter(obs, replace(obs, running=True))
    assert (
        asyncio.run(ready_guardian(store, obs).run_once(adapter))
        == "safety-stop-before-send"
    )
    assert adapter.sent == 0
    assert store.status()["attempts"][0]["state"] == "uncertain"
    adapter = ChangingAdapter(failure(chat="another"), failure(chat="another"))
    assert (
        asyncio.run(ready_guardian(store, adapter.before, False).run_once(adapter))
        == "observe"
    )
    assert adapter.sent == 0


def test_manual_stop_after_reserve_is_checked(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    obs = failure()
    store.reserve(obs, 1000)
    store.stop()
    assert not store.can_send_reserved(obs)
    store.resume()
    store.quota()
    assert not store.can_send_reserved(obs)


def test_real_process_race_and_crash_intent(tmp_path):
    import os
    import subprocess

    path = tmp_path / "process.sqlite"
    Store(path)
    script = """import sys,os
from pathlib import Path
from agenttools_chatgpt.core import Store,Observation
obs=Observation('chat','turn','g',status='Thinking failed',active=True,verified=True,model='Astra',ambiguous=False)
result=Store(Path(sys.argv[1])).reserve(obs,1000)
print(result,flush=True)
os._exit(0)
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "lib"))
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(path)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(6)
    ]
    results = []
    for process in processes:
        out, err = process.communicate(timeout=10)
        assert process.returncode == 0, err.decode()
        results.append(out.decode().strip())
    assert results.count("reserved") == 1
    assert (
        Store(path).reserve(failure(generation="after-crash"), 1080)
        == "unresolved-send"
    )


def test_final_unknown_chat_challenge_latches_global_stop(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    obs = failure()
    adapter = ChangingAdapter(obs, Observation("", "", "", challenge=True))
    assert (
        asyncio.run(ready_guardian(store, obs).run_once(adapter))
        == "safety-stop-before-send"
    )
    assert "stop:*" in store.controls()
    assert store.reserve(failure(chat="another"), None) == "stopped"


@pytest.mark.parametrize("age", [7, None])
def test_cached_old_snapshot_cannot_be_relabelled_fresh(tmp_path, age):
    import time

    store = Store(tmp_path / "state.sqlite")
    obs = failure()
    guardian = ready_guardian(store, obs)
    adapter = ChangingAdapter(obs, obs)
    assert (
        asyncio.run(
            guardian.run_once(
                adapter,
                initial=obs,
                observed_at=None if age is None else time.monotonic() - age,
            )
        )
        == "stale-observation"
    )
    assert adapter.sent == 0 and not guardian.stability
