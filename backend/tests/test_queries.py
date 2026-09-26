import unittest

from app.providers.booru import BooruProvider
from app.services.queries import humanize_artist_query, provider_query_for_folder


class ArtistFolderQueryTests(unittest.TestCase):
    def test_plain_spaced_name_becomes_one_provider_artist_tag(self):
        query = provider_query_for_folder("namako daibakuhatsu", "artist")
        self.assertEqual(query, "artist:namako daibakuhatsu")
        self.assertEqual(
            BooruProvider._normalize_query(query),
            ["artist:namako_daibakuhatsu"],
        )

    def test_filters_stay_separate_from_spaced_artist_name(self):
        query = provider_query_for_folder(
            "rating:safe namako daibakuhatsu -comic", "artist"
        )
        self.assertEqual(query, "artist:namako daibakuhatsu rating:safe -comic")
        self.assertEqual(
            BooruProvider._normalize_query(query),
            ["artist:namako_daibakuhatsu", "rating:safe", "-comic"],
        )

    def test_underscored_input_is_stored_as_readable_text(self):
        self.assertEqual(
            humanize_artist_query("namako_daibakuhatsu"),
            "namako daibakuhatsu",
        )

    def test_provider_url_is_not_modified(self):
        url = "https://x.com/an_artist/media"
        self.assertEqual(humanize_artist_query(url), url)
        self.assertEqual(provider_query_for_folder(url, "artist"), url)


if __name__ == "__main__":
    unittest.main()
