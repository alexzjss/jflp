from .base import Adapter, Bug, Ctx, scan_test_classes, scan_tests
from ..errors import StageError

NAMES = ["d4j", "bugsjar", "bears", "gitbugjava", "manifest"]


def get_adapters(cfg, names=None):
    from .defects4j import Defects4J
    from .bugsjar import BugsJar
    from .bears import Bears
    from .gitbugjava import GitBugJava
    from .manifest import Manifest
    allx = {"d4j": Defects4J, "bugsjar": BugsJar, "bears": Bears,
            "gitbugjava": GitBugJava, "manifest": Manifest}
    return [allx[n](cfg) for n in (names or NAMES)]
