import unittest

from app.services.caption_check import check_caption

FACTS = {'air groove (umamusume)': {'name': 'Air Groove', 'qualifiers': ['umamusume'], 'series': ['umamusume']}}
TAGS = ['Drawn by vent arbre', 'air groove (umamusume)', 'umamusume', '1girl', 'animal ears', 'horse ears']


def caption(body: str, words: int = 220) -> str:
    filler = ' '.join(['detail'] * max(0, words - len(body.split())))
    return f'{body} {filler}.'


class CaptionCheckTests(unittest.TestCase):
    def check(self, text, tags=TAGS):
        return check_caption(text, tags, FACTS, 200, 350)

    def test_a_good_caption_passes(self):
        good = caption('Drawn by vent arbre. Air Groove from Umamusume hugs herself in a fur-lined brown coat, her horse ears drooping.')
        self.assertEqual(self.check(good), [])

    def test_artist_trigger_must_open_the_caption_once(self):
        self.assertIn('artist_start', self.check(caption('Air Groove from Umamusume stands. Drawn by vent arbre.')))
        self.assertIn('double_drawn_by', self.check(caption('Drawn by drawn by vent arbre. Air Groove stands.')))

    def test_length_paragraphs_hedging_markup_and_meta_phrases(self):
        self.assertIn('too_short', self.check('Drawn by vent arbre. Air Groove stands.'))
        self.assertIn('too_long', self.check(caption('Drawn by vent arbre. Air Groove stands.', words=400)))
        self.assertIn('line_break', self.check(caption('Drawn by vent arbre. Air Groove stands.\nShe smiles.')))
        self.assertIn('hedging', self.check(caption('Drawn by vent arbre. Air Groove wears shorts or leggings.')))
        self.assertNotIn('hedging', self.check(caption('Drawn by vent arbre. Air Groove holds a sign reading "now or never".')))
        self.assertIn('markup', self.check(caption('Drawn by vent arbre. **Air Groove** stands.')))
        self.assertIn('meta_phrase', self.check(caption('Drawn by vent arbre. Air Groove stands. In this image she smiles.')))

    def test_characters_must_be_named_without_raw_tag_spelling(self):
        self.assertIn('missing_character', self.check(caption('Drawn by vent arbre. A horse girl in a brown coat stands.')))
        self.assertIn('raw_tag', self.check(caption('Drawn by vent arbre. Air Groove (umamusume) stands.')))
        self.assertIn('raw_tag', self.check(caption('Drawn by vent arbre. Air Groove wears a fur_coat.')))

    def test_text_tags_need_quoted_text(self):
        tags = TAGS + ['speech bubble', 'english text']
        self.assertIn('missing_text', self.check(caption('Drawn by vent arbre. Air Groove shouts in a speech bubble.'), tags))
        self.assertNotIn('missing_text', self.check(caption('Drawn by vent arbre. Air Groove shouts "LET\'S GO!" in a speech bubble.'), tags))

    def test_refusals_and_prompt_echoes(self):
        self.assertIn('refusal', self.check(caption("I'm sorry, but I can't help with describing this image.")))
        self.assertIn('echo', self.check(caption('Drawn by vent arbre. <tags> Air Groove </tags>')))


if __name__ == '__main__':
    unittest.main()
