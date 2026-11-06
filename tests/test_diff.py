from reviewcrew.diff import parse_diff

DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,4 +1,5 @@ def top():
 import os
--- a removed line that looks like a header
+import subprocess
+x = 1
 def f():
     pass
@@ -20,2 +21,3 @@
 a = 1
+b = 2
 c = 3
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+one = 1
+two = 2
\\ No newline at end of file
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-gone = True
diff --git a/logo.png b/logo.png
Binary files a/logo.png and b/logo.png differ
diff --git a/poetry.lock b/poetry.lock
--- a/poetry.lock
+++ b/poetry.lock
@@ -1 +1 @@
-a
+b
"""


def test_line_numbers_and_statuses():
    files = {f.path: f for f in parse_diff(DIFF)}
    app = files["app.py"]
    assert app.added_lines == {2: "import subprocess", 3: "x = 1", 22: "b = 2"}
    assert app.deletions == 1  # "--- a removed line" is a deletion, not a header
    assert {1, 4, 5, 21, 23} <= app.commentable_lines
    assert files["new.py"].status == "added" and files["new.py"].added_lines == {1: "one = 1", 2: "two = 2"}
    assert files["old.py"].status == "deleted" and not files["old.py"].is_reviewable
    assert files["logo.png"].binary and not files["logo.png"].is_reviewable
    assert not files["poetry.lock"].is_reviewable


def test_render_shows_new_side_line_numbers():
    text = parse_diff(DIFF)[0].render()
    assert "    2 +import subprocess" in text and "   22 +b = 2" in text


def test_test_file_detection():
    files = parse_diff(
        "diff --git a/tests/test_x.py b/tests/test_x.py\n--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n@@ -0,0 +1 @@\n+def test(): pass\n"
    )
    assert files[0].is_test
