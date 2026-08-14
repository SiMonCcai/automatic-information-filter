"""Automatic Information Filter public API."""

from .models import InformationItem
from .pipeline import Pipeline, RunStats

__all__ = ["InformationItem", "Pipeline", "RunStats"]
__version__ = "0.1.0"
