"""Which index file and which store belongs to which (project, source). Two different pairs must never share either, and no id may reach outside the index directory: a shared index is retrieval across projects."""
from __future__ import annotations

import itertools
import os

import pytest

from agentic_os.sources_rag import RagIndexer


class Fake:
    """Remembers its path, so the test can see which pairs were given the same file."""

    def __init__(self, path):
        self.path = path

    def index(self, root):
        return {"chunks": 1}

    def search(self, query, k=8):
        return [{"filename": self.path, "text": "x"}]


def indexer(tmp_path):
    return RagIndexer(db_dir=str(tmp_path / ".rag"), rag_factory=Fake)


PLAIN = [("acme", "crm"), ("acme", "drive-2"), ("p.1", "s.1"), ("Proj-9", "Src")]
AWKWARD = [("a_b", "c"), ("a", "b_c"), ("a:b", "c"), ("a", "b:c"), ("../escape", "x"), ("p", "../../etc/passwd"), ("a/b", "c"), ("a", "b/c"), ("a b", "c"), ("a", "b c"),
           ("é", "x"), ("x", "é"), ("..", "x"), ("a", ".."), ("a.", ".b"), (".a", "b"), ("a\\b", "c"), ("a", "b\\c"), ("A", "b"), ("a", "B")]


def test_different_pairs_never_share_an_index_file_or_a_store(tmp_path):
    ix = indexer(tmp_path)
    pairs = PLAIN + AWKWARD
    stores = {pair: ix._store(*pair) for pair in pairs}
    assert len({id(s) for s in stores.values()}) == len(pairs)
    assert len({s.path for s in stores.values()}) == len(pairs)


@pytest.mark.parametrize("a,b", list(itertools.combinations(AWKWARD, 2)))
def test_every_pair_of_awkward_pairs_is_kept_apart(tmp_path, a, b):
    ix = indexer(tmp_path)
    assert ix._store(*a).path != ix._store(*b).path and ix._store(*a) is not ix._store(*b)


@pytest.mark.parametrize("pair", PLAIN + AWKWARD)
def test_no_id_can_place_an_index_outside_the_directory(tmp_path, pair):
    ix = indexer(tmp_path)
    path = ix._store(*pair).path
    assert os.path.dirname(path) == os.path.abspath(ix.db_dir) and path.endswith(".duckdb")


@pytest.mark.parametrize("pair", PLAIN)
def test_ordinary_ids_keep_the_historical_file_name_so_existing_indexes_still_resolve(tmp_path, pair):
    ix = indexer(tmp_path)
    assert os.path.basename(ix._store(*pair).path) == f"{pair[0]}_{pair[1]}.duckdb"


def test_the_same_pair_gets_the_same_store_and_file_every_time(tmp_path):
    ix = indexer(tmp_path)
    assert ix._store("a_b", "c") is ix._store("a_b", "c")
    assert indexer(tmp_path)._store("a_b", "c").path == ix._store("a_b", "c").path        # stable across processes (a digest, not a counter)


@pytest.mark.parametrize("pair", [("", "x"), ("x", ""), ("  ", "x"), ("x", "\t"), (None, "x"), ("x", None), (1, "x")])
def test_a_missing_or_non_string_id_is_refused(tmp_path, pair):
    with pytest.raises(ValueError):
        indexer(tmp_path)._store(*pair)


def test_retrieval_for_one_project_never_reads_another_projects_store(tmp_path):
    ix = indexer(tmp_path)
    a = ix.retrieve("a_b", "c", "q")
    b = ix.retrieve("a", "b_c", "q")
    assert a.refs[0].ref != b.refs[0].ref                    # the filename the fake reports is its own file's path
    assert ix._store("a_b", "c").path != ix._store("a", "b_c").path


def test_the_containment_check_fires_if_the_naming_ever_breaks(tmp_path, monkeypatch):
    """Defence in depth: even if a future change to the naming produced a path with a separator, the index would still not be placed outside the directory."""
    import agentic_os.sources_rag as sr

    class Evil:
        def __init__(self, data):
            pass

        def hexdigest(self):
            return "/../../evil"
    monkeypatch.setattr(sr.hashlib, "sha256", lambda data: Evil(data))
    with pytest.raises(ValueError, match="escapes"):
        indexer(tmp_path)._store("a_b", "c")


def test_an_id_that_merely_contains_two_dots_stays_inside_the_directory_under_its_own_name(tmp_path):
    ix = indexer(tmp_path)
    assert os.path.basename(ix._store("a..b", "c.d").path) == "a..b_c.d.duckdb"
