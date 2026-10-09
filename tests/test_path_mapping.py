from trackseerr.path_mapping import apply_mapping, normalize_key, suggest_mapping


def _lib(root, n=10):
    return [f"{root}/Artist {i}/Album {i}/{j:02d} Track.flac" for i in range(n) for j in range(1, 3)]


def test_suggest_mapping_happy_path():
    server = _lib("/data/music")
    local = _lib("/music")
    assert suggest_mapping(server, local) == ("/data/music", "/music")


def test_suggest_mapping_no_overlap_is_none():
    assert suggest_mapping(_lib("/data/music"), ["/other/x/y/z.flac", "/other/q/r/s.flac"]) is None


def test_suggest_mapping_empty_inputs():
    assert suggest_mapping([], ["/a/b/c.flac"]) is None
    assert suggest_mapping(["/a/b/c.flac"], []) is None


def test_suggest_mapping_under_half_is_none():
    server = _lib("/data/music", 10)  # 20 files
    local = _lib("/music", 10)[:5] + [f"/music/Zed/Zed/{i}.flac" for i in range(40)]
    assert suggest_mapping(server, local) is None


def test_suggest_mapping_unicode_and_case():
    server = ["/Data/Music/Björk/Homogenic/01 Hunter.flac", "/Data/Music/Björk/Homogenic/02 Jóga.flac",
              "/Data/Music/Sade/Lovers Rock/01 By Your Side.flac"]
    local = ["/music/BJÖRK/homogenic/01 hunter.flac", "/music/BJÖRK/homogenic/02 jóga.flac",
             "/music/sade/lovers rock/01 by your side.flac"]
    local = [__import__("unicodedata").normalize("NFD", p) for p in local]
    assert suggest_mapping(server, local) == ("/Data/Music", "/music")


def test_apply_mapping_absolute_component_wise():
    m = ("/data/music", "/music")
    assert apply_mapping("/data/music/A/B/c.flac", m, relative=False, music_root="/music") == "/music/A/B/c.flac"
    assert apply_mapping("/DATA/Music/A/c.flac", m, relative=False, music_root="") == "/music/A/c.flac"
    # string-prefix lookalike must not be rewritten
    assert apply_mapping("/data/music2/A/c.flac", m, relative=False, music_root="") == "/data/music2/A/c.flac"


def test_apply_mapping_no_mapping_unchanged():
    assert apply_mapping("/x/y.flac", None, relative=False, music_root="/music") == "/x/y.flac"


def test_apply_mapping_relative_joins_root():
    assert apply_mapping("A/B/c.flac", None, relative=True, music_root="/music/") == "/music/A/B/c.flac"
    assert apply_mapping("/A/c.flac", ("/a", "/b"), relative=True, music_root="/music") == "/music/A/c.flac"


def test_normalize_key():
    assert normalize_key("C:\\Music\\Bj\u00f6rk\\X.FLAC") == normalize_key("c:/music/Bjo\u0308rk/x.flac")


def test_suggest_mapping_returns_library_root_not_deepest_prefix():
    server = [f"/data/music/Art/Alb{i}/{j:02d}.flac" for i in range(5) for j in range(1, 3)]
    local = [p.replace("/data/music", "/music") for p in server]
    assert suggest_mapping(server, local) == ("/data/music", "/music")


def test_suggest_mapping_widens_when_every_file_is_under_one_artist():
    server = [f"/data/music/Artist 0/Album {i}/{j:02d}.flac" for i in range(4) for j in range(1, 3)]
    local = [p.replace("/data/music", "/music") for p in server]
    assert suggest_mapping(server, local) == ("/data/music", "/music")


def test_suggest_mapping_keeps_distinct_trailing_components():
    server = [f"/data/music/Art/Alb{i}/{j}.flac" for i in range(3) for j in range(2)]
    local = [p.replace("/data/music", "/lib/all") for p in server]
    assert suggest_mapping(server, local) == ("/data/music", "/lib/all")
