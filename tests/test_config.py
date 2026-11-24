import pytest

from url_monitor.config import UrlListError, load_urls


@pytest.fixture
def url_file(tmp_path):
    """Return a function that writes `text` to a temporary file and returns its path."""

    def write(text):
        path = tmp_path / "urls.txt"
        path.write_text(text, encoding="utf-8")
        return path

    return write


def test_reads_one_url_per_line(url_file):
    path = url_file("https://example.com\nhttp://example.org/health\n")
    assert load_urls(path) == ["https://example.com", "http://example.org/health"]


def test_ignores_blank_lines_comments_and_surrounding_spaces(url_file):
    path = url_file("# production\n\n   https://example.com   \n  # indented comment\n")
    assert load_urls(path) == ["https://example.com"]


def test_keeps_fragment_because_hash_is_valid_in_a_url(url_file):
    path = url_file("https://example.com/page#section\n")
    assert load_urls(path) == ["https://example.com/page#section"]


def test_drops_duplicates_and_keeps_first_order(url_file):
    path = url_file("https://b.example\nhttps://a.example\nhttps://b.example\n")
    assert load_urls(path) == ["https://b.example", "https://a.example"]


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("ftp://files.example.com", "http:// or https://"),
        ("file:///etc/passwd", "http:// or https://"),
        ("example.com", "http:// or https://"),
        ("https://", "no host"),
        ("https://user:secret@example.com", "credentials"),
        ("https://example.com:99999", "invalid port"),
    ],
)
def test_rejects_invalid_url(url_file, line, reason):
    path = url_file(f"https://ok.example\n{line}\n")
    with pytest.raises(UrlListError, match=reason):
        load_urls(path)


def test_reports_every_invalid_line_at_once(url_file):
    path = url_file("ftp://a.example\nhttps://ok.example\nfile:///etc/passwd\n")
    with pytest.raises(UrlListError) as error:
        load_urls(path)
    message = str(error.value)
    assert "line 1" in message
    assert "line 3" in message
    assert "line 2" not in message


def test_error_message_does_not_leak_credentials(url_file):
    path = url_file("https://admin:secret@example.com\n")
    with pytest.raises(UrlListError) as error:
        load_urls(path)
    assert "secret" not in str(error.value)


def test_file_with_only_comments_is_an_error(url_file):
    path = url_file("# nothing yet\n\n")
    with pytest.raises(UrlListError, match="no URLs"):
        load_urls(path)


def test_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_urls(tmp_path / "missing.txt")
