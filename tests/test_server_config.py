import re
from pathlib import Path
import pytest
from availarr.server.config import load_server_config

EX = Path(__file__).parent.parent / "examples" / "server.toml"

def write(tmp_path, text):
    p = tmp_path / "s.toml"
    p.write_text(text)
    return str(p)

def test_load_example(tmp_path):
    cfg = load_server_config(write(tmp_path, EX.read_text()))
    assert (cfg.keep_days, cfg.seed_days, cfg.fail_days) == (7, 3, 7)
    assert cfg.profiles["HD-1080p FR"].min_resolution == 1080
    assert cfg.radarr_root == "/movies"

def test_defaults_without_retention(tmp_path):
    text = re.sub(r"\[retention\].*?(?=\[sizes\])", "", EX.read_text(), flags=re.S)
    cfg = load_server_config(write(tmp_path, text))
    assert (cfg.keep_days, cfg.seed_days, cfg.fail_days) == (7, 3, 7)

def test_missing_token(tmp_path):
    text = EX.read_text().replace('token = "change-me"\n', "", 1)
    with pytest.raises(ValueError, match="token"):
        load_server_config(write(tmp_path, text))
