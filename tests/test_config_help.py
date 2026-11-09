import os
import re

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(PLUGIN_DIR, 'POLYGLOT_CONFIG.md')

# The eisy-ui help panel treats text containing any of these tags as HTML and sanitizes it, which drops
# target="_blank" from links. Text without them is rendered as Markdown, whose links open in a new tab.
HTML_TAG = re.compile(r'<(p|div|span|h[1-6]|ul|ol|li|br|strong|em|a|img|table|thead|tbody|tr|th|td|blockquote|pre|code)[\s/>]', re.I)


def _text():
    with open(DOC, 'r', encoding='utf-8') as fh:
        return fh.read()


def test_help_file_has_no_html_tags_so_the_panel_renders_it_as_markdown():
    assert not HTML_TAG.search(_text())


def test_help_file_links_are_markdown_links_and_help_is_sent_as_raw_markdown():
    assert re.search(r'\[[^\]]+\]\(https?://[^)\s]+\)', _text())
    with open(os.path.join(PLUGIN_DIR, 'plugin.py'), 'r', encoding='utf-8') as fh:
        src = fh.read()
    assert 'setCustomParamsDoc(text)' in src
    assert 'getMarkDownData' not in src
