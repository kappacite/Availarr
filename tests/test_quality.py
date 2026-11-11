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
