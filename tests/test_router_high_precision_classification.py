from xobop.router import classify


def test_email_send_requires_high_confidence_phrase():
    assert classify("send email to a@b.com")["name"] == "email_send"
    assert classify("please send an email to wife")["name"] == "email_send"
    assert classify("can you send this later maybe")["name"] != "email_send"
    assert classify("I miss you, send hugs")["name"] != "email_send"


def test_generic_do_it_is_not_execute_last_intent():
    assert classify("do it")["name"] == "none"


def test_find_all_info_is_not_direct_shell_and_lan_queries_route_to_lan_clients():
    assert classify("Find all info about smap-r")["name"] != "direct_shell"
    assert classify("Show all LAN client devices discovered on my network")["name"] == "lan_clients"


def test_web_search_queries_do_not_route_to_direct_shell():
    assert classify("Find it in the web")["name"] == "web_search"
    assert classify("Find all information regarding smap-r on internet")["name"] == "web_search"
