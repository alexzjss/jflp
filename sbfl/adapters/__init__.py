from .base import Adapter, Bug, Ctx, StageError, scan_test_classes


def get_adapters(cfg, names=None):
    from .defects4j import Defects4J
    from .bugsjar import BugsJar
    allx = {"d4j": Defects4J, "bugsjar": BugsJar}
    return [allx[n](cfg) for n in (names or allx)]
