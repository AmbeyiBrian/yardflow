"""P2, P3, §4.15.6 — reading a scanned label.

The vectors file is shared with the frontend's tests so the two readers cannot drift.
"""

import json
from pathlib import Path

import pytest

from stock.labels import LabelReading, read_label

VECTORS = json.loads((Path(__file__).parent / "data" / "label_vectors.json").read_text("utf-8"))


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_label_vector(vector):
    reading = read_label(vector["raw"])

    assert list(reading.serials) == vector["serials"]
    assert reading.box_code == vector["box_code"]
    assert reading.document_token == vector["document_token"]
    assert reading.raw == vector["raw"]


def test_the_vectors_cover_every_rule():
    assert len(VECTORS) >= 25
    assert any(v["document_token"] for v in VECTORS)
    assert any(v["box_code"] for v in VECTORS)


def test_a_reading_is_immutable():
    reading = read_label("A1")

    assert isinstance(reading, LabelReading)
    with pytest.raises(AttributeError):
        reading.box_code = "x"  # type: ignore[misc]


def test_none_reads_as_nothing():
    assert read_label(None).serials == ()  # type: ignore[arg-type]


def test_a_non_empty_label_is_never_discarded():
    for raw in ("{", "[]", "(21)", "]C1", "http://", "SN:", ",", "?"):
        reading = read_label(raw)
        assert reading.serials or reading.box_code or reading.document_token, raw
