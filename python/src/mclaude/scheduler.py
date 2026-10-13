"""Parallel contiguous local reads; all other calls form sequential barriers."""

import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from itertools import groupby

from mclaude.cancellation import (
    cancellable_reads,
    check_read_cancelled,
    protect_cleanup,
)
from mclaude.provider import ToolUseBlock
from mclaude.tools import ToolResult

PARALLEL_TOOLS = frozenset({"read_file", "find_files", "search_text", "git_review"})
Execute = Callable[[ToolUseBlock], ToolResult]
Prepare = Callable[[ToolUseBlock], ToolResult | None]
Record = Callable[[ToolUseBlock, ToolResult], None]


class ToolScheduler:
    def __init__(self, workers: int = 4, *, enabled: bool = True):
        if type(workers) is not int or not 1 <= workers <= 16:
            raise ValueError("read_workers must be between 1 and 16.")
        self.workers = workers
        self.enabled = enabled and workers > 1
        self.started: set[str] = set()
        self.cancel = threading.Event()

    def _execute(self, call: ToolUseBlock, execute: Execute) -> ToolResult:
        with cancellable_reads(self.cancel):
            check_read_cancelled()
            self.started.add(call.id)
            try:
                return execute(call)
            except Exception as exc:
                # Only used for local read workers. Stateful tools stay on the
                # calling thread so persistence errors cannot be swallowed.
                return ToolResult(
                    f"Read-only tool failed ({type(exc).__name__}).", True
                )

    def run(
        self,
        calls: list[ToolUseBlock],
        prepare: Prepare,
        execute: Execute,
        record: Record,
    ) -> None:
        for parallel, group in groupby(
            calls, key=lambda call: self.enabled and call.name in PARALLEL_TOOLS
        ):
            batch = list(group)
            if not parallel or len(batch) == 1:
                for call in batch:
                    denied = prepare(call)
                    if denied is not None:
                        record(call, denied)
                    else:
                        self.started.add(call.id)
                        record(call, execute(call))
                continue
            # Permission prompts remain ordered and on the main thread. No
            # worker is submitted until all permission checks in this batch end.
            approved = []
            for call in batch:
                denied = prepare(call)
                if denied is not None:
                    record(call, denied)
                else:
                    approved.append(call)
            executor = ThreadPoolExecutor(
                max_workers=self.workers, thread_name_prefix="mclaude-read"
            )
            pending: dict[Future, ToolUseBlock] = {}
            try:
                for call in approved:
                    pending[executor.submit(self._execute, call, execute)] = call
                while pending:
                    done, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                    for future in done:
                        call = pending[future]
                        result = future.result()
                        record(call, result)
                        del pending[future]
            except KeyboardInterrupt:
                with protect_cleanup():
                    self.cancel.set()
                    executor.shutdown(wait=True, cancel_futures=True)
                    for future, call in pending.items():
                        if not future.cancelled() and future.done():
                            try:
                                result = future.result()
                            except KeyboardInterrupt:
                                continue
                            record(call, result)
                raise
            finally:
                with protect_cleanup():
                    self.cancel.set()
                    executor.shutdown(wait=True, cancel_futures=True)
                self.cancel.clear()
