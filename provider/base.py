from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from .models import ContextCandidate, ContextFetchRequest


class ProviderError(RuntimeError):
    pass


class BaseContextProvider(ABC):
    name = "base"

    @abstractmethod
    def fetch_candidates(
        self, request: ContextFetchRequest
    ) -> Sequence[ContextCandidate]:
        raise NotImplementedError
