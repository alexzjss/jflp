"""Run: python3 -m unittest bench/test_sbfl_common.py"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sbfl_common import map_to_classes, patch_lines, rank  # noqa: E402

# Defects4J Lang-1, abridged: fixed -> buggy, so '+' lines are the fault.
D4J = """--- a/src/main/java/org/x/N.java
+++ b/src/main/java/org/x/N.java
@@ -464,9 +464,3 @@ public class N {
         if (pfxLen > 0) {
-            char first = 0;
-            for (;;) {}
             final int hexDigits = str.length() - pfxLen;
-            if (hexDigits > 16 || first > '7') {
+            if (hexDigits > 16) {
                 return createBigInteger(str);
"""

# Bugs.jar MATH-1005, abridged: buggy -> fixed, the fix only adds code.
BUGSJAR = """--- a/src/main/java/org/x/M.java
+++ b/src/main/java/org/x/M.java
@@ -818,4 +818,7 @@ public class M {
         }
 
+        if (len == 1) {
+            return a[0] * b[0];
+        }
         final double[] prodHigh = new double[len];
"""


def lines(text, side):
    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False) as f:
        f.write(text)
    try:
        return sorted(l for _, l in patch_lines(f.name, side))
    finally:
        os.unlink(f.name)


class PatchLines(unittest.TestCase):

    def test_changed_line_is_the_buggy_side_line(self):
        # The buggy condition is line 466 of the buggy file; 465 is where the
        # deleted (missing) code belonged, an omission fault.
        self.assertEqual([465, 466], lines(D4J, "+"))

    def test_omission_marks_the_statement_after_the_missing_code(self):
        # Nothing on the buggy side changed; the fault is the absence of a guard
        # before line 820 (818 and 819 are context), the first statement that misbehaves.
        self.assertEqual([820], lines(BUGSJAR, "-"))

    def test_inner_classes_share_the_source_file(self):
        universe = [("org/x/N", 465), ("org/x/N$1", 466), ("org/x/Other", 466)]
        hits = {("src/main/java/org/x/N.java", 465), ("src/main/java/org/x/N.java", 466)}
        self.assertEqual({("org/x/N", 465), ("org/x/N$1", 466)}, map_to_classes(hits, universe))


class Rank(unittest.TestCase):

    def test_ties_share_the_mean_position(self):
        # Two lines tie at the top: either could be inspected first, 1.5 on average.
        universe = ["a", "b", "c"]
        self.assertEqual(1.5, rank({"a": 0.9, "b": 0.9, "c": 0.1}, universe, {"a"}))

    def test_best_ranked_buggy_line_counts(self):
        self.assertEqual(1.0, rank({"a": 0.9, "b": 0.5}, ["a", "b"], {"a", "b"}))


if __name__ == "__main__":
    unittest.main()
