from oex.palette import DEFAULT_PALETTE


def test_the_default_palette_gives_every_layer_its_own_colour_until_it_cycles():
    lowered = [colour.lower() for colour in DEFAULT_PALETTE]
    assert len(set(lowered)) == len(lowered)
