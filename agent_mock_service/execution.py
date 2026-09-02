from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, TypeVar

ResultT = TypeVar("ResultT")


class ManagedExecutionComponent(Protocol):
    execution_resource_id: str


@dataclass(slots=True)
class ExecutionResource:
    resource_id: str
    max_concurrency: int
    executor: ThreadPoolExecutor = field(repr=False)


class ExecutionResourcePool:
    """Process-local governance for explicitly managed shared execution bottlenecks."""

    def __init__(self) -> None:
        self._resources: dict[str, ExecutionResource] = {}
        self._lock = threading.RLock()

    def register(self, resource_id: str, max_concurrency: int) -> None:
        if not resource_id.strip():
            raise ValueError("resource_id must not be empty")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        with self._lock:
            existing = self._resources.get(resource_id)
            if existing is not None:
                if existing.max_concurrency != max_concurrency:
                    raise ValueError(
                        f"execution resource '{resource_id}' is already registered "
                        f"with capacity {existing.max_concurrency}"
                    )
                return
            executor = ThreadPoolExecutor(
                max_workers=max_concurrency,
                thread_name_prefix=f"agent-mock-{resource_id}",
            )
            self._resources[resource_id] = ExecutionResource(resource_id, max_concurrency, executor)

    def submit(
        self,
        resource_id: str,
        fn: Callable[..., ResultT],
        *args: Any,
        **kwargs: Any,
    ) -> Future[ResultT]:
        with self._lock:
            resource = self._resources.get(resource_id)
            if resource is None:
                raise KeyError(f"execution resource '{resource_id}' is not registered")

            def execute() -> ResultT:
                return fn(*args, **kwargs)

            return resource.executor.submit(execute)

    def call(
        self,
        resource_id: str,
        fn: Callable[..., ResultT],
        *args: Any,
        **kwargs: Any,
    ) -> ResultT:
        return self.submit(resource_id, fn, *args, **kwargs).result()

    def call_component(
        self,
        component: object,
        fn: Callable[..., ResultT],
        *args: Any,
        **kwargs: Any,
    ) -> ResultT:
        resource_id = getattr(component, "execution_resource_id", None)
        if resource_id is None:
            return fn(*args, **kwargs)
        return self.call(str(resource_id), fn, *args, **kwargs)

    def submit_component(
        self,
        component: object,
        fn: Callable[..., ResultT],
        *args: Any,
        **kwargs: Any,
    ) -> Future[ResultT] | None:
        resource_id = getattr(component, "execution_resource_id", None)
        if resource_id is None:
            return None
        return self.submit(str(resource_id), fn, *args, **kwargs)

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            resources = list(self._resources.values())
            self._resources.clear()
        for resource in resources:
            resource.executor.shutdown(wait=wait)
