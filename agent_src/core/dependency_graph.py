"""Deterministic dependency graph built from Resource contracts."""

from __future__ import annotations

from collections import deque

from core.schema.resource import Resource


class DependencyGraphError(ValueError):
    pass


class DependencyGraph:
    def __init__(self, resources: list[Resource]) -> None:
        self.resources: dict[str, Resource] = {}
        for resource in resources:
            if resource.resource_id in self.resources:
                raise DependencyGraphError(f"duplicate resource_id: {resource.resource_id}")
            self.resources[resource.resource_id] = resource

        self._upstream: dict[str, tuple[str, ...]] = {}
        self._downstream: dict[str, set[str]] = {resource_id: set() for resource_id in self.resources}
        for resource_id, resource in self.resources.items():
            missing = sorted(set(resource.depends_on) - set(self.resources))
            if missing:
                raise DependencyGraphError(
                    f"resource {resource_id} references missing dependencies: {missing}"
                )
            dependencies = tuple(sorted(set(resource.depends_on)))
            self._upstream[resource_id] = dependencies
            for dependency in dependencies:
                self._downstream[dependency].add(resource_id)
        self._reject_cycles()

    def _reject_cycles(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(resource_id: str, path: list[str]) -> None:
            if resource_id in visiting:
                cycle_start = path.index(resource_id)
                cycle = path[cycle_start:] + [resource_id]
                raise DependencyGraphError(f"dependency cycle: {' -> '.join(cycle)}")
            if resource_id in visited:
                return
            visiting.add(resource_id)
            path.append(resource_id)
            for dependency in self._upstream[resource_id]:
                visit(dependency, path)
            path.pop()
            visiting.remove(resource_id)
            visited.add(resource_id)

        for resource_id in sorted(self.resources):
            visit(resource_id, [])

    def upstream(self, resource_id: str) -> list[str]:
        return self._traverse(resource_id, self._upstream)

    def downstream(self, resource_id: str) -> list[str]:
        adjacency = {key: tuple(sorted(value)) for key, value in self._downstream.items()}
        return self._traverse(resource_id, adjacency)

    def impact(self, resource_id: str) -> list[str]:
        self._require_resource(resource_id)
        return [resource_id, *self.downstream(resource_id)]

    def _traverse(self, resource_id: str, adjacency: dict[str, tuple[str, ...]]) -> list[str]:
        self._require_resource(resource_id)
        result: list[str] = []
        seen: set[str] = set()
        pending: deque[str] = deque(adjacency[resource_id])
        while pending:
            current = pending.popleft()
            if current in seen:
                continue
            seen.add(current)
            result.append(current)
            pending.extend(item for item in adjacency[current] if item not in seen)
        return result

    def _require_resource(self, resource_id: str) -> None:
        if resource_id not in self.resources:
            raise DependencyGraphError(f"unknown resource_id: {resource_id}")
