import modl


def test_modl():
    """Package exposes a non-empty __version__ string."""
    assert isinstance(modl.__version__, str)
    assert modl.__version__ != ""
