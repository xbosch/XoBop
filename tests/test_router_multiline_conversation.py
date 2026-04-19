from xobop.router import is_multiline_conversation


def test_multiline_general_text_routes_to_conversation():
    text = "Hi\nSummarize this\nTranslate to Catalan"
    assert is_multiline_conversation(text) is True


def test_multiline_email_first_line_not_conversation():
    text = "send email to neri1982@gmail.com\nsubject: hi\nbody: test"
    assert is_multiline_conversation(text) is False
