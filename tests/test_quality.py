from availarr.server.quality import FileInfo, Requirement, satisfies, file_tags
REQ = Requirement(["1080p MULTi"], 1080, ["French"], "MULTi.1080p.WEB-DL")
def f(res, langs, q=None): return FileInfo(res, q or f"Bluray-{res}p", langs, "/m/x.mkv", 1)

def test_satisfies():
    assert satisfies(f(1080, ["French", "English"]), REQ)
    assert satisfies(f(2160, ["French"]), REQ)
    assert not satisfies(f(720, ["French"]), REQ)
    assert not satisfies(f(1080, ["English"]), REQ)
    assert not satisfies(None, REQ)

def test_file_tags():
    assert file_tags(f(1080, ["French", "English"])) == "MULTi.1080p.BluRay"
    assert file_tags(f(720, ["English"], "WEBDL-720p")) == "720p.WEB-DL"
    assert file_tags(f(1080, ["French"], "WEBDL-1080p")) == "FRENCH.1080p.WEB-DL"


def test_original_title():
    from availarr.server.quality import FileInfo, Requirement, original_title
    fr = Requirement(["x"], 1080, ["French"], "MULTi.1080p")
    anyl = Requirement(["x"], 1080, [], "1080p")

    def f(path, scene=None, res=1080):
        return FileInfo(res, "Bluray-1080p", ["French", "English"], path, 1, scene)
    assert original_title(f("/m/Thor.The.Dark.World.2013.MULTi.1080p.BluRay.x264-GRP.mkv"), fr) == \
        "Thor.The.Dark.World.2013.MULTi.1080p.BluRay.x264-GRP"
    assert original_title(f("/m/whatever.mkv", "Thor.2013.TRUEFRENCH.1080p.WEB-DL-X"), fr) == "Thor.2013.TRUEFRENCH.1080p.WEB-DL-X"
    assert original_title(f("/m/Thor (2013) Bluray-1080p.mkv"), anyl) == "Thor (2013) Bluray-1080p"
    assert original_title(f("/m/Thor (2013) Bluray-1080p.mkv"), fr) is None      # no language tag: B would see English
    assert original_title(f("/m/Thor.2013.MULTi.BluRay.mkv"), anyl) is None      # no resolution tag
    assert original_title(f("/m/Thor.2013.MULTi.4K.mkv", res=2160), fr) == "Thor.2013.MULTi.4K"
    assert original_title(f("/m/x.mkv", 'bad"name.1080p'), anyl) is None
