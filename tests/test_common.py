import pytest
from availarr.common import Item, build_nzb, parse_nzb, release_title

def test_item_key_roundtrip():
    for it in [Item("movie", 603), Item("season", 81189, 2), Item("episode", 81189, 2, 5)]:
        assert Item.from_key(it.key()) == it
    assert Item("episode", 81189, 2, 5).key() == "episode:81189:2:5"

def test_nzb_roundtrip():
    data = build_nzb({"item": "movie:603", "title": "X"})
    assert data.startswith(b"<?xml") and b"<nzb" in data
    assert parse_nzb(data) == {"item": "movie:603", "title": "X"}

def test_parse_nzb_rejects_foreign():
    with pytest.raises(ValueError):
        parse_nzb(b"<?xml version='1.0'?><nzb xmlns='http://www.newzbin.com/DTD/2003/nzb'></nzb>")

def test_release_title():
    assert release_title("Dune: Part Two", 2024, Item("movie", 693134), "MULTi.1080p.BluRay") == "Dune.Part.Two.2024.MULTi.1080p.BluRay-Availarr"
    assert release_title("The Bear", 2022, Item("season", 1, 3), "1080p.WEB-DL") == "The.Bear.S03.1080p.WEB-DL-Availarr"
    assert release_title("The Bear", 2022, Item("episode", 1, 3, 7), "1080p.WEB-DL") == "The.Bear.S03E07.1080p.WEB-DL-Availarr"
    assert release_title("Amélie", 2001, Item("movie", 194), "FRENCH.1080p.BluRay") == "Amélie.2001.FRENCH.1080p.BluRay-Availarr"
