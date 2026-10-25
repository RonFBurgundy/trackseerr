from trackseerr.media_match import artist_credits, artist_matches, norm_name, title_rank


def test_norm_name_dollar_and_the():
    assert norm_name("Ke$ha") == "kesha"
    assert norm_name("Kesha") == "kesha"
    assert norm_name("Ke$ha") == norm_name("Kesha")
    assert norm_name("The Black Eyed Peas") == norm_name("Black Eyed Peas")
    assert norm_name("Beyoncé") == "beyonce"


def test_artist_credits_split():
    assert artist_credits("Argy;Alok;Adam Beyer") == ["argy", "alok", "adam beyer"]
    credits = artist_credits("Wu-Tang Clan, Michael Bibi, Kettama")
    assert "michael bibi" in credits
    assert artist_credits("A feat. B") == ["a", "b"]
    assert artist_credits("Artist A ft. Artist B") == ["artist a", "artist b"]
    assert artist_credits("Artist A with Artist B") == ["artist a", "artist b"]
    assert artist_credits("Artist A x Artist B") == ["artist a", "artist b"]
    assert artist_credits("Artist A & Artist B") == ["artist a", "artist b"]
    assert artist_credits("Artist A / Artist B") == ["artist a", "artist b"]


def test_artist_matches_cases():
    # 5 live examples from handoff:
    # 1. "Kesha" -> "Ke$ha"
    assert artist_matches("Ke$ha", "Kesha", threshold=0.9)
    # 2. "Black Eyed Peas" -> "The Black Eyed Peas"
    assert artist_matches("The Black Eyed Peas", "Black Eyed Peas", threshold=0.9)
    # 3. "Argy" -> "Argy;Alok;Adam Beyer"
    assert artist_matches("Argy;Alok;Adam Beyer", "Argy", threshold=0.9)
    # 4. "Michael Bibi" -> "Wu-Tang Clan, Michael Bibi, Kettama"
    assert artist_matches("Wu-Tang Clan, Michael Bibi, Kettama", "Michael Bibi", threshold=0.9)
    # 5. "Jimi Jules" -> "Jimi Jules, Black Coffee"
    assert artist_matches("Jimi Jules, Black Coffee", "Jimi Jules", threshold=0.9)

    # Negative cases
    assert not artist_matches("HappyHappy", "Kesha", threshold=0.9)
    assert not artist_matches("Lemaitre", "The Chainsmokers", threshold=0.9)
    # Empty cases
    assert not artist_matches("", "Kesha", threshold=0.9)
    assert not artist_matches("Kesha", "", threshold=0.9)


def test_title_rank():
    # exact 0
    assert title_rank("TiK ToK", "TiK ToK") == 0
    assert title_rank("tik tok", "TiK ToK") == 0
    # Gangsta's Paradise (feat. L.V.) vs Gangsta's Paradise -> 1
    assert title_rank("Gangsta's Paradise (feat. L.V.)", "Gangsta's Paradise") == 1
    assert title_rank("Gangsta's Paradise", "Gangsta's Paradise (feat. L.V.)") == 1
    # remix vs plain -> 2
    assert title_rank("TiK ToK (Fred Falke club remix)", "TiK ToK") == 2
