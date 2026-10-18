"""Fixtures for the dictation cleanup filter (conservative-filler-filter-v1, rules v2)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup_filter import clean_text  # noqa: E402


def on(text, terms=(), rep=True):
    return clean_text(text, enabled=True, protected_terms=list(terms), remove_repetitions=rep)


class Fillers(unittest.TestCase):
    def test_js_reference_removals(self):
        for a, b in [("um I can help", "I can help"), ("I uh think so", "I think so"),
                     ("um, hello uh there", "hello there"), ("10 um tickets", "10 tickets")]:
            self.assertEqual(on(a), b)

    def test_app_recorded_self_test(self):
        # The app's own self-test shape: a name, a number and a time survive.
        self.assertEqual(on("Um, Dana needs 42 at 10:30."), "Dana needs 42 at 10:30.")

    def test_parakeet_capitalised_sentence_fillers(self):
        # Parakeet writes 'Um I ...' without a comma.
        self.assertEqual(on("Dana waters the ferns herself. Um I think she has a hose."),
                         "Dana waters the ferns herself. I think she has a hose.")
        self.assertEqual(on("Soup's on. Um I'm setting the table."), "Soup's on. I'm setting the table.")
        self.assertEqual(on("the pantry has flour. Uh so I guess there's plenty."),
                         "the pantry has flour. So I guess there's plenty.")
        self.assertEqual(on("The basil looks pale. Um check the soil today."),
                         "The basil looks pale. Check the soil today.")

    def test_sentence_start_comma_and_capital(self):
        self.assertEqual(on("Um, so I think we should go."), "So I think we should go.")
        self.assertEqual(on("Uh, uh, so I"), "So I")
        self.assertEqual(on("Erm, hmm, I guess er we could"), "I guess we could")
        # Only a capitalised filler passes its capital on.
        self.assertEqual(on("um so lowercase"), "so lowercase")
        # Names, mixed case and numbers are never recased.
        self.assertEqual(on("Um, iPhone is nice"), "iPhone is nice")
        self.assertEqual(on("Um, 42 people"), "42 people")

    def test_terminal_and_standalone(self):
        self.assertEqual(on("I think um."), "I think.")
        self.assertEqual(on("I think, um."), "I think.")
        self.assertEqual(on("well um?"), "well?")
        self.assertEqual(on("the bread was warm, which is nice. Um"), "the bread was warm, which is nice.")
        self.assertEqual(on("That's good. Um. Let's go."), "That's good. Let's go.")
        self.assertEqual(on("I think um"), "I think")
        self.assertEqual(on("um\nnext"), "um\nnext")  # filler-only line, nothing else changed

    def test_commas_around_fillers(self):
        self.assertEqual(on("so, um, I think"), "so, I think")
        self.assertEqual(on("we should, uh, ship it"), "we should ship it")
        self.assertEqual(on("Done. Then we should, um, go"), "Done. Then we should go")
        self.assertEqual(on("I need eggs, bread, um, milk"), "I need eggs, bread, milk")
        self.assertEqual(on("Erm, let's, um, check the logs"), "Let's check the logs")
        self.assertEqual(on("Yeah, um, that works"), "Yeah, that works")

    def test_parakeet_all_caps_filler(self):
        # Parakeet writes some fillers in capitals.
        self.assertEqual(on("the soup yet. ERM, let's, um, taste the broth first, and uh, then we'll see."),
                         "the soup yet. Let's taste the broth first, and then we'll see.")
        self.assertEqual(on("HMM, okay UMM fine"), "Okay fine")
        self.assertEqual(on("so um, I think"), "so I think")
        self.assertEqual(on("so, um I think"), "so, I think")
        self.assertEqual(on("this spring, uh mostly"), "this spring, mostly")
        self.assertEqual(on("yes; uh; fine"), "yes; fine")
        self.assertEqual(on("wait uh... what"), "wait what")
        self.assertEqual(on("see (um the notes)"), "see (the notes)")

    def test_names_acronyms_unicode_kept(self):
        for a in ["UH is a name", "sodium uhx umber", "αum βuh", "UM, the union", "ER visit", "uMm odd",
                  "uh-huh sure", "huh-uh no", "um-hum yes", "I err on the side", "the ER was busy"]:
            self.assertEqual(on(a), a)

    def test_real_words_kept(self):
        for a in ["like I said, you know, it's fine", "so ah, oh well", "ahh I see"]:
            self.assertEqual(on(a), a)

    def test_contractions_and_apostrophes_do_not_block(self):
        self.assertEqual(on("It's um working"), "It's working")
        self.assertEqual(on("Dana didn't pick uh the peppers"), "Dana didn't pick the peppers")
        self.assertEqual(on("You can mail the jars to 'em. Um check that um label is dry."),
                         "You can mail the jars to 'em. Check that label is dry.")
        self.assertEqual(on("um the students' work"), "the students' work")
        self.assertEqual(on("it’s uh fine"), "it’s fine")

    def test_variants_and_runs(self):
        self.assertEqual(on("so umm we uhh go"), "so we go")
        self.assertEqual(on("so ummm we uhhh go errm now hmmm ok"), "so we go now ok")
        self.assertEqual(on("maybe it's uh uh a muffin"), "maybe it's a muffin")

    def test_plural_ums_kept(self):
        self.assertEqual(on("Dana's podcast keeps the ums yeah"), "Dana's podcast keeps the ums yeah")
        self.assertEqual(on("her ums and uhs aren't edited out"), "her ums and uhs aren't edited out")


class ReviewFindings(unittest.TestCase):
    """Independent review, Oct 12: none of these may lose a real word."""

    def test_talk_about_fillers_is_kept(self):
        for a in ["Words like um, uh and er are fillers.", "Don't say um or uh.", "The word is um.",
                  "Filler words such as um should go.", "Avoid fillers like um and uh in talks."]:
            self.assertEqual(on(a), a)
        # Only the sentence that talks about them is protected.
        self.assertEqual(on("Uh, the word is um. Um so next."), "Uh, the word is um. So next.")
        for a in ["Remove every um and uh from the transcript.", "Is it um or er?"]:
            self.assertEqual(on(a), a)

    def test_lists_and_appositives_keep_their_commas(self):
        self.assertEqual(on("I need eggs, um, milk, and bread."), "I need eggs, milk, and bread.")
        self.assertEqual(on("I like red, uh, blue, and green."), "I like red, blue, and green.")
        self.assertEqual(on("My brother, uh, John, is here."), "My brother, John, is here.")

    def test_meaningful_interjections_kept(self):
        for a in ["Uh huh, that's right.", "Uh oh, the build broke.", "Mm hmm, that's right.",
                  "Um hum, sure.", "and uh oh it broke"]:
            self.assertEqual(on(a), a)

    def test_capitalised_name_mid_sentence_kept(self):
        for a in ["I went to see Um Kalthoum last night.", "I live in Um Qasr.", "Ask Um about it."]:
            self.assertEqual(on(a), a)
        self.assertEqual(on("and then, Um, we left"), "and then we left")
        self.assertEqual(on("Uh, Um, so we left"), "So we left")

    def test_brackets_and_unicode_capitals(self):
        self.assertEqual(on("I need (um) the thing"), "I need the thing")
        self.assertEqual(on("I need (um)."), "I need.")
        self.assertEqual(on("(um) hello"), "hello")
        self.assertEqual(on("Um, ßtraße"), "ßtraße")
        self.assertEqual(on("Um, élan matters"), "Élan matters")
        self.assertEqual(on("Um, eBay works"), "eBay works")


class Repetition(unittest.TestCase):
    def test_function_words(self):
        self.assertEqual(on("Really? I I can't taste it"), "Really? I can't taste it")
        self.assertEqual(on("water the the tulips"), "water the tulips")
        self.assertEqual(on("The the plan"), "The plan")
        self.assertEqual(on("go to to to the store"), "go to the store")

    def test_grammatical_emphatic_names_numbers_kept(self):
        for a in ["I think that that was it", "she had had enough", "very very good",
                  "tea or or coffee?", "we baked we baked bread", "Bob Bob", "10 10", "the The end",
                  "the, the end"]:
            self.assertEqual(on(a), a)

    def test_repetition_option(self):
        self.assertEqual(on("the the end", rep=False), "the the end")
        self.assertEqual(on("um the the end", rep=False), "the the end")

    def test_filler_then_repeat_is_idempotent(self):
        for a, b in [("the uh the end", "the end"), ("I uh I think", "I think")]:
            out = on(a)
            self.assertEqual(out, b)
            self.assertEqual(on(out), out)


class Literals(unittest.TestCase):
    def test_wording_cues_keep_the_filler(self):
        for a in ['Say "um hello"', "`uh` is a variable", "The word um means hesitation",
                  "a literal um marker", "don't say um so much", "it's spelled erm"]:
            self.assertEqual(on(a), a)

    def test_cue_words_elsewhere_no_longer_block(self):
        # v1 skipped any line containing code/say/saying/named/called.
        self.assertEqual(on("print the zip code um labels"), "print the zip code labels")
        self.assertEqual(on("same as always for a door code and um yeah punch that in uh because"),
                         "same as always for a door code and yeah punch that in because")
        self.assertEqual(on("I'm saying that my uh lasagna is good"),
                         "I'm saying that my lasagna is good")
        self.assertEqual(on("what the new puppy will be named. Um for um the vet"),
                         "what the new puppy will be named. For the vet")

    def test_quoted_spans_are_byte_exact(self):
        self.assertEqual(on('um he wrote "uh  the the" then uh left'),
                         'he wrote "uh  the the" then left')
        self.assertEqual(on("uh “um  ok” uh fine"), "“um  ok” fine")
        self.assertEqual(on("so uh 'um the the' ok"), "so 'um the the' ok")

    def test_unbalanced_quotes_protect_everything(self):
        for a in ['um he said "hello', "uh ‘odd", "um `tick"]:
            self.assertEqual(on(a), a)

    def test_fenced_code_complete_span(self):
        text = "um first\n```\nuh the the code\n```\nuh last line"
        self.assertEqual(on(text), "first\n```\nuh the the code\n```\nlast line")
        unterminated = "um first\n```\nuh x"
        self.assertEqual(on(unterminated), "first\n```\nuh x")

    def test_code_symbols_keep_line(self):
        for a in ["uh x = 1", "um call snake_case", "uh open {a}", "um a <b> c"]:
            self.assertEqual(on(a), a)
        # Prose punctuation no longer blocks cleanup.
        self.assertEqual(on("um open foo/bar"), "open foo/bar")
        self.assertEqual(on("um email me@example.com"), "email me@example.com")
        for a in ["go to example.com/um now", "mail um@example.com", "open um.txt", "tag #uh"]:
            self.assertEqual(on(a), a)

    def test_transport_markup_kept(self):
        a = "um <transcript> uh"
        self.assertEqual(on(a), a)


class Options(unittest.TestCase):
    def test_disabled_or_malformed(self):
        self.assertEqual(clean_text("um hello", enabled=False), "um hello")
        self.assertEqual(clean_text("um hello"), "um hello")
        self.assertEqual(clean_text("um hello", enabled="true"), "um hello")
        self.assertEqual(clean_text("um hello", enabled=True, protected_terms=[3]), "um hello")
        self.assertIsNone(clean_text(None, enabled=True))

    def test_protected_terms_disable_mutation(self):
        self.assertEqual(on("um contact Dr Smith", ["Dr Smith"]), "um contact Dr Smith")
        self.assertEqual(on("um contact dr smith", ["Dr Smith"]), "um contact dr smith")
        self.assertEqual(on("um contact Dr Jones", ["Dr Smith", "  "]), "contact Dr Jones")

    def test_filler_only_returns_original(self):
        # Nothing but punctuation would remain: keep the original (the app
        # treats an empty cleanup reply as a failure).
        for a in ["um", "Um.", "um, uh, um", "um ,", "uh ..."]:
            self.assertEqual(on(a), a)

    def test_whitespace_preserved(self):
        self.assertEqual(on("  um hello  "), "  hello  ")
        self.assertEqual(on("a\tum b"), "a b")
        self.assertEqual(on("one\n\num two"), "one\n\ntwo")

    def test_idempotent(self):
        for a in ["um hello uh there", "I uh agree", "αum βuh", "Uh um says yes",
                  "um, uh, the the I I go", 'uh "um" uh ok', "Um, so. Uh. Fine um.",
                  "say um or uh"]:
            b = on(a)
            self.assertEqual(on(b), b, a)


if __name__ == "__main__":
    unittest.main()
