import pytest

from sbfl.toolchain import detect


@pytest.fixture(autouse=True)
def _sem_cache_de_sondas():
    """O cache de detecção é por processo; cada teste começa sem ele."""
    detect.clear_cache()
    yield
    detect.clear_cache()
