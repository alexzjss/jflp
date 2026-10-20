"""Gerador do POM do Jaguar 2 (jaguar2-pom.xml) com correções nomeadas."""
from .fixes import FIXES
from .generate import PomPlan, generate, write

__all__ = ["generate", "write", "PomPlan", "FIXES"]
