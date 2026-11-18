"""The vocabulary corpus, and what the rail resolves to from a cursor position."""

from __future__ import annotations

import pytest
from rich.markup import escape

from icelens import model, terms
from icelens.app import PANES
from icelens.catalog import Lake
from icelens.model import View


def test_every_pane_has_a_fallback_term() -> None:
    """The rail is never empty, so every pane - including SQL - must resolve to something."""
    for pane, _label, _key in PANES:
        assert terms.resolve(pane) is not None, pane
    assert set(model.PANES) <= set(terms.PANE_TERMS)


def test_every_reference_resolves() -> None:
    """A `see` trail that names a term nobody wrote is a dead end printed on screen."""
    for key, term in terms.TERMS.items():
        assert term.see, f"{key} points nowhere"
        for other in term.see:
            assert other in terms.TERMS, f"{key} -> {other}"
            assert other != key, f"{key} points at itself"
    tables = [
        terms.PANE_TERMS.values(),
        terms.TREE_TERMS.values(),
        terms.COMMON_COLUMNS.values(),
        terms.COLUMN_TERMS.values(),
        (key for _, key in terms.ROW_TERMS),
        (key for _, key in terms.VALUE_TERMS),
    ]
    for table in tables:
        for key in table:
            assert key in terms.TERMS, key


def test_a_gloss_stands_alone() -> None:
    """One line, and not one that opens by repeating the word it is there to define."""
    for key, term in terms.TERMS.items():
        assert "\n" not in term.gloss
        assert 20 < len(term.gloss) <= 110, f"{key}: {len(term.gloss)}"
        first_word = term.name.split()[0].rstrip(",").lower()
        assert not term.gloss.lower().startswith(first_word), f"{key} defines itself with itself"


@pytest.mark.parametrize(
    ("pane", "column", "row_label", "value", "expected"),
    [
        # the cell's value wins over the column it sits in
        ("files", "content", "equality deletes", "equality deletes", "Equality delete"),
        ("files", "content", "data", "data", "File content"),
        ("snapshots", "operation", "1", "append", "append"),
        ("snapshots", "operation", "1", "replace", "Compaction (replace)"),
        ("commit", "status", "ADDED", "ADDED", "Entry status"),
        # a row-keyed pane resolves on the row label
        ("overview", "property", "sort order", "id asc", "Sort order"),
        ("overview", "property", "format version", "3", "Format version"),
        ("overview", "property", "ref main", "branch -> 1", "Branch / tag (ref)"),
        ("layout", "node", "  manifest list", "", "Manifest list"),
        # otherwise the column
        ("manifests", "seq", "0", "7", "Sequence number"),
        ("schema", "id", "1", "1", "Field id"),
        ("schema", "key", "", "identifier", "Identifier field"),
        ("partitions", "equality_delete_file_count", "", "0", "Equality delete"),
        # and failing everything, the pane itself
        ("snapshots", "nonexistent column", None, None, "Snapshot"),
        ("sql", None, None, None, "Read route"),
    ],
)
def test_resolution_order(pane: str, column: str | None, row_label: str | None, value: str | None, expected: str) -> None:
    term = terms.resolve(pane, column, row_label, value)
    assert term is not None and term.name == expected


def test_search_prefers_an_exact_name() -> None:
    assert [t.name for t in terms.search("snapshot")] == ["Snapshot"]
    assert terms.search("manifest")[0].name.startswith("Manifest")
    assert terms.search("") == []
    assert terms.search("kafka") == []


#: columns of a real pane that deliberately resolve to nothing more specific than the pane
#: itself. Every other column must have its own term, and a column added to a pane without one
#: fails `test_every_real_column_is_explained` - which is the whole point of pinning this list.
UNEXPLAINED: dict[str, set[str]] = {
    # the Data pane shows the user's own columns, which Iceberg has no vocabulary for
    "data": {"id", "name", "ingest_date", "measured_at"},
    "overview": {"property", "value"},  # a key/value pane: the *row* names the term, not the column
    "manifests": {"manifest"},
    "files": {"file"},
    "partitions": set(),
    "schema": set(),
    "snapshots": set(),
    "commit": set(),
    "layout": set(),
}


def _pane_data(lake: Lake, pane: str):
    return model.PANES[pane](View(lake, "demo.events", limit=5)).data


def test_every_real_column_is_explained(lake: Lake) -> None:
    """Run every pane against a real table and resolve every column it actually produced.

    The corpus is written against the panes, so it drifts the moment a pane grows a column.
    This reads the columns from the panes themselves rather than from a list in a test.
    """
    for pane in model.PANES:
        fallback = terms.TERMS[terms.PANE_TERMS[pane]]
        data = _pane_data(lake, pane)
        fell_back = set()
        for column in data.column_names:
            term = terms.resolve(pane, column)
            assert term is not None, f"{pane}.{column} resolves to nothing at all"
            if term is fallback and (pane, column) not in terms.COLUMN_TERMS:
                fell_back.add(column)
        assert fell_back == UNEXPLAINED[pane], (
            f"{pane}: columns explained only by the pane itself changed - "
            f"{sorted(fell_back)} vs the pinned {sorted(UNEXPLAINED[pane])}"
        )


def test_the_key_value_panes_explain_their_rows(lake: Lake) -> None:
    """Overview and Layout are read down the first column, so the row has to carry the term."""
    for pane in sorted(terms.ROW_KEYED):
        data = _pane_data(lake, pane)
        labels = [data.column(0)[i].as_py() for i in range(data.num_rows)]
        resolved = [terms.resolve(pane, None, label) for label in labels]
        assert all(term is not None for term in resolved)
        # a pane where every row says the same thing is not explaining the rows
        assert len({term.name for term in resolved}) >= 4, f"{pane}: {labels}"


def test_the_deep_entry_is_valid_markup() -> None:
    """The rail renders a term through Textual's markup parser; a stray bracket would crash it."""
    from textual.content import Content

    for term in terms.TERMS.values():
        for text in (term.name, term.gloss, term.body, term.where):
            rendered = Content.from_markup(f"[b]{escape(text)}[/b]")
            assert text.strip() in rendered.plain


def test_the_columns_a_newcomer_misreads_have_their_own_term() -> None:
    """Five columns where the pane's own term is true but useless, so each got its own.

    `type` is the clearest case: a reader who knows SQL types reads `long` and moves on, and
    the thing they needed to be told is that only widening changes are legal in place.
    """
    assert terms.resolve("schema", "type").name == "Data type"
    assert "widening" in terms.resolve("schema", "type").body
    assert terms.resolve("schema", "column").name == "Column name"
    assert terms.resolve("snapshots", "committed at").name == "Commit time"
    assert terms.resolve("partitions", "last_updated_at").name == "Commit time"
    assert terms.resolve("files", "format").name == "File format"
    # the Data pane shows the user's own columns, so its fallback has to explain the read
    assert terms.resolve("data", "whatever_they_called_it").name == "Reading a table (scan)"


def test_commit_time_says_it_is_not_the_ordering() -> None:
    """The trap this term exists for: wall clock is not monotonic, sequence numbers are."""
    term = terms.resolve("snapshots", "committed at")
    assert "sequence number" in term.body.lower()
    assert "sequence-number" in term.see
