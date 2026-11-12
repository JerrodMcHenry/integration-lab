"""Bounded waiting for tests that use threads, processes and pools.

Every helper here either finishes within its timeout or fails the test with a
message saying *what* was being waited for, plus the stack of anything still
running. None of them continue silently after a timeout, and none add sleeps:
they only bound waits that already exist.

Cleanup guarantees:
- ``start_threads`` creates daemon threads, so a stuck thread can never block
  interpreter exit (which is how a hang would otherwise outlive the test).
- ``spawned_process`` always kills and reaps its child in ``finally``, even
  when the test body fails.
- ``ThreadPoolExecutor`` work goes through ``run_in_threads``, which cancels
  outstanding work instead of waiting for it on timeout.
- ``multiprocessing`` pools are used as context managers (``__exit__`` calls
  ``terminate()``), and ``pool_results`` bounds every ``.get()``.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import multiprocessing
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import pytest


def _stacks(threads: Iterable[threading.Thread]) -> str:
    frames = sys._current_frames()
    out = []
    for t in threads:
        frame = frames.get(t.ident)
        stack = "".join(traceback.format_stack(frame)) if frame else "    (no frame)\n"
        out.append(f"--- thread {t.name!r} ---\n{stack}")
    return "\n".join(out)


def start_threads(targets: Iterable[Callable[[], Any]], *, name: str = "test") -> list[threading.Thread]:
    threads = [threading.Thread(target=fn, name=f"{name}-{i}", daemon=True) for i, fn in enumerate(targets)]
    for t in threads:
        t.start()
    return threads


def join_all(threads: Iterable[threading.Thread], *, timeout: float, what: str) -> None:
    threads = list(threads)
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    alive = [t for t in threads if t.is_alive()]
    if alive:
        pytest.fail(f"timed out after {timeout}s waiting for {what}: {len(alive)} thread(s) still running\n"
                    f"{_stacks(alive)}", pytrace=False)


def wait_event(event: threading.Event, *, timeout: float, what: str) -> None:
    if not event.wait(timeout):
        pytest.fail(f"timed out after {timeout}s waiting for {what}", pytrace=False)


def wait_until(predicate: Callable[[], bool], *, timeout: float, what: str, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out after {timeout}s waiting for {what}", pytrace=False)
        time.sleep(interval)


def pool_results(pending: list, *, timeout: float, what: str) -> list:
    """``.get()`` every ``AsyncResult`` within one overall deadline."""
    deadline = time.monotonic() + timeout
    results = []
    for i, p in enumerate(pending):
        try:
            results.append(p.get(timeout=max(0.0, deadline - time.monotonic())))
        except multiprocessing.TimeoutError:
            pytest.fail(f"timed out after {timeout}s waiting for {what}: result {i + 1}/{len(pending)} "
                        f"not ready ({len(results)} completed)", pytrace=False)
    return results


def run_in_threads(fn: Callable[[], Any], *, count: int, timeout: float, what: str) -> list:
    """Run ``fn`` ``count`` times concurrently; return results in completion order."""
    pool = concurrent.futures.ThreadPoolExecutor(count)
    try:
        futures = [pool.submit(fn) for _ in range(count)]
        done, not_done = concurrent.futures.wait(futures, timeout=timeout)
        if not_done:
            pytest.fail(f"timed out after {timeout}s waiting for {what}: {len(not_done)}/{count} still running",
                        pytrace=False)
        return [f.result() for f in done]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


@contextlib.contextmanager
def spawned_process(target: Callable[..., Any], args: tuple = (), *,
                    ctx=None) -> Iterator[multiprocessing.Process]:
    """Start a child process; always kill and reap it, even if the test body fails."""
    ctx = ctx or multiprocessing.get_context("spawn")
    proc = ctx.Process(target=target, args=args, daemon=True)
    proc.start()
    try:
        yield proc
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(10)
        if proc.is_alive():  # pragma: no cover - would indicate an OS-level problem
            pytest.fail(f"child process {proc.pid} could not be reaped", pytrace=False)
