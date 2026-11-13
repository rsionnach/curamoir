"""tests/conftest.py's grant_tmp cleanup (cm-4st): a test may leave a
directory unlistable or read-only, and the cleanup must still remove the
whole tree rather than error in teardown and leave it behind."""

import os

import pytest

from tests.conftest import remove_grant_tmp


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists a directory whatever its mode")
@pytest.mark.parametrize("mode", [0o000, 0o311, 0o500, 0o100])
def test_the_cleanup_removes_a_tree_a_test_left_unlistable_or_read_only(tmp_path, mode):
    root = tmp_path / "grant"
    deep = root / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "f").write_text("x")
    (root / "a" / "link").symlink_to(tmp_path / "outside")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside").chmod(0o500)
    for directory in (deep, root / "a"):
        directory.chmod(mode)
    try:
        remove_grant_tmp(root)
        assert not os.path.lexists(root)
        # The link was removed, not followed: what it named keeps its mode.
        assert (tmp_path / "outside").stat().st_mode & 0o777 == 0o500
    finally:
        (tmp_path / "outside").chmod(0o755)
